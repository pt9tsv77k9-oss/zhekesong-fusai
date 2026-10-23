"""联网结果只用于选择问题角度，不进入产品事实提取与资料库。"""

from dataclasses import dataclass
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import urljoin, urlsplit
import json
import re
import xml.etree.ElementTree as ET

import requests
from pydantic import BaseModel, Field

from evidence_utils import timestamp
from schemas import InspirationSelection
from product_types import is_phone


# 模型只能在中性的选题目录里选择，不能把搜索摘要改写成产品结论。
TOPICS = {
    "study": ("课程与学习清单", "开学选电脑，先把课程和常用软件清单列出来。"),
    "travel": ("出差行李清单", "下次出差准备带电脑处理哪些任务？先列行李与工作需求。"),
    "office": ("换机前的工作清单", "换办公电脑之前，先核对每天的文档、会议和软件需求。"),
    "creative": ("创作工具核对清单", "想做草图或批注，选电脑前要核对哪些软件和笔型需求？"),
    "compare": ("到店选购提问", "到店选电脑，有哪些问题值得先问清楚？"),
    "season": ("近期计划与选购", "最近有哪些学习或工作计划，会影响这次电脑选购？"),
    "phone_daily": ("换手机需求清单", "换手机前，先列常用应用、存储与握持需求。"),
    "phone_photo": ("旅行拍摄清单", "准备带手机拍哪些画面？先列场景，再核对本型号镜头规格。"),
    "phone_work": ("通勤与办公应用", "通勤和出差需要手机处理什么？先核对应用与系统版本。"),
    "phone_compare": ("到店选手机提问", "到店选手机，屏幕、重量与操作习惯要问清哪些事？"),
}


class SearchError(RuntimeError):
    def __init__(self, message: str, code: str = "unavailable", attempts=()):
        super().__init__(message)
        self.code = code
        self.attempts = tuple(attempts)


@dataclass(frozen=True)
class SearchAttempt:
    provider: str
    status: str
    received: int = 0
    accepted: int = 0


@dataclass(frozen=True)
class WebHit:
    title: str
    url: str
    snippet: str


@dataclass(frozen=True)
class SearchBatch:
    query: str
    fetched_at: str
    hits: list[WebHit]
    provider: str = ""
    attempts: tuple[SearchAttempt, ...] = ()


class TopicRecommendations(BaseModel):
    angle_ids: list[str] = Field(min_length=1, max_length=3)


def public_web_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(parsed.scheme == "https" and parsed.hostname and not parsed.username
                    and not parsed.password and not parsed.port and "." in parsed.hostname
                    and not parsed.hostname.endswith((".local", ".internal")))
    except ValueError:
        return False


def matches_query(query: str, title: str, snippet: str) -> bool:
    """基础相关性门槛，排除公开搜索返回的整批无关链接。"""
    ignored = {"常见", "见问", "问题", "如何", "怎么", "哪些", "用户", "需求", "最新", "最近",
               "清单", "指南", "攻略", "推荐", "选购", "the", "and", "for"}
    groups = []
    for word in re.findall(r"[a-zA-Z][a-zA-Z0-9-]{2,}|[\u4e00-\u9fff]{2,}", query.casefold()):
        tokens = ({word} if word.isascii() else
                  {word[index:index + 2] for index in range(len(word) - 1)}) - ignored
        if tokens:
            groups.append(tokens)
    keywords = set().union(*groups) if groups else set()
    heading = title.casefold()
    text = heading + " " + snippet.casefold()
    matched = {word for word in keywords if word in text}
    # 搜索首页/翻译工具的摘要可能偶然提到电脑，标题也须涉及主题。
    return bool(keywords and any(word in heading for word in keywords)
                and len(matched) >= (2 if len(keywords) >= 5 else 1)
                and sum(bool(group & matched) for group in groups) >= min(2, len(groups)))


PROVIDER_NAMES = {"tavily": "Tavily", "sogou": "搜狗", "bing": "Bing"}
SEARCH_BUDGET_SECONDS = 20


class SogouResults(HTMLParser):
    """只取普通结果标题链接，忽略搜索页的 AI 回答、脚本及图片。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hits = []
        self.in_heading = self.in_link = False
        self.url = ""
        self.title = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "h3":
            self.in_heading = "vr-title" in (attrs.get("class") or "").split()
            self.in_link = False
            self.url, self.title = "", []
        elif tag == "a" and self.in_heading and not self.url and attrs.get("href"):
            self.url = urljoin("https://www.sogou.com", attrs["href"])
            self.in_link = True

    def handle_data(self, data):
        if self.in_heading and self.in_link:
            self.title.append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self.in_link = False
        elif tag == "h3" and self.in_heading:
            title = " ".join("".join(self.title).split())
            if title and self.url:
                self.hits.append(WebHit(title, self.url, ""))
            self.in_heading = False


def _read_response(response, deadline: float, limit: int) -> bytes:
    status = response.status_code
    if status in (401, 403):
        raise SearchError("搜索服务拒绝访问，请检查服务配置或稍后再试。", "access_denied")
    if status in (402, 429):
        raise SearchError("搜索服务额度不足或请求过于频繁。", "rate_limited")
    response.raise_for_status()
    if status != 200:
        raise SearchError("搜索服务返回验证页面或异常响应。", "invalid_response")
    data = bytearray()
    for chunk in response.iter_content(16384):
        data.extend(chunk)
        if len(data) > limit:
            raise SearchError("搜索响应过大，本次未采用任何联网内容。", "invalid_response")
        if monotonic() >= deadline:
            raise SearchError("本次搜索等待时间已到。", "timeout")
    return bytes(data)


def _fetch_provider(provider, query, client, settings, deadline) -> list[WebHit]:
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise SearchError("本次搜索等待时间已到。", "timeout")
    headers = {"User-Agent": "Mozilla/5.0 (compatible; StoreMarketingResearch/2.1)",
               "Accept-Encoding": "gzip, deflate"}
    kwargs = {"headers": headers, "timeout": (min(4, remaining), min(7, remaining)),
              "stream": True, "allow_redirects": False}
    if provider == "tavily":
        if not settings or not settings.tavily_api_key:
            raise SearchError("尚未配置搜索服务密钥。", "not_configured")
        headers["Authorization"] = "Bearer " + settings.tavily_api_key
        response = client.post("https://api.tavily.com/search", json={
            "query": query, "topic": "general", "search_depth": "basic", "max_results": 6,
            "include_answer": False, "include_raw_content": False,
        }, **kwargs)
    elif provider == "sogou":
        response = client.get("https://www.sogou.com/web", params={"query": query}, **kwargs)
    else:
        response = client.get("https://www.bing.com/search",
                              params={"q": query, "format": "rss", "mkt": "zh-CN"}, **kwargs)
    with response:
        data = _read_response(response, deadline, 1_000_000 if provider == "sogou" else 600_000)
    if provider == "tavily":
        payload = json.loads(data)
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise SearchError("搜索服务返回格式异常。", "invalid_response")
        hits = []
        for row in rows:
            fields = [row.get(key, "") for key in ("title", "url", "content")]
            if not all(isinstance(value, str) for value in fields):
                raise SearchError("搜索服务返回格式异常。", "invalid_response")
            hits.append(WebHit(*fields))
        return hits
    if provider == "sogou":
        page = data.decode("utf-8", errors="strict")
        if "<title>搜狗搜索" in page and any(word in page for word in ("验证码", "访问异常")):
            raise SearchError("搜索服务需要人工验证。", "access_denied")
        parser = SogouResults()
        parser.feed(page)
        if not parser.hits:
            # 页面结构变化或验证页不能被记成“没有相关网页”。
            raise SearchError("未能读取搜索结果，搜索页面可能已变化或要求验证。", "invalid_response")
        return parser.hits
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise SearchError("搜索返回格式异常。", "invalid_response")
    root = ET.fromstring(data)
    if root.tag != "rss" or root.find("channel") is None:
        raise SearchError("搜索返回格式异常。", "invalid_response")
    return [WebHit((item.findtext("title") or "").strip(),
                   (item.findtext("link") or "").strip(),
                   (item.findtext("description") or "").strip())
            for item in root.findall("./channel/item")]


def _relevant_hits(query: str, candidates: list[WebHit]) -> list[WebHit]:
    hits, seen = [], set()
    for candidate in candidates:
        title, url, snippet = candidate.title.strip()[:200], candidate.url.strip(), candidate.snippet.strip()[:800]
        if (url in seen or len(url) > 2000 or not public_web_url(url) or not title
                or not matches_query(query, title, snippet)):
            continue
        seen.add(url)
        hits.append(WebHit(title, url, snippet))
        if len(hits) >= 6:
            break
    return hits


def search_topics(query: str, session=None, *, settings=None) -> SearchBatch:
    query = query.strip()
    if not query or len(query) > 120:
        raise SearchError("请输入不超过120字的选题关键词。", "invalid_query")
    provider = settings.web_search_provider if settings else "auto"
    if provider not in {"auto", *PROVIDER_NAMES}:
        raise SearchError("搜索服务配置无效，请检查 WEB_SEARCH_PROVIDER。", "invalid_config")
    providers = (["tavily"] if settings and settings.tavily_api_key else []) + ["sogou", "bing"]
    if provider != "auto":
        providers = [provider]
    client = session or requests
    attempts = []
    deadline = monotonic() + SEARCH_BUDGET_SECONDS
    last_error = None
    for current in providers:
        if monotonic() >= deadline:
            break
        try:
            candidates = _fetch_provider(current, query, client, settings, deadline)
            hits = _relevant_hits(query, candidates)
            status = "ok" if hits else ("irrelevant_results" if candidates else "no_results")
            attempts.append(SearchAttempt(PROVIDER_NAMES[current], status, len(candidates), len(hits)))
            if hits:
                return SearchBatch(query, timestamp(), hits, PROVIDER_NAMES[current], tuple(attempts))
        except (requests.RequestException, ET.ParseError, ValueError, SearchError) as exc:
            if isinstance(exc, SearchError):
                last_error = exc
            elif isinstance(exc, requests.Timeout):
                last_error = SearchError("搜索服务响应超时。", "timeout")
            elif isinstance(exc, requests.RequestException):
                last_error = SearchError("暂时无法连接搜索服务。", "unavailable")
            else:
                last_error = SearchError("搜索服务返回格式异常。", "invalid_response")
            attempts.append(SearchAttempt(PROVIDER_NAMES[current], last_error.code))
    statuses = {attempt.status for attempt in attempts}
    if "irrelevant_results" in statuses:
        message, code = "搜索服务返回的内容与关键词不相关，已过滤。", "irrelevant_results"
    elif statuses == {"no_results"}:
        message, code = "搜索服务未返回结果。", "no_results"
    else:
        message = str(last_error) if last_error else "联网搜索暂不可用。"
        code = last_error.code if last_error else "unavailable"
    raise SearchError(message, code, attempts)


def local_topic_ids(audience: str, scenario: str, product_model="") -> list[str]:
    text = audience + " " + scenario
    if is_phone(product_model):
        preferred = []
        if any(word in text for word in ("拍摄", "拍照", "旅行", "出游")):
            preferred.append("phone_photo")
        if any(word in text for word in ("通勤", "办公", "出差")):
            preferred.append("phone_work")
        return list(dict.fromkeys([*preferred, "phone_daily", "phone_compare"]))[:3]
    groups = {"study": ("学生", "新生", "大学", "课程", "开学", "学习"),
              "travel": ("出差", "差旅", "行李", "旅行", "通勤"),
              "creative": ("创作", "设计", "画画", "草图", "批注"),
              "office": ("办公", "职场", "同事", "工作", "会议")}
    preferred = [key for key, words in groups.items() if any(word in text for word in words)]
    return list(dict.fromkeys([*preferred, "compare", "office", "season"]))[:3]


def select_local_inspiration(angle_id: str) -> InspirationSelection:
    if angle_id not in TOPICS:
        raise SearchError("选题方向已失效，请重新选择。")
    return InspirationSelection(angle_id=angle_id, query="", fetched_at="", source="local")


def recommend_topics(batch: SearchBatch, audience: str, engine, product_model="") -> list[str]:
    available = {key: value[1] for key, value in TOPICS.items()
                 if key.startswith("phone_") == is_phone(product_model)}
    result = engine.run("inspire", TopicRecommendations, {
        "audience": audience, "query": batch.query,
        "search_results_untrusted": [vars(hit) for hit in batch.hits],
        "available_angles": available,
    })
    ids = list(dict.fromkeys(key for key in result.angle_ids if key in available))
    if not ids:
        raise SearchError("没有得到有效的选题方向，请从现有场景中选择。")
    return ids


def select_inspiration(batch: SearchBatch, angle_id: str) -> InspirationSelection:
    if angle_id not in TOPICS:
        raise SearchError("选题方向已失效，请重新选择。")
    return InspirationSelection(angle_id=angle_id, query=batch.query, fetched_at=batch.fetched_at,
                                references=[{"title": h.title, "url": h.url} for h in batch.hits],
                                provider=batch.provider)


def creative_context(selection: InspirationSelection | None) -> dict:
    if selection is None:
        return {}
    if selection.angle_id not in TOPICS:
        raise SearchError("联网选题编号无效，请重新选择。")
    return {"question": TOPICS[selection.angle_id][1],
            "boundary": "此问题仅供表达切入，不提供产品事实。产品能力、参数与承诺只能来自已核对facts。"}

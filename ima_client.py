"""ima 适配器：调用官方 Node 脚本，搜索、读原文、取图片和新建笔记。

不同产品始终使用各自选中的目录；新建笔记只在用户点击保存时调用。

可以把这个文件看成“翻译员”：网页使用 Python 对象，本文件把它们转成
ima OpenAPI 的请求。实际请求由 tools/ima_bridge.cjs 调用官方 Skill 发出。
返回的原文/图片整理成 schemas.py 中的 Evidence，再交给工作流程。
"""

from collections import deque  # 队列，用于逐层寻找产品根目录。
from io import BytesIO, StringIO  # 把内存中的字节/文字当成文件读取，不必先存盘。
from pathlib import Path
from urllib.parse import urlparse
import csv
import ipaddress
import json
import os
import re
import subprocess  # 从 Python 启动 Node.js 子进程，执行官方 JavaScript 接口。

import requests  # 用 HTTP 下载原始资料和图片。
from PIL import Image  # Pillow 提供的图片格式验证工具，不执行视觉型号识别。

from config import ROOT, Settings
from node_runtime import find_node
from schemas import Evidence, ImageAsset, Source, normalize_model, Task, Strategy, RetrievalTrace
from evidence_utils import text_fingerprint, timestamp
from product_types import is_phone


class ImaError(RuntimeError):
    """可以直接在界面显示的接入错误。"""


def model_from_document(text: str, fallback: str) -> str:
    """从一级标题取型号，只去掉已知的文档类型后缀，保留系统、年份等信息。"""
    # 正则：^# 找 Markdown 一级标题；MULTILINE 使 ^ 匹配每一行的开头。
    # group(1) 取括号捕获的标题正文；未找到可靠产品标题就保留 fallback。
    heading = re.search(r"^#\s+(.+)", text, re.MULTILINE)
    if not heading or not re.search(r"HUAWEI|华为|MateBook|擎云", heading.group(1), re.I):
        return fallback
    title = heading.group(1).strip()
    # 实际资料使用“型号 - 产品事实”，不能把“产品事实”当成型号的一部分。
    # 只删除明确的文档标签，避免误删 MateBook 的版本和配置。
    title = re.sub(r"\s*(?:\s[-－—–]\s|[｜|])\s*(?:产品事实|卖点与限制|产品参数|产品资料|产品介绍)\s*$", "", title).strip()
    return re.sub(r"\s*产品介绍与参数\s*$", "", title).strip()


class ImaClient:
    """一个连接实例：记住当前知识库，并缓存已读取的目录/媒体元信息。"""

    def __init__(self, settings: Settings):
        # 实例属性通过 self.xxx 使用；不同实例互不共享这两个缓存。
        # 以 _ 开头表示内部细节，不建议在界面里直接修改。
        self.settings = settings
        self.kb_id = ""
        self.kb_name = ""
        self._folders: dict[str, list[dict]] = {}
        self._media: dict[str, dict] = {}
        # 原文缓存仅存在当前客户端内存；不把整库资料落盘。刷新时全部清除。
        self._texts: dict[str, tuple[str, str]] = {}

    def refresh_cache(self):
        """显式刷新原文与目录。缓存不代表举办方资料始终最新。"""
        self._folders.clear()
        self._media.clear()
        self._texts.clear()

    def call(self, endpoint: str, body: dict) -> dict:
        """只开放资料读取和新建笔记；凭证走环境变量，正文走 UTF-8 标准输入。"""
        # endpoint 是接口路径，body 是请求字段；把底层调用集中在这里，
        # 上面的业务方法就不用重复处理进程、编码、错误和凭证。
        # 白名单中的 import_doc 只用于新建个人笔记，没有开放知识库文件写入等接口。
        allowed = {
            "wiki/v1/search_knowledge_base", "wiki/v1/get_knowledge_base",
            "wiki/v1/get_knowledge_list", "wiki/v1/search_knowledge",
            "wiki/v1/get_media_info", "note/v1/get_doc_content", "note/v1/import_doc",
        }
        if endpoint not in allowed:
            raise ImaError("仅开放资料读取和新建笔记操作。")
        # 查找系统 Node、Windows 常见位置，以及 pip 安装的云端备用运行时。
        node = find_node()
        if not node:
            raise ImaError(
                "找不到 Node.js。云端请更新 requirements.txt 并重新构建空间，"
                "确认已安装 nodejs-wheel-binaries；本地也可安装 Node.js LTS。"
                "详细步骤见 README.md 的快速开始部分。"
            )
        if not self.settings.skill_path.is_file():
            raise ImaError("找不到官方 ima 脚本，请检查 .env 的 IMA_SKILL_PATH。")
        try:
            # json.dumps 把 Python 字典变成 JSON 文本；先检查能否编码，
            # 防止特殊的无效字符等到 Node 进程启动后才报错。
            request_body = json.dumps(body, ensure_ascii=False)
            request_body.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise ImaError("内容包含无法编码为 UTF-8 的字符，请检查标题与正文后重试。") from exc
        env = os.environ.copy()
        # 给子进程的是环境副本，不修改整个应用的系统环境。
        # 强制官方地址，避免把凭证发送到继承环境中的其他地址。
        env["IMA_BASE_URL"] = "https://ima.qq.com"
        if self.settings.ima_client_id:
            # 同时设置新/旧变量名是为了兼容官方脚本；二者使用同一份值。
            env["IMA_OPENAPI_CLIENTID"] = self.settings.ima_client_id
            env["IMA_CLIENT_ID"] = self.settings.ima_client_id
        if self.settings.ima_api_key:
            env["IMA_OPENAPI_APIKEY"] = self.settings.ima_api_key
            env["IMA_API_KEY"] = self.settings.ima_api_key
        try:
            # 参数使用列表，shell=False 不让命令解释器拼接/执行正文。
            # input 通过 stdin（标准输入管道）发送长篇中文，避开 Windows 参数长度限制。
            # capture_output 接收 stdout/stderr；45 秒到时由 Python 中止等待。
            process = subprocess.run(
                [node, str(ROOT / "tools/ima_bridge.cjs"), str(self.settings.skill_path),
                 "openapi/" + endpoint],
                input=request_body,
                shell=False, capture_output=True, text=True, encoding="utf-8",
                errors="replace", env=env, timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            if endpoint == "note/v1/import_doc":
                # 超时只表示没收到结果，服务器可能已经写入；所以绝不自动重试新建。
                raise ImaError("笔记请求超时，可能已经创建。请先在 ima 客户端检查，避免重复新建。") from exc
            raise ImaError("ima 请求超时，请检查网络后重试。") from exc
        except OSError as exc:
            raise ImaError("无法启动官方 ima 脚本，请检查 Node.js 和脚本路径。") from exc
        if process.returncode:
            # returncode 非 0 是脚本执行失败；stderr 用来传递结构化错误。
            try:
                error = json.loads(process.stderr)
                message = str(error.get("msg", "ima 脚本执行失败。"))
            except (ValueError, TypeError):
                message = "ima 脚本执行失败，请检查 Node.js、网络及凭证配置。"
            # 不显示更新响应中的原始 instruction 或完整诊断对象。
            if "新版本" in message:
                message = "官方 ima Skill 检测到更新，请按官方指引更新 vendor 中的完整 Skill 后重试。"
            raise ImaError(message[:500])
        try:
            # stdout 是脚本成功时的 JSON 返回；不能混入调试 print，否则无法解析。
            response = json.loads(process.stdout)
        except ValueError as exc:
            raise ImaError("ima 返回了无法解析的数据，请稍后重试。") from exc
        if not isinstance(response, dict):
            raise ImaError("ima 返回的数据格式有误，请稍后重试。")
        # 脚本正常退出不等于业务成功，还必须检查后端 code。
        if response.get("code") != 0:
            raise ImaError(str(response.get("msg") or "ima 接口调用失败。")[:500])
        return response.get("data") or {}
        # 调用方只接收 data 部分；缺失/空值时用空字典，统一后面的读取方式。

    def create_note(self, content: str, title: str = "") -> str:
        """在凭证所属账号的默认笔记位置新建 Markdown 笔记，不关联共享知识库。"""
        if not content.strip():
            raise ImaError("请先填写笔记正文。")
        if re.search(r"!\[|<img\b|data:image/", content, re.I):
            # 首版仅支持文字 Markdown，拒绝内嵌图片语法，避免保存后图片不可用。
            raise ImaError("当前入口支持文字笔记；图片请在 ima 客户端添加。")
        # import_doc 没有独立 title 参数，以 Markdown 一级标题设置标题。
        heading = " ".join(title.split())
        # split/join 把标题中的换行和多余空白压成空格，保持单行标题。
        markdown = f"# {heading}\n\n{content}" if heading else content
        data = self.call("note/v1/import_doc", {"content_format": 1, "content": markdown})
        # content_format=1 是该接口约定的 Markdown 格式编号。
        note_id = data.get("note_id")
        if not isinstance(note_id, str) or not note_id:
            raise ImaError("ima 未返回新笔记标识。请先在 ima 客户端检查是否已创建，避免重复提交。")
        return note_id

    def paged(self, endpoint: str, body: dict, key: str) -> list[dict]:
        """读完游标分页；异常游标立即报错，避免误把不完整目录当作全部内容。"""
        # 接口常常一次只返一部分内容。cursor 类似“下一页书签”，key 指结果列表字段。
        # items 累积每页；visited 记录用过的书签；最多 60 页防止死循环。
        items, cursor, visited = [], "", set()
        for _ in range(60):
            data = self.call(endpoint, {**body, "cursor": cursor})
            # **body 展开原请求，再加上本轮 cursor；不直接修改调用方传入的字典。
            items.extend(data.get(key) or [])
            if data.get("is_end", False):
                return items
            next_cursor = data.get("next_cursor", "")
            if not next_cursor:
                return items
            if next_cursor in visited:
                raise ImaError("ima 返回重复分页游标，请重试。")
            visited.add(next_cursor)
            cursor = next_cursor
        raise ImaError("目录分页超过首版处理上限，请选择更具体的目录。")

    def connect(self) -> str:
        """按配置名称搜索并锁定唯一知识库，返回成功读取到的名称。"""
        matches = self.paged("wiki/v1/search_knowledge_base", {
            "query": self.settings.kb_name, "limit": 20,
        }, "info_list")
        exact = [x for x in matches if (x.get("kb_name") or x.get("name")) == self.settings.kb_name]
        # 模糊搜索可能命中多个库，所以必须精确名称且只匹配一个。
        # get(...)/or 兼容接口中的不同字段名；不能仅因搜索有结果就当连接成功。
        if len(exact) != 1:
            raise ImaError("未找到唯一的指定知识库。请确认账号已加入共享知识库，并核对 .env 中的名称。")
        item = exact[0]
        self.kb_id = item.get("kb_id") or item.get("id") or ""
        self.kb_name = item.get("kb_name") or item.get("name") or ""
        if not self.kb_id:
            raise ImaError("知识库返回缺少标识字段，请检查接口版本。")
        return self.kb_name

    @staticmethod
    def is_folder(item: dict) -> bool:
        """根据接口标记判断条目是否是文件夹；99 是接口的目录类型编号。"""
        return item.get("media_type") == 99 or bool(item.get("folder_id"))

    @staticmethod
    def entry_id(item: dict) -> str:
        """统一取得条目标识，兼容 media_id/folder_id 两种字段。"""
        return item.get("media_id") or item.get("folder_id") or ""

    def list_folder(self, folder_id: str = "") -> list[dict]:
        """列出某层目录；空 ID 表示知识库根目录，同一实例内重复读取会使用缓存。"""
        # 缓存减少页面反复运行时的请求；左侧重新连接会新建实例，进而清空缓存。
        if folder_id not in self._folders:
            body = {"knowledge_base_id": self.kb_id, "limit": 50}
            if folder_id:
                body["folder_id"] = folder_id
            self._folders[folder_id] = self.paged("wiki/v1/get_knowledge_list", body, "knowledge_list")
        return self._folders[folder_id]

    def product_root(self) -> dict:
        """兼容实际知识库多包一层同名目录的情况。"""
        # 广度优先搜索：先看当前层，再看下一层，而不是一条路径走到底。
        # 队列保存 (目录ID, 深度)；seen 防止异常目录结构造成重复遍历。
        queue = deque([("", 0)])
        seen = set()
        while queue:
            folder_id, depth = queue.popleft()
            # popleft 从队首取出最早加入的一层；append/extend 在队尾放新目录。
            if folder_id in seen:
                continue
            seen.add(folder_id)
            children = self.list_folder(folder_id)
            for item in children:
                title = item.get("title") or item.get("name", "")
                if self.is_folder(item) and title.startswith("P1-"):
                    return item
            if depth < 2:
                queue.extend((self.entry_id(x), depth + 1) for x in children if self.is_folder(x))
        raise ImaError("目录中未找到 P1-产品参数库，请在 ima 客户端确认资料是否可见。")

    def search(self, product: str) -> list[dict]:
        """在当前知识库内按关键词搜索；不会自动把命中结果当成产品事实依据。"""
        return self.paged("wiki/v1/search_knowledge", {
            "query": product, "knowledge_base_id": self.kb_id,
        }, "info_list")

    def phone_products(self, root: dict) -> tuple[list[dict], list[str]]:
        """从扩展目录发现散放的手机文档；不移动或修改知识库。"""
        products, excluded = [], []
        folders = [x for x in self.list_folder(self.entry_id(root))
                   if self.is_folder(x) and (x.get("title", "").startswith("07-")
                                             or "手机" in x.get("title", ""))]
        for folder in folders:
            for item in self.list_folder(self.entry_id(folder)):
                name = item.get("title", "")
                if self.is_folder(item) or not re.search(r"(?:华为|HUAWEI).*Mate\s*\d|(?:华为|HUAWEI).*(?:Pura|nova)\s*\d", name, re.I):
                    continue
                if not name.lower().endswith((".md", ".txt")):
                    continue
                model = model_from_document(self.read_text(item), "")
                # 对比和多版本资料不能装成某一个型号的专属依据。
                if (not is_phone(model) or re.search(r"及|系列|对比|选购|、|/|与", model)
                        or re.search(r"系列对比|及典藏版", name)):
                    excluded.append(name)
                    continue
                products.append({**item, "title": model, "document_title": name,
                                 "source_folder_id": self.entry_id(folder),
                                 "source_folder_title": folder.get("title", ""),
                                 "product_document": True})
        return sorted(products, key=lambda x: x["title"]), excluded

    def media_info(self, media_id: str) -> dict:
        """取得条目的下载链接/类型等元信息，并按 ID 缓存；此处还未下载文件内容。"""
        if media_id not in self._media:
            self._media[media_id] = self.call("wiki/v1/get_media_info", {"media_id": media_id})
        return self._media[media_id]

    @staticmethod
    def download(url_info: dict, limit: int = 8_000_000) -> tuple[bytes, str]:
        """使用接口返回的访问头下载；跨域跳转时丢弃访问头，不转发 ima 密钥。"""
        # 返回 (文件字节, 内容类型)。limit 默认 800 万字节，避免单文件过大。
        # 下载链接可能需要它自己的访问头，但不能带 ima API 密钥或手工 Host。
        url = url_info.get("url", "")
        headers = {k: v for k, v in (url_info.get("headers") or {}).items()
                   if k.lower() not in {"ima-openapi-clientid", "ima-openapi-apikey", "host"}}
        try:
            for _ in range(4):
                # 手动处理最多 4 轮访问（包含跳转），每轮都重新检查 HTTPS 地址。
                parsed = urlparse(url)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username:
                    raise ImaError("资料访问链接无效，请使用 ima 客户端查看原文。")
                try:
                    # ip_address 能解析 IP 字面值；拒绝本机/私有 IP 字面值。
                    # 若是普通域名会抛 ValueError，再单独排除 localhost 名称。
                    if not ipaddress.ip_address(parsed.hostname).is_global:
                        raise ImaError("资料访问地址无效。")
                except ValueError:
                    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
                        raise ImaError("资料访问地址无效。")
                with requests.get(url, headers=headers, timeout=(8, 25), stream=True, allow_redirects=False) as response:
                    # timeout=(连接等待, 单次读取等待)；stream 按块读取，不一次全放进内存。
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        new_url = urljoin(url, response.headers.get("Location", ""))
                        if urlparse(new_url).netloc != parsed.netloc:
                            # 跳到不同主机时，原站的访问头不能继续转发。
                            headers = {}
                        url = new_url
                        continue
                    response.raise_for_status()
                    # 4xx/5xx 在此变成异常；读取时再逐块累计大小。
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > limit:
                            raise ImaError("资料超过首版读取大小上限，请使用 ima 客户端查看原文。")
                        chunks.append(chunk)
                    return b"".join(chunks), response.headers.get("Content-Type", "")
        except requests.RequestException as exc:
            # requests 原始异常可能带签名 URL，因此不直接显示异常文本。
            raise ImaError("资料链接读取失败或已过期，请重新连接或使用 ima 客户端查看原文。") from exc
        raise ImaError("资料链接跳转次数过多，请使用 ima 客户端查看原文。")

    def read_text(self, item: dict) -> str:
        """相同条目在本次连接内复用原文；读取时间同时缓存，避免伪装成刚更新。"""
        media_id = self.entry_id(item)
        if not media_id:
            raise ImaError("资料缺少条目标识，请重新连接知识库。")
        if media_id not in self._texts:
            self._texts[media_id] = (self._read_text_uncached(item), timestamp())
        return self._texts[media_id][0]

    def _read_text_uncached(self, item: dict) -> str:
        """读取一项资料的文字：笔记走正文接口，文件下载后按 PDF/文本处理。"""
        data = self.media_info(self.entry_id(item))
        if data.get("media_type") == 11:
            # 11 是 ima 笔记类型；notebook_id 用于换取正文，并非文件下载链接。
            note_id = (data.get("notebook_ext_info") or {}).get("notebook_id")
            if not note_id:
                raise ImaError("笔记缺少原文信息，请使用 ima 客户端查看。")
            return self.call("note/v1/get_doc_content", {"note_id": note_id, "target_content_format": 0}).get("content", "")
        if not (data.get("url_info") or {}).get("url"):
            raise ImaError("该资料无可用访问链接，请使用 ima 客户端查看原文。")
        raw, content_type = self.download(data["url_info"])
        title = item.get("title", "").lower()
        if "pdf" in content_type or title.endswith(".pdf"):
            # PDF 需要 BytesIO 包装字节；只提取前 25 页文本，不做扫描件 OCR。
            # 导入写在分支内，只有读 PDF 时才加载这个库。
            from pypdf import PdfReader
            from pypdf.errors import PyPdfError
            try:
                return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(raw)).pages[:25])
            except (PyPdfError, ValueError, OSError) as exc:
                raise ImaError("PDF 正文无法提取，请使用 ima 客户端查看原文。") from exc
        if "html" in content_type:
            raise ImaError("首版不解析网页正文，请在该产品目录选择 Markdown、文本或 PDF 资料。")
        if b"\x00" in raw[:1000]:
            # 前 1000 字节含空字节通常提示二进制；这是首版的保守文本判断。
            raise ImaError("该资料不是可读取的文本，请使用 ima 客户端查看原文。")
        for encoding in ("utf-8-sig", "gb18030"):
            # 先尝试常见 UTF-8（含 BOM），失败再尝试常见中文编码。
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                pass
        raise ImaError("资料编码无法识别，请使用 ima 客户端查看原文。")

    def retrieve_for_task(self, task: Task, strategy: Strategy, base: Evidence) -> Evidence:
        """按型号和任务搜索，再在所选产品目录白名单中取证。

        搜索摘要只用于发现与排序，不作事实。只接受当前目录中的真实文档，
        避免把同系列其他版本、历史营销稿或全库搜索结果直接混进依据。
        """
        if normalize_model(task.product_model) != normalize_model(base.product_model):
            raise ImaError("任务型号已变化，请重新加载对应产品。")
        if not base.folder_id:
            raise ImaError("缺少产品目录信息，请重新加载产品资料。")
        entries = self.list_folder(base.folder_id)
        candidates = {self.entry_id(x): x for x in entries
                      if not self.is_folder(x) and x.get("title", "").lower().endswith((".md", ".txt", ".pdf"))
                      and not re.search(r"历史|营销稿|软文|草稿|样稿", x.get("title", ""))}
        if base.document_id:
            candidates = {key: item for key, item in candidates.items() if key == base.document_id}
        if not candidates:
            raise ImaError("所选目录没有可用于本次取证的产品原文。")
        queries = list(dict.fromkeys([
            f"{task.product_model} {task.audience} {task.scenario} {task.platform}"[:200],
            f"{task.product_model} {' '.join(strategy.priority_points[:2])}"[:200],
        ]))
        traces, hit_ids = [], set()
        for query in queries:
            # 请求错误直接向上传递；只有真实成功的空列表才是“没有命中”。
            hits = self.search(query)
            accepted = {self.entry_id(hit) for hit in hits} & candidates.keys()
            hit_ids.update(accepted)
            traces.append(RetrievalTrace(query=query, hit_count=len(hits),
                                         accepted_titles=[candidates[key].get("title", "未命名资料") for key in sorted(accepted)],
                                         excluded_count=sum(self.entry_id(hit) not in candidates for hit in hits),
                                         fallback_used=not accepted))
        words = re.findall(r"屏幕|续航|便携|重量|协同|办公|学习|创作|配置|接口|价格|补贴|服务|性能|系统", 
                           task.scenario + task.requirements + " ".join(strategy.priority_points))

        def priority(item):
            name = item.get("title", "")
            # 基础产品事实始终优先，其余由搜索命中与任务关键词共同排序。
            primary = 0 if "产品事实" in name else 1 if name == "article.md" else 2
            return primary, -int(self.entry_id(item) in hit_ids), -sum(word in name for word in words), name

        sources, notices = [], list(base.notices)
        existing = {source.media_id: source for source in base.sources if source.media_id}
        for item in sorted(candidates.values(), key=priority)[:4]:
            media_id = self.entry_id(item)
            if media_id in existing:
                source = existing[media_id].model_copy(deep=True)
            else:
                text = self.read_text(item)
                if not text.strip():
                    notices.append(f"{item.get('title', '')} 未提取到正文，未采用。")
                    continue
                if len(text) > 24000:
                    notices.append(f"{item.get('title', '')} 仅读取前 24000 字。")
                text = text[:24000]
                source = Source(source_id="待分配", title=f"{base.folder_title}/{item.get('title', '')}",
                                text=text, media_id=media_id, text_hash=text_fingerprint(text),
                                loaded_at=self._texts.get(media_id, ("", timestamp()))[1])
            document_model = model_from_document(source.text, "")
            if document_model and normalize_model(document_model) != normalize_model(task.product_model):
                notices.append(f"{source.title} 的标题型号与当前完整型号不一致，已排除。")
                continue
            source.source_id = f"S{len(sources)+1}"
            sources.append(source)
        if not sources:
            raise ImaError("没有通过当前型号核对的原文，已停止生成。")
        if not hit_ids:
            notices.append("任务检索没有可采纳命中，已按当前产品目录回退；未使用其他型号或搜索摘要。")
        if len(candidates) > 4:
            notices.append("本次最多采用四份原文；其他目录资料未全部读取或核验。")
        return base.model_copy(deep=True, update={"sources": sources, "retrieval": traces,
                                                  "notices": list(dict.fromkeys(notices)), "kb_name": self.kb_name})

    def load_product(self, folder: dict) -> Evidence:
        """仅从用户明确选择的产品目录取证，杜绝把相近型号搜索结果直接混用。"""
        # 这是本模块的装配入口：目录 → 原文来源 → 型号 → 图片索引 → 图片 → Evidence。
        # notices 收集“可继续但需提醒”的问题；缺少关键原文则直接抛出异常。
        if folder.get("product_document"):
            return self.load_phone_document(folder)
        folder_id, title = self.entry_id(folder), folder.get("title", "")
        if title.startswith("07-"):
            raise ImaError("扩展目录包含多个型号，请选择具体产品；手机请从“手机”入口选择。")
        notices = []
        # 即使搜索无结果，也继续按已选目录读取（手册要求的回退路径）。
        hits = self.search(title)
        if not hits:
            notices.append("产品搜索没有命中，已按所选产品目录读取原文。")
        entries = self.list_folder(folder_id)
        text_items = [x for x in entries if x.get("title", "").lower().endswith((".md", ".txt", ".pdf"))
                      and not re.search(r"历史|营销稿|软文|草稿|样稿", x.get("title", ""))]
        priority = lambda x: (0 if "产品事实" in x.get("title", "") else 1 if x.get("title") == "article.md" else 2, x.get("title", ""))
        # 排序元组先按优先级、再按标题排序；首版最多读 4 份原文、每份 24000 字。
        sources = []
        for item in sorted(text_items, key=priority)[:4]:
            text = self.read_text(item)
            if text.strip():
                if len(text) > 24000:
                    notices.append(f"{item['title']} 较长，首版仅读取前 24000 字。")
                clipped = text[:24000]
                sources.append(Source(source_id=f"S{len(sources)+1}", title=f"{title}/{item['title']}", text=clipped,
                                      media_id=self.entry_id(item), text_hash=text_fingerprint(clipped),
                                      loaded_at=self._texts.get(self.entry_id(item), ("", timestamp()))[1]))
        if not sources:
            raise ImaError("当前目录没有可读取的产品原文。请继续展开到具体产品目录。")
        # 以产品事实文档的一级标题确认版本，不能仅凭文件夹名推断系统或配置。
        product_model = title
        for source in sources:
            parsed = model_from_document(source.text, "")
            if parsed:
                product_model = parsed
                break
        index_rows = {}
        # 图片索引按“文件名”建立字典，图片条目就能找到对应的型号和来源说明。
        index = next((x for x in entries if x.get("title") == "图片索引.csv"), None)
        if index:
            for row in csv.DictReader(StringIO(self.read_text(index).replace("\r\r\n", "\n"))):
                # DictReader 用 CSV 第一行做字段名，每行得到一个字典。
                index_rows[row.get("文件名", "")] = row
        images = []
        image_entries = [x for x in entries if x.get("media_type") == 9]
        # 9 是图片类型；同时查看当前目录与最多 3 个直接图片子目录，总共最多 8 张。
        image_folders = [x for x in entries if self.is_folder(x) and re.search(r"image|图片", x.get("title", ""), re.I)]
        for image_folder in image_folders[:3]:
            image_entries.extend(x for x in self.list_folder(self.entry_id(image_folder)) if x.get("media_type") == 9)
        for item in image_entries[:8]:
            row = index_rows.get(item.get("title", ""), {})
            matching = bool(row.get("产品型号")) and normalize_model(row["产品型号"]) == normalize_model(product_model)
            # 只有索引明确写了型号、且规范化后完全相同，才自动设 verified=True。
            # 没索引时显示当前型号作为待确认值，仍需人工核对，不能自动通过。
            # API 的权限/限额失败应立即停止，不能当作“这一张没有图片”后继续请求。
            # 只有已成功取得元信息后的文件下载/图片格式问题，才按坏素材排除。
            info = self.media_info(self.entry_id(item))
            try:
                raw, _ = self.download(info.get("url_info") or {})
                with Image.open(BytesIO(raw)) as check:
                    check.verify()  # 下载成功也可能返回 HTML，需验证真实图片格式。
                images.append(ImageAsset(
                    image_id=f"I{len(images)+1}", name=item["title"], product_model=row.get("产品型号") or product_model,
                    source_title=f"{title}/{item['title']}", description=row.get("官网图片说明", ""),
                    source_page=row.get("来源页面", ""), verified=matching, data=raw,
                    verification_method="图片索引型号匹配" if matching else "尚未核对",
                    verified_at=timestamp() if matching else "",
                ))
            except (ImaError, OSError, ValueError):
                notices.append(f"图片 {item.get('title', '')} 无法读取，已排除。")
        if any(not image.verified for image in images):
            notices.append("部分图片未能通过图片索引确认型号，需要人工核对后勾选确认。")
        return Evidence(product_model=product_model, folder_title=title, sources=sources, images=images,
                        notices=notices, folder_id=folder_id, kb_name=self.kb_name, loaded_at=timestamp())

    def load_phone_document(self, product: dict) -> Evidence:
        document_id = self.entry_id(product)
        folder_id = product.get("source_folder_id", "")
        item = next((x for x in self.list_folder(folder_id) if self.entry_id(x) == document_id), None)
        if not item or self.is_folder(item):
            raise ImaError("所选手机资料已变化，请刷新知识库后重新选择。")
        text = self.read_text(item)
        model = model_from_document(text, "")
        if (not is_phone(model) or normalize_model(model) != normalize_model(product["title"])
                or re.search(r"及|系列|对比|选购|、|/|与", model)):
            raise ImaError("手机原文未能确认唯一型号，请刷新后核对资料。")
        if len(text) > 24000:
            raise ImaError("单型号手机资料过长，请先按型号整理后再使用。")
        source = Source(source_id="S1", title=product["source_folder_title"] + "/" + item["title"],
                        text=text, media_id=document_id, text_hash=text_fingerprint(text),
                        loaded_at=self._texts.get(document_id, ("", timestamp()))[1])
        return Evidence(product_model=model, folder_title=model, folder_id=folder_id,
                        document_id=document_id, sources=[source], kb_name=self.kb_name,
                        loaded_at=timestamp(), notices=[
                            "手机按单型号原文取证，不采用同目录的系列对比或其他版本参数。",
                            "本批手机资料标注未人工复核；价格为有日期的快照，不能当作当前成交价。",
                            "当前手机文档未附产品原图，可补充本型号门店实拍。"])

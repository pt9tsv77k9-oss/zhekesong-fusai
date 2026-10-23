"""选题搜索容错、来源标注及失败后继续创作的回归验证。"""

import json
from types import SimpleNamespace

import pytest
import requests

from config import Settings
from exporter import to_markdown
from inspiration import (SearchAttempt, SearchBatch, SearchError, WebHit, creative_context,
                         local_topic_ids, search_topics, select_local_inspiration)
from test_app import button, configured_app, load_first_product
from test_core import evidence, extraction, task, FakeEngine
from workflow import run_workflow


QUERY = "大学新生 电脑选购 常见问题"
TITLE = "大学第一台电脑应该怎么选"
SOGOU = ('<!doctype html><html><h3>AI回答，不是网页结果</h3>'
         '<h3 class="vr-title"><a href="/link?url=reference">大学第一台'
         '<em>电脑</em>应该怎么选</a></h3></html>').encode()
RSS = ('<rss><channel><item><title>大学新生买电脑需要问什么</title>'
       '<link>https://example.org/student</link></item></channel></rss>').encode()


class Response:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("service error")

    def iter_content(self, size):
        yield self.data


def test_public_search_uses_only_relevant_organic_titles():
    client = SimpleNamespace(get=lambda *a, **k: Response(SOGOU))
    batch = search_topics(QUERY, client)
    assert batch.provider == "搜狗"
    assert batch.hits == [WebHit(TITLE, "https://www.sogou.com/link?url=reference", "")]


@pytest.mark.parametrize("failure", ["timeout", "irrelevant", "captcha", "bad-format"])
def test_auto_search_switches_provider_after_an_unusable_response(failure):
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        if "sogou" in url:
            if failure == "timeout":
                raise requests.Timeout()
            if failure == "captcha":
                return Response(b"<html>verification</html>", 403)
            if failure == "bad-format":
                return Response(b"<html>changed search page</html>")
            return Response(SOGOU.replace(TITLE.encode(), b"IBM 2FA").replace(
                "大学第一台<em>电脑</em>应该怎么选".encode(), b"IBM 2FA"))
        return Response(RSS)

    batch = search_topics(QUERY, SimpleNamespace(get=get))
    assert batch.provider == "Bing" and len(calls) == 2
    assert batch.attempts[0].status != "ok"
    assert batch.hits[0].url == "https://example.org/student"


def test_formal_api_is_preferred_and_never_requests_an_answer_or_full_page():
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(json.dumps({"results": [{"title": TITLE, "url": "https://example.org/a",
                                                 "content": "大学新生的电脑选购问题"}]}).encode())

    settings = Settings(tavily_api_key="test-secret-only")
    batch = search_topics(QUERY, SimpleNamespace(post=post), settings=settings)
    assert batch.provider == "Tavily"
    assert calls[0][1]["json"]["include_answer"] is False
    assert calls[0][1]["json"]["include_raw_content"] is False
    assert "test-secret-only" not in repr(settings) + repr(batch)


@pytest.mark.parametrize("status,code", [(401, "access_denied"), (429, "rate_limited")])
def test_api_error_is_classified_without_exposing_a_key(status, code):
    client = SimpleNamespace(post=lambda *a, **k: Response(b"private service detail", status))
    with pytest.raises(SearchError) as error:
        search_topics(QUERY, client, settings=Settings(web_search_provider="tavily", tavily_api_key="secret"))
    assert error.value.code == code
    assert "secret" not in str(error.value) and "private service detail" not in str(error.value)


@pytest.mark.parametrize("data", [b"[]", b'{"results":null}', b'{"results":[{"title":null}]}', b"<html/>"])
def test_malformed_api_data_is_rejected(data):
    client = SimpleNamespace(post=lambda *a, **k: Response(data))
    with pytest.raises(SearchError) as error:
        search_topics(QUERY, client, settings=Settings(web_search_provider="tavily", tavily_api_key="secret"))
    assert error.value.code == "invalid_response"


def test_missing_key_and_invalid_provider_fail_before_a_request():
    for provider, code in [("tavily", "not_configured"), ("typo", "invalid_config")]:
        with pytest.raises(SearchError) as error:
            search_topics(QUERY, SimpleNamespace(), settings=Settings(web_search_provider=provider))
        assert error.value.code == code


def test_irrelevant_hits_are_distinct_from_an_empty_search():
    for xml, code in [(b"<rss><channel/></rss>", "no_results"),
                      (b"<rss><channel><item><title>IBM 2FA</title><link>https://ibm.com/</link></item></channel></rss>",
                       "irrelevant_results")]:
        with pytest.raises(SearchError) as error:
            search_topics(QUERY, SimpleNamespace(get=lambda *a, **k: Response(xml)),
                          settings=Settings(web_search_provider="bing"))
        assert error.value.code == code


def test_search_budget_stops_fallback_requests(monkeypatch):
    times = iter([0, 0, 0, 21])
    monkeypatch.setattr("inspiration.monotonic", lambda: next(times))
    calls = []

    def get(*a, **k):
        calls.append(a)
        raise requests.Timeout()

    with pytest.raises(SearchError) as error:
        search_topics(QUERY, SimpleNamespace(get=get))
    assert len(calls) == 1 and error.value.code == "timeout"


def test_local_fallback_is_labeled_in_export_without_web_references(task, evidence, extraction):
    ids = local_topic_ids("大学新生", "开学选电脑")
    assert ids[0] == "study"
    task.inspiration = select_local_inspiration(ids[0])
    assert not task.inspiration.references and not task.inspiration.fetched_at
    assert "question" in creative_context(task.inspiration)
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    report = to_markdown(result)
    assert "本地选题记录" in report and "非实时热点" in report
    assert "联网选题记录" not in report and result.review.passed


def test_failed_search_keeps_local_choices_and_can_generate(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)

    def failed(*a, **k):
        raise SearchError("搜索服务返回的内容与关键词不相关，已过滤。", "irrelevant_results",
                          [SearchAttempt("搜狗", "irrelevant_results", 10)])

    monkeypatch.setattr("creation_ui.search_topics", failed)
    next(c for c in app.checkbox if c.label == "参考联网选题").check().run()
    button(app, "搜索选题").click().run()
    assert any("不相关" in warning.value for warning in app.warning)
    assert any("非实时热点" in caption.value for caption in app.caption)
    next(s for s in app.selectbox if s.label == "采用哪个问题切入").select("compare").run()
    assert any("不相关" in warning.value for warning in app.warning)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert not app.exception and app.session_state["result"].review.passed
    assert app.session_state["result"].task.inspiration.source == "local"


def test_local_choice_needs_no_search_and_task_change_clears_it(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)
    monkeypatch.setattr("creation_ui.search_topics", lambda *a, **k: pytest.fail("本地选题不应联网"))
    next(c for c in app.checkbox if c.label == "参考联网选题").check().run()
    button(app, "使用本地选题").click().run()
    next(s for s in app.selectbox if s.label == "采用哪个问题切入").select("compare").run()
    next(t for t in app.text_input if t.label == "目标人群 *").set_value("差旅用户").run()
    assert "web_bundle" not in app.session_state and not app.exception


def test_recommendation_failure_keeps_the_real_search_results(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)
    batch = SearchBatch(QUERY, "2026-10-23", [WebHit(TITLE, "https://example.org/a", "")], "搜狗")
    monkeypatch.setattr("creation_ui.search_topics", lambda *a, **k: batch)

    def failed(*a, **k):
        raise SearchError("推荐不可用")

    monkeypatch.setattr("creation_ui.recommend_topics", failed)
    next(c for c in app.checkbox if c.label == "参考联网选题").check().run()
    button(app, "搜索选题").click().run()
    assert app.session_state["web_bundle"]["batch"] == batch
    assert any("已找到相关网页" in warning.value for warning in app.warning)
    assert not app.exception

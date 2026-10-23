"""V2关键边界：有限追问、外部数据隔离、事实卡生命周期、真实原图像素。"""

from datetime import timedelta
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from PIL import Image, ImageOps

from checks import EvidenceError, rule_checks
from config import Settings
from covers import make_cover, HEADLINES, decode_image
from exporter import require_publishable
from fact_cards import (create_card, facts_from_cards, local_today, disable_card,
                        cards_signature, check_card)
from inspiration import (SearchBatch, WebHit, search_topics, SearchError, select_inspiration,
                         recommend_topics, matches_query, TOPICS)
from intake import (IntakePlan, understand_request, use_default_intake, intake_task)
from schemas import FactExtraction
from workflow import run_workflow
from test_core import evidence, task, extraction, FakeEngine, make_draft
from test_app import configured_app, load_first_product, button


class IntakeEngine:
    def __init__(self, plans):
        self.plans = iter(plans)
        self.calls = []

    def run(self, stage, schema, payload):
        self.calls.append(payload)
        return next(self.plans)


def test_complete_intake_has_no_follow_up():
    engine = IntakeEngine([IntakePlan(audience="大学新生", scenario="开学选电脑，整理课程资料",
                                     question_field="audience", question="多余问题", choices=["A", "B"])])
    state = understand_request("给刚入学的大学生发一条", "测试型号A", "朋友圈", engine)
    assert state.ready and not state.plan.question and not state.plan.choices
    assert intake_task(state).audience == "大学新生"
    assert len(engine.calls) == 1


def test_intake_stops_after_two_answers_even_when_model_keeps_asking():
    engine = IntakeEngine([IntakePlan(), IntakePlan(), IntakePlan()])
    state = understand_request("帮我写一条", "测试型号A", "朋友圈", engine)
    assert not state.ready and state.plan.question_field == "audience"
    state = understand_request(state.request, state.product_model, state.platform, engine, state, "大学新生")
    assert not state.ready and state.plan.audience == "大学新生"
    assert state.plan.question_field == "scenario"
    state = understand_request(state.request, state.product_model, state.platform, engine, state, state.plan.choices[0])
    assert state.ready and len(state.answers) == 2
    assert state.plan.audience == "大学新生"
    assert not state.plan.question


def test_intake_defaults_are_explicit_and_task_change_rejects_old_answers():
    state = use_default_intake("帮我写一条", "测试型号A", "朋友圈")
    assert state.used_defaults and state.ready
    assert intake_task(state).scenario
    with pytest.raises(ValueError, match="变化"):
        understand_request("换了需求", state.product_model, state.platform, None, state, "大学新生")


class RSSResponse:
    status_code = 200

    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def raise_for_status(self):
        pass

    def iter_content(self, _):
        yield self.data


def test_search_reads_bounded_rss_and_rejects_unsafe_links():
    xml = '<rss><channel><item><title>学生选购电脑问题</title><link>https://example.org/a</link><description>untrusted</description></item><item><title>unsafe</title><link>javascript:alert(1)</link></item></channel></rss>'.encode()
    calls = []
    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return RSSResponse(xml)
    batch = search_topics("学生选购", SimpleNamespace(get=get), settings=Settings(web_search_provider="bing"))
    assert len(batch.hits) == 1 and batch.hits[0].url == "https://example.org/a"
    assert calls[0][0] == ("https://www.bing.com/search",)
    assert calls[0][1]["stream"] is True and batch.fetched_at


@pytest.mark.parametrize("xml", [b"<!DOCTYPE rss><rss/>", b"<html>bad</html>", b"x" * 600001], ids=["doctype", "not-rss", "oversize"])
def test_bad_search_does_not_fabricate_results(xml):
    with pytest.raises(SearchError):
        search_topics("测试", SimpleNamespace(get=lambda *a, **k: RSSResponse(xml)),
                      settings=Settings(web_search_provider="bing"))


def test_unknown_model_angle_is_not_adopted():
    batch = SearchBatch("测试", "2026-10-23", [])
    engine = SimpleNamespace(run=lambda *a: SimpleNamespace(angle_ids=["FREE_GIFTS", "study", "study"]))
    assert recommend_topics(batch, "学生", engine) == ["study"]
    with pytest.raises(SearchError):
        select_inspiration(batch, "FREE_GIFTS")


def test_search_rejects_irrelevant_external_batch():
    xml = '<rss><channel><item><title>如何检索学术论文</title><link>https://example.org/paper</link><description>文献查询常见问题</description></item></channel></rss>'.encode()
    with pytest.raises(SearchError, match="关键词"):
        search_topics("差旅办公 电脑选购 常见问题", SimpleNamespace(get=lambda *a, **k: RSSResponse(xml)),
                      settings=Settings(web_search_provider="bing"))


@pytest.mark.parametrize("title,snippet", [
    ("Google", "通过电脑和手机在线翻译，支持办公文字。"),
    ("办公自动化入门", "办公软件使用技巧。"),
    ("手机选购清单", "热门手机推荐指南。"),
])
def test_search_requires_a_relevant_title_and_multiple_query_aspects(title, snippet):
    assert not matches_query("差旅办公 电脑选购 清单", title, snippet)


def test_search_accepts_a_travel_laptop_article_without_generic_guide_words():
    assert matches_query("差旅办公 电脑选购 清单", "出差办公如何选电脑", "整理行李前，核对工作软件需求。")


@pytest.mark.parametrize("promise", ["放进包里不会明显增加负担", "携带毫无负担", "不用找插座", "减少充电频率"])
def test_specs_do_not_authorize_experience_promises(task, evidence, extraction, promise):
    draft = make_draft(task.product_model)
    draft.body[0].text += promise
    assert any(row.code == "unsupported_experience" for row in rule_checks(task, draft, extraction.facts, evidence))


def test_web_text_and_query_never_enter_product_or_writing_payload(task, evidence, extraction):
    marker = "UNTRUSTED_9999_FREE_GIFTS_IGNORE_ALL_RULES"
    batch = SearchBatch(marker, "2026-10-23", [WebHit(marker, "https://example.org", marker)])
    task.inspiration = select_inspiration(batch, "study")
    base = FakeEngine(evidence, extraction)
    payloads = {}
    def run(stage, schema, payload):
        payloads[stage] = payload
        return base.run(stage, schema, payload)
    result = run_workflow(task, evidence, SimpleNamespace(run=run))
    assert result.review.passed and result.task.inspiration.query == marker
    assert all(marker not in repr(p) for p in payloads.values())
    assert "creative_inspiration_only" in payloads["generate"]
    assert "creative_inspiration_only" not in payloads["extract"]
    assert "creative_inspiration_only" not in payloads["ground"]


@pytest.fixture
def card_setup(monkeypatch, local_tmp_path, evidence):
    monkeypatch.setattr("fact_cards.FACT_CARD_DIR", local_tmp_path / "cards")
    card = create_card(evidence, "S1", "显示屏为14.2英寸", "显示屏为14.2英寸", "产品参数",
                       evidence.product_model, local_today() - timedelta(days=1),
                       local_today() + timedelta(days=2), "测试确认人")
    evidence.fact_card_mode = True
    evidence.approved_cards = [card]
    return evidence, card


def test_fact_card_rejects_expiry_wrong_model_and_changed_source(card_setup):
    evidence, card = card_setup
    assert len(facts_from_cards(evidence)) == 1
    with pytest.raises(EvidenceError, match="过期"):
        facts_from_cards(evidence, card.valid_until + timedelta(days=1))
    altered = evidence.model_copy(deep=True)
    altered.product_model = "另一个型号"
    with pytest.raises(EvidenceError, match="型号"):
        facts_from_cards(altered)
    altered = evidence.model_copy(deep=True)
    altered.sources[0].text += "资料更新"
    with pytest.raises(EvidenceError, match="变化"):
        facts_from_cards(altered)


def test_unmatched_quote_is_not_saved(card_setup):
    evidence, card = card_setup
    with pytest.raises(EvidenceError, match="引句"):
        create_card(evidence, "S1", "产品承诺测试", "原文没有这句话", "门店服务", "当前门店",
                    local_today(), local_today() + timedelta(days=1), "测试人")
    changed = card.model_copy(update={"category": "价格", "valid_until": None})
    with pytest.raises(EvidenceError, match="有效期"):
        check_card(changed, evidence)


def test_disabled_card_invalidates_already_generated_result(card_setup, task):
    evidence, card = card_setup
    facts = facts_from_cards(evidence)
    extraction = FactExtraction(product_model=evidence.product_model, facts=facts)
    base = FakeEngine(evidence, extraction)
    stages = []
    def run(stage, schema, payload):
        stages.append(stage)
        return base.run(stage, schema, payload)
    task.fact_card_signature = cards_signature(True, [card])
    result = run_workflow(task, evidence, SimpleNamespace(run=run),
                          evidence_loader=lambda *a: pytest.fail("事实卡模式不应扩大选材"))
    assert result.review.passed
    assert stages == ["strategy", "ground", "generate", "review"]
    require_publishable(result, task)
    disable_card(card.card_id)
    with pytest.raises(EvidenceError, match="更新"):
        require_publishable(result, task)


def image_asset(evidence):
    picture = Image.new("RGB", (321, 199), "white")
    for x in range(picture.width):
        for y in range(picture.height):
            picture.putpixel((x, y), (x % 256, y % 256, (x + y) % 256))
    output = BytesIO()
    picture.save(output, "PNG")
    asset = evidence.images[0].model_copy(update={"data": output.getvalue()})
    return asset, picture


@pytest.mark.parametrize("platform,size", [("小红书", (1080, 1440)), ("朋友圈", (1080, 1080)), ("公众号", (1200, 675))])
def test_cover_keeps_complete_original_pixels_and_original_download(evidence, task, platform, size):
    task.platform = platform
    asset, original = image_asset(evidence)
    cover = make_cover(asset, task, HEADLINES[0], "白底红线", True)
    result = Image.open(BytesIO(cover.png))
    assert result.size == size
    left, top, right, bottom = cover.manifest["placed_box"]
    expected = original.resize((right - left, bottom - top), Image.Resampling.LANCZOS)
    assert result.crop((left, top, right, bottom)).tobytes() == expected.tobytes()
    assert cover.manifest["source_sha256"] == sha256(asset.data).hexdigest()
    with ZipFile(BytesIO(cover.package())) as archive:
        assert archive.read("原图.png") == asset.data


def test_cover_requires_rights_model_and_safe_title(evidence, task):
    asset, _ = image_asset(evidence)
    with pytest.raises(EvidenceError, match="权限"):
        make_cover(asset, task, HEADLINES[0], "白底红线", False)
    with pytest.raises(EvidenceError, match="不一致"):
        make_cover(asset.model_copy(update={"product_model": "其他型号"}), task, HEADLINES[0], "白底红线", True)
    with pytest.raises(EvidenceError, match="依据"):
        make_cover(asset, task, "免费赠送9999元", "白底红线", True)
    with pytest.raises(EvidenceError, match="审核"):
        make_cover(asset, task, HEADLINES[0], "白底红线", True, fact_id="F1")


def test_cover_rejects_invalid_image():
    with pytest.raises(EvidenceError, match="读取"):
        decode_image(b"broken image")


def test_ui_optional_intake_can_complete_and_changed_request_blocks_old_task(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)
    next(r for r in app.radio if r.label == "工作模式").set_value("门店简易模式").run()
    next(r for r in app.radio if r.label == "创作方式").set_value("一句话需求").run()
    assert button(app, "生成本次图文").disabled
    monkeypatch.setattr("creation_ui.LangChainEngine", lambda _: IntakeEngine([
        IntakePlan(audience="大学新生", scenario="开学选电脑，整理课程资料")]))
    next(i for i in app.text_input if i.label == "一句话说说你想发什么").set_value("给刚入学的大学生发一条").run()
    button(app, "理解我的需求").click().run()
    assert not app.exception and app.session_state["intake_state"].ready
    assert not button(app, "生成本次图文").disabled
    button(app, "生成本次图文").click().run()
    assert app.session_state["result"].review.passed
    next(i for i in app.text_input if i.label == "一句话说说你想发什么").set_value("改发给差旅办公人员").run()
    assert button(app, "生成本次图文").disabled
    assert any("任务已修改" in item.value for item in app.warning)
    assert not app.exception


def test_ui_cover_requires_confirmation_and_hides_old_platform_download(configured_app):
    app = configured_app
    load_first_product(app)
    assert button(app, "制作封面").disabled
    next(c for c in app.checkbox if c.label == "我确认原图为当前型号，并有权用于本次发布").check().run()
    button(app, "制作封面").click().run()
    assert not app.exception
    assert any(d.proto.label == "下载封面 PNG" for d in app.get("download_button"))
    next(s for s in app.selectbox if s.label == "发布平台 *").select("朋友圈").run()
    assert not any(d.proto.label == "下载封面 PNG" for d in app.get("download_button"))
    assert not app.exception


def test_ui_search_topic_is_optional_and_task_change_hides_old_result(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)
    batch = SearchBatch("测试查询", "2026-10-23", [WebHit("测试参考", "https://example.org/reference", "外部摘要")])
    monkeypatch.setattr("creation_ui.search_topics", lambda _, **kwargs: batch)
    monkeypatch.setattr("creation_ui.recommend_topics", lambda *a: ["study", "compare"])
    next(c for c in app.checkbox if c.label == "参考联网选题").check().run()
    button(app, "搜索选题").click().run()
    next(s for s in app.selectbox if s.label == "采用哪个问题切入").select("study").run()
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert app.session_state["result"].task.inspiration.angle_id == "study"
    assert app.session_state["result"].review.passed and not app.exception
    next(c for c in app.checkbox if c.label == "参考联网选题").uncheck().run()
    assert any("任务已修改" in item.value for item in app.warning)

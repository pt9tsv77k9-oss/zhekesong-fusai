"""可编辑封面标题的事实边界、排版和界面失效检查。"""

from io import BytesIO
import json
from types import SimpleNamespace
from zipfile import ZipFile

from PIL import Image, ImageChops
import pytest

from checks import EvidenceError
from cover_titles import (HEADLINES, HeadlineReview, MAX_HEADLINE_LENGTH,
                          require_current_cover, review_headline)
from covers import make_cover
from llm import ModelError
from workflow import run_workflow
from test_core import evidence, task, extraction, FakeEngine
from test_app import configured_app, load_first_product, button
from test_creation_v2 import image_asset, card_setup


def title_engine(kind="通用选题", fact_ids=None, status="通过", **overrides):
    def run(stage, schema, payload):
        assert stage == "cover_title"
        return HeadlineReview(**({
            "headline": payload["headline"], "product_model": payload["product_model"],
            "kind": kind, "fact_ids": fact_ids or [], "compliance_status": status,
            "reason": "测试核验结论", **overrides,
        }))
    return SimpleNamespace(run=run)


@pytest.mark.parametrize("title,match", [
    ("  ", "请输入"), ("新" * (MAX_HEADLINE_LENGTH + 1), "最多"),
    ("开学\n选电脑", "单行"), ("开学\u200b选电脑", "隐藏"),
    ("百分之百保证不卡顿", "宣传表达"), ("免费赠送9999元", "依据"),
])
def test_invalid_or_unsupported_title_stops_before_model(task, title, match):
    engine = SimpleNamespace(run=lambda *args: pytest.fail("无效标题不应调用模型"))
    with pytest.raises(EvidenceError, match=match):
        review_headline(title, task, engine=engine)


@pytest.mark.parametrize("platform", ["小红书", "公众号", "朋友圈"])
def test_custom_title_is_fitted_exported_and_keeps_product_pixels(evidence, task, platform):
    task.platform = platform
    asset, _ = image_asset(evidence)
    # 30字会触发自动换行/缩小字号；文字不得越过标题预留区域。
    title = "开学选电脑先看课程需求再列软件清单带着问题和资料一起做比较呀"
    assert len(title) == MAX_HEADLINE_LENGTH
    cover = make_cover(asset, task, title, "白底红线", True, engine=title_engine())
    baseline = make_cover(asset, task, HEADLINES[0], "白底红线", True)
    custom_image = Image.open(BytesIO(cover.png))
    diff = ImageChops.difference(custom_image, Image.open(BytesIO(baseline.png))).getbbox()
    assert diff is not None and diff[1] >= 56
    assert diff[3] <= (240 if platform == "公众号" else 165)
    placed_box = tuple(cover.manifest["placed_box"])
    assert custom_image.crop(placed_box).tobytes() == Image.open(BytesIO(baseline.png)).crop(placed_box).tobytes()
    with ZipFile(BytesIO(cover.package())) as archive:
        manifest = json.loads(archive.read("素材记录.json"))
        assert manifest["headline"] == title
        assert manifest["headline_review"]["kind"] == "通用选题"
        assert not manifest["reviewed_result_signature"]
    require_current_cover(cover, task)


def test_custom_title_needs_review_but_presets_do_not(task):
    assert review_headline("  " + HEADLINES[0] + "  ", task).headline == HEADLINES[0]
    with pytest.raises(EvidenceError, match="核验"):
        review_headline("开学选电脑，先看你的课程", task)


@pytest.mark.parametrize("engine", [
    title_engine("需修改", status="待确认"),
    title_engine("通用选题", status="需处理"),
    title_engine("依据事实", ["F999"]),
    title_engine("依据事实"),
    title_engine("通用选题", ["F1"]),
    title_engine(headline="审核偷偷改了标题"),
    title_engine(product_model="其他型号"),
])
def test_review_cannot_release_mismatched_unapproved_or_uncertain_claims(task, engine):
    with pytest.raises(EvidenceError):
        review_headline("开学电脑选购清单", task, engine=engine)


def test_factual_title_binds_current_result_and_task(evidence, task, extraction):
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    asset, _ = image_asset(evidence)
    cover = make_cover(asset, task, "显示屏为14.2英寸", "白底红线", True, result,
                       engine=title_engine("依据事实", ["F1"]))
    assert cover.manifest["headline_review"]["fact_ids"] == ["F1"]
    require_current_cover(cover, task, result)
    with pytest.raises(EvidenceError, match="失效"):
        require_current_cover(cover, task)
    with pytest.raises(EvidenceError, match="更新"):
        require_current_cover(cover, task, result.model_copy(update={"generated_at": "changed"}))
    with pytest.raises(EvidenceError, match="任务"):
        require_current_cover(cover, task.model_copy(update={"platform": "公众号"}), result)
    with pytest.raises(EvidenceError, match="依据"):
        make_cover(asset, task, "显示屏为14.2英寸", "白底红线", True, result,
                   engine=title_engine())


def test_factual_title_expiry_stops_download_even_without_parameter_line(card_setup, task):
    from fact_cards import cards_signature, facts_from_cards, disable_card
    from schemas import FactExtraction

    evidence, card = card_setup
    task.fact_card_signature = cards_signature(True, [card])
    extraction = FactExtraction(product_model=task.product_model, facts=facts_from_cards(evidence))
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    asset, _ = image_asset(evidence)
    cover = make_cover(asset, task, "显示屏为14.2英寸", "白底红线", True, result,
                       engine=title_engine("依据事实", ["F1"]))
    assert not cover.manifest["fact_id"]
    require_current_cover(cover, task, result)
    disable_card(card.card_id)
    with pytest.raises(EvidenceError, match="更新"):
        require_current_cover(cover, task, result)


def test_ui_editable_title_presets_and_invalidated_download(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)
    next(c for c in app.checkbox if c.label == "我确认原图为当前型号，并有权用于本次发布").check().run()
    button(app, "制作封面").click().run()
    assert any(d.proto.label == "下载封面 PNG" for d in app.get("download_button"))
    title = next(t for t in app.text_input if t.label == "封面标题")
    assert title.value == HEADLINES[0]
    custom = "开学选电脑，先看你的课程"
    title.set_value(custom).run()
    assert not any(d.proto.label.startswith("下载封面") for d in app.get("download_button"))
    assert "created_cover" not in app.session_state
    monkeypatch.setattr("creation_ui.LangChainEngine", lambda _: title_engine())
    button(app, "制作封面").click().run()
    assert not app.exception
    assert app.session_state["created_cover"]["item"].manifest["headline"] == custom
    button(app, HEADLINES[2]).click().run()
    assert next(t for t in app.text_input if t.label == "封面标题").value == HEADLINES[2]
    assert not any(d.proto.label.startswith("下载封面") for d in app.get("download_button"))
    next(t for t in app.text_input if t.label == "封面标题").set_value("  ").run()
    assert button(app, "制作封面").disabled


def test_ui_failed_review_clears_old_cover_and_keeps_input(configured_app, monkeypatch):
    app = configured_app
    load_first_product(app)
    next(c for c in app.checkbox if c.label == "我确认原图为当前型号，并有权用于本次发布").check().run()
    button(app, "制作封面").click().run()
    custom = "开学电脑选购清单"
    next(t for t in app.text_input if t.label == "封面标题").set_value(custom).run()

    def fail(*args):
        raise ModelError("模型连接失败或超时，请重试。")

    monkeypatch.setattr("creation_ui.LangChainEngine", lambda _: SimpleNamespace(run=fail))
    button(app, "制作封面").click().run()
    assert not app.exception and "created_cover" not in app.session_state
    assert not any(d.proto.label.startswith("下载封面") for d in app.get("download_button"))
    assert next(t for t in app.text_input if t.label == "封面标题").value == custom
    assert any("超时" in error.value for error in app.error)

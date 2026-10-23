"""门店交付回归：写前拦截、选材、成品字节和页面领取，不请求网络。"""

from io import BytesIO
from zipfile import ZipFile

from PIL import Image
import pytest

from checks import EvidenceError
from exporter import to_publish_package, to_publish_text
from schemas import Fact
from workflow import run_workflow
from test_core import evidence, extraction, task, FakeEngine
from test_app import configured_app, load_first_product, button


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unknown", "unsupported", "uncertain", "bad_selection", "empty_selection"])
def test_bad_fact_plan_stops_before_writing(task, evidence, extraction, mutation):
    class BadPlan(FakeEngine):
        def run(self, stage, schema, payload):
            result = super().run(stage, schema, payload)
            if stage == "ground":
                if mutation == "missing": result.checks = []
                if mutation == "duplicate": result.checks *= 2
                if mutation == "unknown": result.checks[0].fact_id = "F999"
                if mutation == "unsupported": result.checks[0].status = "不支持"
                if mutation == "uncertain": result.checks[0].status = "待确认"
                if mutation == "bad_selection": result.selected_fact_ids = ["F999"]
                if mutation == "empty_selection": result.selected_fact_ids = []
            return result
    engine = BadPlan(evidence, extraction)
    with pytest.raises(EvidenceError):
        run_workflow(task, evidence, engine)
    assert "generate" not in engine.calls


def test_capacity_cannot_turn_into_runtime_even_if_model_approves(task, evidence, extraction):
    evidence.sources[0].text = "电池额定容量70Wh，实际续航因使用条件而异。"
    extraction.facts = [Fact(claim="续航可达24小时", source_id="S1", quote=evidence.sources[0].text)]
    engine = FakeEngine(evidence, extraction)
    with pytest.raises(EvidenceError, match="支持检查") as error:
        run_workflow(task, evidence, engine)
    assert "数值单位" in error.value.details[0]["reason"]
    assert "generate" not in engine.calls


def test_handwriting_selection_uses_explicit_id_not_first_fact(task, evidence, extraction):
    task.platform, task.scenario = "朋友圈", "手写草图"
    evidence.sources[0].text = "测试系统。支持测试笔型的手写输入。"
    extraction.facts = [Fact(claim="测试系统", quote="测试系统", source_id="S1"),
                        Fact(claim="支持测试笔型的手写输入", quote="支持测试笔型的手写输入", source_id="S1")]
    class Selected(FakeEngine):
        def run(self, stage, schema, payload):
            result = super().run(stage, schema, payload)
            if stage == "ground":
                result.selected_fact_ids = ["F2"]
            if stage == "generate":
                assert [f["fact_id"] for f in payload["facts"]] == ["F2"]
                result.body[0].text = "支持测试笔型的手写输入。"
                result.body[0].fact_ids = ["F2"]
            if stage == "review":
                assert payload["selected_fact_ids"] == ["F2"]
                assert all(fact["product_model"] == task.product_model and fact["source_title"]
                           for fact in payload["facts"])
            return result
    result = run_workflow(task, evidence, Selected(evidence, extraction))
    assert result.review.passed and result.fact_plan.selected_fact_ids == ["F2"]


def test_package_has_real_images_in_layout_order_and_no_audit(task, evidence, extraction):
    for index, image in enumerate(evidence.images):
        buffer = BytesIO()
        Image.new("RGB", (20, 20), (index * 60, 0, 0)).save(buffer, format="PNG")
        image.data = buffer.getvalue()
        image.name = "../../不可信路径.jpg"
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    result.draft.images[0].after_paragraph = 1
    text = to_publish_text(result, task)
    assert "知识库依据" not in text and "F1" not in text and "核验" not in text
    with ZipFile(BytesIO(to_publish_package(result, task))) as archive:
        names = archive.namelist()
        pictures = [name for name in names if name.startswith("图片/")]
        assert len(pictures) == 3
        assert all(".." not in name and "\\" not in name and name.endswith(".png") for name in pictures)
        assert archive.read(pictures[0]) == evidence.images[1].data
        assert archive.read(pictures[-1]) == evidence.images[0].data
        assert archive.read("发布文案.txt").decode("utf-8-sig") == text
        assert not any("核验" in name for name in names)


def test_failed_stale_or_corrupt_result_cannot_be_claimed(task, evidence, extraction):
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    changed = task.model_copy(update={"audience": "另一人群"})
    with pytest.raises(EvidenceError, match="任务已修改"):
        to_publish_package(result, changed)
    with pytest.raises(EvidenceError, match="解码"):
        to_publish_package(result, task)
    result.review.passed = False
    with pytest.raises(EvidenceError, match="尚未通过"):
        to_publish_text(result, task)


def test_publish_gate_rejects_missing_audit_even_with_pass_flag(task, evidence, extraction):
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    result.review.fact_checks.pop()
    assert result.review.passed
    with pytest.raises(EvidenceError, match="完整"):
        to_publish_text(result, task)


def test_moments_text_excludes_internal_title(task, evidence, extraction):
    task.platform = "朋友圈"
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    result.draft.title.text = "导购内部主题"
    assert result.draft.title.text not in to_publish_text(result, task)
    assert result.draft.intro.text in to_publish_text(result, task)


@pytest.mark.parametrize("promise", ["到店体验可提前预约。", "到店时可以在样机上安装您的常用软件。", "带着自己的笔到店，直接连接试用。"])
def test_unsubstantiated_store_service_cannot_be_published(task, evidence, extraction, promise):
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    result.draft.cta.text = promise
    with pytest.raises(EvidenceError, match="成品复核"):
        to_publish_text(result, task)


def test_store_mode_generates_changes_angle_and_blocks_stale_download(configured_app):
    app = configured_app
    next(item for item in app.radio if item.label == "工作模式").set_value("门店简易模式").run()
    load_first_product(app)
    assert not any(item.label == "目标人群 *" for item in app.text_input)
    button(app, "生成本次图文").click().run()
    assert not app.exception and app.session_state["result"].review.passed
    assert any(item.proto.label == "领取文案与图片包" for item in app.get("download_button"))
    brief = app.session_state["result"].task.writing_brief
    button(app, "换个角度生成").click().run()
    assert not app.exception and app.session_state["result"].task.writing_brief != brief
    assert next(item for item in app.selectbox if item.label == "内容切入").value == "场景清单"
    next(item for item in app.selectbox if item.label == "发给谁").select("差旅办公").run()
    assert any("任务已修改" in item.value for item in app.warning)
    assert not any(item.proto.label == "领取文案与图片包" for item in app.get("download_button"))


def test_failed_review_has_no_publish_controls(configured_app, monkeypatch):
    app = configured_app
    next(item for item in app.radio if item.label == "工作模式").set_value("门店简易模式").run()
    load_first_product(app)
    original = run_workflow
    def failed(*args, **kwargs):
        result = original(*args, **kwargs)
        result.review.passed = False
        return result
    monkeypatch.setattr("workflow.run_workflow", failed)
    button(app, "生成本次图文").click().run()
    assert not app.exception
    assert not any(item.proto.label in {"领取文案与图片包", "下载纯净文案"} for item in app.get("download_button"))
    assert any("下载待确认草稿" in item.proto.label for item in app.get("download_button"))

"""网页关键交互测试：使用合成产品和本地图片，不调用 ima 或收费模型。"""

# AppTest 在本地执行 Streamlit 并模拟点击/输入，不需要人工打开浏览器。
# .click()/.set_value() 设置操作，后面的 .run() 触发页面重新执行。
# 每次操作后检查界面组件与 session_state，验证用户能实际走通关键路径。

from io import BytesIO

from PIL import Image
import pytest
from streamlit.testing.v1 import AppTest

from config import ROOT, Settings
from ima_client import ImaClient
from llm import ModelError
from schemas import Evidence, Fact, FactExtraction, ImageAsset, Source
from test_core import FakeEngine


class FakeIma(ImaClient):
    """替代真实客户端，提供固定目录/资料；笔记仅记录到内存列表，不写账号。"""
    def __init__(self, settings):
        super().__init__(settings)
        self.kb_name = settings.kb_name
        self.created_notes = []

    def search(self, query):
        return [{"media_id": "search-doc", "title": "搜索命中的资料.md", "highlight_content": "测试内容匹配"}]

    def read_text(self, item):
        return "搜索资料正文：显示屏为14.2英寸。"

    def create_note(self, content, title=""):
        self.created_notes.append((content, title))
        return "test-created-note"

    def connect(self):
        return self.kb_name

    def product_root(self):
        return {"media_id": "root", "title": "P1-产品参数库", "media_type": 99}

    def list_folder(self, folder_id=""):
        if folder_id == "root":
            return [{"media_id": "category", "title": "测试产品分类", "media_type": 99}]
        if folder_id == "category":
            return [{"media_id": name, "title": title, "media_type": 99}
                    for name, title in [("product-a", "测试型号A"), ("product-b", "测试型号B")]]
        return [{"media_id": "doc", "title": "产品事实.md", "media_type": 7}]

    def load_product(self, folder):
        # UI 要解码真实图像，因此用 Pillow 在内存里生成 20×20 蓝色 PNG。
        # BytesIO 是内存文件，不会在项目里新增测试图片。
        images = []
        for i, color in enumerate(["blue", "green", "red"], 1):
            image = BytesIO()
            Image.new("RGB", (20, 20), color).save(image, format="PNG")
            images.append(ImageAsset(image_id=f"I{i}", name=f"picture{i}.png", product_model=folder["title"],
                                     source_title="测试图片索引", verified=True, data=image.getvalue()))
        return Evidence(
            product_model=folder["title"], folder_title=folder["title"], folder_id=folder["media_id"],
            sources=[Source(source_id="S1", title="产品事实.md", text="显示屏为14.2英寸。")],
            images=images,
        )

    def retrieve_for_task(self, task, strategy, base):
        # UI 测试不请求网络，真实任务检索逻辑由专项客户端测试覆盖。
        return base.model_copy(deep=True)


def button(app, label):
    """按显示文字找到按钮，比依赖按钮在页面中的排列序号更容易读。"""
    return next(item for item in app.button if item.label == label)


@pytest.fixture
def configured_app(monkeypatch):
    """准备配置、假 ima 和假模型，再运行页面；每项测试得到独立页面状态。"""
    # 替换的是 app.py 即将导入的模块成员；占位 Key 不用于任何真实请求。
    monkeypatch.setattr("config.load_settings", lambda: Settings(api_key="local-test-placeholder"))
    monkeypatch.setattr("ima_client.ImaClient", FakeIma)

    def engine(settings):
        # 仅模拟四阶段交互；真实接口质量需要另外人工验收。
        evidence = FakeIma(settings).load_product({"title": "测试型号A", "media_id": "product-a"})
        extraction = FactExtraction(product_model=evidence.product_model, facts=[
            Fact(claim="显示屏为14.2英寸", source_id="S1", quote="显示屏为14.2英寸"),
        ])
        return FakeEngine(evidence, extraction)

    monkeypatch.setattr("llm.LangChainEngine", engine)
    app = AppTest.from_file(ROOT / "app.py", default_timeout=15).run()
    next(item for item in app.radio if item.label == "工作模式").set_value("完整工作台").run()
    return app


def load_first_product(app):
    """复用一组操作：连接 → 分类 → 第一个产品 → 加载资料。"""
    button(app, "连接 / 刷新 ima 知识库").click().run()
    # 下拉框的实际值是整数；AppTest.select_index 使用显示文案，故按真实值选择。
    app.selectbox[0].select(0).run()
    app.selectbox[1].select(0).run()
    button(app, "加载当前产品资料与图片").click().run()


def test_generate_then_task_and_product_changes_invalidate_results(configured_app):
    """先生成成功；修改平台禁用旧下载；切换产品彻底清除旧结果与依据。"""
    app = configured_app
    load_first_product(app)
    assert not app.exception
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert not app.exception
    assert app.session_state["result"].review.passed
    assert [item.label for item in app.tabs] == ["场景判断", "知识库依据", "图文软文", "核验结果"]
    content_download = next(item for item in app.get("download_button") if "图文内容" in item.proto.label)
    assert not content_download.proto.disabled

    next(item for item in app.selectbox if item.label == "发布平台 *").select("朋友圈").run()
    assert any("任务已修改" in item.value for item in app.warning)
    assert next(item for item in app.get("download_button") if "图文内容" in item.proto.label).proto.disabled

    app.selectbox[1].select(1).run()
    assert "result" not in app.session_state
    assert "evidence" not in app.session_state
    assert not app.exception


def test_missing_required_input_disables_generation(configured_app):
    """只输入空格也视为未填，生成按钮应禁用并给出提示。"""
    app = configured_app
    load_first_product(app)
    next(item for item in app.text_input if item.label == "目标人群 *").set_value("   ").run()
    assert button(app, "生成 / 按当前要求重新生成").disabled
    assert any("补充目标人群" in item.value for item in app.warning)
    assert not app.exception


def test_model_failure_clears_previous_result(configured_app, monkeypatch):
    """成功生成后故意让模型失败，网页应清除旧稿并显示可读错误。"""
    app = configured_app
    load_first_product(app)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert "result" in app.session_state

    def failing_engine(settings):
        raise ModelError("测试模型不可用")

    monkeypatch.setattr("llm.LangChainEngine", failing_engine)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert "result" not in app.session_state
    assert any("测试模型不可用" in item.value for item in app.error)
    assert not app.exception


def test_search_and_read_work_before_product_selection(configured_app):
    """独立搜索可先于产品选择使用；搜索原文不自动变成营销依据或笔记。"""
    app = configured_app
    button(app, "连接 / 刷新 ima 知识库").click().run()
    next(item for item in app.text_input if item.label == "资料关键词").set_value("显示屏")
    button(app, "搜索资料").click().run()
    button(app, "读取选中资料").click().run()
    assert not app.exception
    assert any("搜索资料正文" in item.value for item in app.text)
    assert "evidence" not in app.session_state
    assert not app.session_state["ima"].created_notes


def test_create_note_is_explicit_and_not_repeated_on_rerun(configured_app):
    """填写不写入；点击只写一次；成功后按钮禁用，页面重跑不重复创建。"""
    app = configured_app
    button(app, "连接 / 刷新 ima 知识库").click().run()
    next(item for item in app.text_input if item.label == "笔记标题").set_value("门店记录").run()
    next(item for item in app.text_area if item.label == "笔记正文（Markdown）").set_value("今天的产品咨询记录。").run()
    assert not app.session_state["ima"].created_notes
    button(app, "在 IMA 新建笔记").click().run()
    assert not app.exception
    assert app.session_state["ima"].created_notes == [("今天的产品咨询记录。", "门店记录")]
    assert button(app, "在 IMA 新建笔记").disabled
    app.run()
    assert len(app.session_state["ima"].created_notes) == 1


def test_marketing_note_contains_review_and_stale_result_cannot_save(configured_app):
    """保存营销稿需含依据和核验记录；修改任务后不能保存旧结果。"""
    app = configured_app
    load_first_product(app)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert not app.session_state["ima"].created_notes
    next(item for item in app.selectbox if item.label == "发布平台 *").select("朋友圈").run()
    assert button(app, "在 IMA 新建营销笔记").disabled
    next(item for item in app.selectbox if item.label == "发布平台 *").select("小红书").run()
    button(app, "在 IMA 新建营销笔记").click().run()
    assert not app.exception
    content, title = app.session_state["ima"].created_notes[0]
    assert "二、知识库依据" in content and "四、核验结果" in content
    assert button(app, "在 IMA 新建营销笔记").disabled

    next(item for item in app.selectbox if item.label == "发布平台 *").select("朋友圈").run()
    assert button(app, "在 IMA 新建营销笔记").disabled


def test_failed_note_write_has_no_success_receipt(configured_app, monkeypatch):
    """模拟权限失败：页面显示错误，不应记录成功回执。"""
    from ima_client import ImaError

    def fail(*args):
        raise ImaError("测试写入权限不足")

    monkeypatch.setattr(FakeIma, "create_note", fail)
    app = configured_app
    button(app, "连接 / 刷新 ima 知识库").click().run()
    next(item for item in app.text_area if item.label == "笔记正文（Markdown）").set_value("测试正文").run()
    button(app, "在 IMA 新建笔记").click().run()
    assert not app.exception
    assert not app.session_state["ima_created_notes"]
    assert any("测试写入权限不足" in item.value for item in app.error)


def test_vague_task_is_blocked_before_any_model_request(configured_app):
    """明显模糊的任务需要具体补充，而不是带着模糊条件去收费生成。"""
    app = configured_app
    load_first_product(app)
    next(item for item in app.text_input if item.label == "目标人群 *").set_value("所有人").run()
    next(item for item in app.text_input if item.label == "使用场景 *").set_value("日常用").run()
    assert button(app, "生成 / 按当前要求重新生成").disabled
    assert any("主要人群" in item.value for item in app.warning)
    assert any("主要用途" in item.value for item in app.warning)


def test_evidence_failure_details_display_and_old_draft_removed(configured_app, monkeypatch):
    """让取证报出具体片段错误：网页要展示明细，不能残留旧稿。"""
    from checks import EvidenceError
    app = configured_app
    load_first_product(app)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert "result" in app.session_state

    def failing(*args, **kwargs):
        raise EvidenceError("测试原文片段错误", details=[{"segment_id": "S1-P999", "reason": "编号不存在"}])
    monkeypatch.setattr("workflow.run_workflow", failing)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert "result" not in app.session_state
    assert any("测试原文片段错误" in item.value for item in app.error)
    assert any("取证失败明细" in item.value for item in app.markdown)
    assert len(app.dataframe) == 1 and not app.exception


def test_unknown_error_is_locatable_without_exposing_exception(configured_app, monkeypatch, local_tmp_path):
    """复现“生成末尾未知异常”：保留阶段和编号，清除旧稿，不泄露异常原文。"""
    import json
    import diagnostics
    tmp_path = local_tmp_path
    monkeypatch.setattr(diagnostics, "LOG_DIR", tmp_path / "logs")
    app = configured_app
    load_first_product(app)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert "result" in app.session_state

    def failing(*args, **kwargs):
        kwargs["progress"]("正在重新核验修正结果")
        raise AttributeError("test-secret-in-exception")

    monkeypatch.setattr("workflow.run_workflow", failing)
    button(app, "生成 / 按当前要求重新生成").click().run()
    assert "result" not in app.session_state and not app.exception
    assert any("正在重新核验修正结果" in item.value for item in app.error)
    assert any("错误编号" in item.value for item in app.caption)
    assert all("test-secret-in-exception" not in item.value for item in app.error)
    record = json.loads((tmp_path / "logs/errors.jsonl").read_text(encoding="utf-8"))
    assert record["exceptions"][0]["type"] == "AttributeError"


def test_code_update_discards_previous_connection_objects(configured_app, monkeypatch):
    """源码更新后不能把旧类对象继续留在网页缓存中使用。"""
    app = configured_app
    load_first_product(app)
    assert "evidence" in app.session_state
    monkeypatch.setattr("diagnostics.runtime_signature", lambda: "changed-runtime-version")
    app.run()
    assert "ima" not in app.session_state and "evidence" not in app.session_state
    assert any("程序或配置已更新" in item.value for item in app.info)
    assert not app.exception

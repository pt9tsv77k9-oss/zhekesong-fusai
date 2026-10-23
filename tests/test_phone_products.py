"""手机入口、单文档取证、场景隔离与缺图拦截；全部使用合成资料。"""

from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from streamlit.testing.v1 import AppTest

from checks import EvidenceError
from config import ROOT, Settings
from cover_titles import HEADLINES, PHONE_HEADLINES, review_headline
from ima_client import ImaClient, ImaError, model_from_document
from inspiration import (TOPICS, SearchBatch, SearchError, TopicRecommendations,
                         creative_context, local_topic_ids, recommend_topics, select_local_inspiration)
from intake import IntakePlan, understand_request, use_default_intake
from playbooks import PHONE_PLAYBOOKS, playbooks_for, store_task
from product_types import is_phone
from schemas import Evidence, ImageAsset, Task, Strategy
from test_app import FakeIma, button


class PhoneIma(FakeIma):
    documents = {
        "base": ("华为Mate90_产品介绍与参数_20261023.md", "HUAWEI Mate 90 产品介绍与参数"),
        "pro": ("华为Mate90Pro_产品介绍与参数_20261023.md", "HUAWEI Mate 90 Pro 产品介绍与参数"),
        "rs": ("华为Mate90RS非凡大师_产品介绍与参数_20261023.md", "HUAWEI Mate 90 RS 非凡大师产品介绍与参数"),
        "max": ("华为Mate90ProMax及典藏版_产品介绍与参数_20261023.md", "HUAWEI Mate 90 Pro Max 及典藏版产品介绍与参数"),
        "compare": ("华为官网重点手机新品_Mate90系列对比与选购_20261023.md", "华为官网重点手机新品：Mate 90 系列对比与选购参考"),
    }

    def list_folder(self, folder_id=""):
        if folder_id == "root":
            return [*super().list_folder(folder_id), {"media_id": "ext", "title": "07-扩展产品目录", "media_type": 99}]
        if folder_id == "ext":
            return [{"media_id": key, "title": row[0], "media_type": 7} for key, row in self.documents.items()]
        return super().list_folder(folder_id)

    def read_text(self, item):
        if item["media_id"] in self.documents:
            return "# " + self.documents[item["media_id"]][1] + "\n\n合成测试资料，不代表真实产品规格。"
        return super().read_text(item)

    def load_product(self, folder):
        return ImaClient.load_product(self, folder) if folder.get("product_document") else super().load_product(folder)

    def search(self, query):
        return self.list_folder("ext")


@pytest.fixture
def phone_client():
    return PhoneIma(Settings())


@pytest.mark.parametrize("model,expected", [
    ("HUAWEI Mate 90", True), ("华为Mate90 Pro", True), ("HUAWEI Pura 80", True),
    ("HUAWEI nova 15", True), ("HUAWEI MateBook 14", False), ("HUAWEI MateView", False),
])
def test_category_does_not_confuse_matebook_with_phone(model, expected):
    assert is_phone(model) == expected


def test_phone_discovery_excludes_comparisons_and_keeps_complete_model(phone_client):
    phones, excluded = phone_client.phone_products(phone_client.product_root())
    assert [p["title"] for p in phones] == ["HUAWEI Mate 90", "HUAWEI Mate 90 Pro", "HUAWEI Mate 90 RS 非凡大师"]
    assert len(excluded) == 2
    assert model_from_document("# HUAWEI MateBook 14（2026 鸿蒙版） - 产品事实", "") == "HUAWEI MateBook 14（2026 鸿蒙版）"
    loaded = phone_client.load_product(phones[1])
    assert loaded.product_model == phones[1]["title"]
    assert len(loaded.sources) == 1 and loaded.sources[0].media_id == "pro"
    assert loaded.document_id == "pro" and loaded.folder_id == "ext"
    assert "document_id" not in loaded.model_dump()
    assert not loaded.images


def test_task_retrieval_cannot_import_sibling_phone_documents(phone_client):
    phones, _ = phone_client.phone_products(phone_client.product_root())
    base = phone_client.load_product(phones[1])
    strategy = Strategy(audience_problem="换机", angle="清单", priority_points=["拍摄"],
                        structure=["需求"], tone="口语", image_count=1, call_to_action="咨询")
    task = store_task(base.product_model, "日常换机", PHONE_PLAYBOOKS["日常换机"]["scenes"][0], "朋友圈", "选购问题")
    result = ImaClient.retrieve_for_task(phone_client, task, strategy, base)
    assert len(result.sources) == 1 and result.sources[0].media_id == "pro"
    assert all(row.excluded_count == 4 for row in result.retrieval)
    assert result.document_id == "pro"


def test_changed_phone_header_and_aggregate_folder_are_rejected(phone_client, monkeypatch):
    phones, _ = phone_client.phone_products(phone_client.product_root())
    monkeypatch.setattr(phone_client, "read_text", lambda _: "# HUAWEI Mate 90 Pro 产品介绍与参数\n已替换")
    with pytest.raises(ImaError, match="唯一型号"):
        phone_client.load_product(phones[0])
    with pytest.raises(ImaError, match="多个型号"):
        ImaClient.load_product(phone_client, {"media_id": "ext", "title": "07-扩展产品目录"})


def test_phone_scenarios_defaults_questions_and_titles_are_separate():
    model = "HUAWEI Mate 90 Pro"
    assert playbooks_for(model) is PHONE_PLAYBOOKS
    state = use_default_intake("帮我写一条", model, "朋友圈")
    assert "手机" in state.plan.scenario and "电脑" not in state.plan.scenario
    calls = []

    def run(stage, schema, payload):
        calls.append(payload)
        return IntakePlan()

    state = understand_request("帮我写一条", model, "朋友圈", SimpleNamespace(run=run))
    assert "日常换机用户" in state.plan.choices
    assert calls[0]["product_model"] == model and calls[0]["product_category"] == "手机"
    task = Task(product_model=model, audience="换机用户", scenario="换手机", platform="朋友圈")
    assert review_headline(PHONE_HEADLINES[0], task).kind == "通用选题"
    with pytest.raises(EvidenceError, match="核验"):
        review_headline(HEADLINES[0], task)


def test_phone_inspiration_filters_model_recommendations():
    model = "HUAWEI Mate 90 Pro"
    ids = local_topic_ids("旅行拍摄", "出游拍照", model)
    assert ids[0] == "phone_photo" and all(i.startswith("phone_") for i in ids)
    assert all("电脑" not in TOPICS[i][1] for i in ids)
    assert "手机" in creative_context(select_local_inspiration(ids[0]))["question"]
    calls = []

    def run(stage, schema, payload):
        calls.append(payload)
        return TopicRecommendations(angle_ids=["study", "phone_photo"])

    assert recommend_topics(SearchBatch("旅行手机", "test", []), "旅行拍摄", SimpleNamespace(run=run), model) == ["phone_photo"]
    assert all(i.startswith("phone_") for i in calls[0]["available_angles"])
    with pytest.raises(SearchError):
        recommend_topics(SearchBatch("旅行手机", "test", []), "旅行拍摄",
                         SimpleNamespace(run=lambda *a: TopicRecommendations(angle_ids=["study"])), model)


def test_phone_ui_blocks_missing_photos_and_clears_previous_model(monkeypatch):
    monkeypatch.setattr("config.load_settings", lambda: Settings(api_key="test-unused"))
    monkeypatch.setattr("ima_client.ImaClient", PhoneIma)
    app = AppTest.from_file(ROOT / "app.py", default_timeout=15).run()
    button(app, "连接 / 刷新 ima 知识库").click().run()
    category = next(x for x in app.selectbox if x.label == "产品分类")
    category.select(1).run()
    assert not any(x.label == "加载当前产品资料与图片" for x in app.button)
    next(x for x in app.selectbox if x.label == "产品分类").select(2).run()
    phones = next(x for x in app.selectbox if x.label == "手机型号")
    assert len(phones.options) == 3
    phones.select(0).run()
    button(app, "加载当前产品资料与图片").click().run()
    assert not app.exception and app.session_state["evidence"].product_model == "HUAWEI Mate 90"
    assert "日常换机" in next(x for x in app.selectbox if x.label == "发给谁").options
    assert button(app, "生成本次图文").disabled
    assert any("请先补充" in x.value for x in app.info)
    app.session_state["result"] = "previous-result"
    next(x for x in app.selectbox if x.label == "手机型号").select(1).run()
    assert "result" not in app.session_state and "evidence" not in app.session_state
    button(app, "加载当前产品资料与图片").click().run()
    assert app.session_state["evidence"].product_model == "HUAWEI Mate 90 Pro"
    buffer = BytesIO()
    Image.new("RGB", (32, 24), "red").save(buffer, "PNG")
    monkeypatch.setattr("streamlit.file_uploader", lambda *a, **k: [SimpleNamespace(name="测试.png", getvalue=buffer.getvalue)])
    app.run()
    next(x for x in app.checkbox if "所选图片" in x.label).check().run()
    assert button(app, "生成本次图文").disabled  # 小红书需要至少3张图。
    next(x for x in app.selectbox if x.label == "发布平台 *").select("朋友圈").run()
    assert not button(app, "生成本次图文").disabled
    assert next(x for x in app.text_input if x.label == "封面标题").value == PHONE_HEADLINES[0]
    next(x for x in app.selectbox if x.label == "产品分类").select(0).run()
    assert "evidence" not in app.session_state
    assert not app.exception


def test_photos_need_confirmation_and_revoking_it_invalidates_result(monkeypatch):
    buffer = BytesIO()
    Image.new("RGB", (32, 24), "red").save(buffer, "PNG")
    upload = SimpleNamespace(name="合成测试图.png", getvalue=buffer.getvalue)
    monkeypatch.setattr("streamlit.file_uploader", lambda *a, **k: [upload])
    app = AppTest.from_string('''
import streamlit as st
from creation_ui import render_product_photos
from schemas import Evidence
evidence = Evidence(product_model="HUAWEI Mate 90", folder_title="test", sources=[])
st.session_state.photo_evidence = render_product_photos(evidence)
''', default_timeout=15).run()
    assert not app.session_state["photo_evidence"].images
    app.checkbox[0].check().run()
    assert app.session_state["photo_evidence"].images[0].verified
    assert app.session_state["photo_evidence"].images[0].verified_at
    app.session_state["result"] = "previous-result"
    app.checkbox[0].uncheck().run()
    assert not app.session_state["photo_evidence"].images
    assert "result" not in app.session_state


def phone_picture(image_id, color, model="HUAWEI Mate 90", verified=True):
    buffer = BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, "PNG")
    return ImageAsset(image_id=image_id, name=image_id + ".png", product_model=model,
                      source_title="合成测试原图", data=buffer.getvalue(), verified=verified)


def phone_photo_app(images):
    app = AppTest.from_string(r'''
import streamlit as st
from creation_ui import render_product_photos, render_cover
from config import Settings
from schemas import Task
selected = render_product_photos(st.session_state.base_evidence)
st.session_state.photo_evidence = selected
render_cover(selected, Task(product_model=selected.product_model, audience="phone shoppers",
                           scenario="phone selection", platform="\u670b\u53cb\u5708"), Settings())
''', default_timeout=15)
    app.session_state["base_evidence"] = Evidence(product_model="HUAWEI Mate 90", folder_title="test",
                                                   sources=[], images=images)
    return app.run()


def test_phone_original_selection_only_uses_confirmed_current_model_images():
    images = [phone_picture("O1", "blue"), phone_picture("O2", "green"),
              phone_picture("OTHER", "red", model="HUAWEI Mate 90 Pro"),
              phone_picture("UNVERIFIED", "white", verified=False)]
    app = phone_photo_app(images)
    assert app.radio[0].value == "产品资料原图"
    assert app.multiselect[0].options == ["O1.png", "O2.png"]
    assert not app.session_state["photo_evidence"].images
    app.checkbox[0].check().run()
    assert [a.image_id for a in app.session_state["photo_evidence"].images] == ["O1", "O2"]
    assert len(app.checkbox) == 1  # 封面复用已确认图片，不再要求第二次确认。
    assert len(app.session_state["base_evidence"].images) == 4
    button(app, "制作封面").click().run()
    assert "created_cover" in app.session_state
    app.multiselect[0].set_value(["O2"]).run()
    assert not app.checkbox[0].value and not app.session_state["photo_evidence"].images
    assert "created_cover" not in app.session_state
    assert not app.get("download_button") and not app.exception


def test_phone_source_switch_reuses_upload_for_cover_and_removes_old_download(monkeypatch):
    upload = phone_picture("UPLOAD_TEST", "red")
    monkeypatch.setattr("streamlit.file_uploader", lambda *a, **k: [
        SimpleNamespace(name=upload.name, getvalue=lambda: upload.data)])
    app = phone_photo_app([phone_picture("O1", "blue")])
    app.checkbox[0].check().run()
    button(app, "制作封面").click().run()
    assert app.get("download_button")
    app.radio[0].set_value("上传门店实拍").run()
    assert not app.session_state["photo_evidence"].images
    assert not app.get("download_button")
    app.checkbox[0].check().run()
    selected = app.session_state["photo_evidence"].images
    assert len(selected) == 1 and selected[0].data == upload.data
    assert len(app.checkbox) == 1
    assert not any(r.label == "封面原图" for r in app.radio)
    assert next(s for s in app.selectbox if s.label == "封面使用的图片").options == [upload.name]
    button(app, "制作封面").click().run()
    assert app.session_state["created_cover"]["item"].manifest["source_sha256"]
    assert app.get("download_button")
    app.checkbox[0].uncheck().run()
    assert "created_cover" not in app.session_state and not app.get("download_button")
    assert not app.exception


def test_phone_same_id_with_replaced_image_cannot_reuse_confirmation():
    app = phone_photo_app([phone_picture("O1", "blue")])
    app.checkbox[0].check().run()
    button(app, "制作封面").click().run()
    app.session_state["result"] = "previous-result"
    base = app.session_state["base_evidence"]
    app.session_state["base_evidence"] = base.model_copy(update={"images": [phone_picture("O1", "green")]})
    app.run()
    assert not app.session_state["photo_evidence"].images
    assert not app.checkbox[0].value
    assert "result" not in app.session_state and "created_cover" not in app.session_state
    assert not app.exception

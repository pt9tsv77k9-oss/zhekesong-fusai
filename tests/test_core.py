"""离线业务测试：所有数据为测试夹具，不使用真实账号，不调用收费模型。"""

# 测试不是应用启动入口。运行 python -m pytest -q 时，pytest 自动找到 test_ 函数。
# assert 表示“应该成立的条件”，不成立就判为失败；pytest.raises 检查“应该报错”。
# @pytest.fixture 提供可复用测试数据：函数参数同名时，pytest 会自动准备并传入。
# monkeypatch 临时替换真实函数，单项测试结束后自动还原，避免访问真实网络。

from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from checks import EvidenceError, rule_checks, validate_facts, draft_sections
from config import Settings
from exporter import to_markdown
from ima_client import ImaClient, ImaError, model_from_document
from schemas import (Block, Draft, Evidence, Fact, FactExtraction, ImageAsset,
                     ImagePlacement, Issue, ModelReview, Source, Strategy, Task,
                     FactAudit, FactPlan, FactSupport, ComplianceAudit, COMPLIANCE_CATEGORIES)
from workflow import run_workflow


@pytest.fixture
def evidence():
    """合成产品依据：一份原文、四张仅用于业务判断的占位图片。"""
    # 本文件不显示图片，因此 data 不必是真 PNG；网页测试才需要真实格式。
    return Evidence(
        product_model="测试型号A（2026版）", folder_title="测试目录",
        sources=[Source(source_id="S1", title="测试原文", text="显示屏为14.2英寸；重量约1.33kg，具体配置为准。", media_id="internal-test-id")],
        images=[ImageAsset(image_id=f"I{i}", name=f"test{i}.png", product_model="测试型号A（2026版）",
                           source_title="测试图片索引", verified=True, data=f"not-exported-{i}".encode()) for i in range(1, 5)],
    )


@pytest.fixture
def extraction(evidence):
    """模拟提取结果：原文中存在的屏幕事实；来源编号指向 S1。"""
    return FactExtraction(product_model=evidence.product_model, facts=[
        Fact(claim="显示屏为14.2英寸", source_id="S1", quote="显示屏为14.2英寸"),
    ])


@pytest.fixture
def task(evidence):
    """使用相同型号构造合法任务，避免每个测试重复填写必填字段。"""
    return Task(product_model=evidence.product_model, audience="测试学生", scenario="测试学习场景", platform="小红书")


def make_draft(product):
    """创建一份符合基本规则的小红书草稿，供测试逐项修改制造错误。"""
    return Draft(product_model=product, title=Block(text="从学习场景开始了解"), intro=Block(text="选购时先明确自己的需要。"),
                 body=[Block(text="显示屏为14.2英寸。", fact_ids=["F1"])], cta=Block(text="欢迎咨询或到店体验。"),
                 tags=["#学习场景"], images=[ImagePlacement(image_id=f"I{i}", after_paragraph=0, caption="测试图片", reason="对应使用场景") for i in range(1, 4)])


def test_quote_must_exist_in_original(evidence, extraction):
    """故意替换为不存在的引句，应停止，而不是继续编写文案。"""
    extraction.facts[0].quote = "处理器为未证实型号"
    with pytest.raises(EvidenceError, match="原文引用"):
        validate_facts(extraction, evidence)


def test_conflicting_sources_stop_generation(evidence, extraction):
    """同一配置资料冲突必须人工处理，不能让后续生成自行挑一个值。"""
    extraction.conflicts = ["同一配置有矛盾数值"]
    with pytest.raises(EvidenceError, match="冲突"):
        validate_facts(extraction, evidence)


def test_similar_model_cannot_share_evidence(evidence, extraction):
    """年份差异也是型号差异，不能把 2026 版依据用于 2025 版。"""
    extraction.product_model = "测试型号A（2025版）"
    with pytest.raises(EvidenceError, match="型号"):
        validate_facts(extraction, evidence)


def test_unsupported_source_id_stops_generation(evidence, extraction):
    """模型编造 S999 来源编号时，即使其他字段完整，也应拒绝。"""
    extraction.facts[0].source_id = "S999"
    with pytest.raises(EvidenceError):
        validate_facts(extraction, evidence)


def test_fact_ids_are_assigned_by_program(evidence, extraction):
    """不沿用模型自报 F999，由程序按顺序重排为 F1。"""
    extraction.facts[0].fact_id = "F999"
    assert validate_facts(extraction, evidence)[0].fact_id == "F1"


@pytest.mark.parametrize("mutation,expected", [
    ("price", "unsupported_number"), ("unknown_fact", "unknown_fact"),
    ("unknown_image", "unknown_image"), ("duplicate", "duplicate_image"),
    ("wrong_model", "model_mismatch"), ("bad_position", "image_position"),
    ("no_tags", "missing_tags"), ("absolute", "risky_expression"),
])
def test_draft_business_rules(task, evidence, extraction, mutation, expected):
    """参数化测试：上面每一行各运行一次，分别检查一种草稿错误。"""
    # mutation 表示本轮故意制造的问题，expected 是预期返回的问题 code。
    # 只检查对应 code 是否出现，不要求完整报错文本，避免修改措辞就误判。
    facts = validate_facts(extraction, evidence)
    draft = make_draft(evidence.product_model)
    if mutation == "price": draft.body[0].text += "只需2999元。"
    if mutation == "unknown_fact": draft.body[0].fact_ids = ["F999"]
    if mutation == "unknown_image": draft.images[0].image_id = "I999"
    if mutation == "duplicate": draft.images[1].image_id = "I1"
    if mutation == "wrong_model": draft.product_model = "测试型号B"
    if mutation == "bad_position": draft.images[0].after_paragraph = 99
    if mutation == "no_tags": draft.tags = []
    if mutation == "absolute": draft.cta.text = "全网第一，绝不卡顿"
    assert expected in {issue.code for issue in rule_checks(task, draft, facts, evidence)}


def test_unverified_images_cannot_pass(task, evidence, extraction):
    """即使图片 ID 存在，型号未经索引或人工确认也不能通过。"""
    evidence.images[0].verified = False
    issues = rule_checks(task, make_draft(evidence.product_model), validate_facts(extraction, evidence), evidence)
    assert "unverified_image" in {issue.code for issue in issues}


def test_switching_platform_rechecks_image_count(task, evidence, extraction):
    """朋友圈最多 3 张图；把 4 张图的小红书草稿换平台后应重新检查。"""
    task.platform = "朋友圈"
    draft = make_draft(evidence.product_model)
    draft.images += [ImagePlacement(image_id="I4", after_paragraph=1, caption="测试", reason="测试")]
    assert "image_count" in {x.code for x in rule_checks(task, draft, validate_facts(extraction, evidence), evidence)}


class FakeEngine:
    """只验证流程行为；不声称模拟真实模型的生成质量。"""
    def __init__(self, evidence, extraction, always_bad=False, confirm=False):
        # 用可控返回值替代收费模型；calls 记录阶段顺序，便于检查修正次数。
        self.evidence, self.extraction = evidence, extraction
        self.always_bad, self.confirm = always_bad, confirm
        self.calls = []

    def run(self, stage, schema, payload):
        # 与真实 LangChainEngine 保持相同调用形式，因此 workflow 可以直接使用。
        # deep=True 深复制数据，防止流程修改一个结果影响下一次调用。
        self.calls.append(stage)
        if stage == "strategy":
            return Strategy(audience_problem="测试问题", angle="测试角度", priority_points=["测试屏幕"],
                            structure=["场景", "事实", "咨询"], tone="简洁", image_count=3, call_to_action="咨询")
        if stage == "extract": return self.extraction.model_copy(deep=True)
        if stage == "ground":
            return FactPlan(checks=[FactSupport(fact_id=f["fact_id"], status="支持", reason="合成资料明确支持")
                                    for f in payload["facts"]], selected_fact_ids=["F1"],
                            angle="测试屏幕", selection_reason="合成资料中选择屏幕事实")
        if stage == "generate":
            draft = make_draft(self.evidence.product_model)
            if self.always_bad: draft.cta.text = "绝不卡顿"
            return draft
        # 测试也必须提供全覆盖审核；空审核不能冒充通过。
        draft = Draft.model_validate(payload["draft"])
        return ModelReview(needs_confirmation=self.confirm, pending=["测试人工确认"] if self.confirm else [],
                           fact_checks=[FactAudit(location=location, fact_ids=ids,
                                                  status="通过" if ids else "不涉及", reason="合成资料测试结论")
                                        for location, _, ids in draft_sections(draft)],
                           compliance_checks=[ComplianceAudit(category=category, status="不涉及", reason="测试无此类风险")
                                              for category in COMPLIANCE_CATEGORIES])


def test_complete_workflow_and_safe_export(task, evidence, extraction):
    """合法依据走完四阶段；导出含四部分，但不含内部 ID 或图片字节。"""
    engine = FakeEngine(evidence, extraction)
    result = run_workflow(task, evidence, engine)
    assert result.review.passed
    assert engine.calls == ["strategy", "extract", "ground", "generate", "review"]
    text = to_markdown(result)
    assert all(x in text for x in ["一、场景判断", "二、知识库依据", "三、图文软文", "四、核验结果"])
    assert "internal-test-id" not in text
    assert "not-exported" not in result.model_dump_json()


def test_repair_runs_once_and_is_reviewed_again(task, evidence, extraction):
    """假模型持续返回风险词：只能修正一次，仍失败就导出待确认草稿。"""
    engine = FakeEngine(evidence, extraction, always_bad=True)
    result = run_workflow(task, evidence, engine)
    assert not result.review.passed
    assert result.repair_count == 1
    assert engine.calls.count("generate") == 2
    assert engine.calls.count("review") == 2
    assert "待确认草稿" in to_markdown(result)


def test_human_confirmation_does_not_auto_rewrite(task, evidence, extraction):
    """模型要求人工确认时停止自动改写，不能用再次生成代替人的判断。"""
    engine = FakeEngine(evidence, extraction, confirm=True)
    result = run_workflow(task, evidence, engine)
    assert not result.review.passed and result.review.needs_confirmation
    assert engine.calls.count("generate") == 1


def test_insufficient_images_does_not_repair_forever(task, evidence, extraction):
    """原始图片不足无法靠文案改写解决，应省下无意义的第二次生成。"""
    evidence.images = evidence.images[:1]
    engine = FakeEngine(evidence, extraction)
    result = run_workflow(task, evidence, engine)
    assert not result.review.passed
    assert engine.calls.count("generate") == 1


def test_changed_product_requires_new_evidence(task, evidence, extraction):
    """任务型号改了但资料没换，流程入口应立即拦截。"""
    task.product_model = "另一型号"
    with pytest.raises(EvidenceError, match="不一致"):
        run_workflow(task, evidence, FakeEngine(evidence, extraction))


def test_pagination_reads_all_pages(monkeypatch):
    """用迭代器依次返回两页响应，确认分页结果合并，没有只读第一页。"""
    client = ImaClient(Settings())
    pages = iter([{"knowledge_list": [{"title": "a"}], "next_cursor": "next", "is_end": False},
                  {"knowledge_list": [{"title": "b"}], "is_end": True}])
    monkeypatch.setattr(client, "call", lambda *args: next(pages))
    assert len(client.paged("wiki/v1/get_knowledge_list", {}, "knowledge_list")) == 2


def test_repeated_cursor_fails(monkeypatch):
    """接口重复返回同一游标时应报错，避免一直请求同一页。"""
    client = ImaClient(Settings())
    monkeypatch.setattr(client, "call", lambda *args: {"info_list": [], "next_cursor": "same", "is_end": False})
    with pytest.raises(ImaError, match="重复"):
        client.paged("wiki/v1/search_knowledge", {}, "info_list")


def test_connection_supports_actual_kb_field_names(monkeypatch):
    """核对 kb_id/kb_name 字段能正常映射到客户端连接状态。"""
    client = ImaClient(Settings())
    monkeypatch.setattr(client, "paged", lambda *args: [{"kb_id": "hidden-test-id", "kb_name": client.settings.kb_name}])
    assert client.connect() == client.settings.kb_name


def test_backend_error_not_mistaken_for_success(monkeypatch):
    """进程正常退出但 API code 表示失败，仍应显示业务错误。"""
    # SimpleNamespace 用几个属性模拟 subprocess 的返回对象，不真正运行 Node。
    client = ImaClient(Settings())
    monkeypatch.setattr("ima_client.subprocess.run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=json.dumps({"code": 110001, "msg": "测试权限不足"}), stderr=""))
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "node")
    with pytest.raises(ImaError, match="测试权限不足"):
        client.call("wiki/v1/search_knowledge", {})


def test_no_write_endpoints_allowed():
    """历史测试名称：当前允许新建个人笔记，但仍禁止此处的知识库添加接口。"""
    with pytest.raises(ImaError, match="读取"):
        ImaClient(Settings()).call("wiki/v1/add_knowledge", {})


def test_credentials_not_in_command_arguments(monkeypatch):
    """捕获拟发送的进程参数：密钥应在环境副本中，不出现在命令行参数。"""
    # replace 复制不可变 Settings 并替换指定字段；test-secret 是虚构占位值。
    client = ImaClient(replace(Settings(), ima_client_id="test-client", ima_api_key="test-secret"))
    captured = {}
    def run(args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        return SimpleNamespace(returncode=0, stdout='{"code":0,"data":{}}', stderr="")
    monkeypatch.setattr("ima_client.subprocess.run", run)
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "node")
    client.call("wiki/v1/search_knowledge", {})
    assert "test-secret" not in repr(captured["args"])
    assert captured["kwargs"]["env"]["IMA_OPENAPI_APIKEY"] == "test-secret"
    assert captured["kwargs"]["shell"] is False


@pytest.mark.parametrize("heading,expected", [
    ("# HUAWEI MateBook 14（鸿蒙版） - 产品事实", "HUAWEI MateBook 14（鸿蒙版）"),
    ("# HUAWEI MateBook 14（Windows版 2025）｜卖点与限制", "HUAWEI MateBook 14（Windows版 2025）"),
    ("# HUAWEI MateBook X Pro（2026款）", "HUAWEI MateBook X Pro（2026款）"),
    ("# HUAWEI MateBook 14 - Windows 2025", "HUAWEI MateBook 14 - Windows 2025"),
])
def test_document_label_not_confused_with_product_version(heading, expected):
    """多组文档标题应只去掉文档标签，保留 Windows/鸿蒙/年份等型号信息。"""
    assert model_from_document(heading, "fallback") == expected

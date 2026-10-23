"""本轮关键边界测试：模拟错误与真实数据格式，不访问云端、不写笔记。"""

import pytest

from checks import EvidenceError, itemized_checks, rule_checks, task_questions
from config import Settings
from evidence_utils import build_segments, materialize_facts, text_fingerprint
from exporter import public_source_url, table, to_markdown
from ima_client import ImaClient, ImaError
from schemas import (Fact, FactProposal, ModelReview,
                     SegmentExtraction, Strategy)
from workflow import run_workflow
# 导入合成夹具，复用相同产品、任务和草稿，避免每项测试重复造数据。
from test_core import evidence, task, extraction, FakeEngine, make_draft


def proposal(evidence, segment_id="S1-P001", **changes):
    """构造新提取格式：只含结论和编号，不由模型抄原句。"""
    values = dict(product_model=evidence.product_model,
                  facts=[FactProposal(claim="显示屏为14.2英寸", source_id="S1", segment_id=segment_id)])
    return SegmentExtraction(**(values | changes))


def test_segments_preserve_contiguous_original_and_table(evidence):
    evidence.sources[0].text = "开头段落。\n\n| 配置 | 条件 |\n| --- | --- |\n| 甲 | 典型值 |\n\n末尾限制。"
    segments = build_segments(evidence, target_chars=12)
    assert len(segments) == 3
    assert "典型值" in segments[1].text and "| 配置 |" in segments[1].text
    for segment in segments:
        assert segment.text == evidence.sources[0].text[segment.start:segment.end]
    facts = materialize_facts(proposal(evidence, segments[1].segment_id), evidence, segments)
    assert facts.facts[0].quote == segments[1].text
    assert facts.facts[0].source_hash == text_fingerprint(evidence.sources[0].text)


def test_changed_original_hash_stops_without_retry(evidence):
    evidence.sources[0].text_hash = text_fingerprint("旧的文字")
    with pytest.raises(EvidenceError, match="版本发生变化") as error:
        build_segments(evidence)
    assert not error.value.retryable


def test_changed_original_after_segmentation_stops(evidence):
    segments = build_segments(evidence)
    evidence.sources[0].text += "变化"
    with pytest.raises(EvidenceError, match="版本发生变化"):
        materialize_facts(proposal(evidence), evidence, segments)


@pytest.mark.parametrize("source_id,segment_id", [("S999", "S1-P001"), ("S1", "S2-P001")])
def test_invalid_selection_has_specific_failure_details(evidence, source_id, segment_id):
    selected = proposal(evidence)
    selected.facts[0].source_id = source_id
    selected.facts[0].segment_id = segment_id
    with pytest.raises(EvidenceError) as error:
        materialize_facts(selected, evidence, build_segments(evidence))
    assert error.value.retryable
    assert error.value.details[0]["source_id"] == source_id
    assert error.value.details[0]["segment_id"] == segment_id


class SelectionEngine(FakeEngine):
    """第一次选错编号、第二次正确；记录反馈，验证针对性重试。"""
    def __init__(self, evidence, extraction, *, fail_twice=False, **kwargs):
        super().__init__(evidence, extraction, **kwargs)
        self.extract_payloads = []
        self.fail_twice = fail_twice

    def run(self, stage, schema, payload):
        if stage == "extract":
            self.calls.append(stage)
            self.extract_payloads.append(payload)
            bad = self.fail_twice or len(self.extract_payloads) == 1
            return proposal(self.evidence, "S1-P999" if bad else payload["segments"][0]["segment_id"])
        return super().run(stage, schema, payload)


def test_bad_selection_retries_once_then_succeeds(task, evidence, extraction):
    engine = SelectionEngine(evidence, extraction)
    result = run_workflow(task, evidence, engine)
    assert result.review.passed and result.extraction_retry_count == 1
    assert engine.calls.count("extract") == 2
    assert engine.extract_payloads[1]["extraction_feedback"][0]["segment_id"] == "S1-P999"
    assert result.facts[0].quote == evidence.sources[0].text


def test_bad_selection_twice_stops_before_generation(task, evidence, extraction):
    engine = SelectionEngine(evidence, extraction, fail_twice=True)
    with pytest.raises(EvidenceError, match="已重新提取一次") as error:
        run_workflow(task, evidence, engine)
    assert engine.calls.count("extract") == 2 and "generate" not in engine.calls
    assert error.value.details


@pytest.mark.parametrize("changes", [{"conflicts": ["配置矛盾"]}, {"product_model": "另一型号"},
                                     {"blocking": ["当前承诺缺关键依据"]}])
def test_conflict_model_or_blocking_never_retry(task, evidence, extraction, changes):
    class StopEngine(FakeEngine):
        def run(self, stage, schema, payload):
            if stage == "extract":
                self.calls.append(stage)
                return proposal(evidence, **changes)
            return super().run(stage, schema, payload)
    engine = StopEngine(evidence, extraction)
    with pytest.raises(EvidenceError):
        run_workflow(task, evidence, engine)
    assert engine.calls.count("extract") == 1 and "generate" not in engine.calls


def test_missing_review_coverage_cannot_pass(task, evidence, extraction):
    class EmptyReviewEngine(FakeEngine):
        def run(self, stage, schema, payload):
            return ModelReview() if stage == "review" else super().run(stage, schema, payload)
    result = run_workflow(task, evidence, EmptyReviewEngine(evidence, extraction))
    assert not result.review.passed
    assert any(row.location == "标签1" and row.status == "待确认" for row in result.review.fact_checks)
    assert any(row.location == "图注1" and row.status == "待确认" for row in result.review.fact_checks)
    assert any(issue.code == "missing_compliance_review" for issue in result.review.issues)


def test_missing_price_unused_by_draft_is_notice_only(task, evidence, extraction):
    extraction.pending = ["未提供当前价格"]
    evidence.kb_name = "任德耀新建华为PC知识库copy"
    result = run_workflow(task, evidence, FakeEngine(evidence, extraction))
    assert result.review.passed
    assert "未提供当前价格" in result.review.notices
    assert not result.review.blocking
    assert "比赛适用性待举办方确认" in to_markdown(result)


def test_model_confirmation_row_stops_even_without_global_flag(task, evidence, extraction):
    class PendingReviewEngine(FakeEngine):
        def run(self, stage, schema, payload):
            value = super().run(stage, schema, payload)
            if stage == "review":
                value.compliance_checks[0].status = "待确认"
            return value
    result = run_workflow(task, evidence, PendingReviewEngine(evidence, extraction))
    assert not result.review.passed and result.review.needs_confirmation
    assert result.repair_count == 0


def test_same_number_with_changed_unit_is_error(task, evidence, extraction):
    draft = make_draft(evidence.product_model)
    draft.body[0].text = "显示屏为14.2元。"
    issues = rule_checks(task, draft, extraction.facts[0:0] + [extraction.facts[0].model_copy(update={"fact_id": "F1"})], evidence)
    assert "unsupported_quantity" in {issue.code for issue in issues}


def test_different_ids_same_image_bytes_are_duplicate(task, evidence, extraction):
    evidence.images[1].data = evidence.images[0].data
    issues = rule_checks(task, make_draft(evidence.product_model), [], evidence)
    assert "duplicate_image_content" in {issue.code for issue in issues}


def test_task_questions_do_not_reject_short_specific_scenario(task):
    task.audience, task.scenario = "大家", "日常用"
    assert len(task_questions(task)) == 2
    task.audience, task.scenario = "学生", "编程"
    assert task_questions(task) == []


def strategy():
    return Strategy(audience_problem="学习", angle="屏幕", priority_points=["屏幕"], structure=["场景", "事实"],
                    tone="简洁", image_count=3, call_to_action="咨询")


def retrieval_client(monkeypatch, evidence, *, hits=None):
    """一个白名单原文、一个历史稿；全库外部命中必须排除。"""
    client = ImaClient(Settings())
    client.kb_name = "测试copy库"
    evidence.folder_id = "selected-folder"
    evidence.sources[0].media_id = "accepted"
    entries = [{"media_id": "accepted", "title": "产品事实.md", "media_type": 7},
               {"media_id": "old", "title": "历史营销稿.md", "media_type": 7}]
    monkeypatch.setattr(client, "list_folder", lambda folder: entries)
    queries = []
    def search(query):
        queries.append(query)
        return hits or []
    monkeypatch.setattr(client, "search", search)
    return client, queries


def test_task_retrieval_uses_task_and_only_selected_folder(monkeypatch, task, evidence):
    client, queries = retrieval_client(monkeypatch, evidence, hits=[
        {"media_id": "accepted"}, {"media_id": "other-product"}, {"media_id": "old"}])
    result = client.retrieve_for_task(task, strategy(), evidence)
    assert task.product_model in queries[0] and task.scenario in queries[0] and task.platform in queries[0]
    assert result.sources[0].media_id == "accepted" and len(result.sources) == 1
    assert result.retrieval[0].excluded_count == 2
    assert not result.retrieval[0].fallback_used


def test_empty_task_search_falls_back_to_directory(monkeypatch, task, evidence):
    client, _ = retrieval_client(monkeypatch, evidence)
    result = client.retrieve_for_task(task, strategy(), evidence)
    assert result.sources and all(trace.fallback_used for trace in result.retrieval)
    assert any("目录回退" in notice for notice in result.notices)


def test_permission_failure_not_treated_as_empty_search(monkeypatch, task, evidence):
    client, _ = retrieval_client(monkeypatch, evidence)
    calls = []
    def denied(query):
        calls.append(query)
        raise ImaError("测试权限不足")
    monkeypatch.setattr(client, "search", denied)
    with pytest.raises(ImaError, match="权限不足"):
        client.retrieve_for_task(task, strategy(), evidence)
    assert len(calls) == 1


def test_refresh_clears_all_read_caches(monkeypatch):
    client = ImaClient(Settings())
    reads = []
    monkeypatch.setattr(client, "_read_text_uncached", lambda item: reads.append(item) or str(len(reads)))
    item = {"media_id": "one"}
    assert client.read_text(item) == client.read_text(item) == "1"
    client._folders["cached"] = []
    client._media["cached"] = {}
    client.refresh_cache()
    assert not client._folders and not client._media
    assert client.read_text(item) == "2"


def test_export_tables_and_url_filters():
    assert "a\\|b<br>c" in "\n".join(table(["列"], [["a|b\nc"]]))
    assert public_source_url("https://example.com/product")
    assert not public_source_url("https://example.com/image?token=private")
    assert not public_source_url("https://secret@example.com/product")


def test_image_api_limit_stops_instead_of_skipping_to_next_image(monkeypatch):
    client = ImaClient(Settings())
    entries = [{"media_id": "doc", "title": "产品事实.md", "media_type": 7},
               *[{"media_id": f"image-{i}", "title": f"image{i}.png", "media_type": 9} for i in range(3)]]
    monkeypatch.setattr(client, "search", lambda _: [])
    monkeypatch.setattr(client, "list_folder", lambda _: entries)
    monkeypatch.setattr(client, "read_text", lambda _: "本地测试产品正文。")
    calls = []
    def limited(media_id):
        calls.append(media_id)
        raise ImaError("测试资料获取次数已达上限")
    monkeypatch.setattr(client, "media_info", limited)
    with pytest.raises(ImaError, match="已达上限"):
        client.load_product({"media_id": "folder", "title": "测试产品"})
    assert calls == ["image-0"]


def test_moments_exposes_only_one_primary_fact_and_rejects_others(task, evidence, extraction):
    """多个已验证事实仍可归档，但朋友圈写作输入只能暴露一个核心卖点。"""
    task.platform = "朋友圈"
    extraction.facts.append(Fact(claim="重量约1.33kg，具体配置为准", quote="重量约1.33kg，具体配置为准", source_id="S1"))
    class ScopeEngine(FakeEngine):
        def __init__(self, *args):
            super().__init__(*args)
            self.writing_inputs = []
        def run(self, stage, schema, payload):
            result = super().run(stage, schema, payload)
            if stage == "strategy":
                result.priority_points = ["重量", "屏幕"]
            if stage == "ground":
                result.selected_fact_ids = ["F2"]
            if stage == "generate":
                self.writing_inputs.append(payload)
            return result
    engine = ScopeEngine(evidence, extraction)
    result = run_workflow(task, evidence, engine)
    assert len(result.facts) == 2  # 完整事实仍可追溯。
    assert all([row["fact_id"] for row in payload["facts"]] == ["F2"] for payload in engine.writing_inputs)
    assert result.strategy.priority_points == [extraction.facts[1].claim]
    assert "outside_primary_fact" in {issue.code for issue in result.review.issues}

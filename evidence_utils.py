"""原文取证工具：模型选片段，程序复制引句。

本文件不联网。阅读顺序：build_segments 建片段 → materialize_facts 组装事实。
字符下标来自同一份 Source.text；重新读取的文字改变后不能套用旧下标。
"""

from datetime import datetime, timezone, timedelta
import hashlib
import re

from checks import EvidenceError
from schemas import Evidence, EvidenceSegment, Fact, FactExtraction, SegmentExtraction


def timestamp() -> str:
    """统一记录北京时间；这是读取/操作时间，不是举办方资料更新时间。"""
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def text_fingerprint(text: str) -> str:
    """同样的文字得到同样的摘要，用来绑定片段与本次原文版本。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_segments(evidence: Evidence, target_chars: int = 1800) -> list[EvidenceSegment]:
    """按空行划分段落，再合并相邻段落；不在句子或表格中间硬截断。

    target_chars 是软目标。一个完整段落/表格很长时仍保留全文，避免丢失
    适用条件。最终语义审核仍会看完整 Source.text，分段不能代替语义核对。
    """
    segments = []
    seen = set()
    for source in evidence.sources:
        if source.source_id in seen:
            raise EvidenceError("原文来源编号重复，请重新加载资料。")
        seen.add(source.source_id)
        digest = text_fingerprint(source.text)
        if source.text_hash and source.text_hash != digest:
            raise EvidenceError(f"资料《{source.title}》版本发生变化，请重新加载，不沿用旧片段。")
        source.text_hash = digest
        source.loaded_at = source.loaded_at or evidence.loaded_at or timestamp()
        # finditer 返回每一段在原字符串中的位置；不使用 split 后重新拼接的文字。
        spans = [(m.start(), m.end()) for m in re.finditer(r".+?(?:\n[ \t]*\n|\Z)", source.text, re.S)]
        groups = []
        for start, end in spans:
            if groups and end - groups[-1][0] <= target_chars:
                groups[-1] = (groups[-1][0], end)
            else:
                groups.append((start, end))
        for start, end in groups:
            if not source.text[start:end].strip():
                continue
            segments.append(EvidenceSegment(
                segment_id=f"{source.source_id}-P{sum(s.source_id == source.source_id for s in segments)+1:03d}",
                source_id=source.source_id, start=start, end=end,
                text=source.text[start:end], source_hash=digest,
            ))
    if not segments:
        raise EvidenceError("当前原文没有可提取的文字，请重新加载资料。")
    return segments


def materialize_facts(extracted: SegmentExtraction, evidence: Evidence,
                      segments: list[EvidenceSegment]) -> FactExtraction:
    """检查模型选择的片段，然后直接复制原文；不猜测或模糊匹配错误编号。"""
    sources = {s.source_id: s for s in evidence.sources}
    segment_map = {s.segment_id: s for s in segments}
    facts, failures = [], []
    for index, proposal in enumerate(extracted.facts, 1):
        source = sources.get(proposal.source_id)
        segment = segment_map.get(proposal.segment_id)
        reason = ""
        if source is None:
            reason = "所选来源不存在"
        elif segment is None or segment.source_id != proposal.source_id:
            reason = "片段不存在或不属于所选来源"
        elif (text_fingerprint(source.text) != segment.source_hash
              or source.text[segment.start:segment.end] != segment.text):
            # 版本变化不是模型重选编号能修复的问题，要求重新固定证据。
            raise EvidenceError(f"资料《{source.title}》版本发生变化，请重新加载资料。")
        if reason:
            failures.append({"index": index, "claim": proposal.claim, "source_id": proposal.source_id,
                             "source_title": source.title if source else "不存在的来源",
                             "segment_id": proposal.segment_id, "reason": reason})
            continue
        facts.append(Fact(claim=proposal.claim, source_id=proposal.source_id,
                          quote=source.text[segment.start:segment.end], segment_id=segment.segment_id,
                          source_hash=segment.source_hash, quote_start=segment.start, quote_end=segment.end))
    if failures:
        raise EvidenceError("提取结果选择了无效原文片段，已停止生成。", details=failures, retryable=True)
    return FactExtraction(product_model=extracted.product_model, facts=facts,
                          conflicts=extracted.conflicts, pending=extracted.pending, blocking=extracted.blocking)

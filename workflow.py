"""固定工作流：策略 -> 事实提取 -> 写前审查选材 -> 生成 -> 双重核验。

整个首版不依赖 LangGraph。通过回调向网页报告当前进度。

输入是任务 task、已经加载的依据 evidence 和模型入口 engine；输出为 Result。
本模块只负责安排步骤，网络细节在 llm.py，检查细节在 checks.py，显示细节在 app.py。
"""

from datetime import datetime, timedelta, timezone
from typing import Callable

from checks import (EvidenceError, rule_checks, validate_facts, task_questions, draft_sections,
                    itemized_checks, image_audit, validate_fact_plan)
from config import COMPETITION_KB_NAME
from evidence_utils import build_segments, materialize_facts
from fact_cards import facts_from_cards
from inspiration import creative_context
from campaigns import require_current_campaign
from schemas import (Evidence, FactExtraction, SegmentExtraction, ModelReview, PLATFORM_RULES,
                     Result, Review, Strategy, Task, Draft, Issue, FactPlan, normalize_model, COMPLIANCE_CATEGORIES)


def run_workflow(task: Task, evidence: Evidence, engine, progress: Callable[[str], None] = lambda _: None,
                 previous: Draft | None = None,
                 evidence_loader: Callable[[Task, Strategy, Evidence], Evidence] | None = None) -> Result:
    """执行一次生成，或基于上一稿按新要求重新生成。

    engine 提供 run(阶段名, 输出格式, 输入数据)。真实运行使用 LangChainEngine，
    测试使用 FakeEngine，因此可以验证流程而不花模型调用费。
    progress 是回调函数：网页传 st.write 来展示进度；默认函数不做任何事。
    previous 为可选上一稿；None 表示从头写。这里不会把结果写入 ima。
    """
    # 入口先核对产品，防止把 A 产品的任务与 B 产品的资料放在一起。
    if normalize_model(task.product_model) != normalize_model(evidence.product_model):
        raise EvidenceError("任务与证据的产品不一致，请重新加载产品资料。")
    require_current_campaign(task)
    questions = task_questions(task)
    if questions:
        raise EvidenceError("请补充任务：" + "；".join(questions))
    # 本次结果保存独立快照，之后勾选图片/刷新资料不会偷偷改变已生成的档案。
    evidence = evidence.model_copy(deep=True)
    task_data = task.model_dump(exclude={"inspiration", "campaign_id", "campaign_signature"})
    inspiration = creative_context(task.inspiration)
    if evidence.fact_card_mode:
        facts = facts_from_cards(evidence)
        extraction = FactExtraction(product_model=evidence.product_model, facts=facts)
    # model_dump 把 Pydantic 对象转成普通字典，便于后面序列化为 JSON 发给模型。
    # 第 1 阶段：只看任务和平台规则，决定内容角度，不在此阶段编写产品参数。
    progress("正在形成场景与平台策略")
    strategy = engine.run("strategy", Strategy, {
        "task": task_data, "platform_rules": PLATFORM_RULES[task.platform],
        "creative_inspiration_only": inspiration,
    })
    # 不能让模型覆盖手册的图片数量上限、下限。
    rules = PLATFORM_RULES[task.platform]
    strategy.image_count = max(rules["min_images"], min(rules["max_images"], strategy.image_count))
    if task.platform == "朋友圈":
        # 手册要求一个核心卖点；把策略候选方向也缩到一个，减少后续堆砌。
        strategy.priority_points = strategy.priority_points[:1]
    if evidence_loader is not None and not evidence.fact_card_mode:
        progress("正在按型号、场景与平台查找资料")
        evidence = evidence_loader(task, strategy, evidence)
        if normalize_model(evidence.product_model) != normalize_model(task.product_model):
            raise EvidenceError("任务检索返回了其他型号，已停止生成。")
    segments = build_segments(evidence)
    evidence_payload = evidence.model_dump()
    # 第 2 阶段：从原文提取事实，并让程序验证来源、型号和连续引句。
    # validate_facts 若发现冲突或假引句会抛错，后面的生成阶段不会继续。
    progress("正在提取并核对原文依据")
    extract_payload = {"task": task_data,
                       "sources": [{"source_id": s.source_id, "title": s.title} for s in evidence.sources],
                       "segments": [segment.model_dump() for segment in segments]}
    extraction_retry_count = 0
    for extraction_attempt in range(0 if evidence.fact_card_mode else 2):
        try:
            selected = engine.run("extract", SegmentExtraction, extract_payload)
            if selected.conflicts:
                raise EvidenceError("资料存在冲突，需要人工确认：" + "；".join(selected.conflicts))
            if normalize_model(selected.product_model) != normalize_model(evidence.product_model):
                raise EvidenceError("模型提取的产品型号与当前证据不一致，请人工确认。")
            if selected.blocking:
                raise EvidenceError("当前任务缺少关键依据：" + "；".join(selected.blocking))
            # 兼容旧版调用者的 FactExtraction，但仍用严格原句检查，不做模糊放行。
            extraction = (selected if isinstance(selected, FactExtraction)
                          else materialize_facts(selected, evidence, segments))
            facts = validate_facts(extraction, evidence)
            break
        except EvidenceError as exc:
            if not exc.retryable or extraction_attempt:
                raise EvidenceError(str(exc) + (" 已重新提取一次，仍未通过。" if extraction_attempt else ""),
                                    details=exc.details) from exc
            extraction_retry_count = 1
            progress("发现取证编号或引句问题，正在针对性重新提取一次")
            extract_payload = {**extract_payload, "extraction_feedback": exc.details}
    # 第 3 阶段先审查结论与原文的支持关系，再根据任务明确选择卖点。
    progress("正在检查事实支持关系并选择写作卖点")
    fact_plan = engine.run("ground", FactPlan, {
        "task": task_data, "strategy": strategy.model_dump(),
        "facts": [fact.model_dump() for fact in facts],
    })
    generation_facts = validate_fact_plan(fact_plan, facts, task)
    strategy.angle = fact_plan.angle
    strategy.priority_points = [fact.claim for fact in generation_facts]
    # 第 4 阶段仅看到已选事实和已核对型号的图片；修改任务可参考上一稿。
    payload = {
        "task": task_data, "strategy": strategy.model_dump(),
        "creative_inspiration_only": inspiration,
        "facts": [fact.model_dump() for fact in generation_facts],
        "writing_scope": "朋友圈只围绕这一条已核对结论写，原文片段中的其他参数不是额外卖点授权。" if task.platform == "朋友圈" else "按策略组织有依据的事实。",
        "images": [image.model_dump() for image in evidence.images
                   if image.verified and normalize_model(image.product_model) == normalize_model(task.product_model)],
        "previous_draft": previous.model_dump() if previous and normalize_model(previous.product_model) == normalize_model(task.product_model) else None,
    }
    progress("正在生成图文内容")
    draft = engine.run("generate", Draft, payload)
    repair_count = 0
    for attempt in range(2):
        # 模型没有看到图片字节，不能让它凭文件名猜画面。图注由程序限制为
        # 索引中的已知说明；没有说明时只用当前型号的中性文字。
        image_map = {image.image_id: image for image in evidence.images}
        for placement in draft.images:
            image = image_map.get(placement.image_id)
            if image is not None:
                placement.caption = image.description.strip() or f"{image.product_model} 产品图"
        # range(2) 只产生 0、1：第一次检查，以及最多一次改写后的第二次检查。
        # “格式重试”在 llm.py 内部处理，与这里的“内容改写”是两回事。
        progress("正在核验事实、图片及营销表述" if not attempt else "正在重新核验修正结果")
        deterministic = rule_checks(task, draft, facts, evidence)
        allowed_ids = {fact.fact_id for fact in generation_facts}
        if any(set(ids) - allowed_ids for _, _, ids in draft_sections(draft)):
            deterministic.append(Issue(severity="error", code="outside_primary_fact",
                                       message="稿件使用了本次选材范围之外的事实编号，请围绕已选卖点改写。"))
        # 第 5 阶段：模型对照原始资料检查语义，补充固定规则难以识别的问题。
        model_review = engine.run("review", ModelReview, {
            "task": task_data, "draft": draft.model_dump(),
            "facts": [{**fact.model_dump(), "product_model": evidence.product_model,
                       "source_title": next(source.title for source in evidence.sources if source.source_id == fact.source_id)}
                      for fact in facts], "evidence": evidence_payload,
            "selected_fact_ids": fact_plan.selected_fact_ids,
            "audit_locations": [{"location": location, "text": text, "fact_ids": ids}
                                for location, text, ids in draft_sections(draft)],
            "required_compliance_categories": COMPLIANCE_CATEGORIES,
        })
        fact_rows, audit_issues = itemized_checks(draft, model_review, facts)
        issues = deterministic + model_review.issues + audit_issues
        issues = list({(issue.code, issue.message): issue for issue in issues}.values())
        # dict.fromkeys 用字典键去重，并保留第一次出现的顺序，避免同一提醒重复显示。
        blocking = list(dict.fromkeys(model_review.blocking + (model_review.pending if model_review.needs_confirmation else [])))
        notices = extraction.pending + evidence.notices + ([] if model_review.needs_confirmation else model_review.pending)
        if evidence.kb_name and evidence.kb_name != COMPETITION_KB_NAME:
            notices.append(f"当前来源为“{evidence.kb_name}”，与手册指定库不同；已按用户选择用于探索，比赛适用性待举办方确认。")
        notices.append("图注采用图片索引说明或中性型号说明；未执行自动视觉识别。")
        needs_confirmation = bool(model_review.needs_confirmation or blocking
                                  or any(row.status == "待确认" for row in fact_rows)
                                  or any(row.status == "待确认" for row in model_review.compliance_checks))
        passed = not any(issue.severity == "error" for issue in issues) and not needs_confirmation
        # 仅 warning 不会阻止通过；需要人工确认时，即使没有 error 也不能通过。
        review = Review(passed=passed, issues=issues, pending=blocking, blocking=blocking,
                        notices=list(dict.fromkeys(notices)), needs_confirmation=needs_confirmation,
                        fact_checks=fact_rows, image_checks=image_audit(draft, evidence, issues),
                        compliance_checks=model_review.compliance_checks)
        if passed or attempt or needs_confirmation:
            # 已通过、已经到第二次检查、或需要人工确认，三者任一成立就结束循环。
            break
        # 原始图片不足无法靠改写补齐，避免白白再调一次生成模型。
        if sum(image.verified for image in evidence.images) < rules["min_images"]:
            break
        progress("发现可修正问题，正在修正一次")
        draft = engine.run("generate", Draft, {
            # **payload 复制原有输入；同名字段写在后面，会替换前面的旧值。
            # 将问题明细和本次草稿交回生成阶段，下一轮必须重新核验。
            **payload, "previous_draft": draft.model_dump(),
            "repair_feedback": [issue.model_dump() for issue in issues],
        })
        repair_count = 1
    # 汇总成一个对象。这里固定 UTC+8，对应北京时间，不依赖电脑当前时区。
    if evidence.fact_card_mode:
        facts_from_cards(evidence)
    return Result(task=task, strategy=strategy, facts=facts, evidence=evidence,
                  draft=draft, review=review, fact_plan=fact_plan, repair_count=repair_count,
                  extraction_retry_count=extraction_retry_count,
                  generated_at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"))

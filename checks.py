"""确定性核验。程序检查引用和格式，模型另外检查语义与原文支持关系。"""

import re
import hashlib
from schemas import (Draft, Evidence, Fact, FactExtraction, Issue, PLATFORM_RULES, Task, normalize_model,
                     FactAudit, FactPlan, ImageAudit, ModelReview, COMPLIANCE_CATEGORIES)


RISKY_EXPRESSIONS = [r"全网第一|全球第一|行业第一|最强|绝不卡顿|永不卡顿",
                     r"永久保修|终身免费|百分之百保证|100%保证|吊打|碾压", r"亲测|我用了|用了一个月"]


class EvidenceError(RuntimeError):
    """依据缺失、型号不同或原文无法匹配时，阻止后续写作。"""
    def __init__(self, message: str, *, details: list[dict] | None = None, retryable: bool = False):
        super().__init__(message)
        self.details = details or []
        # 只有错误编号/错误引句等提取问题可以重试；冲突、版本变化不会自动重试。
        self.retryable = retryable


def compact(text: str) -> str:
    """去掉空格、换行等空白，避免 PDF 换行等排版差异影响引用匹配。"""
    # 正则表达式 \s+ 表示“连续一个或多个空白字符”；替换为没有字符。
    return re.sub(r"\s+", "", text)


def validate_facts(extracted: FactExtraction, evidence: Evidence) -> list[Fact]:
    """确保原文和引用确实存在；这一步不能替代语义审查。"""
    if extracted.conflicts:
        raise EvidenceError("资料存在冲突，需要人工确认：" + "；".join(extracted.conflicts))
    if normalize_model(extracted.product_model) != normalize_model(evidence.product_model):
        raise EvidenceError("模型提取的产品型号与所选产品不一致，请人工确认。")
    sources = {source.source_id: source for source in evidence.sources}
    # 来源表用 S1/S2 这样的编号查找资料，比在列表里反复寻找更直接。
    if not extracted.facts:
        raise EvidenceError("没有提取到可支持本次内容的产品事实，请补充资料或调整任务。")
    facts, failures = [], []
    for index, fact in enumerate(extracted.facts, 1):
        source = sources.get(fact.source_id)
        if not source or not compact(fact.quote) or compact(fact.quote) not in compact(source.text):
            failures.append({"index": index, "claim": fact.claim, "source_id": fact.source_id,
                             "source_title": source.title if source else "不存在的来源", "quote": fact.quote,
                             "reason": "来源不存在" if not source else "原文引用不能连续匹配，请核对是否改写或拼接"})
            continue
        # 重新按顺序发 F1/F2 编号，避免模型重复/乱编编号；不改动原模型实例。
        facts.append(fact.model_copy(update={"fact_id": f"F{len(facts)+1}"}))
    if failures:
        raise EvidenceError("提取结果的原文引用无法匹配，已停止生成。请查看具体失败项。",
                            details=failures, retryable=True)
    return facts


def task_questions(task: Task) -> list[str]:
    """只拦截明显模糊的必要项；短文本不一定模糊，不额外索取电话或预算。"""
    questions = []
    vague_people = {"所有人", "大家", "任何人", "不限", "随便", "普通人", "用户"}
    vague_scene = {"日常用", "日常使用", "好用", "随便写", "随便", "不知道", "都行", "无"}
    clean = lambda value: re.sub(r"[\s，。！？!?、]", "", value)
    if clean(task.audience) in vague_people:
        questions.append("请在“目标人群”明确一个主要人群，例如大学新生、出差办公人员或企业采购。")
    if clean(task.scenario) in vague_scene:
        questions.append("请在“使用场景”补充主要用途或问题，例如上课携带并整理课业、出差处理文档。")
    return questions


def validate_fact_plan(plan: FactPlan, facts: list[Fact], task: Task) -> list[Fact]:
    """写前核对覆盖、支持结论和编号；不猜关键词，也不默认使用第一条。"""
    known = {fact.fact_id: fact for fact in facts}
    ids = [row.fact_id for row in plan.checks]
    if len(ids) != len(set(ids)) or set(ids) != set(known):
        raise EvidenceError("写前事实审查漏项、重复或含未知编号，已停止生成。")
    failures = []
    for row in plan.checks:
        fact = known[row.fact_id]
        allowed = fact.quote + task.product_model
        numbers = lambda text: set(re.findall(r"\d+(?:\.\d+)?", text))
        units = r"万元|元|千克|公斤|克|英寸|小时|分钟|赫兹|kg|GHz|MHz|GB|TB|Wh|Hz|g|W|%|％"
        quantities = lambda text: {(n, u.casefold().replace("％", "%").replace("公斤", "kg").replace("千克", "kg").replace("克", "g"))
                                   for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(" + units + r")", text)}
        reason = row.reason if row.status != "支持" else ""
        if numbers(fact.claim) - numbers(allowed) or quantities(fact.claim) - quantities(allowed):
            reason = "结论含原文没有的数字或数值单位组合。"
        if reason:
            failures.append({"fact_id": fact.fact_id, "claim": fact.claim, "source_id": fact.source_id,
                             "quote": fact.quote, "reason": reason})
    if failures:
        raise EvidenceError("写前事实支持检查未通过，已停止写作。请核对结论、条件与产品原文。", details=failures)
    selected = plan.selected_fact_ids
    if (not selected or len(selected) > 4 or len(selected) != len(set(selected)) or set(selected) - known.keys()
            or (task.platform == "朋友圈" and len(selected) != 1)):
        raise EvidenceError("写前选材编号无效；朋友圈必须明确选择一个有依据的主卖点。")
    return [known[fact_id] for fact_id in selected]


def draft_sections(draft: Draft) -> list[tuple[str, str, list[str]]]:
    """所有审核层共用同一套位置名称，避免标题、标签和图注被遗漏。"""
    sections = [("标题", draft.title.text, draft.title.fact_ids), ("导语", draft.intro.text, draft.intro.fact_ids)]
    sections += [(f"正文第{i}段", b.text, b.fact_ids) for i, b in enumerate(draft.body, 1)]
    sections += [("行动引导", draft.cta.text, draft.cta.fact_ids)]
    sections += [(f"标签{i}", text, []) for i, text in enumerate(draft.tags, 1)]
    sections += [(f"图注{i}", image.caption, []) for i, image in enumerate(draft.images, 1)]
    return sections


def itemized_checks(draft: Draft, review: ModelReview, facts: list[Fact]):
    """要求语义审核覆盖全稿与五类风险；未报告不能被自动理解成通过。"""
    issues, rows = [], []
    expected = {location for location, _, _ in draft_sections(draft)}
    known_facts = {f.fact_id for f in facts}
    reported = {}
    for row in review.fact_checks:
        if row.location not in expected or row.location in reported:
            issues.append(Issue(severity="error", code="invalid_audit_location", message=f"事实审核位置无效或重复：{row.location}。"))
        reported[row.location] = row
    for location, text, declared in draft_sections(draft):
        row = reported.get(location)
        if row is None:
            row = FactAudit(location=location, status="待确认", reason="模型未报告此位置的语义核验。", fact_ids=declared)
        # 展示原稿中的真实文字，不采信审核模型重新转述的 text。
        row = row.model_copy(update={"text": text})
        if (set(row.fact_ids) - known_facts or set(declared) - set(row.fact_ids)
                or (declared and row.status == "不涉及")):
            row = row.model_copy(update={"status": "需处理", "reason": "审核所用事实编号缺失、无效，或把含事实的内容标为不涉及。"})
        rows.append(row)
        if row.status in {"需处理", "待确认"}:
            issues.append(Issue(severity="error", code="semantic_fact", message=f"{location}：{row.reason}"))
    categories = {row.category for row in review.compliance_checks}
    for category in COMPLIANCE_CATEGORIES:
        if category not in categories:
            issues.append(Issue(severity="error", code="missing_compliance_review", message=f"缺少“{category}”的审核结论。"))
    for row in review.compliance_checks:
        if row.status in {"需处理", "待确认"}:
            issues.append(Issue(severity="error", code="compliance_review", message=f"{row.category} / {row.location}：{row.reason}"))
    return rows, issues


def image_audit(draft: Draft, evidence: Evidence, issues: list[Issue]) -> list[ImageAudit]:
    """图片表由程序根据当前素材组装，不声称已做视觉识别。"""
    images = {image.image_id: image for image in evidence.images}
    rows, used, contents = [], set(), set()
    for placement in draft.images:
        image = images.get(placement.image_id)
        digest = hashlib.sha256(image.data).hexdigest() if image and image.data else ""
        reasons = []
        if image is None:
            reasons.append("不存在的图片")
        elif not image.verified or normalize_model(image.product_model) != normalize_model(evidence.product_model):
            reasons.append("型号未确认或不一致")
        if placement.image_id in used or (digest and digest in contents):
            reasons.append("图片内容重复，不能凑数")
        if placement.after_paragraph > len(draft.body):
            reasons.append("插入位置超出正文")
        if any(i.code == "image_count" for i in issues):
            reasons.append("整篇图片数量不符合当前平台")
        rows.append(ImageAudit(image_id=placement.image_id, name=image.name if image else placement.image_id,
                               source_title=image.source_title if image else "不存在", product_model=image.product_model if image else "待确认",
                               verification_method=(image.verification_method or "索引或人工核对（历史记录）") if image else "无",
                               location="导语后" if placement.after_paragraph == 0 else f"正文第{placement.after_paragraph}段后",
                               caption=placement.caption, status="需处理" if reasons else "通过",
                               reason="；".join(reasons) or "已按素材记录核对型号、数量与位置；未进行自动视觉识别。"))
        used.add(placement.image_id)
        if digest:
            contents.add(digest)
    return rows


def rule_checks(task: Task, draft: Draft, facts: list[Fact], evidence: Evidence) -> list[Issue]:
    """用明确的程序规则收集草稿问题，返回列表，空列表表示本层没有发现问题。

    本函数不调用大模型。规则检查可重复，但覆盖范围有限：引用出现过不等于
    论断正确，数字存在也不等于单位/适用条件正确，语义要再交给 review 阶段。
    """
    issues = []

    def error(code: str, message: str):
        # 嵌套小函数统一创建错误对象；code 给程序看，message 给使用者看。
        issues.append(Issue(severity="error", code=code, message=message))

    if normalize_model(draft.product_model) != normalize_model(evidence.product_model):
        error("model_mismatch", "草稿产品型号与当前证据不一致。")
    fact_map = {fact.fact_id: fact for fact in facts}
    blocks = [("标题", draft.title), ("导语", draft.intro)]
    # 将标题、导语、正文、行动引导放进同一列表，使用相同规则逐项检查。
    # enumerate(..., 1) 同时取出“从 1 开始的序号”和段落本身。
    blocks += [(f"正文第{i}段", block) for i, block in enumerate(draft.body, 1)]
    blocks += [("行动引导", draft.cta)]
    for name, block in blocks:
        missing = set(block.fact_ids) - fact_map.keys()
        # set 是不重复的集合；集合相减得到“草稿用了，但事实表里没有”的编号。
        if missing:
            error("unknown_fact", f"{name}引用了不存在的事实编号。")
        # 结论 claim 本身仍可能有错，不能拿它作为新数字的依据；只比较原句。
        supported = task.product_model + " ".join(fact_map[x].quote for x in block.fact_ids if x in fact_map)
        # 数字逐项比对，可发现凭空增加的价格、重量、性能数值；语义仍交给模型审核。
        # 例如正文新增“9999 元”而所引用事实没有 9999，就会报错。
        # 此规则也可能把“3 个理由”的编号当作数字，需要按核验记录修改。
        numbers = set(re.findall(r"\d+(?:\.\d+)?", block.text))
        allowed = set(re.findall(r"\d+(?:\.\d+)?", supported))
        if numbers - allowed:
            error("unsupported_number", f"{name}出现引用依据中没有的数字：{'、'.join(sorted(numbers - allowed))}。")
        # 同样的数字也可能被换成别的单位，例如 1.33kg → 1.33元。
        units = r"万元|元|千克|公斤|克|英寸|小时|分钟|赫兹|kg|GHz|MHz|GB|TB|Wh|Hz|kg|g|W|%|％"
        quantity = lambda value: {(n, u.replace("％", "%").replace("公斤", "kg").replace("千克", "kg").replace("克", "g"))
                                  for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(" + units + r")", value)}
        if quantity(block.text) - quantity(supported):
            error("unsupported_quantity", f"{name}中的数值与单位组合没有对应原文依据，请核对含义与适用条件。")
        # 非数字政策同样需要依据，例如“有现货”“终身保修”。精细语义再由模型核对。
        for match in re.finditer(r"到手价|补贴|赠品|现货|库存充足|永久保修|终身保修|免费赠送|可提前预约|支持预约|可(?:以)?在样机上安装|支持现场安装|直接连接试用", block.text):
            prefix = block.text[max(0, match.start()-8):match.start()]
            if not re.search(r"不承诺|无依据|未确认|不含|不提供|未知|没有", prefix) and match.group() not in supported:
                error("unsupported_policy", f"{name}使用了没有引用依据的销售承诺：{match.group()}。")
        # 重量和电池参数不能被包装成未经测试的携带或续航保证。
        for match in re.finditer(r"不会(?:明显)?(?:增加|带来|造成)(?:携带)?负担|毫无负担|不添负担|全天课程|早课到晚自习|不用找插座|减少充电频率", block.text):
            prefix = block.text[max(0, match.start() - 10):match.start()]
            if not re.search(r"不承诺|不代表|不能推断|不能说", prefix) and match.group() not in supported:
                error("unsupported_experience", f"{name}把规格推成了无依据的体验承诺：{match.group()}。请删除推论，保留参数与适用条件。")
    all_text = "\n".join(block.text for _, block in blocks) + "\n" + " ".join(draft.tags)
    # 风险词覆盖标题/正文/标签/图注；这只是有限词表，不是完整的法规审核。
    all_text += "\n" + "\n".join(image.caption for image in draft.images)
    for pattern in RISKY_EXPRESSIONS:
        match = re.search(pattern, all_text)
        if match:
            error("risky_expression", f"发现需要删除或重新核实的宣传表达：{match.group()}。")
    rules = PLATFORM_RULES[task.platform]
    # 平台规则来自 schemas.py 一处，界面展示和这里的检查共用它，避免不一致。
    if not rules["min_images"] <= len(draft.images) <= rules["max_images"]:
        error("image_count", f"{task.platform}要求{rules['min_images']}-{rules['max_images']}张不同图片，当前为{len(draft.images)}张。")
    images = {image.image_id: image for image in evidence.images}
    used = set()
    used_contents = set()
    # 图片检查：ID 存在、未重复、已核对型号、插入位置没有超出正文。
    for placement in draft.images:
        image = images.get(placement.image_id)
        if not image:
            error("unknown_image", "配图方案引用了不存在的图片。")
            continue
        if placement.image_id in used:
            error("duplicate_image", "同一图片被重复使用，不能重复凑数。")
        used.add(placement.image_id)
        digest = hashlib.sha256(image.data).hexdigest() if image.data else ""
        if digest and digest in used_contents:
            error("duplicate_image_content", "不同图片条目的内容相同，不能换名称重复凑数。")
        if digest:
            used_contents.add(digest)
        if not image.verified or normalize_model(image.product_model) != normalize_model(evidence.product_model):
            error("unverified_image", f"图片 {image.name} 的型号尚未确认或与当前产品不同。")
        if placement.after_paragraph > len(draft.body):
            error("image_position", f"图片 {image.name} 的插入位置超过正文范围。")
    if task.platform == "小红书" and not draft.tags:
        error("missing_tags", "小红书内容缺少话题标签。")
    if task.platform == "朋友圈" and len(all_text) > 500:
        # 这里给 warning（建议），不会像 error 一样单独阻止最终通过。
        issues.append(Issue(severity="warning", code="too_long", message="朋友圈内容较长，建议进一步压缩。"))
    return issues

"""将结果导出为可直接查看的 Markdown，保留事实及图片来源。"""

from urllib.parse import urlsplit
from io import BytesIO
import re
from zipfile import ZipFile, ZIP_DEFLATED

from PIL import Image, UnidentifiedImageError

from checks import EvidenceError, rule_checks, validate_fact_plan, itemized_checks, draft_sections
from config import COMPETITION_KB_NAME
from schemas import Result, Task, ModelReview


def require_publishable(result: Result, current_task: Task | None = None):
    """导出也执行门禁，避免仅靠页面隐藏按钮。"""
    if current_task is not None and current_task != result.task:
        raise EvidenceError("任务已修改，请重新生成后领取。")
    from campaigns import require_current_campaign
    require_current_campaign(result.task)
    if result.evidence.fact_card_mode:
        from fact_cards import facts_from_cards
        if result.facts != facts_from_cards(result.evidence):
            raise EvidenceError("稿件事实与当前事实卡不一致，请重新生成。")
    review = result.review
    if (not review.passed or review.needs_confirmation or review.blocking
            or any(row.severity == "error" for row in review.issues)
            or any(row.status in {"需处理", "待确认"} for row in review.fact_checks + review.compliance_checks)):
        raise EvidenceError("尚未通过检查，不能领取发布成品。")
    if result.fact_plan is None:
        raise EvidenceError("此结果缺少写前事实审查，请重新生成。")
    selected = validate_fact_plan(result.fact_plan, result.facts, result.task)
    allowed = {fact.fact_id for fact in selected}
    if any(set(ids) - allowed for _, _, ids in draft_sections(result.draft)):
        raise EvidenceError("稿件使用了本次选材范围以外的事实，不能领取。")
    _, audit_issues = itemized_checks(result.draft, ModelReview(
        fact_checks=review.fact_checks, compliance_checks=review.compliance_checks), result.facts)
    if audit_issues:
        raise EvidenceError("成品缺少完整且通过的逐项审核，请重新生成。")
    if any(issue.severity == "error" for issue in rule_checks(result.task, result.draft, result.facts, result.evidence)):
        raise EvidenceError("成品复核未通过，请重新生成。")


def to_publish_text(result: Result, current_task: Task | None = None) -> str:
    """只输出给读者看的文字，不混入引用表、来源和审核记录。"""
    require_publishable(result, current_task)
    draft = result.draft
    # 朋友圈直接发布短消息，不把内部主题标题再贴一次。
    parts = ([] if result.task.platform == "朋友圈" else [draft.title.text])
    parts += [draft.intro.text, *[block.text for block in draft.body], draft.cta.text]
    if draft.tags:
        parts.append(" ".join(draft.tags))
    return "\n\n".join(parts).strip() + "\n"


def to_publish_package(result: Result, current_task: Task | None = None) -> bytes:
    """正文与原图组成 ZIP；按排版顺序编号，不相信原文件名中的路径和扩展名。"""
    text = to_publish_text(result, current_task)
    images = {image.image_id: image for image in result.evidence.images}
    output = BytesIO()
    order = []
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("发布文案.txt", text.encode("utf-8-sig"))
        for index, placement in enumerate(sorted(result.draft.images, key=lambda p: p.after_paragraph), 1):
            image = images[placement.image_id]
            try:
                with Image.open(BytesIO(image.data)) as picture:
                    extension = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}.get(picture.format)
                    picture.verify()
                if not extension:
                    raise ValueError("不支持的图片格式")
            except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
                raise EvidenceError("图片无法完整解码，未创建发布包，请重新加载素材。") from exc
            stem = re.split(r"[/\\]", image.name)[-1].rsplit(".", 1)[0]
            stem = re.sub(r"[^\w\-]", "_", stem)[:60] or "产品图"
            filename = f"图片/{index:02d}_{stem}{extension}"
            archive.writestr(filename, image.data)
            location = "导语后" if placement.after_paragraph == 0 else f"正文第{placement.after_paragraph}段后"
            order.append(f"{filename}：{location}")
        archive.writestr("配图顺序.txt", "\n".join(order).encode("utf-8-sig"))
    return output.getvalue()


def table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """生成可读的 Markdown 表；竖线和换行需转义，避免一个单元格拆成多列。"""
    def cell(value):
        return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")

    return ["| " + " | ".join(map(cell, headers)) + " |",
            "| " + " | ".join("---" for _ in headers) + " |",
            *["| " + " | ".join(map(cell, row)) + " |" for row in rows], ""]


def public_source_url(value: str) -> str:
    """只保留不带查询串、片段或账号的 HTTPS 来源页，排除临时凭证链接。"""
    try:
        parsed = urlsplit(value)
        if (parsed.scheme == "https" and parsed.hostname and not parsed.query
                and not parsed.fragment and not parsed.username and not parsed.password):
            return value
    except ValueError:
        pass
    return ""


def to_markdown(result: Result) -> str:
    """把完整结果拼成 Markdown 字符串，供下载、复制或写入文字笔记。

    本函数不会调用模型或 ima，也不会把图片二进制写进文件。导出中的配图是
    图片名称、图注、来源与位置说明；实际发布时仍要取用有权限的原图。
    """
    # 一行取出三个常用对象，后续写 task/draft/evidence 更简洁。
    task, draft, evidence = result.task, result.draft, result.evidence
    status = "检查通过，发布前请人工复核" if result.review.passed else "待确认草稿，尚未通过检查"
    # Markdown：# 是标题，> 是引用说明，- 是列表；空字符串生成空行。
    # 先用列表保存每行，最后 join 成完整文本，便于按顺序追加内容。
    lines = [f"# {draft.title.text}", "", f"> {status}", f"> 生成时间：{result.generated_at}", "",
             "## 一、场景判断", "", f"- 产品：{task.product_model}", f"- 目标人群：{task.audience}",
             f"- 场景：{task.scenario}", f"- 平台：{task.platform}", f"- 内容目标：{task.goal}",
             f"- 用户问题：{result.strategy.audience_problem}", f"- 内容角度：{result.strategy.angle}", "",
             "## 二、知识库依据", ""]
    lines += [f"- 实际来源库：{evidence.kb_name or '历史记录未提供'}",
              f"- 资料加载时间：{evidence.loaded_at or '历史记录未提供'}"]
    if task.campaign_id:
        lines += [f"- 领取任务编号：{task.campaign_id}", f"- 任务版本指纹：{task.campaign_signature}",
                  "- 总部任务仅预设创作意图，不能替代产品事实核验；演示任务以页面标注为准。"]
    if task.inspiration:
        from inspiration import creative_context
        if task.inspiration.source == "local":
            lines += ["", "### 本地选题记录", "", "来源：本地选题库，非实时热点；未作为产品事实来源。"]
        else:
            lines += ["", "### 联网选题记录", "", "仅用于选题，未作为产品事实来源。",
                      f"- 检索时间：{task.inspiration.fetched_at}"]
            if task.inspiration.provider:
                lines.append(f"- 搜索服务：{task.inspiration.provider}")
            lines += table(["检索参考", "链接"], [[ref.get("title", ""), ref.get("url", "")]
                                                   for ref in task.inspiration.references])
        lines.append(f"- 切入问题：{creative_context(task.inspiration)['question']}")
    if evidence.fact_card_mode:
        lines += ["", "### 已确认事实卡", ""]
        lines += table(["允许表述", "适用范围", "来源", "有效期至", "确认人"], [
            [c.claim, c.scope, c.source_title, str(c.valid_until or "长期"), c.confirmed_by]
            for c in evidence.approved_cards])
    if evidence.kb_name and evidence.kb_name != COMPETITION_KB_NAME:
        lines.append("- 来源说明：按用户选择使用 copy 库，与参赛手册指定库不同；比赛适用性待举办方确认。")
    for trace in evidence.retrieval:
        lines += [f"- 任务检索：{trace.query}",
                  f"  - 命中 {trace.hit_count} 项，排除 {trace.excluded_count} 项，目录回退：{'是' if trace.fallback_used else '否'}。",
                  f"  - 可采纳命中：{'；'.join(trace.accepted_titles) or '无，使用当前产品目录原文'}"]
    sources = {source.source_id: source for source in evidence.sources}
    for fact in result.facts:
        source = sources.get(fact.source_id)
        lines += [f"- [{fact.fact_id}] {fact.claim}", f"  - 资料：{source.title if source else '待确认'}",
                  f"  - 片段编号：{fact.segment_id or fact.source_id}；读取时间：{source.loaded_at if source else '未记录'}",
                  f"  - 原文：{fact.quote}"]
    if result.fact_plan:
        lines += ["", "### 写前事实支持检查", "", f"- 选用事实：{'、'.join(result.fact_plan.selected_fact_ids)}",
                  f"- 选材理由：{result.fact_plan.selection_reason}"]
        lines += table(["事实编号", "结论", "原因"], [
            [row.fact_id, row.status, row.reason] for row in result.fact_plan.checks])
    lines += ["", "## 三、图文软文", "", f"### {draft.title.text}", "", draft.intro.text, ""]
    images = {image.image_id: image for image in evidence.images}

    def insert_images(position: int):
        """在当前位置追加配图记录；0 为导语后，1 为正文第一段后。"""
        # 函数可使用外层的 lines/images/draft，这种写法称为闭包。
        for placement in draft.images:
            if placement.after_paragraph != position:
                continue
            image = images.get(placement.image_id)
            if image:
                lines.extend([f"> 配图：{image.name}", f"> 说明：{placement.caption}",
                              f"> 来源：{image.source_title}", f"> 插入理由：{placement.reason}", ""])
                page = public_source_url(image.source_page)
                if page:
                    # 只导出索引中的 HTTPS 来源页面；不导出下载时使用的临时签名链接。
                    lines.extend([f"> 公开来源页面：{page}", ""])

    insert_images(0)
    for index, block in enumerate(draft.body, 1):
        lines.extend([block.text, ""])
        insert_images(index)
    lines.extend([draft.cta.text, "", " ".join(draft.tags), "", "## 四、核验结果", "", f"- 状态：{status}",
                  f"- 取证重试次数：{result.extraction_retry_count}", f"- 自动修正次数：{result.repair_count}",
                  "- 图片检查依据：知识库目录、图片索引型号及用户明确确认；不代表完成自动视觉识别。", "",
                  "### 事实逐项核验", ""])
    lines += table(["位置", "原稿文字", "事实编号", "状态", "原因"], [
        [row.location, row.text, "、".join(row.fact_ids), row.status, row.reason] for row in result.review.fact_checks])
    lines += ["### 图片核验", ""]
    lines += table(["图片", "来源", "型号", "核对方式", "位置", "图注", "状态", "原因"], [
        [row.name, row.source_title, row.product_model, row.verification_method, row.location,
         row.caption, row.status, row.reason] for row in result.review.image_checks])
    lines += ["### 宣传风险核验", ""]
    lines += table(["类别", "位置", "原句", "状态", "原因", "修改建议"], [
        [row.category, row.location, row.text, row.status, row.reason, row.action]
        for row in result.review.compliance_checks])
    for issue in result.review.issues:
        # 即使检查未通过，也保留问题清单，让导出的草稿可供修改。
        lines.append(f"- {'需处理' if issue.severity == 'error' else '建议'}：{issue.message}")
    for blocking in result.review.blocking:
        lines.append(f"- 阻塞项，需处理：{blocking}")
    for notice in result.review.notices:
        lines.append(f"- 一般说明：{notice}")
    lines.append("\n> 核验是辅助检查，模型可能漏检；发布前需人工核对实际销售配置和有效信息。\n")
    return "\n".join(lines)

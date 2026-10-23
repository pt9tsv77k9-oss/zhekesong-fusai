"""数据说明书：约定各模块传递什么，模型应当返回什么格式。

可以把这里的类理解为不同用途的“表格模板”：Task 是任务表，Fact 是事实表，
Draft 是草稿表，Result 是最终档案。创建对象时，Pydantic 检查必填项、类型和范围。
例如 audience 不能是空白，after_paragraph 不能为负数；内容真实与否另由 checks 核验。
本文件主要定义格式，本身不会请求网络。
"""

from datetime import date
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

# Literal 限定字段只能选这三个字符串；随便填“短视频”会触发格式校验错误。
Platform = Literal["小红书", "公众号", "朋友圈"]
# 共享规则表：网页展示、流程限制、程序核验共同使用，避免各处数量规则不一致。
# min_images/max_images 是图片下限/上限；style 是表达要求；cta 是行动引导建议。
PLATFORM_RULES = {
    "小红书": {"min_images": 3, "max_images": 6, "style": "口语化标题、短段落、具体场景、话题标签，不伪造个人体验", "cta": "邀请评论或咨询选型问题"},
    "公众号": {"min_images": 2, "max_images": 4, "style": "标题、导语、分段分析、明确的小标题或逻辑层次", "cta": "文末给出选型咨询或到店体验引导"},
    "朋友圈": {"min_images": 1, "max_images": 3, "style": "短文案，只突出一个核心卖点，适合导购发布", "cta": "简洁的私信咨询或到店体验引导"},
}


class InspirationSelection(BaseModel):
    angle_id: str
    query: str
    fetched_at: str
    references: list[dict[str, str]] = Field(default_factory=list, max_length=6)
    source: Literal["web", "local"] = "web"
    provider: str = ""


class ProductFactCard(BaseModel):
    card_id: str
    product_model: str
    category: Literal["产品参数", "软件兼容", "价格", "赠品", "门店服务"]
    claim: str = Field(min_length=4, max_length=500)
    quote: str = Field(min_length=4, max_length=3000)
    source_title: str
    source_hash: str
    scope: str = Field(min_length=2, max_length=300)
    valid_from: date
    valid_until: date | None = None
    confirmed_by: str = Field(min_length=1, max_length=60)
    confirmed_at: str
    status: Literal["已确认", "已停用"] = "已确认"


# 1. 用户任务：网页收集输入后创建，贯穿整个生成流程。
class Task(BaseModel):
    # 创建任务时先去掉字符串两端空白，所以只输入空格也无法满足必填要求。
    model_config = ConfigDict(str_strip_whitespace=True)
    product_model: str = Field(min_length=2)
    audience: str = Field(min_length=1)
    scenario: str = Field(min_length=1)
    platform: Platform
    # 没有 Field 限制的默认值用于减少填写负担；revision 是对上一稿的修改要求。
    goal: str = "引导咨询或到店体验"
    buying_stage: str = "了解与比较"
    requirements: str = ""
    revision: str = ""
    writing_brief: str = ""
    inspiration: InspirationSelection | None = None
    fact_card_signature: str = ""
    campaign_id: str = ""
    campaign_signature: str = ""


# 2. 一份原文：source_id 是程序内部短编号 S1、S2，便于模型引用；text 是正文。
class Source(BaseModel):
    source_id: str
    title: str
    text: str
    # media_id 是 ima 的真实条目标识；exclude=True 使 model_dump 时排除它，
    # repr=False 使打印对象时不显示它。它仍保留在内存里供程序使用。
    media_id: str = Field(default="", exclude=True, repr=False)
    # 文本指纹检查的是“这一份已读取文字有没有变化”，不是资料真实性认证。
    text_hash: str = ""
    loaded_at: str = ""


# 3. 一张实际读到的图片：image_id 为 I1、I2；name 是文件名。
class ImageAsset(BaseModel):
    image_id: str
    name: str
    product_model: str
    source_title: str
    # description 来自图片索引的说明；source_page 是索引提供的来源页面。
    description: str = ""
    source_page: str = ""
    verified: bool = False
    # 核对方式与时间随图片保存，不能把格式验证描述成视觉型号识别。
    verification_method: str = ""
    verified_at: str = ""
    # verified 表示索引型号匹配或用户已人工确认，不代表程序识别了画面中的硬件。
    # data 是下载后的二进制字节，供网页显示；不放入模型请求或 JSON 导出。
    data: bytes = Field(default=b"", exclude=True, repr=False)


class RetrievalTrace(BaseModel):
    """一条任务检索记录；只展示名称和数量，不暴露 ima 的内部媒体标识。"""
    query: str
    hit_count: int = 0
    accepted_titles: list[str] = Field(default_factory=list)
    excluded_count: int = 0
    fallback_used: bool = False


# 4. 当前产品的全部已读依据：原文列表 + 图片列表 + 加载时需要说明的问题。
class Evidence(BaseModel):
    product_model: str
    folder_title: str
    sources: list[Source]
    # list[Source] 表示“由 Source 对象组成的列表”。
    # default_factory=list 会为每个新对象创建自己的空列表，避免不同对象共用一份。
    images: list[ImageAsset] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    folder_id: str = Field(default="", exclude=True, repr=False)
    # 散放的单型号文档只允许从这一份取证，不继承同目录的其他型号。
    document_id: str = Field(default="", exclude=True, repr=False)
    kb_name: str = ""
    loaded_at: str = ""
    retrieval: list[RetrievalTrace] = Field(default_factory=list)
    fact_card_mode: bool = False
    approved_cards: list[ProductFactCard] = Field(default_factory=list)


# 5. 第一次模型调用的输出：先决定怎么讲，再依据资料决定能讲哪些产品事实。
class Strategy(BaseModel):
    # audience_problem：用户的问题；angle：切入角度；priority_points：卖点方向。
    audience_problem: str
    angle: str
    priority_points: list[str]
    structure: list[str]
    # tone：语气；image_count：建议配图数；call_to_action：希望读者下一步做什么。
    tone: str
    image_count: int = Field(ge=1, le=6)
    call_to_action: str


# 6. 一条有证据的产品事实：claim 为结论，quote 为原文，引句指向 source_id。
# 示例：F1 → S1 中的一句原文。程序会验证引句存在，模型还要核验引句是否支持结论。
class Fact(BaseModel):
    fact_id: str = ""  # 提取结束后由程序重新编号，模型不能自行创建证据。
    claim: str
    source_id: str
    quote: str = Field(min_length=4, description="直接复制原文的连续片段，保留限定条件")
    segment_id: str = ""
    # 原文切片坐标和指纹供本地复核，不需要发给编辑模型。
    source_hash: str = Field(default="", exclude=True, repr=False)
    quote_start: int = Field(default=0, exclude=True)
    quote_end: int = Field(default=0, exclude=True)


class EvidenceSegment(BaseModel):
    """程序从原文切出的连续段落；start/end 是 Python 字符下标，终点不包含。"""
    segment_id: str
    source_id: str
    start: int
    end: int
    text: str
    source_hash: str


class FactProposal(BaseModel):
    """模型只提出结论并选择片段，真正的 quote 由程序复制。"""
    claim: str = Field(min_length=1)
    source_id: str
    segment_id: str


class SegmentExtraction(BaseModel):
    """真实模型的新提取格式；保留旧 FactExtraction 供已有调用者使用。"""
    product_model: str
    facts: list[FactProposal] = Field(max_length=8)
    conflicts: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)


# 7. 事实提取阶段的整体返回值。conflicts 有内容时停止；pending 记录待确认信息。
class FactExtraction(BaseModel):
    product_model: str
    facts: list[Fact]
    conflicts: list[str] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    blocking: list[str] = Field(default_factory=list)


class FactSupport(BaseModel):
    fact_id: str
    status: Literal["支持", "不支持", "待确认"]
    reason: str = Field(min_length=1)


class FactPlan(BaseModel):
    """写前审查与选材；程序要求逐条覆盖，不能用空审查放行。"""
    checks: list[FactSupport]
    selected_fact_ids: list[str]
    angle: str = Field(min_length=1)
    selection_reason: str = Field(min_length=1)


# 8. 一块文案：正文不是一个大字符串，而是多个 Block，方便逐段检查来源和放图片。
# fact_ids 可以为空（例如通用咨询引导）；有产品结论的段落应引用相应 F 编号。
class Block(BaseModel):
    text: str = Field(min_length=1)
    fact_ids: list[str] = Field(default_factory=list)


# 9. 配图安排：只保存已知图片编号与位置，不让模型编造图片 URL。
class ImagePlacement(BaseModel):
    image_id: str
    after_paragraph: int = Field(ge=0, description="0=导语后；1=正文第一段后，以此类推")
    caption: str
    # caption 是给读者看的图注；reason 解释为什么放在此处，便于人工审查。
    reason: str


# 10. 一篇草稿：title/intro/body/cta 分别是标题、导语、正文和行动引导。
# body 允许 1～12 个段落；tags 是话题标签；images 是配图安排，不是图片字节。
class Draft(BaseModel):
    product_model: str
    title: Block
    intro: Block
    body: list[Block] = Field(min_length=1, max_length=12)
    cta: Block
    tags: list[str] = Field(default_factory=list)
    images: list[ImagePlacement] = Field(default_factory=list)


# 11. 一个检查问题。error 会阻止“检查通过”，warning 是建议；code 便于测试识别。
class Issue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str


# 审核状态采用固定文字，防止模型使用“基本通过”等无法计算的模糊状态。
AuditStatus = Literal["通过", "需处理", "待确认", "不涉及"]
COMPLIANCE_CATEGORIES = ["绝对化宣传", "虚假承诺", "价格误导", "无依据比较", "竞品贬低"]


class FactAudit(BaseModel):
    """按草稿位置检查语义；text 最终由程序回填，防止位置与内容错配。"""
    location: str
    status: AuditStatus
    reason: str
    fact_ids: list[str] = Field(default_factory=list)
    text: str = ""


class ComplianceAudit(BaseModel):
    category: str
    status: AuditStatus
    reason: str
    location: str = "全稿"
    text: str = ""
    action: str = ""


class ImageAudit(BaseModel):
    image_id: str
    name: str
    source_title: str
    product_model: str
    verification_method: str
    location: str
    caption: str
    status: AuditStatus
    reason: str


# 12. 模型的语义审核结果。needs_confirmation=True 表示必须人工处理，不自动放行。
class ModelReview(BaseModel):
    issues: list[Issue] = Field(default_factory=list)
    pending: list[str] = Field(default_factory=list)
    needs_confirmation: bool = False
    blocking: list[str] = Field(default_factory=list)
    fact_checks: list[FactAudit] = Field(default_factory=list)
    compliance_checks: list[ComplianceAudit] = Field(default_factory=list)


# 13. 程序与模型检查合并后的结果；passed 由 workflow.py 计算，不让模型自行决定。
class Review(BaseModel):
    passed: bool
    issues: list[Issue]
    pending: list[str]
    needs_confirmation: bool = False
    blocking: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    fact_checks: list[FactAudit] = Field(default_factory=list)
    image_checks: list[ImageAudit] = Field(default_factory=list)
    compliance_checks: list[ComplianceAudit] = Field(default_factory=list)


# 14. 一次生成的完整档案。网页展示、Markdown 导出、保存 ima 营销笔记都使用它。
class Result(BaseModel):
    task: Task
    strategy: Strategy
    facts: list[Fact]
    evidence: Evidence
    draft: Draft
    review: Review
    fact_plan: FactPlan | None = None
    repair_count: int = 0
    extraction_retry_count: int = 0
    # generated_at 是带时区的生成时间文本；repair_count 是自动改写次数（最多一次）。
    generated_at: str


def normalize_model(value: str) -> str:
    """忽略空格和中英文括号形式，但保留年份、系统和具体型号差异。"""
    import re
    import unicodedata
    # NFKC 把部分全角字符转成对应形式；\s+ 匹配连续空白；casefold 忽略英文大小写。
    # “鸿蒙版”和“Windows版”、不同年份不会被删掉，因此仍视为不同型号。
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()

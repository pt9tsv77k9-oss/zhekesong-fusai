"""自定义封面标题：长度检查、语义核验与已审事实绑定。"""

from hashlib import sha256
import re
from typing import Literal
import unicodedata

from pydantic import BaseModel, Field

from checks import EvidenceError, RISKY_EXPRESSIONS
from exporter import require_publishable
from schemas import normalize_model
from product_types import is_phone


HEADLINES = ["选电脑，先聊需求", "带着清单看电脑", "下一台电脑怎么选"]
PHONE_HEADLINES = ["换手机，先聊需求", "带着清单选手机", "下一部手机怎么选"]
MAX_HEADLINE_LENGTH = 30


def headlines_for(product_model):
    return PHONE_HEADLINES if is_phone(product_model) else HEADLINES


class HeadlineReview(BaseModel):
    headline: str
    product_model: str
    kind: Literal["通用选题", "依据事实", "需修改"]
    compliance_status: Literal["通过", "需处理", "待确认"]
    fact_ids: list[str] = Field(max_length=8)
    reason: str = Field(min_length=1, max_length=500)


def clean_headline(headline):
    if any(unicodedata.category(char).startswith("C") for char in headline):
        raise EvidenceError("封面标题请使用单行文字，不含换行或隐藏字符。")
    headline = headline.strip()
    if not headline:
        raise EvidenceError("请输入封面标题。")
    if len(headline) > MAX_HEADLINE_LENGTH:
        raise EvidenceError(f"封面标题最多{MAX_HEADLINE_LENGTH}字，请缩短后再制作。")
    return headline


def result_signature(result):
    return sha256(result.model_dump_json().encode("utf-8")).hexdigest()


def check_quantities(headline, product_model, facts):
    def quantities(text):
        text = unicodedata.normalize("NFKC", text).casefold()
        return set(re.findall(r"(\d+(?:\.\d+)?)\s*(万元|元|千克|公斤|克|英寸|小时|分钟|赫兹|kg|ghz|mhz|gb|tb|wh|hz|g|w|%)", text))

    supported = product_model + " " + " ".join(f.quote for f in facts)
    if quantities(headline) - quantities(supported):
        raise EvidenceError("封面标题中的参数或金额没有已审核依据，请修改标题或先生成并审核文案。")


def review_headline(headline, task, result=None, engine=None):
    headline = clean_headline(headline)
    normalized = unicodedata.normalize("NFKC", headline)
    for pattern in RISKY_EXPRESSIONS:
        match = re.search(pattern, re.sub(r"\s+", "", normalized))
        if match:
            raise EvidenceError(f"封面标题包含需删除或核实的宣传表达：{match.group()}。")
    if headline in headlines_for(task.product_model):
        return HeadlineReview(headline=headline, product_model=task.product_model, kind="通用选题",
                              compliance_status="通过", fact_ids=[], reason="预设中性选题标题。")

    facts = []
    if result is not None:
        require_publishable(result, task)
        facts = [f for f in result.facts if f.fact_id in result.fact_plan.selected_fact_ids]
    check_quantities(headline, task.product_model, facts)
    if engine is None:
        raise EvidenceError("自定义标题需要完成核验，请连接模型后重试，或选用推荐标题。")
    review = HeadlineReview.model_validate(engine.run("cover_title", HeadlineReview, {
        "headline": headline, "product_model": task.product_model,
        "platform": task.platform, "approved_facts": [f.model_dump() for f in facts],
    }))
    if review.headline != headline or normalize_model(review.product_model) != normalize_model(task.product_model):
        raise EvidenceError("标题核验内容与当前标题或型号不一致，请重试。")
    if review.kind == "需修改" or review.compliance_status != "通过":
        raise EvidenceError("封面标题需修改：" + review.reason)
    if review.kind == "通用选题" and review.fact_ids:
        raise EvidenceError("标题核验的分类与事实引用不一致，请重试。")
    allowed = {f.fact_id for f in facts}
    if (len(review.fact_ids) != len(set(review.fact_ids)) or set(review.fact_ids) - allowed
            or (review.kind == "依据事实" and not review.fact_ids)):
        raise EvidenceError("封面标题的产品表述缺少本次已审核事实，请修改标题或先生成并审核文案。")
    check_quantities(headline, task.product_model, [f for f in facts if f.fact_id in review.fact_ids])
    return review


def require_current_cover(cover, task, result=None):
    if cover.manifest["task_signature"] != sha256(task.model_dump_json().encode("utf-8")).hexdigest():
        raise EvidenceError("任务已修改，请重新制作封面。")
    signature = cover.manifest["reviewed_result_signature"]
    if signature:
        if result is None:
            raise EvidenceError("封面的事实依据已失效，请重新制作。")
        require_publishable(result, task)
        if signature != result_signature(result):
            raise EvidenceError("封面的事实依据已更新，请重新制作。")

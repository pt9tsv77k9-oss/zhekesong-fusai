"""可选的需求理解：最多两轮选择题，所有输入仍只代表写作意图。"""

from typing import Literal

from pydantic import BaseModel, Field

from checks import task_questions
from playbooks import PLATFORM_BRIEFS
from schemas import Task
from product_types import is_phone, product_label


class IntakePlan(BaseModel):
    audience: str = Field(default="", max_length=150)
    scenario: str = Field(default="", max_length=300)
    question_field: Literal["", "audience", "scenario"] = ""
    question: str = Field(default="", max_length=120)
    choices: list[str] = Field(default_factory=list, max_length=4)


class IntakeState(BaseModel):
    request: str
    product_model: str
    platform: str
    answers: list[dict[str, str]] = Field(default_factory=list)
    plan: IntakePlan
    ready: bool = False
    used_defaults: bool = False


DEFAULTS = {"audience": "日常办公用户", "scenario": "选购电脑，核对文档处理和会议的实际需求"}
CHOICES = {
    "audience": ["大学新生", "差旅办公人员", "日常办公用户", "手写创作人群"],
    "scenario": ["上课携带，整理课程资料", "出差携带，准备会议材料", "处理文档和线上会议", "核对草图、批注的软件与笔型需求"],
}
PHONE_DEFAULTS = {"audience": "日常换机用户", "scenario": "选购手机，核对常用应用、握持和存储需求"}
PHONE_CHOICES = {
    "audience": ["日常换机用户", "旅行拍摄人群", "通勤办公人员", "为家人选手机"],
    "scenario": ["换机前核对常用应用与资料迁移需求", "旅行拍照，核对镜头规格", "通勤途中处理消息与办公应用", "到店核对屏幕、重量和握持偏好"],
}


def normalize_plan(plan: IntakePlan, answers: list[dict[str, str]], use_defaults=False, product_model=""):
    plan = plan.model_copy(deep=True)
    defaults = PHONE_DEFAULTS if is_phone(product_model) else DEFAULTS
    choices = PHONE_CHOICES if is_phone(product_model) else CHOICES
    for answer in answers:
        if answer["field"] in DEFAULTS:
            setattr(plan, answer["field"], answer["answer"])
    probe = Task(product_model="需求核对", audience=plan.audience or "所有人",
                 scenario=plan.scenario or "不知道", platform="朋友圈")
    questions = task_questions(probe)
    if any("目标人群" in q for q in questions):
        plan.audience = ""
    if any("使用场景" in q for q in questions):
        plan.scenario = ""
    missing = [field for field in DEFAULTS if not getattr(plan, field).strip()]
    used_defaults = bool(missing and (use_defaults or len(answers) >= 2))
    if used_defaults:
        for field in missing:
                setattr(plan, field, defaults[field])
        missing = []
    if not missing:
        plan.question_field, plan.question, plan.choices = "", "", []
    else:
        field = plan.question_field if plan.question_field in missing else missing[0]
        # 只允许询问缺失的必要信息；不采用模型提出的预算、联系方式等问题。
        proposed = plan.choices if plan.question_field == field else []
        plan.question_field = field
        plan.question = "这条内容主要发给谁？" if field == "audience" else "主要想聊哪种选购场景？"
        plan.choices = list(dict.fromkeys(c.strip()[:150] for c in proposed if c.strip()))
        if len(plan.choices) < 2:
            plan.choices = choices[field]
    return plan, not missing, used_defaults


def understand_request(request, product_model, platform, engine, previous=None, answer=None):
    request = request.strip()
    if not request or len(request) > 1000:
        raise ValueError("请输入一句需求，长度不超过1000字。")
    answers = []
    if previous is not None:
        if (previous.request, previous.product_model, previous.platform) != (request, product_model, platform):
            raise ValueError("需求或产品已变化，请重新理解需求。")
        if previous.ready or len(previous.answers) >= 2:
            return previous
        if answer not in previous.plan.choices:
            raise ValueError("请选择当前问题中的一个答案。")
        answers = previous.answers + [{"field": previous.plan.question_field, "answer": answer}]
    plan = engine.run("intake", IntakePlan, {
        "request": request, "platform": platform, "answers": answers,
        "product_model": product_model, "product_category": product_label(product_model),
        "previous_understanding": previous.plan.model_dump() if previous else None,
        "remaining_questions": max(0, 2 - len(answers)),
    })
    if previous:
        for field in DEFAULTS:
            if not getattr(plan, field).strip() and getattr(previous.plan, field).strip():
                setattr(plan, field, getattr(previous.plan, field))
    plan, ready, defaults = normalize_plan(plan, answers, product_model=product_model)
    return IntakeState(request=request, product_model=product_model, platform=platform,
                       answers=answers, plan=plan, ready=ready, used_defaults=defaults)


def use_default_intake(request, product_model, platform, previous=None):
    plan, ready, defaults = normalize_plan(previous.plan if previous else IntakePlan(),
                                          previous.answers if previous else [], use_defaults=True,
                                          product_model=product_model)
    return IntakeState(request=request.strip(), product_model=product_model, platform=platform,
                       answers=previous.answers if previous else [], plan=plan, ready=ready, used_defaults=defaults)


def intake_task(state: IntakeState) -> Task:
    if not state.ready:
        raise ValueError("请先完成当前问题或采用默认方案。")
    return Task(product_model=state.product_model, audience=state.plan.audience,
                scenario=state.plan.scenario, platform=state.platform, requirements=state.request,
                writing_brief=PLATFORM_BRIEFS[state.platform] + "\n需求与补问仅用于选择人群、场景和表达，不能作为产品能力、价格或服务的证据。")

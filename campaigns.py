"""本机总部任务配置：只预设创作意图，不提供产品事实。"""

from datetime import date
from hashlib import sha256
from typing import Literal
import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from checks import EvidenceError
from config import ROOT
from fact_cards import local_today
from playbooks import PLATFORM_BRIEFS
from schemas import Platform, Task, normalize_model


CAMPAIGN_FILE = ROOT / "data" / "campaigns.json"


class Campaign(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    campaign_id: str = Field(min_length=1, max_length=60)
    title: str = Field(min_length=2, max_length=120)
    product_model: str = Field(min_length=2, max_length=120)
    audience: str = Field(min_length=2, max_length=150)
    scenario: str = Field(min_length=2, max_length=300)
    platform: Platform
    valid_from: date
    valid_until: date
    confirmed_by: str = Field(min_length=2, max_length=60)
    status: Literal["启用", "停用"] = "启用"
    demo: bool = False

    @model_validator(mode="after")
    def valid_dates(self):
        if self.valid_until < self.valid_from:
            raise ValueError("任务结束日期不能早于开始日期。")
        return self


def read_campaigns() -> list[Campaign]:
    if not CAMPAIGN_FILE.exists():
        return []
    try:
        payload = json.loads(CAMPAIGN_FILE.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, list) or len(payload) > 200:
            raise ValueError()
        campaigns = [Campaign.model_validate(row) for row in payload]
        if len({c.campaign_id for c in campaigns}) != len(campaigns):
            raise ValueError()
        return campaigns
    except (ValueError, ValidationError, OSError) as exc:
        raise EvidenceError("总部任务配置读取失败，请核对data/campaigns.json。") from exc


def available_campaigns(product_model: str, today=None) -> list[Campaign]:
    today = today or local_today()
    return [c for c in read_campaigns() if c.status == "启用" and c.valid_from <= today <= c.valid_until
            and normalize_model(c.product_model) == normalize_model(product_model)]


def campaign_signature(campaign: Campaign) -> str:
    return sha256(campaign.model_dump_json().encode("utf-8")).hexdigest()


def campaign_task(campaign: Campaign) -> Task:
    return Task(product_model=campaign.product_model, audience=campaign.audience,
                scenario=campaign.scenario, platform=campaign.platform,
                campaign_id=campaign.campaign_id, campaign_signature=campaign_signature(campaign),
                writing_brief=PLATFORM_BRIEFS[campaign.platform] +
                "\n总部任务仅提供目标人群和选题，不是产品能力、价格、活动或服务承诺的依据。")


def require_current_campaign(task: Task):
    if not task.campaign_id:
        if task.campaign_signature:
            raise EvidenceError("总部任务编号缺失，请重新领取。")
        return
    campaign = next((c for c in available_campaigns(task.product_model)
                     if c.campaign_id == task.campaign_id), None)
    if campaign is None or campaign_signature(campaign) != task.campaign_signature:
        raise EvidenceError("总部任务已变更、停用或过期，请重新领取。")
    expected = campaign_task(campaign)
    if any(getattr(task, field) != getattr(expected, field)
           for field in ("product_model", "audience", "scenario", "platform", "writing_brief")):
        raise EvidenceError("总部任务内容已修改，请重新领取或改用自主创作。")

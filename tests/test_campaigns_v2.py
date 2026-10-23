"""总部任务领取、时效变更与导出门禁，不发起外部请求。"""

from datetime import timedelta
import json

import pytest

from campaigns import Campaign, available_campaigns, campaign_task, require_current_campaign
from checks import EvidenceError
from exporter import require_publishable
from fact_cards import local_today
from workflow import run_workflow
from test_app import configured_app, load_first_product, button
from test_core import evidence, extraction, FakeEngine


@pytest.fixture
def campaign_setup(monkeypatch, local_tmp_path):
    path = local_tmp_path / "campaigns.json"
    monkeypatch.setattr("campaigns.CAMPAIGN_FILE", path)
    campaign = Campaign(campaign_id="fixture-campaign", title="测试任务",
                        product_model="测试型号A", audience="大学新生", scenario="上课携带，整理课程资料",
                        platform="小红书", valid_from=local_today() - timedelta(days=1),
                        valid_until=local_today() + timedelta(days=1), confirmed_by="测试配置", demo=True)
    def save(value):
        path.write_text(json.dumps([value.model_dump(mode="json")], ensure_ascii=False), encoding="utf-8")
    save(campaign)
    return campaign, save


def test_campaign_is_bound_to_model_dates_and_current_configuration(campaign_setup):
    campaign, save = campaign_setup
    assert available_campaigns(campaign.product_model) == [campaign]
    assert not available_campaigns("另一个型号")
    assert not available_campaigns(campaign.product_model, campaign.valid_until + timedelta(days=1))
    task = campaign_task(campaign)
    require_current_campaign(task)
    save(campaign.model_copy(update={"audience": "差旅办公人员"}))
    with pytest.raises(EvidenceError, match="变更"):
        require_current_campaign(task)


def test_claimed_campaign_does_not_authorize_modified_target(campaign_setup):
    campaign, _ = campaign_setup
    task = campaign_task(campaign).model_copy(update={"platform": "朋友圈"})
    with pytest.raises(EvidenceError, match="内容已修改"):
        require_current_campaign(task)


@pytest.mark.parametrize("change", ["disable", "expire", "edit"])
def test_old_campaign_output_is_blocked_at_export(campaign_setup, evidence, extraction, change):
    campaign, save = campaign_setup
    campaign = campaign.model_copy(update={"product_model": evidence.product_model})
    save(campaign)
    result = run_workflow(campaign_task(campaign), evidence, FakeEngine(evidence, extraction))
    assert result.review.passed
    require_publishable(result)
    if change == "disable":
        changed = campaign.model_copy(update={"status": "停用"})
    elif change == "expire":
        changed = campaign.model_copy(update={"valid_until": local_today() - timedelta(days=1)})
    else:
        changed = campaign.model_copy(update={"scenario": "另一个任务场景"})
    save(changed)
    with pytest.raises(EvidenceError, match="变更|过期"):
        require_publishable(result)


def test_campaign_ui_claims_without_questionnaire_and_revokes_old_result(campaign_setup, configured_app):
    campaign, save = campaign_setup
    app = configured_app
    load_first_product(app)
    next(r for r in app.radio if r.label == "工作模式").set_value("门店简易模式").run()
    next(r for r in app.radio if r.label == "创作方式").set_value("领取总部任务").run()
    assert button(app, "生成本次图文").disabled
    assert not any(i.label == "一句话说说你想发什么" for i in app.text_input)
    button(app, "领取这项任务").click().run()
    assert not button(app, "生成本次图文").disabled
    button(app, "生成本次图文").click().run()
    assert app.session_state["result"].task.campaign_id == campaign.campaign_id
    assert app.session_state["result"].review.passed and not app.exception
    save(campaign.model_copy(update={"status": "停用"}))
    app.run()
    assert button(app, "生成本次图文").disabled and not app.exception
    assert not any(d.proto.label == "领取文案与图片包" for d in app.get("download_button"))

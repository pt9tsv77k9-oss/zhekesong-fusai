"""需求辅助、联网选题、事实卡与原图封面的可选界面。"""

from datetime import timedelta
from hashlib import sha256
import json

import streamlit as st
from pydantic import ValidationError

from checks import EvidenceError
from covers import HEADLINES, PALETTES, decode_image, make_cover
from cover_titles import MAX_HEADLINE_LENGTH, clean_headline, result_signature, require_current_cover
from cover_titles import headlines_for
from product_types import is_phone, product_label
from exporter import require_publishable
from fact_cards import (check_card, create_card, disable_card, list_cards, local_today,
                        cards_signature)
from inspiration import (TOPICS, SearchError, search_topics, recommend_topics, select_inspiration,
                         local_topic_ids, select_local_inspiration)
from intake import understand_request, use_default_intake, intake_task
from llm import LangChainEngine, ModelError
from schemas import ImageAsset, normalize_model
from campaigns import available_campaigns, campaign_task, campaign_signature
from evidence_utils import timestamp


def product_key(evidence):
    return sha256(evidence.product_model.encode()).hexdigest()[:12]


def render_product_photos(evidence):
    if not is_phone(evidence.product_model):
        return evidence
    with st.expander("产品图片", expanded=True):
        key = product_key(evidence)
        originals = [a for a in evidence.images if a.verified
                     and normalize_model(a.product_model) == normalize_model(evidence.product_model)]
        source_mode = st.radio("图片来源", ["产品资料原图", "上传门店实拍"], horizontal=True,
                               index=0 if originals else 1, key="phone_image_source_" + key)
        assets, seen = [], set()
        if source_mode == "产品资料原图":
            if originals:
                selected = st.multiselect("选择原图", [a.image_id for a in originals],
                                          default=[a.image_id for a in originals[:6]], max_selections=6,
                                          format_func=lambda value: next(a.name for a in originals if a.image_id == value),
                                          key="phone_originals_" + key)
                for asset in originals:
                    digest = sha256(asset.data).hexdigest()
                    if asset.image_id in selected and digest not in seen:
                        seen.add(digest)
                        assets.append(asset)
            else:
                st.info("当前型号暂无已核对的产品原图，请选择上传门店实拍。")
        else:
            uploads = st.file_uploader("上传当前手机型号的实拍图片", type=["png", "jpg", "jpeg", "webp"],
                                       accept_multiple_files=True, key="phone_photos_" + key)
            if len(uploads) > 6:
                st.warning("一次最多使用6张实拍，请减少文件后继续。")
            else:
                for upload in uploads:
                    data = upload.getvalue()
                    digest = sha256(data).hexdigest()
                    if digest in seen:
                        continue
                    seen.add(digest)
                    try:
                        decode_image(data)
                        assets.append(ImageAsset(image_id="PHOTO_" + digest[:12], name=upload.name,
                                                 product_model=evidence.product_model,
                                                 source_title="用户上传门店实拍", data=data,
                                                 verification_method="用户确认型号及使用权限"))
                    except EvidenceError as exc:
                        st.warning(upload.name + "：" + str(exc))
        confirmed = False
        fingerprints = tuple((a.image_id, sha256(a.data).hexdigest()) for a in assets)
        if assets:
            st.image([a.data for a in assets], caption=[a.name for a in assets], width=160)
            digest = sha256(repr((source_mode, fingerprints)).encode()).hexdigest()[:16]
            confirmed = st.checkbox("我确认所选图片为当前手机型号，并有权用于本次发布",
                                     key=f"phone_photo_rights_{key}_{digest}")
        anchor = (key, source_mode, fingerprints, confirmed)
        if st.session_state.get("phone_photo_anchor") != anchor:
            st.session_state.pop("result", None)
            st.session_state.pop("created_cover", None)
            st.session_state.phone_photo_anchor = anchor
        images = [a.model_copy(update={"verified": True, "verified_at": a.verified_at or timestamp()})
                  for a in assets] if confirmed else []
    return evidence.model_copy(update={"images": images})


def render_campaign(evidence):
    try:
        campaigns = available_campaigns(evidence.product_model)
    except EvidenceError as exc:
        st.warning(str(exc))
        return None
    if not campaigns:
        st.info("当前型号暂无有效总部任务。")
        return None
    mapping = {c.campaign_id: c for c in campaigns}
    selected_id = st.selectbox("本次领取的任务", list(mapping),
                               format_func=lambda value: ("演示任务 · " if mapping[value].demo else "") + mapping[value].title,
                               key="campaign_choice_" + product_key(evidence))
    campaign = mapping[selected_id]
    st.write(f"{campaign.audience} · {campaign.platform} · 有效期至 {campaign.valid_until}")
    if campaign.demo:
        st.caption("演示任务，尚未获得总部批准。")
    else:
        st.caption(f"任务确认人：{campaign.confirmed_by}")
    anchor = campaign_signature(campaign)
    if st.button("领取这项任务", icon=":material/assignment_turned_in:"):
        st.session_state.claimed_campaign = anchor
    if st.session_state.get("claimed_campaign") == anchor:
        st.success("任务已领取")
        return campaign_task(campaign)
    return None


def render_intake(evidence, settings):
    platform = st.selectbox("发布平台 *", ["小红书", "公众号", "朋友圈"], key="intake_platform")
    request = st.text_input("一句话说说你想发什么", placeholder="例如：给刚入学的大学生发一条", max_chars=1000,
                            key="intake_request")
    state = st.session_state.get("intake_state")
    if state and (state.request, state.product_model, state.platform) != (request.strip(), evidence.product_model, platform):
        st.session_state.pop("intake_state", None)
        state = None
    understand, defaults = st.columns(2)
    if understand.button("理解我的需求", icon=":material/chat:", disabled=not request.strip() or not settings.api_key):
        st.session_state.pop("intake_state", None)
        try:
            with st.spinner("正在理解需求…"):
                state = understand_request(request, evidence.product_model, platform, LangChainEngine(settings))
            st.session_state.intake_state = state
        except (ValueError, ModelError) as exc:
            st.error(str(exc))
            state = None
    if defaults.button("按默认方案继续", disabled=not request.strip(), icon=":material/arrow_forward:"):
        state = use_default_intake(request, evidence.product_model, platform, state)
        st.session_state.intake_state = state
    if state and not state.ready:
        round_number = len(state.answers) + 1
        answer = st.radio(state.plan.question, state.plan.choices,
                          key=f"intake_answer_{product_key(evidence)}_{round_number}_{sha256(request.encode()).hexdigest()[:8]}")
        if st.button("确认这个选择", icon=":material/check:", disabled=not settings.api_key):
            try:
                with st.spinner("正在补全需求…"):
                    state = understand_request(request, evidence.product_model, platform,
                                               LangChainEngine(settings), state, answer)
                st.session_state.intake_state = state
                st.rerun()
            except (ValueError, ModelError) as exc:
                st.error(str(exc))
    if state and state.ready:
        st.write(f"目标人群：{state.plan.audience}")
        st.write(f"本次场景：{state.plan.scenario}")
        if state.used_defaults:
            st.caption("未明确的项目采用日常换机方案。" if is_phone(evidence.product_model) else "未明确的项目采用日常办公方案。")
        return intake_task(state)
    return None


def render_inspiration(task, settings):
    if task is None:
        return task
    with st.expander("找选题灵感（可选）"):
        enabled = st.checkbox("参考联网选题", key="use_web_inspiration")
        if not enabled:
            return task
        query = st.text_input("搜索关键词", value=f"{task.audience} {product_label(task.product_model)}选购 常见问题", max_chars=120,
                              key="web_query_" + sha256((task.product_model + task.audience).encode()).hexdigest()[:10])
        # 绑定人群和场景，防止改了任务后继续沿用另一人群的搜索结果。
        anchor = (task.product_model, task.audience, task.scenario, query.strip())
        bundle = st.session_state.get("web_bundle")
        if bundle and bundle["anchor"] != anchor:
            bundle = None
            st.session_state.pop("web_bundle", None)
        search, local = st.columns(2)
        if search.button("搜索选题", icon=":material/search:", disabled=not query.strip()):
            st.session_state.pop("web_bundle", None)
            bundle = None
            try:
                with st.spinner("正在查找公开选题…"):
                    batch = search_topics(query, settings=settings)
                    ids = local_topic_ids(task.audience, task.scenario, task.product_model)
                    notice = ""
                    if settings.api_key:
                        try:
                            ids = recommend_topics(batch, task.audience, LangChainEngine(settings), task.product_model)
                        except (SearchError, ModelError):
                            notice = "已找到相关网页，选题推荐暂不可用，本次使用预设问题方向。"
                bundle = {"anchor": anchor, "batch": batch, "ids": ids, "notice": notice}
            except SearchError as exc:
                bundle = {"anchor": anchor, "batch": None,
                          "ids": local_topic_ids(task.audience, task.scenario, task.product_model),
                          "notice": str(exc), "attempts": exc.attempts}
            st.session_state.web_bundle = bundle
        if local.button("使用本地选题", icon=":material/list_alt:"):
            bundle = {"anchor": anchor, "batch": None,
                      "ids": local_topic_ids(task.audience, task.scenario, task.product_model), "notice": ""}
            st.session_state.web_bundle = bundle
        if bundle:
            batch = bundle["batch"]
            if bundle.get("notice"):
                st.warning(bundle["notice"])
            if batch:
                provider = f" · {batch.provider}" if batch.provider else ""
                st.caption(f"公开网页{provider} · 检索时间：{batch.fetched_at}。产品信息仍以已核对资料为准。")
                for hit in batch.hits:
                    st.link_button(hit.title, hit.url, icon=":material/open_in_new:")
            else:
                st.caption("来源：本地选题库 · 非实时热点")
            if bundle.get("attempts"):
                with st.expander("查看搜索状态"):
                    labels = {"irrelevant_results": "结果与关键词不相关，已过滤", "no_results": "未返回结果",
                              "timeout": "响应超时", "unavailable": "连接失败", "invalid_response": "返回格式异常",
                              "access_denied": "访问受限或需要验证", "rate_limited": "额度不足或请求受限",
                              "not_configured": "尚未配置"}
                    for attempt in bundle["attempts"]:
                        st.write(f"{attempt.provider}：{labels.get(attempt.status, '暂不可用')}")
            angle = st.selectbox("采用哪个问题切入", ["不采用", *bundle["ids"]],
                                 format_func=lambda key: TOPICS[key][0] if key in TOPICS else key,
                                 key="web_angle_" + sha256(repr((anchor, batch)).encode()).hexdigest()[:10])
            if angle in TOPICS:
                st.write(TOPICS[angle][1])
                selection = select_inspiration(batch, angle) if batch else select_local_inspiration(angle)
                return task.model_copy(update={"inspiration": selection})
    return task


def render_fact_cards(evidence, task):
    key = product_key(evidence)
    selected, enabled = [], False
    with st.expander("已确认产品事实卡（可选）"):
        try:
            cards = list_cards(evidence.product_model)
            valid, states = [], {}
            for card in cards:
                try:
                    check_card(card, evidence)
                    valid.append(card)
                    states[card.card_id] = "有效"
                except EvidenceError:
                    states[card.card_id] = "已停用 / 已过期 / 需重新核对"
            if cards:
                st.dataframe([{"允许表述": c.claim, "适用范围": c.scope, "来源": c.source_title,
                               "有效期至": str(c.valid_until or "长期"), "状态": states[c.card_id]}
                              for c in cards], hide_index=True, use_container_width=True)
                enabled = st.checkbox("本次仅使用已确认事实卡", value=bool(valid), key="card_mode_" + key)
                ids = [c.card_id for c in valid]
                chosen = st.multiselect("本次采用的事实卡", ids, default=ids[:8],
                                        format_func=lambda value: next(c.claim for c in valid if c.card_id == value),
                                        max_selections=8, key="selected_cards_" + key)
                selected = [c for c in valid if c.card_id in chosen]
                if enabled and not selected:
                    st.warning("没有可采用的有效事实卡，请补充资料或取消事实卡模式。")
                active = [c for c in cards if c.status == "已确认"]
                if active:
                    stop_id = st.selectbox("需要停用的事实卡", ["", *[c.card_id for c in active]],
                                           format_func=lambda value: next((c.claim for c in active if c.card_id == value), "请选择"),
                                           key="stop_card_" + key)
                    if st.button("停用选中事实卡", disabled=not stop_id, icon=":material/block:"):
                        disable_card(stop_id)
                        st.rerun()
                st.download_button("导出当前型号事实卡", json.dumps([c.model_dump(mode="json") for c in cards],
                                   ensure_ascii=False, indent=2), "产品事实卡.json", mime="application/json")
            else:
                st.write("当前型号还没有已确认事实卡。")
            with st.form("new_fact_card_" + key, clear_on_submit=True):
                source_id = st.selectbox("原文来源", [s.source_id for s in evidence.sources],
                                         format_func=lambda value: next(s.title for s in evidence.sources if s.source_id == value))
                quote = st.text_area("连续原文引句", max_chars=3000)
                claim = st.text_area("允许使用的产品表述", max_chars=500)
                category = st.selectbox("事实类别", ["产品参数", "软件兼容", "价格", "赠品", "门店服务"])
                scope = st.text_input("适用型号、配置或条件", value=evidence.product_model, max_chars=300)
                start = st.date_input("生效日期", value=local_today())
                end = st.date_input("有效期至", value=local_today() + timedelta(days=30))
                person = st.text_input("资料确认人", max_chars=60)
                confirmed = st.checkbox("我已核对原始资料、适用条件和有效期")
                submitted = st.form_submit_button("保存已确认事实卡", icon=":material/save:")
            if submitted:
                if not confirmed:
                    st.warning("请先核对原始资料并确认。")
                else:
                    create_card(evidence, source_id, claim, quote, category, scope, start, end, person)
                    st.rerun()
        except (EvidenceError, ValidationError, OSError):
            st.error("事实卡未能保存或读取，请核对引句、有效期、确认人及本地文件权限。")
    snapshot = evidence.model_copy(update={"fact_card_mode": enabled, "approved_cards": selected})
    if task is not None:
        task = task.model_copy(update={"fact_card_signature": cards_signature(enabled, selected)})
    return snapshot, task


def set_cover_headline(value_key, revision_key, headline):
    st.session_state[value_key] = headline
    st.session_state[revision_key] = st.session_state.get(revision_key, 0) + 1


def render_cover(evidence, task, settings):
    if task is None:
        return
    with st.expander("制作真实产品封面（可选）"):
        key = product_key(evidence)
        headlines = headlines_for(task.product_model)
        asset = None
        phone = is_phone(task.product_model)
        if phone:
            assets = [a for a in evidence.images if a.verified
                      and normalize_model(a.product_model) == normalize_model(task.product_model)]
            if assets:
                image_id = st.selectbox("封面使用的图片", [a.image_id for a in assets],
                                       format_func=lambda value: next(a.name for a in assets if a.image_id == value),
                                       key="phone_cover_asset_" + key)
                asset = next(a for a in assets if a.image_id == image_id)
            else:
                st.info("请先在“产品图片”中选择并确认图片。")
        else:
            source_mode = st.radio("封面原图", ["产品资料原图", "上传门店实拍"], horizontal=True, key="cover_source_" + key)
            if source_mode == "产品资料原图":
                assets = [a for a in evidence.images if a.verified
                          and normalize_model(a.product_model) == normalize_model(task.product_model)]
                if assets:
                    image_id = st.selectbox("选择原图", [a.image_id for a in assets],
                                           format_func=lambda value: next(a.name for a in assets if a.image_id == value),
                                           key="cover_asset_" + key)
                    asset = next(a for a in assets if a.image_id == image_id)
                else:
                    st.info("暂无已核对的原图，可以上传门店实拍。")
            else:
                upload = st.file_uploader("上传当前型号的实拍图片", type=["png", "jpg", "jpeg", "webp"], key="cover_upload_" + key)
                if upload:
                    data = upload.getvalue()
                    try:
                        decode_image(data)
                        asset = ImageAsset(image_id="UPLOAD_" + sha256(data).hexdigest()[:12], name=upload.name,
                                           product_model=task.product_model, source_title="用户上传门店实拍",
                                           verified=True, verification_method="用户确认型号及使用权限", data=data)
                    except EvidenceError as exc:
                        st.warning(str(exc))
        if asset is None:
            st.session_state.pop("created_cover", None)
            return
        digest = sha256(asset.data).hexdigest()
        st.image(asset.data, width=300)
        rights = True if phone else st.checkbox("我确认原图为当前型号，并有权用于本次发布", key="cover_rights_" + key + digest[:16])
        value_key = "cover_title_value_" + key
        revision_key = "cover_title_revision_" + key
        # 独立保存标题；推荐按钮创建新输入实例，避免切换入口时前端丢失默认值。
        title_key = f"cover_headline_text_{key}_{st.session_state.get(revision_key, 0)}"
        headline = st.text_input("封面标题", value=st.session_state.get(value_key, headlines[0]),
                                 max_chars=MAX_HEADLINE_LENGTH, key=title_key)
        st.session_state[value_key] = headline
        st.caption("推荐标题")
        with st.container(horizontal=True, gap="small"):
            for index, suggestion in enumerate(headlines):
                st.button(suggestion, key=f"cover_suggestion_{key}_{index}",
                          on_click=set_cover_headline, args=(value_key, revision_key, suggestion))
        palette = st.radio("封面配色", list(PALETTES), horizontal=True, key="cover_palette_" + key)
        result = st.session_state.get("result")
        approved = []
        if result:
            try:
                require_publishable(result, task)
                approved = [f for f in result.facts if f.fact_id in result.fact_plan.selected_fact_ids]
            except EvidenceError:
                result = None
        fact_id = st.selectbox("封面展示的已核对事实", ["", *[f.fact_id for f in approved]],
                               format_func=lambda value: next((f.claim for f in approved if f.fact_id == value), "不展示参数"),
                               key="cover_fact_" + key)
        basis = result_signature(result) if result and (fact_id or headline.strip() not in headlines) else ""
        anchor = (digest, task.model_dump_json(), headline, palette, fact_id, rights, basis)
        cover = st.session_state.get("created_cover")
        if cover and cover["anchor"] != anchor:
            st.session_state.pop("created_cover", None)
            cover = None
        if st.button("制作封面", icon=":material/image:", disabled=not rights or not headline.strip()):
            st.session_state.pop("created_cover", None)
            try:
                title = clean_headline(headline)
                engine = LangChainEngine(settings) if title not in headlines else None
                with st.spinner("正在核验标题并制作封面…"):
                    item = make_cover(asset, task, title, palette, rights, result, fact_id, engine=engine)
                cover = {"anchor": anchor, "item": item}
                st.session_state.created_cover = cover
            except (EvidenceError, ModelError) as exc:
                st.error(str(exc))
                cover = None
        if cover:
            # 领取前再次检查参数稿件与事实卡，过期或停用后旧封面也不再放行。
            try:
                require_current_cover(cover["item"], task, result)
                st.image(cover["item"].png, width=420)
                st.download_button("下载封面 PNG", cover["item"].png, "产品封面.png", mime="image/png")
                st.download_button("下载封面与原图包", cover["item"].package(), "产品封面素材包.zip", mime="application/zip")
            except EvidenceError as exc:
                st.warning(str(exc))

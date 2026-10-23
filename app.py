"""网页入口：把各模块组合成可点击操作的 Streamlit 网页。

启动：在项目目录执行 python -m streamlit run app.py。
阅读本文件时，可以把 st.xxx 理解成“在网页上画出某个组件”。
真正的资料读取、模型调用、规则核验分别放在其他文件，方便单独理解。

特别注意 Streamlit 的运行方式：用户点击按钮、修改输入框时，通常会从头
重新执行整个文件。因此，需要跨次执行保留的数据放在 st.session_state
（当前浏览器会话的临时记忆）中；普通变量每次执行都会重新赋值。
"""

import base64
import hashlib  # Python 自带的摘要工具，用于判断配置是否变化，不用于存放明文密钥。

import streamlit as st
from pydantic import ValidationError

# from 文件名 import 名称：使用其他文件已经定义好的类或函数。
# 这里的文件名不写 .py；这些模块都在本项目中。
from checks import EvidenceError, task_questions
from config import ROOT, COMPETITION_KB_NAME, load_settings
from diagnostics import record_unexpected_error, runtime_signature
from evidence_utils import timestamp
from exporter import to_markdown, to_publish_text, to_publish_package
from ima_client import ImaClient, ImaError
from ima_ui import render_ima_workspace, render_note_save
from llm import LangChainEngine, ModelError
from playbooks import playbooks_for, ANGLES, store_task
from product_types import is_phone
from schemas import PLATFORM_RULES, Task, normalize_model
from workflow import run_workflow
from creation_ui import (render_campaign, render_intake, render_inspiration, render_fact_cards,
                         render_cover, render_product_photos)
from fact_cards import facts_from_cards

brand_logo = ROOT / "assets/huawei_logo.jpg"
st.set_page_config(page_title="华为门店营销助手", page_icon=str(brand_logo), layout="wide")
st.logo(str(brand_logo), size="large")
logo_data = base64.b64encode(brand_logo.read_bytes()).decode("ascii")
st.html(f"""
<style>
.brand-header {{ display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }}
.brand-header img {{ width: 72px; height: 72px; flex: 0 0 auto; object-fit: contain; }}
.brand-header h1 {{ margin: 0; padding: 0; font-size: 32px; font-weight: 600;
    line-height: 1.35; letter-spacing: 0; min-width: 0; overflow-wrap: anywhere; }}
@media (max-width: 640px) {{
    .brand-header {{ gap: 12px; margin-bottom: 16px; }}
    .brand-header img {{ width: 52px; height: 52px; }}
    .brand-header h1 {{ font-size: 24px; }}
}}
</style>
<header class="brand-header">
    <img src="data:image/jpeg;base64,{logo_data}" alt="HUAWEI">
    <h1>华为门店营销助手</h1>
</header>
""")
mode = st.radio("工作模式", ["门店简易模式", "完整工作台"], horizontal=True, key="workspace_mode")

# try/except：先尝试执行；出现指定错误时给出中文提示，而不是显示整屏报错。
try:
    settings = load_settings()
except (ValueError, TypeError):
    st.error(".env 配置格式有误，请检查 LLM_TIMEOUT 是否为整数。")
    st.stop()  # 立即结束本次页面执行；修正配置并刷新后再继续。

# 凭证变化时清除旧连接和旧产品，避免使用上一次账号的缓存。
# repr 把多个配置值组成文字，encode 转字节，sha256 再生成摘要。
# session_state 只用摘要比较配置是否变化；摘要并不是可逆的密钥加密备份。
signature = hashlib.sha256(repr((settings.ima_client_id, settings.ima_api_key, settings.kb_name,
                                str(settings.skill_path), runtime_signature())).encode()).hexdigest()
if st.session_state.get("config_signature") != signature:
    if st.session_state.get("config_signature"):
        st.info("程序或配置已更新，旧连接和资料已清理。请重新连接知识库并加载产品资料。")
    st.session_state.pop("account_confirmed", None)
    for key in ("ima", "product_root", "phone_products", "phone_exclusions", "evidence", "result", "ima_search_results", "ima_search_document",
                "ima_search_selection", "ima_created_notes", "ima_note_title", "ima_note_content",
                "intake_state", "web_bundle", "created_cover"):
        st.session_state.pop(key, None)  # 删除旧状态；不存在时返回 None，不报错。
    st.session_state.config_signature = signature

# with 表示“下面缩进的组件都放在侧边栏内”。主页面同样使用这种容器写法。
with st.sidebar:
    st.subheader("连接与配置")
    st.caption("探索版 V2 · 独立改进目录")
    st.write("模型：", settings.model)
    st.caption("配置文件：本项目的 .env。修改保存后刷新本页。")
    st.write("模型密钥：" + ("已配置" if settings.api_key else "未配置"))
    st.write("ima 凭证：" + ("已找到，连接后验证" if settings.ima_credentials_available() else "未找到"))
    st.caption(f"本次配置知识库：{settings.kb_name}")
    if settings.kb_name != COMPETITION_KB_NAME:
        st.info("按你的选择继续使用 copy 库。它与手册指定库不同，参赛适用性需向举办方确认。")
    if st.button("测试模型连接", use_container_width=True):
        # button 只在刚点击的这次执行中返回 True；刷新页面不会自动再次调用。
        # ping 会真实调用模型，可能产生少量费用。
        try:
            with st.spinner("正在测试模型接口…"):
                st.success(LangChainEngine(settings).ping())
        except ModelError as exc:
            st.error(str(exc))
    if st.button("连接 / 刷新 ima 知识库", type="primary", use_container_width=True):
        # 刷新失败时同样清除旧结果，不能留下看似仍然可用的状态。
        for key in ("ima", "product_root", "phone_products", "phone_exclusions", "evidence", "result", "ima_search_results", "ima_search_document",
                    "ima_search_selection", "ima_created_notes", "ima_note_title", "ima_note_content",
                    "intake_state", "web_bundle", "created_cover"):
            st.session_state.pop(key, None)
        try:
            with st.spinner("正在连接知识库…"):
                client = ImaClient(settings)
                client.connect()
                root = client.product_root()
                phone_products, phone_exclusions = client.phone_products(root)
            # 只有连接和目录读取都成功，才保存客户端供后面的界面使用。
            st.session_state.ima = client
            st.session_state.product_root = root
            st.session_state.phone_products = phone_products
            st.session_state.phone_exclusions = phone_exclusions
        except ImaError as exc:
            st.error(str(exc))
    if "ima" in st.session_state:
        st.success("知识库读取验证成功")
        st.write(st.session_state.ima.kb_name)
        st.caption("凭证对应账号可读取该库；具体登录账号请在 ima 客户端确认。")
        # 官方接口只能验证读权限，不能据此声称自动识别了登录账号。
        st.checkbox("我已在 ima 中核对，凭证与参赛账号属于同一账号", key="account_confirmed")
    else:
        st.info("先连接知识库，再选择具体产品。")
    with st.expander("第一次使用"):
        st.write("1. 在 .env 填写密钥并保存。\n2. 先测试模型，再连接 ima。\n3. 逐级选择产品并加载资料。\n4. 填写任务，生成并检查结果。")
    # 下载全部阶段的提示词；下载操作不需要调用模型。
    prompt_text = "\n\n".join(f"# {path.name}\n\n{path.read_text(encoding='utf-8')}" for path in sorted((ROOT / "prompts").glob("*.txt")))
    st.download_button("下载系统提示词", prompt_text, "系统提示词.txt", mime="text/plain", use_container_width=True)

if "ima" not in st.session_state:
    st.info("请先点击左侧“连接 / 刷新 ima 知识库”。模型密钥暂未填写时，也可以先浏览产品资料。")
    st.stop()

# 下面只有在连接成功时才会运行。搜索/笔记入口由 ima_ui.py 单独绘制。
client = st.session_state.ima
if mode == "完整工作台":
    render_ima_workspace(client)
st.subheader("1. 选择产品")
st.caption("按知识库真实目录逐级选择，版本以加载后的产品原文为准。")
current = st.session_state.product_root
selected_folder = None
try:
    # 从产品根目录逐层显示下拉框，最多展开 7 层，避免异常目录无限深入。
    # readable 表示本层存在可读资料；图片和管理目录不作为产品分类展示。
    for depth in range(7):
        if current.get("phone_catalog"):
            phones = st.session_state.get("phone_products", [])
            phone_choice = st.selectbox("手机型号", range(len(phones)),
                                       format_func=lambda i: phones[i]["title"], index=None,
                                       placeholder="请选择手机型号", key="phone_product_choice")
            if st.session_state.get("phone_exclusions"):
                st.caption("系列对比及 Pro Max / 典藏版合并资料暂未作为单型号开放。")
            if phone_choice is not None:
                selected_folder = phones[phone_choice]
            break
        entries = client.list_folder(client.entry_id(current))
        readable = any(x.get("title", "").lower().endswith((".md", ".txt", ".pdf")) for x in entries if not client.is_folder(x))
        if current.get("title", "").startswith("07-"):
            readable = False
        folders = [x for x in entries if client.is_folder(x) and not x.get("title", "").lower().startswith(("images", "00-", "90-", "99-")) and "图片" not in x.get("title", "")]
        if depth == 0 and st.session_state.get("phone_products"):
            folders.append({"title": "手机", "phone_catalog": True})
        if readable:
            selected_folder = current
        if not folders:
            break
        labels = [x.get("title") or x.get("name", "未命名目录") for x in folders]
        # key 是组件的唯一标识。不同文件夹用不同 key，避免下拉框状态串到别处。
        widget_key = "browse_" + hashlib.sha256(client.entry_id(current).encode()).hexdigest()[:12]
        # 下拉框实际返回整数序号；format_func 只负责把序号显示成目录名。
        # lambda 的 names=labels 固定当前这层的名称列表。
        choice = st.selectbox("产品分类" if depth == 0 else f"第 {depth + 1} 级目录", range(len(folders)),
                              format_func=lambda i, names=labels: names[i], index=None,
                              placeholder="请选择目录或产品", key=widget_key)
        if choice is None:
            break
        current = folders[choice]
        selected_folder = None
except ImaError as exc:
    st.error(str(exc))
    st.stop()

if selected_folder is None:
    for key in ("evidence", "result", "created_cover"):
        st.session_state.pop(key, None)
    st.info("请继续选择到包含产品事实资料的具体产品目录。")
    st.stop()

selected_id = client.entry_id(selected_folder)
# 切换产品后旧依据和草稿都失效，防止把 A 产品的结果误当成 B 产品结果。
if "evidence" in st.session_state and (st.session_state.evidence.document_id or st.session_state.evidence.folder_id) != selected_id:
    st.session_state.pop("evidence", None)
    st.session_state.pop("result", None)
    st.session_state.pop("created_cover", None)
if st.button("加载当前产品资料与图片", type="primary"):
    # 读取较慢的原文和图片只在点击时发生，日常输入文字不会反复下载。
    st.session_state.pop("evidence", None)
    st.session_state.pop("result", None)
    try:
        with st.spinner("正在读取产品原文、图片索引和图片…"):
            # 明确重新加载时清除内存缓存；填写表单不会触发反复下载。
            client.refresh_cache()
            st.session_state.evidence = client.load_product(selected_folder)
    except ImaError as exc:
        st.error(str(exc))

if "evidence" not in st.session_state:
    st.stop()
evidence = st.session_state.evidence
st.success(f"已加载：{evidence.product_model} · {len(evidence.sources)} 份资料 · {len(evidence.images)} 张图片")
for notice in evidence.notices:
    st.info(notice)
# expander 是可展开的面板。这里只截短“页面预览”，不会把已加载的依据改短。
with st.expander("查看原始资料与图片依据"):
    for source in evidence.sources:
        st.markdown(f"**{source.title}**")
        st.text(source.text[:8000])
        if len(source.text) > 8000:
            st.caption("页面仅预览前 8000 字，提取使用已读取的完整文本范围。")
    for image in evidence.images:
        left, right = st.columns([1, 3])
        left.image(image.data, width=180)
        with right:
            st.write(image.name)
            st.caption(f"来源：{image.source_title}；索引型号：{image.product_model}")
            if image.verified:
                st.caption("型号已按图片索引或此前的人工确认核对；不代表完成视觉自动识别。")
            elif normalize_model(image.product_model) == normalize_model(evidence.product_model):
                # 只有索引型号相同但尚未验证的图片，才允许用户明确勾选确认。
                # verified 是资料/人工核对状态，不是图像识别模型给出的结论。
                # 确认绑定图片内容。即便同名图片换了内容，也不能沿用旧勾选。
                image_digest = hashlib.sha256(image.data).hexdigest()[:16]
                if st.checkbox("我已在 ima 中人工核对，此图确为当前型号", key=f"confirm_{selected_id}_{image.image_id}_{image_digest}"):
                    image.verified = True
                    image.verification_method = "用户在 ima 中人工核对型号"
                    image.verified_at = timestamp()
                    st.session_state.pop("result", None)
            else:
                st.warning("图片索引型号不一致，该图片不会用于生成。")

evidence = render_product_photos(evidence)

st.subheader("2. 本次发布")
task = None
revision = ""
left, right = st.columns(2)
creation_method = "快捷选择"
if mode == "门店简易模式":
    creation_method = st.radio("创作方式", ["快捷选择", "领取总部任务", "一句话需求"], horizontal=True, key="creation_method")
    if creation_method == "领取总部任务":
        task = render_campaign(evidence)
    elif creation_method == "一句话需求":
        task = render_intake(evidence, settings)
    else:
        with left:
            books = playbooks_for(evidence.product_model)
            audience = st.selectbox("发给谁", list(books), key="store_audience_" + ("phone" if is_phone(evidence.product_model) else "pc"))
            scene = st.selectbox("聊什么场景", books[audience]["scenes"], key=f"store_scene_{audience}")
        with right:
            platform = st.selectbox("发布平台 *", list(PLATFORM_RULES), key="store_platform")
            angle = st.selectbox("内容切入", list(ANGLES), key="store_angle")
        task = store_task(evidence.product_model, audience, scene, platform, angle)
else:
    # 完整工作台保留自由输入，避免精选方案限制已有任务。
    with left:
        audience = st.text_input("目标人群 *", value="大学新生", max_chars=150)
        scenario = st.text_input("使用场景 *", value="换手机，核对常用应用、拍摄和握持需求" if is_phone(evidence.product_model) else "开学购机，兼顾学习、携带和多设备协同", max_chars=300,
                                 key="full_scenario_" + ("phone" if is_phone(evidence.product_model) else "pc"))
        platform = st.selectbox("发布平台 *", list(PLATFORM_RULES))
    with right:
        goal = st.text_input("内容目标", value="引导咨询或到店体验", max_chars=150)
        buying_stage = st.selectbox("购买阶段", ["了解与比较", "准备到店体验", "接近购买决策"])
        requirements = st.text_area("补充要求", placeholder="例如：避免堆砌参数，先讲使用场景。", max_chars=1500)
    revision = st.text_input("修改要求（已有草稿时使用）", placeholder="例如：压缩篇幅，改为导购口吻。", max_chars=1000)
    try:
        task = Task(product_model=evidence.product_model, audience=audience, scenario=scenario, platform=platform,
                    goal=goal or "引导咨询或到店体验", buying_stage=buying_stage, requirements=requirements, revision=revision)
    except ValidationError:
        st.warning("请补充目标人群和使用场景。")

task = render_inspiration(task, settings)
generation_evidence, task = render_fact_cards(evidence, task)
card_error = False
if generation_evidence.fact_card_mode:
    try:
        facts_from_cards(generation_evidence)
    except EvidenceError as exc:
        card_error = True
        st.warning(str(exc))

# 必填不等于清楚。例如“所有人、日常用”虽然不为空，仍需补充具体任务。
questions = task_questions(task) if task else []
for question in questions:
    st.warning(question)

if not settings.api_key:
    st.info("在本项目 .env 填写 DEEPSEEK_API_KEY 并保存，刷新页面即可生成。")
phone_image_missing = bool(task and is_phone(task.product_model)
                           and len([image for image in generation_evidence.images if image.verified]) < PLATFORM_RULES[task.platform]["min_images"])
if phone_image_missing:
    st.info(f"本平台图文至少需要 {PLATFORM_RULES[task.platform]['min_images']} 张本型号图片，请先补充并确认产品图片，可选择原图或上传实拍。")
generate = st.button("生成本次图文" if mode == "门店简易模式" else "生成 / 按当前要求重新生成", type="primary",
                     disabled=not task or bool(questions) or card_error or phone_image_missing or not settings.api_key)
if mode == "门店简易模式" and creation_method == "快捷选择":
    def next_angle():
        names = list(ANGLES)
        st.session_state.store_angle = names[(names.index(st.session_state.store_angle) + 1) % len(names)]

    change_angle = st.button("换个角度生成", icon=":material/refresh:", on_click=next_angle,
                             disabled=card_error or phone_image_missing or not settings.api_key)
    generate = generate or change_angle
if generate:
    previous = st.session_state.get("result")
    # 生成失败时不把旧草稿冒充为当前结果。
    st.session_state.pop("result", None)
    generation_stage = "初始化生成"

    def show_progress(message):
        # 保存最后一个固定阶段，遇到未知异常时可以知道停在哪里。
        global generation_stage
        generation_stage = message
        st.write(message)

    try:
        with st.status("正在生成内容…", expanded=True) as status:
            # show_progress 把阶段进度画到网页，并为错误定位保留最后一个阶段。
            # 有旧草稿且填写了修改要求，才把旧稿作为本次生成的参考。
            result = run_workflow(task, generation_evidence, LangChainEngine(settings), progress=show_progress,
                                  previous=previous.draft if previous and revision else None,
                                  evidence_loader=client.retrieve_for_task)
            generation_stage = "保存生成结果"
            st.session_state.result = result
            status.update(label="内容已生成，请查看核验结果", state="complete", expanded=False)
    except EvidenceError as exc:
        st.error(str(exc))
        if exc.details:
            st.write("取证失败明细（请按来源与片段编号核对）：")
            st.dataframe(exc.details, hide_index=True, use_container_width=True)
    except (ModelError, ImaError) as exc:
        st.error(str(exc))
    except Exception as exc:
        # 不展示原始异常文本，避免请求或凭证进入页面；本地日志只记类型和代码位置。
        st.session_state.pop("result", None)
        error_id = record_unexpected_error(exc, generation_stage)
        st.error(f"处理未完成，停止在：{generation_stage}。本次没有可用的新稿。")
        if error_id:
            st.caption(f"错误编号：{error_id}。定位记录已保存到本项目 logs/errors.jsonl，可据此检查程序。")
        else:
            st.caption("本地错误记录无法写入，请检查项目目录写入权限。")
        st.info("代码更新后请重新连接知识库、加载资料再生成。持续出错时保留错误编号，避免连续重复调用模型。")

render_cover(generation_evidence, task, settings)

if "result" not in st.session_state:
    st.stop()
result = st.session_state.result
st.subheader("3. 查看与导出")
st.caption(f"结果对应：{result.task.product_model} / {result.task.audience} / {result.task.platform}；生成时间：{result.generated_at}")
# Result 保存了生成时的 Task 快照。当前输入与快照不同，旧稿仍可看但不能导出/保存。
stale = task is None or result.task != task
if stale:
    st.warning("任务已修改。下面仍是上一次结果，请重新生成后再导出当前任务。")
if not stale and result.review.passed:
    try:
        publish_text = to_publish_text(result, task)
        st.success("本次图文检查通过")
        st.code(publish_text, language=None, wrap_lines=True)
        st.download_button("下载纯净文案", publish_text.encode("utf-8-sig"), f"{result.task.platform}_发布文案.txt", mime="text/plain")
        package = to_publish_package(result, task)
        st.download_button("领取文案与图片包", package, f"{result.task.platform}_发布包.zip", mime="application/zip")
    except EvidenceError as exc:
        st.error(str(exc))
elif not stale:
    st.warning("本次内容尚未通过检查，请在核验结果中查看原因。")
# 四个页签分别展示流程产物；切换页签本身不代表重新调用模型。
tabs = st.tabs(["场景判断", "知识库依据", "图文软文", "核验结果"])
with tabs[0]:
    st.write("用户问题：", result.strategy.audience_problem)
    st.write("内容角度：", result.strategy.angle)
    st.write("优先卖点方向：", "；".join(result.strategy.priority_points))
    st.write("结构：", " → ".join(result.strategy.structure))
    st.write("语气：", result.strategy.tone)
    st.write("行动引导：", result.strategy.call_to_action)
with tabs[1]:
    st.write("实际来源库：", result.evidence.kb_name or client.kb_name)
    st.caption(f"资料加载时间：{result.evidence.loaded_at or '历史记录未提供'}；采用本次读取的资料快照。")
    for trace in result.evidence.retrieval:
        st.write(f"检索：{trace.query}")
        st.caption(f"命中 {trace.hit_count} 项；排除 {trace.excluded_count} 项；目录回退：{'是' if trace.fallback_used else '否'}")
        st.write("可采纳资料：", "；".join(trace.accepted_titles) or "无，使用当前型号目录原文")
    # 字典把 S1 等来源编号映射到实际资料，后面用编号快速找到资料标题。
    sources = {source.source_id: source for source in result.evidence.sources}
    if result.fact_plan:
        st.write("选材理由：", result.fact_plan.selection_reason)
        st.dataframe([{"事实编号": row.fact_id, "状态": row.status, "原因": row.reason}
                      for row in result.fact_plan.checks], hide_index=True, use_container_width=True)
    for fact in result.facts:
        st.markdown(f"**{fact.fact_id} · {fact.claim}**")
        source = sources[fact.source_id]
        st.caption(f"{source.title} · {fact.segment_id or fact.source_id} · 读取于 {source.loaded_at or '未记录'}")
        st.text(fact.quote)
with tabs[2]:
    draft = result.draft
    st.subheader(draft.title.text)
    st.write(draft.intro.text)
    image_map = {image.image_id: image for image in result.evidence.images}

    def render_images(position):
        """按位置显示配图：0 表示导语之后，1 表示正文第一段之后，以此类推。"""
        # 这是只供当前页签使用的小函数，复用后不必为每一段重复写显示逻辑。
        for placement in draft.images:
            if placement.after_paragraph == position and placement.image_id in image_map:
                image = image_map[placement.image_id]
                st.image(image.data, caption=placement.caption, width=500)
                st.caption(f"来源：{image.source_title}；插入理由：{placement.reason}")

    render_images(0)
    for index, block in enumerate(draft.body, 1):
        st.write(block.text)
        render_images(index)
    st.write(draft.cta.text)
    st.write(" ".join(draft.tags))
with tabs[3]:
    if result.review.passed:
        st.success("程序与模型检查通过；发布前请人工复核销售配置及有效信息。")
    else:
        st.warning("尚未通过检查，仅供修改和人工确认。")
    st.caption(f"取证重试：{result.extraction_retry_count} 次；文案修正：{result.repair_count} 次。图片未执行自动视觉型号识别。")
    st.write("事实逐项核验")
    st.dataframe([{"位置": row.location, "原稿文字": row.text, "事实编号": "、".join(row.fact_ids),
                   "状态": row.status, "原因": row.reason} for row in result.review.fact_checks],
                 hide_index=True, use_container_width=True)
    st.write("图片核验")
    st.dataframe([{"图片": row.name, "来源": row.source_title, "型号": row.product_model,
                   "核对方式": row.verification_method, "位置": row.location, "图注": row.caption,
                   "状态": row.status, "原因": row.reason} for row in result.review.image_checks],
                 hide_index=True, use_container_width=True)
    st.write("宣传风险核验")
    st.dataframe([{"类别": row.category, "位置": row.location, "原句": row.text, "状态": row.status,
                   "原因": row.reason, "修改建议": row.action} for row in result.review.compliance_checks],
                 hide_index=True, use_container_width=True)
    for issue in result.review.issues:
        # 条件表达式先选“红色错误框”或“黄色提醒框”，再用问题文本调用它。
        (st.error if issue.severity == "error" else st.warning)(issue.message)
    if result.review.needs_confirmation:
        st.error("存在需要人工确认的问题，请处理后重新生成。")
    for blocking in result.review.blocking:
        st.error("需要处理：" + blocking)
    for notice in result.review.notices:
        st.info(notice)

# exporter 只拼接文本，不调用模型、不写 ima；保存笔记必须再点击专门的按钮。
markdown = to_markdown(result)
label = "下载图文内容与核验记录" if result.review.passed else "下载待确认草稿（含问题记录）"
st.download_button(label, markdown, f"{result.task.platform}_{'已检查' if result.review.passed else '待确认'}_营销内容.md",
                   mime="text/markdown", disabled=stale)
st.caption("也可将完整内容与核验记录新建为 IMA 个人笔记；图片保存为来源与配图说明。")
render_note_save(client, markdown, key="ima_save_result", label="在 IMA 新建营销笔记", disabled=stale)
with st.expander("复制完整内容"):
    if stale:
        st.warning("当前输入已变化，请重新生成后复制。")
    else:
        st.code(markdown, language="markdown")

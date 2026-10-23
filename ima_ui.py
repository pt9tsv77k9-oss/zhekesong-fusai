"""IMA 资料搜索与笔记入口；复用后端客户端，不将凭证交给浏览器或模型。"""

import hashlib

import streamlit as st

from ima_client import ImaClient, ImaError


def note_fingerprint(content: str, title: str = "") -> str:
    """把标题和正文生成固定长度摘要，用来识别同一篇笔记，不传送或保存明文密钥。"""
    # \0 是分隔符，避免标题/正文拼接位置不同却得到相同的输入文本。
    return hashlib.sha256((title + "\0" + content).encode("utf-8")).hexdigest()


def render_note_save(client: ImaClient, content: str, *, title: str = "", key: str,
                     label: str = "在 IMA 新建笔记", disabled: bool = False):
    """一次点击只创建一篇；当前会话内相同正文成功后禁用重复保存。"""
    # 参数中的 * 表示其后的参数必须写名字，例如 key="ima_note_create"。
    # disabled 由外部控制：营销任务修改后，禁止把旧结果保存成当前任务的笔记。
    try:
        fingerprint = note_fingerprint(content, title)
    except UnicodeError:
        st.error("标题或正文含有无法编码为 UTF-8 的字符，请修改后重试。")
        return
    receipts = st.session_state.setdefault("ima_created_notes", {})
    # setdefault：已存在就取原字典，不存在就放入空字典。
    # 回执只在本次连接会话有效；重新连接/新会话不具备服务器端全局去重能力。
    saved = fingerprint in receipts
    if st.button(label, key=key, disabled=disabled or saved or not content.strip()):
        try:
            with st.spinner("正在新建 IMA 笔记…"):
                # create_note 成功返回 ID 后才存回执；出错不会显示“已保存”。
                receipts[fingerprint] = client.create_note(content, title)
            # 更新按钮的禁用状态；后续页面刷新不再次提交。
            st.rerun()
        except ImaError as exc:
            st.error(str(exc))
    if saved:
        st.success("已创建 IMA 笔记，可在当前凭证账号的个人笔记中查看。")


def render_ima_workspace(client: ImaClient):
    """绘制两个独立工具：搜索/读资料，以及新建个人文字笔记。"""
    with st.expander("搜索 IMA 资料"):
        st.caption("搜索当前连接的知识库；查阅结果不会替换营销任务中已选产品的依据。")
        with st.form("ima_search_form"):
            # form 将输入和提交合并；输入过程中不会每改一个字就搜索。
            query = st.text_input("资料关键词", max_chars=200)
            submitted = st.form_submit_button("搜索资料")
        if submitted:
            # 新搜索先清旧搜索/正文，失败或空结果时不会残留上一轮的原文。
            st.session_state.pop("ima_search_results", None)
            st.session_state.pop("ima_search_document", None)
            st.session_state.pop("ima_search_selection", None)
            if not query.strip():
                st.warning("请填写资料关键词。")
            else:
                try:
                    with st.spinner("正在搜索 IMA 资料…"):
                        st.session_state.ima_search_results = client.search(query.strip())
                except ImaError as exc:
                    st.error(str(exc))
        hits = st.session_state.get("ima_search_results")
        if hits is not None:
            if not hits:
                st.info("没有找到匹配资料，请换一个关键词，或按下方产品目录查找。")
            else:
                st.caption(f"找到 {len(hits)} 项结果。")
                selected = st.selectbox("选择搜索结果", range(len(hits)),
                                        format_func=lambda i: hits[i].get("title") or hits[i].get("name") or "未命名资料",
                                        key="ima_search_selection")
                item = hits[selected]
                if item.get("highlight_content"):
                    st.text(item["highlight_content"])
                if st.button("读取选中资料", key="ima_search_read"):
                    st.session_state.pop("ima_search_document", None)
                    try:
                        with st.spinner("正在读取原文…"):
                            text = client.read_text(item)
                        if text.strip():
                            st.session_state.ima_search_document = (selected, text)
                        else:
                            st.info("没有提取到正文；扫描 PDF 等资料请在 ima 客户端查看。")
                    except ImaError as exc:
                        st.error(str(exc))
                document = st.session_state.get("ima_search_document")
                # 正文和当时选中的结果序号一起保存，切换结果后不展示错位正文。
                if document and document[0] == selected:
                    st.text(document[1][:24000])
                    st.caption("预览最多 24000 字；PDF 仅提取前 25 页文字，暂不支持扫描件 OCR 或网页正文解析。")

    with st.expander("新建 IMA 笔记"):
        # 这个入口独立于营销生成；没有选产品也可以记录文字，点击按钮才写入。
        st.caption("支持文字 Markdown，保存到当前凭证账号的个人笔记默认位置。点击新建后才写入。")
        title = st.text_input("笔记标题", max_chars=200, key="ima_note_title")
        content = st.text_area("笔记正文（Markdown）", height=180, key="ima_note_content")
        render_note_save(client, content, title=title, key="ima_note_create")

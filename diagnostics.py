"""只记录定位程序错误需要的信息，不记录密钥、任务正文或接口响应。

未知异常不能只显示一句“请联系开发者”，也不能直接打印完整异常：
Pydantic/SDK 的异常文本可能带输入数据。因此这里只保存错误类型、程序位置、
固定阶段名和错误编号，不调用 str(error)，不保存局部变量或源码行。
"""

import hashlib
import json
from pathlib import Path
import uuid

from config import ROOT
from evidence_utils import timestamp

LOG_DIR = ROOT / "logs"

# 来自 workflow.py 的固定进度文字，不允许把用户输入作为日志阶段。
STAGES = {
    "初始化生成", "正在形成场景与平台策略", "正在按型号、场景与平台查找资料",
    "正在提取并核对原文依据", "发现取证编号或引句问题，正在针对性重新提取一次",
    "正在检查事实支持关系并选择写作卖点",
    "正在生成图文内容", "正在核验事实、图片及营销表述",
    "发现可修正问题，正在修正一次", "正在重新核验修正结果", "保存生成结果",
}


def runtime_signature() -> str:
    """业务代码/提示词更新时改变指纹，避免网页沿用旧类创建的缓存对象。

    Streamlit 会在代码改动后重新运行页面，但 session_state 中可能仍保留旧的
    ImaClient、Source、Result 对象。旧对象不一定具有新字段/方法，应重新连接读取。
    这里只对源码取摘要，不读取 .env，也不会因修改说明书而清理资料。
    """
    digest = hashlib.sha256()
    paths = list(ROOT.glob("*.py")) + list((ROOT / "prompts").glob("*.txt"))
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def record_unexpected_error(error: Exception, stage: str) -> str | None:
    """返回供页面显示的错误编号；日志无法写入时返回 None，不掩盖原故障。"""
    error_id = uuid.uuid4().hex[:12]
    frames = []
    current = error
    seen = set()
    # 顺着异常链保留类型和代码位置；不保存任何一层异常的原始 message。
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        positions = []
        trace = current.__traceback__
        while trace is not None:
            source_path = Path(trace.tb_frame.f_code.co_filename).resolve()
            if source_path.is_relative_to(ROOT):
                positions.append({"file": source_path.relative_to(ROOT).as_posix(),
                                  "function": trace.tb_frame.f_code.co_name, "line": trace.tb_lineno})
            trace = trace.tb_next
        frames.append({"type": type(current).__name__, "positions": positions})
        current = current.__cause__ or current.__context__
    record = {"error_id": error_id, "time": timestamp(),
              "stage": stage if stage in STAGES else "初始化生成", "exceptions": frames}
    try:
        LOG_DIR.mkdir(exist_ok=True)
        with (LOG_DIR / "errors.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return error_id

"""查找 Node.js：兼容 Windows 本机和魔搭等 Linux Streamlit 部署环境。

ima 官方接口用 JavaScript 实现，需要 Node.js 执行。Python 的 shutil.which
通常只看当前进程的 PATH（可执行程序搜索目录），不会自动刷新系统配置。
因此：先尊重 PATH，再检查 Windows 常见位置，最后找 pip 安装的 Node.js。
requirements.txt 中的 nodejs-wheel-binaries 会把运行时安装到 Python 包目录，
不一定生成 PATH 中的 node 命令，所以必须支持直接使用包里的可执行文件。
本模块只找程序，不下载软件、不联网、不修改系统 PATH。
"""

from importlib.util import find_spec
from pathlib import Path
import os
import shutil


def find_node() -> str | None:
    """返回可执行文件路径；找不到返回 None，让调用者给出中文提示。

    str | None 表示返回值可能是字符串，也可能为空。每次调用都重新检查，
    不使用缓存，因此安装好后即使原页面进程还在，也有机会找到新程序。
    """
    executable = shutil.which("node")
    if executable:
        return executable

    # LOCALAPPDATA 是当前用户的本地应用目录；ProgramFiles 是系统程序目录。
    # 这里只读取系统变量，不更改它们。某个变量不存在时，跳过对应位置。
    locations = [
        ("LOCALAPPDATA", "Programs/nodejs/node.exe"),
        ("ProgramFiles", "nodejs/node.exe"),
        ("ProgramFiles(x86)", "nodejs/node.exe"),
    ]
    for variable, relative_path in locations:
        base = os.environ.get(variable)
        if base:
            candidate = Path(base) / relative_path
            if candidate.is_file():
                # 后续 subprocess 使用这个绝对位置，不需要通过 shell 拼命令。
                return str(candidate)

    # find_spec 只查询包位置，不执行第三方包里的 Python 函数。
    # 不硬编码 /usr/local/lib/python3.x：云端 Python 版本和虚拟环境位置会变化。
    spec = find_spec("nodejs_wheel")
    if spec and spec.origin:
        package_dir = Path(spec.origin).parent
        # 发行包在 Linux/macOS 使用 bin/node，在 Windows 使用 node.exe。
        # 同时检查两个布局，避免把部署环境假定为用户电脑的 Windows。
        for relative_path in ("bin/node", "node.exe"):
            candidate = package_dir / relative_path
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None

"""诊断配置。默认不联网；添加参数可验证真实接口，不打印凭证。"""

from pathlib import Path
import argparse
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 从 tools 子目录直接运行时，把项目根目录加入 Python 模块搜索路径，
# 这样下面才能 import config/ima_client/llm。parents[1] 是 tools 的上一层。
from config import ROOT, load_settings
from ima_client import ImaClient, ImaError
from llm import LangChainEngine, ModelError
from node_runtime import find_node


def main():
    """读取命令行选项、显示配置状态，成功返回 0，真实接口检查失败返回 1。"""
    # argparse 将 --live-ima 这样的选项解析为 args.live_ima 布尔值。
    # action="store_true"：写了该参数是 True，没写则 False。
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-ima", action="store_true", help="真实验证知识库连接和产品目录")
    parser.add_argument("--live-model", action="store_true", help="真实验证模型接口，会产生少量调用费用")
    args = parser.parse_args()
    settings = load_settings()
    # 默认只检查本地安装和“是否存在配置”；存在并不代表凭证有效。
    # --live-model 会真实调用模型并产生少量费用；--live-ima 只验证读取。
    print("项目目录：", ROOT)
    print("Node.js：", "已安装" if find_node() else "未找到")
    print("官方 ima 脚本：", "已找到" if settings.skill_path.is_file() else "未找到")
    print("ima 凭证：", "已找到" if settings.ima_credentials_available() else "未配置")
    print("模型密钥：", "已配置" if settings.api_key else "未配置，请填写 .env")
    print("模型名称：", settings.model)
    try:
        if args.live_ima:
            client = ImaClient(settings)
            print("知识库读取成功：", client.connect())
            root = client.product_root()
            print("产品目录读取成功：", root.get("title"))
        if args.live_model:
            print(LangChainEngine(settings).ping())
    except (ImaError, ModelError) as exc:
        print("检查未通过：", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    # 直接运行脚本才执行 main；被别的模块 import 时不自动检查。
    # SystemExit 把 main 的 0/1 结果作为程序退出码交给终端或脚本。
    raise SystemExit(main())

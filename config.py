"""配置入口：把 .env 中的文字转换为程序能使用的 Settings 对象。

由 app.py、诊断工具等调用。这里不调用模型、不连接知识库，也不修改 .env。
阅读顺序建议：先看 Settings 里有哪些配置，再看 load_settings 如何填写它们。
"""

# dataclass 会帮我们生成“保存一组配置”的类；field 可以控制默认值和显示方式。
from dataclasses import dataclass, field
# Path 用来组合路径，比自己拼接 Windows 的反斜杠更稳妥。
from pathlib import Path
import os

# python-dotenv 库负责读 KEY=VALUE 格式；dotenv_values 返回字典，不修改系统环境。
from dotenv import dotenv_values

# __file__ 是当前 config.py 的位置；resolve 得到绝对路径，parent 取所在文件夹。
# 因此即使从其他工作目录启动，也会找到本项目的 .env 和 prompts。
ROOT = Path(__file__).resolve().parent
COMPETITION_KB_NAME = "任德耀新建华为PC知识库"


# frozen=True 表示创建后不允许直接改字段，避免流程中意外改变接口配置。
# 冒号后面的 str、int、Path 是类型提示，帮助读代码的人理解数据种类。
@dataclass(frozen=True)
class Settings:
    # repr=False 防止调试打印配置对象时意外输出密钥。
    api_key: str = field(default="", repr=False)
    # base_url 是接口地址；model 是服务商要求的模型 ID，两者要与密钥匹配。
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-pro"
    timeout: int = 90
    # ima 的凭证与 DeepSeek 密钥分开；它们属于两个不同服务。
    ima_client_id: str = field(default="", repr=False)
    ima_api_key: str = field(default="", repr=False)
    kb_name: str = "任德耀新建华为PC知识库"
    skill_path: Path = ROOT / "vendor/ima-skill/ima_api.cjs"
    web_search_provider: str = "auto"
    tavily_api_key: str = field(default="", repr=False)

    def ima_credentials_available(self) -> bool:
        """只检查是否存在凭证，不把内容显示到页面。"""
        home_config = Path.home() / ".config/ima"
        # 满足下面任一项即可认为“找到了配置”：显式填写两项，或本机两个文件都存在。
        # 这里只检查存在性，不保证密钥有效；真实权限要通过 connect() 请求确认。
        return bool(
            (self.ima_client_id and self.ima_api_key)
            or all((home_config / name).is_file() for name in ("client_id", "api_key"))
        )


def load_settings() -> Settings:
    """读取配置并返回对象。调用示例：settings = load_settings()。"""
    values = dotenv_values(ROOT / ".env")

    def value(name: str, default: str = "") -> str:
        # 系统环境变量优先；没有设置时读取本项目 .env。
        return str(os.environ.get(name, values.get(name) or default)).strip()

    # 相对路径以项目根目录为起点；绝对路径则按用户填写的位置使用。
    path = Path(value("IMA_SKILL_PATH", "vendor/ima-skill/ima_api.cjs"))
    return Settings(
        api_key=value("DEEPSEEK_API_KEY") or value("LLM_API_KEY"),
        base_url=value("LLM_BASE_URL", "https://api.deepseek.com"),
        model=value("LLM_MODEL", "deepseek-v4-pro"),
        # .env 读出来是字符串，int 转成整数；max/min 把超时限定在 10～180 秒。
        # 如果填成“九十”，int 会报错，网页会捕获并提醒配置格式有误。
        timeout=max(10, min(180, int(value("LLM_TIMEOUT", "90")))),
        ima_client_id=value("IMA_OPENAPI_CLIENTID"),
        ima_api_key=value("IMA_OPENAPI_APIKEY"),
        kb_name=value("IMA_KB_NAME", "任德耀新建华为PC知识库"),
        skill_path=path if path.is_absolute() else ROOT / path,
        web_search_provider=value("WEB_SEARCH_PROVIDER", "auto").casefold(),
        tavily_api_key=value("TAVILY_API_KEY"),
    )

"""大模型适配层：把“提示词 + 业务数据”变成符合指定模板的结果。

其他模块只调用 LangChainEngine.run，不需要了解 HTTP 请求细节。
这里使用 LangChain 的 ChatOpenAI 对接兼容接口；这不表示实际调用 OpenAI
的模型，真正的服务商由 settings.base_url 和 settings.model 决定。
"""

import json
from urllib.parse import urlparse

from langchain_core.exceptions import OutputParserException
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError
import openai

from config import ROOT, Settings


class ModelError(RuntimeError):
    """供界面捕获的模型调用错误；提示文字由本模块统一整理。"""
    pass


class LangChainEngine:
    """保存模型客户端，并提供连接测试、分阶段生成两个入口。"""

    def __init__(self, settings: Settings):
        # __init__ 在 LangChainEngine(settings) 时自动运行，负责检查和初始化。
        if not settings.api_key:
            raise ModelError("请先在本项目 .env 中填写 DEEPSEEK_API_KEY，保存后刷新页面。")
        if urlparse(settings.base_url).scheme != "https":
            raise ModelError("模型接口地址必须使用 https。")
        # LangChain 的 ChatOpenAI 会把 max_tokens 改名为 max_completion_tokens，
        # DeepSeek 文档使用 max_tokens；通过 extra_body 保留服务商要求的原字段。
        extra_body = {"max_tokens": 6000}
        if urlparse(settings.base_url).hostname == "api.deepseek.com":
            # V4 默认开启思考；首版关闭以降低四阶段工作流的等待时间。
            extra_body["thinking"] = {"type": "disabled"}
        self.model = ChatOpenAI(
            model=settings.model, base_url=settings.base_url, api_key=settings.api_key,
            temperature=0.2, timeout=settings.timeout,
            # 只协商 Python 标准库支持的 gzip/deflate 压缩。
            # 某些 Anaconda 环境预装的 Brotli 解压库与新版 HTTP 客户端不兼容，
            # 响应解压失败会被 SDK 包装成“连接失败”；排除 br 可避免这个问题。
            # 这不改变模型、提示词或输出内容，也没有关闭 HTTPS 证书校验。
            default_headers={"Accept-Encoding": "gzip, deflate"},
            max_retries=0, extra_body=extra_body,
        )
        # temperature 越低通常越稳定，但不保证每次完全相同或事实绝对正确。
        # 关闭 SDK 隐式重试，避免与取证/内容重试相乘。网络故障由用户明确重试。
        self.request_count = 0
        self.max_requests = 16  # 含写前审查、格式重试、一次取证重试和一次改写。

    @staticmethod
    def friendly_error(exc: Exception) -> ModelError:
        """不显示 SDK 原始异常，防止接口地址、请求内容和令牌进入截图。"""
        # staticmethod 表示函数不依赖某个实例（没有 self 参数）。
        # isinstance 用来辨认异常类型；各分支把技术错误转为可操作的中文提示。
        if isinstance(exc, openai.AuthenticationError):
            return ModelError("模型鉴权失败，请检查 .env 中的 API Key 是否属于当前接口服务商。")
        if isinstance(exc, openai.RateLimitError):
            return ModelError("模型接口限流或额度不足，请稍后重试并检查账户额度。")
        if isinstance(exc, openai.NotFoundError):
            return ModelError("模型或接口不存在，请核对 LLM_MODEL 和 LLM_BASE_URL。")
        if isinstance(exc, openai.APIStatusError) and exc.status_code == 402:
            return ModelError("模型账户余额不足，请检查服务商账户。")
        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError)):
            return ModelError("模型连接失败或超时，请检查网络和接口地址后重试。")
        return ModelError("模型调用失败，请核对接口配置、模型权限和余额后重试。")

    def ping(self) -> str:
        """发一条很短的真实请求，验证当前模型权限和连通性，会消耗少量额度。"""
        try:
            self.model.invoke("请只回答：连接成功")
            return "模型接口连接成功"
        except Exception as exc:
            raise self.friendly_error(exc) from exc

    def run(self, stage: str, schema: type[BaseModel], payload: dict) -> BaseModel:
        """调用一个阶段，格式不合格时最多再尝试一次。

        stage：提示词文件名（不含 .txt），例如 strategy/extract/generate/review。
        schema：期望输出的 Pydantic 类，例如 Strategy，而不是某条具体数据。
        payload：本阶段需要的任务、原文、事实等业务数据组成的字典。
        返回：已经通过字段与类型验证的 schema 实例；并不表示事实已核验。
        """
        # ROOT 是项目路径，UTF-8 用于正确读取中文；提示词和 Python 代码分开维护。
        instructions = (ROOT / "prompts" / f"{stage}.txt").read_text(encoding="utf-8")
        prompt = ChatPromptTemplate.from_messages([
            ("system", "{instructions}\n必须只输出符合以下 schema 的 JSON 对象，不要输出解释或代码围栏。\n{schema}"),
            ("human", "{payload}"),
        ])
        # system 消息放规则和输出模板；human 消息放本次任务数据。
        # {instructions}/{schema}/{payload} 是占位符，invoke 时才填入实际值。
        # 这里的 | 是 LangChain 的串接运算：先构造消息，再调用模型并解析输出。
        # json_mode 要求返回 JSON；Pydantic 进一步检查字段、类型和约束。
        temperature = {"strategy": 0.5, "generate": 0.65, "inspire": 0.3}.get(stage, 0.1)
        stage_model = self.model.model_copy(update={"temperature": temperature})
        chain = prompt | stage_model.with_structured_output(schema, method="json_mode")
        for attempt in range(2):
            try:
                if self.request_count >= self.max_requests:
                    raise ModelError("本次生成已达到模型请求上限，请查看问题后再发起新任务。")
                self.request_count += 1
                output = chain.invoke({
                    "instructions": instructions + ("\n上次格式无效，请严格检查JSON字段与类型。" if attempt else ""),
                    # JSON schema 是数据模板的机器可读说明，不是另一个模型。
                    # ensure_ascii=False 保留中文可读性，避免转成一串 Unicode 转义。
                    "schema": json.dumps(schema.model_json_schema(), ensure_ascii=False),
                    "payload": json.dumps(payload, ensure_ascii=False),
                })
                # 部分适配器返回实例，部分返回字典；两种情况统一成同一种返回值。
                return output if isinstance(output, schema) else schema.model_validate(output)
            except (OutputParserException, ValidationError, ValueError) as exc:
                # attempt 第一次为 0、第二次为 1；第二次仍失败才向上抛出。
                # 此处修的是 JSON 格式，不是营销文案的事实问题（后者在 workflow）。
                if attempt:
                    raise ModelError("模型连续两次返回了无效的数据格式，请重试或切换模型。") from exc
            except ModelError:
                raise
            except Exception as exc:
                raise self.friendly_error(exc) from exc
        raise ModelError("模型未返回有效结果。")

"""用本地 HTTP 模拟响应验证实际请求格式和 LangChain 解析，不访问模型接口。"""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI

from config import Settings
from llm import LangChainEngine, ModelError
from schemas import Strategy, FactProposal, SegmentExtraction
from workflow import run_workflow
from test_core import evidence, task, extraction, FakeEngine


def test_deepseek_wire_format_and_json_retry(monkeypatch):
    """经过真实 LangChain/SDK 封装，但用本地响应检查发送字段和格式重试。"""
    # requests 在这里是“收到的请求列表”，不是 ima_client 中 requests 下载库。
    requests = []
    strategy = Strategy(audience_problem="测试需求", angle="测试角度", priority_points=["测试"],
                        structure=["场景", "事实"], tone="简洁", image_count=3, call_to_action="咨询")

    def respond(request):
        # MockTransport 将待发送 HTTP 请求交到这里，不连接 api.deepseek.com。
        # 返回结构模仿兼容接口的 chat.completion，而非直接跳过 LangChain 解析。
        # 每次实际请求都应限制压缩协商，避免服务器选择本机不兼容的 Brotli。
        assert request.headers["accept-encoding"] == "gzip, deflate"
        requests.append(json.loads(request.content))
        # 首次格式无效，验证格式重试后真的能解析为 Pydantic 对象。
        content = "not valid JSON" if len(requests) == 1 else strategy.model_dump_json()
        return httpx.Response(200, json={
            "id": "local-test-response", "object": "chat.completion", "created": 0,
            "model": "deepseek-v4-pro",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        # 注入本地 HTTP 客户端，因此保留 SDK 序列化/解析，却没有真实模型费用。
        monkeypatch.setattr("llm.ChatOpenAI", lambda **kwargs: ChatOpenAI(http_client=client, **kwargs))
        result = LangChainEngine(Settings(api_key="local-test-placeholder")).run("strategy", Strategy, {"task": "测试任务"})

    assert result == strategy
    assert len(requests) == 2
    for request in requests:
        # 逐条核对实际请求 JSON：模型 ID、输出长度、思考开关、JSON 模式。
        assert request["model"] == "deepseek-v4-pro"
        assert request["max_tokens"] == 6000
        assert "max_completion_tokens" not in request
        assert request["thinking"] == {"type": "disabled"}
        assert request["response_format"] == {"type": "json_object"}
        assert "JSON" in request["messages"][0]["content"]
        assert request["temperature"] == 0.5


def test_actual_langchain_pipeline_uses_new_segment_schema(monkeypatch, task, evidence, extraction):
    """真实运行四阶段 LangChain 序列化/解析，仅替换 HTTP，覆盖新提取格式。"""
    requests = []
    fake = FakeEngine(evidence, extraction)
    def respond(request):
        body = json.loads(request.content)
        payload = json.loads(body["messages"][-1]["content"])
        if "audit_locations" in payload:
            stage = "review"
        elif "segments" in payload:
            stage = "extract"
        elif "facts" in payload:
            stage = "generate" if "writing_scope" in payload else "ground"
        else:
            stage = "strategy"
        requests.append(stage)
        if stage == "extract":
            assert "FactProposal" in body["messages"][0]["content"]
            selected = payload["segments"][0]
            value = SegmentExtraction(product_model=task.product_model, facts=[FactProposal(
                claim="显示屏为14.2英寸", source_id=selected["source_id"], segment_id=selected["segment_id"])])
        else:
            value = fake.run(stage, None, payload)
        return httpx.Response(200, json={"id": "mock-completion", "object": "chat.completion", "created": 0,
            "model": "deepseek-v4-pro", "choices": [{"index": 0, "message": {"role": "assistant", "content": value.model_dump_json()}, "finish_reason": "stop"}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr("llm.ChatOpenAI", lambda **kwargs: ChatOpenAI(http_client=client, **kwargs))
        engine = LangChainEngine(Settings(api_key="local-test-placeholder"))
        result = run_workflow(task, evidence, engine)
    assert result.review.passed
    assert requests == ["strategy", "extract", "ground", "generate", "review"]
    assert engine.request_count == 5 and result.facts[0].segment_id == "S1-P001"


def test_request_budget_blocks_format_retry(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "mock", "object": "chat.completion", "created": 0,
            "model": "mock", "choices": [{"index": 0, "message": {"role": "assistant", "content": "invalid JSON"}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr("llm.ChatOpenAI", lambda **kwargs: ChatOpenAI(http_client=client, **kwargs))
        engine = LangChainEngine(Settings(api_key="local-test-placeholder"))
        engine.max_requests = 1
        with pytest.raises(ModelError, match="请求上限"):
            engine.run("strategy", Strategy, {})
    assert len(calls) == 1


def test_network_failure_has_no_hidden_sdk_retry(monkeypatch):
    calls = []
    def respond(request):
        calls.append(request)
        raise httpx.ConnectError("模拟网络故障", request=request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr("llm.ChatOpenAI", lambda **kwargs: ChatOpenAI(http_client=client, **kwargs))
        engine = LangChainEngine(Settings(api_key="local-test-placeholder"))
        with pytest.raises(ModelError, match="连接失败或超时"):
            engine.run("strategy", Strategy, {})
    assert len(calls) == 1

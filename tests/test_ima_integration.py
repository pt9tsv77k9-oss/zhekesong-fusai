"""IMA 集成边界：不访问真实 IMA，也不会在用户账号创建测试笔记。"""

# 大部分测试用 monkeypatch 替换进程返回值，专门检查异常边界。
# 最后两项真的运行 Node，但导入的是临时模拟脚本，而不是联网的官方脚本。

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from config import ROOT, Settings
from ima_client import ImaClient, ImaError
from node_runtime import find_node


@pytest.fixture
def bridge_directory():
    """提供测试临时目录；yield 后的清理由 with 在测试结束时自动完成。"""
    # 本机系统临时目录中有其他用户遗留的 pytest 目录，使用项目缓存隔离夹具。
    cache = ROOT / ".pytest_cache"
    cache.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="ima-bridge-", dir=cache) as directory:
        yield Path(directory)


def test_long_chinese_note_uses_utf8_stdin_not_command_arguments(monkeypatch):
    """长篇中文应完整放进 stdin，命令行不应含正文或凭证。"""
    client = ImaClient(Settings(ima_client_id="test-client", ima_api_key="test-secret"))
    captured = {}

    def run(args, **kwargs):
        captured.update(args=args, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"code":0,"data":{"note_id":"new-note"}}', stderr="")

    monkeypatch.setattr("ima_client.subprocess.run", run)
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "node")
    content = "中文产品资料与营销核验\n" * 10000
    assert client.create_note(content, "营销记录") == "new-note"
    assert json.loads(captured["input"]) == {"content_format": 1, "content": "# 营销记录\n\n" + content}
    assert "中文产品资料" not in repr(captured["args"])
    assert "test-secret" not in repr(captured["args"])
    assert captured["encoding"] == "utf-8"
    assert captured["env"]["IMA_BASE_URL"] == "https://ima.qq.com"


@pytest.mark.parametrize("content,title", [("正文\ud800", "标题"), ("正文", "标题\udfff")])
def test_invalid_utf8_note_never_starts_request(monkeypatch, content, title):
    """正文/标题中的孤立代理字符无法编码，必须在启动子进程前被拒绝。"""
    # pytest.fail 作为替代函数：只要代码错误地启动请求，本项立即失败。
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "node")
    monkeypatch.setattr("ima_client.subprocess.run", lambda *a, **kw: pytest.fail("不得发送非法编码"))
    with pytest.raises(ImaError, match="UTF-8"):
        ImaClient(Settings()).create_note(content, title)


def test_note_write_backend_error_is_not_reported_as_success(monkeypatch):
    """即便 Node 正常退出，只要返回业务错误，就不能声称笔记创建成功。"""
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "node")
    monkeypatch.setattr("ima_client.subprocess.run", lambda *a, **kw: SimpleNamespace(
        returncode=0, stdout='{"code":20004,"msg":"测试鉴权失败"}', stderr=""))
    with pytest.raises(ImaError, match="测试鉴权失败"):
        ImaClient(Settings()).create_note("正文")


def test_note_images_are_not_sent_without_image_preflight(monkeypatch):
    """当前文字笔记入口不接受内嵌图片，应该在调用 API 前拦截。"""
    client = ImaClient(Settings())
    monkeypatch.setattr(client, "call", lambda *a: pytest.fail("未预检的图片不得写入"))
    with pytest.raises(ImaError, match="图片"):
        client.create_note("![图片](https://example.com/image.png)")


def test_note_timeout_does_not_retry_a_possible_success(monkeypatch):
    """模拟超时并统计次数：只能尝试一次，提示先到客户端检查，避免重建。"""
    calls = []

    def timeout(*args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("node", 45)

    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "node")
    monkeypatch.setattr("ima_client.subprocess.run", timeout)
    with pytest.raises(ImaError, match="可能已经创建"):
        ImaClient(Settings()).create_note("正文")
    assert len(calls) == 1


def test_shared_knowledge_note_read_uses_actual_note_identifier(monkeypatch):
    """知识库条目 ID 不等于笔记 ID；读取必须使用元信息中的 notebook_id。"""
    client = ImaClient(Settings())
    monkeypatch.setattr(client, "media_info", lambda _: {
        "media_type": 11, "notebook_ext_info": {"notebook_id": "actual-note"}})
    calls = []

    def call(endpoint, body):
        calls.append((endpoint, body))
        return {"content": "真实正文结构的测试内容"}

    monkeypatch.setattr(client, "call", call)
    assert client.read_text({"media_id": "knowledge-entry"}) == "真实正文结构的测试内容"
    assert calls == [("note/v1/get_doc_content", {"note_id": "actual-note", "target_content_format": 0})]


@pytest.mark.skipif(not find_node(), reason="需要 Node.js 验证进程间 UTF-8 传输")
def test_bridge_passes_large_body_to_skill_without_network(bridge_directory):
    """真实跨 Python/Node 传输长中文与 emoji，验证桥接编码，不访问网络。"""
    # skipif：机器没安装 Node 时跳过这两项，其余离线规则测试仍可运行。
    # stub 的 imaApi 只原样返回参数，用它核对桥接有没有丢字或截断。
    stub = bridge_directory / "ima_stub.cjs"
    stub.write_text("exports.imaApi = async (endpoint, body) => JSON.stringify({endpoint, body});", encoding="utf-8")
    content = "华为笔记🙂\n" * 20000
    response = subprocess.run(
        [find_node(), str(ROOT / "tools/ima_bridge.cjs"), str(stub), "openapi/note/v1/import_doc"],
        input=json.dumps({"content": content}, ensure_ascii=False), encoding="utf-8", capture_output=True,
        timeout=10, check=True,
    )
    assert json.loads(response.stdout) == {"endpoint": "openapi/note/v1/import_doc", "body": {"content": content}}


@pytest.mark.skipif(not find_node(), reason="需要 Node.js 验证脚本错误透传")
def test_bridge_preserves_skill_update_error(bridge_directory):
    """模拟官方脚本抛出版本错误，桥接应保留错误码/提示并以失败码退出。"""
    stub = bridge_directory / "ima_stub.cjs"
    stub.write_text("exports.imaApi = async () => { throw {code: -200, msg: '测试新版本'}; };", encoding="utf-8")
    response = subprocess.run(
        [find_node(), str(ROOT / "tools/ima_bridge.cjs"), str(stub), "openapi/note/v1/import_doc"],
        input="{}", encoding="utf-8", capture_output=True, timeout=10,
    )
    assert response.returncode != 0
    assert json.loads(response.stderr) == {"code": -200, "msg": "测试新版本"}

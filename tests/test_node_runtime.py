"""Node 查找测试：只替换环境和文件检查，不安装软件，也不启动外部进程。"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from node_runtime import find_node


def test_node_from_path_takes_priority(monkeypatch):
    """用户 PATH 已配置 Node 时，优先采用它，不擅自切换到其他安装版本。"""
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: "configured-node")
    assert find_node() == "configured-node"


def test_user_install_is_found_without_refreshed_path(monkeypatch):
    """模拟旧窗口找不到 Node，但用户目录已有安装的情形。"""
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: None)
    base = Path("demo-user-local").resolve()
    expected = base / "Programs/nodejs/node.exe"
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    # 不真正创建 node.exe，只让路径检查对预期位置返回 True。
    monkeypatch.setattr(Path, "is_file", lambda path: path == expected)
    assert find_node() == str(expected)


def test_missing_node_returns_none(monkeypatch):
    """PATH 和所有候选位置都没有 Node 时，返回 None 供页面显示安装提示。"""
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda _: False)
    assert find_node() is None


@pytest.mark.parametrize("relative_path", ["bin/node", "node.exe"])
def test_packaged_node_is_found_without_system_node(monkeypatch, local_tmp_path, relative_path):
    """模拟云端没有系统 Node，只在 Python 包中安装运行时；覆盖两种目录布局。"""
    package = local_tmp_path / "nodejs_wheel"
    expected = package / relative_path
    expected.parent.mkdir(parents=True)
    expected.write_bytes(b"test-only-placeholder")
    expected.chmod(0o755)
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: None)
    for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("node_runtime.find_spec", lambda _: SimpleNamespace(origin=str(package / "__init__.py")))
    assert find_node() == str(expected)


def test_no_system_or_packaged_runtime_returns_none(monkeypatch):
    """依赖没安装时仍返回缺失，不能伪装为已经具备运行环境。"""
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: None)
    monkeypatch.setattr("node_runtime.find_spec", lambda _: None)
    for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    assert find_node() is None


def test_packaged_node_without_execute_permission_is_rejected(monkeypatch, local_tmp_path):
    """Linux 包内文件存在但没有执行权限时，不能当成可用的 Node。"""
    package = local_tmp_path / "nodejs_wheel"
    package.mkdir()
    (package / "bin").mkdir()
    (package / "bin/node").write_bytes(b"test-only-placeholder")
    monkeypatch.setattr("node_runtime.shutil.which", lambda _: None)
    monkeypatch.setattr("node_runtime.find_spec", lambda _: SimpleNamespace(origin=str(package / "__init__.py")))
    monkeypatch.setattr("node_runtime.os.access", lambda *args: False)
    for name in ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)"):
        monkeypatch.delenv(name, raising=False)
    assert find_node() is None

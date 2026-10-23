"""测试共享准备：将临时文件放在项目缓存内，不依赖系统 Temp 的权限。"""

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from config import ROOT


@pytest.fixture
def local_tmp_path():
    """每项测试独占一个临时目录，完成后由标准库清理该目录。

    某些 Windows 安装的用户名与 USERNAME 不同，pytest 默认 Temp 目录可能
    属于别的账号。项目内缓存可写，也不会误操作其他人的系统临时文件。
    """
    cache = (ROOT / ".pytest_cache").resolve()
    cache.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="local-test-", dir=cache) as folder:
        path = Path(folder).resolve()
        assert path.parent == cache
        yield path

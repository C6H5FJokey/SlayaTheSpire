"""spire-agent 测试夹具。

两件事：

1. 把 core 与 agent 的 src、以及 core 的 tests（复用其中手写的观测夹具）都加进
   `sys.path` —— 两个包都是 src-layout 且刻意不要求 `pip install`。
2. 覆盖 `tmp_path`：本沙箱的系统临时目录不可读（`pytest-of-<user>` 建得出来、
   list 不了），所以测试用的临时目录放在仓库内的 `.pytest-tmp/`（已 gitignore）。
"""

import pathlib
import shutil
import sys
import uuid

import pytest

HERE = pathlib.Path(__file__).resolve()
REPO = HERE.parents[3]
PACKAGES = REPO / "packages"
CORE_SRC = PACKAGES / "spire-core" / "src"
CORE_TESTS = PACKAGES / "spire-core" / "tests"
AGENT_SRC = PACKAGES / "spire-agent" / "src"

for path in (CORE_SRC, CORE_TESTS, AGENT_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

TMP_ROOT = REPO / ".pytest-tmp"


@pytest.fixture
def tmp_path():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / uuid.uuid4().hex
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
"""Windows 控制台语义：Ctrl-C 必须真的能中断我们，中文必须不是乱码。

`CREATE_NEW_PROCESS_GROUP`（IDE、终端配置、任务脚本、`start /b` 都用它）会给子进程
留一个**可继承的"忽略 Ctrl-C"标记**，`signal.signal(SIGINT, ...)` 清不掉它。
这个测试把那个场景复现出来，验证 `enable_ctrl_c()` 之后 Ctrl-C 能到达。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from spire_agent.console import enable_ctrl_c, enable_utf8_output

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows 控制台语义")

CHILD = textwrap.dedent(
    """
    import time

    from spire_agent.console import enable_ctrl_c

    enable_ctrl_c()
    print("ready", flush=True)
    try:
        time.sleep(30)
    except KeyboardInterrupt:
        print("interrupted", flush=True)
    """
)


def test_enable_ctrl_c_is_a_noop_off_windows():
    if os.name == "nt":
        assert enable_ctrl_c() is True
    else:
        assert enable_ctrl_c() is False


# 用字符码拼，免得测试文件本身的编码参与进来
_ZH = "".join(chr(c) for c in (0x7B2C, 0x4E00, 0x884C))

CHILD_UTF8 = textwrap.dedent(
    """
    from spire_agent.console import enable_utf8_output

    enable_utf8_output()
    print("".join(chr(c) for c in (0x7B2C, 0x4E00, 0x884C)))
    """
)


def test_utf8_output_works_even_when_stream_encoding_is_gbk(tmp_path: Path):
    """cp936 控制台上 Python 默认用 GBK 编码输出；`enable_utf8_output()` 之后必须是 UTF-8。"""
    import spire_agent

    script = tmp_path / "utf8_child.py"
    script.write_text(CHILD_UTF8, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(spire_agent.__file__).resolve().parents[1])
    env["PYTHONIOENCODING"] = "gbk"

    proc = subprocess.run([sys.executable, str(script)], capture_output=True, env=env)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert proc.stdout.decode("utf-8").strip() == _ZH


def test_ctrl_c_reaches_a_child_in_a_new_process_group(tmp_path: Path):
    import spire_agent

    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(spire_agent.__file__).resolve().parents[1])

    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.CTRL_C_EVENT)
        try:
            out, _ = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            pytest.fail("Ctrl-C 没有送达子进程（继承的忽略标记没被清掉）")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
    assert "interrupted" in out

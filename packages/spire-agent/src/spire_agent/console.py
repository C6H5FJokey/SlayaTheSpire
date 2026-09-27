"""Windows 控制台小工具：让 Ctrl-C 真的能中断我们，让中文别变成乱码。

坑：用 `CREATE_NEW_PROCESS_GROUP` 启动的子进程（IDE、终端配置、任务脚本、`start /b`
等都爱这么干）会带上一个**可继承的"忽略 Ctrl-C"标记**，而 `signal.signal(SIGINT, ...)`
**不会**替你清掉它 —— 表现就是"Ctrl-C 毫无反应，但 Ctrl-Break 管用"。
这里显式清掉，让 Ctrl-C 恢复正常。

第二个坑：Windows 控制台默认用 ANSI 代码页（简中机器上是 cp936），而 Python 3.12
之前不会自动切到 UTF-8，于是我们打印的中文到了终端就是乱码。`enable_utf8_output()`
把控制台输出代码页和 `sys.stdout/stderr` 的编码都尽量切成 UTF-8。

两者都只影响当前进程（外加控制台输出代码页）；非 Windows 上是空操作。
"""

from __future__ import annotations

import os
import sys

__all__ = ["enable_ctrl_c", "enable_utf8_output"]


def enable_ctrl_c() -> bool:
    """清掉继承来的"忽略 Ctrl-C"标记。返回是否（尝试）成功。"""
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return bool(kernel32.SetConsoleCtrlHandler(None, False))
    except Exception:  # pragma: no cover - 只在异常环境下走到
        return False


def enable_utf8_output() -> bool:
    """把标准输出/错误切到 UTF-8。返回是否真的改了编码。"""
    if os.name == "nt":
        try:
            import ctypes

            # 只改输出代码页；动 stdin 的代码页会影响子进程读入，没必要。
            ctypes.WinDLL("kernel32", use_last_error=True).SetConsoleOutputCP(65001)
        except Exception:  # pragma: no cover - 只在异常环境下走到
            pass

    changed = False
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", "") or "").replace("-", "").lower()
        if encoding == "utf8":
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
            changed = True
        except (ValueError, OSError):  # pragma: no cover - 流已关闭等
            pass
    return changed

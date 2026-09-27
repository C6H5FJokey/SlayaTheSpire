#!/usr/bin/env python
"""本地假 Laya 服务：还没有远端 GPU / 还没部署 Laya 时，先把 agent 全链路跑通。

它**不是模型**：每题按 `criteria` 的字典序最小 key 作答（`score` 题固定 `good`），
回答是确定性的，只用来验证 桥接 -> 构题 -> 请求 -> 仲裁 -> 执行 -> 落盘 -> 面板 这条链路。
换成真模型见 [docs/07-laya-contract.md](../docs/07-laya-contract.md)。

用法：
    python tools/fake_laya_server.py                      # 127.0.0.1:8000
    python tools/fake_laya_server.py --port 9000 --api-key test
    python tools/fake_laya_server.py --duration 300       # 跑够 5 分钟自动退出（不依赖 Ctrl-C）

然后把 agent 配置的 `[laya] base_url` 指过来（`api_key` 留空也行，但设了要一致）：
    [laya]
    base_url = "http://127.0.0.1:8000"
    api_key  = ""      # 本服务默认 key 是 selftest；`--api-key ""` 可关掉鉴权

停止方式：Ctrl-C / Ctrl-Break；若终端的 Ctrl-C 不生效（某些终端与后台启动方式会吞掉它），
启动时会打印 PID，用 `Stop-Process -Id <pid>`（或关掉窗口）结束。
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-agent" / "src"))

from spire_agent.console import enable_ctrl_c, enable_utf8_output  # noqa: E402
from spire_agent.fake_laya import FakeLaya  # noqa: E402


def main() -> int:
    enable_ctrl_c()   # 父进程若带"忽略 Ctrl-C"标记，继承下来后 Ctrl-C 会没反应
    enable_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="默认只绑 loopback")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--api-key", default="selftest", help='空字符串 = 不校验 Authorization')
    parser.add_argument("--model", default="english")
    parser.add_argument("--latency-ms", type=int, default=0, help="人为延迟，冒烟超时用")
    parser.add_argument("--duration", type=float, default=0.0,
                        help="0 = 一直跑到 Ctrl-C；>0 = 秒数到点自动退出（脚本/冒烟用）")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        print(f"[fake-laya] 只允许 loopback，忽略 --host {args.host}", file=sys.stderr)
        args.host = "127.0.0.1"

    fake = FakeLaya(
        api_key=args.api_key,
        model=args.model,
        latency_ms=args.latency_ms,
        bind_port=args.port,
    ).start()

    print(f"[fake-laya] listening on {fake.base_url} (ckpt={args.model})")
    print(f"[fake-laya] health: {fake.base_url}/health")
    if args.api_key:
        print(f"[fake-laya] Authorization: Bearer {args.api_key}")
    print(f"[fake-laya] pid={os.getpid()}  Ctrl-C 停止；不生效就用 Stop-Process -Id {os.getpid()}")

    # 信号处理器只置事件、不抛异常：主线程用 0.5s 轮询等待，任何平台上都能及时退出。
    stop = threading.Event()

    def _on_signal(signum, _frame):
        print(f"\n[fake-laya] signal {signum} -> shutdown", flush=True)
        stop.set()

    for name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError, RuntimeError):
            pass

    deadline = time.monotonic() + args.duration if args.duration > 0 else None
    try:
        while not stop.wait(0.5):
            if deadline is not None and time.monotonic() >= deadline:
                print(f"[fake-laya] duration {args.duration}s reached", flush=True)
                break
    except KeyboardInterrupt:      # 万一信号处理器没装上（例如非主线程/受限环境）
        print("\n[fake-laya] keyboard interrupt", flush=True)
    finally:
        try:
            fake.stop()
        except Exception as exc:   # 清理失败也不该让进程留在前台
            print(f"[fake-laya] stop() failed: {exc}", file=sys.stderr)
    print("[fake-laya] bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""假 Laya：stdlib HTTP 服务，给单测与 `tools/laya_health.py --selftest` 用。

刻意只用标准库（本机/CI 没有 fastapi、也没有网络装依赖），并且**回答是确定性的**：
每题取 `criteria` 的字典序最小 key（`score` 题固定给 "good"），这样测试可以断言
"给定构题 -> 给定动作"的完整链路，而不用 mock 猜测。

它还会记录每个请求，方便断言"缓存生效时只打了一次"这类行为。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_LEVEL = "good"


def _candidate_keys(q: dict[str, Any]) -> list[str]:
    """choice 题的 key 空间：`criteria` 可以是 {key: 描述} 或 [key]，也兼容 `options`。"""
    criteria = q.get("criteria")
    if isinstance(criteria, dict) and criteria:
        return sorted(criteria.keys())
    if isinstance(criteria, list) and criteria:
        return [str(k) for k in criteria]
    options = q.get("options")
    if isinstance(options, list) and options:
        return [str(o) for o in options]
    return []


def answer_questions(questions: dict[str, Any]) -> dict[str, Any]:
    """确定性地回答一类构题（纯函数，单测可直接用）。

    返回形态与真实 Laya 一致（见 `docs/07-laya-contract.md`）：每题一个对象，含
    `type`、题型对应的答案键（`choice` / `score` / `noul`），以及
    `probabilities` / `confidence` / `answer_confidence`。假服务必须与真服务同形，
    否则测试全绿而真机失败 —— 这正是最初踩过的坑。
    """
    answers: dict[str, Any] = {}
    for qid, q in (questions or {}).items():
        if not isinstance(q, dict):
            continue
        qtype = q.get("type")
        if qtype == "score":
            # score 题的等级表在线上是 `criteria`（有序列表），也兼容 `levels` 写法
            levels = q.get("levels") or q.get("criteria") or []
            if not isinstance(levels, list) or not levels:
                continue
            best = levels.index(DEFAULT_LEVEL) if DEFAULT_LEVEL in levels else len(levels) - 1
            answers[qid] = {
                "type": "score",
                "score": float(best),
                "legend": {str(i): str(c) for i, c in enumerate(levels)},
                "probabilities": {str(i): (1.0 if i == best else 0.0) for i in range(len(levels))},
                "confidence": 1.0,
                "answer_confidence": 1.0,
                "action": {"act_probability": 1.0},
            }
            continue
        if qtype == "noul":
            answers[qid] = {
                "type": "noul",
                "noul": 1.0,
                "confidence": 1.0,
                "answer_confidence": 1.0,
                "action": {"act_probability": 1.0},
            }
            continue
        keys = _candidate_keys(q)
        if not keys:
            continue
        chosen = keys[0]
        answers[qid] = {
            "type": "choice",
            "choice": chosen,
            "probabilities": {k: (1.0 if k == chosen else 0.0) for k in keys},
            "confidence": 1.0,
            "answer_confidence": 1.0,
            "action": {"act_probability": 1.0},
        }
    return answers


@dataclass
class FakeLayaRecord:
    body: dict[str, Any]
    headers: dict[str, str]


@dataclass
class FakeLaya:
    """`start()` 后监听 `127.0.0.1:<bind_port>`（只绑 loopback；`0` 取随机端口）。"""

    api_key: str = "selftest"
    model: str = "english"
    fail_times: int = 0           # 前 N 次返回 503，用来测重试
    latency_ms: int = 0
    # 0 = 随机（单测用）；固定端口给 `tools/fake_laya_server.py`。只读的 `port` 属性是实际端口。
    bind_port: int = 0
    requests: list[FakeLayaRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._remaining_failures = self.fail_times

    # ------------------------------------------------------------ 生命周期

    def start(self) -> "FakeLaya":
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", self.bind_port), handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def port(self) -> int:
        assert self._server is not None, "FakeLaya.start() first"
        return self._server.server_address[1]

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeLaya":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    # ------------------------------------------------------------ 内部

    def _make_handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # 静默
                return

            def _send(self, status: int, body: dict[str, Any]) -> None:
                raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:  # noqa: N802
                if self.path.startswith("/health"):
                    self._send(200, {"status": "ok", "loaded": [fake.model], "device": "cpu"})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    self._send(400, {"error": "bad json"})
                    return
                headers = {k.lower(): v for k, v in self.headers.items()}
                fake.requests.append(FakeLayaRecord(body=body, headers=headers))

                expected = f"Bearer {fake.api_key}"
                if fake.api_key and headers.get("authorization") != expected:
                    self._send(401, {"error": "unauthorized"})
                    return
                if fake._remaining_failures > 0:
                    fake._remaining_failures -= 1
                    self._send(503, {"error": "temporarily unavailable"})
                    return

                questions = body.get("questions") or {}
                self._send(
                    200,
                    {
                        # 与真实 Laya 一致：顶层 `model` 是架构名，checkpoint 在 routing 里
                        "model": "laya-rl-agent",
                        "answers": answer_questions(questions),
                        "usage": {
                            "input_tokens": len(json.dumps(body, ensure_ascii=False)) // 4,
                            "output_tokens": 0,
                        },
                        "routing": {
                            "model": body.get("model") or fake.model,
                            "reason": "explicit" if body.get("model") else "auto",
                        },
                    },
                )

        return Handler

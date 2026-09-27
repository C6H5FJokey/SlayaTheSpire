"""桥接层的真 socket 往返：握手 / 动作应答 / 心跳 / 断帧恢复 / 协议不符拒绝。

用 loopback 上的假 mod 服务跑一次真 TCP —— `parse_line` 单测证明不了解码器以外的
东西（读线程、分帧、重连、`seq` 缺口统计），这一层值得真跑。
"""

import json
import socket
import threading
import time

import pytest

from spire_agent.bridge import (
    FRAME_ACTION,
    FRAME_ACTION_RESULT,
    FRAME_CONFIGURE,
    FRAME_OBSERVATION,
    IncompatibleMod,
    ModBridge,
)
from spire_core.config import ModConfig


class FakeMod:
    """接受一个连接、立刻发 hello，然后按行读 NDJSON 并自动应答。

    协议 v2：agent 收到 hello 后**必须**发 `configure`，模组回 `configured` 回执。
    """

    def __init__(self, *, protocol: int = 2, reject_configure: bool = False) -> None:
        self.protocol = protocol
        self.reject_configure = reject_configure
        self.received: list[dict] = []
        self.auto_ok = True
        self._stop = threading.Event()
        self._server = socket.socket()
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._conn: socket.socket | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "FakeMod":
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        for sock in (self._conn, self._server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    # ------------------------------------------------------------ 发送

    def _send(self, obj: dict) -> None:
        line = (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            if self._conn is not None:
                self._conn.sendall(line)

    def send_raw_line(self, text: str) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.sendall((text + "\n").encode("utf-8"))

    def send_observation(self, seq: int, raw: dict) -> None:
        self._send({"v": 2, "type": "observation", "id": 0, "reply_to": None,
                    "payload": {"seq": seq, "raw": raw}})

    def send_action_result(self, reply_to: int, ok: bool) -> None:
        self._send({"v": 2, "type": "action_result", "id": 0, "reply_to": reply_to,
                    "payload": {"ok": ok, "code": None, "error": None}})

    def _send_hello(self) -> None:
        self._send({
            "v": 2, "type": "hello", "id": 0, "reply_to": None,
            "payload": {"protocol": self.protocol, "game_version": "2.3.4",
                        "mod_version": "0.1.0", "configured": False,
                        "capabilities": ["observe", "act", "human_action", "watchdog",
                                         "configure"]},
        })

    # ------------------------------------------------------------ 服务循环

    def _serve(self) -> None:
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        self._conn = conn
        conn.settimeout(0.2)
        self._send_hello()
        buffer = b""
        while not self._stop.is_set():
            try:
                chunk = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = rest
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                self.received.append(msg)
                self._on_message(msg)

    def _on_message(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "ping":
            self._send({"v": 2, "type": "pong", "id": 0, "reply_to": msg.get("id"),
                        "payload": {}})
        elif kind == "configure":
            payload = msg.get("payload") or {}
            if self.reject_configure:
                self._send({"v": 2, "type": "configured", "id": 0, "reply_to": msg.get("id"),
                            "payload": {"ok": False, "error": "unknown mode: bogus"}})
            else:
                self._send({"v": 2, "type": "configured", "id": 0, "reply_to": msg.get("id"),
                            "payload": {"ok": True, "mode": payload.get("mode"),
                                        "watchdog_sec": payload.get("watchdog_sec", 30)}})
        elif kind == "action" and self.auto_ok:
            self.send_action_result(msg.get("id"), True)


def make_bridge(mod: FakeMod, *, mode: str = "agent", watchdog_sec: int = 30,
                **overrides) -> ModBridge:
    config = ModConfig(host="127.0.0.1", port=mod.port)
    for key, value in overrides.items():
        setattr(config, key, value)
    return ModBridge(config, mode=mode, watchdog_sec=watchdog_sec)


def recv_until(bridge: ModBridge, msg_type: str, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = bridge.recv(timeout=0.2)
        if msg is not None and msg.type == msg_type:
            return msg
    return None


def test_handshake_and_action_round_trip():
    with FakeMod() as mod:
        bridge = make_bridge(mod, connect_timeout_sec=8.0)
        try:
            assert bridge.connect(retry=False) is True
            assert bridge.hello["game_version"] == "2.3.4"
            assert bridge.hello["protocol"] == 2
            assert bridge.stats["connects"] == 1

            message_id = bridge.send_action(seq=7, kind="end_turn", args={})
            assert message_id >= 0

            result = recv_until(bridge, FRAME_ACTION_RESULT)
            assert result is not None
            assert result.reply_to == message_id
            assert result.payload["ok"] is True
            assert bridge.stats["actions_sent"] == 1

            actions = [m for m in mod.received if m.get("type") == FRAME_ACTION]
            assert len(actions) == 1
            assert actions[0]["payload"] == {"seq": 7, "kind": "end_turn", "args": {}}
        finally:
            bridge.close()


def test_observations_are_delivered_in_order_with_gap_counting():
    with FakeMod() as mod:
        bridge = make_bridge(mod, connect_timeout_sec=8.0)
        try:
            assert bridge.connect(retry=False) is True

            mod.send_observation(1, {"screen": "NONE"})
            first = recv_until(bridge, FRAME_OBSERVATION)
            assert first is not None
            assert first.payload["seq"] == 1
            assert first.payload["raw"] == {"screen": "NONE"}

            mod.send_observation(5, {"screen": "MAP"})
            second = recv_until(bridge, FRAME_OBSERVATION)
            assert second is not None and second.payload["seq"] == 5

            assert bridge.last_observation_seq == 5
            assert bridge.stats["observation_gaps"] == 1
            assert bridge.stats["frames_dropped"] == 0
        finally:
            bridge.close()


def test_malformed_frame_is_dropped_and_the_stream_recovers():
    with FakeMod() as mod:
        bridge = make_bridge(mod, connect_timeout_sec=8.0)
        try:
            assert bridge.connect(retry=False) is True

            mod.send_raw_line("{ this is not json")
            mod.send_observation(2, {"screen": "NONE"})

            msg = recv_until(bridge, FRAME_OBSERVATION)
            assert msg is not None and msg.payload["seq"] == 2
            assert bridge.stats["frames_dropped"] == 1
        finally:
            bridge.close()


def test_heartbeat_ping_is_answered_by_pong():
    with FakeMod() as mod:
        bridge = make_bridge(mod, connect_timeout_sec=8.0, heartbeat_sec=0.05)
        try:
            assert bridge.connect(retry=False) is True
            deadline = time.time() + 8.0
            while bridge.stats["pongs"] == 0 and time.time() < deadline:
                bridge.recv(timeout=0.2)
            assert bridge.stats["pings"] >= 1
            assert bridge.stats["pongs"] >= 1
            assert bridge.connected is True
        finally:
            bridge.close()


def test_configure_is_pushed_right_after_hello():
    """模组没有自己的模式：握手成功后第一件事就是把 mode/watchdog 推过去。"""
    with FakeMod() as mod:
        bridge = make_bridge(mod, mode="observe_human", watchdog_sec=45,
                             connect_timeout_sec=8.0)
        try:
            assert bridge.connect(retry=False) is True
            assert bridge.configured == {"mode": "observe_human", "watchdog_sec": 45}
            # 心跳 ping 是 recv 的副作用，会排在前面；configure 仍须是最早的语义帧。
            sent = [m["type"] for m in mod.received if m["type"] != "ping"]
            assert sent[0] == FRAME_CONFIGURE
            frame = next(m for m in mod.received if m["type"] == FRAME_CONFIGURE)
            assert frame["payload"] == {"mode": "observe_human", "watchdog_sec": 45}
        finally:
            bridge.close()


def test_protocol_mismatch_is_refused_without_degrading():
    with FakeMod(protocol=1) as mod:
        bridge = make_bridge(mod, connect_timeout_sec=4.0)
        try:
            with pytest.raises(IncompatibleMod) as excinfo:
                bridge.connect(retry=False)
            assert "protocol 1" in str(excinfo.value)
            assert bridge.connected is False
            assert bridge.hello == {}
        finally:
            bridge.close()


def test_a_rejected_configure_aborts_the_connection():
    """模组拒配 = 跑下去也是空转，直接冒泡（不参与重连）。"""
    with FakeMod(reject_configure=True) as mod:
        bridge = make_bridge(mod, connect_timeout_sec=4.0)
        try:
            with pytest.raises(IncompatibleMod) as excinfo:
                bridge.connect(retry=False)
            assert "rejected configure" in str(excinfo.value)
            assert bridge.configured == {}
        finally:
            bridge.close()


def test_send_action_without_a_connection_returns_minus_one():
    with FakeMod() as mod:
        bridge = make_bridge(mod, connect_timeout_sec=4.0)
        try:
            assert bridge.send_action(seq=1, kind="end_turn", args={}) == -1
        finally:
            bridge.close()

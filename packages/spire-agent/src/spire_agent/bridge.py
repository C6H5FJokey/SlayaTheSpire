"""模组桥接：TCP + NDJSON 客户端（见 docs/03-mod-protocol.md）。

职责边界：
- 只管"把行收发好"：重连、心跳、分帧、协议版本校验、seq 单调性检查；
- **不懂语义**：不解析观测、不构造动作（那是 core 与 runner 的事）。

关键设计：读线程只做"读一行 -> 塞队列"，绝不在读线程里做网络写；
写走锁保护的 `send`。这样 Laya 慢也不会把 mod 的读缓冲憋爆。
"""

from __future__ import annotations

import json
import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from spire_core.config import MODE_AGENT, ModConfig

log = logging.getLogger(__name__)

PROTOCOL = 2
FRAME_HELLO = "hello"
FRAME_CONFIGURE = "configure"
FRAME_CONFIGURED = "configured"
FRAME_OBSERVATION = "observation"
FRAME_HUMAN_ACTION = "human_action"
FRAME_ACTION = "action"
FRAME_ACTION_RESULT = "action_result"
FRAME_PING = "ping"
FRAME_PONG = "pong"


class ProtocolError(RuntimeError):
    """协议级错误：版本不符、hello 缺失等。**不做降级兼容。**"""


class IncompatibleMod(ProtocolError):
    """模组与 agent 对不上话（协议版本不符 / 拒绝 configure）。

    这类错误重试一万次也一样，所以不参与指数退避重连，直接冒泡到 `run()` 停跑。
    绝大多数情况下意味着"装的是旧 jar"，重新构建安装即可。
    """


@dataclass
class Message:
    type: str
    id: int
    payload: dict[str, Any] = field(default_factory=dict)
    reply_to: int | None = None

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Message":
        return Message(
            type=str(raw.get("type", "")),
            id=int(raw.get("id", 0) or 0),
            payload=raw.get("payload") or {},
            reply_to=raw.get("reply_to"),
        )

    def to_line(self, protocol: int = PROTOCOL) -> str:
        body = {
            "v": protocol,
            "type": self.type,
            "id": self.id,
            "reply_to": self.reply_to,
            "payload": self.payload,
        }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def parse_line(line: str) -> Message | None:
    """解析一行 NDJSON。畸形输入返回 None（调用方记日志并跳过）。"""
    line = line.strip()
    if not line:
        return None
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    if raw.get("type") is None:
        return None
    return Message.from_dict(raw)


def backoff_delay(attempt: int, *, base: float = 1.0, cap: float = 30.0) -> float:
    """指数退避（纯函数，便于单测）：1,2,4,8,... 但不超过 cap。"""
    if attempt < 1:
        attempt = 1
    return min(cap, base * (2 ** (attempt - 1)))


class ModBridge:
    """一个可反复重连的 mod 连接。

    典型用法：

        bridge = ModBridge(config)
        bridge.connect()                      # 阻塞直到 hello 拿到
        while True:
            msg = bridge.recv(timeout=0.2)
            if msg is None: continue
            ...
            bridge.send_action(seq=msg.payload["seq"], kind="end_turn", args={})
    """

    def __init__(
        self,
        config: ModConfig,
        *,
        mode: str = MODE_AGENT,
        watchdog_sec: int = 30,
    ) -> None:
        self.config = config
        self.mode = mode
        self.watchdog_sec = int(watchdog_sec)
        self.hello: dict[str, Any] = {}
        self.configured: dict[str, Any] = {}
        self.last_observation_seq: int = -1
        self.stats: dict[str, int] = {
            "connects": 0,
            "disconnects": 0,
            "frames_in": 0,
            "frames_dropped": 0,
            "observation_gaps": 0,
            "pings": 0,
            "pongs": 0,
            "actions_sent": 0,
        }
        self._sock: socket.socket | None = None
        self._inbox: queue.Queue[Message] = queue.Queue(maxsize=4096)
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._pending_pings: dict[int, float] = {}
        self._missed_pongs = 0

    # ------------------------------------------------------------ 连接

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self, *, retry: bool = True) -> bool:
        """连上并完成 hello 握手。失败时按指数退避重试（`retry=False` 只试一次）。"""
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            # 重连时先把上一根连接收干净：`_open()` 会直接覆盖 `self._sock`，不先关就漏一个
            # fd。正常断线（对端关闭 / 读线程出错）时 `_close()` 已经跑过，这里是空操作。
            self._close()
            try:
                self._open()
                if self._handshake():
                    self.stats["connects"] += 1
                    return True
            except IncompatibleMod:
                # 重试解决不了（旧 jar / 拒配），交给 run() 停跑并说人话。
                self._close()
                raise
            except (OSError, ProtocolError) as exc:
                log.warning("connect attempt %d failed: %s", attempt, exc)
                self._close()
            if not retry:
                return False
            delay = backoff_delay(
                attempt,
                base=self.config.reconnect_min_sec,
                cap=self.config.reconnect_max_sec,
            )
            log.info("reconnecting in %.1fs", delay)
            if self._stop.wait(delay):
                return False
        return False

    def _open(self) -> None:
        sock = socket.create_connection(
            (self.config.host, self.config.port),
            timeout=self.config.connect_timeout_sec,
        )
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.settimeout(0.5)
        self._sock = sock
        self._inbox = queue.Queue(maxsize=4096)
        self._pending_pings.clear()
        self._missed_pongs = 0
        self._reader = threading.Thread(target=self._read_loop, args=(sock,), daemon=True)
        self._reader.start()

    def _handshake(self) -> bool:
        deadline = time.monotonic() + self.config.connect_timeout_sec
        while time.monotonic() < deadline:
            msg = self.recv(timeout=0.2)
            if msg is None:
                continue
            if msg.type != FRAME_HELLO:
                log.warning("expected hello first, got %s", msg.type)
                continue
            protocol = int(msg.payload.get("protocol", 0) or 0)
            if protocol != PROTOCOL:
                raise IncompatibleMod(
                    f"mod speaks protocol {protocol}, agent speaks {PROTOCOL}; "
                    "重新构建并安装模组（tools/build_mod.ps1 -> tools/install_mod.ps1）后重启游戏"
                )
            self.hello = dict(msg.payload)
            log.info(
                "hello: game=%s mod=%s capabilities=%s",
                msg.payload.get("game_version"),
                msg.payload.get("mod_version"),
                msg.payload.get("capabilities"),
            )
            return self._configure()
        return False

    def _configure(self) -> bool:
        """握手后立刻把 `mode` / `watchdog_sec` 推给模组。

        模组没有自己的模式：在那之前它不发观测、不接管、不报人类动作。所以这一步
        失败就不能继续跑 —— 否则 agent 会对着一个哑模组空转。
        """
        msg_id = self._next_id
        self._next_id += 1
        msg = Message(
            type=FRAME_CONFIGURE,
            id=msg_id,
            payload={"mode": self.mode, "watchdog_sec": self.watchdog_sec},
        )
        if not self.send(msg):
            return False
        deadline = time.monotonic() + self.config.connect_timeout_sec
        while time.monotonic() < deadline and not self._stop.is_set():
            ack = self.recv(timeout=0.2)
            if ack is None:
                continue
            if ack.type == FRAME_PONG:
                # 心跳是 recv 的副作用，配置期间收到 pong 很正常，不算乱序。
                continue
            if ack.type != FRAME_CONFIGURED or ack.reply_to != msg_id:
                # 正常顺序下 configure 的 ack 一定先于任何观测；乱序只记日志。
                log.warning("unexpected frame while configuring: %s", ack.type)
                continue
            if not ack.payload.get("ok"):
                raise IncompatibleMod(f"mod rejected configure: {ack.payload.get('error')}")
            self.configured = {
                "mode": str(ack.payload.get("mode", self.mode)),
                "watchdog_sec": int(ack.payload.get("watchdog_sec", self.watchdog_sec) or 0),
            }
            log.info(
                "mod configured: mode=%s watchdog_sec=%s",
                self.configured["mode"],
                self.configured["watchdog_sec"],
            )
            return True
        raise IncompatibleMod("mod did not acknowledge configure within "
                              f"{self.config.connect_timeout_sec}s")

    def close(self) -> None:
        self._stop.set()
        self._close()

    def _close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            self.stats["disconnects"] += 1

    # ------------------------------------------------------------ 读

    def recv(self, timeout: float = 0.0) -> Message | None:
        """取一条消息；`timeout=0` 表示不等待。超时返回 None。"""
        self._maybe_ping()
        try:
            msg = self._inbox.get(timeout=timeout if timeout > 0 else None)
        except queue.Empty:
            return None
        return self._after_receive(msg)

    def _after_receive(self, msg: Message) -> Message:
        self.stats["frames_in"] += 1
        if msg.type == FRAME_PONG:
            self.stats["pongs"] += 1
            self._pending_pings.pop(msg.reply_to or 0, None)
            self._missed_pongs = 0
        elif msg.type == FRAME_OBSERVATION:
            seq = int(msg.payload.get("seq", -1) or -1)
            if self.last_observation_seq >= 0 and seq > self.last_observation_seq + 1:
                self.stats["observation_gaps"] += 1
                log.warning(
                    "observation gap: %s -> %s (missing %d)",
                    self.last_observation_seq,
                    seq,
                    seq - self.last_observation_seq - 1,
                )
            self.last_observation_seq = max(self.last_observation_seq, seq)
        return msg

    def _read_loop(self, sock: socket.socket) -> None:
        buffer = bytearray()
        limit = self.config.max_frame_bytes
        while not self._stop.is_set():
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                log.info("read loop ended: %s", exc)
                break
            if not chunk:
                log.info("mod closed the connection")
                break
            buffer.extend(chunk)
            if len(buffer) > limit and b"\n" not in buffer:
                log.error("frame exceeds %d bytes; dropping connection", limit)
                break
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                text = line.decode("utf-8", errors="replace")
                msg = parse_line(text)
                if msg is None:
                    self.stats["frames_dropped"] += 1
                    log.warning("dropped malformed frame (%d bytes)", len(line))
                    continue
                try:
                    self._inbox.put_nowait(msg)
                except queue.Full:
                    self.stats["frames_dropped"] += 1
                    log.error("inbox full; dropping %s", msg.type)
        self._close()

    # ------------------------------------------------------------ 写

    def send(self, msg: Message) -> bool:
        sock = self._sock
        if sock is None:
            return False
        with self._write_lock:
            try:
                sock.sendall(msg.to_line().encode("utf-8") + b"\n")
            except OSError as exc:
                log.warning("send failed: %s", exc)
                self._close()
                return False
        return True

    def send_action(self, *, seq: int, kind: str, args: dict[str, Any]) -> int:
        """发一个动作。返回该消息的 id（agent 侧据此匹配 action_result）。"""
        msg_id = self._next_id
        self._next_id += 1
        msg = Message(
            type=FRAME_ACTION,
            id=msg_id,
            payload={"seq": seq, "kind": kind, "args": args or {}},
        )
        if not self.send(msg):
            return -1
        self.stats["actions_sent"] += 1
        return msg_id

    # ------------------------------------------------------------ 心跳

    def _maybe_ping(self) -> None:
        """每 `heartbeat_sec` ping 一次；连续 3 次没 pong 就断开重连。"""
        if self._sock is None:
            return
        now = time.monotonic()
        last = max(self._pending_pings.values()) if self._pending_pings else None
        if last is not None and now - last < self.config.heartbeat_sec:
            return
        if len(self._pending_pings) >= 3:
            log.warning("no pong after 3 pings; dropping connection")
            self._missed_pongs = len(self._pending_pings)
            self._close()
            return
        msg_id = self._next_id
        self._next_id += 1
        self._pending_pings[msg_id] = now
        self.stats["pings"] += 1
        self.send(Message(type=FRAME_PING, id=msg_id, payload={}))

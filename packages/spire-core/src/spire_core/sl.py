"""SL（save-scum）检测：纯逻辑。

作用域是**当前房间节点**：STS 的自动存档发生在进入房间时，读档会退回到
当前房间开头（战斗从头开始）。因此交易边界取"当前房间的所有决策"
（见 docs/08-dataset.md#sl-语义）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

EVENT_NEW_ROOM = "new_room"
EVENT_SAME_ROOM = "same_room"
EVENT_REWIND = "rewind"


@dataclass(frozen=True)
class RoomToken:
    """房间的唯一标识。注意：**不含 run_seed 的哈希**，因为它本身就是标识。"""

    act: int
    floor: int
    node: int

    @property
    def key(self) -> tuple[int, int, int]:
        return (self.act, self.floor, self.node)

    @property
    def is_room(self) -> bool:
        """是不是**局内**的房间。

        `act`/`floor` 都为 0 的观测是局外：主菜单、选角色、结算画面、刚启动还没
        进图。这类 token 在一局里会反复出现（回主菜单再来一局就是），所以绝不能
        进 `_seen` —— 否则"新开一局又看到 (0,0,0)"会被当成"重访节点"从而误判读档。
        真机上就是这样误报了 9 次（见 docs/08-dataset.md#sl-语义）。
        """
        return self.act >= 1 and self.floor >= 1

    def mark_seen(self, seen: set[tuple[int, int, int]]) -> None:
        """只有局内的房间才进"见过的节点"集合，理由见 `is_room`。"""
        if self.is_room:
            seen.add(self.key)


@dataclass(frozen=True)
class Snapshot:
    """房间入口时刻的状态快照，用于兜底的"状态回退"检测。"""

    turn: int
    player_hp: int
    player_block: int
    monster_hp: tuple[int, ...]
    in_combat: bool = False

    @staticmethod
    def from_observation(raw: dict[str, Any]) -> "Snapshot":
        player = raw.get("player") or {}
        combat = raw.get("combat") or {}
        monsters = combat.get("monsters") or []
        return Snapshot(
            turn=int(combat.get("turn", 0) or 0),
            player_hp=int(player.get("hp", 0) or 0),
            player_block=int(player.get("block", 0) or 0),
            monster_hp=tuple(int(m.get("hp", 0) or 0) for m in monsters),
            in_combat=bool(raw.get("in_combat", False)),
        )


@dataclass
class RoomEvent:
    kind: str
    token: RoomToken
    combat_instance: int
    post_sl: bool
    room_key: str
    detail: str = ""


def _regressed(prev: Snapshot, now: Snapshot) -> str | None:
    """判断状态是否相对**上一次观测**回退。返回原因（None 表示没回退）。

    **只认一个信号：战斗中的回合数倒退。**

    这里踩过坑，写下来免得再犯：`combat.turn` 会在**战斗刚结束的那一帧**被游戏
    重置回 1（紧随其后就是奖励界面），所以裸比较 `turn` 会把"刚好打完一场"
    误判成"读档"。两个闸门缺一不可：前后两次观测都 `in_combat`，且当前快照里
    还有活着的怪（战斗没结束）。

    曾经还比较过玩家 HP 与怪物 HP，两个在实战里都不成立：

    - 战斗内治疗（血瓶 / Regen / 战后回血遗物）会让玩家 HP 合法上升；
    - 召唤类敌人（Gremlin Leader / Reptomancer）会让怪物血量上升，
      按位置比较也会被插入的召唤物打乱。

    所以"状态回退"只能当**兜底**：主信号是模组 hook 的读档入口
    （`CardCrawlGame.loadPlayerSave`），它一响就直接回滚，不需要猜。
    """
    if not (prev.in_combat and now.in_combat):
        return None
    if not any(hp > 0 for hp in now.monster_hp):
        return None
    if now.turn < prev.turn:
        return f"turn {now.turn} < {prev.turn}"
    return None


class RoomTracker:
    """跨观测跟踪房间，检测重进（= 读档）。

    刻意做成无 IO 的纯状态机，便于单测构造"记录 -> 回退 -> 重记"序列
    （见 docs/11-testing.md#sl-回滚）。

    生命周期是**一个游戏会话**：游戏重启后必须换一个新的 tracker（runner 在观测
    `seq` 归零时负责），否则"重开一局又走到同一个节点"会被 `_seen` 当成重访节点。
    """

    def __init__(self) -> None:
        self._seen: set[tuple[int, int, int]] = set()
        self._current: RoomToken | None = None
        self._entry: Snapshot | None = None
        self._last: Snapshot | None = None
        self._combat_instance = 0
        self._post_sl = False
        self.events: list[RoomEvent] = []

    @property
    def combat_instance(self) -> int:
        return self._combat_instance

    @property
    def post_sl(self) -> bool:
        return self._post_sl

    @property
    def current(self) -> RoomToken | None:
        return self._current

    def make_key(self, token: RoomToken) -> str:
        from .dataset import room_key

        return room_key(token.act, token.floor, token.node, self._combat_instance)

    def observe(self, token: RoomToken, snapshot: Snapshot) -> RoomEvent:
        """喂入一次观测，返回发生了什么。"""
        event = self._observe(token, snapshot)
        self.events.append(event)
        return event

    def _observe(self, token: RoomToken, snapshot: Snapshot) -> RoomEvent:
        if self._current is None:
            return self._enter(token, snapshot, EVENT_NEW_ROOM, "first room")

        if token.key == self._current.key:
            assert self._entry is not None
            reason = _regressed(self._last or self._entry, snapshot)
            if reason is not None:
                return self._rewind(token, snapshot, reason)
            self._last = snapshot
            return RoomEvent(
                kind=EVENT_SAME_ROOM,
                token=token,
                combat_instance=self._combat_instance,
                post_sl=self._post_sl,
                room_key=self.make_key(token),
            )

        if token.key in self._seen:
            # 节点不会被重新访问，所以"回到见过的节点"只可能是读档
            return self._rewind(token, snapshot, f"revisited node {token.key}")

        return self._enter(token, snapshot, EVENT_NEW_ROOM, "next room")

    def _enter(
        self, token: RoomToken, snapshot: Snapshot, kind: str, detail: str
    ) -> RoomEvent:
        self._current = token
        self._entry = snapshot
        self._last = snapshot
        token.mark_seen(self._seen)
        self._combat_instance = 1
        self._post_sl = False
        return RoomEvent(
            kind=kind,
            token=token,
            combat_instance=self._combat_instance,
            post_sl=self._post_sl,
            room_key=self.make_key(token),
            detail=detail,
        )

    def _rewind(
        self, token: RoomToken, snapshot: Snapshot, reason: str
    ) -> RoomEvent:
        """读档：当前房间的 pending 必须整体丢弃，并从头重记。"""
        self._current = token
        self._entry = snapshot
        self._last = snapshot
        token.mark_seen(self._seen)
        self._combat_instance += 1
        self._post_sl = True
        return RoomEvent(
            kind=EVENT_REWIND,
            token=token,
            combat_instance=self._combat_instance,
            post_sl=self._post_sl,
            room_key=self.make_key(token),
            detail=reason,
        )


__all__ = [
    "EVENT_NEW_ROOM",
    "EVENT_REWIND",
    "EVENT_SAME_ROOM",
    "RoomEvent",
    "RoomToken",
    "RoomTracker",
    "Snapshot",
]

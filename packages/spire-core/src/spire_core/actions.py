"""候选 id <-> 语义动作 的双向映射。

候选 id 是跨"构题 -> 裁决 -> 执行 -> 记录"的字符串主键，语法在
docs/06-decision-points.md#候选-id-规范 固定。本模块是它的唯一定义点：
**不要在别处拼字符串。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ParseError

# --- 动作 kind（与 docs/03-mod-protocol.md 的白名单一一对应） ---
KIND_PLAY_CARD = "play_card"
KIND_USE_POTION = "use_potion"
KIND_DISCARD_POTION = "discard_potion"
KIND_END_TURN = "end_turn"
KIND_SELECT_CHOICE = "select_choice"
KIND_SELECT_REWARD = "select_reward"
KIND_PROCEED = "proceed"
KIND_RETURN = "return"
KIND_SELECT_CARDS = "select_cards"
KIND_SELECT_CARD_REWARD = "select_card_reward"
KIND_SELECT_MAP_NODE = "select_map_node"

ACTION_KINDS = (
    KIND_PLAY_CARD,
    KIND_USE_POTION,
    KIND_DISCARD_POTION,
    KIND_END_TURN,
    KIND_SELECT_CHOICE,
    KIND_SELECT_REWARD,
    KIND_PROCEED,
    KIND_RETURN,
    KIND_SELECT_CARDS,
    KIND_SELECT_CARD_REWARD,
    KIND_SELECT_MAP_NODE,
)

# --- 候选 id 前缀 ---
P_PLAY = "play"
P_POTION = "potion"
P_TARGET = "target"
P_CARD = "card"
P_NODE = "node"
P_REWARD = "reward"
P_RELIC = "relic"
P_OPTION = "option"
P_BUY = "buy"
P_REST = "rest"
P_NEOW = "neow"
P_CHOICE = "choice"

END_TURN = "end_turn"
SKIP = "skip"
LEAVE = "leave"
REST_HEAL = "rest:heal"
REST_SMITH = "rest:smith"

ARROW = "->"


@dataclass(frozen=True)
class Action:
    """一个语义动作。`args` 必须可 JSON 序列化。"""

    kind: str
    args: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"unknown action kind: {self.kind!r}")

    def wire(self) -> dict[str, Any]:
        """转成发给 mod 的 `action` 消息体。"""
        return {"kind": self.kind, "args": dict(self.args)}


# --------------------------------------------------------------------------- #
# 构造
# --------------------------------------------------------------------------- #


def format_play(hand_index: int, target_index: int | None = None) -> str:
    if target_index is None:
        return f"{P_PLAY}:h{hand_index}"
    return f"{P_PLAY}:h{hand_index}{ARROW}m{target_index}"


def format_potion(slot: int, target_index: int | None = None) -> str:
    if target_index is None:
        return f"{P_POTION}:p{slot}"
    return f"{P_POTION}:p{slot}{ARROW}m{target_index}"


def format_target(monster_index: int) -> str:
    return f"{P_TARGET}:m{monster_index}"


def format_card(zone: str, index: int) -> str:
    return f"{P_CARD}:{zone}:{index}"


def format_node(node_id: str) -> str:
    return f"{P_NODE}:{node_id}"


def format_reward(index: int) -> str:
    return f"{P_REWARD}:{index}"


def format_relic(index: int) -> str:
    return f"{P_RELIC}:{index}"


def format_option(index: int) -> str:
    return f"{P_OPTION}:{index}"


def format_buy(index: int) -> str:
    return f"{P_BUY}:{index}"


def format_neow(index: int) -> str:
    return f"{P_NEOW}:{index}"


def format_choice(index: int) -> str:
    return f"{P_CHOICE}:{index}"


# --------------------------------------------------------------------------- #
# 解析
# --------------------------------------------------------------------------- #


def _split_arrow(cid: str) -> tuple[str, str | None]:
    if ARROW not in cid:
        return cid, None
    head, tail = cid.split(ARROW, 1)
    return head, tail


def _int(s: str, what: str) -> int:
    try:
        return int(s)
    except ValueError as exc:
        raise ParseError(f"candidate id has non-integer {what}: {s!r}") from exc


def parse_candidate(cid: str) -> Action:
    """把候选 id 解析成**独立可执行**的动作。

    选牌类候选（`card:<zone>:<index>`）不是独立动作 —— 它们由决策点层
    聚合成一次 `select_cards` 提交。对它们调用本函数会抛 ParseError。
    """
    if cid == END_TURN:
        return Action(KIND_END_TURN, {})
    if cid == SKIP:
        return Action(KIND_SELECT_CARD_REWARD, {"index": -1})
    if cid == LEAVE:
        return Action(KIND_SELECT_CHOICE, {"index": -1})
    if cid == REST_HEAL:
        return Action(KIND_SELECT_CHOICE, {"index": 0})
    if cid == REST_SMITH:
        return Action(KIND_SELECT_CHOICE, {"index": 1})

    head, target = _split_arrow(cid)
    prefix, _, rest = head.partition(":")

    if prefix == P_PLAY:
        if not rest.startswith("h"):
            raise ParseError(f"bad play candidate: {cid!r}")
        hand_index = _int(rest[1:], "hand index")
        args: dict[str, Any] = {"hand_index": hand_index}
        if target is not None:
            if not target.startswith("m"):
                raise ParseError(f"bad target in candidate: {cid!r}")
            args["target"] = target
        return Action(KIND_PLAY_CARD, args)

    if prefix == P_POTION:
        if not rest.startswith("p"):
            raise ParseError(f"bad potion candidate: {cid!r}")
        slot = _int(rest[1:], "potion slot")
        args = {"potion_index": slot}
        if target is not None:
            args["target"] = target
        return Action(KIND_USE_POTION, args)

    if prefix == P_TARGET:
        if not rest.startswith("m"):
            raise ParseError(f"bad target candidate: {cid!r}")
        return Action(KIND_SELECT_CHOICE, {"monster": _int(rest[1:], "monster index")})

    if prefix == P_NODE:
        if not rest:
            raise ParseError(f"bad node candidate: {cid!r}")
        return Action(KIND_SELECT_MAP_NODE, {"node": rest})

    if prefix == P_REWARD:
        return Action(KIND_SELECT_REWARD, {"index": _int(rest, "reward index")})

    if prefix == P_RELIC:
        return Action(KIND_SELECT_CHOICE, {"index": _int(rest, "relic index")})

    if prefix == P_OPTION:
        return Action(KIND_SELECT_CHOICE, {"index": _int(rest, "option index")})

    if prefix == P_BUY:
        return Action(KIND_SELECT_CHOICE, {"index": _int(rest, "shop index")})

    if prefix == P_NEOW:
        return Action(KIND_SELECT_CHOICE, {"index": _int(rest, "neow index")})

    if prefix == P_CHOICE:
        return Action(KIND_SELECT_CHOICE, {"index": _int(rest, "choice index")})

    if prefix == P_CARD:
        raise ParseError(
            f"{cid!r} is a card-selection candidate; aggregate with select_cards()"
        )

    raise ParseError(f"unknown candidate id: {cid!r}")


def parse_card_candidate(cid: str) -> tuple[str, int]:
    """解析选牌类候选 id -> (zone, index)。"""
    prefix, _, rest = cid.partition(":")
    if prefix != P_CARD:
        raise ParseError(f"not a card candidate: {cid!r}")
    zone, _, idx = rest.rpartition(":")
    if not zone:
        raise ParseError(f"bad card candidate: {cid!r}")
    return zone, _int(idx, "card index")


def select_cards(card_candidates: list[str]) -> Action:
    """把若干选牌候选聚合成一次提交。

    一次性提交而不是逐张点选，解决两种形态：
    - 必选 k 张：indices 恰好 k 个；
    - 任意多选：indices 可以是 0..max 个（空列表表示跳过）。
    """
    items = [parse_card_candidate(c) for c in card_candidates]
    return Action(KIND_SELECT_CARDS, {"indices": [[z, i] for z, i in items]})


def is_card_candidate(cid: str) -> bool:
    return cid.startswith(P_CARD + ":")


__all__ = [
    "ACTION_KINDS",
    "ARROW",
    "Action",
    "END_TURN",
    "KIND_DISCARD_POTION",
    "KIND_END_TURN",
    "KIND_PLAY_CARD",
    "KIND_PROCEED",
    "KIND_RETURN",
    "KIND_SELECT_CARD_REWARD",
    "KIND_SELECT_CARDS",
    "KIND_SELECT_CHOICE",
    "KIND_SELECT_MAP_NODE",
    "KIND_SELECT_REWARD",
    "KIND_USE_POTION",
    "LEAVE",
    "P_BUY",
    "P_CARD",
    "P_CHOICE",
    "P_NODE",
    "P_NEOW",
    "P_OPTION",
    "P_PLAY",
    "P_POTION",
    "P_RELIC",
    "P_REST",
    "P_REWARD",
    "P_TARGET",
    "REST_HEAL",
    "REST_SMITH",
    "SKIP",
    "format_buy",
    "format_card",
    "format_choice",
    "format_neow",
    "format_node",
    "format_option",
    "format_play",
    "format_potion",
    "format_relic",
    "format_reward",
    "format_target",
    "is_card_candidate",
    "parse_candidate",
    "parse_card_candidate",
    "select_cards",
]
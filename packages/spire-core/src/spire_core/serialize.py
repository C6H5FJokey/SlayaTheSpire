"""公平视图 -> 送给 Laya 的英文紧凑 state。

必须是**纯函数**：同样的 `FairObservation` 输入，必然得到逐字节相同的
`json.dumps` 输出（见 docs/05-state-schema.md#5-确定性）。因此本模块里
不允许出现时间、随机数、集合迭代顺序。
"""

from __future__ import annotations

from typing import Any

from .describe import (
    ROOM_LABEL,
    card_brief,
    card_name,
    deck_summary,
    monster_brief,
    truncate_keep_numbers,
)
from .ids import ZONE_DECK, canonical_json
from .model import (
    FairObservation,
    RawCard,
    RawMap,
    RewardDetail,
    SCREEN_NONE,
)

MODE_COMBAT = "combat"
MODE_MAP = "map"
MODE_CARD_REWARD = "card_reward"
MODE_EVENT = "event"
MODE_SHOP = "shop"
MODE_REST = "rest"
MODE_SELECT = "select"
MODE_REWARD = "reward"
MODE_IDLE = "idle"

_SCREEN_TO_MODE = {
    "NONE": MODE_COMBAT,
    "MAP": MODE_MAP,
    "CARD_REWARD": MODE_CARD_REWARD,
    "COMBAT_REWARD": MODE_REWARD,
    "GRID": MODE_SELECT,
    "EVENT": MODE_EVENT,
    "SHOP_ROOM": MODE_SHOP,
    "REST": MODE_REST,
    "BOSS_RELIC": MODE_CARD_REWARD,
    "NEOW": MODE_EVENT,
}


def _mode(fair: FairObservation) -> str:
    if fair.in_combat and fair.screen == SCREEN_NONE:
        return MODE_COMBAT
    return _SCREEN_TO_MODE.get(fair.screen, MODE_IDLE)


def _card_entry(card: RawCard, short_id: str) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": short_id,
        "card": card.id,
        "up": card.upgrades,
        # 类型与稀有度是人类选卡时的第一眼信息（稀有度决定值不值得为它撑大卡组），
        # 原始观测里一直有，只是以前没往下传。
        "type": card.type,
        "rarity": card.rarity,
        "cost": card.effective_cost,
        "playable": card.is_playable,
    }
    if card.target_type != "NONE":
        entry["target"] = card.target_type
    entry["text"] = truncate_keep_numbers(card.text)
    return entry


def _reward_detail(d: RewardDetail) -> dict[str, Any]:
    """一条奖励的结构化内容，与 `screen.options` 的文案逐条对齐。"""
    out: dict[str, Any] = {"kind": d.kind}
    if d.text:
        out["text"] = d.text
    if d.cards:
        # 卡牌奖励：三张候选的英文名（+稀有度），这是"要不要再点进去"的全部依据。
        out["cards"] = [
            {"card": c.id, "up": c.upgrades, "rarity": c.rarity, "type": c.type}
            for c in d.cards
        ]
    if d.id:
        out["id"] = d.id
    if d.amount:
        out["amount"] = d.amount
    return out


def _player(fair: FairObservation) -> dict[str, Any]:
    p = fair.player
    potions = [
        {
            "slot": i,
            "id": potion.id,
            "name": potion.name,
            "text": potion.text,
            "target": bool(potion.requires_target),
            "usable": bool(potion.can_use),
        }
        for i, potion in fair.potions()
    ]
    return {
        "character": p.character,
        "hp": p.hp,
        "max_hp": p.max_hp,
        "block": p.block,
        "energy": p.energy,
        "gold": p.gold,
        "powers": [
            {"id": x.id, "amount": x.amount, "name": x.name, "text": x.text}
            for x in p.powers
        ],
        "relics": [
            {"id": x.id, "counter": x.counter, "name": x.name, "text": x.text}
            for x in p.relics
        ],
        "potions": potions,
        # 空槽不发给模型，但槽位总数是公平信息（人类看得见有几格）。
        "potion_slots": p.potion_slots or len(p.potions),
    }


def _map(fair: FairObservation, m: RawMap | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "act": m.act,
        "current": m.current,
        "reachable": list(m.reachable),
    }


def state(fair: FairObservation, *, compact_deck: bool = True) -> dict[str, Any]:
    """构造 state。

    `compact_deck=True` 时牌组只给计数摘要（省预算，见 docs/05-state-schema.md#预算与压缩
    的第 3 条优先级）。
    """
    out: dict[str, Any] = {
        "mode": _mode(fair),
        "act": fair.act,
        "floor": fair.floor,
        "room_type": ROOM_LABEL.get(fair.room_type, fair.room_type),
        "ascension": fair.ascension,
        "player": _player(fair),
    }

    if fair.map is not None:
        out["map"] = _map(fair, fair.map)

    hand = list(fair.hand)
    if hand:
        out["hand"] = [
            _card_entry(c, f"h{c.index}") for c in hand
        ]

    monsters = [m for m in fair.monsters if m.hp > 0]
    if monsters:
        out["monsters"] = [m.to_dict() for m in monsters]

    if fair.in_combat:
        out["combat"] = {
            "turn": fair.combat_turn,
            "cards_discarded_this_turn": fair.cards_discarded_this_turn,
            "times_damaged": fair.times_damaged,
        }

    zones = fair.zones.to_dict()
    if compact_deck:
        # 牌组只保留总张数与"每种几张"，不给每张文本
        deck = zones.pop(ZONE_DECK)
        zones["deck"] = {"total": deck["total"], "stacks": deck["stacks"]}
        out["deck_summary"] = deck_summary(fair)
    out["zones"] = zones

    screen = fair.screen_state
    screen_out: dict[str, Any] = {}
    if screen.options:
        screen_out["options"] = list(screen.options)
    # 选牌是**有状态**的：同一块 "选一张牌" 界面对应升级 / 删除 / 变形 / 事件。
    # `origin` 是机器可读的来由，`event_name`/`event_text` 是事件上下文（都是
    # 玩家进入事件时就看得见的公平信息）。不给上下文的话模型只能瞎猜。
    if screen.origin:
        screen_out["origin"] = screen.origin
    if screen.event_name:
        screen_out["event_name"] = screen.event_name
    if screen.event_text:
        screen_out["event_text"] = screen.event_text
    if screen.select_cards:
        screen_out["min_select"] = screen.min_select
        screen_out["max_select"] = screen.max_select
        screen_out["selectable"] = [
            _card_entry(zc.card, f"{zc.zone}:{zc.card.index}")
            for zc in screen.select_cards
        ]
    if screen.reward_details:
        # 与 `options` 逐条对齐：`options` 里已经带了文案，这里给结构化内容，
        # 尤其是卡牌奖励那三张牌的名字（"再点进去值不值"就靠它判断）。
        screen_out["reward_details"] = [
            _reward_detail(d) for d in screen.reward_details
        ]
    if screen.reward_cards:
        screen_out["reward_cards"] = [
            {"index": i, **_card_entry(c, card_name(c))}
            for i, c in enumerate(screen.reward_cards)
        ]
    if screen.reward_relics:
        screen_out["reward_relics"] = [
            {"index": i, "id": r.id} for i, r in enumerate(screen.reward_relics)
        ]
    if screen.shop_items:
        screen_out["shop"] = [
            {
                "index": it.index,
                "kind": it.kind,
                "id": it.id,
                "name": it.name,
                "text": it.text,
                "price": it.price,
                "affordable": it.affordable,
            }
            for it in screen.shop_items
        ]
    if screen.rest_options:
        screen_out["rest_options"] = list(screen.rest_options)
    if screen.neow_options:
        screen_out["neow_options"] = list(screen.neow_options)
    if screen_out:
        out["screen"] = screen_out

    return out


def state_json(fair: FairObservation, *, compact_deck: bool = True) -> str:
    return canonical_json(state(fair, compact_deck=compact_deck))


__all__ = [
    "MODE_CARD_REWARD",
    "MODE_COMBAT",
    "MODE_EVENT",
    "MODE_IDLE",
    "MODE_MAP",
    "MODE_REST",
    "MODE_REWARD",
    "MODE_SELECT",
    "MODE_SHOP",
    "state",
    "state_json",
]

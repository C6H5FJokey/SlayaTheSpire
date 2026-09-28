"""公平过滤器：`RawObservation` -> `FairObservation`。

这是"模型能看到什么"的唯一实现点。新增字段时的规矩：
**默认不进 FairObservation，必须显式加进去并在 docs/04-fairness.md 登记。**
"""

from __future__ import annotations

from .model import (
    CardStack,
    FairObservation,
    Intent,
    Monster,
    RawCard,
    RawObservation,
    ScreenFair,
    ScreenState,
    ZonedCard,
)

FAIRNESS_STRICT = "strict"
FAIRNESS_OMNISCIENT = "omniscient"

# 供测试引用的"绝不允许出现在输出里"的字段名清单。
FORBIDDEN_KEYS = (
    "run_seed",
    "seed",
    "move_history",
    "upcoming_moves",
    "base_damage",
    "draw_pile_order",
)


def card_stacks(cards: tuple[RawCard, ...]) -> tuple[CardStack, ...]:
    """把一组牌折叠成排序后的多重集。

    排序是必须的：raw 的数组顺序**就是**抽牌顺序，保留它等于泄漏
    （见 docs/04-fairness.md 的"抽牌堆变多重集"）。
    """
    counts: dict[tuple[str, int], int] = {}
    for c in cards:
        key = (c.id, c.upgrades)
        counts[key] = counts.get(key, 0) + 1
    stacks = [
        CardStack(id=cid, upgrades=up, count=n) for (cid, up), n in counts.items()
    ]
    stacks.sort(key=lambda s: (s.id, s.upgrades))
    return tuple(stacks)


def _fair_monster(m) -> Monster:
    intent = None
    if m.intent is not None:
        # 只保留结算后伤害：base_damage 属于内部数值，反推力量/易伤会带来
        # 不公平优势（见 docs/04-fairness.md 的字段级规则表）。
        intent = Intent(
            id=m.intent.id,
            hits=m.intent.hits,
            adjusted_damage=m.intent.adjusted_damage,
            text=m.intent.text,
        )
    return Monster(
        index=m.index,
        id=m.id,
        name=m.name,
        hp=m.hp,
        max_hp=m.max_hp,
        block=m.block,
        powers=m.powers,
        intent=intent,
    )


def _fair_screen(s: ScreenState) -> ScreenFair:
    """界面状态整体可见（它就是给玩家看的），但显式构造而非复制。"""
    return ScreenFair(
        options=s.options,
        min_select=s.min_select,
        max_select=s.max_select,
        select_cards=s.select_cards,
        reward_cards=s.reward_cards,
        reward_relics=s.reward_relics,
        reward_details=s.reward_details,
        shop_items=s.shop_items,
        rest_options=s.rest_options,
        neow_options=s.neow_options,
        origin=s.origin,
        event_name=s.event_name,
        event_text=s.event_text,
        reason=s.reason,
    )


def filter_(raw: RawObservation, mode: str = FAIRNESS_STRICT) -> FairObservation:
    """产出公平视图。

    `mode == "omniscient"` 时仍走同一条构造路径（结构一致），但调用方
    **不应**依赖它产生额外字段——全知模式由 `serialize` 侧另行处理。
    本函数的存在意义是：无论哪种模式，`FairObservation` 的结构都稳定，
    免得下游出现两套代码路径。
    """
    if mode not in (FAIRNESS_STRICT, FAIRNESS_OMNISCIENT):
        raise ValueError(f"unknown fairness mode: {mode!r}")

    combat = raw.combat
    hand: tuple[RawCard, ...] = combat.hand if combat else ()
    monsters = tuple(_fair_monster(m) for m in (combat.monsters if combat else ()))

    # 手牌保留顺序（位置是玩家实际操作的依据，见 docs/04-fairness.md#3-手牌例外）
    if not combat:
        hand = ()

    zones_cards = {
        "draw": combat.draw_pile if combat else (),
        "discard": combat.discard_pile if combat else (),
        "exhaust": combat.exhaust_pile if combat else (),
        "deck": raw.deck,
    }
    from .model import Zones

    return FairObservation(
        game_version=raw.game_version,
        screen=raw.screen,
        in_combat=raw.in_combat,
        act=raw.act,
        floor=raw.floor,
        node=raw.node,
        room_type=raw.room_type,
        ascension=raw.ascension,
        player=raw.player,
        hand=hand,
        monsters=monsters,
        zones=Zones(
            draw=card_stacks(zones_cards["draw"]),
            discard=card_stacks(zones_cards["discard"]),
            exhaust=card_stacks(zones_cards["exhaust"]),
            deck=card_stacks(zones_cards["deck"]),
        ),
        combat_turn=combat.turn if combat else 0,
        cards_discarded_this_turn=combat.cards_discarded_this_turn if combat else 0,
        times_damaged=combat.times_damaged if combat else 0,
        screen_state=_fair_screen(raw.screen_state),
        map=raw.map,
    )


__all__ = [
    "FAIRNESS_OMNISCIENT",
    "FAIRNESS_STRICT",
    "FORBIDDEN_KEYS",
    "card_stacks",
    "filter_",
]

"""候选枚举。

每个决策点一个枚举器，全部是纯函数。候选的**顺序即 tie-break 顺序**，
所以顺序必须确定（见 docs/06-decision-points.md#子选择裁决器）。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import actions as A
from .describe import (
    ROOM_LABEL,
    card_brief,
    card_cost,
    card_name,
    describe_map_candidate,
    monster_brief,
    relic_brief,
    shop_brief,
)
from .errors import NoCandidates
from .model import (
    SCREEN_COMBAT_REWARD,
    TARGET_ALL_ENEMY,
    TARGET_ENEMY,
    FairObservation,
    Monster,
    RawCard,
)
from .decision import (
    CARD_REWARD,
    COMBAT_PLAY,
    EVENT_OPTION,
    GENERIC_CHOICE,
    MAP_NODE,
    NEOW_BONUS,
    RELIC_SELECT,
    REST_SITE,
    SELECT_CARD_ANY,
    SELECT_CARD_MUST_K,
    SELECT_TARGET,
    SHOP,
)


@dataclass(frozen=True)
class Candidate:
    """一个可执行（或可被选中）的候选。"""

    cid: str
    description: str
    action: A.Action | None = None  # None => 选牌类，由决策点聚合


def _targets(fair: FairObservation, card: RawCard) -> list[Monster]:
    alive = fair.alive_monsters()
    if card.target_type == TARGET_ENEMY:
        return list(alive)
    return []


def _play_candidates(fair: FairObservation) -> list[Candidate]:
    out: list[Candidate] = []
    alive = fair.alive_monsters()
    for card in fair.hand:
        if not card.is_playable:
            continue
        targets = _targets(fair, card)
        if card.target_type == TARGET_ENEMY and not targets:
            # 需要单体目标的牌但场上没有活着的敌人：不可打
            continue
        if targets:
            for m in targets:
                out.append(
                    Candidate(
                        cid=A.format_play(card.index, m.index),
                        description=(
                            f"Play {card_brief(card)} on {m.name} (m{m.index})."
                        ),
                        action=A.Action(
                            A.KIND_PLAY_CARD,
                            {"hand_index": card.index, "target": f"m{m.index}"},
                        ),
                    )
                )
        else:
            suffix = ""
            if card.target_type == TARGET_ALL_ENEMY:
                suffix = f" (hits all {len(alive)} enemies)"
            out.append(
                Candidate(
                    cid=A.format_play(card.index),
                    description=f"Play {card_brief(card)}.{suffix}",
                    action=A.Action(
                        A.KIND_PLAY_CARD, {"hand_index": card.index}
                    ),
                )
            )

    # 药水：放在牌之后、end_turn 之前
    for slot, potion in fair.potions():
        if not potion.can_use:
            continue
        if potion.requires_target and alive:
            for m in alive:
                out.append(
                    Candidate(
                        cid=A.format_potion(slot, m.index),
                        description=f"Use potion {potion.id} on {m.name} (m{m.index}).",
                        action=A.Action(
                            A.KIND_USE_POTION,
                            {"potion_index": slot, "target": f"m{m.index}"},
                        ),
                    )
                )
        elif not potion.requires_target:
            out.append(
                Candidate(
                    cid=A.format_potion(slot),
                    description=f"Use potion {potion.id}.",
                    action=A.Action(A.KIND_USE_POTION, {"potion_index": slot}),
                )
            )

    out.append(
        Candidate(
            cid=A.END_TURN,
            description="End your turn.",
            action=A.Action(A.KIND_END_TURN, {}),
        )
    )
    return out


def _card_select_candidates(fair: FairObservation) -> list[Candidate]:
    purpose = selection_purpose(fair)
    out: list[Candidate] = []
    for zc in fair.screen_state.select_cards:
        out.append(
            Candidate(
                cid=A.format_card(zc.zone, zc.card.index),
                description=f"Select {card_brief(zc.card)} from {zc.zone} ({purpose}).",
                action=None,
            )
        )
    return out


# `origin` -> 人话。选牌界面本身不带语义，语义全在"谁开的这块界面"上：
# 同样的 "Strike"，被要求升级、被要求删掉、被要求变形，答案完全不同。
_ORIGIN_PURPOSE = {
    "rest_smith": "rest site smith: upgrade a card",
    "transform": "transform a card",
    "purge": "remove a card from your deck",
    "upgrade": "upgrade a card",
    "confirm": "just confirming a group of cards",
    "hand_select": "card selection from hand",
    # 观者的预见：看抽牌堆顶的若干张，任意张丢进弃牌堆（`draw_order` 告诉
    # 模型哪张离牌堆顶更近）。这是"任意多选"，min=0 也合法。
    "scry": "scry: look at that many cards from the top of your draw pile and discard any of them",
    # 战斗内的检索（头槌 / 全息影像 / 发掘 / 秘密技法 / 万能药…）：具体效果由
    # 界面上的 `reason` 说，`origin` 只负责说清"这是战斗里的一次挑牌"。
    "combat_select": "in-combat card retrieval; the on-screen reason says what it does",
    "select": "card selection",
}


def selection_purpose(fair: FairObservation) -> str:
    """选牌界面的来由，一句话人话（带事件名）。构题与候选描述共用同一个定义。"""
    s = fair.screen_state
    if s.origin == "event":
        return f"event {s.event_name}" if s.event_name else "event effect"
    return _ORIGIN_PURPOSE.get(s.origin, "card selection")


def enumerate_candidates(
    fair: FairObservation, decision_point: str, *, context: dict | None = None
) -> list[Candidate]:
    """枚举候选。`context` 可携带 `selection_picked`（必选 k 张的已选集合）。"""
    context = context or {}
    picked: set[str] = set(context.get("selection_picked") or ())

    if decision_point == COMBAT_PLAY:
        cands = _play_candidates(fair)

    elif decision_point == SELECT_TARGET:
        cands = [
            Candidate(
                cid=A.format_target(m.index),
                description=f"Target {monster_brief(m)}.",
                action=A.Action(A.KIND_SELECT_CHOICE, {"monster": m.index}),
            )
            for m in fair.alive_monsters()
        ]

    elif decision_point in (SELECT_CARD_MUST_K, SELECT_CARD_ANY):
        cands = [c for c in _card_select_candidates(fair) if c.cid not in picked]
        # A confirmation/optional selection screen can legitimately contain no
        # cards. Submit the empty selection so the game can consume its confirm
        # button; otherwise the pipeline raises NoCandidates and waits for the
        # watchdog forever.
        if not cands and decision_point == SELECT_CARD_ANY and fair.screen_state.min_select == 0:
            cands = [
                Candidate(
                    cid=A.EMPTY_SELECTION,
                    description="Select no cards and confirm.",
                    action=A.select_cards([]),
                )
            ]

    elif decision_point == MAP_NODE:
        by_id = {n.id: n for n in (fair.map.nodes if fair.map else ())}
        cands = []
        for nid in fair.map.reachable if fair.map else ():
            node = by_id.get(nid)
            desc = (
                describe_map_candidate(fair, node)
                if node is not None
                else f"Go to node {nid}."
            )
            cands.append(
                Candidate(
                    cid=A.format_node(nid),
                    description=desc,
                    action=A.Action(A.KIND_SELECT_MAP_NODE, {"node": nid}),
                )
            )

    elif decision_point == CARD_REWARD:
        cands = [
            Candidate(
                cid=A.format_reward(i),
                description=f"Take {card_brief(c)}.",
                action=A.Action(A.KIND_SELECT_REWARD, {"index": i}),
            )
            for i, c in enumerate(fair.screen_state.reward_cards)
        ]
        cands.append(
            Candidate(
                cid=A.SKIP,
                description="Skip: none of these three cards is worth taking.",
                action=A.Action(A.KIND_SELECT_CARD_REWARD, {"index": -1}),
            )
        )

    elif decision_point == RELIC_SELECT:
        cands = [
            Candidate(
                cid=A.format_relic(i),
                description=relic_brief(r),
                action=A.Action(A.KIND_SELECT_CHOICE, {"index": i}),
            )
            for i, r in enumerate(fair.screen_state.reward_relics)
        ]

    elif decision_point == EVENT_OPTION:
        cands = [
            Candidate(
                cid=A.format_option(i),
                description=f"Choose option: {text}",
                action=A.Action(A.KIND_SELECT_CHOICE, {"index": i}),
            )
            for i, text in enumerate(fair.screen_state.options)
            if not text.startswith("[disabled]")
        ]

    elif decision_point == SHOP:
        cands = []
        for item in fair.screen_state.shop_items:
            if not item.affordable:
                continue
            desc = shop_brief(item)
            cands.append(
                Candidate(
                    cid=A.format_buy(item.index),
                    description=desc,
                    action=A.Action(A.KIND_SELECT_CHOICE, {"index": item.index}),
                )
            )
        cands.append(
            Candidate(
                cid=A.LEAVE,
                description="Leave the shop.",
                action=A.Action(A.KIND_SELECT_CHOICE, {"index": -1}),
            )
        )

    elif decision_point == REST_SITE:
        # rest_options 是游戏给出的按钮文本（"Rest" / "Smith"）。Fusion Hammer
        # 之类的遗物会让某个按钮根本不出现，所以候选必须**按实际选项**生成，
        # 并沿用选项在界面里的下标（mod 执行时用 index 点选项）。
        opts = fair.screen_state.rest_options
        if not opts and fair.screen_state.options:
            cands = [
                Candidate(
                    cid=A.PROCEED,
                    description="Continue from the completed campfire.",
                    action=A.Action(A.KIND_PROCEED, {}),
                )
            ]
        else:
            lowered = [o.lower() for o in opts]
            spec = (
                (("rest", "heal"), A.REST_HEAL, "Rest (heal)"),
                (("smith", "upgrade"), A.REST_SMITH, "Smith (upgrade a card)"),
            )
            cands = []
            for keys, cid, label in spec:
                for i, opt in enumerate(lowered):
                    if any(k in opt for k in keys):
                        cands.append(
                            Candidate(
                                cid=cid,
                                description=f"{label}.",
                                action=A.Action(A.KIND_SELECT_CHOICE, {"index": i}),
                            )
                        )
                        break

    elif decision_point == NEOW_BONUS:
        cands = [
            Candidate(
                cid=A.format_neow(i),
                description=f"Choose Neow bonus: {text}",
                action=A.Action(A.KIND_SELECT_CHOICE, {"index": i}),
            )
            for i, text in enumerate(fair.screen_state.neow_options)
        ]

    elif decision_point == GENERIC_CHOICE:
        cands = [
            Candidate(
                cid=A.format_choice(i),
                description=f"Choose: {text}",
                action=A.Action(A.KIND_SELECT_CHOICE, {"index": i}),
            )
            for i, text in enumerate(fair.screen_state.options)
        ]

    else:
        raise NoCandidates(f"no enumerator for decision point {decision_point!r}")

    if not cands:
        raise NoCandidates(
            f"decision point {decision_point!r} produced zero candidates "
            f"(screen={fair.screen!r}, in_combat={fair.in_combat})"
        )
    return cands


__all__ = ["Candidate", "enumerate_candidates", "selection_purpose"]

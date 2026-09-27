"""兜底策略：不依赖任何模型的保守规则。

只在 Laya 调用失败、预算超限、答案无法解析时使用（见 docs/07-laya-contract.md#兜底策略）。
兜底产生的行会标 `agent_fallback=true`，默认不进训练集。
"""

from __future__ import annotations

from . import actions as A
from .candidates import Candidate
from .decision import (
    COMBAT_PLAY,
    MAP_NODE,
    REST_SITE,
    SELECT_CARD_ANY,
    SELECT_CARD_MUST_K,
    SELECT_TARGET,
)
from .model import CARD_TYPE_ATTACK, FairObservation

REST_HEAL_HP_RATIO = 0.6


def fallback_action(
    fair: FairObservation,
    decision_point: str,
    candidates: list[Candidate],
) -> Candidate:
    """返回被选中的候选。永远不抛异常：最差也会返回 candidates[0]。"""
    if decision_point == COMBAT_PLAY:
        picked = _combat_fallback(fair, candidates)
        if picked is not None:
            return picked

    if decision_point == SELECT_TARGET:
        alive = fair.alive_monsters()
        if alive:
            lowest = min(alive, key=lambda m: (m.hp, m.index))
            for c in candidates:
                if c.cid == A.format_target(lowest.index):
                    return c

    if decision_point == REST_SITE:
        want = A.REST_HEAL if fair.player.hp < fair.player.max_hp * REST_HEAL_HP_RATIO else A.REST_SMITH
        for c in candidates:
            if c.cid == want:
                return c

    # 其余决策点：取第一个候选（枚举顺序即确定性优先序）
    return candidates[0]


def _combat_fallback(
    fair: FairObservation, candidates: list[Candidate]
) -> Candidate | None:
    alive = fair.alive_monsters()
    by_index = {c.index: c for c in fair.hand}

    playable = [c for c in candidates if c.cid.startswith("play:")]
    if not playable:
        return None

    def card_of(cand: Candidate) -> object | None:
        try:
            action = cand.action
            if action is None:
                return None
            hi = action.args.get("hand_index")
            return by_index.get(hi) if isinstance(hi, int) else None
        except Exception:
            return None

    # 1) 攻击牌：伤害最高；目标取血量最低的敌人
    attacks = [
        (c, card_of(c))
        for c in playable
        if card_of(c) is not None and card_of(c).type == CARD_TYPE_ATTACK
    ]
    if attacks and alive:
        lowest = min(alive, key=lambda m: (m.hp, m.index))
        best: Candidate | None = None
        best_damage = -1
        for cand, card in attacks:
            if cand.action is None:
                continue
            target = cand.action.args.get("target")
            # 需要单体目标时只考虑打向血量最低敌人的那一个候选
            if target is not None and target != f"m{lowest.index}":
                continue
            dmg = getattr(card, "damage", 0) * max(1, getattr(card, "magic_number", 0) or 1)
            if dmg > best_damage:
                best_damage = dmg
                best = cand
        if best is not None:
            return best

    # 2) 防御牌：格挡最高
    blocks = [
        (c, card_of(c))
        for c in playable
        if card_of(c) is not None and getattr(card_of(c), "block", 0) > 0
    ]
    if blocks:
        return max(blocks, key=lambda pair: getattr(pair[1], "block", 0))[0]

    # 3) 结束回合
    for c in candidates:
        if c.cid == A.END_TURN:
            return c
    return playable[0]


def select_cards_fallback(candidates: list[Candidate], count: int) -> list[str]:
    """必选/多选的兜底：按枚举顺序取前 count 个。"""
    return [c.cid for c in candidates[: max(0, count)]]


__all__ = ["REST_HEAL_HP_RATIO", "fallback_action", "select_cards_fallback"]
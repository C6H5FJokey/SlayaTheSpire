"""合法性判定：一个候选动作，游戏到底认不认。

**唯一的事实来源是 agent 侧的观测**，不是游戏 API 的结论。血泪教训：空药水槽的
`canUse()` 返回 true（游戏自己判空槽看的是 id，不是这个方法），我们照抄之后
把"用药水"当成了可选项，模型选一次被拒一次、局面又不变，于是无限空转。

所以规则是：

1. `enumerate_candidates` 只产出合法动作 —— **不合法的动作根本不会出现在
   送给 Laya 的 `criteria` 里**，模型没有机会选到它。
2. 构题前再拿本模块过一遍（`pipeline.make_plan`），把枚举器的 bug 挡在
   发请求之前，并记录被丢掉的原因。
3. 模组侧仍有第二层白名单（`ActionSpec.validate`），但那是防"agent 给出的
   东西和当前局面错位"，不是第一道防线。

本模块是纯函数，与 `fairness` 一样：只读 `FairObservation`，不碰网络与文件。
"""

from __future__ import annotations

from . import actions as A
from .decision import (
    EVENT_OPTION,
    GENERIC_CHOICE,
    RELIC_SELECT,
    REST_SITE,
    SHOP,
)
from .model import FairObservation
from .model import TARGET_ENEMY
from .errors import ParseError


def check(
    fair: FairObservation,
    cid: str,
    action: A.Action | None,
    decision_point: str | None = None,
) -> str | None:
    """合法返回 None，不合法返回人类可读的原因。

    `action is None` 表示"选牌类候选"，它不是独立动作，由决策点聚合成一次
    `select_cards`，所以只能校验它是否落在当前选牌界面的候选里。
    """
    if action is None:
        return _check_card_select(fair, cid)

    handler = _HANDLERS.get(action.kind)
    if handler is None:
        return f"unknown action kind: {action.kind!r}"
    return handler(fair, action, decision_point)


def legal_only(
    fair: FairObservation,
    candidates: list,
    decision_point: str | None = None,
) -> tuple[list, list[tuple[str, str]]]:
    """把候选集里的非法项摘掉。返回 `(合法候选, [(cid, 原因)])`。"""
    kept: list = []
    dropped: list[tuple[str, str]] = []
    for cand in candidates:
        reason = check(fair, cand.cid, cand.action, decision_point)
        if reason is None:
            kept.append(cand)
        else:
            dropped.append((cand.cid, reason))
    return kept, dropped


# --------------------------------------------------------------------------- #
# 各 kind 的判定
# --------------------------------------------------------------------------- #


def _check_card_select(fair: FairObservation, cid: str) -> str | None:
    try:
        zone, index = A.parse_card_candidate(cid)
    except ParseError:
        return f"not a card-select candidate: {cid!r}"
    for zc in fair.screen_state.select_cards:
        if zc.zone == zone and zc.card.index == index:
            return None
    return f"card {zone}:{index} is not on the current card-select screen"


def _check_play_card(fair, action, _dp) -> str | None:
    if not fair.in_combat:
        return "play_card outside combat"
    hand_index = action.args.get("hand_index")
    if not isinstance(hand_index, int):
        return "play_card without an integer hand_index"
    if not 0 <= hand_index < len(fair.hand):
        return f"hand_index {hand_index} out of range"
    card = fair.hand[hand_index]
    if not card.is_playable:
        return f"{card.id} is not playable right now"
    target = action.args.get("target")
    requires = card.target_type == TARGET_ENEMY
    if isinstance(target, str):
        monster = _monster(fair, target)
        if monster is None:
            return f"target {target} is not an alive monster"
        if not requires:
            return f"{card.id} does not take a single-enemy target"
    elif requires:
        return f"{card.id} needs a single-enemy target"
    return None


def _check_use_potion(fair, action, _dp) -> str | None:
    slot = action.args.get("potion_index")
    if not isinstance(slot, int):
        return "use_potion without an integer potion_index"
    potions = fair.player.potions
    if not 0 <= slot < len(potions):
        return f"potion_index {slot} out of range"
    potion = potions[slot]
    if potion is None or potion.is_empty:
        return f"potion slot {slot} is empty"
    if not potion.can_use:
        return f"potion {potion.id} cannot be used right now"
    target = action.args.get("target")
    if potion.requires_target:
        if not isinstance(target, str) or _monster(fair, target) is None:
            return f"potion {potion.id} needs an alive target"
    elif isinstance(target, str):
        return f"potion {potion.id} does not take a target"
    return None


def _check_end_turn(fair, _action, _dp) -> str | None:
    return None if fair.in_combat else "end_turn outside combat"


def _check_select_map_node(fair, action, _dp) -> str | None:
    node = action.args.get("node")
    if not isinstance(node, str):
        return "select_map_node without a node id"
    if fair.map is None:
        return "select_map_node without a map"
    if node not in fair.map.reachable:
        return f"node {node} is not reachable"
    return None


def _check_select_card_reward(fair, action, _dp) -> str | None:
    index = action.args.get("index")
    if index == -1:
        return None
    if not isinstance(index, int):
        return "select_card_reward without an integer index"
    if not 0 <= index < len(fair.screen_state.reward_cards):
        return f"reward index {index} out of range"
    return None


def _check_select_reward(fair, action, _dp) -> str | None:
    index = action.args.get("index")
    if not isinstance(index, int):
        return "select_reward without an integer index"
    if not 0 <= index < len(fair.screen_state.reward_cards):
        return f"reward index {index} out of range"
    return None


def _check_select_cards(fair, action, _dp) -> str | None:
    indices = action.args.get("indices")
    if not isinstance(indices, list):
        return "select_cards without an indices list"
    screen = fair.screen_state
    for pair in indices:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return f"select_cards entry is not a [zone, index] pair: {pair!r}"
        zone, index = pair
        if not any(zc.zone == zone and zc.card.index == index
                   for zc in screen.select_cards):
            return f"select_cards entry {zone}:{index} is not selectable"
    if len(indices) < screen.min_select:
        return f"select_cards needs at least {screen.min_select}"
    if screen.max_select > 0 and len(indices) > screen.max_select:
        return f"select_cards allows at most {screen.max_select}"
    return None


def _check_select_choice(fair, action, decision_point) -> str | None:
    index = action.args.get("index")
    if not isinstance(index, int):
        return "select_choice without an integer index"
    if index == -1:
        # 只有商店有"离开"这一个 -1 语义。
        return None if decision_point == SHOP else "index -1 is only valid in a shop"

    screen = fair.screen_state
    table = {
        SHOP: (len(screen.shop_items), "shop item"),
        REST_SITE: (len(screen.rest_options), "rest option"),
        RELIC_SELECT: (len(screen.reward_relics), "relic option"),
        EVENT_OPTION: (len(screen.options), "event option"),
        GENERIC_CHOICE: (len(screen.options), "option"),
    }
    if decision_point in table:
        limit, what = table[decision_point]
        if not 0 <= index < limit:
            return f"{what} index {index} out of range"
        return None

    # 决策点未知时给一个宽松但仍有界的判定：至少得落在某个真实按钮表里。
    limit = max(
        len(screen.options),
        len(screen.rest_options),
        len(screen.reward_relics),
        len(screen.shop_items),
    )
    if not 0 <= index < limit:
        return f"option index {index} is outside every known button list"
    return None


def _check_proceed(_fair, _action, _dp) -> str | None:
    return None


def _check_return(_fair, _action, _dp) -> str | None:
    return None


def _check_discard_potion(fair, action, _dp) -> str | None:
    slot = action.args.get("potion_index")
    if not isinstance(slot, int):
        return "discard_potion without an integer potion_index"
    potions = fair.player.potions
    if not 0 <= slot < len(potions):
        return f"potion_index {slot} out of range"
    potion = potions[slot]
    if potion is None or potion.is_empty:
        return f"potion slot {slot} is empty"
    return None


def _monster(fair: FairObservation, ref: str):
    """`m3` -> 活着的怪物；找不到返回 None。"""
    if not ref.startswith("m"):
        return None
    try:
        index = int(ref[1:])
    except ValueError:
        return None
    for m in fair.monsters:
        if m.index == index and m.hp > 0:
            return m
    return None


_HANDLERS = {
    A.KIND_PLAY_CARD: _check_play_card,
    A.KIND_USE_POTION: _check_use_potion,
    A.KIND_DISCARD_POTION: _check_discard_potion,
    A.KIND_END_TURN: _check_end_turn,
    A.KIND_SELECT_MAP_NODE: _check_select_map_node,
    A.KIND_SELECT_CARD_REWARD: _check_select_card_reward,
    A.KIND_SELECT_REWARD: _check_select_reward,
    A.KIND_SELECT_CARDS: _check_select_cards,
    A.KIND_SELECT_CHOICE: _check_select_choice,
    A.KIND_PROCEED: _check_proceed,
    A.KIND_RETURN: _check_return,
}


__all__ = ["check", "legal_only"]

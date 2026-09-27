"""决策点识别。

判定顺序见 docs/06-decision-points.md#决策点识别。自上而下、命中即返回。
"""

from __future__ import annotations

from .errors import UnknownDecisionPoint
from .model import (
    SCREEN_BOSS_RELIC,
    SCREEN_CARD_REWARD,
    SCREEN_COMBAT_REWARD,
    SCREEN_EVENT,
    SCREEN_GAME_OVER,
    SCREEN_GRID,
    SCREEN_MAP,
    SCREEN_NEOW,
    SCREEN_NONE,
    SCREEN_REST,
    SCREEN_SHOP_ROOM,
    FairObservation,
)

COMBAT_PLAY = "combat_play"
SELECT_TARGET = "select_target"
SELECT_CARD_MUST_K = "select_card_must_k"
SELECT_CARD_ANY = "select_card_any"
MAP_NODE = "map_node"
CARD_REWARD = "card_reward"
RELIC_SELECT = "relic_select"
EVENT_OPTION = "event_option"
SHOP = "shop"
REST_SITE = "rest_site"
NEOW_BONUS = "neow_bonus"
GENERIC_CHOICE = "generic_choice"
RUN_OVER = "run_over"

ALL_DECISION_POINTS = (
    COMBAT_PLAY,
    SELECT_TARGET,
    SELECT_CARD_MUST_K,
    SELECT_CARD_ANY,
    MAP_NODE,
    CARD_REWARD,
    RELIC_SELECT,
    EVENT_OPTION,
    SHOP,
    REST_SITE,
    NEOW_BONUS,
    GENERIC_CHOICE,
    RUN_OVER,
)

# 需要模型参与决策的决策点（RUN_OVER 不需要）
DECISION_POINTS = tuple(d for d in ALL_DECISION_POINTS if d != RUN_OVER)


def grid_kind(fair: FairObservation) -> str:
    """区分"必选 k 张"与"任意多选"。

    min == max > 0 -> 必选 k 张；否则（含 min=0 的"可以跳过"）-> 任意多选。
    """
    s = fair.screen_state
    if s.max_select > 0 and s.min_select == s.max_select:
        return SELECT_CARD_MUST_K
    return SELECT_CARD_ANY


def identify(fair: FairObservation) -> str:
    """返回决策点名。无法识别时抛 UnknownDecisionPoint。

    **刻意不静默降级**：兜底到 generic_choice 只发生在"界面确实给出了
    一组选项"的情况下（最后一条），而不是"我们不认识这个界面"——
    后者必须炸出来，否则会静默产生垃圾数据。
    """
    screen = fair.screen
    s = fair.screen_state

    if screen == SCREEN_GAME_OVER:
        return RUN_OVER

    if screen == SCREEN_MAP:
        if fair.map and fair.map.reachable:
            return MAP_NODE
        return RUN_OVER

    if screen == SCREEN_CARD_REWARD:
        return CARD_REWARD

    if screen == SCREEN_GRID:
        return grid_kind(fair)

    if screen == SCREEN_COMBAT_REWARD:
        if s.reward_relics:
            return RELIC_SELECT
        if s.reward_cards:
            return CARD_REWARD
        if s.options or s.shop_items:
            return GENERIC_CHOICE
        return RUN_OVER

    if screen == SCREEN_EVENT:
        return EVENT_OPTION

    if screen == SCREEN_SHOP_ROOM:
        return SHOP

    if screen == SCREEN_REST:
        return REST_SITE

    if screen == SCREEN_NEOW:
        return NEOW_BONUS

    if screen == SCREEN_BOSS_RELIC:
        return RELIC_SELECT

    if fair.in_combat and screen == SCREEN_NONE:
        # 战斗内的"选目标"由 mod 上报的 pending_target 决定；公平视图里
        # 没有该字段时视为常规出牌。
        return COMBAT_PLAY

    if s.options:
        return GENERIC_CHOICE

    raise UnknownDecisionPoint(
        f"screen={screen!r} in_combat={fair.in_combat} has no known decision point"
    )


__all__ = [
    "ALL_DECISION_POINTS",
    "CARD_REWARD",
    "COMBAT_PLAY",
    "DECISION_POINTS",
    "EVENT_OPTION",
    "GENERIC_CHOICE",
    "MAP_NODE",
    "NEOW_BONUS",
    "RELIC_SELECT",
    "REST_SITE",
    "RUN_OVER",
    "SELECT_CARD_ANY",
    "SELECT_CARD_MUST_K",
    "SELECT_TARGET",
    "SHOP",
    "grid_kind",
    "identify",
]
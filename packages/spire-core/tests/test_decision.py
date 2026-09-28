"""决策点识别。见 docs/11-testing.md#决策点识别。"""

from __future__ import annotations

import pytest

from fixtures import combat_observation, grid_observation, map_observation

from spire_core import fairness
from spire_core.decision import (
    CARD_REWARD,
    COMBAT_PLAY,
    EVENT_OPTION,
    GENERIC_CHOICE,
    MAP_NODE,
    NEOW_BONUS,
    RELIC_SELECT,
    REST_SITE,
    RUN_OVER,
    SELECT_CARD_ANY,
    SELECT_CARD_MUST_K,
    SHOP,
    identify,
)
from spire_core.errors import UnknownDecisionPoint
from spire_core.model import RawObservation


def fair(raw):
    return fairness.filter_(RawObservation.from_dict(raw))


def with_screen(raw, screen, **screen_state):
    raw = dict(raw)
    raw["screen"] = screen
    raw["screen_state"] = screen_state
    if screen != "NONE":
        raw["in_combat"] = False
        raw["combat"] = None
    return raw


def test_combat_play():
    assert identify(fair(combat_observation())) == COMBAT_PLAY


def test_map_node():
    assert identify(fair(map_observation())) == MAP_NODE


def test_map_without_reachable_is_run_over():
    assert identify(fair(map_observation(reachable=[]))) == RUN_OVER


def test_grid_must_k_vs_any():
    assert identify(fair(grid_observation(min_select=2, max_select=2))) == SELECT_CARD_MUST_K
    raw = grid_observation(min_select=0, max_select=3)
    assert identify(fair(raw)) == SELECT_CARD_ANY
    raw2 = grid_observation(min_select=1, max_select=3)
    assert identify(fair(raw2)) == SELECT_CARD_ANY


def test_card_reward_screen():
    raw = with_screen(combat_observation(), "CARD_REWARD",
                      reward_cards=[{"id": "Anger", "name": "Anger", "type": "ATTACK"}])
    assert identify(fair(raw)) == CARD_REWARD


def test_combat_reward_branches():
    base = combat_observation()
    relic = with_screen(base, "COMBAT_REWARD", reward_relics=[{"id": "Sozu"}])
    assert identify(fair(relic)) == RELIC_SELECT

    cards = with_screen(base, "COMBAT_REWARD",
                        reward_cards=[{"id": "Anger", "name": "Anger"}])
    assert identify(fair(cards)) == CARD_REWARD

    opts = with_screen(base, "COMBAT_REWARD", options=["Proceed"])
    assert identify(fair(opts)) == GENERIC_CHOICE

    empty = with_screen(base, "COMBAT_REWARD")
    assert identify(fair(empty)) == RUN_OVER


def test_event_shop_rest_neow_boss_relic():
    base = combat_observation()
    assert identify(fair(with_screen(base, "EVENT", options=["x"]))) == EVENT_OPTION
    assert identify(fair(with_screen(base, "SHOP_ROOM"))) == SHOP
    assert identify(fair(with_screen(base, "REST", rest_options=["Rest"]))) == REST_SITE
    assert identify(fair(with_screen(base, "REST", rest_options=[]))) == RUN_OVER
    assert identify(fair(with_screen(
        base, "REST", options=["Proceed"], rest_options=[]
    ))) == REST_SITE
    assert identify(fair(with_screen(base, "NEOW", neow_options=["a"]))) == NEOW_BONUS
    assert identify(fair(with_screen(base, "BOSS_RELIC",
                                     reward_relics=[{"id": "Sozu"}]))) == RELIC_SELECT


def test_game_over_is_run_over():
    assert identify(fair(with_screen(combat_observation(), "GAME_OVER"))) == RUN_OVER


def test_options_fallback_to_generic_choice():
    raw = with_screen(combat_observation(), "NONE", options=["Continue"])
    raw["in_combat"] = False
    raw["combat"] = None
    assert identify(fair(raw)) == GENERIC_CHOICE


def test_unknown_screen_raises():
    raw = with_screen(combat_observation(), "SOMETHING_NEW")
    with pytest.raises(UnknownDecisionPoint):
        identify(fair(raw))

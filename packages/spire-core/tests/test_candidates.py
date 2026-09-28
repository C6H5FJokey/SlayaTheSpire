"""候选枚举边界。见 docs/11-testing.md#候选枚举。"""

from __future__ import annotations

import pytest

from fixtures import combat_observation, grid_observation, map_observation, player

from spire_core import actions as A
from spire_core import fairness
from spire_core.candidates import enumerate_candidates
from spire_core.decision import (
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
from spire_core.errors import NoCandidates
from spire_core.model import RawObservation


def fair(raw):
    return fairness.filter_(RawObservation.from_dict(raw))


def cids(cands):
    return [c.cid for c in cands]


def test_combat_enumerates_play_potion_end_turn():
    f = fair(combat_observation())
    cands = enumerate_candidates(f, COMBAT_PLAY)
    ids = cids(cands)
    # Strike(h0) 需要目标 -> play:h0->m0；Defend(h2) 无目标 -> play:h2
    assert "play:h0->m0" in ids
    assert "play:h2" in ids
    assert "potion:p0->m0" in ids
    assert ids[-1] == A.END_TURN


def test_zero_energy_only_end_turn():
    raw = combat_observation(energy=0)
    for i, c in enumerate(raw["combat"]["hand"]):
        c["is_playable"] = False
    f = fair(raw)
    ids = cids(enumerate_candidates(f, COMBAT_PLAY))
    # 0 能量：不能出牌，但药水仍然可用（药水不耗能量）
    assert not any(cid.startswith("play:") for cid in ids)
    assert ids[-1] == A.END_TURN


def test_empty_hand_only_end_turn():
    raw = combat_observation(hand=[])
    f = fair(raw)
    ids = cids(enumerate_candidates(f, COMBAT_PLAY))
    assert not any(cid.startswith("play:") for cid in ids)
    assert ids[-1] == A.END_TURN


def test_dead_enemy_is_not_a_valid_target():
    raw = combat_observation(monsters=[])
    f = fair(raw)
    ids = cids(enumerate_candidates(f, COMBAT_PLAY))
    # Strike 需要单体目标但场上无活敌人 -> 不可打
    assert not any(cid.startswith("play:h0") for cid in ids)
    assert "play:h2" in ids


def test_dead_enemy_hp_zero_excluded():
    raw = combat_observation()
    raw["combat"]["monsters"].append(
        {
            "index": 1,
            "id": "Cultist",
            "name": "Cultist",
            "hp": 0,
            "max_hp": 50,
            "block": 0,
            "half_dead": False,
            "is_gone": False,
            "powers": [],
            "intent": None,
        }
    )
    f = fair(raw)
    ids = cids(enumerate_candidates(f, COMBAT_PLAY))
    assert "play:h0->m0" in ids
    assert "play:h0->m1" not in ids


def test_both_enemies_are_targetable():
    raw = combat_observation()
    raw["combat"]["monsters"].append(
        {
            "index": 1,
            "id": "Cultist",
            "name": "Cultist",
            "hp": 50,
            "max_hp": 50,
            "block": 0,
            "half_dead": False,
            "is_gone": False,
            "powers": [],
            "intent": None,
        }
    )
    f = fair(raw)
    ids = cids(enumerate_candidates(f, COMBAT_PLAY))
    assert "play:h0->m0" in ids and "play:h0->m1" in ids


def test_full_potions_still_enumerate_usable_ones():
    potions = [
        {"id": "FirePotion", "can_use": True, "requires_target": True},
        {"id": "BlockPotion", "can_use": True, "requires_target": False},
        {"id": "FlexPotion", "can_use": False, "requires_target": False},
    ]
    f = fair(combat_observation(potions=potions))
    ids = cids(enumerate_candidates(f, COMBAT_PLAY))
    assert "potion:p0->m0" in ids
    assert "potion:p1" in ids
    assert not any(cid.startswith("potion:p2") for cid in ids)


def test_select_target_candidates():
    f = fair(combat_observation())
    assert cids(enumerate_candidates(f, SELECT_TARGET)) == ["target:m0"]


def test_card_select_must_k_excludes_already_picked():
    f = fair(grid_observation(min_select=2, max_select=2))
    cands = enumerate_candidates(f, SELECT_CARD_MUST_K)
    assert cids(cands) == ["card:hand:0", "card:hand:1", "card:hand:2"]
    again = enumerate_candidates(
        f, SELECT_CARD_MUST_K, context={"selection_picked": ["card:hand:1"]}
    )
    assert cids(again) == ["card:hand:0", "card:hand:2"]


def test_card_select_any_uses_same_candidates():
    f = fair(grid_observation(min_select=0, max_select=3))
    assert cids(enumerate_candidates(f, SELECT_CARD_ANY)) == [
        "card:hand:0",
        "card:hand:1",
        "card:hand:2",
    ]


def test_empty_optional_card_selection_submits_empty_selection():
    raw = combat_observation()
    raw["screen"] = "GRID"
    raw["in_combat"] = True
    raw["screen_state"] = {
        "min_select": 0,
        "max_select": 0,
        "select_cards": [],
    }
    f = fair(raw)
    cands = enumerate_candidates(f, SELECT_CARD_ANY)
    assert [c.cid for c in cands] == [A.EMPTY_SELECTION]
    assert cands[0].action == A.select_cards([])


def test_map_node_candidates_follow_reachable_order():
    f = fair(map_observation(reachable=["n4_3", "n3_3"]))
    ids = cids(enumerate_candidates(f, MAP_NODE))
    assert ids == ["node:n4_3", "node:n3_3"]


def test_map_node_description_mentions_downstream():
    f = fair(map_observation())
    cands = enumerate_candidates(f, MAP_NODE)
    desc = cands[0].description
    assert "Monster" in desc
    assert "Elite" in desc or "Rest" in desc or "Shop" in desc
    assert "HP" in desc


def test_card_reward_includes_skip():
    raw = combat_observation()
    raw["screen"] = "CARD_REWARD"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {
        "reward_cards": [
            {"index": 0, "id": "Anger", "name": "Anger", "type": "ATTACK", "cost": 0,
             "cost_for_turn": 0, "upgrades": 0, "rarity": "COMMON", "exhausts": False,
             "ethereal": False, "is_playable": True, "target_type": "ENEMY",
             "uuid": "r0", "text": "Deal 6 damage.", "damage": 6, "block": 0,
             "magic_number": 0},
        ]
    }
    f = fair(raw)
    assert cids(enumerate_candidates(f, CARD_REWARD)) == ["reward:0", "skip"]


def test_shop_skips_unaffordable_and_offers_leave():
    raw = combat_observation()
    raw["screen"] = "SHOP_ROOM"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {
        "shop_items": [
            {"index": 0, "kind": "card", "id": "Anger", "price": 50, "affordable": True},
            {"index": 1, "kind": "relic", "id": "Anchor", "price": 300, "affordable": False},
        ]
    }
    f = fair(raw)
    assert cids(enumerate_candidates(f, SHOP)) == ["buy:0", A.LEAVE]


def test_rest_site_single_option_does_not_crash():
    raw = combat_observation()
    raw["screen"] = "REST"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"rest_options": ["Rest"]}
    f = fair(raw)
    assert cids(enumerate_candidates(f, REST_SITE)) == [A.REST_HEAL]


def test_rest_site_default_two_options():
    raw = combat_observation()
    raw["screen"] = "REST"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"rest_options": ["Rest", "Smith"]}
    f = fair(raw)
    assert cids(enumerate_candidates(f, REST_SITE)) == [A.REST_HEAL, A.REST_SMITH]


def test_rest_site_after_choice_offers_proceed():
    raw = combat_observation()
    raw["screen"] = "REST"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"options": ["Proceed"], "rest_options": []}
    f = fair(raw)
    assert cids(enumerate_candidates(f, REST_SITE)) == ["proceed"]


def test_event_option_candidates():
    raw = combat_observation()
    raw["screen"] = "EVENT"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"options": ["Take the gold", "Leave"]}
    f = fair(raw)
    assert cids(enumerate_candidates(f, EVENT_OPTION)) == ["option:0", "option:1"]


def test_relic_select_candidates():
    raw = combat_observation()
    raw["screen"] = "BOSS_RELIC"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"reward_relics": [{"id": "Sozu", "counter": -1},
                                             {"id": "BlackBlood", "counter": -1}]}
    f = fair(raw)
    assert cids(enumerate_candidates(f, RELIC_SELECT)) == ["relic:0", "relic:1"]


def test_neow_and_generic_choice():
    raw = combat_observation()
    raw["screen"] = "NEOW"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"neow_options": ["Three cards", "250 gold"]}
    f = fair(raw)
    assert cids(enumerate_candidates(f, NEOW_BONUS)) == ["neow:0", "neow:1"]


def test_generic_choice_uses_options():
    raw = combat_observation()
    raw["screen"] = "NONE"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"options": ["Continue", "Skip"]}
    f = fair(raw)
    assert cids(enumerate_candidates(f, GENERIC_CHOICE)) == ["choice:0", "choice:1"]


def test_empty_screen_raises_no_candidates():
    raw = map_observation(reachable=[])
    raw["screen"] = "EVENT"
    raw["screen_state"] = {"options": []}
    raw.pop("map")
    f = fair(raw)
    with pytest.raises(NoCandidates):
        enumerate_candidates(f, EVENT_OPTION)


def test_unknown_decision_point_raises():
    f = fair(combat_observation())
    with pytest.raises(NoCandidates):
        enumerate_candidates(f, "nope")

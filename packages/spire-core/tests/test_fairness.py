"""无泄漏断言 —— 这是本项目最重要的一组测试。

见 docs/11-testing.md#无泄漏断言：任何新增字段都要补一条对应的负向断言。
"""

from __future__ import annotations

import json

from fixtures import combat_observation

from spire_core import fairness, serialize
from spire_core.model import RawObservation


def _fair():
    raw = RawObservation.from_dict(combat_observation())
    return fairness.filter_(raw)


def test_fair_view_drops_seed():
    fair = _fair()
    assert not hasattr(fair, "run_seed")
    assert "run_seed" not in json.dumps(serialize.state(fair))


def test_draw_pile_is_multiset_without_order():
    raw = RawObservation.from_dict(combat_observation())
    fair = fairness.filter_(raw)

    # raw 的顺序是 Strike, Defend, Strike, AscendersBane
    assert [c.id for c in raw.combat.draw_pile] == [
        "Strike_R",
        "Defend_R",
        "Strike_R",
        "AscendersBane",
    ]
    # fair 必须是排序后的多重集
    keys = [(s.id, s.upgrades) for s in fair.zones.draw]
    assert keys == sorted(keys)
    assert [(s.id, s.count) for s in fair.zones.draw] == [
        ("AscendersBane", 1),
        ("Defend_R", 1),
        ("Strike_R", 2),
    ]


def test_deck_and_discard_are_multisets_too():
    fair = _fair()
    for stacks in (fair.zones.discard, fair.zones.exhaust, fair.zones.deck):
        keys = [(s.id, s.upgrades) for s in stacks]
        assert keys == sorted(keys)


def test_monster_intent_has_no_base_damage_or_future_moves():
    fair = _fair()
    for m in fair.monsters:
        assert m.intent is not None
        assert not hasattr(m.intent, "base_damage")
        assert not hasattr(m, "move_history")
        assert not hasattr(m, "upcoming_moves")


def test_serialized_state_has_no_forbidden_keys():
    fair = _fair()
    blob = serialize.state_json(fair)
    for bad in fairness.FORBIDDEN_KEYS:
        assert bad not in blob, f"{bad} leaked into state"


def test_hand_keeps_order_and_short_ids():
    fair = _fair()
    assert [c.index for c in fair.hand] == [0, 1, 2]
    state = serialize.state(fair)
    assert [c["id"] for c in state["hand"]] == ["h0", "h1", "h2"]


def test_unknown_fairness_mode_rejected():
    raw = RawObservation.from_dict(combat_observation())
    try:
        fairness.filter_(raw, mode="cheat")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown fairness mode")
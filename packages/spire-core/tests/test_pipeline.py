"""端到端构题 + 裁决（假 LayaResult）。见 docs/11-testing.md#管线。"""

from __future__ import annotations

import pytest

from fixtures import combat_observation, grid_observation, map_observation

from spire_core import actions as A
from spire_core import fairness, pipeline
from spire_core.decision import COMBAT_PLAY, MAP_NODE, SELECT_CARD_ANY
from spire_core.pipeline import RunOver
from spire_core.model import RawObservation
from spire_core.types import LayaResult


def fair(raw):
    return fairness.filter_(RawObservation.from_dict(raw))


def answer(qid, choice, probs=None, conf=0.8):
    return {
        qid: {
            "type": "choice",
            "choice": choice,
            "probabilities": probs or {choice: conf, "__other__": 1 - conf},
            "confidence": conf,
            "answer_confidence": conf,
        }
    }


def test_plan_lists_candidates_as_criteria_in_order():
    f = fair(combat_observation())
    plan = pipeline.make_plan(f)
    assert plan.decision_point == COMBAT_PLAY
    assert plan.question_id == "q_action"
    criteria = list(plan.questions["q_action"]["criteria"].keys())
    assert criteria == plan.candidate_ids
    assert plan.state["mode"] == "combat"


def test_committed_campfire_transition_needs_no_plan():
    raw = combat_observation()
    raw["screen"] = "REST"
    raw["in_combat"] = False
    raw["combat"] = None
    raw["screen_state"] = {"rest_options": []}

    with pytest.raises(RunOver):
        pipeline.make_plan(fair(raw))


def test_resolve_choice_produces_action():
    f = fair(combat_observation())
    plan = pipeline.make_plan(f)
    cid = plan.candidate_ids[0]
    decision = pipeline.resolve(plan, LayaResult(answers=answer("q_action", cid)))
    assert decision.chosen == cid
    assert decision.fallback is False
    action = pipeline.action_from_decision(decision)
    assert action.kind == A.KIND_PLAY_CARD
    assert action.args["hand_index"] == 0


def test_resolve_end_turn():
    f = fair(combat_observation())
    plan = pipeline.make_plan(f)
    decision = pipeline.resolve(plan, LayaResult(answers=answer("q_action", A.END_TURN)))
    assert decision.chosen == A.END_TURN
    assert pipeline.action_from_decision(decision).kind == A.KIND_END_TURN


def test_resolve_rejects_answer_outside_candidates():
    f = fair(combat_observation())
    plan = pipeline.make_plan(f)
    with pytest.raises(Exception):
        pipeline.resolve(plan, LayaResult(answers=answer("q_action", "made_up")))


def test_fallback_decision_is_marked_and_actionable():
    f = fair(combat_observation())
    plan = pipeline.make_plan(f)
    decision = pipeline.fallback_decision(plan, f, "laya down")
    assert decision.fallback is True
    assert decision.fallback_reason == "laya down"
    action = pipeline.action_from_decision(decision)
    assert action.kind in A.ACTION_KINDS


def test_run_over_raises():
    raw = map_observation(reachable=[])
    with pytest.raises(pipeline.RunOver):
        pipeline.make_plan(fair(raw))


def test_map_plan_and_resolve():
    f = fair(map_observation(reachable=["n4_3"]))
    plan = pipeline.make_plan(f)
    assert plan.decision_point == MAP_NODE
    decision = pipeline.resolve(plan, LayaResult(answers=answer("q_map", "node:n4_3")))
    action = pipeline.action_from_decision(decision)
    assert action.kind == A.KIND_SELECT_MAP_NODE
    assert action.args["node"] == "n4_3"


def test_select_card_any_uses_score_and_topk():
    f = fair(grid_observation(min_select=0, max_select=2))
    plan = pipeline.make_plan(f)
    assert plan.decision_point == SELECT_CARD_ANY
    assert len(plan.score_map) == 3
    # 只把 card:hand:1 评为 good(3)，其余 neutral(2) -> 只选它
    answers = {}
    for qid, cid in plan.score_map.items():
        level = "3" if cid == "card:hand:1" else "2"
        answers[qid] = {
            "type": "score",
            "score": float(level),
            "probabilities": {level: 1.0},
            "confidence": 1.0,
            "answer_confidence": 1.0,
        }
    decision = pipeline.resolve(plan, LayaResult(answers=answers))
    assert decision.chosen_ids == ["card:hand:1"]
    action = pipeline.action_from_decision(decision)
    assert action.kind == A.KIND_SELECT_CARDS
    assert action.args["indices"] == [["hand", 1]]


def test_plan_budget_report_is_recorded():
    f = fair(combat_observation())
    plan = pipeline.make_plan(f)
    assert plan.budget["question_count"] >= 1
    assert plan.budget["state_chars"] > 0

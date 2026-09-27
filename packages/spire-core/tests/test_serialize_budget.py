"""序列化确定性 + 预算。见 docs/11-testing.md#序列化确定性 / #预算。"""

from __future__ import annotations

import json

from fixtures import combat_observation

from spire_core import budget, fairness, serialize
from spire_core.ids import canonical_json
from spire_core.model import RawObservation


def _fair():
    return fairness.filter_(RawObservation.from_dict(combat_observation()))


def test_serialization_is_byte_stable():
    fair = _fair()
    first = serialize.state_json(fair)
    for _ in range(50):
        assert serialize.state_json(fair) == first


def test_serialization_ignores_raw_dict_key_order():
    base = combat_observation()
    shuffled = {k: base[k] for k in reversed(list(base.keys()))}
    a = serialize.state_json(fairness.filter_(RawObservation.from_dict(base)))
    b = serialize.state_json(fairness.filter_(RawObservation.from_dict(shuffled)))
    assert a == b


def test_no_float_artifacts_in_state():
    fair = _fair()
    blob = serialize.state_json(fair)
    assert "0.30000000000000004" not in blob


def test_state_fits_budget_easily():
    fair = _fair()
    state = serialize.state(fair)
    report = budget.check(state, {})
    assert report.state_chars < budget.MAX_STATE_CHARS


def test_enforce_truncates_candidates_deterministically():
    fair = _fair()
    state = serialize.state(fair)
    criteria = {f"play:h{i}": f"option {i}" for i in range(150)}
    questions = {"q_action": {"type": "choice", "instructions": "x", "criteria": criteria}}

    _s, q, report = budget.enforce(fair, state, questions, max_candidates_per_question=100)
    kept = list(q["q_action"]["criteria"].keys())
    assert len(kept) == 100
    assert kept == [f"play:h{i}" for i in range(100)]
    assert len(report.truncated_candidates) == 50


def test_enforce_raises_when_state_too_large():
    fair = _fair()
    state = {"blob": "x" * (budget.MAX_STATE_CHARS + 10)}
    try:
        budget.enforce(fair, state, {})
    except budget.BudgetExceeded:
        return
    raise AssertionError("expected BudgetExceeded")


def test_too_many_questions_raises():
    fair = _fair()
    state = serialize.state(fair)
    questions = {
        f"q{i}": {"type": "choice", "instructions": "", "criteria": {"a": "a"}}
        for i in range(budget.MAX_QUESTIONS + 1)
    }
    try:
        budget.enforce(fair, state, questions)
    except budget.BudgetExceeded:
        return
    raise AssertionError("expected BudgetExceeded for too many questions")


def test_canonical_json_is_key_sorted():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert json.loads(canonical_json({"a": 2, "b": 1})) == {"a": 2, "b": 1}
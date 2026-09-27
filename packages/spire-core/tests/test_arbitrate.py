"""答案解析与子选择裁决。见 docs/11-testing.md#裁决。"""

from __future__ import annotations

import pytest

from spire_core.arbitrate import (
    argmax_by_insertion_order,
    expected_level,
    parse_answers,
    pick_choice,
    pick_topk,
)
from spire_core.errors import ParseError


def answers(payload):
    return parse_answers(payload)


def test_parse_dict_and_list_probabilities():
    """早期契约的 `answer` / `label` 写法（老记录回放要能读）。"""
    p = answers(
        {
            "q1": {"answer": "a", "probabilities": {"a": 0.7, "b": 0.3},
                   "confidence": 0.7},
            "q2": {"answer": "1", "probabilities": [0.1, 0.2, 0.7],
                   "confidence": 0.7},
            "bad": "not-a-dict",
        }
    )
    assert set(p) == {"q1", "q2"}
    assert p["q1"].probabilities == {"a": 0.7, "b": 0.3}
    assert p["q2"].probabilities == {"0": 0.1, "1": 0.2, "2": 0.7}
    assert p["q1"].answer_confidence == 0.7


def test_pick_choice_rebuilds_probs_in_allowed_order():
    p = answers({"q": {"answer": "b", "probabilities": {"b": 0.9, "a": 0.1},
                       "confidence": 0.9}})
    chosen, conf, probs = pick_choice(p, "q", ["a", "b"])
    assert chosen == "b"
    assert conf == 0.9
    assert list(probs.keys()) == ["a", "b"]


def test_pick_choice_missing_question():
    with pytest.raises(ParseError):
        pick_choice({}, "q", ["a"])


def test_selected_answer_reads_the_real_laya_shape():
    """真实 Laya 按题型分派：choice 放 `choice`，noul 放 P(true)，score 只有数值。"""
    p = answers(
        {
            "q_choice": {"type": "choice", "choice": "play:h1->m0",
                         "probabilities": {"play:h0->m0": 0.3, "play:h1->m0": 0.7},
                         "confidence": 0.4, "answer_confidence": 0.7},
            "q_score": {"type": "score", "score": 3.2,
                        "probabilities": {"0": 0.0, "1": 0.1, "2": 0.1, "3": 0.6, "4": 0.2},
                        "confidence": 0.5, "answer_confidence": 0.6},
            "q_noul": {"type": "noul", "noul": 0.82, "confidence": 0.82,
                       "answer_confidence": 0.82},
        }
    )
    assert p["q_choice"].answer == "play:h1->m0"
    # `answer_confidence` 是校准过的那个；`confidence` 对 choice 是归一化熵
    assert p["q_choice"].answer_confidence == 0.7
    assert p["q_noul"].answer == "true"
    assert p["q_noul"].probabilities == {"false": pytest.approx(0.18),
                                         "true": pytest.approx(0.82)}
    # score 题没有 key：期望等级由 probabilities 算（等级就是下标）
    assert expected_level(p["q_score"].probabilities) == pytest.approx(2.9)


def test_noul_below_half_maps_to_false():
    p = answers({"q": {"type": "noul", "noul": 0.2}})
    assert p["q"].answer == "false"


def test_legacy_answer_and_label_keys_still_parse():
    assert answers({"q": {"answer": "b"}})["q"].answer == "b"
    assert answers({"q": {"label": "c"}})["q"].answer == "c"


def test_choice_without_probabilities_still_picks():
    p = answers({"q": {"type": "choice", "choice": "b"}})
    chosen, _conf, probs = pick_choice(p, "q", ["a", "b"])
    assert chosen == "b"
    assert probs == {"a": 0.0, "b": 0.0}


def test_pick_choice_answer_outside_criteria():
    p = answers({"q": {"answer": "zzz", "probabilities": {}, "confidence": 0.5}})
    with pytest.raises(ParseError):
        pick_choice(p, "q", ["a", "b"])


def test_tie_break_is_insertion_order_not_alphabetical():
    probs = {"b": 0.5, "a": 0.5}
    assert argmax_by_insertion_order(["b", "a"], probs) == "b"
    assert argmax_by_insertion_order(["a", "b"], probs) == "a"


def test_expected_level_normalizes_weights():
    assert expected_level({"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0, "4": 0.0}) == 3.0
    assert expected_level({"0": 0.5, "1": 0.5}) == 0.5
    assert expected_level({}) == 0.0


def test_pick_topk_keeps_threshold_and_forced_min():
    expected = {"a": 4.0, "b": 3.0, "c": 1.0, "d": 0.0}
    assert pick_topk(expected, 0, 4, order=["a", "b", "c", "d"]) == ["a", "b"]
    assert pick_topk(expected, 3, 4, order=["a", "b", "c", "d"]) == ["a", "b", "c"]


def test_pick_topk_caps_at_k_max():
    expected = {"a": 4.0, "b": 4.0, "c": 4.0}
    assert pick_topk(expected, 0, 2, order=["a", "b", "c"]) == ["a", "b"]


def test_pick_topk_ties_follow_order():
    expected = {"x": 3.0, "y": 3.0}
    assert pick_topk(expected, 0, 1, order=["x", "y"]) == ["x"]
    assert pick_topk(expected, 0, 1, order=["y", "x"]) == ["y"]


def test_pick_topk_empty_when_all_neutral_and_kmin_zero():
    # 默认阈值是 "good"(3.0)：neutral 及以下都不选，所以可以"什么都不选"
    expected = {"a": 2.0, "b": 1.0}
    assert pick_topk(expected, 0, 2, order=["a", "b"]) == []


def test_pick_topk_threshold_is_configurable():
    expected = {"a": 2.0, "b": 1.0}
    assert pick_topk(expected, 0, 2, order=["a", "b"],
                     threshold_level=2.0) == ["a"]
    assert pick_topk(expected, 0, 2, order=["a", "b"],
                     threshold_level=2.5) == []

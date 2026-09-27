"""回放可复现性。见 docs/11-testing.md#回放。"""

from __future__ import annotations

from spire_core.replay import replay_all, replay_decision, score_map_from_row


def choice_row(chosen="end_turn"):
    return {
        "row_id": "run-1#c1#t3#s1",
        "decision_point": "combat_play",
        "questions": {
            "q_action": {
                "type": "choice",
                "instructions": "pick",
                "criteria": {"play:h0->m0": "strike", "end_turn": "end"},
            }
        },
        "candidate_ids": ["play:h0->m0", "end_turn"],
        "answers": {
            "q_action": {
                "type": "choice",
                "choice": "end_turn",
                "probabilities": {"play:h0->m0": 0.4, "end_turn": 0.6},
                "confidence": 0.6,
                "answer_confidence": 0.6,
            }
        },
        "chosen": chosen,
        "chosen_ids": [chosen],
    }


def score_row():
    return {
        "row_id": "run-1#c1#t1#s9",
        "decision_point": "select_card_any",
        "questions": {
            "q_card_card_hand_0": {
                "type": "score",
                "criteria": ["very bad", "bad", "neutral", "good", "very good"],
            },
            "q_card_card_hand_1": {
                "type": "score",
                "criteria": ["very bad", "bad", "neutral", "good", "very good"],
            },
        },
        "candidate_ids": ["card:hand:0", "card:hand:1"],
        "answers": {
            "q_card_card_hand_0": {
                "type": "score",
                "score": 4.0,
                "probabilities": {"0": 0.0, "1": 0.0, "2": 0.1, "3": 0.1, "4": 0.8},
                "confidence": 0.8,
                "answer_confidence": 0.8,
            },
            "q_card_card_hand_1": {
                "type": "score",
                "score": 1.0,
                "probabilities": {"0": 0.1, "1": 0.8, "2": 0.1, "3": 0.0, "4": 0.0},
                "confidence": 0.8,
                "answer_confidence": 0.8,
            },
        },
        "chosen": "card:hand:0",
        "chosen_ids": ["card:hand:0"],
        "k_min": 0,
        "k_max": 2,
    }


def test_score_map_recovers_ids_with_underscore_candidate_names():
    smap = score_map_from_row(score_row())
    assert smap == {
        "q_card_card_hand_0": "card:hand:0",
        "q_card_card_hand_1": "card:hand:1",
    }


def test_choice_replay_matches():
    r = replay_decision(choice_row())
    assert r.ok and r.recomputed == "end_turn"


def test_choice_replay_detects_mismatch():
    r = replay_decision(choice_row(chosen="play:h0->m0"))
    assert not r.ok
    assert r.recorded == "play:h0->m0"
    assert r.recomputed == "end_turn"


def test_score_replay_matches():
    r = replay_decision(score_row())
    assert r.ok, r.reason
    assert r.recomputed_ids == ["card:hand:0"]


def test_score_replay_detects_mismatch():
    row = score_row()
    row["chosen_ids"] = ["card:hand:1"]
    row["chosen"] = "card:hand:1"
    r = replay_decision(row)
    assert not r.ok


def test_replay_all_summarizes():
    out = replay_all([choice_row(), score_row(), choice_row(chosen="play:h0->m0")])
    assert out["total"] == 3
    assert len(out["mismatches"]) == 1
    assert not out["ok"]


def test_replay_missing_criteria_reports_reason():
    row = choice_row()
    row["questions"]["q_action"]["criteria"] = {}
    r = replay_decision(row)
    assert not r.ok
    assert r.reason

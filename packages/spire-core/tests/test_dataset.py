"""数据集构造：切分防泄漏、过滤、choice->noul 展开。见 docs/08-dataset.md。"""

from __future__ import annotations

from spire_core.dataset import (
    SPLIT_TEST,
    SPLIT_TRAIN,
    SPLIT_VAL,
    expand_choice_to_noul,
    filter_rows,
    make_row,
    room_key,
    row_hash,
    split_of,
    summarize_rows,
)


def _row(**over):
    base = dict(
        run_id="run-0001",
        seq=1,
        decision_point="combat_play",
        room={"act": 1, "floor": 7, "node": 5, "type": "MONSTER",
              "combat_instance": 1, "post_sl": False},
        context={"character": "IRONCLAD", "ascension": 0},
        source="agent",
        split="train",
        state={"mode": "combat", "combat": {"turn": 3}},
        questions={"q_action": {"type": "choice", "instructions": "pick",
                                "criteria": {"play:h0->m0": "a", "end_turn": "b"}}},
        candidate_ids=["play:h0->m0", "end_turn"],
        labels={"q_action": "play:h0->m0"},
        meta={"agent_fallback": False},
    )
    base.update(over)
    return make_row(**base)


def test_row_id_and_shape():
    row = _row()
    assert row["row_id"] == "run-0001#c1#t3#s1"
    for key in ("run_id", "seq", "decision_point", "room", "context", "source",
                "split", "state", "questions", "candidate_ids", "labels", "meta",
                "outcome"):
        assert key in row


def test_split_is_deterministic_and_run_stable():
    a = split_of("run-0001", split_seed=42)
    assert a == split_of("run-0001", split_seed=42)
    assert a in (SPLIT_TRAIN, SPLIT_VAL, SPLIT_TEST)


def test_split_never_splits_a_run_across_buckets():
    buckets = {}
    for i in range(400):
        rid = f"run-{i:04d}"
        buckets.setdefault(split_of(rid, split_seed=7), []).append(rid)
    # 每个 run 只属于一个桶；三桶都非空（比例 0.1/0.1）
    assert len(buckets) == 3
    all_runs = [r for v in buckets.values() for r in v]
    assert len(all_runs) == len(set(all_runs))


def test_split_ratios_are_roughly_respected():
    vals = [split_of(f"run-{i}", split_seed=1) for i in range(2000)]
    test = vals.count(SPLIT_TEST) / len(vals)
    val = vals.count(SPLIT_VAL) / len(vals)
    assert 0.06 < test < 0.15
    assert 0.06 < val < 0.15


def test_room_key_format():
    assert room_key(1, 7, 5, 2) == "a1_f7_n5_c2"


def test_expand_choice_to_noul_is_lossless():
    row = _row()
    rows = expand_choice_to_noul(row)
    assert len(rows) == 2
    assert sum(1 for r in rows if r["labels"]["q_action"] is True) == 1
    trues = [r for r in rows if r["labels"]["q_action"] is True]
    assert trues[0]["questions"]["q_action"]["type"] == "noul"
    assert "play:h0->m0" in trues[0]["questions"]["q_action"]["instructions"]
    # state 与原始行完全一致
    assert all(r["state"] == row["state"] for r in rows)
    assert all(r["meta"]["derived_from"] == row["row_id"] for r in rows)


def test_expand_skips_non_choice_rows():
    row = _row(questions={"q_card_a": {"type": "score", "criteria": [1, 2]}},
               labels={"q_card_a": "1"})
    assert expand_choice_to_noul(row) == []


def test_filter_drops_post_sl_fallback_and_unmatched():
    good = _row()
    post_sl = _row(room={"act": 1, "floor": 7, "node": 5, "type": "MONSTER",
                         "combat_instance": 2, "post_sl": True})
    fallback = _row(meta={"agent_fallback": True})
    unmatched = _row(meta={"matched": False})
    kept, dropped = filter_rows([good, post_sl, fallback, unmatched])
    assert len(kept) == 1
    assert dropped == {"post_sl": 1, "fallback": 1, "unmatched": 1}


def test_filter_can_be_disabled():
    post_sl = _row(room={"act": 1, "floor": 7, "node": 5, "type": "MONSTER",
                         "combat_instance": 2, "post_sl": True})
    kept, dropped = filter_rows([post_sl], exclude_post_sl_rooms=False)
    assert len(kept) == 1 and dropped["post_sl"] == 0


def test_summary_rates():
    human = _row(source="human", meta={"matched": True, "model_answer": "end_turn",
                                       "agreement": False})
    human_hit = _row(source="human", meta={"matched": True, "model_answer": "end_turn",
                                           "agreement": True})
    fb = _row(meta={"agent_fallback": True})
    s = summarize_rows([human, human_hit, fb])
    assert s["rows_total"] == 3
    assert s["by_source"]["human"] == 2
    assert s["by_source"]["agent"] == 1
    assert s["match_rate"] == 1.0
    assert s["agreement_rate"] == 0.5
    assert s["fallback_rate"] == round(1 / 3, 4)
    assert s["post_sl_rows"] == 0


def test_row_hash_ignores_outcome_and_volatile_meta():
    a = _row()
    b = _row()
    b["outcome"] = {"run_won": True, "hp_after": 1, "hp_delta_room": -1,
                    "floor_reached": 50}
    b["meta"] = {"agent_fallback": True, "latency_ms": 999}
    assert row_hash(a) == row_hash(b)
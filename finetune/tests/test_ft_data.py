"""`ft_data.py` 的纯逻辑单测：行 -> 记录 / 训练项的翻译规则，以及确定性 IO。

**不需要 torch / numpy / laya**，`.venv` 里也跑得起来（见 conftest 的说明）。
"""

from __future__ import annotations

import json
import os

import pytest

import ft_data as fd


def test_choice_conversion_keeps_option_order():
    question = {"type": "choice", "instructions": "Pick.",
                "criteria": {"play:h0": "Strike.", "end_turn": "End turn.", "play:h1": "Defend."}}
    internal, options, index = fd.question_to_internal(question, "end_turn")
    assert options == ["play:h0", "end_turn", "play:h1"]      # dict 顺序就是选项顺序
    assert index == 1
    assert internal["t"] == "choice"
    assert internal["ins"] == "Pick."
    assert internal["crit"] == question["criteria"]


def test_instructions_that_are_not_strings_are_json_encoded():
    question = {"type": "choice", "instructions": {"text": "中文", "n": 1},
                "criteria": {"a": "A."}}
    internal, _, _ = fd.question_to_internal(question, "a")
    assert internal["ins"] == json.dumps({"text": "中文", "n": 1}, ensure_ascii=False)


@pytest.mark.parametrize("label,index", [(2, 2), ("1", 1)])
def test_score_conversion(label, index):
    question = {"type": "score", "instructions": "Rate.", "criteria": ["bad", "ok", "good"]}
    internal, options, got = fd.question_to_internal(question, label)
    assert internal["t"] == "score"
    assert options == ["0", "1", "2"]
    assert got == index


@pytest.mark.parametrize("label,index", [(True, 1), ("false", 0), (0, 0), ("yes", 1)])
def test_noul_conversion(label, index):
    question = {"type": "noul", "instructions": "Is it worth it?",
                "criteria": {"false": "no", "true": "yes"}}
    internal, options, got = fd.question_to_internal(question, label)
    assert options == ["false", "true"]
    assert got == index
    assert internal["crit"] == question["criteria"]


def test_single_item_list_label_is_unwrapped():
    """`select_card_must_k` 落盘的是"这次点的那一张"，记成单元素列表。"""
    question = {"type": "choice", "instructions": "Pick.",
                "criteria": {"card:deck:0": "Strike.", "card:deck:1": "Defend."}}
    _, options, index = fd.question_to_internal(question, ["card:deck:1"])
    assert options[index] == "card:deck:1"


def test_multi_select_label_is_refused_not_guessed():
    question = {"type": "choice", "instructions": "Pick.",
                "criteria": {"a": "A.", "b": "B.", "c": "C."}}
    with pytest.raises(fd.SkipRecord) as excinfo:
        fd.question_to_internal(question, ["a", "b"])
    assert excinfo.value.args[0] == fd.SKIP_MULTI_LABEL


def test_label_outside_criteria_is_refused():
    question = {"type": "choice", "instructions": "Pick.", "criteria": {"a": "A.", "b": "B."}}
    with pytest.raises(fd.SkipRecord) as excinfo:
        fd.question_to_internal(question, "zzz")
    assert excinfo.value.args[0] == fd.SKIP_LABEL_UNKNOWN


@pytest.mark.parametrize("questions", [
    {"q": {"type": "choice", "instructions": "Pick."}},
    {"q": {"type": "choice", "instructions": "Pick.", "criteria": {}}},
    {"q": {"type": "weird", "instructions": "Pick.", "criteria": {"a": "A."}}},
])
def test_broken_questions_are_refused(questions):
    with pytest.raises(fd.SkipRecord) as excinfo:
        fd.question_to_internal(questions["q"], "a")
    assert excinfo.value.args[0] == fd.SKIP_NO_QUESTION


def test_option_count_matches_render_options():
    assert fd.question_option_count({"type": "choice", "criteria": {"a": 1, "b": 2}}) == 2
    assert fd.question_option_count({"type": "score", "criteria": ["x", "y", "z"]}) == 3
    assert fd.question_option_count({"type": "noul"}) == 2


def _reasons(rows, **kwargs):
    options = dict(sources=("human",), min_options=2, include_degenerate=False)
    options.update(kwargs)
    return [fd.skip_reason(row, **options) for row in rows]


def test_skip_reason_covers_every_rejection_path(skipped_rows):
    assert _reasons(skipped_rows) == [
        fd.SKIP_SOURCE,          # source=agent 不在 --sources 里
        fd.SKIP_FALLBACK,        # agent_fallback
        fd.SKIP_UNMATCHED,       # matched=false
        fd.SKIP_POST_SL,         # 读档后重记的房间
        fd.SKIP_NO_LABEL,        # 没有人类标签
        None,                    # 标签不在候选里，这一层看不出来（行->记录时才拒）
        None,                    # 多选标签，同上
        fd.SKIP_TOO_FEW_OPTIONS, # 只有一个候选
    ]


def test_degenerate_rows_can_be_kept_explicitly(skipped_rows):
    reasons = _reasons(skipped_rows, include_degenerate=True)
    assert reasons[-1] is None


def test_row_to_record_carries_gold_recorded_and_weight(tiny_rows):
    row = tiny_rows[0]
    record = fd.row_to_record(row, split=fd.SPLIT_TRAIN, weight=1.0)
    gold = record["gold"]["q_action"]
    assert gold["type"] == "choice"
    assert gold["option_count"] == 3
    assert gold["options"][gold["y"]] == "play:h1"
    assert "soft" not in gold                       # 没开 --soft-mix 就不该有软目标
    assert record["recorded"]["q_action"]["probabilities"]["play:h1"] == 0.5
    assert record["split"] == fd.SPLIT_TRAIN
    assert record["room_key"] == "a1_f1_n0_c1"
    assert record["weight"] == 1.0


def test_soft_mix_blends_one_hot_with_recorded_distribution(tiny_rows):
    record = fd.row_to_record(tiny_rows[0], split=fd.SPLIT_TRAIN, weight=1.0, soft_mix=0.5)
    soft = record["gold"]["q_action"]["soft"]
    assert soft == pytest.approx([0.1, 0.75, 0.15])
    assert record["gold"]["q_action"]["soft_mix_used"] == 0.5


def test_soft_mix_falls_back_to_one_hot_when_distribution_is_missing(tiny_rows):
    row = dict(tiny_rows[3])                        # map_node 那条没有 probabilities
    record = fd.row_to_record(row, split=fd.SPLIT_TRAIN, weight=1.0, soft_mix=0.5)
    assert "soft" not in record["gold"]["q_map"]


def test_soft_mix_falls_back_when_keys_do_not_line_up(tiny_rows):
    row = json.loads(json.dumps(tiny_rows[0]))
    row["answers"]["q_action"]["probabilities"] = {"a": 0.5, "b": 0.5}
    record = fd.row_to_record(row, split=fd.SPLIT_TRAIN, weight=1.0, soft_mix=0.5)
    assert "soft" not in record["gold"]["q_action"]


def test_dev_split_is_room_level_and_deterministic(tiny_rows):
    records = [fd.row_to_record(row, split=fd.SPLIT_TRAIN, weight=1.0) for row in tiny_rows]
    first = fd.assign_dev_split(records, frac=0.5, seed=7)
    second = fd.assign_dev_split(records, frac=0.5, seed=7)
    assert first == second                                  # 同一个 seed 结果必须一样
    assert fd.assign_dev_split(records, frac=0.0, seed=7) == {}
    # 同一房间的多行必须同进同出（否则相邻近重复状态会泄漏）
    by_room: dict[str, set[str]] = {}
    for record in records:
        if record["record_id"] in first:
            by_room.setdefault(record["room_key"], set()).add(first[record["record_id"]])
    assert all(len(values) == 1 for values in by_room.values())


def test_jsonl_roundtrip_is_utf8_and_deterministic(tmp_path):
    rows = [{"中文": "值", "n": 1}, {"中文": "值", "n": 2}]
    path = tmp_path / "rows.jsonl"
    assert fd.write_jsonl(path, rows) == 2
    assert fd.read_jsonl(path) == rows
    raw = open(path, "rb").read()
    assert raw.endswith(b"\n") and b"\\u" not in raw       # 不许转义成 ASCII


def test_canonical_json_sorts_keys(tiny_rows):
    assert fd.canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert fd.canonical_json({"a": "中"}) == '{"a":"中"}'


def test_row_sort_key_orders_by_run_then_seq():
    rows = [{"run_id": "r2", "seq": 1}, {"run_id": "r1", "seq": 9}, {"run_id": "r1", "seq": 2}]
    assert [r["seq"] for r in sorted(rows, key=fd.row_sort_key)] == [2, 9, 1]


def test_sha256_file_matches_hashlib(tmp_path):
    import hashlib

    path = tmp_path / "blob.bin"
    path.write_bytes(b"slaya")
    assert fd.sha256_file(path) == hashlib.sha256(b"slaya").hexdigest()


def test_item_length_stats_summarizes_truncation():
    items = [
        {"seq_len": 10, "state_tokens": 100, "state_kept": 50},
        {"seq_len": 20, "state_tokens": 100, "state_kept": 100},
    ]
    stats = fd.item_length_stats(items)
    assert stats["n"] == 2
    assert stats["seq_max"] == 20
    assert stats["state_kept_min"] == 0.5
    assert stats["state_kept_total"] == 150
    assert fd.item_length_stats([]) == {"n": 0}


def test_find_cached_checkpoint_returns_none_when_cache_is_empty(tmp_path):
    """查缓存**不发网络请求**：给的根目录里没有就是 None（而不是去下）。"""
    assert fd.find_cached_checkpoint(search_roots=[str(tmp_path)]) is None
    assert fd.find_cached_checkpoint("convaiinnovations/laya", "multilingual",
                                     search_roots=[str(tmp_path)]) is None


def test_resolve_base_checkpoint_never_silently_downloads(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        fd.resolve_base_checkpoint("some/repo", "english")
    assert "snapshot_download" in str(excinfo.value)


def test_resolve_base_checkpoint_rejects_a_directory_without_config(tmp_path):
    plain = tmp_path / "not-a-checkpoint"
    plain.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        fd.resolve_base_checkpoint(str(plain))
    assert "rl_agent_config.json" in str(excinfo.value)


def test_resolve_base_checkpoint_accepts_a_local_directory(tiny_checkpoint):
    assert fd.resolve_base_checkpoint(tiny_checkpoint) == tiny_checkpoint

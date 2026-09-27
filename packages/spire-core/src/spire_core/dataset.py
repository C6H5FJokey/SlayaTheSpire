"""训练行构造。

数据契约见 docs/08-dataset.md。本模块是纯逻辑：不碰文件、不碰网络。
落盘在 spire-agent 的 recorder 里做。
"""

from __future__ import annotations

import hashlib
from typing import Any

from .model import FairObservation
from .ids import canonical_json

SPLIT_TRAIN = "train"
SPLIT_VAL = "val"
SPLIT_TEST = "test"

SOURCE_HUMAN = "human"
SOURCE_AGENT = "agent"

LABEL_HUMAN = "human"
LABEL_AGENT = "agent"


def split_of(
    run_id: str,
    *,
    split_seed: int,
    val_ratio: float = 0.1,
    test_ratio: float = 0.1,
) -> str:
    """按 run 切分（**绝不按行切分**，否则相邻的近重复状态会同时出现在
    train 与 val，指标虚高）。纯哈希、无状态、可重复：新增 run 不会打乱
    已有 run 的归属。
    """
    digest = hashlib.sha256(f"{split_seed}:{run_id}".encode("utf-8")).digest()
    x = int.from_bytes(digest[:4], "big") / 2**32
    if x < test_ratio:
        return SPLIT_TEST
    if x < test_ratio + val_ratio:
        return SPLIT_VAL
    return SPLIT_TRAIN


def room_key(act: int, floor: int, node: int, combat_instance: int) -> str:
    return f"a{act}_f{floor}_n{node}_c{combat_instance}"


def make_row(
    *,
    run_id: str,
    seq: int,
    decision_point: str,
    room: dict[str, Any],
    context: dict[str, Any],
    source: str,
    split: str,
    state: dict[str, Any],
    questions: dict[str, Any],
    candidate_ids: list[str],
    labels: dict[str, Any],
    meta: dict[str, Any],
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一行训练样本。字段与 docs/08-dataset.md 的训练行格式一一对应。"""
    ci = int(room.get("combat_instance", 1))
    turn = int((state.get("combat") or {}).get("turn", 0))
    row_id = f"{run_id}#c{ci}#t{turn}#s{seq}"
    return {
        "row_id": row_id,
        "run_id": run_id,
        "seq": seq,
        "decision_point": decision_point,
        "room": room,
        "context": context,
        "source": source,
        "split": split,
        "state": state,
        "questions": questions,
        "candidate_ids": list(candidate_ids),
        "labels": dict(labels),
        "meta": dict(meta),
        "outcome": outcome or {
            "run_won": None,
            "hp_after": None,
            "hp_delta_room": None,
            "floor_reached": None,
        },
    }


def expand_choice_to_noul(row: dict[str, Any]) -> list[dict[str, Any]]:
    """把一行 `choice` 无损展开成 N 行 `noul`。

    选中的候选 -> true，其余 -> false。免费得到均衡的二分类样本，
    且与 `choice` 共享同一份 state/questions（见 docs/08-dataset.md#一次采集两种问答形态）。
    """
    questions = row.get("questions") or {}
    if len(questions) != 1:
        return []
    (qid, q), = questions.items()
    if not isinstance(q, dict) or q.get("type") != "choice":
        return []

    labels = row.get("labels") or {}
    chosen = labels.get(qid)
    if chosen is None:
        return []

    criteria = q.get("criteria") or {}
    out: list[dict[str, Any]] = []
    for key, desc in criteria.items():
        clone = {k: v for k, v in row.items() if k != "questions"}
        clone["questions"] = {
            qid: {
                "type": "noul",
                "instructions": (
                    f"{q.get('instructions', '')} Candidate [{key}] {desc} "
                    "Is this the correct choice?"
                ),
            }
        }
        clone["labels"] = {qid: key == chosen}
        clone["row_id"] = f"{row['row_id']}#noul:{key}"
        meta = dict(row.get("meta") or {})
        meta["derived_from"] = row["row_id"]
        clone["meta"] = meta
        out.append(clone)
    return out


def row_hash(row: dict[str, Any]) -> str:
    """训练行去重用的哈希（不含 outcome 与 meta 里的易变字段）。"""
    basis = {
        "decision_point": row.get("decision_point"),
        "state": row.get("state"),
        "questions": row.get("questions"),
        "labels": row.get("labels"),
    }
    return hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()[:16]


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """manifest.json 里的统计口径（见 docs/08-dataset.md#统计口径）。"""
    by_source: dict[str, int] = {}
    by_dp: dict[str, int] = {}
    matched = 0
    human_rows = 0
    agree = 0
    model_rows = 0
    fallback = 0
    post_sl = 0
    states: set[str] = set()

    for r in rows:
        by_source[r.get("source", "?")] = by_source.get(r.get("source", "?"), 0) + 1
        dp = r.get("decision_point", "?")
        by_dp[dp] = by_dp.get(dp, 0) + 1
        meta = r.get("meta") or {}
        if r.get("source") == SOURCE_HUMAN:
            human_rows += 1
            if meta.get("matched"):
                matched += 1
        if meta.get("model_answer") is not None:
            model_rows += 1
            if meta.get("agreement"):
                agree += 1
        if meta.get("agent_fallback"):
            fallback += 1
        if (r.get("room") or {}).get("post_sl"):
            post_sl += 1
        states.add(row_hash(r))

    total = len(rows)
    return {
        "rows_total": total,
        "by_source": by_source,
        "by_decision_point": by_dp,
        "match_rate": round(matched / human_rows, 4) if human_rows else None,
        "agreement_rate": round(agree / model_rows, 4) if model_rows else None,
        "fallback_rate": round(fallback / total, 4) if total else None,
        "post_sl_rows": post_sl,
        "unique_fair_states": len(states),
    }


def filter_rows(
    rows: list[dict[str, Any]],
    *,
    exclude_post_sl_rooms: bool = True,
    exclude_fallback_rows: bool = True,
    exclude_unmatched: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """导出前的过滤。返回 (保留的行, 被丢弃原因的计数)。"""
    kept: list[dict[str, Any]] = []
    dropped = {"post_sl": 0, "fallback": 0, "unmatched": 0}
    for r in rows:
        meta = r.get("meta") or {}
        if exclude_post_sl_rooms and (r.get("room") or {}).get("post_sl"):
            dropped["post_sl"] += 1
            continue
        if exclude_fallback_rows and meta.get("agent_fallback"):
            dropped["fallback"] += 1
            continue
        if exclude_unmatched and meta.get("matched") is False:
            dropped["unmatched"] += 1
            continue
        kept.append(r)
    return kept, dropped


__all__ = [
    "LABEL_AGENT",
    "LABEL_HUMAN",
    "SOURCE_AGENT",
    "SOURCE_HUMAN",
    "SPLIT_TEST",
    "SPLIT_TRAIN",
    "SPLIT_VAL",
    "expand_choice_to_noul",
    "filter_rows",
    "make_row",
    "room_key",
    "row_hash",
    "split_of",
    "summarize_rows",
]

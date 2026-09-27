"""回放：从 `decisions.jsonl` 的一行重建裁决，验证可复现性。

用途有二：

1. 断言"同样的 (state, questions) 必然得到同样的裁决"（见 docs/11-testing.md#回放）；
2. 在不开游戏、不连模型的前提下，对历史决策做离线分析（例如换阈值看
   多选结果会怎么变）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import actions as A
from .arbitrate import expected_level, parse_answers, pick_choice, pick_topk
from .decision import SELECT_CARD_ANY
from .questions import _slug


@dataclass
class ReplayResult:
    ok: bool
    reason: str | None = None
    recorded: str | None = None
    recomputed: str | None = None
    recomputed_ids: list[str] = field(default_factory=list)
    probabilities: dict[str, float] = field(default_factory=dict)


def score_map_from_row(row: dict[str, Any]) -> dict[str, str]:
    """从记录里重建 `{question_id: candidate_id}`。

    规则必须与 `questions.build()` 一致：`{qid}_{slug(cid)}`。这里**不猜前缀**
    （候选 id 自身可能含下划线，猜前缀会失准），而是直接在题目 id 上做后缀匹配。
    """
    questions = row.get("questions") or {}
    if not isinstance(questions, dict):
        return {}
    candidates = list(row.get("candidate_ids") or [])
    out: dict[str, str] = {}
    for qid, q in questions.items():
        if not isinstance(q, dict) or q.get("type") != "score":
            continue
        for cid in candidates:
            if str(qid).endswith("_" + _slug(cid)):
                out[str(qid)] = cid
                break
    return out


def _main_question_id(row: dict[str, Any]) -> str | None:
    questions = row.get("questions") or {}
    if not isinstance(questions, dict) or not questions:
        return None
    # score 题会有多个 id，取公共前缀：去掉最后一个下划线段
    keys = sorted(questions.keys())
    if len(keys) == 1:
        return keys[0]
    first = keys[0]
    prefix = first.rsplit("_", 1)[0]
    return prefix


def replay_decision(row: dict[str, Any], *, threshold_level: float | None = None) -> ReplayResult:
    """重算一行决策，返回与记录是否一致。"""
    recorded = row.get("chosen")
    parsed = parse_answers(row.get("answers") or {})
    dp = row.get("decision_point")

    if dp == SELECT_CARD_ANY:
        smap = score_map_from_row(row)
        if not smap:
            return ReplayResult(ok=False, reason="no score questions to replay")
        expected = {
            cid: expected_level(parsed[qid].probabilities)
            if qid in parsed
            else 0.0
            for qid, cid in smap.items()
        }
        picked = row.get("chosen_ids") or ([recorded] if recorded else [])
        k_min = int(row.get("k_min", 0) or 0)
        k_max = int(row.get("k_max", 0) or 0) or len(expected)
        kwargs = {}
        if threshold_level is not None:
            kwargs["threshold_level"] = threshold_level
        recomputed_ids = pick_topk(
            expected, k_min, k_max, order=list(smap.values()), **kwargs
        )
        ok = recomputed_ids == list(picked)
        return ReplayResult(
            ok=ok,
            reason=None if ok else "top-k differs from recorded",
            recorded=",".join(picked),
            recomputed=",".join(recomputed_ids),
            recomputed_ids=recomputed_ids,
            probabilities=expected,
        )

    qid = _main_question_id(row)
    allowed = list((row.get("questions") or {}).get(qid, {}).get("criteria", {}).keys()) if qid else []
    if not qid or not allowed:
        return ReplayResult(ok=False, reason="missing question or criteria")
    try:
        chosen, _conf, probs = pick_choice(parsed, qid, allowed)
    except Exception as exc:  # noqa: BLE001 - 回放工具，任何异常都算不一致
        return ReplayResult(ok=False, reason=str(exc), recorded=recorded)
    ok = chosen == recorded
    return ReplayResult(
        ok=ok,
        reason=None if ok else "choice differs from recorded",
        recorded=recorded,
        recomputed=chosen,
        recomputed_ids=[chosen],
        probabilities=probs,
    )


def replay_all(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """批量回放，用于回归测试。"""
    total = 0
    mismatches: list[dict[str, Any]] = []
    for row in rows:
        total += 1
        result = replay_decision(row)
        if not result.ok:
            mismatches.append(
                {
                    "row_id": row.get("row_id"),
                    "reason": result.reason,
                    "recorded": result.recorded,
                    "recomputed": result.recomputed,
                }
            )
    return {"total": total, "mismatches": mismatches, "ok": not mismatches}


__all__ = [
    "ReplayResult",
    "replay_all",
    "replay_decision",
    "score_map_from_row",
]

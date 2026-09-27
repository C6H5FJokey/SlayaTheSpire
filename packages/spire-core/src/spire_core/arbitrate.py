"""答案解析与子选择裁决器。

规则（见 docs/06-decision-points.md#子选择裁决器）：

- 单选一律 `choice`；
- 任意多选一律 `score` + `pick_topk`；
- 必选 k 张一律 `choice` 连问 k 次。

**平票按 criteria 的插入顺序，不按字典序** —— 这是可复现性的一部分。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ParseError

SCORE_NEUTRAL_LEVEL = 2.0  # DEFAULT_LEVELS 里 "neutral" 的下标
# 任意多选的默认取舍阈值：只有 "good"(3) 及以上才值得选。
# "neutral" 表示"选不选都行"，因此默认不选 —— 否则任何多选界面都会
# 无条件选满，等于放弃"可以跳过"这个选项（见 docs/06-decision-points.md）。
SCORE_SELECT_THRESHOLD = 3.0


@dataclass(frozen=True)
class ParsedAnswer:
    question_id: str
    answer: Any = None
    probabilities: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    answer_confidence: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


def _as_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def selected_answer(raw: dict[str, Any]) -> Any:
    """从 Laya 的**单题**应答里取出"被选中的那个 key"。

    Laya 按题型分派（见 docs/07-laya-contract.md）：

    - `choice` -> `choice`（criterion 的 key）；
    - `noul`   -> `noul` 是 P(true)，不是 key；按 Laya 自己的 labels 约定折回
      `"true"` / `"false"`；
    - `score`  -> 只有数值 `score`，没有 key —— 多选裁决走 `probabilities` +
      `expected_level`，不经过这里。

    `answer` / `label` 是早期契约里的写法，留作兼容。
    """
    choice = raw.get("choice")
    if isinstance(choice, str):
        return choice
    noul = raw.get("noul")
    if noul is not None:
        try:
            return "true" if float(noul) >= 0.5 else "false"
        except (TypeError, ValueError):
            return None
    return raw.get("answer", raw.get("label"))


def _probabilities(raw: dict[str, Any]) -> dict[str, float]:
    probs_raw = raw.get("probabilities")
    if isinstance(probs_raw, dict):
        return {str(k): _as_float(v) for k, v in probs_raw.items()}
    if isinstance(probs_raw, list):
        return {str(i): _as_float(v) for i, v in enumerate(probs_raw)}
    # `noul` 只给 P(true)，但它本来就是个二分类分布；补齐成两个键，下游
    # pick_choice 就不必为这个题型写特例。
    noul = raw.get("noul")
    if noul is not None:
        p = _as_float(noul)
        return {"false": 1.0 - p, "true": p}
    return {}


def parse_answers(answers: dict[str, Any]) -> dict[str, ParsedAnswer]:
    """把 Laya 的 `answers` 规范化。缺失/畸形条目跳过（由调用方判 parse_error）。"""
    out: dict[str, ParsedAnswer] = {}
    for qid, raw in (answers or {}).items():
        if not isinstance(raw, dict):
            continue
        out[str(qid)] = ParsedAnswer(
            question_id=str(qid),
            answer=selected_answer(raw),
            probabilities=_probabilities(raw),
            confidence=_as_float(raw.get("confidence")),
            answer_confidence=_as_float(
                raw.get("answer_confidence", raw.get("confidence"))
            ),
            raw=raw,
        )
    return out


def pick_choice(
    parsed: dict[str, ParsedAnswer],
    question_id: str,
    allowed: list[str],
) -> tuple[str, float, dict[str, float]]:
    """单选裁决。返回 (候选 id, 答案置信度, 全概率)。

    `allowed` 的顺序即 tie-break 顺序。
    """
    if question_id not in parsed:
        raise ParseError(f"missing answer for question {question_id!r}", question_id)

    pa = parsed[question_id]
    answer = pa.answer
    if not isinstance(answer, str) or answer not in allowed:
        raise ParseError(
            f"answer {answer!r} is not one of the criteria for {question_id!r}",
            question_id,
        )

    # 用 allowed 的顺序重建概率表，保证顺序确定
    probs = {cid: pa.probabilities.get(cid, 0.0) for cid in allowed}
    return answer, pa.answer_confidence, probs


def argmax_by_insertion_order(
    allowed: list[str], probs: dict[str, float]
) -> str:
    """按 `allowed` 的插入顺序找最大概率项（平票取先者）。"""
    best = None
    best_p = -1.0
    for cid in allowed:
        p = probs.get(cid, 0.0)
        if p > best_p:
            best = cid
            best_p = p
    if best is None:
        raise ParseError("no candidates to argmax over")
    return best


def expected_level(probabilities: dict[str, float]) -> float:
    """`score` 题的期望等级（等级为 0..n-1 的有序下标）。"""
    total = 0.0
    weight = 0.0
    for key, p in probabilities.items():
        try:
            level = int(key)
        except ValueError:
            continue
        total += level * p
        weight += p
    if weight <= 0:
        return 0.0
    return total / weight


def pick_topk(
    expected: dict[str, float],
    k_min: int,
    k_max: int,
    *,
    threshold_level: float = SCORE_SELECT_THRESHOLD,
    order: list[str] | None = None,
) -> list[str]:
    """任意多选裁决。

    - 至少 `k_min` 个（即使分数低于阈值也要凑够）；
    - 至多 `k_max` 个；
    - 默认只保留期望等级 >= `threshold_level` 的候选。

    `order` 给定候选的确定性顺序，用于同分排序（默认按 key 的插入顺序）。
    """
    ids = list(order) if order is not None else list(expected.keys())
    # 稳定排序：先按 key 的顺序，再按期望分降序
    ranked = sorted(ids, key=lambda cid: (-expected.get(cid, 0.0), ids.index(cid)))

    chosen = [cid for cid in ranked if expected.get(cid, 0.0) >= threshold_level]
    if len(chosen) < k_min:
        chosen = ranked[:k_min]
    if k_max > 0:
        chosen = chosen[:k_max]
    return chosen


__all__ = [
    "SCORE_NEUTRAL_LEVEL",
    "SCORE_SELECT_THRESHOLD",
    "ParsedAnswer",
    "argmax_by_insertion_order",
    "expected_level",
    "parse_answers",
    "pick_choice",
    "pick_topk",
    "selected_answer",
]

"""Laya 的硬预算：断言与确定性压缩。

预算数值直接来自 laya/serve.py 的实现（见 docs/07-laya-contract.md#硬预算）。
客户端必须先自己断言 —— 不要让远端回 413，因为那意味着我们发了非法请求。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from .errors import BudgetExceeded
from .ids import canonical_json
from .model import FairObservation

MAX_STATE_CHARS = 50000
MAX_QUESTIONS = 64
MAX_CHOICE_OPTIONS = 100
MAX_SCORE_LEVELS = 32
MAX_TOTAL_OPTIONS = 512
MAX_BODY_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class Limits:
    state_chars: int = MAX_STATE_CHARS
    questions: int = MAX_QUESTIONS
    choice_options: int = MAX_CHOICE_OPTIONS
    score_levels: int = MAX_SCORE_LEVELS
    total_options: int = MAX_TOTAL_OPTIONS
    body_bytes: int = MAX_BODY_BYTES


DEFAULT_LIMITS = Limits()


@dataclass
class BudgetReport:
    """压缩过程的可观测记录。落盘进数据集的 `meta`。"""

    state_chars: int = 0
    question_count: int = 0
    total_options: int = 0
    body_bytes: int = 0
    truncated_candidates: list[str] = field(default_factory=list)
    compactions: list[str] = field(default_factory=list)


def count_options(questions: dict[str, Any]) -> tuple[int, int]:
    """返回 (总选项数, 最大单题选项数)。"""
    total = 0
    biggest = 0
    for q in questions.values():
        if not isinstance(q, dict):
            continue
        crit = q.get("criteria")
        if q.get("type") == "choice" and isinstance(crit, dict):
            n = len(crit)
        elif q.get("type") == "score" and isinstance(crit, list):
            n = len(crit)
        else:
            n = 0
        total += n
        biggest = max(biggest, n)
    return total, biggest


def measure(state: dict[str, Any], questions: dict[str, Any]) -> BudgetReport:
    body = canonical_json({"state": state, "questions": questions})
    total, biggest = count_options(questions)
    return BudgetReport(
        state_chars=len(canonical_json(state)),
        question_count=len(questions),
        total_options=total,
        body_bytes=len(body.encode("utf-8")),
    )


def check(
    state: dict[str, Any],
    questions: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
) -> BudgetReport:
    """校验是否超限。超限则抛 BudgetExceeded（不静默截断）。"""
    report = measure(state, questions)
    _, biggest = count_options(questions)

    problems: list[str] = []
    if report.state_chars > limits.state_chars:
        problems.append(f"state {report.state_chars} > {limits.state_chars} chars")
    if report.question_count > limits.questions:
        problems.append(f"{report.question_count} questions > {limits.questions}")
    if biggest > limits.choice_options:
        problems.append(f"{biggest} options in one question > {limits.choice_options}")
    if report.total_options > limits.total_options:
        problems.append(f"{report.total_options} total options > {limits.total_options}")
    if report.body_bytes > limits.body_bytes:
        problems.append(f"body {report.body_bytes} > {limits.body_bytes} bytes")

    if problems:
        raise BudgetExceeded("; ".join(problems), detail=report.__dict__)
    return report


def enforce(
    fair: FairObservation,
    state: dict[str, Any],
    questions: dict[str, Any],
    limits: Limits = DEFAULT_LIMITS,
    *,
    max_candidates_per_question: int = MAX_CHOICE_OPTIONS,
) -> tuple[dict[str, Any], dict[str, Any], BudgetReport]:
    """按优先级压缩，直到进入预算。

    压缩优先级（见 docs/05-state-schema.md#预算与压缩）：
      1. 折叠 zones 同名牌     —— 由 serialize 默认完成
      2. 卡牌文本截断到 160 字符 —— 由 serialize 默认完成
      3. deck 只给计数摘要      —— compact_deck=True 时已完成
      4. 候选超过上限时确定性截断（并在 report 里记录被丢弃项）
      5. 仍超限 -> 抛 BudgetExceeded

    第 1-3 步已经在 serialize 里做完，这里负责第 4 步与最终断言。
    """
    state = dict(state)
    questions = {k: dict(v) if isinstance(v, dict) else v for k, v in questions.items()}
    report = BudgetReport()

    # 第 4 步：按题目裁剪候选。截断顺序 = criteria 的插入顺序（确定性）。
    for qid, q in questions.items():
        if not isinstance(q, dict):
            continue
        crit = q.get("criteria")
        if q.get("type") == "choice" and isinstance(crit, dict):
            if len(crit) > max_candidates_per_question:
                keys = list(crit.keys())
                keep = keys[:max_candidates_per_question]
                dropped = keys[max_candidates_per_question:]
                report.truncated_candidates.extend(dropped)
                report.compactions.append(
                    f"{qid}: dropped {len(dropped)} candidates"
                )
                questions[qid]["criteria"] = {k: crit[k] for k in keep}

    final = check(state, questions, limits)
    report.state_chars = final.state_chars
    report.question_count = final.question_count
    report.total_options = final.total_options
    report.body_bytes = final.body_bytes
    if report.compactions:
        # 压缩记录要保留下来（check 返回的是新对象）
        pass
    return state, questions, report


def load_report(report: BudgetReport) -> dict[str, Any]:
    """把报告转成可 JSON 序列化的 meta 片段。"""
    return {
        "state_chars": report.state_chars,
        "question_count": report.question_count,
        "total_options": report.total_options,
        "body_bytes": report.body_bytes,
        "truncated_candidates": list(report.truncated_candidates),
        "compactions": list(report.compactions),
    }


__all__ = [
    "DEFAULT_LIMITS",
    "MAX_BODY_BYTES",
    "MAX_CHOICE_OPTIONS",
    "MAX_QUESTIONS",
    "MAX_SCORE_LEVELS",
    "MAX_STATE_CHARS",
    "MAX_TOTAL_OPTIONS",
    "BudgetReport",
    "Limits",
    "check",
    "count_options",
    "enforce",
    "load_report",
    "measure",
]
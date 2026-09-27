"""决策管线：把"一个公平视图"变成"一次决策"。

这是 core 的主入口，也是 agent 唯一需要调的东西：

    plan = pipeline.make_plan(fair)                 # 构题
    result = await laya_client.call(plan)           # agent 负责网络
    decision = pipeline.resolve(plan, result)       # 裁决

`make_plan` 与 `resolve` 都是纯函数；网络只在两者之间发生。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from . import actions as A
from . import budget as budget_mod
from . import legality
from . import policy
from .arbitrate import (
    expected_level,
    parse_answers,
    pick_choice,
    pick_topk,
)
from .candidates import Candidate, enumerate_candidates
from .decision import (
    DECISION_POINTS,
    RUN_OVER,
    SELECT_CARD_ANY,
    SELECT_CARD_MUST_K,
    identify,
)
from .errors import NoCandidates, ParseError
from .model import FairObservation
from .questions import build, load_templates, score_question_ids
from .types import Decision, LayaResult

log = logging.getLogger(__name__)


class RunOver(Exception):
    """当前局面不再需要决策（局终 / 地图无路可走）。"""


@dataclass
class Plan:
    """一次决策的全部输入。agent 把它发给 Laya，然后交给 `resolve`。"""

    decision_point: str
    question_id: str
    candidates: list[Candidate]
    state: dict[str, Any]
    questions: dict[str, Any]
    candidate_ids: list[str] = field(default_factory=list)
    # 被 `legality` 摘掉的候选 `[(cid, 原因)]`。正常永远是空的；非空就是
    # 枚举器写出了游戏不会接受的动作 —— 是 bug 信号，要进日志与数据集。
    dropped_candidates: list[tuple[str, str]] = field(default_factory=list)
    score_map: dict[str, str] = field(default_factory=dict)
    k_min: int = 0
    k_max: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    budget: dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        """发给 Laya 的请求体。"""
        return {"state": self.state, "questions": self.questions}


def make_plan(
    fair: FairObservation,
    *,
    decision_point: str | None = None,
    templates: dict[str, dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    compact_deck: bool = True,
    limits: budget_mod.Limits = budget_mod.DEFAULT_LIMITS,
) -> Plan:
    """识别决策点 -> 枚举候选 -> 构题 -> 预算压缩。"""
    dp = decision_point or identify(fair)
    if dp == RUN_OVER or dp not in DECISION_POINTS:
        raise RunOver(f"decision point {dp!r} needs no decision")

    templates = templates or load_templates()
    context = dict(context or {})

    candidates = enumerate_candidates(fair, dp, context=context)
    # 合法性过滤：不合法的动作**不进** criteria，模型根本没有机会选到它。
    # 唯一的事实来源是观测本身，不是游戏 API 的结论（见 legality 的模块注释）。
    candidates, dropped = legality.legal_only(fair, candidates, dp)
    if dropped:
        log.warning("decision point %s: dropped illegal candidates %s", dp, dropped)
    if not candidates:
        raise NoCandidates(
            f"every candidate for {dp!r} failed the legality check: {dropped}"
        )
    state, questions = build(
        fair,
        dp,
        candidates,
        context=context,
        templates=templates,
        compact_deck=compact_deck,
    )
    state, questions, report = budget_mod.enforce(fair, state, questions, limits)

    tpl = templates.get(dp, {})
    qid = str(tpl.get("question_id", "q_action"))
    score_map = (
        score_question_ids(candidates, dp, templates)
        if str(tpl.get("type", "choice")) == "score"
        else {}
    )
    if score_map:
        # score 题每题一个 id，主 question_id 只是前缀
        qid = next(iter(score_map))

    return Plan(
        decision_point=dp,
        question_id=qid,
        candidates=candidates,
        state=state,
        questions=questions,
        candidate_ids=[c.cid for c in candidates],
        dropped_candidates=dropped,
        score_map=score_map,
        k_min=int(fair.screen_state.min_select),
        k_max=int(fair.screen_state.max_select),
        context=context,
        budget=budget_mod.load_report(report),
    )


def resolve(plan: Plan, result: LayaResult) -> Decision:
    """把 Laya 的答案裁决成一次（或一组）候选。"""
    parsed = parse_answers(result.answers)

    if plan.decision_point == SELECT_CARD_ANY and plan.score_map:
        expected: dict[str, float] = {}
        for qid, cid in plan.score_map.items():
            pa = parsed.get(qid)
            expected[cid] = (
                expected_level(pa.probabilities) if pa is not None else 0.0
            )
        order = list(plan.score_map.values())
        k_max = plan.k_max if plan.k_max > 0 else len(order)
        chosen_ids = pick_topk(expected, plan.k_min, k_max, order=order)
        action = A.select_cards(chosen_ids) if chosen_ids else None
        return Decision(
            seq=-1,
            decision_point=plan.decision_point,
            state=plan.state,
            questions=plan.questions,
            candidate_ids=plan.candidate_ids,
            chosen=chosen_ids[0] if chosen_ids else None,
            chosen_ids=chosen_ids,
            chosen_action=action.wire() if action else None,
            confidence=_mean_confidence(parsed, plan.score_map),
            probabilities=expected,
            fallback=False,
            fallback_reason=None,
        )

    allowed = _choice_ids(plan)
    chosen, confidence, probs = pick_choice(parsed, plan.question_id, allowed)
    action = _action_for(plan, chosen)
    return Decision(
        seq=-1,
        decision_point=plan.decision_point,
        state=plan.state,
        questions=plan.questions,
        candidate_ids=plan.candidate_ids,
        chosen=chosen,
        chosen_ids=[chosen],
        chosen_action=action.wire() if action else None,
        confidence=confidence,
        probabilities=probs,
        fallback=False,
        fallback_reason=None,
    )


def fallback_decision(plan: Plan, fair: FairObservation, reason: str) -> Decision:
    """Laya 不可用时的保守兜底。模型字段留空 —— 确实没有模型参与。"""
    if plan.decision_point in (SELECT_CARD_MUST_K, SELECT_CARD_ANY):
        count = max(plan.k_min, 0)
        chosen_ids = policy.select_cards_fallback(plan.candidates, count)
        action = A.select_cards(chosen_ids) if chosen_ids else None
    else:
        pick = policy.fallback_action(fair, plan.decision_point, plan.candidates)
        chosen_ids = [pick.cid]
        action = pick.action

    return Decision(
        seq=-1,
        decision_point=plan.decision_point,
        state=plan.state,
        questions=plan.questions,
        candidate_ids=plan.candidate_ids,
        chosen=chosen_ids[0] if chosen_ids else None,
        chosen_ids=chosen_ids,
        chosen_action=action.wire() if action else None,
        confidence=0.0,
        probabilities={},
        fallback=True,
        fallback_reason=reason,
    )


def action_from_decision(decision: Decision) -> A.Action:
    """把 Decision 变成可发给 mod 的动作。"""
    if decision.chosen_action is None:
        raise ParseError(
            f"decision for {decision.decision_point!r} produced no action "
            f"(chosen_ids={decision.chosen_ids})"
        )
    wire = decision.chosen_action
    return A.Action(wire["kind"], wire.get("args") or {})


def _choice_ids(plan: Plan) -> list[str]:
    """choice 题的 criteria key 列表，顺序 = tie-break 顺序。

    对 `select_card_must_k` 而言，这就是"还没被选过"的候选。
    """
    q = plan.questions.get(plan.question_id)
    if not isinstance(q, dict):
        raise ParseError(f"question {plan.question_id!r} missing from plan")
    if q.get("type") == "score":
        return list(plan.score_map.keys())
    criteria = q.get("criteria")
    if not isinstance(criteria, dict) or not criteria:
        raise ParseError(f"question {plan.question_id!r} has no criteria")
    return list(criteria.keys())


def _action_for(plan: Plan, cid: str) -> A.Action | None:
    """候选 id -> 语义动作。选牌类候选返回 None（由决策点层聚合）。"""
    if A.is_card_candidate(cid):
        return None
    return A.parse_candidate(cid)


def _mean_confidence(parsed, score_map: dict[str, str]) -> float:
    values = [
        parsed[qid].answer_confidence
        for qid in score_map
        if qid in parsed
    ]
    if not values:
        return 0.0
    return round(sum(values) / len(values), 4)


__all__ = [
    "Plan",
    "RunOver",
    "action_from_decision",
    "fallback_decision",
    "make_plan",
    "resolve",
]

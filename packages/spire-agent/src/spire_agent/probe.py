"""给 Laya 的最小合法探针 —— 契约对不上要在**启动前**就红（见 docs/07-laya-contract.md）。

真实 Laya 对每道题的要求（摘自 `laya.agent.LayaAgent._check_question`）：

- `type` 必须是 choice / score / noul 之一；
- **每题必须有 `instructions`**（模型要回答的那句话）。缺了就是 422
  `question 'q': no 'instructions'; add the text the model should answer`；
- `choice`：`criteria` 是非空 dict（key -> 描述）或 list（标签）；
- `score`：`criteria` 是非空 list（等级描述，index 0 在前）；
- `noul`：`criteria` 只能键 `true`/`false`（或省略）；`labels` 只对 noul 有效。

应答侧：choice 的答案在 `answers[qid]["choice"]`，服用的 checkpoint 在 `routing["model"]`。

`cli.preflight_laya`、`cli doctor` 与 `tools/laya_health.py` 共用这一份 payload 和同一个
校验器，免得三处各写一份、改了一处就悄悄漂移（曾经探针缺 `instructions`，真机上直接 422）。
"""

from __future__ import annotations

import json
from typing import Any

PROBE_QUESTION_ID = "q_action"
PROBE_OPTIONS: tuple[str, ...] = ("play:h0->m0", "play:h1->m0")

PROBE_INSTRUCTIONS = (
    "You are playing Slay the Spire as the Ironclad. "
    "Given the state, choose the single best action for this turn."
)


def probe_payload() -> dict[str, Any]:
    """最小但合法的一问：单题 choice，两个候选。"""
    state = {
        "screen": "NONE",
        "room": {"act": 1, "floor": 1, "type": "MONSTER"},
        "in_combat": True,
        "combat": {
            "turn": 1,
            "player": {"hp": 80, "max_hp": 80, "block": 0, "energy": 3},
            "hand": [
                {"id": "Strike_R", "cost": 1, "playable": True},
                {"id": "Defend_R", "cost": 1, "playable": True},
            ],
            "monsters": [{"id": "Cultist", "hp": 50, "block": 0, "intent": "buff"}],
        },
    }
    questions = {
        PROBE_QUESTION_ID: {
            "type": "choice",
            "instructions": PROBE_INSTRUCTIONS,
            "criteria": {
                PROBE_OPTIONS[0]: "Play Strike on the Cultist.",
                PROBE_OPTIONS[1]: "Play Defend.",
            },
        }
    }
    return {"state": state, "questions": questions}


def check_probe_answer(result: dict[str, Any] | None) -> tuple[bool, str]:
    """校验应答形态，返回 `(是否通过, 说明)`。

    只认真实形态：choice 走 `answers[qid]["choice"]`，且必须是候选之一。老写法
    （`answers[qid]["answer"]` 之类）在这里就会红 —— 这正是要挡住的东西。
    """
    if result is None:
        return False, "没拿到应答（连接 / 鉴权 / 超时 / 非 JSON，见上面的日志）"
    answers = result.get("answers")
    if not isinstance(answers, dict):
        return False, f"应答里没有 answers 对象：{_brief(result)}"
    answer = answers.get(PROBE_QUESTION_ID)
    if not isinstance(answer, dict):
        return False, f"answers[{PROBE_QUESTION_ID}] 缺失或不是对象：{_brief(answers)}"
    choice = answer.get("choice")
    if choice not in PROBE_OPTIONS:
        return False, (
            f"answers[{PROBE_QUESTION_ID}]['choice']={choice!r} 不在候选 {PROBE_OPTIONS} 里"
            f"（本来拿到的是 {_brief(answer)}）"
        )
    return True, str(choice)


def _brief(value: Any, limit: int = 240) -> str:
    return json.dumps(value, ensure_ascii=False)[:limit]

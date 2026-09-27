"""跨模块的值类型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LayaResult:
    """一次 `/v1/systemone` 调用的结果。"""

    model: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    routing: dict[str, Any] = field(default_factory=dict)
    latency_ms: int = 0
    cache_hit: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def checkpoint(self) -> str | None:
        """routing 里报告的 checkpoint 名（决定了标签来自哪个模型）。

        Laya 把它放在 `routing["model"]`；`checkpoint` 是早期契约里的写法，留作兼容。
        """
        if not isinstance(self.routing, dict):
            return None
        value = self.routing.get("model") or self.routing.get("checkpoint")
        return str(value) if value else None


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    error: str | None = None
    code: str | None = None

    @staticmethod
    def from_payload(payload: dict[str, Any]) -> "ActionResult":
        return ActionResult(
            ok=bool(payload.get("ok", False)),
            error=payload.get("error"),
            code=payload.get("code"),
        )


@dataclass(frozen=True)
class Decision:
    """一次决策的完整结果，贯穿记录与执行。"""

    seq: int
    decision_point: str
    state: dict[str, Any]
    questions: dict[str, Any]
    candidate_ids: list[str]
    chosen: str | None
    chosen_ids: list[str]
    chosen_action: dict[str, Any] | None
    confidence: float
    probabilities: dict[str, float]
    fallback: bool
    fallback_reason: str | None
    meta: dict[str, Any] = field(default_factory=dict)


__all__ = ["ActionResult", "Decision", "LayaResult"]

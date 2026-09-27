"""spire-core 的异常层级。

所有异常都是纯 Python 的，不依赖任何第三方库——core 包刻意做到零依赖，
以便整包搬到远端复用。
"""


class SpireCoreError(Exception):
    """core 包所有异常的基类。"""


class BudgetExceeded(SpireCoreError):
    """构题结果超出 Laya 的硬预算，且确定性压缩无法挽回。

    这是实现缺陷级的错误，不是运行时噪声：`budget.enforce` 应当保证
    正常情况下永远不会抛它（见 docs/05-state-schema.md 的"预算与压缩"）。
    """

    def __init__(self, reason: str, detail: dict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class UnknownDecisionPoint(SpireCoreError):
    """观测不属于任何已知决策点，且无法降级到 generic_choice。"""


class NoCandidates(SpireCoreError):
    """某个决策点枚举不出任何候选。这是 bug，不做静默降级。"""


class ParseError(SpireCoreError):
    """Laya 的答案无法解析成合法动作。"""

    def __init__(self, reason: str, question_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.question_id = question_id
"""spire-core —— 纯逻辑包。

**零第三方依赖、零 IO、零网络。** 这一点是刻意的：

- 它让"模型看到什么、问什么、怎么裁决"这些最重的逻辑可以在没有游戏、
  没有模型、没有网络的情况下全部被单测覆盖；
- 它让整个包可以原样搬到远端服务里复用（见 docs/02-architecture.md）。

对外只需要记三个入口：

    from spire_core import pipeline, fairness, model

    fair = fairness.filter_(model.RawObservation.from_dict(raw))
    plan = pipeline.make_plan(fair)
    decision = pipeline.resolve(plan, laya_result)
"""

from . import (
    actions,
    arbitrate,
    budget,
    candidates,
    config,
    dataset,
    decision,
    describe,
    fairness,
    ids,
    model,
    pipeline,
    policy,
    questions,
    replay,
    serialize,
    sl,
    types,
)

__version__ = "0.1.0"

__all__ = [
    "actions",
    "arbitrate",
    "budget",
    "candidates",
    "config",
    "dataset",
    "decision",
    "describe",
    "fairness",
    "ids",
    "model",
    "pipeline",
    "policy",
    "questions",
    "replay",
    "serialize",
    "sl",
    "types",
    "__version__",
]
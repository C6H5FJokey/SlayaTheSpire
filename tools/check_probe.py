#!/usr/bin/env python
r"""用**真的** laya 校验器检查 `spire_agent.probe` 的探针还合法（离线，不起服务）。

    .\.venv-laya\Scripts\python.exe tools\check_probe.py

laya 的题型规则在 `laya.agent.Agent._check_question`。agent 的 `.venv` 里没有 laya
（也不该有），所以这个检查只能在 `.venv-laya` 里跑；agent 侧的 `test_probe.py` 只是把
规则抄了一份当回归闸门，**这里才是拿真规则对一遍**。
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-agent" / "src"))


def main() -> int:
    try:
        from laya.agent import QTYPES, Agent
    except ImportError:
        print("FAIL: 这个脚本要用 .venv-laya 跑（agent 的 .venv 里没有 laya）")
        return 1

    from spire_agent.probe import probe_payload

    payload = probe_payload()
    for qid, qdef in payload["questions"].items():
        Agent._check_question(qid, qdef)

    # 反例：缺 instructions 必须被拒 —— 否则说明这个检查没在测真规则
    ok = True
    try:
        Agent._check_question("__negative__", {"type": "choice", "criteria": {"a": "A"}})
    except ValueError:
        pass
    else:
        ok = False
        print("FAIL: the real validator accepted a question without 'instructions'")

    print(f"QTYPES={sorted(QTYPES)}")
    print(f"questions={sorted(payload['questions'])}")
    print("CHECK_PROBE " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

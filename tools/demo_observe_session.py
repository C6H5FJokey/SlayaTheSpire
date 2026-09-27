#!/usr/bin/env python
"""离线演示 / 冒烟：不碰游戏，跑一段 `observe_human` 采集，再导出成 dataset。

造一串假对话（观察 -> 人类动作 -> 换房提交 -> 读档回滚），产出**真实**的
`runs/<id>/{meta.json,raw_states.jsonl,decisions.jsonl,pending/,summary.json}` 与
`dataset/{train,val,test}.jsonl + manifest.json`，用来回答两件事：

1. **采集长什么样**：看 `runs/<id>/decisions.jsonl`（每次决策点一行）；
2. **导出长什么样**：看 `dataset/train.jsonl`（一行 = 能直接喂 Laya 微调的
   `(英文 state, typed questions) -> labels`）。

观测夹具直接复用 core 的测试夹具（`packages/spire-core/tests/fixtures.py`），
免得这里再养第二套 schema。**不连游戏、不连真 Laya**（用进程内 `FakeLaya`），
所以它可以随时跑；真机采集见 docs/08-dataset.md 的「采集 runbook」。

用法：
    python tools/demo_observe_session.py                    # 写到 .pytest-tmp/demo/（先清空旧产物）
    python tools/demo_observe_session.py --out D:\\tmp\\demo
    python tools/demo_observe_session.py --keep             # 保留上次产物，不清理
    python tools/demo_observe_session.py --dump-row         # 额外原样打印一行数据集
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-core" / "tests"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-agent" / "src"))

from fixtures import combat_observation  # noqa: E402
from spire_agent.bridge import Message  # noqa: E402
from spire_agent.console import enable_ctrl_c, enable_utf8_output  # noqa: E402
from spire_agent.fake_laya import FakeLaya  # noqa: E402
from spire_agent.laya_client import LayaClient  # noqa: E402
from spire_agent.recorder import RunRecorder  # noqa: E402
from spire_agent.runner import AgentRunner  # noqa: E402
from spire_core.config import from_dict  # noqa: E402


class RecordingBridge:
    """假桥：只记 agent 发出去的动作（observe 模式本来就不该发，正好用来断言）。"""

    def __init__(self) -> None:
        self.hello = {
            "mod_version": "0.1.0",
            "game_version": "2.3.4",
            "protocol": 2,
            "capabilities": ["observe", "act", "human_action", "watchdog", "configure"],
        }
        self.configured = {"mode": "observe_human", "watchdog_sec": 30}
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._next_id = 1

    def connect(self, retry: bool = False) -> bool:
        return True

    def recv(self, timeout: float = 0.2) -> None:
        return None

    def send_action(self, *, seq: int, kind: str, args: dict | None = None) -> int:
        message_id = self._next_id
        self._next_id += 1
        self.sent.append({"id": message_id, "seq": seq, "kind": kind, "args": args or {}})
        return message_id

    def close(self) -> None:
        self.closed = True


def observation(seq: int, raw: dict, **extra: Any) -> Message:
    body: dict[str, Any] = {"seq": seq, "raw": raw}
    body.update(extra)
    return Message(type="observation", id=0, payload=body)


def human_action(seq: int, kind: str, args: dict | None = None) -> Message:
    return Message(type="human_action", id=0, payload={"seq": seq, "kind": kind, "args": args or {}})


def export(runs_dir: Path, out_dir: Path, *flags: str) -> list[dict[str, Any]]:
    """跑真正的 `dataset/export.py`，返回它写出来的 train 行。"""
    cmd = [sys.executable, str(REPO_ROOT / "dataset" / "export.py"),
           "--runs", str(runs_dir), "--out", str(out_dir), *flags]
    # 子进程输出统一按 UTF-8 收，免得受本机代码页影响
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", env=env)
    if proc.returncode != 0:
        print(proc.stdout[-2000:], proc.stderr[-2000:], sep="\n", file=sys.stderr)
        raise SystemExit(f"export.py failed (exit {proc.returncode})")
    print("  " + proc.stdout.strip().replace("\n", "\n  "))
    path = out_dir / "train.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    enable_ctrl_c()
    enable_utf8_output()
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default=str(REPO_ROOT / ".pytest-tmp" / "demo"))
    parser.add_argument("--run-id", default="demo-0001")
    parser.add_argument("--keep", action="store_true",
                        help="keep previous demo output instead of wiping it")
    parser.add_argument("--dump-row", action="store_true", help="原样打印一行数据集")
    args = parser.parse_args()

    out = Path(args.out).resolve()
    runs_dir = out / "runs"
    if not args.keep:
        for stale in (runs_dir, out / "dataset", out / "dataset-all"):
            if stale.exists():
                shutil.rmtree(stale)
    run_id = args.run_id
    suffix = 2
    while (runs_dir / run_id).exists():
        run_id = f"{args.run_id}-{suffix}"
        suffix += 1
    print(f"[demo] run_id={run_id}  out={out}")

    fake = FakeLaya(api_key="demo", model="english").start()
    cfg = from_dict(
        {
            "mode": "observe_human",
            "character": "IRONCLAD",
            "ascension": 0,
            "record": {"runs_dir": str(runs_dir), "save_raw_states": True},
            "laya": {"base_url": fake.base_url, "api_key": "demo", "model": "english",
                     "backoff_base_sec": 0.0},
            "observe_human": {"also_query_model": True},
        }
    )
    bridge = RecordingBridge()
    recorder = RunRecorder(runs_dir, run_id)
    runner = AgentRunner(cfg, bridge=bridge, laya=LayaClient(cfg.laya), recorder=recorder)
    runner.start()

    # 第 7 层战斗房：人类出第 2 张牌打第 1 个敌人 -> 命中候选，强标签
    runner.handle_observation(observation(1, combat_observation()))
    runner.handle_human_action(human_action(1, "play_card", {"hand_index": 1, "target": "m0"}))

    # 同一间房：人类做了我们没枚举到的事 -> matched=false（候选枚举器的改进信号）
    runner.handle_observation(observation(2, combat_observation(turn=4, hp=68)))
    runner.handle_human_action(human_action(2, "play_card", {"hand_index": 99, "target": "m9"}))

    # 走进第 8 层 -> 提交第 7 层
    floor8 = combat_observation(turn=1, hp=68)
    floor8["room"] = {"act": 1, "floor": 8, "node": 6, "type": "MONSTER"}
    runner.handle_observation(observation(3, floor8))
    runner.handle_human_action(human_action(3, "end_turn", {}))

    # 读档（SL）：当前房间 pending 全丢、combat_instance+1、post_sl=true，从房间开头重记
    runner.handle_observation(observation(4, floor8, sl={"count": 1, "seam": "loadPlayerSave"}))
    runner.handle_human_action(human_action(4, "end_turn", {}))

    # 第 9 层 -> 提交第 8 层；然后局终
    floor9 = combat_observation(turn=1, hp=60)
    floor9["room"] = {"act": 1, "floor": 9, "node": 7, "type": "ELITE"}
    runner.handle_observation(observation(5, floor9))
    runner.handle_human_action(human_action(5, "end_turn", {}))
    runner.stop(result="aborted")
    fake.stop()

    committed = recorder.load_committed()
    print(f"\n[runs] {recorder.dir}")
    print(f"  committed rows = {len(committed)}  (source={sorted({r['source'] for r in committed})})")
    print(f"  matched={runner.matched} unmatched={runner.unmatched} "
          f"sl_events={len(recorder.meta.sl_events)}")
    for event in recorder.meta.sl_events:
        print(f"    SL @ {event.room_key}: dropped {event.dropped_rows} rows (seam={event.seam})")
    print("  agent 发出去的动作 =", bridge.sent or "[]（observe 模式不接管，正确）")

    print("\n[export] 默认口径（排除 post_sl 房间 / fallback 行 / matched=false 行）")
    strict = export(runs_dir, out / "dataset")
    print("\n[export] --keep-post-sl --keep-fallback --keep-unmatched")
    loose = export(runs_dir, out / "dataset-all", "--keep-post-sl", "--keep-fallback", "--keep-unmatched")

    print(f"\n[结果] dataset/train.jsonl {len(strict)} 行；dataset-all/train.jsonl {len(loose)} 行"
          f"（被默认口径挡掉 {len(loose) - len(strict)} 行）")
    if strict:
        row = strict[0]
        print("\n[fine] 第一行的关键字段：")
        for key in ("row_id", "decision_point", "room", "context", "source", "split"):
            print(f"  {key:14} {json.dumps(row.get(key), ensure_ascii=False)}")
        meta = {k: row["meta"].get(k) for k in
                ("label_source", "matched", "model_answer", "model_confidence",
                 "agreement", "checkpoint", "agent_fallback")}
        print(f"  {'meta':14} {json.dumps(meta, ensure_ascii=False)}")
        print(f"  {'labels':14} {json.dumps(row.get('labels'), ensure_ascii=False)}")
        print(f"  {'candidate_ids':14} {json.dumps(row.get('candidate_ids'), ensure_ascii=False)}")
        print(f"  {'state':14} {len(json.dumps(row['state'], ensure_ascii=False))} 字符（英文，见 docs/05）")
        print(f"  {'questions':14} {list(row['questions'])}（题干模板见 docs/06）")
    if args.dump_row and strict:
        print("\n[fine] 原样一行：")
        print(json.dumps(strict[0], ensure_ascii=False, indent=2))
    print(f"\n看文件：{runs_dir / run_id / 'decisions.jsonl'} 和 {out / 'dataset' / 'train.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

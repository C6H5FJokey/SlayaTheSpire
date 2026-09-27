"""对局记录（见 docs/08-dataset.md#采集落盘布局）。

核心不变式：**交易边界是"当前房间节点"**。

- 决策点产生的训练行先写进 `pending/<room_key>.jsonl`；
- 离开该节点时才把它们**提交**到 `decisions.jsonl`；
- 一旦检测到 SL（读档），当前房间的 pending 整体删除、`combat_instance` 自增，
  从房间开头重新记录；前序房间的已提交数据不受影响。

这样"SL 要抛弃本场战斗已记录的数据"这条规则就落在文件系统上，而不是靠内存里的
一个 flag —— agent 中途被杀掉，pending 文件也还在，不会污染已提交的数据。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

RUN_ID_RE = re.compile(r"^run-(\d+)$")


def next_run_id(runs_dir: Path) -> str:
    """`runs/` 里已有的最大编号 +1（`run-0001` 起，4 位补零）。"""
    highest = 0
    if runs_dir.is_dir():
        for child in runs_dir.iterdir():
            match = RUN_ID_RE.match(child.name)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"run-{highest + 1:04d}"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """追加一行 JSONL。**先落盘再返回**：崩溃也不能丢已决策的数据。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                log.warning("skipping malformed row in %s", path)
    return rows


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@dataclass
class SlEvent:
    """一次 SL 的完整记录（进 `meta.json` 的 `sl_events`）。"""

    at_millis: int
    seam: str
    room_key: str
    detail: str
    dropped_rows: int
    combat_instance: int


@dataclass
class RunMeta:
    run_id: str
    mod_version: str = ""
    game_version: str = ""
    protocol: int = 1
    mode: str = "agent"
    character: str = "IRONCLAD"
    ascension: int = 0
    seed: int = -1
    # 界面语言。序列化契约要求英文（`ENG`），别的语言会让 state 里全是本地化文本。
    game_language: str = ""
    fairness_mode: str = "strict"
    laya_base_url: str = ""
    laya_model: str = ""
    laya_on_error: str = "stop"
    # 模组 hello 里报告的 observe_human；None = 模组没报这个键（老版本）
    mod_observe_human: bool | None = None
    started_at: str = ""
    finished_at: str = ""
    result: str = "in_progress"
    floors_reached: int = 0
    sl_events: list[SlEvent] = field(default_factory=list)
    watchdog_events: int = 0


class RunRecorder:
    def __init__(
        self,
        runs_dir: str | os.PathLike[str],
        run_id: str | None = None,
        *,
        save_raw_states: bool = True,
    ) -> None:
        self.runs_dir = Path(runs_dir)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or next_run_id(self.runs_dir)
        self.dir = self.runs_dir / self.run_id
        self.pending_dir = self.dir / "pending"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.save_raw_states = save_raw_states
        self.meta = RunMeta(run_id=self.run_id, started_at=_now_iso())
        self._pending: dict[str, list[dict[str, Any]]] = {}
        self._committed: list[dict[str, Any]] = []
        self._floor_hp: list[tuple[int, int, int]] = []
        self._hp_deltas: dict[str, int] = {}
        self.write_meta()

    # ------------------------------------------------------------ 路径

    @property
    def meta_path(self) -> Path:
        return self.dir / "meta.json"

    @property
    def decisions_path(self) -> Path:
        return self.dir / "decisions.jsonl"

    @property
    def raw_path(self) -> Path:
        return self.dir / "raw_states.jsonl"

    @property
    def summary_path(self) -> Path:
        return self.dir / "summary.json"

    def pending_path(self, room_key: str) -> Path:
        return self.pending_dir / f"{room_key}.jsonl"

    # ------------------------------------------------------------ 原始观测

    def write_raw(self, seq: int, raw: dict[str, Any], *, sl: dict[str, Any] | None = None) -> None:
        if not self.save_raw_states:
            return
        append_jsonl(self.raw_path, {"seq": seq, "at": _now_iso(), "raw": raw, "sl": sl})

    # ------------------------------------------------------------ pending / commit

    def add_pending(self, room_key: str, row: dict[str, Any]) -> None:
        """决策行先落 pending 文件，离开房间时才提交。"""
        self._pending.setdefault(room_key, []).append(row)
        append_jsonl(self.pending_path(room_key), row)

    def pending_rows(self, room_key: str) -> list[dict[str, Any]]:
        return list(self._pending.get(room_key, ()))

    def pending_keys(self) -> list[str]:
        """当前还有 pending 的房间 key（正常最多一个 = 正在记录的房间）。

        内存里的桶 ∪ 磁盘上的文件：agent 重启后内存是空的，但磁盘上的
        `pending/<room_key>.jsonl` 还在，那些行也必须能被 SL 清掉。
        """
        keys = set(self._pending)
        if self.pending_dir.is_dir():
            keys |= {path.stem for path in self.pending_dir.glob("*.jsonl")}
        return sorted(keys)

    def commit_room(self, room_key: str) -> int:
        """离开节点：pending 转正。返回提交的行数。"""
        rows = self._pending.pop(room_key, [])
        for row in rows:
            append_jsonl(self.decisions_path, row)
            self._committed.append(row)
        path = self.pending_path(room_key)
        if path.exists():
            path.unlink()
        if rows:
            log.info("committed %d rows from %s", len(rows), room_key)
        return len(rows)

    def rollback_room(
        self,
        room_key: str,
        *,
        seam: str,
        detail: str,
        combat_instance: int,
    ) -> int:
        """SL：整间房的 pending 作废（**已提交的前序房间不受影响**）。

        agent 重启后 `_pending` 是空的，但磁盘上的文件可能还有行，所以行数要回读
        文件，不能只看内存。
        """
        rows = self._pending.pop(room_key, None)
        path = self.pending_path(room_key)
        if rows is None:
            rows = read_jsonl(path) if path.exists() else []
        if path.exists():
            path.unlink()
        event = SlEvent(
            at_millis=int(time.time() * 1000),
            seam=seam,
            room_key=room_key,
            detail=detail,
            dropped_rows=len(rows),
            combat_instance=combat_instance,
        )
        self.meta.sl_events.append(event)
        self.write_meta()
        log.warning(
            "[sl] rolled back %s (%d rows dropped, seam=%s, detail=%s)",
            room_key,
            len(rows),
            seam,
            detail,
        )
        return len(rows)

    # ------------------------------------------------------------ 元数据

    def note_floor(self, floor: int, hp: int, max_hp: int) -> None:
        if not self._floor_hp or self._floor_hp[-1][0] != floor:
            self._floor_hp.append((floor, hp, max_hp))
        else:
            self._floor_hp[-1] = (floor, hp, max_hp)

    def note_hp_delta(self, room_key: str, delta: int) -> None:
        self._hp_deltas[room_key] = delta

    def note_watchdog(self, count: int) -> None:
        self.meta.watchdog_events = max(self.meta.watchdog_events, int(count))

    def write_meta(self) -> None:
        write_json(self.meta_path, _meta_to_dict(self.meta))

    def finish(self, *, result: str, floors_reached: int) -> None:
        self.meta.finished_at = _now_iso()
        self.meta.result = result
        self.meta.floors_reached = floors_reached
        self.write_meta()
        write_json(self.summary_path, self.summary())

    def summary(self) -> dict[str, Any]:
        from spire_core.dataset import summarize_rows

        summary = summarize_rows(self._committed)
        summary.update(
            {
                "run_id": self.run_id,
                # 这一局到底是"人类玩"还是"智能体控制"，落盘里一眼可查
                "mode": self.meta.mode,
                "mod_observe_human": self.meta.mod_observe_human,
                "result": self.meta.result,
                "floors_reached": self.meta.floors_reached,
                "sl_events": [asdict(e) for e in self.meta.sl_events],
                "watchdog_events": self.meta.watchdog_events,
                "floor_hp": [
                    {"floor": f, "hp": hp, "max_hp": mx} for f, hp, mx in self._floor_hp
                ],
                "pending_rooms": sorted(self._pending),
                "committed_rows": len(self._committed),
            }
        )
        return summary

    # ------------------------------------------------------------ 直接读回

    @property
    def committed_rows(self) -> list[dict[str, Any]]:
        return list(self._committed)

    def load_committed(self) -> list[dict[str, Any]]:
        """从磁盘重读已提交行（agent 重启后接着写同一局时用）。"""
        self._committed = read_jsonl(self.decisions_path)
        return list(self._committed)


def _meta_to_dict(meta: RunMeta) -> dict[str, Any]:
    out = asdict(meta)
    out["sl_events"] = [asdict(e) for e in meta.sl_events]
    return out


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def load_run_rows(runs_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """按 run 顺序读出所有已提交行（`dataset/export.py` 的数据源）。"""
    root = Path(runs_dir)
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    for run_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        rows.extend(read_jsonl(run_dir / "decisions.jsonl"))
    return rows


def iter_run_dirs(runs_dir: str | os.PathLike[str]) -> Iterable[Path]:
    root = Path(runs_dir)
    if not root.is_dir():
        return ()
    return sorted(p for p in root.iterdir() if p.is_dir())

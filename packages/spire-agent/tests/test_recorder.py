"""对局记录单测：pending / commit / SL 回滚 / 元数据。"""

import json


from spire_agent.recorder import (
    RunRecorder,
    load_run_rows,
    next_run_id,
)


def row(seq: int, **overrides):
    base = {
        "row_id": f"run-0001#c1#t3#s{seq}",
        "run_id": "run-0001",
        "seq": seq,
        "decision_point": "combat_play",
        "room": {"act": 1, "floor": 7, "node": 5, "combat_instance": 1, "post_sl": False},
        "source": "agent",
        "split": "train",
        "state": {},
        "questions": {},
        "candidate_ids": ["end_turn"],
        "labels": {"q_action": "end_turn"},
        "meta": {"label_source": "agent", "agent_fallback": False, "matched": True},
        "outcome": {"run_won": None, "hp_after": None, "hp_delta_room": None, "floor_reached": 7},
    }
    base.update(overrides)
    return base


def read_lines(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_next_run_id_starts_at_one_and_increments(tmp_path):
    assert next_run_id(tmp_path) == "run-0001"
    (tmp_path / "run-0001").mkdir()
    (tmp_path / "run-0003").mkdir()
    (tmp_path / "not-a-run").mkdir()
    assert next_run_id(tmp_path) == "run-0004"


def test_recorder_creates_run_dir_and_meta(tmp_path):
    rec = RunRecorder(tmp_path, "run-0007")
    assert rec.dir == tmp_path / "run-0007"
    assert rec.pending_dir.is_dir()
    meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
    assert meta["run_id"] == "run-0007"
    assert meta["result"] == "in_progress"
    assert meta["sl_events"] == []


def test_pending_then_commit_moves_rows_to_decisions(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001")
    rec.add_pending("a1_f7_n5_c1", row(1))
    rec.add_pending("a1_f7_n5_c1", row(2))

    assert rec.pending_path("a1_f7_n5_c1").exists()
    assert rec.decisions_path.exists() is False

    assert rec.commit_room("a1_f7_n5_c1") == 2
    assert not rec.pending_path("a1_f7_n5_c1").exists()
    assert rec.pending_rows("a1_f7_n5_c1") == []
    assert [r["seq"] for r in read_lines(rec.decisions_path)] == [1, 2]
    assert [r["seq"] for r in rec.committed_rows] == [1, 2]


def test_commit_empty_room_is_a_noop(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001")
    assert rec.commit_room("a1_f1_n1_c1") == 0
    assert rec.decisions_path.exists() is False


def test_rollback_drops_pending_and_records_sl_event(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001")
    rec.add_pending("a1_f7_n5_c1", row(1, room={"act": 1, "floor": 7, "node": 5,
                                                "combat_instance": 1, "post_sl": False}))

    dropped = rec.rollback_room(
        "a1_f7_n5_c1", seam="state_rewind", detail="turn 1 < 3", combat_instance=2
    )

    assert dropped == 1
    assert not rec.pending_path("a1_f7_n5_c1").exists()
    assert rec.pending_rows("a1_f7_n5_c1") == []
    assert rec.decisions_path.exists() is False
    assert rec.committed_rows == []

    assert len(rec.meta.sl_events) == 1
    event = rec.meta.sl_events[0]
    assert event.dropped_rows == 1
    assert event.combat_instance == 2
    assert event.seam == "state_rewind"
    meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
    assert meta["sl_events"][0]["dropped_rows"] == 1


def test_rollback_keeps_previously_committed_rooms(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001")
    rec.add_pending("a1_f1_n1_c1", row(1))
    rec.commit_room("a1_f1_n1_c1")

    rec.add_pending("a1_f7_n5_c1", row(2))
    rec.rollback_room(
        "a1_f7_n5_c1", seam="mod:loadPlayerSave", detail="mod hook", combat_instance=1
    )
    rec.add_pending("a1_f7_n5_c2", row(3))
    rec.commit_room("a1_f7_n5_c2")

    assert [r["seq"] for r in rec.load_committed()] == [1, 3]


def test_finish_writes_meta_and_summary(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001")
    rec.add_pending("a1_f1_n1_c1", row(1))
    rec.commit_room("a1_f1_n1_c1")
    rec.note_floor(1, 75, 80)
    rec.note_floor(2, 60, 80)
    rec.note_watchdog(2)
    rec.finish(result="death", floors_reached=2)

    meta = json.loads(rec.meta_path.read_text(encoding="utf-8"))
    assert meta["result"] == "death"
    assert meta["floors_reached"] == 2
    assert meta["watchdog_events"] == 2

    summary = json.loads(rec.summary_path.read_text(encoding="utf-8"))
    assert summary["run_id"] == "run-0001"
    assert summary["result"] == "death"
    assert summary["committed_rows"] == 1
    assert summary["rows_total"] == 1
    assert summary["floor_hp"][-1]["hp"] == 60
    assert summary["pending_rooms"] == []


def test_write_raw_and_read_back(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001")
    rec.write_raw(1, {"screen": "NONE"})
    rec.write_raw(2, {"screen": "MAP"}, sl={"count": 1, "seam": "x"})
    raw = read_lines(rec.raw_path)
    assert [r["seq"] for r in raw] == [1, 2]
    assert raw[0]["raw"]["screen"] == "NONE"
    assert raw[1]["sl"]["seam"] == "x"


def test_write_raw_can_be_disabled(tmp_path):
    rec = RunRecorder(tmp_path, "run-0001", save_raw_states=False)
    rec.write_raw(1, {"screen": "NONE"})
    assert not rec.raw_path.exists()


def test_load_run_rows_reads_every_run_in_order(tmp_path):
    first = RunRecorder(tmp_path, "run-0001")
    first.add_pending("a1_f1_n1_c1", row(1))
    first.commit_room("a1_f1_n1_c1")
    second = RunRecorder(tmp_path, "run-0002")
    second.add_pending("a1_f2_n1_c1", row(2, run_id="run-0002"))
    second.commit_room("a1_f2_n1_c1")

    rows = load_run_rows(tmp_path)
    assert [r["run_id"] for r in rows] == ["run-0001", "run-0002"]
    assert load_run_rows(tmp_path / "does-not-exist") == []

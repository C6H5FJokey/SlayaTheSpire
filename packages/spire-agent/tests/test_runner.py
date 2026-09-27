"""agent 主循环：观测 -> 构题 -> 动作 -> 记录，含 SL 回滚与 observe_human。

这里不碰真 socket：桥接换成记录用的假桥，Laya 换成 `httpx.MockTransport`
（回答规则与 `FakeLaya` 完全一致，所以断言的候选命中是确定的）。
"""

import json

import httpx
import pytest

from spire_agent.bridge import IncompatibleMod, Message
from spire_agent.fake_laya import answer_questions
from spire_agent.laya_client import LayaClient
from spire_agent.recorder import RunRecorder
from spire_agent.runner import REJECTION_LIMIT, AgentRunner, ModelUnavailable
from spire_core.config import MODE_AGENT, LayaConfig, MODE_OBSERVE_HUMAN, from_dict

from fixtures import combat_observation, grid_observation, map_observation

COMBAT_ROOM = "a1_f7_n5_c1"


class FakeBridge:
    """记录所有发出的动作，并给出可预测的 message id。"""

    def __init__(self, observe_human: bool = False, *, acked: bool = True):
        mode = MODE_OBSERVE_HUMAN if observe_human else MODE_AGENT
        self.hello = {
            "mod_version": "0.1.0",
            "game_version": "2.3.4",
            "protocol": 2,
            "capabilities": ["observe", "act", "human_action", "watchdog", "configure"],
        }
        # 协议 v2：模组的模式来自 agent 的 configure，agent 只看回执。
        self.configured = {"mode": mode, "watchdog_sec": 30} if acked else {}
        self.sent = []
        self.closed = False
        self.connected = True
        self._next_id = 100

    def connect(self, retry: bool = False) -> bool:
        return True

    def recv(self, timeout: float = 0.2):
        return None

    def send_action(self, *, seq, kind, args=None):
        message_id = self._next_id
        self._next_id += 1
        self.sent.append({"id": message_id, "seq": seq, "kind": kind, "args": args or {}})
        return message_id

    def close(self):
        self.closed = True


def offline_laya_client() -> LayaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read().decode("utf-8") or "{}")
        model = payload.get("model") or "english"
        return httpx.Response(
            200,
            json={
                "model": "laya-rl-agent",
                "answers": answer_questions(payload.get("questions") or {}),
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "routing": {"model": model, "reason": "explicit"},
            },
        )

    config = LayaConfig(base_url="http://laya.test", api_key="k", backoff_base_sec=0.0)
    return LayaClient(
        config, client=httpx.Client(transport=httpx.MockTransport(handler), timeout=1.0)
    )


def dead_laya_client() -> LayaClient:
    """连接必然失败的客户端：模拟"Laya 没起来 / 网络不通"。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    config = LayaConfig(base_url="http://127.0.0.1:9", retries=1, backoff_base_sec=0.0)
    return LayaClient(
        config, client=httpx.Client(transport=httpx.MockTransport(handler), timeout=0.1)
    )


def make_runner(tmp_path, *, mode="agent", laya=True, run_id="run-0001", panel=None):
    config = from_dict(
        {
            "mode": mode,
            "record": {"runs_dir": str(tmp_path)},
            "observe_human": {"also_query_model": True},
        }
    )
    bridge = FakeBridge(observe_human=(mode == MODE_OBSERVE_HUMAN))
    client = offline_laya_client() if laya else None
    recorder = RunRecorder(tmp_path, run_id)
    runner = AgentRunner(config, bridge=bridge, laya=client, recorder=recorder, panel=panel)
    runner.start()
    return runner, bridge, recorder


def observation(seq, raw, **payload):
    body = {"seq": seq, "raw": raw}
    body.update(payload)
    return Message(type="observation", id=0, payload=body)


class ScriptedBridge(FakeBridge):
    """按脚本吐消息，吐完就假装 Ctrl-C（让 run() 的循环停下来）。"""

    def __init__(self, messages, **kwargs):
        super().__init__(**kwargs)
        self._messages = list(messages)

    def recv(self, timeout=0.2):
        if not self._messages:
            raise KeyboardInterrupt
        return self._messages.pop(0)

    def connect(self, retry=False):
        return True


def action_result(message_id, ok=True, **extra):
    body = {"ok": ok}
    body.update(extra)
    return Message(type="action_result", id=message_id, payload=body)


def mod_action_result(action_id, ok=True, mod_id=4242, **extra):
    """模组真实的 framing：动作 id 在 `reply_to`，`id` 是模组自己的出站序号。"""
    body = {"ok": ok}
    body.update(extra)
    return Message(type="action_result", id=mod_id, reply_to=action_id, payload=body)


def human_action(seq, kind, args):
    return Message(type="human_action", id=0, payload={"seq": seq, "kind": kind, "args": args})


def test_agent_decides_and_records_pending_on_ok(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))

    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert sent["seq"] == 1
    # criteria 的字典序最小 key 是 end_turn
    assert sent["kind"] == "end_turn"
    assert recorder.pending_rows(COMBAT_ROOM) == []

    runner.handle_action_result(action_result(sent["id"], ok=True))

    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    row = rows[0]
    assert row["decision_point"] == "combat_play"
    assert row["source"] == "agent"
    assert row["meta"]["label_source"] == "agent"
    assert row["meta"]["matched"] is True
    assert row["meta"]["model_answer"] == "end_turn"
    assert row["meta"]["checkpoint"] == "english"
    assert row["room"]["combat_instance"] == 1
    assert row["room"]["post_sl"] is False
    assert row["labels"]["q_action"] == "end_turn"
    assert row["split"] in ("train", "val", "test")
    assert runner.rows_written == 1


def test_action_result_is_matched_by_reply_to(tmp_path):
    """真模组把动作 id 放在 `reply_to`（`id` 是它自己的出站序号）——必须按 reply_to 配对。

    真机事故：只按 `id` 取，一条 action_result 都匹配不上，跑完一局 `pending/` 一行没有。
    """
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))
    sent = bridge.sent[0]

    runner.handle_action_result(mod_action_result(sent["id"], mod_id=4242, ok=True))

    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    assert rows[0]["decision_point"] == "combat_play"
    assert rows[0]["labels"]["q_action"] == "end_turn"
    assert runner.rows_written == 1


def test_watchdog_result_without_reply_to_clears_the_inflight(tmp_path):
    """看门狗那条没有 `reply_to`，只有 `seq`：按 seq 兜底配对，别留一条 inflight 挂着。"""
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))
    assert len(runner.inflight) == 1

    runner.handle_action_result(
        Message(type="action_result", id=77, payload={"ok": True, "watchdog": True, "seq": 1})
    )

    assert runner.inflight == {}
    assert recorder.pending_rows(COMBAT_ROOM) == []


def test_pending_is_committed_when_leaving_the_room(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1

    next_room = combat_observation()
    next_room["room"] = {"act": 1, "floor": 8, "node": 6, "type": "MONSTER"}
    runner.handle_observation(observation(2, next_room))

    assert recorder.pending_rows(COMBAT_ROOM) == []
    assert not recorder.pending_path(COMBAT_ROOM).exists()
    committed = [json.loads(line) for line in recorder.decisions_path.read_text("utf-8").splitlines()]
    assert [r["room"]["floor"] for r in committed] == [7]


def test_rejected_action_retries_once_with_the_next_candidate(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))

    first = bridge.sent[0]
    assert first["kind"] == "end_turn"
    runner.handle_action_result(action_result(first["id"], ok=False, code="illegal", error="nope"))

    assert len(bridge.sent) == 2
    second = bridge.sent[1]
    assert second["seq"] == 1
    assert second["kind"] != "end_turn"
    assert recorder.pending_rows(COMBAT_ROOM) == []

    runner.handle_action_result(action_result(second["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1


def test_second_rejection_gives_up_and_writes_nothing(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))

    first = bridge.sent[0]
    runner.handle_action_result(action_result(first["id"], ok=False))
    second = bridge.sent[1]
    runner.handle_action_result(action_result(second["id"], ok=False))

    assert len(bridge.sent) == 2
    assert recorder.pending_rows(COMBAT_ROOM) == []
    assert recorder.decisions_path.exists() is False


def test_watchdog_default_is_not_recorded_as_our_decision(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation()))
    sent = bridge.sent[0]

    runner.handle_action_result(action_result(sent["id"], ok=False, watchdog=True))

    assert len(bridge.sent) == 1
    assert recorder.pending_rows(COMBAT_ROOM) == []


def test_watchdog_counter_is_recorded(tmp_path):
    runner, _bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation(), watchdog_events=3))
    assert recorder.meta.watchdog_events == 3


def test_save_scum_rolls_back_the_room_and_restarts_recording(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation(turn=3, hp=68)))
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1

    # 读档：同一节点、回合与 HP 双双回退
    runner.handle_observation(observation(2, combat_observation(turn=1, hp=75)))

    assert recorder.pending_rows(COMBAT_ROOM) == []
    assert not recorder.pending_path(COMBAT_ROOM).exists()
    assert recorder.decisions_path.exists() is False
    assert len(recorder.meta.sl_events) == 1
    event = recorder.meta.sl_events[0]
    assert event.dropped_rows == 1
    assert event.combat_instance == 2
    assert event.seam == "state_rewind"
    assert runner.room_post_sl is True

    assert len(bridge.sent) == 2
    runner.handle_action_result(action_result(bridge.sent[1]["id"], ok=True))
    rows = recorder.pending_rows("a1_f7_n5_c2")
    assert len(rows) == 1
    assert rows[0]["room"]["combat_instance"] == 2
    assert rows[0]["room"]["post_sl"] is True
    assert rows[0]["meta"]["agent_fallback"] is False


def test_mod_load_signal_drops_pending_and_marks_post_sl(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation(turn=3, hp=68)))
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1

    # 模组 hook 到读档入口，但状态还没回退
    runner.handle_observation(
        observation(
            2,
            combat_observation(turn=3, hp=68),
            sl={"count": 1, "seam": "loadPlayerSave"},
        )
    )

    assert recorder.pending_rows(COMBAT_ROOM) == []
    assert runner.room_post_sl is True
    assert len(recorder.meta.sl_events) == 1
    assert recorder.meta.sl_events[0].seam == "mod:loadPlayerSave"
    assert recorder.meta.sl_events[0].dropped_rows == 1

def test_agent_mode_without_a_laya_client_refuses_to_start(tmp_path):
    """默认不保底：没有模型客户端 = 配置错误，直接拒绝开跑，而不是偷偷用规则玩。"""
    config = from_dict({"mode": "agent", "record": {"runs_dir": str(tmp_path)}})
    runner = AgentRunner(
        config, bridge=FakeBridge(), laya=None, recorder=RunRecorder(tmp_path, "run-0001")
    )
    with pytest.raises(ModelUnavailable):
        runner.start()


def test_a_failed_model_call_stops_the_run_instead_of_falling_back(tmp_path):
    """on_error=stop（默认）：一次拿不到模型决策就停跑，绝不发兜底动作。"""
    config = from_dict(
        {"mode": "agent", "record": {"runs_dir": str(tmp_path)}, "laya": {"on_error": "stop"}}
    )
    bridge = FakeBridge()
    runner = AgentRunner(
        config,
        bridge=bridge,
        laya=dead_laya_client(),
        recorder=RunRecorder(tmp_path, "run-0001"),
    )
    runner.start()

    with pytest.raises(ModelUnavailable) as excinfo:
        runner.handle_observation(observation(1, combat_observation()))

    assert "laya unavailable" in str(excinfo.value)
    assert bridge.sent == []                     # 一个动作都没发
    assert runner.rows_written == 0


def test_on_error_fallback_keeps_the_old_rule_policy(tmp_path):
    """显式配 fallback 才回到旧行为：规则兜底、行标 agent_fallback、导出默认排除。"""
    config = from_dict(
        {"mode": "agent", "record": {"runs_dir": str(tmp_path)}, "laya": {"on_error": "fallback"}}
    )
    bridge = FakeBridge()
    recorder = RunRecorder(tmp_path, "run-0001")
    runner = AgentRunner(config, bridge=bridge, laya=dead_laya_client(), recorder=recorder)
    runner.start()

    runner.handle_observation(observation(1, combat_observation()))

    assert len(bridge.sent) == 1
    assert bridge.sent[0]["kind"] == "play_card"
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    assert rows[0]["meta"]["label_source"] == "fallback"
    assert rows[0]["meta"]["agent_fallback"] is True
    assert rows[0]["meta"]["fallback_reason"] == "laya unavailable"


def test_the_mod_mode_comes_from_the_agent(tmp_path):
    """模组没有自己的模式：落盘里的 mod_observe_human 就是 configure 的回执。"""
    config = from_dict({"mode": MODE_OBSERVE_HUMAN, "record": {"runs_dir": str(tmp_path)}})
    recorder = RunRecorder(tmp_path, "run-0001")
    runner = AgentRunner(
        config, bridge=FakeBridge(observe_human=True), laya=None, recorder=recorder
    )
    runner.start()

    assert recorder.meta.mode == MODE_OBSERVE_HUMAN
    assert recorder.meta.mod_observe_human is True


def test_a_mod_that_never_acked_configure_stops_the_run(tmp_path):
    """旧 jar 不认识 configure：agent 会对着哑模组空转，必须当场停跑。"""
    config = from_dict({"mode": "agent", "record": {"runs_dir": str(tmp_path)}})
    runner = AgentRunner(
        config,
        bridge=FakeBridge(acked=False),
        laya=offline_laya_client(),
        recorder=RunRecorder(tmp_path, "run-0001"),
    )

    with pytest.raises(IncompatibleMod) as excinfo:
        runner.start()
    assert "configure" in str(excinfo.value)


def test_the_runner_builds_its_bridge_from_the_config_mode(tmp_path):
    """不注入桥时，mode / watchdog_sec 从配置直推给模组 —— 没有第二处开关。"""
    config = from_dict(
        {
            "mode": MODE_OBSERVE_HUMAN,
            "watchdog_sec": 45,
            "record": {"runs_dir": str(tmp_path)},
        }
    )
    runner = AgentRunner(config, laya=None, recorder=RunRecorder(tmp_path, "run-0001"))

    assert runner.bridge.mode == MODE_OBSERVE_HUMAN
    assert runner.bridge.watchdog_sec == 45


def test_run_returns_4_and_records_the_reason_when_the_model_is_unreachable(tmp_path):
    """退出码 4 + summary.result=model_unavailable：跑批脚本能靠这个判断"没跑成"。"""
    config = from_dict(
        {"mode": "agent", "record": {"runs_dir": str(tmp_path)}, "laya": {"on_error": "stop"}}
    )
    bridge = ScriptedBridge([observation(1, combat_observation())])
    runner = AgentRunner(
        config, bridge=bridge, laya=dead_laya_client(), recorder=RunRecorder(tmp_path, "run-0001")
    )

    assert runner.run() == 4
    assert bridge.sent == []
    summary = json.loads((tmp_path / "run-0001" / "summary.json").read_text("utf-8"))
    assert summary["result"] == "model_unavailable"
    assert summary["mode"] == "agent"
    assert summary["mod_observe_human"] is False


def test_run_returns_5_when_the_mod_is_incompatible(tmp_path):
    """退出码 5 + result=mod_incompatible：旧 jar / 拒配，重试没有意义。"""
    config = from_dict({"mode": "agent", "record": {"runs_dir": str(tmp_path)}})

    class IncompatibleBridge(ScriptedBridge):
        def connect(self, retry=False):
            raise IncompatibleMod("mod speaks protocol 1, agent speaks 2")

    runner = AgentRunner(
        config,
        bridge=IncompatibleBridge([]),
        laya=offline_laya_client(),
        recorder=RunRecorder(tmp_path, "run-0001"),
    )

    assert runner.run() == 5
    summary = json.loads((tmp_path / "run-0001" / "summary.json").read_text("utf-8"))
    assert summary["result"] == "mod_incompatible"
    assert summary["mode"] == "agent"


def test_run_returns_2_and_closes_the_record_when_the_mod_is_not_running(tmp_path):
    """连不上模组也要把这局记成"没连上"，而不是留一个 result=in_progress 的孤儿目录。"""
    config = from_dict({"mode": "agent", "record": {"runs_dir": str(tmp_path)}})

    class NoModBridge(ScriptedBridge):
        def connect(self, retry=False):
            return False

    runner = AgentRunner(
        config,
        bridge=NoModBridge([]),
        laya=offline_laya_client(),
        recorder=RunRecorder(tmp_path, "run-0001"),
    )

    assert runner.run() == 2
    summary = json.loads((tmp_path / "run-0001" / "summary.json").read_text("utf-8"))
    assert summary["result"] == "no_mod"


def test_a_dropped_connection_is_reconnected_instead_of_spinning(tmp_path):
    """跑到一半断线（关游戏 / 换 jar）必须重连，而不是在空队列上无限空转。"""
    config = from_dict({"mode": "agent", "record": {"runs_dir": str(tmp_path)}})

    class DropThenReconnectBridge(ScriptedBridge):
        def __init__(self, messages, **kwargs):
            super().__init__(messages, **kwargs)
            self.connected = True
            self.reconnects = 0
            self.delivered = 0

        def recv(self, timeout=0.2):
            msg = super().recv(timeout)
            self.delivered += 1
            if self.delivered == 1:
                self.connected = False      # 收完第一条就假装游戏被关了
            return msg

        def connect(self, retry=False):
            self.reconnects += 1
            self.connected = True
            return True

    bridge = DropThenReconnectBridge(
        [observation(1, combat_observation()), observation(2, combat_observation())]
    )
    runner = AgentRunner(
        config,
        bridge=bridge,
        laya=offline_laya_client(),
        recorder=RunRecorder(tmp_path, "run-0001"),
    )

    assert runner.run() == 130          # 脚本吐完 = Ctrl-C
    assert bridge.reconnects == 2       # 1 次开跑握手 + 1 次断线重连
    assert len(bridge.sent) == 2        # 第二条观测在重连之后照样被处理
    assert runner.last_obs_seq == 2


def test_game_over_finishes_the_run(tmp_path):
    runner, _bridge, recorder = make_runner(tmp_path)
    raw = combat_observation()
    raw["screen"] = "GAME_OVER"
    raw["victory"] = True

    runner.handle_observation(observation(1, raw))

    assert recorder.meta.result == "victory"
    assert recorder.meta.finished_at != ""
    summary = json.loads(recorder.summary_path.read_text("utf-8"))
    assert summary["result"] == "victory"


def test_map_node_decision_sends_select_map_node(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, map_observation()))

    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert sent["kind"] == "select_map_node"
    assert sent["args"] == {"node": "n4_3"}

    runner.handle_action_result(action_result(sent["id"], ok=True))
    row = recorder.pending_rows("a1_f7_n5_c1")[0]
    assert row["decision_point"] == "map_node"
    assert row["labels"]["q_map"] == "node:n4_3"


def test_must_select_k_asks_k_times_and_writes_k_rows(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, grid_observation(min_select=2, max_select=2)))

    assert len(bridge.sent) == 1
    sent = bridge.sent[0]
    assert sent["kind"] == "select_cards"
    assert sent["args"] == {"indices": [["hand", 0], ["hand", 1]]}

    runner.handle_action_result(action_result(sent["id"], ok=True))
    rows = recorder.pending_rows("a1_f7_n5_c1")
    assert len(rows) == 2
    assert all(r["decision_point"] == "select_card_must_k" for r in rows)
    picked = [r["labels"]["q_pick"][0] for r in rows]
    assert picked == ["card:hand:0", "card:hand:1"]
    already = rows[1]["state"]["already_selected"]
    assert len(already) == 1
    assert already[0].startswith("- ALREADY SELECTED: Strike")
    assert already[0].endswith("(card:hand:0)")


def test_select_card_any_uses_score_questions(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, grid_observation(min_select=0, max_select=3)))

    sent = bridge.sent[0]
    assert sent["kind"] == "select_cards"
    assert sent["args"]["indices"] == [["hand", 0], ["hand", 1], ["hand", 2]]

    runner.handle_action_result(action_result(sent["id"], ok=True))
    row = recorder.pending_rows("a1_f7_n5_c1")[0]
    assert row["decision_point"] == "select_card_any"
    assert row["questions"]["q_card_card_hand_0"]["type"] == "score"
    assert set(row["labels"]["q_card_card_hand_0"]) == {
        "card:hand:0",
        "card:hand:1",
        "card:hand:2",
    }


def test_observe_human_matched_records_a_strong_label(tmp_path):
    runner, bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))

    assert bridge.sent == []
    assert 1 in runner.plans

    runner.handle_human_action(human_action(1, "play_card", {"hand_index": 0, "target": "m0"}))

    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "human"
    assert row["meta"]["label_source"] == "human"
    assert row["meta"]["matched"] is True
    assert row["meta"]["human_action"] == {
        "kind": "play_card",
        "args": {"hand_index": 0, "target": "m0"},
    }
    assert row["labels"]["q_action"] == "play:h0->m0"
    assert runner.matched == 1 and runner.unmatched == 0
    assert runner.armed is None


def test_observe_human_ignores_a_duplicated_action(tmp_path):
    """模组对同一次出牌可能重复上报同一个 seq，只能记一行。"""
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))

    action = human_action(1, "play_card", {"hand_index": 0, "target": "m0"})
    runner.handle_human_action(action)
    runner.handle_human_action(action)

    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1
    assert runner.matched == 1 and runner.unmatched == 0


def test_observe_human_keeps_one_plan_per_observation(tmp_path):
    """一轮里连打两张牌：每张都对自己的 seq 构题，互不覆盖。"""
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))
    runner.handle_human_action(human_action(1, "play_card", {"hand_index": 0, "target": "m0"}))
    runner.handle_observation(observation(2, combat_observation()))
    runner.handle_human_action(human_action(2, "play_card", {"hand_index": 0, "target": "m0"}))

    assert sorted(runner.plans) == [1, 2]
    rows = recorder.pending_rows(COMBAT_ROOM)
    assert [r["seq"] for r in rows] == [1, 2]
    assert all(r["meta"]["matched"] for r in rows)


def test_observe_human_unmatched_keeps_the_raw_action(tmp_path):
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))

    runner.handle_human_action(human_action(1, "play_card", {"hand_index": 99, "target": "m9"}))

    row = recorder.pending_rows(COMBAT_ROOM)[0]
    assert row["meta"]["matched"] is False
    assert row["labels"]["q_action"] is None
    assert row["meta"]["human_action"]["args"] == {"hand_index": 99, "target": "m9"}
    assert runner.unmatched == 1


def test_observe_human_without_armed_plan_is_counted(tmp_path):
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_human_action(human_action(1, "proceed", {}))

    assert runner.unmatched == 1
    assert recorder.pending_rows(COMBAT_ROOM) == []


def test_observe_human_action_for_an_older_seq_is_dropped(tmp_path):
    """拿不到那一次构题现场就不记 —— 编一行 (state, action) 不搭的数据更糟。"""
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(7, combat_observation()))
    runner.handle_human_action(human_action(3, "play_card", {"hand_index": 0, "target": "m0"}))

    assert runner.unmatched == 1
    assert recorder.pending_rows(COMBAT_ROOM) == []


def test_observe_human_unknown_screen_clears_the_armed_plan(tmp_path):
    """认不出决策点时不能留着旧构题现场，否则界面事件会写出错配的行。"""
    runner, _bridge, _recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))
    runner.armed = runner.plans[1]
    runner.handle_observation(observation(2, dict(combat_observation(), screen="NONE", in_combat=False)))

    assert runner.armed is None


def test_observation_seq_restart_drops_plan_history(tmp_path):
    runner, _bridge, _recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(5, combat_observation()))
    runner.handle_observation(observation(1, combat_observation()))

    assert sorted(runner.plans) == [1]


def test_observe_human_records_the_model_contrast(tmp_path):
    """`also_query_model` 的对照答案必须落进 meta，否则 agreement_rate 是假的。

    离线 Laya 的规则是"取 criteria 字典序最小 key"，对这份战斗观测就是 end_turn。
    """
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))

    runner.handle_human_action(human_action(1, "play_card", {"hand_index": 0, "target": "m0"}))

    meta = recorder.pending_rows(COMBAT_ROOM)[0]["meta"]
    assert meta["model_answer"] == "end_turn"      # 模型自己的选择
    assert meta["agreement"] is False              # 人类打了 play:h0->m0，与模型不一致
    assert meta["model_confidence"] == 1.0
    assert meta["checkpoint"] == "english"


def test_observe_human_marks_agreement_when_the_model_would_have_done_the_same(tmp_path):
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, combat_observation()))

    runner.handle_human_action(human_action(1, "end_turn", {}))

    row = recorder.pending_rows(COMBAT_ROOM)[0]
    assert row["labels"]["q_action"] == "end_turn"
    assert row["meta"]["model_answer"] == "end_turn"
    assert row["meta"]["agreement"] is True


def test_observe_human_without_also_query_model_has_no_contrast_fields(tmp_path):
    config = from_dict(
        {
            "mode": MODE_OBSERVE_HUMAN,
            "record": {"runs_dir": str(tmp_path)},
            "observe_human": {"also_query_model": False},
        }
    )
    bridge = FakeBridge(observe_human=True)
    recorder = RunRecorder(tmp_path, "run-0001")
    runner = AgentRunner(config, bridge=bridge, laya=None, recorder=recorder)
    runner.start()

    runner.handle_observation(observation(1, combat_observation()))
    runner.handle_human_action(human_action(1, "end_turn", {}))

    meta = recorder.pending_rows(COMBAT_ROOM)[0]["meta"]
    assert "model_answer" not in meta and "agreement" not in meta
    assert meta["label_source"] == "human"


def test_observe_human_keeps_collecting_when_laya_is_down(tmp_path):
    """对照模型挂了不该毁掉采集：人类标签照记，只是那一步没有对照字段。"""
    config = from_dict(
        {
            "mode": MODE_OBSERVE_HUMAN,
            "record": {"runs_dir": str(tmp_path)},
            "observe_human": {"also_query_model": True},
        }
    )
    bridge = FakeBridge(observe_human=True)
    recorder = RunRecorder(tmp_path, "run-0001")
    runner = AgentRunner(config, bridge=bridge, laya=dead_laya_client(), recorder=recorder)
    runner.start()

    runner.handle_observation(observation(1, combat_observation()))
    runner.handle_human_action(human_action(1, "end_turn", {}))

    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    assert rows[0]["meta"]["label_source"] == "human"
    assert rows[0]["labels"]["q_action"] == "end_turn"
    assert "model_answer" not in rows[0]["meta"]
    assert bridge.sent == []                     # observe 模式永不替人类发动作


def test_observe_human_panel_event_carries_a_readable_model_answer(tmp_path):
    """面板要能直接显示模型对照，事件里必须是答案字符串而不是原始 answers 字典。"""
    events: list[dict] = []
    runner, _bridge, _recorder = make_runner(
        tmp_path, mode=MODE_OBSERVE_HUMAN, panel=events.append
    )
    runner.handle_observation(observation(1, combat_observation()))

    prompt = [e for e in events if e["kind"] == "human_prompt"][-1]
    assert prompt["model_answer"] == "end_turn"
    assert prompt["model_confidence"] == 1.0
    assert prompt["candidates"]


def test_observe_human_card_picks_are_split_into_k_rows(tmp_path):
    runner, _bridge, recorder = make_runner(tmp_path, mode=MODE_OBSERVE_HUMAN)
    runner.handle_observation(observation(1, grid_observation(min_select=2, max_select=2)))

    runner.handle_human_action(
        human_action(1, "select_cards", {"indices": [["hand", 0], ["hand", 2]]})
    )

    rows = recorder.pending_rows("a1_f7_n5_c1")
    assert len(rows) == 2
    assert [r["labels"]["q_pick"][0] for r in rows] == ["card:hand:0", "card:hand:2"]
    assert all(r["meta"]["matched"] for r in rows)
    assert all(r["source"] == "human" for r in rows)
    assert rows[0]["meta"]["note"] == "card select pick 1/2"

def _load_export_module():
    """`dataset/export.py` 不是包，按文件路径加载（避免污染 sys.path）。"""
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[3] / "dataset" / "export.py"
    spec = importlib.util.spec_from_file_location("spire_export", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_to_export_to_replay_round_trip(tmp_path):
    """真 runner -> decisions.jsonl -> 导出(确定性) -> 回放：一条不断裂的链路。"""
    from spire_core import dataset as ds
    from spire_core import replay
    from spire_core.config import DatasetConfig

    runner, bridge, recorder = make_runner(tmp_path)

    for floor in (7, 8, 9):
        raw = combat_observation(turn=1, hp=70 - floor)
        raw["room"] = {"act": 1, "floor": floor, "node": floor - 2, "type": "MONSTER"}
        runner.handle_observation(observation(floor, raw))
        runner.handle_action_result(action_result(bridge.sent[-1]["id"], ok=True))

    # 局终（这里用 stop 模拟）：提交最后一间房并写 summary
    runner.stop(result="aborted")

    committed = recorder.load_committed()
    assert len(committed) == 3
    assert [r["room"]["floor"] for r in committed] == [7, 8, 9]
    assert {r["source"] for r in committed} == {"agent"}
    assert all(r["meta"]["agent_fallback"] is False for r in committed)

    export = _load_export_module()
    cfg = DatasetConfig()

    files, manifest = export.build_outputs(tmp_path, cfg)
    assert manifest["counts"]["total_rows"] == 3
    assert manifest["counts"]["kept_rows"] == 3
    assert manifest["counts"]["dropped"] == {"post_sl": 0, "fallback": 0, "unmatched": 0}
    assert manifest["split_rule"]["by"] == "run_id"

    # 同一局的所有行必须在同一个 split 里（按 run 切分 => 无跨集泄漏）
    split = committed[0]["split"]
    assert split == ds.split_of(
        "run-0001",
        split_seed=cfg.split_seed,
        val_ratio=cfg.val_ratio,
        test_ratio=cfg.test_ratio,
    )
    assert all(r["split"] == split for r in committed)
    assert any(b"\n" in blob for blob in files.values())

    out = tmp_path / "out"
    assert export.export_main(runs_dir=tmp_path, out_dir=out, cfg=cfg) == 0
    # 确定性：重算一遍必须与已落盘的文件逐字节一致
    assert export.export_main(runs_dir=tmp_path, out_dir=out, verify=True, cfg=cfg) == 0

    # choice 行可以无损展开成 noul（每个候选一行）
    assert export.export_main(runs_dir=tmp_path, out_dir=out, expand_noul=True, cfg=cfg) == 0
    noul_path = out / f"{split}.noul.jsonl"
    noul_rows = [json.loads(line) for line in noul_path.read_text("utf-8").splitlines() if line]
    (qid, question), = committed[0]["questions"].items()
    assert len(noul_rows) == 3 * len(question["criteria"])
    assert all(isinstance(r["labels"][qid], bool) for r in noul_rows)
    assert sum(1 for r in noul_rows if r["labels"][qid]) == 3

    # 回放：同样的 (state, questions, answers) 必须重建出同样的动作
    report = replay.replay_all(committed)
    assert report["ok"] is True, report["mismatches"]
    assert report["total"] == 3


def test_export_skips_runs_marked_excluded(tmp_path):
    """`EXCLUDED.txt` 把整局摘出导出，原因进 manifest，原始记录不删。"""
    from spire_core.config import DatasetConfig

    runner, bridge, recorder = make_runner(tmp_path)
    raw = combat_observation(turn=1, hp=70)
    raw["room"] = {"act": 1, "floor": 7, "node": 5, "type": "MONSTER"}
    runner.handle_observation(observation(7, raw))
    runner.handle_action_result(action_result(bridge.sent[-1]["id"], ok=True))
    runner.stop(result="aborted")

    export = _load_export_module()
    cfg = DatasetConfig()
    files, manifest = export.build_outputs(tmp_path, cfg)
    assert manifest["counts"]["total_rows"] == 1
    assert manifest["counts"]["kept_rows"] == 1
    assert manifest["excluded_runs"] == {}

    run_dir = tmp_path / "run-0001"
    assert (run_dir / "decisions.jsonl").exists()
    (run_dir / "EXCLUDED.txt").write_text("检测器误报\n细节若干\n", encoding="utf-8")

    files, manifest = export.build_outputs(tmp_path, cfg)
    assert manifest["counts"]["total_rows"] == 0
    assert manifest["excluded_runs"] == {"run-0001": "检测器误报"}
    assert all(blob == b"" for blob in files.values())
    # 原始记录必须原样留着，隔离只发生在导出这一层
    assert (run_dir / "decisions.jsonl").exists()


def test_seq_restart_starts_a_new_session_and_drops_the_room(tmp_path):
    """游戏重启（观测 seq 归零）：检测器换新，未提交的房间作废。

    跨会话留着 `RoomTracker._seen` 会把"重开一局又走到同一个节点"当成重访节点：
    真机上结算画面停在 act4 boss 节点不动，这样误报了 9 次。
    """
    published = []
    runner, bridge, recorder = make_runner(tmp_path, panel=published.append)
    runner.handle_observation(observation(1, combat_observation(turn=3, hp=68)))
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1

    # 新会话 + 不同种子 => 新的一局，不是续玩
    raw = combat_observation(turn=1, hp=75)
    raw["run_seed"] = 1234
    runner.handle_observation(observation(1, raw))

    assert recorder.pending_rows(COMBAT_ROOM) == []
    assert not recorder.pending_path(COMBAT_ROOM).exists()
    assert runner.room_post_sl is False
    assert runner.tracker.combat_instance == 1
    assert [e.seam for e in recorder.meta.sl_events] == ["session_restart"]
    assert recorder.meta.sl_events[0].dropped_rows == 1
    assert "abandoned" in recorder.meta.sl_events[0].detail

    # 面板必须能看到这件事（docs/09-observability.md#事件流）
    sl_events = [e for e in published if e["kind"] == "sl_event"]
    assert len(sl_events) == 1
    assert sl_events[0]["dropped_rows"] == 1
    assert sl_events[0]["seam"] == "session_restart"


def test_seq_restart_with_the_same_seed_is_a_resume(tmp_path):
    """种子没变且上一局没结束 => 退出游戏再继续 = 读档（存档在房间入口）。"""
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation(turn=3, hp=68)))
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))

    runner.handle_observation(observation(1, combat_observation(turn=1, hp=68)))

    assert recorder.pending_rows(COMBAT_ROOM) == []
    assert runner.room_post_sl is True
    assert recorder.meta.sl_events[0].seam == "session_restart"
    assert "resumed" in recorder.meta.sl_events[0].detail

    # 重记的这间房整体标 post_sl（抽牌顺序 / 敌人行动人类已经知道，标签被污染）
    runner.handle_action_result(action_result(bridge.sent[-1]["id"], ok=True))
    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    assert rows[0]["room"]["post_sl"] is True


def test_mod_load_signal_before_any_room_is_not_a_save_scum(tmp_path):
    """`CardCrawlGame.loadPlayerSave` 是"从主菜单开一局"的路径，每次启动都会响。

    真机上 10 次会话启动 = 10 笔假 SL（见 docs/08-dataset.md#sl-语义）。
    """
    runner, _bridge, recorder = make_runner(tmp_path)
    raw = combat_observation()
    raw["in_combat"] = False
    raw["combat"] = {}
    raw["room"] = {"act": 0, "floor": 0, "node": 0, "type": "UNKNOWN"}

    runner.handle_observation(observation(1, raw, sl={"count": 1, "seam": "loadPlayerSave"}))

    assert recorder.meta.sl_events == []
    assert runner.room_post_sl is False


def test_out_of_run_screen_does_not_commit_the_room(tmp_path):
    """回主菜单（局外观测）不能算"离开节点"，否则那间房的 pending 会被提前提交。

    真机上的 SL 路径就是"战斗中退出到主菜单再继续"：pending 必须留着等着被回滚。
    """
    runner, bridge, recorder = make_runner(tmp_path)
    runner.handle_observation(observation(1, combat_observation(turn=3, hp=68)))
    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1

    raw = combat_observation(turn=3, hp=68)
    raw["in_combat"] = False
    raw["combat"] = {}
    raw["room"] = {"act": 0, "floor": 0, "node": 0, "type": "UNKNOWN"}
    runner.handle_observation(observation(2, raw))

    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1
    assert recorder.load_committed() == []


def test_agent_start_drops_pending_left_by_an_interrupted_session(tmp_path):
    """agent 被杀留下的 pending 不能信，也不能让新会话的行追加进同一个文件。"""
    first, bridge, recorder = make_runner(tmp_path)
    first.handle_observation(observation(1, combat_observation(turn=3, hp=68)))
    first.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    assert len(recorder.pending_rows(COMBAT_ROOM)) == 1

    # 同一个 run 目录重开（agent 被杀后 --run-id 续接当前局）
    restarted = RunRecorder(tmp_path, "run-0001")
    config = from_dict({"mode": "agent", "record": {"runs_dir": str(tmp_path)}})
    runner = AgentRunner(
        config, bridge=FakeBridge(), laya=offline_laya_client(), recorder=restarted
    )
    runner.start()

    assert restarted.pending_rows(COMBAT_ROOM) == []
    assert not restarted.pending_path(COMBAT_ROOM).exists()
    assert [e.seam for e in restarted.meta.sl_events] == ["agent_start"]
    assert restarted.meta.sl_events[0].dropped_rows == 1
    # 上一次的局还没结束 => 下一间房按"续玩"标 post_sl
    assert runner.post_sl_next_room is True


def reject_everything(runner, bridge, start: int) -> int:
    """把这一轮发出的动作全拒掉（含重试），返回本轮发了几个。"""
    cursor = start
    while len(bridge.sent) > cursor:
        message = bridge.sent[cursor]
        cursor += 1
        runner.handle_action_result(
            action_result(message["id"], ok=False, code="E_ILLEGAL_ACTION", error="nope")
        )
    return cursor - start


def test_repeated_rejections_on_an_unchanged_state_stop_the_spam(tmp_path):
    """模组一直拒、state 又不变 = 死循环（真机上那次是空药水槽）。

    旧写法每来一次观测就重发一轮，永远停不下来，还把看门狗一直压着不触发：
    真机上它每秒多在"用药水", 游戏却一步没动。攒够 `REJECTION_LIMIT` 轮就停发，
    这一屏交给看门狗（它超时后会结束回合），state 一变计数自然清零。
    """
    runner, bridge, recorder = make_runner(tmp_path)
    raw = combat_observation()
    rounds = []

    for seq in range(1, REJECTION_LIMIT + 3):
        start = len(bridge.sent)
        runner.handle_observation(observation(seq, raw))
        rounds.append(reject_everything(runner, bridge, start))

    # 停发之前每轮是"原动作 + 一次重试"；之后整轮一个都不发。
    assert rounds[:REJECTION_LIMIT] == [2] * REJECTION_LIMIT
    assert rounds[REJECTION_LIMIT:] == [0, 0]
    assert recorder.pending_rows(COMBAT_ROOM) == []


def spy_on_laya(runner):
    """记录 Laya 被问了几次，同时保持真实回答。

    不能让替身返回 None —— 那会被当成"服务不可用"，直接抛 ModelUnavailable。
    """
    real_ask = runner.laya.ask
    asked = []
    runner.laya.ask = lambda plan: (asked.append(plan.decision_point), real_ask(plan))[1]
    return asked


def test_single_legal_action_is_forced_without_asking_the_model(tmp_path):
    """只有一个合法动作时**不调 Laya**（见 docs/06#强制决策短路）。

    没有可决策的东西时问模型，纯属浪费 budget，还给了模型在无选择余地的局面上
    乱答的机会。0 能量 + 无药水 + 手牌全打不出来 -> 只有 end_turn。
    """
    runner, bridge, recorder = make_runner(tmp_path)
    asked = spy_on_laya(runner)

    # potions=[] 而不是 None：None 会落回夹具默认的"一瓶火药剂"。
    raw = combat_observation(energy=0, potions=[])
    for c in raw["combat"]["hand"]:
        c["is_playable"] = False
    runner.handle_observation(observation(1, raw))

    assert asked == []
    assert [m["kind"] for m in bridge.sent] == ["end_turn"]

    runner.handle_action_result(action_result(bridge.sent[0]["id"], ok=True))
    rows = recorder.pending_rows(COMBAT_ROOM)
    assert len(rows) == 1
    row = rows[0]
    assert row["labels"]["q_action"] == "end_turn"
    # 强制决策不是兜底：它照常进训练集，只是能事后分辨出来。
    assert row["meta"]["label_source"] == "agent"
    assert row["meta"]["agent_fallback"] is False
    assert row["meta"]["forced"] is True
    assert "model_answer" not in row["meta"]


def test_each_model_call_is_published_to_the_panel_with_its_decision(tmp_path):
    """面板要能回答"这一次决策到底发了什么、拿回了什么"（见 docs/09）。"""
    events = []
    runner, bridge, recorder = make_runner(tmp_path, panel=events.append)

    runner.handle_observation(observation(1, combat_observation()))

    exchanges = [e for e in events if e["kind"] == "exchange"]
    assert len(exchanges) == 1
    record = exchanges[0]
    # 请求/返回是原样的，seq 与决策点是 runner 贴上去的
    assert record["seq"] == 1
    assert record["decision_point"] == "combat_play"
    assert record["request"]["questions"]["q_action"]["criteria"]
    assert record["response"]["answers"]["q_action"]["choice"]
    assert record["ok"] is True and record["status"] == 200
    assert record["checkpoint"] == "english"


def test_a_forced_decision_publishes_no_exchange(tmp_path):
    """唯一合法动作不问模型，所以面板上不该冒出一条假的请求记录。"""
    events = []
    runner, bridge, recorder = make_runner(tmp_path, panel=events.append)
    raw = combat_observation(energy=0, potions=[])
    for c in raw["combat"]["hand"]:
        c["is_playable"] = False

    runner.handle_observation(observation(1, raw))

    assert [e for e in events if e["kind"] == "exchange"] == []
    assert [e for e in events if e["kind"] == "decision"][0]["forced"] is True


def test_exchanges_carry_the_seq_of_the_decision_they_belong_to(tmp_path):
    """连续几次观测之后，每条往返都要对上自己那次决策的 seq。"""
    events = []
    runner, bridge, recorder = make_runner(tmp_path, panel=events.append)

    for seq in (1, 2):
        runner.handle_observation(observation(seq, combat_observation()))
        runner.handle_action_result(action_result(bridge.sent[-1]["id"], ok=True))

    assert [e["seq"] for e in events if e["kind"] == "exchange"] == [1, 2]


def test_select_card_any_with_one_candidate_still_asks_the_model(tmp_path):
    """`select_card_any` 的"一张都不选"是个真实选项，剩一个候选也不算没得选。"""
    runner, bridge, recorder = make_runner(tmp_path)
    asked = spy_on_laya(runner)

    raw = grid_observation(min_select=0, max_select=2)
    raw["screen_state"]["select_cards"] = raw["screen_state"]["select_cards"][:1]
    runner.handle_observation(observation(1, raw))

    assert asked == ["select_card_any"]


def test_a_changed_state_clears_the_rejection_counter(tmp_path):
    """计数是"这个 state"的，不是全局的：状态一变（看门狗结束回合等）就恢复发动作。"""
    runner, bridge, recorder = make_runner(tmp_path)
    raw = combat_observation()

    for seq in range(1, REJECTION_LIMIT + 2):
        start = len(bridge.sent)
        runner.handle_observation(observation(seq, raw))
        reject_everything(runner, bridge, start)
    assert len(bridge.sent) == 2 * REJECTION_LIMIT

    start = len(bridge.sent)
    runner.handle_observation(observation(99, combat_observation(energy=2)))

    assert len(bridge.sent) > start

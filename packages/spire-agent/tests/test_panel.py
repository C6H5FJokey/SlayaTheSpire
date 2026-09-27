"""只读观战面板：状态容器 + HTTP 服务 + loopback 约束。"""

import json
import re
import shutil
import subprocess
import urllib.request

import pytest

from spire_agent.panel import PAGE, PanelServer, PanelState


def exchange_event(exchange_id, *, request_blob="x" * 500):
    """一次典型的往返记录：请求体很大，返回体很小 —— 正是面板必须分开存的原因。"""
    return {
        "kind": "exchange",
        "id": exchange_id,
        "at": 1000.0 + exchange_id,
        "seq": exchange_id,
        "decision_point": "combat_play",
        "attempt": 1,
        "checkpoint": "english",
        "ok": True,
        "status": 200,
        "cached": False,
        "error": None,
        "latency_ms": 12,
        "request": {"state": {"blob": request_blob}},
        "request_bytes": len(request_blob),
        "response": {"answers": {"q_action": {"choice": "end_turn"}}, "routing": {"model": "english"}},
        "response_bytes": 60,
        "answer": {"choice": "end_turn"},
    }

SCREEN_LABEL_CASES = """
const cases = [
  [{ screen: "NONE" }, { decision_point: "combat_play" }],
  [{ screen: "NONE" }, { decision_point: "map_node" }],
  [{ screen: "MAP" }, { decision_point: "map_node" }],
];
for (const c of cases) console.log(screenLabel(c[0], c[1]));
"""


def test_state_update_and_snapshot():
    state = PanelState(timeline_size=10)
    state.update({"kind": "observation", "seq": 1, "hp": 50})
    state.update({"kind": "decision", "chosen": "end_turn", "fallback": False})

    snapshot = state.snapshot()
    assert snapshot["observation"]["seq"] == 1
    assert snapshot["decision"]["chosen"] == "end_turn"
    assert snapshot["counters"] == {"observation": 1, "decision": 1}
    assert [e["kind"] for e in snapshot["timeline"]] == ["observation", "decision"]
    assert snapshot["updated_at"] > 0


def test_timeline_is_bounded():
    state = PanelState(timeline_size=2)
    for i in range(5):
        state.update({"kind": "observation", "seq": i})
    assert [e["seq"] for e in state.snapshot()["timeline"]] == [3, 4]


def test_human_prompt_is_cleared_by_human_action():
    state = PanelState()
    state.update({"kind": "human_prompt", "decision_point": "combat_play", "candidates": ["a"]})
    assert state.snapshot()["human_prompt"]["decision_point"] == "combat_play"
    state.update({"kind": "human_action", "matched": True})
    assert state.snapshot()["human_prompt"] == {}


def test_snapshot_is_a_copy():
    state = PanelState()
    state.update({"kind": "observation", "seq": 1})
    snapshot = state.snapshot()
    snapshot["observation"]["seq"] = 999
    assert state.snapshot()["observation"]["seq"] == 1


def test_server_refuses_non_loopback():
    with pytest.raises(ValueError):
        PanelServer(host="0.0.0.0", port=0)
    with pytest.raises(ValueError):
        PanelServer(host="192.168.1.10", port=0)


def test_server_serves_state_and_health():
    server = PanelServer(host="127.0.0.1", port=0, timeline_size=5)
    url = server.start()
    try:
        server.update({"kind": "observation", "seq": 9, "hp": 61, "max_hp": 80})

        with urllib.request.urlopen(url + "api/state", timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["observation"]["seq"] == 9
        assert body["counters"]["observation"] == 1

        with urllib.request.urlopen(url + "health", timeout=10) as resp:
            assert json.loads(resp.read().decode("utf-8"))["ok"] is True

        with urllib.request.urlopen(url, timeout=10) as resp:
            page = resp.read().decode("utf-8")
        assert "SlayaTheSpire" in page
        assert "/api/state" in page
        # agent 停掉后面板就没人应答了 —— 那句话必须是"agent 已停止"，不能说成网络问题
        assert "agent 已停止" in page
        assert "重启 agent 后本页会自动恢复" in page
    finally:
        server.stop()


def test_server_returns_404_for_unknown_path():
    server = PanelServer(port=0)
    url = server.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            urllib.request.urlopen(url + "nope", timeout=10)
        assert excinfo.value.code == 404
    finally:
        server.stop()


def test_server_is_callable_as_a_panel_sink():
    server = PanelServer(port=0)
    server.start()
    try:
        server({"kind": "decision", "chosen": "end_turn"})
        assert server.state.snapshot()["decision"]["chosen"] == "end_turn"
    finally:
        server.stop()


def test_panel_js_parses_and_labels_the_in_combat_screen(tmp_path):
    """页面脚本嵌在 Python 字符串里，pytest 看不出 JS 语法错 —— 有 node 就交给它。

    这是给"改页面的人"的一道闸：没有 node 就跳过（面板本身不需要 node）。
    """
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")

    js = re.search(r"<script>(.*?)</script>", PAGE, re.S).group(1)
    whole = tmp_path / "panel.js"
    whole.write_text(js, encoding="utf-8")
    # node 往管道写的是 UTF-8，不能让它按本机代码页（简中是 GBK）解码，否则中文直接炸。
    checked = subprocess.run([node, "--check", str(whole)], capture_output=True, encoding="utf-8")
    assert checked.returncode == 0, checked.stderr

    # 顺带跑一遍 screenLabel：NONE + 战斗决策点要说清"战斗内"，别只丢个 NONE
    labels = tmp_path / "labels.mjs"
    labels.write_text(js.split("async function tick()")[0] + SCREEN_LABEL_CASES, encoding="utf-8")
    out = subprocess.run([node, str(labels)], capture_output=True, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == ["NONE（战斗内）", "NONE（无特殊界面）", "MAP"]


# --------------------------------------------------------------------------- #
# Laya 请求 / 返回：列表只放摘要，原文按 id 取
# --------------------------------------------------------------------------- #


def test_exchanges_are_kept_whole_but_listed_as_summaries():
    """摘要进 /api/state（每秒轮询），原文留在缓冲里等 /api/exchange/<id>。"""
    state = PanelState(timeline_size=5, exchange_size=2)
    for i in range(1, 4):
        state.update(exchange_event(i))

    snapshot = state.snapshot()
    assert [e["id"] for e in snapshot["exchanges"]] == [2, 3]  # 环形缓冲挤掉最老的
    assert snapshot["exchanges"][0]["served"] == "english"
    assert snapshot["exchanges"][0]["answer"] == {"choice": "end_turn"}

    # 关键：轮询体里绝不能夹带请求体。500 个 x 在原文里，不该出现在摘要里。
    dumped = json.dumps(snapshot, ensure_ascii=False)
    assert "x" * 50 not in dumped
    assert "request" not in snapshot["exchanges"][0]

    assert state.exchange(3)["request"]["state"]["blob"] == "x" * 500  # 原文取得到
    assert state.exchange(1) is None  # 已被挤出缓冲


def test_exchange_shows_up_in_the_timeline_as_a_summary():
    state = PanelState(timeline_size=5)
    state.update(exchange_event(1))

    entry = state.snapshot()["timeline"][0]
    assert entry["kind"] == "exchange" and entry["id"] == 1
    assert "request" not in entry
    assert state.snapshot()["counters"] == {"exchange": 1}


def test_server_serves_the_raw_exchange_by_id():
    server = PanelServer(port=0, exchange_size=2)
    url = server.start()
    try:
        server.update(exchange_event(7))

        with urllib.request.urlopen(url + "api/exchange/7", timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        assert body["request"]["state"]["blob"] == "x" * 500
        assert body["response"]["answers"]["q_action"]["choice"] == "end_turn"

        with urllib.request.urlopen(url + "api/state", timeout=10) as resp:
            listing = json.loads(resp.read().decode("utf-8"))
        assert [e["id"] for e in listing["exchanges"]] == [7]

        for path, code in (("api/exchange/99", 404), ("api/exchange/abc", 400)):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                urllib.request.urlopen(url + path, timeout=10)
            assert excinfo.value.code == code
    finally:
        server.stop()


def test_page_can_open_an_exchange_by_id(tmp_path):
    """页面里要有"取原文"的那条路：没有它，列表点开是空的。"""
    assert "/api/exchange/" in PAGE
    assert 'data-ex=' in PAGE
    assert "renderExchangeDetail" in PAGE

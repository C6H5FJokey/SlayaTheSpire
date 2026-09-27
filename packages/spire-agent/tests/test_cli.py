"""CLI 顶层管线：`--mode` 是唯一模式开关，必须原样传到桥接与 runner。

这里不碰真 socket / 真 HTTP：`load_config` / `ModBridge` / `AgentRunner` / `LayaClient`
全部替换成假对象，只断言"谁来承载模式"。
"""

from spire_agent import cli
from spire_core.config import MODE_AGENT, MODE_OBSERVE_HUMAN, from_dict


class FakeBridge:
    def __init__(self, config, *, mode=MODE_AGENT, watchdog_sec=30):
        self.config = config
        self.mode = mode
        self.watchdog_sec = watchdog_sec
        self.hello = {}
        self.configured = {"mode": mode, "watchdog_sec": watchdog_sec}
        self.closed = False

    def connect(self, retry=False):
        return True

    def close(self):
        self.closed = True


class FakeLaya:
    def __init__(self, config):
        self.config = config

    def ask_raw(self, body):
        return None


def test_doctor_pushes_the_cli_mode_to_the_mod(monkeypatch):
    seen = {}

    class RecordingBridge(FakeBridge):
        def __init__(self, config, **kwargs):
            super().__init__(config, **kwargs)
            seen.update(kwargs)

    monkeypatch.setattr(cli, "load_config", lambda path: from_dict({"mode": MODE_AGENT}))
    monkeypatch.setattr(cli, "ModBridge", RecordingBridge)
    monkeypatch.setattr(cli, "LayaClient", FakeLaya)
    monkeypatch.setattr(cli, "warn_stale_mod_keys", lambda: None)

    # Laya 探不通 -> 退出码 1，但模组那半已经验完了
    assert cli.main(["doctor", "--mode", MODE_OBSERVE_HUMAN]) == 1
    assert seen["mode"] == MODE_OBSERVE_HUMAN


def test_run_mode_flag_overrides_the_config(monkeypatch):
    captured = {}

    class RecordingRunner:
        def __init__(self, config, **kwargs):
            captured["mode"] = config.mode

        def run(self):
            return 0

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda path: from_dict({"mode": MODE_AGENT, "panel": {"enabled": False}}),
    )
    monkeypatch.setattr(cli, "AgentRunner", RecordingRunner)
    monkeypatch.setattr(cli, "ModBridge", FakeBridge)
    monkeypatch.setattr(cli, "LayaClient", FakeLaya)
    monkeypatch.setattr(cli, "RunRecorder", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "preflight_laya", lambda cfg: True)
    monkeypatch.setattr(cli, "warn_stale_mod_keys", lambda: None)

    assert cli.main(["run", "--mode", MODE_OBSERVE_HUMAN, "--no-log-file"]) == 0
    assert captured["mode"] == MODE_OBSERVE_HUMAN


def test_run_without_the_flag_keeps_the_config_mode(monkeypatch):
    captured = {}

    class RecordingRunner:
        def __init__(self, config, **kwargs):
            captured["mode"] = config.mode

        def run(self):
            return 0

    monkeypatch.setattr(
        cli,
        "load_config",
        lambda path: from_dict({"mode": MODE_OBSERVE_HUMAN, "panel": {"enabled": False}}),
    )
    monkeypatch.setattr(cli, "AgentRunner", RecordingRunner)
    monkeypatch.setattr(cli, "ModBridge", FakeBridge)
    monkeypatch.setattr(cli, "LayaClient", FakeLaya)
    monkeypatch.setattr(cli, "RunRecorder", lambda *a, **kw: None)
    monkeypatch.setattr(cli, "preflight_laya", lambda cfg: True)
    monkeypatch.setattr(cli, "warn_stale_mod_keys", lambda: None)

    assert cli.main(["run", "--no-log-file"]) == 0
    assert captured["mode"] == MODE_OBSERVE_HUMAN

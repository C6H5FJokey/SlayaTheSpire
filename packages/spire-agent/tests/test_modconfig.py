"""模组 SpireConfig 读写 + 遗留键告警。

协议 v2 起模组**没有自己的模式**：`mode` / `watchdog_sec` 由 agent 在握手时推送，
文件里只剩 `host` / `port`。这里同时守住"别再让人去改那个文件"这条线 ——
写遗留键要被拒绝并指路，文件里有遗留键要被点出来。
"""

import pytest

from spire_agent import cli
from spire_agent import modconfig
def test_read_properties_parses_comments_and_separators(tmp_path):
    path = tmp_path / "x.properties"
    path.write_text(
        "# comment\n! bang\n\nhost=127.0.0.1\nport: 17777\ntrailing =  a b  \nempty=\n",
        encoding="utf-8",
    )
    assert modconfig.read_properties(path) == {
        "host": "127.0.0.1",
        "port": "17777",
        "trailing": "a b",
        "empty": "",
    }


def test_read_properties_of_a_missing_file_is_empty(tmp_path):
    assert modconfig.read_properties(tmp_path / "nope.properties") == {}


def test_set_property_creates_parent_updates_in_place_and_keeps_the_rest(tmp_path):
    path = tmp_path / "sub" / "x.properties"
    modconfig.set_property("port", "17777", path)
    modconfig.set_property("host", "1.2.3.4", path)
    modconfig.set_property("port", "17778", path)
    assert path.read_text(encoding="utf-8") == "port=17778\nhost=1.2.3.4\n"


def test_set_property_preserves_comments(tmp_path):
    path = tmp_path / "x.properties"
    path.write_text("# keep me\nhost=1.2.3.4\n", encoding="utf-8")
    modconfig.set_property("port", "17777", path)
    assert path.read_text(encoding="utf-8") == "# keep me\nhost=1.2.3.4\nport=17777\n"


def test_stale_keys_points_out_the_v1_leftovers(tmp_path, monkeypatch):
    path = tmp_path / "x.properties"
    path.write_text("host=127.0.0.1\nobserve_human=true\nwatchdog_sec=30\n", encoding="utf-8")
    monkeypatch.setattr(modconfig, "properties_path", lambda *a, **kw: path)
    assert modconfig.stale_keys() == ["observe_human", "watchdog_sec"]


def test_stale_keys_is_quiet_for_a_clean_file(tmp_path, monkeypatch):
    path = tmp_path / "x.properties"
    path.write_text("host=127.0.0.1\nport=17777\n", encoding="utf-8")
    monkeypatch.setattr(modconfig, "properties_path", lambda *a, **kw: path)
    assert modconfig.stale_keys() == []


def test_cli_mod_config_sets_the_value_and_prints_where(tmp_path, capsys):
    path = tmp_path / "x.properties"
    assert cli.main(["mod-config", "--path", str(path), "--set", "host=1.2.3.4"]) == 0
    out = capsys.readouterr().out
    assert str(path) in out
    assert "host = 1.2.3.4" in out
    assert "重启" in out
    assert modconfig.read_properties(path) == {"host": "1.2.3.4"}


def test_cli_mod_config_refuses_the_deprecated_mode_keys(tmp_path, capsys):
    """这是本次设计的重点：不该再有"模组侧模式开关"这种让人手工同步的东西。"""
    path = tmp_path / "x.properties"
    assert cli.main(["mod-config", "--path", str(path), "--set", "observe_human=true"]) == 2
    err = capsys.readouterr().err
    assert "只读 host/port" in err
    assert "已废弃" in err
    assert "--mode" in err
    assert not path.exists()


def test_cli_mod_config_rejects_a_bad_assignment(tmp_path, capsys):
    path = tmp_path / "x.properties"
    assert cli.main(["mod-config", "--path", str(path), "--set", "oops"]) == 2
    assert "KEY=VALUE" in capsys.readouterr().err


def test_cli_warns_about_leftover_v1_keys(tmp_path, capsys, monkeypatch):
    path = tmp_path / "x.properties"
    path.write_text("observe_human=true\n", encoding="utf-8")
    monkeypatch.setattr(modconfig, "properties_path", lambda *a, **kw: path)
    cli.warn_stale_mod_keys()
    err = capsys.readouterr().err
    assert "observe_human" in err
    assert "已废弃" in err
    assert "不会看这个文件" in err


def test_cli_is_quiet_when_the_file_has_no_leftovers(tmp_path, capsys, monkeypatch):
    path = tmp_path / "x.properties"
    path.write_text("host=127.0.0.1\n", encoding="utf-8")
    monkeypatch.setattr(modconfig, "properties_path", lambda *a, **kw: path)
    cli.warn_stale_mod_keys()
    assert capsys.readouterr().err == ""

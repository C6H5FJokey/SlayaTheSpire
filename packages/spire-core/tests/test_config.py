"""配置加载：嵌套映射、默认值、校验警告。"""

from __future__ import annotations

import pytest

from spire_core import config


def test_nested_sections_become_dataclasses():
    cfg = config.from_dict(
        {
            "mode": "observe_human",
            "laya": {"base_url": "http://laya:8000", "model": "multilingual", "retries": 5},
            "mod": {"port": 12345, "heartbeat_sec": 1.5},
            "dataset": {"exclude_post_sl_rooms": False},
        }
    )
    assert isinstance(cfg.laya, config.LayaConfig)
    assert cfg.laya.base_url == "http://laya:8000"
    assert cfg.laya.model == "multilingual"
    assert cfg.laya.retries == 5
    assert isinstance(cfg.mod, config.ModConfig)
    assert cfg.mod.port == 12345
    assert cfg.mod.heartbeat_sec == 1.5
    assert cfg.dataset.exclude_post_sl_rooms is False
    # 没写的字段保留默认值
    assert cfg.mod.host == "127.0.0.1"
    assert cfg.panel.port == 8788


def test_unknown_keys_are_ignored():
    cfg = config.from_dict({"nope": 1, "laya": {"also_nope": True, "model": "english"}})
    assert cfg.laya.model == "english"


def test_defaults_match_documented_contract():
    cfg = config.from_dict({})
    assert cfg.mode == config.MODE_AGENT
    assert cfg.fairness_mode == "strict"
    assert cfg.allow_save_scum is False          # 默认禁止 SL
    assert cfg.dataset.exclude_post_sl_rooms is True
    assert cfg.dataset.exclude_fallback_rows is True
    assert cfg.character == "IRONCLAD"
    assert cfg.ascension == 0


def test_bad_enum_values_raise():
    with pytest.raises(ValueError):
        config.from_dict({"mode": "sandbox"})
    with pytest.raises(ValueError):
        config.from_dict({"fairness_mode": "cheat"})


def test_validate_reports_save_scum_and_omniscient():
    warnings = config.validate(config.from_dict({"allow_save_scum": True}))
    assert any("save_scum" in w for w in warnings)
    warnings = config.validate(config.from_dict({"fairness_mode": "omniscient"}))
    assert any("omniscient" in w for w in warnings)
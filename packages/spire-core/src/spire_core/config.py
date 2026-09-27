"""配置模型与加载。

core 只吃 dict（不碰文件），落盘读取由 agent 负责 —— 保持 core 可搬远端。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from typing import get_type_hints
import dataclasses

MODE_AGENT = "agent"
MODE_OBSERVE_HUMAN = "observe_human"

# 兜底：本地调试用的默认端点（docs/10-deployment.md 建议用远端真机）
DEFAULT_LAYA_BASE_URL = "http://127.0.0.1:8000"


@dataclass
class ModConfig:
    host: str = "127.0.0.1"
    port: int = 17777
    connect_timeout_sec: float = 10.0
    heartbeat_sec: float = 5.0
    reconnect_min_sec: float = 1.0
    reconnect_max_sec: float = 30.0
    max_frame_bytes: int = 4 * 1024 * 1024


@dataclass
class LayaConfig:
    base_url: str = DEFAULT_LAYA_BASE_URL
    api_key: str = ""
    model: str = "english"          # english | multilingual | typed-decisions | "" (Router 自选)
    timeout_sec: float = 5.0
    retries: int = 3
    backoff_base_sec: float = 0.5
    cache: bool = True
    cache_size: int = 512
    prefer_multilingual_over_chars: int = 6000
    endpoint: str = "/v1/systemone"
    # 拿不到模型决策时怎么办：stop = 立刻报错停跑（默认，不替模型猜）；fallback = 规则兜底打完这局
    on_error: str = "stop"
    # run 启动时先探一次 Laya：连不上就别开跑，别白烧一局
    preflight: bool = True


@dataclass
class PanelConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8788
    timeline_size: int = 30
    # 面板保留多少次"发给 Laya 的请求 + 拿回的原始返回"。请求体上限 2MB，
    # 所以这是个内存旋钮：20 次足够回看最近的决策，又不至于把 agent 撑爆。
    exchange_size: int = 20


@dataclass
class RecordConfig:
    runs_dir: str = "runs"
    save_raw_states: bool = True


@dataclass
class DatasetConfig:
    dir: str = "dataset"
    exclude_post_sl_rooms: bool = True
    exclude_fallback_rows: bool = True
    exclude_unmatched: bool = True
    val_ratio: float = 0.1
    test_ratio: float = 0.1
    split_seed: int = 20260927


@dataclass
class ObserveHumanConfig:
    also_query_model: bool = True


@dataclass
class AgentConfig:
    mode: str = MODE_AGENT
    character: str = "IRONCLAD"
    ascension: int = 0
    allow_save_scum: bool = False
    watchdog_sec: int = 30
    decision_delay_sec: float = 0.0   # 每个动作前的固定延迟（秒）；0 = 全速。演示用
    fairness_mode: str = "strict"
    seed: int = -1
    auto_restart: bool = True
    max_rooms: int = 0              # 0 = 不限
    log_level: str = "INFO"
    log_file: str = "spire_agent.log"
    templates_path: str = ""       # 空 = 用 core 内置 decision_points.toml

    mod: ModConfig = field(default_factory=ModConfig)
    laya: LayaConfig = field(default_factory=LayaConfig)
    panel: PanelConfig = field(default_factory=PanelConfig)
    record: RecordConfig = field(default_factory=RecordConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    observe_human: ObserveHumanConfig = field(default_factory=ObserveHumanConfig)


def _build(cls, data: dict[str, Any] | None):
    """把 dict 映射到 dataclass，**递归嵌套**，忽略未知键（前向兼容）。

    注意：本模块用了 `from __future__ import annotations`，所以 `f.type` 是字符串
    而不是类型对象。必须用 `get_type_hints` 解析回真正的类型，否则嵌套配置
    （如 `[laya]`）会被当成 dict 原样塞进去，后面 `cfg.laya.base_url` 就会炸。
    """
    data = data or {}
    hints = get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in cls.__dataclass_fields__.values():  # type: ignore[attr-defined]
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name)
        if isinstance(value, dict) and isinstance(ftype, type) and dataclasses.is_dataclass(ftype):
            kwargs[f.name] = _build(ftype, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def from_dict(data: dict[str, Any] | None) -> AgentConfig:
    data = data or {}
    cfg = _build(AgentConfig, data)
    if cfg.mode not in (MODE_AGENT, MODE_OBSERVE_HUMAN):
        raise ValueError(f"unknown mode: {cfg.mode!r}")
    if cfg.fairness_mode not in ("strict", "omniscient"):
        raise ValueError(f"unknown fairness_mode: {cfg.fairness_mode!r}")
    return cfg


def validate(cfg: AgentConfig) -> list[str]:
    """返回警告列表（不抛异常：配置问题应当可见而非致命）。"""
    warnings: list[str] = []
    if cfg.allow_save_scum:
        warnings.append(
            "allow_save_scum=true 但协议层没有 SL 动作；该开关只影响分析口径"
        )
    if cfg.mode == MODE_AGENT and not cfg.allow_save_scum:
        pass
    if cfg.fairness_mode == "omniscient":
        warnings.append("fairness_mode=omniscient：产生的数据默认不进训练集")
    if not cfg.laya.api_key:
        warnings.append("laya.api_key 为空：远端若设置了 LAYA_API_KEY 会返回 401")
    if cfg.laya.model not in ("", "english", "multilingual", "typed-decisions"):
        warnings.append(
            f"laya.model={cfg.laya.model!r} 不是已知 checkpoint，将由 Router 自选"
        )
    if cfg.laya.on_error not in ("stop", "fallback"):
        warnings.append(
            f"laya.on_error={cfg.laya.on_error!r} 不认识，按 stop 处理（可选 stop | fallback）"
        )
    if cfg.mode == MODE_AGENT and cfg.laya.on_error == "fallback":
        warnings.append(
            "laya.on_error=fallback：模型不可用时用规则兜底，产出的行 label_source=fallback，"
            "默认不进训练集"
        )
    if cfg.decision_delay_sec < 0:
        warnings.append(f"decision_delay_sec={cfg.decision_delay_sec} 为负，按 0（全速）处理")
    if cfg.mode == MODE_AGENT and cfg.decision_delay_sec >= cfg.watchdog_sec:
        warnings.append(
            f"decision_delay_sec={cfg.decision_delay_sec} >= watchdog_sec={cfg.watchdog_sec}："
            "慢到模组看门狗会先替你出手（战斗内结束回合）"
        )
    return warnings


__all__ = [
    "AgentConfig",
    "DatasetConfig",
    "LayaConfig",
    "MODE_AGENT",
    "MODE_OBSERVE_HUMAN",
    "ModConfig",
    "ObserveHumanConfig",
    "PanelConfig",
    "RecordConfig",
    "from_dict",
    "validate",
]

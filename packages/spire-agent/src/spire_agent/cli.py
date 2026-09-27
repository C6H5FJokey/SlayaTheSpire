"""命令行入口。

    python -m spire_agent run      --config packages/spire-agent/config.example.toml
    python -m spire_agent doctor   --config ...
    python -m spire_agent export   --runs runs --out dataset
    python -m spire_agent replay   --run runs/run-0001

设计上把"配置 -> 对象"这件事全放在这里，`runner.py` / `panel.py` / `laya_client.py`
都不碰文件系统或环境变量，所以它们可以直接被单测构造。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any

from spire_core import config as core_config
from spire_core.config import DatasetConfig
from spire_core.questions import load_templates as load_question_templates

from . import __version__
from .bridge import IncompatibleMod, ModBridge
from .console import enable_ctrl_c, enable_utf8_output
from .laya_client import LayaClient
from .modconfig import (
    DEPRECATED_KEYS,
    SUPPORTED_KEYS,
    properties_path,
    read_properties,
    set_property,
    stale_keys,
)
from .panel import PanelServer
from .probe import check_probe_answer, probe_payload
from .recorder import RunRecorder, load_run_rows
from .runner import AgentRunner

log = logging.getLogger("spire_agent")


def setup_logging(level: str, log_file: str | None = None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        handlers=handlers,
    )


def load_config(path: str | None) -> core_config.AgentConfig:
    if not path:
        return core_config.from_dict({})
    text = Path(path).read_text(encoding="utf-8")
    data = tomllib.loads(text)
    cfg = core_config.from_dict(data)
    # 环境变量覆盖密钥：配置文件里不该出现 API Key
    env_key = os.environ.get("LAYA_API_KEY")
    if env_key:
        cfg.laya.api_key = env_key
    env_url = os.environ.get("LAYA_BASE_URL")
    if env_url:
        cfg.laya.base_url = env_url
    return cfg


def print_mode_banner(cfg: core_config.AgentConfig) -> None:
    """开局前把"谁在玩"打在脸上：模式搞错了整局数据都是废的。"""
    if cfg.mode == core_config.MODE_OBSERVE_HUMAN:
        who = "observe_human —— 人类玩，agent 只观察记录（不控制游戏）"
        laya = (
            f"每步同时问模型做对照：{cfg.laya.base_url}"
            if cfg.observe_human.also_query_model
            else "不问模型（没有 model_answer / agreement 对照列）"
        )
    else:
        who = "agent —— 智能体控制，人类不参与"
        laya = f"模型：{cfg.laya.base_url}{cfg.laya.endpoint}（on_error={cfg.laya.on_error}）"
    print(f"运行模式：{who}")
    print(f"  模组侧无需任何设置：mode/watchdog_sec 由 agent 在握手时推送（{cfg.mod.host}:{cfg.mod.port} 除外）")
    print(f"  {laya}")


def preflight_laya(cfg: core_config.AgentConfig) -> bool:
    """启动前探一次 Laya。连不上就别开跑 —— 否则整局都是没有模型参与的废数据。"""
    url = cfg.laya.base_url.rstrip("/") + cfg.laya.endpoint
    print(f"预检 Laya {url} ...", end=" ")
    client = LayaClient(replace(cfg.laya, retries=1, backoff_base_sec=0.0))
    ok, detail = check_probe_answer(client.ask_raw(probe_payload()))
    if ok:
        print(f"OK（answer={detail}）")
        return True
    print(f"失败：{detail}")
    print("ERROR Laya 预检没过，已停止：没有开跑，也没有碰游戏。", file=sys.stderr)
    print("  先修好 Laya（或起 tools/fake_laya_server.py）再重跑。确实不想等模型：", file=sys.stderr)
    if cfg.mode == core_config.MODE_OBSERVE_HUMAN:
        print(
            "    observe_human 采集：--skip-preflight 即可照常采集人类标签，"
            "只是没有 model_answer / agreement 对照列",
            file=sys.stderr,
        )
    else:
        print(
            "    agent 模式没有模型就没法决策：--skip-preflight 只会让它跑一步就报错停跑（不去猜）",
            file=sys.stderr,
        )
    return False


def cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.mode:
        cfg.mode = args.mode
    setup_logging(cfg.log_level, cfg.log_file if not args.no_log_file else None)
    for warning in core_config.validate(cfg):
        log.warning("config: %s", warning)
    warn_stale_mod_keys()
    print_mode_banner(cfg)
    log.info("spire-agent %s starting in %s mode", __version__, cfg.mode)

    wants_laya = cfg.mode != core_config.MODE_OBSERVE_HUMAN or cfg.observe_human.also_query_model
    if wants_laya and cfg.laya.preflight and not args.skip_preflight:
        if not preflight_laya(cfg):
            return 3

    panel_sink = None
    panel: PanelServer | None = None
    if cfg.panel.enabled:
        panel = PanelServer(
            cfg.panel.host, cfg.panel.port, cfg.panel.timeline_size, cfg.panel.exchange_size
        )
        url = panel.start()
        print(f"观战面板：{url}")
        panel_sink = panel

    templates = (
        load_question_templates(Path(cfg.templates_path)) if cfg.templates_path else None
    )

    laya = None
    if cfg.mode == core_config.MODE_OBSERVE_HUMAN and not cfg.observe_human.also_query_model:
        laya = None
    else:
        laya = LayaClient(cfg.laya)

    runner = AgentRunner(
        cfg,
        bridge=ModBridge(cfg.mod, mode=cfg.mode, watchdog_sec=cfg.watchdog_sec),
        laya=laya,
        recorder=RunRecorder(cfg.record.runs_dir, args.run_id, save_raw_states=cfg.record.save_raw_states),
        panel=panel_sink,
        templates=templates,
    )
    try:
        return runner.run()
    finally:
        if panel is not None:
            panel.stop()


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if args.mode:
        cfg.mode = args.mode
    setup_logging(cfg.log_level)
    ok = True
    for warning in core_config.validate(cfg):
        print(f"WARN  {warning}")
    print(f"mode={cfg.mode} character={cfg.character} ascension={cfg.ascension}")
    print(f"fairness_mode={cfg.fairness_mode} allow_save_scum={cfg.allow_save_scum}")
    warn_stale_mod_keys()

    bridge = ModBridge(cfg.mod, mode=cfg.mode, watchdog_sec=cfg.watchdog_sec)
    print(f"检查模组端口 {cfg.mod.host}:{cfg.mod.port} ...", end=" ")
    try:
        connected = bridge.connect(retry=False)
        error = ""
    except IncompatibleMod as exc:
        connected, error = False, str(exc)
    if connected:
        print("OK")
        print(f"  hello={json.dumps(bridge.hello, ensure_ascii=False)}")
        print(f"  configure={json.dumps(bridge.configured, ensure_ascii=False)}")
        bridge.close()
    elif error:
        ok = False
        print("失败")
        print(f"  ERROR {error}")
    else:
        ok = False
        print("失败 —— 游戏是否已用 ModTheSpire 启动并勾选本模组？")

    print(f"检查 Laya {cfg.laya.base_url}{cfg.laya.endpoint} ...", end=" ")
    client = LayaClient(cfg.laya)
    result = client.ask_raw(probe_payload())
    answered, detail = check_probe_answer(result)
    if not answered:
        ok = False
        print(f"失败：{detail}")
    else:
        print("OK")
        routing = (result or {}).get("routing") or {}
        print(f"  model={result.get('model')} checkpoint={routing.get('model')} answer={detail}")
    print("doctor:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_export(args: argparse.Namespace) -> int:
    setup_logging("INFO")
    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "dataset"))
    import export as export_mod  # type: ignore[import-not-found]

    return export_mod.export_main(
        runs_dir=Path(args.runs),
        out_dir=Path(args.out),
        verify=args.verify,
        expand_noul=args.expand_noul,
        cfg=DatasetConfig(
            exclude_post_sl_rooms=not args.keep_post_sl,
            exclude_fallback_rows=not args.keep_fallback,
            exclude_unmatched=not args.keep_unmatched,
        ),
    )


def cmd_replay(args: argparse.Namespace) -> int:
    """用 core 的 replay 把某局已提交行重放一遍，验证构题可复现。"""
    setup_logging("INFO")
    from spire_core import replay

    run_dir = Path(args.run)
    rows = load_run_rows(run_dir.parent) if run_dir.name.startswith("run-") else []
    rows = [r for r in rows if r.get("run_id") == run_dir.name]
    report = replay.replay_all(rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


def warn_stale_mod_keys() -> None:
    """文件里留着协议 v1 的遗留键时点一句 —— 模组已经不读它们了，别再照着改。"""
    stale = stale_keys()
    if not stale:
        return
    print(
        f"提醒：{properties_path()} 里的 {'/'.join(stale)} 已废弃，模组不再读它。"
        "模式由 agent 决定（config 的 [agent] mode），下次切模式不会看这个文件。",
        file=sys.stderr,
    )


def cmd_mod_config(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else properties_path()
    for item in args.set:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            print(f"bad assignment: {item!r} (want KEY=VALUE)", file=sys.stderr)
            return 2
        key = key.strip()
        if key not in SUPPORTED_KEYS:
            print(
                f"拒绝写 {key}：模组只读 {'/'.join(SUPPORTED_KEYS)}。",
                file=sys.stderr,
            )
            if key.lower() in DEPRECATED_KEYS:
                print(
                    "  这个键在协议 v2 已废弃：mode 与 watchdog_sec 由 agent 在握手时推送。",
                    file=sys.stderr,
                )
                print(
                    "  切模式：改配置文件的 [agent] mode，或跑 "
                    "python -m spire_agent run --mode observe_human —— 不用重启游戏。",
                    file=sys.stderr,
                )
            return 2
        set_property(key, value.strip(), path)
    props = read_properties(path)
    print(f"path: {path}")
    for key in sorted(props):
        print(f"  {key} = {props[key]}")
    if not props:
        print("  (空：全部走默认值 127.0.0.1:17777，正常情况不用建这个文件)")
    warn_stale_mod_keys()
    if args.set:
        print("注意：模组只在启动时读一次，改完必须重启 Slay the Spire。")
    return 0


def main(argv: list[str] | None = None) -> int:
    # 父进程可能带着"忽略 Ctrl-C"标记（见 console.enable_ctrl_c），先清掉再干活
    enable_ctrl_c()
    enable_utf8_output()
    parser = argparse.ArgumentParser(prog="spire-agent", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="连接模组并按配置跑一局")
    run.add_argument("--config", default="packages/spire-agent/config.example.toml")
    run.add_argument("--run-id", default=None, help="覆盖 run id（默认按 runs/ 自增）")
    run.add_argument("--no-log-file", action="store_true")
    run.add_argument("--skip-preflight", action="store_true", help="跳过启动时的 Laya 预检")
    run.add_argument(
        "--mode",
        choices=[core_config.MODE_AGENT, core_config.MODE_OBSERVE_HUMAN],
        default=None,
        help="覆盖配置里的 [agent] mode：agent=智能体控制（默认）；"
        "observe_human=人类玩、agent 只观察记录",
    )
    run.set_defaults(func=cmd_run)

    doctor = sub.add_parser("doctor", help="检查配置 / 模组端口 / Laya 端点")
    doctor.add_argument("--config", default="packages/spire-agent/config.example.toml")
    doctor.add_argument(
        "--mode",
        choices=[core_config.MODE_AGENT, core_config.MODE_OBSERVE_HUMAN],
        default=None,
        help="按这个模式去配置模组（验证 configure 回执用；不影响配置文件）",
    )
    doctor.set_defaults(func=cmd_doctor)

    export = sub.add_parser("export", help="把 runs/ 导出成 train/val/test.jsonl")
    export.add_argument("--runs", default="runs")
    export.add_argument("--out", default="dataset")
    export.add_argument("--verify", action="store_true", help="只校验不重写（确定性检查）")
    export.add_argument("--expand-noul", action="store_true", help="额外产出 *.noul.jsonl")
    export.add_argument("--keep-post-sl", action="store_true", help="不排除 post_sl 房间的行")
    export.add_argument("--keep-fallback", action="store_true", help="不排除 fallback 行")
    export.add_argument("--keep-unmatched", action="store_true", help="不排除未命中候选的行")
    export.set_defaults(func=cmd_export)

    replay = sub.add_parser("replay", help="重放一局，验证构题可复现")
    replay.add_argument("--run", required=True, help="例如 runs/run-0001")
    replay.set_defaults(func=cmd_replay)

    modcfg = sub.add_parser(
        "mod-config", help="查看/修改模组的 SpireConfig（只有 host/port 会被模组读取）"
    )
    modcfg.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help="写入键值，可重复；不写就只打印当前值")
    modcfg.add_argument("--path", default=None, help="覆盖 properties 路径（默认按 MTS 的 CONFIG_DIR 推导）")
    modcfg.set_defaults(func=cmd_mod_config)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

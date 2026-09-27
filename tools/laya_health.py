#!/usr/bin/env python
"""远程 Laya 服务的健康检查 / 冒烟（见 docs/10-deployment.md）。

做三件事：

1. `--ping`        ：`GET /health`（或 `--path`）确认服务活着；
2. `--probe`       ：发一个**最小** `systemone` 请求，确认契约通（鉴权、路由、
                     返回结构都对得上），并打印延迟与 usage；
3. `--selftest`    ：用本地 mock 起一个假 Laya，验证 `LayaClient` 的重试/缓存/兜底
                     逻辑在没有远端的情况下也是对的。

用法：
    python tools/laya_health.py --base-url https://laya.example.com --api-key $env:LAYA_API_KEY --probe
    python tools/laya_health.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-core" / "src"))
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-agent" / "src"))


def _probe_payload() -> dict:
    """与 agent 的 preflight / doctor 共用同一份探针（`spire_agent.probe`）。"""
    from spire_agent.probe import probe_payload

    return probe_payload()


def cmd_ping(args: argparse.Namespace) -> int:
    import httpx

    url = args.base_url.rstrip("/") + args.path
    headers = {"Authorization": f"Bearer {args.api_key}"} if args.api_key else {}
    try:
        resp = httpx.get(url, headers=headers, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001 - 这是给用户看的诊断
        print(f"FAIL {url} -> {exc}")
        return 1
    mark = "ok" if resp.status_code == 200 else "FAIL"
    print(f"[{mark}]   {args.path:<22} {resp.status_code}")
    print("       " + resp.text[:300])
    return 0 if resp.status_code < 500 else 1


def cmd_probe(args: argparse.Namespace) -> int:
    from spire_agent.laya_client import LayaClient, LayaConfig

    config = LayaConfig(
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
        timeout_sec=args.timeout,
        retries=args.retries,
    )
    client = LayaClient(config)
    result = client.ask_raw(_probe_payload())
    if result is None:
        print("FAIL: no answer (see log above)")
        return 1

    # 这一层要挡住"契约对不上"：与 preflight / doctor 共用同一个校验器（答案在
    # `answers[q]["choice"]`，checkpoint 在 `routing["model"]`），形态一变就在这里红。
    from spire_agent.probe import PROBE_QUESTION_ID, check_probe_answer

    answered, detail = check_probe_answer(result)
    if not answered:
        print(f"FAIL: {detail}")
        print(json.dumps(result, ensure_ascii=False, indent=2)[:1500])
        return 1
    answer = (result.get("answers") or {}).get(PROBE_QUESTION_ID) or {}
    choice = answer.get("choice")

    routing = result.get("routing") or {}
    print(
        f"[ok]   /v1/systemone          200  {result.get('latency_ms')}ms  "
        f"model={result.get('model')} checkpoint={routing.get('model')} answer={choice}"
    )
    print(f"       probabilities={json.dumps(answer.get('probabilities'), ensure_ascii=False)}")
    print(f"       confidence={answer.get('answer_confidence')} "
          f"usage={json.dumps(result.get('usage'), ensure_ascii=False)}")
    print(f"       stats={client.stats.as_dict()}")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from spire_agent.laya_client import LayaClient, LayaConfig
    from spire_agent.fake_laya import FakeLaya  # 与 agent 单测共用同一个假服务
    from spire_agent.probe import PROBE_OPTIONS, PROBE_QUESTION_ID

    server = FakeLaya()
    server.start()
    try:
        client = LayaClient(
            LayaConfig(
                base_url=server.base_url,
                api_key="selftest",
                model="english",
                timeout_sec=2.0,
                retries=2,
            )
        )
        payload = _probe_payload()
        first = client.ask_raw(payload)
        second = client.ask_raw(payload)
        # 第二次必须是缓存命中：answers 相同、cache_hit=True，且没有多打一次网络
        ok = (
            first is not None
            and second is not None
            and first["answers"] == second["answers"]
            and (first["answers"].get(PROBE_QUESTION_ID) or {}).get("choice") in PROBE_OPTIONS
            and second.get("cache_hit") is True
            and client.stats.calls == 1
            and client.stats.cache_hits == 1
        )
        answer = (first or {}).get("answers", {}).get(PROBE_QUESTION_ID) or {}
        print(f"answer={json.dumps(answer, ensure_ascii=False)}")
        print(f"cached_hit={(second or {}).get('cache_hit')}")
        print(f"stats={client.stats.as_dict()}")
        print("SELFTEST " + ("PASS" if ok else "FAIL"))
        return 0 if ok else 1
    finally:
        server.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=os.environ.get("LAYA_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("LAYA_API_KEY", ""))
    parser.add_argument("--model", default=os.environ.get("LAYA_MODEL", "english"))
    parser.add_argument("--path", default="/health")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--retries", type=int, default=3)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--ping", action="store_true")
    group.add_argument("--probe", action="store_true")
    group.add_argument("--selftest", action="store_true")
    args = parser.parse_args(argv)

    if args.selftest:
        return cmd_selftest(args)
    if not args.base_url:
        parser.error("--base-url (or LAYA_BASE_URL) is required")
    return cmd_ping(args) if args.ping else cmd_probe(args)


if __name__ == "__main__":
    raise SystemExit(main())

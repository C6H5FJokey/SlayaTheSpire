#!/usr/bin/env python
r"""用**微调后的** checkpoint 起一个 Laya 服务（见 docs/13-finetune.md）。

    # 先自检：加载 checkpoint，发一道最小题，确认能答（不启服务）
    .\.venv-laya\Scripts\python.exe finetune\serve.py --model finetune\checkpoints\laya-spire-v1 --check

    # 起服务（只监听 loopback，和 tools\serve_laya.ps1 一样）
    .\.venv-laya\Scripts\python.exe finetune\serve.py --model finetune\checkpoints\laya-spire-v1 --api-key <key>

为什么不用 `laya-serve`：`laya.serve` 的 Router 只认内置 checkpoint 名
（`english` / `multilingual` / `typed-decisions`），**喂不进一个本地目录**。这里直接
`laya.load(<目录>)` 起同一套 `/v1/systemone` 契约（HTTP 外壳、错误码、预算守卫都对齐
`laya.serve`），agent 侧只改 `[laya] base_url` 与 `model` 两个字段，客户端一行不用动。

两条硬规则：

1. **只服务本地目录**。`--model` 必须是含 `rl_agent_config.json` 的**已存在目录**；
   路径不会被当成 HF repo id，也就不会在服务启动时偷偷联网下载。要服务官方 checkpoint
   请用 `laya-serve`。
2. **训练与部署共用同一个 venv**（`.venv-laya`）：这里的预算守卫、温度夹取、序列构造直接
   复用同一个 `laya 0.3.20` 的实现 —— 版本一漂移，训练时看到的概率就不再是线上给的概率。

环境变量（尽量与 `laya-serve` 同名，运维不用记两套）：

======================  ================================================  =============
env var                 meaning                                           default
======================  ================================================  =============
``LAYA_MODEL``          `--model` 的默认值（微调 checkpoint 目录）          finetune/checkpoints/laya-spire
``LAYA_HOST``           `--host`                                          127.0.0.1
``LAYA_PORT``           `--port`                                          8000
``LAYA_DEVICE``         `--device`（auto = 有 CUDA 就用 CUDA）             auto
``LAYA_API_KEY``        设置后要求 ``Authorization: Bearer <it>``           无
``LAYA_THREADS``        CPU 推理的 torch 线程数（<= 物理核数）              torch 默认
``LAYA_PRELOAD``        1 = 启动时载入并热一次前向                          1
``LAYA_LOG_LEVEL``      uvicorn 日志级别                                   info
``LAYA_MAX_LEN``        覆盖序列总长                                        checkpoint 自带
``LAYA_HEAD_MAX_LEN``   覆盖题面 + 选项的 token 预算                        checkpoint 自带
======================  ================================================  =============
"""

# 这里**不能**用 `from __future__ import annotations`：PEP 563 会把注解变成字符串，而
# FastAPI 需要真正的 `Request` 类才能把它识别成请求对象（`Request` 在 `create_app` 里局部
# import，字符串注解解析不到，于是被当成 query 参数，每个请求都 422）。见 test_serve.py。
import argparse
import asyncio
import hmac
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

DEFAULT_MODEL_DIR = "finetune/checkpoints/laya-spire"

# 与 `spire_agent.probe` 同形的最小探针题：choice 的 criteria 非空、instructions 非空。
# （客户端侧那套校验在 tools/laya_health.py --probe / spire_agent.Probe 里；这里只做进程内自检，
#   所以不强依赖 spire-agent 包 —— `.venv-laya` 里没有它。）
PROBE_QUESTION_ID = "q"
PROBE_QUESTIONS = {
    PROBE_QUESTION_ID: {
        "type": "choice",
        "instructions": "Answer a.",
        "criteria": {"a": "a", "b": "b"},
    }
}


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def resolve_model_dir(spec: str) -> str:
    """把 `--model` 解析成一个**本地** checkpoint 目录；不解析 HF repo id、不联网。"""
    path = os.path.abspath(os.path.expanduser(spec))
    if not os.path.isdir(path):
        raise SystemExit(
            f"{spec!r} 不是目录。finetune/serve.py 只服务本地 checkpoint"
            "（要服务官方 checkpoint 用 laya-serve）。"
        )
    if not os.path.isfile(os.path.join(path, "rl_agent_config.json")):
        raise SystemExit(
            f"{path} 里没有 rl_agent_config.json —— 不是 Laya checkpoint。\n"
            "训练产物应有 rl_agent_config.json / model.safetensors / encoder/ / tokenizer/。"
        )
    return path


def check_agent(agent, name: str, *, max_len: Optional[int], head_max_len: Optional[int],
                state: Any = "ping") -> tuple[bool, Dict[str, Any]]:
    """进程内自检：发一道最小的 choice 题，按契约校验应答形态（答案键 + routing）。"""
    overrides = {}
    if max_len is not None:
        overrides["max_len"] = max_len
    if head_max_len is not None:
        overrides["head_max_len"] = head_max_len
    t0 = time.perf_counter()
    result = dict(agent.system_one(state, PROBE_QUESTIONS, **overrides))
    latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    result.setdefault("routing", {"model": name, "reason": "fine-tuned"})
    answer = (result.get("answers") or {}).get(PROBE_QUESTION_ID) or {}
    choice = answer.get("choice")
    ok = isinstance(choice, str) and choice in PROBE_QUESTIONS[PROBE_QUESTION_ID]["criteria"]
    result["latency_ms"] = latency_ms
    return ok, result


def create_app(agent, *, name: str, api_key: Optional[str], max_len: Optional[int] = None,
               head_max_len: Optional[int] = None, fine_tuned: Optional[bool] = None) -> Any:
    """同一个 `/v1/systemone` 契约的 HTTP 外壳（单 worker + 一把锁串行跑推理，同 `laya.serve`）。

    `agent` 由调用方传进来（`main()` 里 `laya.load` 的结果），所以这里不做任何加载、
    也不 import torch —— 单测可以塞一个假 agent 进来验契约。
    """
    # 守卫与 laya.serve 逐字同源：预算上限、流式 body 截断都从那边复用，避免两套阈值漂移。
    from fastapi import FastAPI, Header, HTTPException, Request
    from laya.serve import _check_request_limits, _read_body_capped

    overrides: Dict[str, int] = {}
    if max_len is not None:
        overrides["max_len"] = max_len
    if head_max_len is not None:
        overrides["head_max_len"] = head_max_len

    # 串行推理：单 agent 本来就是串行的，排队比并行更省显存、也更可预测（见 docs/10-deployment.md）。
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="laya-infer")
    gate: Optional[asyncio.Lock] = None

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            pool.shutdown(wait=True, cancel_futures=True)

    app = FastAPI(title="laya-serve-finetuned", lifespan=lifespan)
    expected_auth = ("Bearer " + api_key).encode("utf-8", "surrogateescape") if api_key else b""

    def check_auth(authorization: Optional[str]) -> None:
        if api_key is None:
            return
        supplied = (authorization or "").encode("utf-8", "surrogateescape")
        if not hmac.compare_digest(supplied, expected_auth):
            raise HTTPException(status_code=401, detail="invalid or missing bearer token")

    @app.get("/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "loaded": True,
            "model": name,
            "checkpoint": getattr(agent, "model_id", None),
            "device": str(getattr(agent, "device", "unknown")),
            "fine_tuned": bool(fine_tuned if fine_tuned is not None
                               else getattr(agent, "cfg", {}).get("fine_tuned", False)),
        }

    @app.post("/v1/systemone")
    async def systemone(request: Request, authorization: Optional[str] = Header(default=None)):
        nonlocal gate
        check_auth(authorization)
        raw = await _read_body_capped(request)
        try:
            body = json.loads(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail="request body must be valid JSON")
        if not isinstance(body, dict) or "questions" not in body:
            raise HTTPException(status_code=400, detail="request body must be an object with a 'questions' field")
        state = body.get("state")
        questions = body["questions"]
        _check_request_limits(state, questions)
        if gate is None:
            gate = asyncio.Lock()
        try:
            async with gate:
                loop = asyncio.get_running_loop()
                result = await loop.run_in_executor(
                    pool, lambda: agent.system_one(state, questions, **overrides)
                )
        except HTTPException:
            raise
        except ValueError as exc:      # 题目本身不合法：错误信息对客户端有用
            raise HTTPException(status_code=422, detail=str(exc))
        except Exception:              # noqa: BLE001 -- 不外泄路径 / 权重 / OOM 文本
            raise HTTPException(status_code=500, detail="inference failed")
        # `routing.model` 是 checkpoint 名：agent 侧把它落进 `runs/<run_id>/meta.json`
        # （见 docs/07-laya-contract.md）。顶层 `model` 恒为架构名，没有信息量。
        payload = dict(result)
        payload["routing"] = {"model": name, "reason": "explicit",
                              "checkpoint": os.path.abspath(str(getattr(agent, "model_id", name)))}
        return payload

    return app


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="环境变量见文件头；命令行参数优先于对应的 LAYA_* 变量。",
    )
    parser.add_argument("--model", default=env("LAYA_MODEL", DEFAULT_MODEL_DIR),
                        help="微调 checkpoint **目录**（环境变量 LAYA_MODEL）")
    parser.add_argument("--host", default=env("LAYA_HOST", "127.0.0.1"),
                        help="绑定地址（环境变量 LAYA_HOST）；非环回地址必须配 --api-key")
    parser.add_argument("--port", type=int, default=int(env("LAYA_PORT", "8000") or 8000),
                        help="端口（环境变量 LAYA_PORT）")
    parser.add_argument("--device", default=env("LAYA_DEVICE", "auto"),
                        help="torch 设备：auto / cuda / cuda:1 / cpu（环境变量 LAYA_DEVICE）")
    parser.add_argument("--api-key", default=env("LAYA_API_KEY"),
                        help="设置后要求 Authorization: Bearer（环境变量 LAYA_API_KEY）")
    parser.add_argument("--threads", type=int, default=None,
                        help="CPU 推理的 torch 线程数（环境变量 LAYA_THREADS，<= 物理核数）")
    parser.add_argument("--max-len", type=int, default=int(env("LAYA_MAX_LEN", "0") or 0) or None,
                        help="覆盖序列总长（环境变量 LAYA_MAX_LEN）")
    parser.add_argument("--head-max-len", type=int, default=int(env("LAYA_HEAD_MAX_LEN", "0") or 0) or None,
                        help="覆盖题面 + 选项的 token 预算（环境变量 LAYA_HEAD_MAX_LEN）")
    parser.add_argument("--name", default=env("LAYA_MODEL_NAME"),
                        help="写进应答 routing.model 的 checkpoint 名（默认取 checkpoint 里的 model_name）")
    parser.add_argument("--log-level", default=env("LAYA_LOG_LEVEL", "info"),
                        help="uvicorn 日志级别（环境变量 LAYA_LOG_LEVEL）")
    parser.add_argument("--no-preload", action="store_true",
                        help="不预加载（等价 LAYA_PRELOAD=0）：第一次请求才载入并热前向")
    parser.add_argument("--check", action="store_true",
                        help="只加载 checkpoint 并发一道最小题自检，然后退出（不启服务）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # 先挡"把没鉴权的推理端口暴露到内网"这件事，再看 checkpoint 路径 —— 配置不安全就该立刻拒绝。
    loopback = {"127.0.0.1", "localhost", "::1", "[::1]"}
    if args.host not in loopback and not args.api_key:
        raise SystemExit(
            f"绑定 {args.host} 是**跨机可达**的地址，必须配 --api-key / LAYA_API_KEY —— "
            "这个端口后面是模型权重和 GPU。"
        )
    model_dir = resolve_model_dir(args.model)
    if args.threads:
        os.environ["LAYA_THREADS"] = str(args.threads)

    import laya
    from laya.serve import _apply_thread_limit

    applied = _apply_thread_limit()
    device = args.device
    if device == "auto":
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[serve] laya {laya.__version__} checkpoint={model_dir} device={device} 端口 {args.host}:{args.port}")
    if applied:
        print(f"[serve] torch 线程数 {applied}（LAYA_THREADS）")
    if not args.api_key:
        print("[serve] 没设 --api-key：只应在 loopback 上这样跑")

    # model_name / fine_tuned 从磁盘上的 config 读：`--no-preload` 时也拿得到（不必先加载模型）
    with open(os.path.join(model_dir, "rl_agent_config.json"), "r", encoding="utf-8") as handle:
        checkpoint_cfg = json.load(handle)
    name = args.name or checkpoint_cfg.get("model_name") or "laya-spire"
    if not checkpoint_cfg.get("fine_tuned"):
        print("[serve][warn] 这份 checkpoint 的 rl_agent_config.json 里没有 fine_tuned=true："
              "它是官方基座还是手工拷来的目录？微调产物会带这个标记。")

    t0 = time.perf_counter()
    preload = not args.no_preload and env_bool("LAYA_PRELOAD", True)
    agent = None
    if preload or args.check:
        agent = laya.load(model_dir, device=device)
        print(f"[serve] 载入耗时 {time.perf_counter() - t0:.1f}s")

    if args.check:
        assert agent is not None
        ok, result = check_agent(agent, name, max_len=args.max_len, head_max_len=args.head_max_len)
        answer = (result.get("answers") or {}).get(PROBE_QUESTION_ID) or {}
        print(f"[serve][check] {'ok' if ok else 'FAIL'}  {result['latency_ms']}ms  "
              f"checkpoint={result['routing']['model']} answer={answer.get('choice')} "
              f"confidence={answer.get('answer_confidence')} usage={json.dumps(result.get('usage'))}")
        if not ok:
            print(json.dumps(result, ensure_ascii=False, indent=2)[:1500])
        return 0 if ok else 1

    def build():
        nonlocal agent
        if agent is None:
            agent = laya.load(model_dir, device=device)
            print(f"[serve] 首次请求触发载入，耗时 {time.perf_counter() - t0:.1f}s")
        return agent

    class LazyAgent:
        """`--no-preload` 时把加载推迟到第一个请求（`laya-serve` 的 LAYA_PRELOAD=0 语义）。"""

        model_id = model_dir

        @property
        def device(self):
            return getattr(agent, "device", device)

        @property
        def cfg(self):
            return getattr(agent, "cfg", {}) if agent is not None else {}

        def system_one(self, state, questions, **kwargs):
            return build().system_one(state, questions, **kwargs)

    import uvicorn

    app = create_app(agent if agent is not None else LazyAgent(), name=name, api_key=args.api_key,
                     max_len=args.max_len, head_max_len=args.head_max_len,
                     fine_tuned=bool(checkpoint_cfg.get("fine_tuned")))
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

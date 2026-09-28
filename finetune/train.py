#!/usr/bin/env python
r"""按 Laya 官方 RLCD 配方微调 checkpoint（见 docs/13-finetune.md）。

    # 先看计划（不训练）：环境、基座、训练项、批大小、可训练参数量
    .\.venv-laya\Scripts\python.exe finetune\train.py --dry-run

    # 冒烟：16 项、2 步，几十秒，确认链路和存盘都对
    .\.venv-laya\Scripts\python.exe finetune\train.py --limit-items 16 --max-steps 2 --out finetune\checkpoints\smoke

    # 真训练（远端多卡：torchrun --standalone --nproc_per_node=N finetune/train.py ...）
    .\.venv-laya\Scripts\python.exe finetune\train.py --freeze-encoder --out finetune\checkpoints\laya-spire-v1

配方与官方 `docs/finetune.md`（以及公开的单卡实现）对齐：

1. **RLCD**：每题采样 G 个「加噪 logit」，用严格恰当评分规则（log + spherical + RPS）当奖励做
   GRPO 式策略梯度，再叠一份全权重 soft cross-entropy。噪音 σ 随 epoch 从 `--sigma-start` 退火到 `--sigma-end`。
2. **校准**：训练**前**先从训练集里留出一份校准片（默认 <=400 项 / 10%），训练后在它上面按
   `(题型, 候选数档)` 拟合温度并写进 `rl_agent_config.json`。旧 checkpoint 自带的
   `temperature_by_options` 一律清掉 —— 它在推理端优先级更高，会静默顶掉新拟合值。
   拟合值用 `laya.common.clamp_temperature` 夹到 `[0.5, 5]`：服务端也夹这一区间，训练侧写什么就生效什么。
3. **可部署**：产物就是一份普通 Laya checkpoint（`rl_agent_config.json` / `model.safetensors` /
   `encoder/` / `tokenizer/`），`laya.load(path)` 或 `finetune/serve.py` 直接可用。

数据量小时（本仓库当前只有一局 ~700 行）建议 `--freeze-encoder`：只训决策头，
别拿几百个样本去动 3.95 亿参数的编码器。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import random
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ft_data as fd  # noqa: E402

# `laya.common.QTYPES` 的镜像（train() 里会断言两者一致）。放在模块级只是为了让 collate()
# 能用它，同时避免在 import 阶段就拖进 torch/transformers。
QTYPES = {"choice": 0, "score": 1, "noul": 2}
QTYPE_NAMES = {v: k for k, v in QTYPES.items()}

IS_MAIN = True


def _atomic_replace(src: str, dst: str, *, budget: float = 60.0) -> bool:
    """`os.replace` + 指数退避重试。成功返回 True；等满 `budget` 秒仍失败返回 False（不抛）。

    为什么需要这个：续跑点有几 GB（模型 + AdamW 动量），而 Windows 上**刚写完就把这个文件
    改名**经常撞上搜索索引 / 杀毒实时扫描仍拿着句柄 —— `WinError 32`（共享冲突）或
    `WinError 5`（拒绝访问）。实测同一目录写 2 GB 立刻改名是 0 延迟成功，5 GB 会卡住好几秒：
    文件越大，扫描器握句柄的窗口越长。

    所以这里从 0.25 s 起指数退避、最长等 `budget` 秒。**返回 False 的处理权交给调用方**：
    续跑点写不进去不能把整个训练打挂 —— 它是崩溃恢复的辅助产物，最终产物是 checkpoint 目录。
    """
    delay, waited = 0.25, 0.0
    while True:
        try:
            os.replace(src, dst)
            return True
        except OSError as exc:
            if waited >= budget:
                log(f"改名失败（已等 {waited:.0f}s）：{src} -> {dst}：{exc}")
                return False
            time.sleep(delay)
            waited += delay
            delay = min(2.0, delay * 1.5)


def log(message: str) -> None:
    if IS_MAIN:
        print(f"[train] {message}", flush=True)


def _transformers_version() -> str:
    import transformers

    return transformers.__version__


# ----------------------------------------------------------------------------- 环境


def setup_distributed(args) -> dict | None:
    """torchrun 起的多卡就初始化进程组；单进程返回 None。"""
    global IS_MAIN
    import torch

    world = int(os.environ.get("WORLD_SIZE", "1") or 1)
    if world <= 1 or not torch.distributed.is_available():
        return None
    import torch.distributed as dist

    rank = int(os.environ["RANK"])
    local_rank = int(os.environ.get("LOCAL_RANK", "0"))
    backend = args.backend or ("nccl" if torch.cuda.is_available() else "gloo")
    dist.init_process_group(backend=backend)
    if backend == "nccl":
        torch.cuda.set_device(local_rank)
    IS_MAIN = rank == 0
    return {"rank": rank, "world": world, "local_rank": local_rank, "backend": backend}


def resolve_device(spec: str):
    import torch

    if spec == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(spec)


def cuda_dtype(cfg: dict, device):
    """amp dtype：bf16 需要算力 >= 8；T4 这类只支持 fp16（与 `laya.agent` 的判断一致）。"""
    import torch

    from laya.common import amp_dtype

    if device.type != "cuda":
        return torch.float32
    if torch.cuda.get_device_capability(device)[0] < 8:
        return torch.float16
    return amp_dtype(cfg.get("amp_dtype", "bf16"))


def load_checkpoint(base: str, device, *, max_len: int | None = None, head_max_len: int | None = None):
    """加载一份普通 Laya checkpoint -> `(model, tokenizer, cfg)`。

    训练（`train.py`）与评测（`eval.py`）共用这一条路径：`laya.load` 走的是同一套
    `laya.common.build_model` + `model.safetensors`，所以这里读出来的模型与线上服务看到的是同一个。

    `max_len` / `head_max_len` 覆盖 checkpoint 自带的默认值（`build.py` 按这两个值切序列，
    训练和评测必须与它一致）。
    """
    from safetensors.torch import load_file
    from transformers import AutoTokenizer

    from laya.common import build_model

    with open(os.path.join(base, "rl_agent_config.json"), "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    cfg = dict(cfg)
    if max_len is not None:
        cfg["max_len"] = max_len
    if head_max_len is not None:
        cfg["head_max_len"] = head_max_len
    tok = AutoTokenizer.from_pretrained(os.path.join(base, "tokenizer"))
    model = build_model(cfg, encoder_dir=os.path.join(base, "encoder"))
    model.load_state_dict(load_file(os.path.join(base, "model.safetensors")), strict=True)
    with contextlib.suppress(AttributeError):
        model.encoder.config.reference_compile = False
    model.to(device)
    model.eval()
    return model, tok, cfg


def collate(items: list[dict], pad_id: int):
    """组 batch。等同 `laya.common.collate_items`，外加 per-item 权重 `weight`。"""
    import torch

    n = len(items)
    length = max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, length), pad_id, dtype=torch.long)
    att = torch.zeros((n, length), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax), dtype=torch.float32)
    weight = torch.ones(n, dtype=torch.float32)
    for i, it in enumerate(items):
        ids[i, : len(it["ids"])] = torch.tensor(it["ids"], dtype=torch.long)
        att[i, : len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"], dtype=torch.long)
        mmask[i, :k] = True
        target[i, :k] = torch.tensor(it["target"], dtype=torch.float32)
        weight[i] = float(it.get("weight", 1.0))
    return {
        "input_ids": ids,
        "attention_mask": att,
        "marker_pos": mpos,
        "marker_mask": mmask,
        "target": target,
        "weight": weight,
        "qtype": torch.tensor([QTYPES[it["qtype"]] for it in items], dtype=torch.long),
    }


def rlcd_loss(logits, act, batch: dict, sigma: float, args) -> dict:
    """RLCD：加噪 logit 的策略梯度 + 全权重 soft CE（见文件头）。"""
    import torch

    from laya.common import proper_reward

    mask = batch["marker_mask"]
    target = batch["target"]
    weight = batch["weight"]
    qtype = batch["qtype"]
    logits = logits.float()
    k = mask.sum(-1, keepdim=True).float()
    noise = torch.randn((args.samples,) + logits.shape, device=logits.device) * sigma * mask
    noise = (noise - noise.sum(-1, keepdim=True) / k) * mask
    with torch.no_grad():  # 采样出来的分布只当奖励，不参与梯度
        z = logits.detach().unsqueeze(0) + noise
        q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
        reward = proper_reward(q, target.unsqueeze(0), qtype, mask, w_sph=args.w_sph, w_rps=args.w_rps)
        advantage = reward - reward.mean(0, keepdim=True)
        advantage = advantage / (advantage.std(0, keepdim=True) + 1e-6)
    logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (2 * sigma**2)
    per_item_rl = -(advantage * logp).mean(0)
    log_probs = torch.log_softmax(logits.masked_fill(~mask, -1e4), -1)
    per_item_ce = -(target * log_probs).sum(-1)
    return {
        "loss_rl": (weight * per_item_rl).mean(),
        "loss_ce": (weight * per_item_ce).mean(),
        "reward": reward.mean(),
    }


# ----------------------------------------------------------------------------- 校准


def calibration_slice(items: list[dict], *, frac: float, cap: int, seed: int):
    """按**记录**留出校准片：同一题的多个问法不会一半在训练里、一半在校准里。"""
    by_record: dict[str, list[dict]] = {}
    for item in items:
        by_record.setdefault(item["record_id"], []).append(item)
    order = sorted(by_record)
    random.Random(seed).shuffle(order)
    budget = min(cap, int(len(items) * frac)) if frac > 0 else 0
    picked: list[dict] = []
    chosen: set[str] = set()
    for record_id in order:
        if len(picked) >= budget:
            break
        picked.extend(by_record[record_id])
        chosen.add(record_id)
    return [it for it in items if it["record_id"] not in chosen], picked


def fit_temperatures(model, items: list[dict], pad_id: int, device, args) -> dict:
    """在留出片上按 `(题型, 候选数档)` 网格搜索温度（最小化 NLL）。

    返回 `temperature`（每题型一个）与 `temperature_by_options`（每档一个，样本不足退回题型值）。
    档位键名由 `laya.common.temp_bucket` 生成，保证与服务端 `temp_bucket` 完全一致。
    """
    import numpy as np
    import torch

    from laya.common import clamp_temperature, temp_bucket

    grid = np.exp(np.linspace(np.log(0.5), np.log(5.0), 64))
    raw: dict[str, list[tuple]] = {}
    model.eval()
    with torch.no_grad():
        for start in range(0, len(items), args.calib_batch):
            chunk = items[start : start + args.calib_batch]
            batch = collate(chunk, pad_id)
            logits, _ = model(
                batch["input_ids"].to(device),
                batch["attention_mask"].to(device),
                batch["marker_pos"].to(device),
                batch["marker_mask"].to(device),
                batch["qtype"].to(device),
            )
            logits = logits.float().cpu()
            for row, item in enumerate(chunk):
                k = len(item["markers"])
                raw.setdefault(temp_bucket(QTYPES[item["qtype"]], k), []).append(
                    (logits[row, :k].numpy(), int(item["label"]))
                )

    def nll(matrix, labels, t: float) -> float:
        z = matrix / t
        z = z - z.max(axis=1, keepdims=True)
        logp = z - np.log(np.exp(z).sum(axis=1, keepdims=True))
        return float(-logp[np.arange(len(labels)), labels].mean())

    def best(pairs: list[tuple], min_n: int):
        if len(pairs) < min_n:
            return None
        kmax = max(len(z) for z, _ in pairs)
        matrix = np.full((len(pairs), kmax), -1e4)
        for i, (z, _) in enumerate(pairs):
            matrix[i, : len(z)] = z
        labels = np.array([y for _, y in pairs])
        base = nll(matrix, labels, 1.0)
        scored = sorted((nll(matrix, labels, t), float(t)) for t in grid)
        picked_nll, picked = scored[0]
        if picked_nll >= base:  # 拟合不出比 T=1 更好的：那就别动
            picked, picked_nll = 1.0, base
        return clamp_temperature(picked), base, picked_nll

    def stack(pairs: list[tuple]) -> list[tuple]:
        return pairs

    per_type: dict[int, list[tuple]] = {}
    for key, pairs in raw.items():
        per_type.setdefault(QTYPES[key.split(":")[0]], []).extend(stack(pairs))

    temperatures = [1.0, 1.0, 1.0]
    report: dict[str, dict] = {}
    for qtype, pairs in sorted(per_type.items()):
        fitted = best(pairs, args.min_calib)
        if fitted is None:
            temperatures[qtype] = 1.0
            report[f"type:{QTYPE_NAMES[qtype]}"] = {"n": len(pairs), "temperature": 1.0,
                                                    "nll_t1": None, "nll_fitted": None, "used_type_temperature": True}
        else:
            temperature, base, value = fitted
            temperatures[qtype] = temperature
            report[f"type:{QTYPE_NAMES[qtype]}"] = {"n": len(pairs), "temperature": temperature,
                                                    "nll_t1": base, "nll_fitted": value,
                                                    "used_type_temperature": True}

    by_options: dict[str, float] = {}
    for key, pairs in sorted(raw.items()):
        qtype = QTYPES[key.split(":")[0]]
        fitted = best(pairs, args.min_bucket_n)
        if fitted is None:  # 样本太少：退回题型温度，别拟合出一个噪声值
            by_options[key] = temperatures[qtype]
            report[f"bucket:{key}"] = {"n": len(pairs), "temperature": temperatures[qtype],
                                       "nll_t1": None, "nll_fitted": None, "used_type_temperature": True}
        else:
            temperature, base, value = fitted
            by_options[key] = temperature
            report[f"bucket:{key}"] = {"n": len(pairs), "temperature": temperature,
                                       "nll_t1": base, "nll_fitted": value, "used_type_temperature": False}
    for qtype in per_type:  # 校准片里没出现的档位也要给个值，否则服务端会回落到 1.0
        for size in ("2", "3-5", "6-10", "11+"):
            by_options.setdefault(f"{QTYPE_NAMES[qtype]}:{size}", temperatures[qtype])

    model.train()
    return {"temperature": temperatures, "temperature_by_options": by_options, "report": report}


# ----------------------------------------------------------------------------- 主流程


def train(args) -> int:
    global QTYPES
    import torch
    from safetensors.torch import save_file

    global IS_MAIN

    dist_info = setup_distributed(args)
    device = resolve_device(args.device)
    if dist_info and dist_info["backend"] == "nccl":
        device = torch.device("cuda", dist_info["local_rank"])

    items_path = os.path.abspath(args.items)
    out_dir = os.path.abspath(args.out)
    manifest_path = os.path.join(os.path.dirname(items_path), "build_manifest.json")
    build_manifest = None
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            build_manifest = json.load(handle)
        params = build_manifest.get("params", {})
        if not args.force and (params.get("max_len") != args.max_len
                               or params.get("head_max_len") != args.head_max_len):
            raise SystemExit(
                f"训练项是按 max_len={params.get('max_len')} / head_max_len={params.get('head_max_len')} "
                f"切好的，与这里的 {args.max_len}/{args.head_max_len} 不一致。\n"
                "改这两个值必须重新 build（--force 才能强行忽略）。"
            )

    base = fd.resolve_base_checkpoint(args.base, args.subfolder)
    model, tok, cfg = load_checkpoint(
        base, device, max_len=args.max_len, head_max_len=args.head_max_len
    )

    import laya
    from laya.common import QTYPES as LAYA_QTYPES

    if dict(LAYA_QTYPES) != QTYPES:
        raise SystemExit(f"题型表与 laya.common.QTYPES 不一致：{dict(LAYA_QTYPES)} vs {QTYPES}")
    QTYPES = dict(LAYA_QTYPES)

    items = fd.read_jsonl(items_path)
    if not items:
        raise SystemExit(f"{items_path} 是空的 —— 先跑 finetune/build.py")
    if args.limit_items:
        items = items[: args.limit_items]
    train_items, calib_items = calibration_slice(
        items, frac=args.calib_frac, cap=args.calib_max, seed=args.calib_seed
    )
    if not args.no_calibrate and len(calib_items) < args.min_calib:
        raise SystemExit(
            f"校准片只有 {len(calib_items)} 项（< --min-calib {args.min_calib}）："
            "要么多给点数据，要么调小 --min-calib，要么用 --no-calibrate 明确跳过。"
        )

    dtype = cuda_dtype(cfg, device)
    amp_enabled = device.type == "cuda"
    use_scaler = amp_enabled and dtype == torch.float16

    if args.freeze_encoder:
        for param in model.encoder.parameters():
            param.requires_grad_(False)
    elif args.grad_checkpoint in ("encoder", "all"):
        model.encoder.gradient_checkpointing_enable(
            gradient_checkpointing_kwargs={"use_reentrant": False}
        )
    if args.grad_checkpoint == "all":
        model.head_checkpointing = True

    world = dist_info["world"] if dist_info else 1
    micro = max(1, min(args.micro_batch, args.max_tokens_per_batch // max(1, args.max_len)))
    usable_per_epoch = max(1, len(train_items) - (len(train_items) % world))
    windows_per_epoch = max(1, -(-(usable_per_epoch // world) // micro))
    steps_per_epoch = max(1, -(-windows_per_epoch // args.accum))
    total_steps = args.max_steps or steps_per_epoch * args.epochs
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total_params = sum(p.numel() for p in model.parameters())

    if args.dry_run:
        log(f"解释器 {sys.executable}")
        log(f"torch {torch.__version__}  device {device}  cuda={torch.cuda.is_available()}  amp={dtype if amp_enabled else 'off'}")
        log(f"laya {laya.__version__}  transformers {_transformers_version()}  grad_scaler={use_scaler}")
        log(f"基座 {base}")
        log(f"训练项 {len(train_items)}（校准片 {len(calib_items)}）  world={world} micro={micro} accum={args.accum}")
        log(f"每 epoch {steps_per_epoch} 步，合计 {total_steps} 步，等价批 {micro * args.accum * world} 序列")
        log(f"可训练参数 {trainable / 1e6:.1f}M / {total_params / 1e6:.1f}M（freeze_encoder={args.freeze_encoder}）")
        log(f"输出 {out_dir}")
        return 0

    torch.manual_seed(args.seed)
    model.to(device)
    if dist_info:
        model = torch.nn.parallel.DistributedDataParallel(
            model,
            device_ids=[dist_info["local_rank"]] if dist_info["backend"] == "nccl" else None,
        )
    raw_model = model.module if dist_info else model
    if args.compile:
        raw_model.encoder = torch.compile(raw_model.encoder, dynamic=True)

    encoder_params = [p for n, p in raw_model.named_parameters()
                      if n.startswith("encoder.") and p.requires_grad]
    head_params = [p for n, p in raw_model.named_parameters()
                   if not n.startswith("encoder.") and p.requires_grad]
    groups = [{"params": g, "lr": lr} for g, lr in
              ((encoder_params, args.lr_encoder), (head_params, args.lr_head)) if g]
    optimizer = torch.optim.AdamW(groups, weight_decay=args.weight_decay)
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=total_steps, eta_min=1e-6)
    scaler = torch.amp.GradScaler("cuda", enabled=use_scaler)

    os.makedirs(out_dir, exist_ok=True)
    resume_path = os.path.join(out_dir, "resume.pt")
    step, start_epoch, start_window = 0, 0, 0
    if args.resume and os.path.isfile(resume_path):
        saved = torch.load(resume_path, map_location="cpu", weights_only=False)
        raw_model.load_state_dict(
            {k.replace("encoder._orig_mod.", "encoder."): v for k, v in saved["model"].items()},
            strict=False,
        )
        optimizer.load_state_dict(saved["opt"])
        scheduler.load_state_dict(saved["sched"])
        scaler.load_state_dict(saved["scaler"])
        step = int(saved["step"])
        start_epoch, start_window = int(saved["epoch"]), int(saved["window"])
        log(f"从 {resume_path} 续跑：epoch {start_epoch} 第 {start_window} 个窗口，step {step}")

    stop = {"flag": False}

    def request_stop(signum, _frame):
        stop["flag"] = True
        log(f"收到信号 {signum}：跑完当前窗口就存续跑点退出")

    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            with contextlib.suppress(ValueError, OSError):
                signal.signal(getattr(signal, name), request_stop)

    def save_resume(epoch: int, window: int) -> None:
        if dist_info and dist_info["rank"] != 0:
            return
        tmp = resume_path + ".tmp"
        try:
            torch.save(
                {"model": raw_model.state_dict(), "opt": optimizer.state_dict(),
                 "sched": scheduler.state_dict(), "scaler": scaler.state_dict(),
                 "step": step, "epoch": epoch, "window": window},
                tmp,
            )
        except OSError as exc:      # 磁盘满 / 句柄占用：不中断训练
            log(f"写续跑点失败，跳过这一次（训练继续）：{exc}")
            with contextlib.suppress(OSError):
                os.remove(tmp)
            return
        if not _atomic_replace(tmp, resume_path):
            # 旧续跑点还完整，多出来的 tmp 是废文件：能删就删，删不掉留给下次覆盖
            with contextlib.suppress(OSError):
                os.remove(tmp)
            log(f"续跑点未能落盘（文件被占用）；旧的 {os.path.basename(resume_path)} 仍然有效，训练继续。")

    log(f"开始训练：{len(train_items)} 项，epochs={args.epochs}，micro={micro}，accum={args.accum}，"
        f"world={world}，device={device}")
    started = time.time()
    order = list(range(len(train_items)))
    finished = False
    for epoch in range(start_epoch, args.epochs):
        random.Random(args.seed + epoch).shuffle(order)
        usable = len(order) - (len(order) % world)
        shuffled = [train_items[order[i]] for i in range(usable)]
        mine = shuffled[dist_info["rank"] :: world] if dist_info else shuffled
        windows = [mine[i : i + micro] for i in range(0, len(mine), micro)]
        sigma = args.sigma_start + (args.sigma_end - args.sigma_start) * (epoch / max(1, args.epochs - 1))
        model.train()
        running = {"loss": 0.0, "rl": 0.0, "ce": 0.0, "reward": 0.0, "n": 0}
        first_window = start_window if epoch == start_epoch else 0
        for window_index in range(first_window, len(windows)):
            window = windows[window_index]
            group_start = window_index - window_index % args.accum
            group_size = min(args.accum, len(windows) - group_start)
            group_end = (window_index + 1) % args.accum == 0 or window_index == len(windows) - 1
            sync = model.no_sync() if dist_info and not group_end else contextlib.nullcontext()
            batch = {k: v.to(device) for k, v in collate(window, tok.pad_token_id).items()}
            with sync, torch.autocast(device_type=device.type, dtype=dtype, enabled=amp_enabled):
                logits, act = model(
                    batch["input_ids"], batch["attention_mask"], batch["marker_pos"],
                    batch["marker_mask"], batch["qtype"],
                )
            stats = rlcd_loss(logits, act, batch, sigma, args)
            loss = (stats["loss_rl"] + stats["loss_ce"]) / group_size + 0.0 * act.sum()
            if use_scaler:
                scaler.scale(loss).backward()
            else:
                loss.backward()
            running["loss"] += float(loss.item()) * group_size
            running["rl"] += float(stats["loss_rl"].item())
            running["ce"] += float(stats["loss_ce"].item())
            running["reward"] += float(stats["reward"].item())
            running["n"] += 1
            if group_end:
                if use_scaler:
                    scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(
                    [p for p in raw_model.parameters() if p.requires_grad], args.clip
                )
                if use_scaler:
                    scaler.step(optimizer)
                    scaler.update()
                else:
                    optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
                step += 1
                if step % args.log_every == 0:
                    n = max(1, running["n"])
                    log(f"epoch {epoch + 1}/{args.epochs} step {step}/{total_steps} "
                        f"loss {running['loss'] / n:.3f} ce {running['ce'] / n:.3f} "
                        f"reward {running['reward'] / n:.3f} sigma {sigma:.2f} "
                        f"lr {scheduler.get_last_lr()[0]:.2e} {time.time() - started:.0f}s")
                    running = {"loss": 0.0, "rl": 0.0, "ce": 0.0, "reward": 0.0, "n": 0}
                if args.max_steps and step >= args.max_steps:
                    log(f"达到 --max-steps {args.max_steps}，收尾（不写续跑点）")
                    finished = True
                    break
                if args.save_every and step % args.save_every == 0:
                    save_resume(epoch, window_index + 1)
                if stop["flag"]:
                    save_resume(epoch, window_index + 1)
                    log(f"续跑点已存 {resume_path}（epoch {epoch + 1}，窗口 {window_index + 1}，step {step}）")
                    if dist_info:
                        torch.distributed.barrier()
                        torch.distributed.destroy_process_group()
                    return 0
        else:
            if epoch + 1 < args.epochs:
                save_resume(epoch + 1, 0)
            log(f"=== epoch {epoch + 1}/{args.epochs} 结束，{time.time() - started:.0f}s")
            continue
        if finished:
            break

    model.eval()
    if dist_info:
        torch.distributed.barrier()
    if dist_info and dist_info["rank"] != 0:
        torch.distributed.destroy_process_group()
        return 0

    calibration = None
    if args.no_calibrate:
        # 不校准时也必须清掉旧桶温度：它在推理端优先级更高，会静默顶掉题型温度
        cfg.pop("temperature_by_options", None)
        cfg["temperature"] = [1.0, 1.0, 1.0]
    else:
        calibration = fit_temperatures(raw_model, calib_items, tok.pad_token_id, device, args)
        cfg["temperature"] = calibration["temperature"]
        cfg["temperature_by_options"] = calibration["temperature_by_options"]
        log("校准（留出片上拟合，温度夹在 [0.5, 5]，与服务端一致）：")
        for key, row in sorted(calibration["report"].items()):
            tail = ("样本不足，用题型温度" if row["used_type_temperature"]
                    else f"NLL {row['nll_t1']:.3f} -> {row['nll_fitted']:.3f}")
            log(f"  {key:22s} n={row['n']:4d} T={row['temperature']:.3f}  {tail}")

    if hasattr(raw_model.encoder, "_orig_mod"):
        raw_model.encoder = raw_model.encoder._orig_mod
    save_file(
        {k: v.half().contiguous().cpu() for k, v in raw_model.state_dict().items()},
        os.path.join(out_dir, "model.safetensors"),
    )
    raw_model.encoder.config.save_pretrained(os.path.join(out_dir, "encoder"))
    tok.save_pretrained(os.path.join(out_dir, "tokenizer"))
    cfg.update({"max_len": args.max_len, "head_max_len": args.head_max_len,
                "fine_tuned": True, "model_name": args.model_name})
    with open(os.path.join(out_dir, "rl_agent_config.json"), "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=2)
    report = {
        "generator": "finetune/train.py",
        "version": 1,
        "base_checkpoint": base,
        "out": out_dir,
        "args": vars(args),
        "items": {"path": items_path, "train_items": len(train_items),
                  "calibration_items": len(calib_items), "epochs": args.epochs,
                  "steps": step, "effective_batch": micro * args.accum * world},
        "calibration": calibration["report"] if calibration else None,
        "temperature": cfg["temperature"],
        "temperature_by_options": cfg.get("temperature_by_options", {}),
        "build_params": build_manifest["params"] if build_manifest else None,
        "build_manifest_sha256": fd.sha256_file(manifest_path) if build_manifest else None,
        "items_sha256": fd.sha256_file(items_path),
        "environment": {"python": sys.version.split()[0], "torch": torch.__version__,
                        "transformers": _transformers_version(), "laya": laya.__version__,
                        "device": str(device), "dtype": str(dtype) if amp_enabled else "fp32"},
        "trainable_params": trainable,
        "total_params": total_params,
        "seconds": round(time.time() - started, 1),
    }
    with open(os.path.join(out_dir, "finetune_report.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with contextlib.suppress(FileNotFoundError):
        os.remove(resume_path)
    log(f"checkpoint -> {out_dir}")
    log("评估：finetune/eval.py --records finetune/out/dev_records.jsonl "
        f"--checkpoint base=<基座> --checkpoint ft={out_dir}")
    if dist_info:
        torch.distributed.destroy_process_group()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--items", default="finetune/out/train_items.jsonl")
    parser.add_argument("--out", default="finetune/checkpoints/laya-spire")
    parser.add_argument("--base", default="convaiinnovations/laya", help="基座 checkpoint（目录或 HF repo id）")
    parser.add_argument("--subfolder", default=None)
    parser.add_argument("--model-name", default="laya-spire", help="写进 rl_agent_config.json 的 model_name")
    parser.add_argument("--max-len", type=int, default=2048, help="必须与 build 的一致")
    parser.add_argument("--head-max-len", type=int, default=320, help="必须与 build 的一致")
    parser.add_argument("--force", action="store_true", help="忽略「训练项与参数不一致」的检查")
    parser.add_argument("--epochs", type=int, default=4)
    parser.add_argument("--max-steps", type=int, default=0, help="跑这么多优化步就收尾（冒烟用）")
    parser.add_argument("--limit-items", type=int, default=0, help="只用前 N 项（冒烟用）")
    parser.add_argument("--micro-batch", type=int, default=8, help="单个 micro-batch 最多几题")
    parser.add_argument("--max-tokens-per-batch", type=int, default=8192, help="micro-batch 的 padded token 预算")
    parser.add_argument("--accum", type=int, default=4, help="累积几个 micro-batch 更新一次")
    parser.add_argument("--lr-encoder", type=float, default=2.5e-5)
    parser.add_argument("--lr-head", type=float, default=1e-4)
    parser.add_argument("--weight-decay", type=float, default=0.01)
    parser.add_argument("--clip", type=float, default=1.0)
    parser.add_argument("--samples", type=int, default=4, help="每题采样几个加噪分布（GRPO）")
    parser.add_argument("--sigma-start", type=float, default=0.4)
    parser.add_argument("--sigma-end", type=float, default=0.1)
    parser.add_argument("--w-sph", type=float, default=0.75)
    parser.add_argument("--w-rps", type=float, default=1.0)
    parser.add_argument("--freeze-encoder", action="store_true", help="只训决策头（小数据量推荐）")
    parser.add_argument("--grad-checkpoint", choices=("none", "encoder", "all"), default="encoder")
    parser.add_argument("--compile", action="store_true", help="torch.compile 编码器（变长 batch 上常更慢）")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--backend", default=None, help="DDP backend（默认 cuda->nccl / 其它->gloo）")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--save-every", type=int, default=200, help="每多少步写一次续跑点")
    parser.add_argument("--log-every", type=int, default=5)
    parser.add_argument("--resume", action="store_true", help="有 resume.pt 时从它续跑")
    parser.add_argument("--calib-frac", type=float, default=0.1, help="从训练项里留出的校准片比例")
    parser.add_argument("--calib-max", type=int, default=400)
    parser.add_argument("--calib-seed", type=int, default=7)
    parser.add_argument("--calib-batch", type=int, default=8)
    parser.add_argument("--min-calib", type=int, default=10, help="校准片少于这么多项就不拟合")
    parser.add_argument("--min-bucket-n", type=int, default=30, help="某档样本少于这么多就退回题型温度")
    parser.add_argument("--no-calibrate", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="只打印环境与训练计划")
    args = parser.parse_args(argv)
    if args.accum < 1 or args.micro_batch < 1 or args.samples < 2:
        raise SystemExit("--accum / --micro-batch 要 >= 1，--samples 要 >= 2（GRPO 需要组内比较）")
    return train(args)


if __name__ == "__main__":
    raise SystemExit(main())

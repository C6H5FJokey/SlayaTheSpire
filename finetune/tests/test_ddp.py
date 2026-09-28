"""多卡（DDP）路径的验证。

两层：

1. `test_two_rank_gloo_training_writes_the_checkpoint` 用 `subprocess` 起**真的两个进程**
   （gloo、CPU），走的和远端 `torchrun --standalone --nproc_per_node=2 finetune/train.py` 同一段
   代码。**这台 Windows 机器的 hostname 解析不到可用地址**（gloo 报
   `makeDeviceForHostname(): unsupported gloo device`），所以本机跑不了会 skip；Linux 上会真跑。
2. 进程内用桩进程组跑 `WORLD_SIZE=2` 的**编排**逻辑（rank 切分、no_sync/梯度累积分组、
   只有 rank0 写产物）—— 这部分在任何机器上都能验，且不依赖 gloo。
"""

import contextlib
import json
import os
import socket
import subprocess
import sys

import pytest

FINETUNE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 这台机器/这类环境的 gloo 起不来时，子进程会打印这些字样之一。只在这几种**环境指纹**上跳过，
# 别的失败（真的代码 bug）必须报错。
ENV_SIGNATURES = ("unsupported gloo device", "makeDeviceForHostname", "makeDeviceForInterface")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def distributed_available():
    pytest.importorskip("torch")
    import torch

    if not torch.distributed.is_available() or not torch.distributed.is_gloo_available():
        pytest.skip("这份 torch 没编译 gloo 后端")
    return True


def train_args(tiny_build: str, tiny_checkpoint: str, out: str, *extra: str) -> list[str]:
    return [
        sys.executable, os.path.join(FINETUNE, "train.py"),
        "--items", os.path.join(tiny_build, "train_items.jsonl"),
        "--base", tiny_checkpoint, "--out", out,
        "--max-len", "256", "--head-max-len", "96", "--epochs", "1",
        "--calib-frac", "0.5", "--min-calib", "1", "--min-bucket-n", "1",
        "--device", "cpu", "--backend", "gloo", "--log-every", "1", *extra,
    ]


def test_two_rank_gloo_training_writes_the_checkpoint(tiny_build, tiny_checkpoint, tmp_dir,
                                                      distributed_available):
    out = tmp_dir("ddp")
    env_common = dict(os.environ, WORLD_SIZE="2", MASTER_ADDR="127.0.0.1",
                      MASTER_PORT=str(free_port()), PYTHONIOENCODING="utf-8")
    procs = [
        subprocess.Popen(train_args(tiny_build, tiny_checkpoint, out),
                         env=dict(env_common, RANK=str(rank), LOCAL_RANK=str(rank)),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace")
        for rank in range(2)
    ]
    outputs = []
    for proc in procs:
        text, _ = proc.communicate(timeout=900)
        outputs.append(text)
    if any(signature in text for text in outputs for signature in ENV_SIGNATURES):
        pytest.skip("这台机器的 gloo 起不来（hostname 解析不到可用地址）；Linux 上会真跑："
                    + next(line for text in outputs for line in text.splitlines()
                           if any(s in line for s in ENV_SIGNATURES)))
    for rank, proc in enumerate(procs):
        assert proc.returncode == 0, f"rank {rank} 退出码 {proc.returncode}：\n{outputs[rank]}"

    assert "world=2" in outputs[0]
    assert "checkpoint ->" in outputs[0]
    assert "checkpoint ->" not in outputs[1]        # 只有 rank0 写产物
    assert not os.path.exists(os.path.join(out, "resume.pt"))
    with open(os.path.join(out, "rl_agent_config.json"), "r", encoding="utf-8") as handle:
        cfg = json.load(handle)
    assert cfg["fine_tuned"] is True
    assert all(0.5 <= t <= 5.0 for t in cfg["temperature"])
    with open(os.path.join(out, "finetune_report.json"), "r", encoding="utf-8") as handle:
        report = json.load(handle)
    effective = report["items"]["effective_batch"]
    assert effective > 0 and effective % 2 == 0    # micro * accum * world
    assert report["environment"]["torch"]


class _StubDDP:
    """假 DDP：只提供 `train.py` 用到的那几样（`module` / `no_sync()` / 前向）。"""

    def __init__(self, module, device_ids=None, **kwargs):
        self.module = module
        self.device_ids = device_ids

    def no_sync(self):
        return contextlib.nullcontext()

    def __call__(self, *args, **kwargs):
        return self.module(*args, **kwargs)

    def __getattr__(self, name):      # train()/eval()/parameters() 都直接落到内层模型
        return getattr(self.module, name)


def _stub_process_group(monkeypatch, rank: int, world: int):
    """让 `train.py` 以为自己在 DDP 里（不起真进程组），用来验它自己的编排逻辑。"""
    import torch
    import torch.distributed as dist

    monkeypatch.setenv("WORLD_SIZE", str(world))
    monkeypatch.setenv("RANK", str(rank))
    monkeypatch.setenv("LOCAL_RANK", str(rank))
    monkeypatch.setattr(dist, "is_available", lambda: True)
    monkeypatch.setattr(dist, "init_process_group", lambda *a, **k: None)
    monkeypatch.setattr(dist, "barrier", lambda *a, **k: None)
    monkeypatch.setattr(dist, "destroy_process_group", lambda *a, **k: None)
    monkeypatch.setattr(torch.nn.parallel, "DistributedDataParallel", _StubDDP)


@pytest.mark.parametrize("rank,expect_checkpoint", [(0, True), (1, False)])
def test_distributed_orchestration_only_rank0_writes(monkeypatch, tiny_build, tiny_checkpoint,
                                                     tmp_dir, rank, expect_checkpoint):
    import train as trainer

    _stub_process_group(monkeypatch, rank=rank, world=2)
    out = tmp_dir(f"ddp-rank{rank}")
    code = trainer.main([
        "--items", os.path.join(tiny_build, "train_items.jsonl"),
        "--base", tiny_checkpoint, "--out", out,
        "--max-len", "256", "--head-max-len", "96", "--epochs", "1", "--save-every", "1",
        "--calib-frac", "0.5", "--min-calib", "1", "--min-bucket-n", "1",
        # 明确 gloo：默认 backend 在"有 CUDA 但只有一张卡"的机器上会走 nccl 分支，
        # 然后 set_device(local_rank=1) 直接 invalid device ordinal
        "--device", "cpu", "--backend", "gloo", "--seed", "3", "--log-every", "1",
    ])
    assert code == 0
    written = os.path.exists(os.path.join(out, "rl_agent_config.json"))
    assert written is expect_checkpoint
    if not written:
        return
    with open(os.path.join(out, "finetune_report.json"), "r", encoding="utf-8") as handle:
        report = json.load(handle)
    # 等价批把 world 也算进去；rank 切分让每个 rank 只看到一半训练项
    assert report["items"]["effective_batch"] % 2 == 0
    assert report["items"]["train_items"] > 0

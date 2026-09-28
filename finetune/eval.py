#!/usr/bin/env python
r"""在留出记录上评测 checkpoint，对比「基座 / 微调后 / 采集时落盘的答案」（见 docs/13-finetune.md）。

    # 最常用：基座 vs 微调后（序列长度自动取 build_manifest.json）
    .\.venv-laya\Scripts\python.exe finetune\eval.py ^
        --records finetune\out\dev_records.jsonl ^
        --checkpoint base=convaiinnovations/laya ^
        --checkpoint ft=finetune\checkpoints\laya-spire-v1

    # 只评微调后，并把完整结果（含分层）落一份 JSON 存档
    .\.venv-laya\Scripts\python.exe finetune\eval.py --checkpoint ft=finetune\checkpoints\laya-spire-v1 --json finetune\out\eval.json

`--checkpoint` 的两种写法：

    NAME=PATH    加载 checkpoint（目录，或能被 `ft_data.resolve_base_checkpoint` 解析的 HF repo id）
    recorded     用数据集行里 `answers` 落下的**采集时基座**概率（不占显存、不用 GPU）

**序列与温度都按部署口径算**：构题走 `laya.common.build_sequence`（训练 / 服务用的是同一个
函数），温度按 `(题型, 候选数档)` 取该 checkpoint 的 `temperature_by_options`（与服务端
`Agent._decode_answers` 同一条规则），所以这里报的概率就是线上 `/v1/systemone` 会给的概率。

每个变体报：

| 指标 | 含义 |
|---|---|
| 准确率 | argmax 是否落在人类选择上 |
| NLL / Brier | 概率质量有没有放在正确答案上，越小越好 |
| ECE(`answer_confidence`) | 置信度与正确率的偏差（15 桶）；统计一律用这个量 |
| 平均置信度 | 明显偏高就是过自信，要和 ECE 一起看 |
| NLL@T=1 -> NLL@T | checkpoint 自带的温度这一层到底有没有用（只对有 logits 的变体） |
| p50/p95 延迟 | 逐题端到端（含分词），给部署预算用 |
| 与 recorded 的一致率 | 与「采集时基座的选择」相同的比例 —— 路线图里 `agreement_rate` 的离线版 |

**别把 dev 当泛化上界**：只有一局数据时，dev 是 build 从 train 里按房间切出来的，同一局、
同一角色、同一牌风，数字偏乐观（见 docs/13-finetune.md「只有一局数据时」）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ft_data as fd  # noqa: E402
import train as trainer  # noqa: E402


def softmax(z: np.ndarray) -> np.ndarray:
    z = np.asarray(z, dtype=np.float64)
    z = z - z.max()
    e = np.exp(z)
    return e / e.sum()


def resolve_lengths(records_path: str, args) -> tuple[int, int]:
    """序列长度：命令行 > 同目录的 build_manifest.json > 报错（**不猜**）。

    评测必须在**同一个** `max_len` 下比：基座自己的 cfg 是 512，微调后是 2048 ——
    拿两种不同的截断去比准确率是自欺欺人。`build.py` 把这两个值写进了 `build_manifest.json`，
    所以默认跟它走。
    """
    if args.max_len and args.head_max_len:
        return args.max_len, args.head_max_len
    manifest_path = os.path.join(os.path.dirname(os.path.abspath(records_path)), "build_manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as handle:
            params = json.load(handle).get("params", {})
        if not args.max_len and not args.head_max_len:
            return int(params["max_len"]), int(params["head_max_len"])
    raise SystemExit(
        "不知道用哪个 max_len / head_max_len：同目录没有 build_manifest.json，"
        "请显式给 --max-len 与 --head-max-len。"
    )


def make_row(record: dict, item: dict, *, bucket: str, pred: int, probs: np.ndarray,
             nll_t1: float | None, temperature: float | None, latency_ms: float | None,
             recorded: dict | None, options: list[str] | None = None) -> dict:
    """一条评测行。`probs` 是部署口径的概率（已按该 checkpoint 的温度缩放）。"""
    k = int(item["option_count"])
    gold = int(item["label"])
    p = np.asarray(probs, dtype=np.float64)[:k]
    onehot = np.zeros(k, dtype=np.float64)
    onehot[gold] = 1.0
    entropy = -(p * np.log(np.clip(p, 1e-12, 1.0))).sum()
    row = {
        "record_id": record["record_id"],
        "qid": item["qid"],
        "decision_point": item["decision_point"],
        "qtype": item["qtype"],
        "bucket": bucket,
        "option_count": k,
        "gold": gold,
        "pred": int(pred),
        "correct": int(pred) == gold,
        "conf": float(p.max()),
        "entropy_conf": float(1.0 - entropy / np.log(k)) if k > 1 else 1.0,
        "nll": float(-np.log(max(float(p[gold]), 1e-12))),
        "brier": float(((p - onehot) ** 2).sum()),
        "nll_t1": nll_t1,
        "temperature": temperature,
        "latency_ms": latency_ms,
        "agreement_with_recorded": None,
    }
    if recorded:
        probs_recorded = recorded.get("probabilities")
        keys = options if options is not None else item.get("options")
        if isinstance(probs_recorded, dict) and keys and set(probs_recorded) == set(keys):
            pred_recorded = int(np.argmax([float(probs_recorded[key]) for key in keys]))
            row["agreement_with_recorded"] = pred_recorded == int(pred)
    return row


def run_checkpoint(name: str, path: str, records: list[dict], *, device, max_len: int,
                   head_max_len: int, batch: int) -> tuple[dict, list[dict]]:
    """在一个 checkpoint 上跑完整批记录，返回 `(统计, 逐题行)`。"""
    import torch

    from laya.common import QTYPE_NAMES, QTYPES, clamp_temperature, temp_bucket

    model, tok, cfg = trainer.load_checkpoint(path, device, max_len=max_len, head_max_len=head_max_len)
    dtype = trainer.cuda_dtype(cfg, device)
    amp = device.type == "cuda"
    temps = [clamp_temperature(t) for t in cfg.get("temperature", [1.0, 1.0, 1.0])]
    by_options = {key: clamp_temperature(value)
                  for key, value in (cfg.get("temperature_by_options") or {}).items()}

    rows: list[dict] = []
    skipped: dict[str, int] = {}
    pending: list[tuple[dict, dict]] = []
    started = time.perf_counter()

    def flush() -> None:
        if not pending:
            return
        batch_tensors = {
            key: value.to(device)
            for key, value in trainer.collate([it for _, it in pending], tok.pad_token_id).items()
            if key in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")
        }
        t0 = time.perf_counter()
        with torch.no_grad(), torch.autocast(device_type=device.type, dtype=dtype, enabled=amp):
            logits, _ = model(**batch_tensors)
        forward_ms = (time.perf_counter() - t0) / max(1, len(pending)) * 1000.0
        logits = logits.float().cpu().numpy()
        for index, (record, item) in enumerate(pending):
            k = int(item["option_count"])
            qtype = QTYPES[item["qtype"]]
            z = logits[index, :k]
            temperature = by_options.get(temp_bucket(qtype, k), temps[qtype])
            probs = softmax(z / temperature)
            probs_t1 = softmax(z)
            rows.append(
                make_row(
                    record, item,
                    bucket=temp_bucket(qtype, k),
                    pred=int(np.argmax(probs)),
                    probs=probs,
                    nll_t1=float(-np.log(max(float(probs_t1[int(item["label"])]), 1e-12))),
                    temperature=float(temperature),
                    latency_ms=forward_ms + float(item["_tok_ms"]),
                    recorded=(record.get("recorded") or {}).get(item["qid"]),
                    options=record["gold"][item["qid"]]["options"],
                )
            )
        pending.clear()

    for record in records:
        t0 = time.perf_counter()
        items, _ = fd.make_items(record, tok, max_len=max_len, head_max_len=head_max_len)
        tokenize_ms = (time.perf_counter() - t0) / max(1, len(items)) * 1000.0
        missing = len(record["gold"]) - len({item["qid"] for item in items})
        if missing > 0:      # 选项塞不进 head_max_len，build 时也会丢掉这些题
            skipped["options_not_fit"] = skipped.get("options_not_fit", 0) + missing
        for item in items:
            item["_tok_ms"] = tokenize_ms
            pending.append((record, item))
        if len(pending) >= batch:
            flush()
    flush()

    seconds = time.perf_counter() - started
    del model, tok
    if device.type == "cuda":
        torch.cuda.empty_cache()
    return summarize(name, rows, skipped=skipped, seconds=seconds), rows


def run_recorded(name: str, records: list[dict]) -> tuple[dict, list[dict]]:
    """用数据集行里落下的基座概率离线复算同一批指标（没有 logits，故没有 NLL@T=1）。"""
    from laya.common import QTYPES, temp_bucket

    rows: list[dict] = []
    skipped: dict[str, int] = {}
    for record in records:
        for qid, gold in record["gold"].items():
            probs_recorded = ((record.get("recorded") or {}).get(qid) or {}).get("probabilities")
            keys = gold["options"]
            if not isinstance(probs_recorded, dict) or set(probs_recorded) != set(keys):
                skipped["no_recorded_answers"] = skipped.get("no_recorded_answers", 0) + 1
                continue
            vector = np.array([float(probs_recorded[key]) for key in keys], dtype=np.float64)
            vector = vector / vector.sum()
            item = {"qid": qid, "option_count": gold["option_count"], "label": gold["y"],
                    "qtype": gold["type"], "decision_point": record["decision_point"]}
            rows.append(
                make_row(record, item, bucket=temp_bucket(QTYPES[gold["type"]], gold["option_count"]),
                         pred=int(np.argmax(vector)), probs=vector, nll_t1=None, temperature=None,
                         latency_ms=None, recorded=None, options=keys)
            )
    return summarize(name, rows, skipped=skipped, seconds=0.0), rows


def group_stats(rows: list[dict]) -> dict:
    correct = np.array([1.0 if r["correct"] else 0.0 for r in rows], dtype=np.float64)
    return {
        "n": len(rows),
        "accuracy": round(float(correct.mean()), 4),
        "mean_nll": round(float(np.mean([r["nll"] for r in rows])), 4),
        "mean_confidence": round(float(np.mean([r["conf"] for r in rows])), 4),
        "mean_options": round(float(np.mean([r["option_count"] for r in rows])), 2),
    }


def summarize(name: str, rows: list[dict], *, skipped: dict[str, int], seconds: float) -> dict:
    if not rows:
        return {"name": name, "n": 0, "skipped": skipped}
    from laya.common import ece_score

    conf = np.array([r["conf"] for r in rows], dtype=np.float64)
    correct = np.array([1.0 if r["correct"] else 0.0 for r in rows], dtype=np.float64)
    out: dict = {
        "name": name,
        "n": len(rows),
        "skipped": skipped,
        "accuracy": round(float(correct.mean()), 4),
        "mean_nll": round(float(np.mean([r["nll"] for r in rows])), 4),
        "mean_brier": round(float(np.mean([r["brier"] for r in rows])), 4),
        "ece": round(float(ece_score(conf, correct)), 4),
        "mean_confidence": round(float(conf.mean()), 4),
        "mean_entropy_confidence": round(float(np.mean([r["entropy_conf"] for r in rows])), 4),
        "seconds": round(seconds, 1),
    }
    nll_t1 = [r["nll_t1"] for r in rows if r["nll_t1"] is not None]
    if len(nll_t1) == len(rows):
        out["mean_nll_t1"] = round(float(np.mean(nll_t1)), 4)
        out["temperature_gain"] = round(out["mean_nll_t1"] - out["mean_nll"], 4)
    latency = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    if latency:
        out["latency_ms_p50"] = round(float(np.percentile(latency, 50)), 2)
        out["latency_ms_p95"] = round(float(np.percentile(latency, 95)), 2)
    agreement = [r["agreement_with_recorded"] for r in rows if r["agreement_with_recorded"] is not None]
    if agreement:
        out["agreement_with_recorded"] = round(float(np.mean(agreement)), 4)
        out["agreement_n"] = len(agreement)
    for key, field in (("by_decision_point", "decision_point"), ("by_bucket", "bucket"),
                       ("by_qtype", "qtype")):
        names = sorted({str(r[field]) for r in rows})
        out[key] = {n: group_stats([r for r in rows if str(r[field]) == n]) for n in names}
    return out


def format_line(summary: dict) -> str:
    if not summary.get("n"):
        # 一个变体一条都没评上（例如 records 里没有落盘答案）时也要能打印，不能 KeyError
        return f"{summary['name']:<10} n=0（没有可评的题：{summary.get('skipped') or {}}）"
    parts = [
        f"{summary['name']:<10} 准确率 {summary['accuracy']:.3f}",
        f"NLL {summary['mean_nll']:.3f}",
        f"Brier {summary['mean_brier']:.3f}",
        f"ECE {summary['ece']:.3f}",
        f"平均置信度 {summary['mean_confidence']:.3f}",
    ]
    if "mean_nll_t1" in summary:
        parts.append(f"NLL@T=1 {summary['mean_nll_t1']:.3f}（温度 {summary['temperature_gain']:+.3f}）")
    if "latency_ms_p50" in summary:
        parts.append(f"p50 {summary['latency_ms_p50']:.0f}ms p95 {summary['latency_ms_p95']:.0f}ms")
    if "agreement_with_recorded" in summary:
        parts.append(f"与 recorded 一致率 {summary['agreement_with_recorded']:.3f}")
    return "  ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--records", default="finetune/out/dev_records.jsonl",
                        help="要评的记录（build.py 产出的 {split}_records.jsonl）")
    parser.add_argument("--checkpoint", action="append", default=[], metavar="NAME=PATH",
                        help="NAME=PATH 加载 checkpoint；裸 `recorded` 用数据集里落盘的答案。"
                             "不给就是 `base=<--base>`（外加 recorded）")
    parser.add_argument("--base", default="convaiinnovations/laya",
                        help="默认变体 `base` 指向的 checkpoint（目录或 HF repo id）")
    parser.add_argument("--subfolder", default=None, help="HF 仓库内的子目录（multilingual / typed-decisions）")
    parser.add_argument("--max-len", type=int, default=0, help="0 = 从 build_manifest.json 读")
    parser.add_argument("--head-max-len", type=int, default=0, help="0 = 从 build_manifest.json 读")
    parser.add_argument("--batch", type=int, default=8, help="每批几题（显存换速度）")
    parser.add_argument("--limit", type=int, default=0, help="只评前 N 条记录（冒烟用）")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--json", default=None, help="把完整结果（含分层）写成 JSON")
    args = parser.parse_args(argv)

    records = fd.read_jsonl(args.records)
    if not records:
        raise SystemExit(f"{args.records} 是空的 —— 先跑 finetune/build.py")
    if args.limit:
        records = records[: args.limit]
    max_len, head_max_len = resolve_lengths(args.records, args)
    device = trainer.resolve_device(args.device)

    specs = list(args.checkpoint)
    has_recorded = any(bool(r.get("recorded")) for r in records)
    if not specs:
        specs = ["base=" + args.base] + (["recorded"] if has_recorded else [])
    print(f"[eval] {args.records}：{len(records)} 条记录，max_len={max_len} head_max_len={head_max_len} device={device}")
    results: dict[str, dict] = {}
    order: list[str] = []
    for spec in specs:
        name, _, path = spec.partition("=")
        if name == "recorded" and not path:
            summary, _ = run_recorded(name, records)
        else:
            if not path:
                raise SystemExit(f"--checkpoint 要写成 NAME=PATH（或裸 `recorded`），收到 {spec!r}")
            resolved = fd.resolve_base_checkpoint(path, args.subfolder if name == "base" else None)
            summary, _ = run_checkpoint(name, resolved, records, device=device, max_len=max_len,
                                        head_max_len=head_max_len, batch=max(1, args.batch))
        results[name] = summary
        order.append(name)
        print(f"[eval] {format_line(summary)}")
        if summary.get("skipped"):
            print(f"[eval]   skipped {summary['skipped']}")

    detail = order[-1]
    print(f"[eval] 按决策点（{detail}）：")
    for name, stats in results[detail].get("by_decision_point", {}).items():
        print(f"[eval]   {name:<22} n={stats['n']:<4} 准确率 {stats['accuracy']:.3f}"
              f"  NLL {stats['mean_nll']:.3f}  平均候选 {stats['mean_options']:.1f}")
    print(f"[eval] 按候选数档（{detail}）：")
    for name, stats in results[detail].get("by_bucket", {}).items():
        print(f"[eval]   {name:<12} n={stats['n']:<4} 准确率 {stats['accuracy']:.3f}  NLL {stats['mean_nll']:.3f}")
    if len(order) > 1 and results[order[0]].get("n") and results[order[0]]["n"] == results[detail].get("n"):
        first, last = results[order[0]], results[detail]
        print(f"[eval] 相对 {order[0]}：准确率 {last['accuracy'] - first['accuracy']:+.3f}"
              f"  NLL {last['mean_nll'] - first['mean_nll']:+.3f}"
              f"  ECE {last['ece'] - first['ece']:+.3f}")

    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        payload = {
            "generator": "finetune/eval.py",
            "version": 1,
            "records": {"path": os.path.abspath(args.records), "n": len(records),
                        "sha256": fd.sha256_file(args.records)},
            "max_len": max_len,
            "head_max_len": head_max_len,
            "device": str(device),
            "variants": results,
        }
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"[eval] -> {os.path.abspath(args.json)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python
r"""把 `dataset/*.jsonl` 编译成 Laya 训练项（见 docs/13-finetune.md）。

    # 用 .venv-laya 跑（要 transformers + laya；见 docs/10-deployment.md）
    .\.venv-laya\Scripts\python.exe finetune\build.py --dataset dataset --out finetune\out
    .\.venv-laya\Scripts\python.exe finetune\build.py --dataset dataset --out finetune\out --verify

产物（每行一个 JSON，无缩进、顺序固定 —— 同样的输入逐字节相同）：

    {split}_items.jsonl    训练项：ids / markers / qtype / target / weight
    {split}_records.jsonl  记录：state + questions（Jev 形态）+ gold（含选项 key 顺序）
    build_manifest.json    参数 / 输入 sha256 / 计数 / 告警

split 有四份：`train` / `val` / `test` 来自行内写死的字段（按 run 切分，见 docs/08-dataset.md），
`dev` 是**训练期的房间级留出**（只有 train 时用来给出一个不掺训练数据的数字）。

**为什么要有 build 这一步**：`dataset/` 是数据契约（人读的、与模型无关），训练项是模型契约
（分词后的 ids、选项下标、目标分布）。翻译规则必须唯一、可复现、可校验，所以它单独一层，
而不是散在训练脚本里。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ft_data as fd  # noqa: E402


def build(rows: list[dict], tok, args) -> tuple[dict, dict, dict]:
    """行 -> {split: records} / {split: items} / 统计。"""
    weights = {"human": args.weight_human, "agent": args.weight_agent}
    sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    for source in sources:
        if source not in weights:
            raise SystemExit(f"--sources 里出现未知来源 {source!r}；只有 human / agent")

    skipped: Counter[str] = Counter()
    kept: list[dict] = []
    for row in rows:
        reason = fd.skip_reason(
            row,
            sources=sources,
            min_options=args.min_options,
            include_degenerate=args.include_degenerate,
        )
        if reason:
            skipped[reason] += 1
            continue
        try:
            kept.append(
                fd.row_to_record(
                    row,
                    split=str(row.get("split") or fd.SPLIT_TRAIN),
                    weight=weights[str(row.get("source"))],
                    soft_mix=args.soft_mix,
                )
            )
        except fd.SkipRecord as exc:
            skipped[exc.args[0]] += 1

    dev_map = fd.assign_dev_split(kept, frac=args.dev_frac, seed=args.dev_seed)
    for record in kept:
        record["split"] = dev_map.get(record["record_id"], record["split"])

    records: dict[str, list[dict]] = {}
    per_source: Counter[str] = Counter()
    per_dp: Counter[str] = Counter()
    soft_available = soft_used = soft_total = 0
    for record in kept:
        records.setdefault(record["split"], []).append(record)
        per_source[record["source"]] += 1
        per_dp[record["decision_point"]] += 1
        soft_available += len(record["recorded"])
        for gold in record["gold"].values():
            soft_total += 1
            soft_used += int("soft" in gold)
    for name in records:
        records[name].sort(key=lambda r: (str(r["run_id"]), int(r["seq"]), str(r["record_id"])))

    items: dict[str, list[dict]] = {}
    stats: dict[str, dict] = {}
    for name, recs in records.items():
        bucket: list[dict] = []
        agg = {"records": len(recs), "questions": 0, "state_truncated": 0, "options_truncated": 0}
        for record in recs:
            produced, one = fd.make_items(
                record, tok, max_len=args.max_len, head_max_len=args.head_max_len
            )
            for key in ("questions", "state_truncated", "options_truncated"):
                agg[key] += one[key]
            bucket.extend(produced)
        items[name] = bucket
        agg.update(fd.item_length_stats(bucket))
        stats[name] = agg

    extra = {
        "stats": stats,
        "meta": {
            "skipped": dict(sorted(skipped.items())),
            "per_source": dict(sorted(per_source.items())),
            "per_decision_point": dict(sorted(per_dp.items())),
            "soft_targets": {"available": soft_available, "used": soft_used, "questions": soft_total},
        },
    }
    return records, items, extra


def warnings_for(args, records: dict, extra: dict) -> list[str]:
    warnings: list[str] = []
    if not records.get(fd.SPLIT_VAL) and not records.get(fd.SPLIT_TEST):
        warnings.append(
            "val/test 为空（只有一局）：校准与评测只能落在 train 切出的 dev / 校准片上，数字偏乐观。"
            "再跑一局（或换个 seed）后重导即可拿到真正的留出集。"
        )
    stats = extra["stats"].values()
    state_total = sum(s.get("state_tokens_total", 0) for s in stats)
    state_kept = sum(s.get("state_kept_total", 0) for s in stats)
    if state_kept < state_total:
        warnings.append(
            f"state 被截断：{state_total - state_kept} 个 token 没进序列（保留 {state_kept}/{state_total}）。"
            "模型看不到整份 state。调大 --max-len，或者压缩 state 序列化（见 docs/05-state-schema.md）。"
        )
    truncated = sum(s.get("state_truncated", 0) for s in stats)
    if truncated:
        warnings.append(f"其中 {truncated} 题的 state 不完整（同一局里越靠后的房间越长，越容易撞上限）")
    options_truncated = sum(s.get("options_truncated", 0) for s in stats)
    if options_truncated:
        warnings.append(
            f"{options_truncated} 题的候选项放不进 --head-max-len={args.head_max_len}，这些题被丢弃；"
            "调大 --head-max-len（代价是 state 能用的 token 变少）"
        )
    soft = extra["meta"]["soft_targets"]
    if args.soft_mix > 0 and soft["used"] < soft["questions"]:
        warnings.append(
            f"软目标只覆盖 {soft['used']}/{soft['questions']} 题（其余退回 one-hot）："
            "采集时没开 observe_human.also_query_model，或候选 key 与答案 key 对不齐"
        )
    return warnings


def render(records: dict | None = None, items: dict | None = None) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for name, recs in sorted((records or {}).items()):
        out[f"{name}_records.jsonl"] = b"".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for r in recs
        )
    for name, its in sorted((items or {}).items()):
        out[f"{name}_items.jsonl"] = b"".join(
            json.dumps(r, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n" for r in its
        )
    return out


def with_file_hashes(manifest: dict, blobs: dict[str, bytes]) -> dict:
    manifest = dict(manifest)
    manifest["files"] = {
        name: {
            "sha256": hashlib.sha256(blob).hexdigest(),
            "bytes": len(blob),
            "rows": blob.count(b"\n"),
        }
        for name, blob in sorted(blobs.items())
    }
    return manifest


def manifest_bytes(manifest: dict) -> str:
    return json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def report(args, manifest: dict) -> None:
    counts = manifest["counts"]
    print(f"[build] 行 {counts['rows_read']} -> 记录 {counts['records']} -> 训练项 {counts['items']}")
    print(f"[build] 记录按分片 {counts['per_split']}")
    print(f"[build] 训练项按分片 {counts['items_per_split']}")
    print(f"[build] 决策点 {counts['per_decision_point']}")
    if counts["skipped"]:
        print(f"[build] 丢弃 {counts['skipped']}")
    for name in sorted(manifest["item_stats"]):
        st = manifest["item_stats"][name]
        if not st.get("n"):
            continue
        print(
            f"[build]   {name}: n={st['n']} 序列 p50={st['seq_p50']} p90={st['seq_p90']} max={st['seq_max']}"
            f" | state 保留 均值 {st['state_kept_mean']:.2f} 最少 {st['state_kept_min']:.2f}"
        )
    for line in manifest["warnings"]:
        print(f"[build][warn] {line}")
    print(f"[build] -> {os.path.abspath(args.out)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", default="dataset", help="导出目录（train/val/test.jsonl）")
    parser.add_argument("--out", default="finetune/out", help="训练项输出目录")
    parser.add_argument("--base", default="convaiinnovations/laya",
                        help="checkpoint 目录或 HF repo id（只用它的 tokenizer）")
    parser.add_argument("--subfolder", default=None, help="HF 仓库内的子目录（multilingual / typed-decisions）")
    parser.add_argument("--tokenizer", default=None, help="直接指定 tokenizer 目录（覆盖 --base）")
    parser.add_argument("--max-len", type=int, default=2048, help="序列总长（= 部署时的 max_len）")
    parser.add_argument("--head-max-len", type=int, default=320, help="题面 + 选项的 token 预算")
    parser.add_argument("--min-options", type=int, default=fd.DEFAULT_MIN_OPTIONS,
                        help="候选少于这个数的行不进训练集")
    parser.add_argument("--include-degenerate", action="store_true",
                        help="保留只有一个候选的行（softmax 恒为 1，无梯度）")
    parser.add_argument("--sources", default="human", help="human / agent / human,agent")
    parser.add_argument("--weight-human", type=float, default=fd.DEFAULT_WEIGHTS["human"])
    parser.add_argument("--weight-agent", type=float, default=fd.DEFAULT_WEIGHTS["agent"])
    parser.add_argument("--soft-mix", type=float, default=0.0,
                        help=">0 时把采集时基础模型的分布混进目标分布（0 = 纯人类 one-hot）")
    parser.add_argument("--dev-frac", type=float, default=0.15,
                        help="train 里按房间留出的 dev 比例（0 = 不留）")
    parser.add_argument("--dev-seed", type=int, default=20260927)
    parser.add_argument("--verify", action="store_true", help="只校验现有产物与重算结果是否逐字节一致")
    args = parser.parse_args(argv)

    from transformers import AutoTokenizer

    base = None
    tok_dir = args.tokenizer
    if tok_dir is None:
        base = fd.resolve_base_checkpoint(args.base, args.subfolder)
        tok_dir = os.path.join(base, "tokenizer")
    tok = AutoTokenizer.from_pretrained(tok_dir)

    rows = fd.load_dataset_rows(args.dataset)
    if not rows:
        print(f"[build] {args.dataset} 里没有 train/val/test.jsonl —— 先跑 spire_agent export", file=sys.stderr)
        return 1

    records, items, extra = build(rows, tok, args)
    manifest = {
        "generator": "finetune/build.py",
        "version": 1,
        "params": {
            "max_len": args.max_len,
            "head_max_len": args.head_max_len,
            "min_options": args.min_options,
            "include_degenerate": bool(args.include_degenerate),
            "sources": [s.strip() for s in args.sources.split(",") if s.strip()],
            "weights": {"human": args.weight_human, "agent": args.weight_agent},
            "soft_mix": args.soft_mix,
            "dev_frac": args.dev_frac,
            "dev_seed": args.dev_seed,
            "tokenizer": os.path.abspath(tok_dir),
            "base": os.path.abspath(base) if base else None,
        },
        "inputs": {
            f"{split}.jsonl": (
                fd.sha256_file(os.path.join(args.dataset, f"{split}.jsonl"))
                if os.path.exists(os.path.join(args.dataset, f"{split}.jsonl"))
                else None
            )
            for split in fd.DATASET_SPLITS
        },
        "counts": {
            "rows_read": len(rows),
            "records": sum(len(v) for v in records.values()),
            "items": sum(len(v) for v in items.values()),
            "per_split": {name: len(records[name]) for name in sorted(records)},
            "items_per_split": {name: len(items[name]) for name in sorted(items)},
            **extra["meta"],
        },
        "item_stats": {name: extra["stats"][name] for name in sorted(extra["stats"])},
        "warnings": warnings_for(args, records, extra),
    }
    manifest = with_file_hashes(manifest, render(records, items))

    if args.verify:
        ok = True
        for name, blob in sorted(render(records, items).items()):
            path = os.path.join(args.out, name)
            if not os.path.exists(path):
                print(f"[verify] MISSING {path}")
                ok = False
            elif open(path, "rb").read() != blob:
                print(f"[verify] DIFFERS {path}")
                ok = False
        path = os.path.join(args.out, "build_manifest.json")
        if not os.path.exists(path) or open(path, "r", encoding="utf-8").read() != manifest_bytes(manifest):
            print(f"[verify] DIFFERS {path}")
            ok = False
        print("[verify]", "OK（build 是确定性的）" if ok else "FAILED")
        return 0 if ok else 1

    os.makedirs(args.out, exist_ok=True)
    for name, blob in sorted(render(records, items).items()):
        with open(os.path.join(args.out, name), "wb") as handle:
            handle.write(blob)
    with open(os.path.join(args.out, "build_manifest.json"), "w", encoding="utf-8", newline="\n") as handle:
        handle.write(manifest_bytes(manifest))
    report(args, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

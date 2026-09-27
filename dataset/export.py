#!/usr/bin/env python
"""确定性导出微调数据集（见 docs/08-dataset.md）。

输入：`runs/<run_id>/decisions.jsonl`（已提交的决策行）。
带 `runs/<run_id>/EXCLUDED.txt` 的局整个跳过，原因记进 manifest 的 `excluded_runs`。
输出：`dataset/{manifest.json,train.jsonl,val.jsonl,test.jsonl}`。

三条硬约束：

1. **确定性**：同样的 runs/ 一定产出逐字节相同的文件（排序 + 固定键序 + 无时间戳）。
   `--verify` 就是用来守住这一点的：重算一遍并逐字节比对。
2. **切分按 run**：`split` 字段在采集时就写死在行里（`core.dataset.split_of` 是纯哈希
   函数），导出只做分组，**绝不重新随机切分**，否则同一局的状态会同时出现在 train 与 val。
3. **过滤不改原始记录**：被排除的行（post_sl 房间 / fallback / 未命中候选）只在导出时
   丢弃，并把丢弃计数写进 manifest —— 原始数据永远留着，可以随时改口径重导。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "packages" / "spire-core" / "src"))

from spire_core import dataset as ds  # noqa: E402
from spire_core.config import DatasetConfig  # noqa: E402

SPLIT_FILES = {
    ds.SPLIT_TRAIN: "train.jsonl",
    ds.SPLIT_VAL: "val.jsonl",
    ds.SPLIT_TEST: "test.jsonl",
}

EXCLUDE_MARKER = "EXCLUDED.txt"


def exclusion_reason(run_dir: Path) -> str | None:
    """`runs/<run_id>/EXCLUDED.txt` 存在时返回原因（首行），否则 None。

    用来隔离"已知被污染、但不想删原始记录"的整局数据（例如检测器误报导致某类
    决策被整批丢弃）。标记是输入树的一部分，所以导出依然是确定性的。
    """
    marker = run_dir / EXCLUDE_MARKER
    if not marker.is_file():
        return None
    try:
        text = marker.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    return text.splitlines()[0].strip() if text else "EXCLUDED.txt（未写原因）"


def load_rows(runs_dir: Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """按 run 目录名排序读出所有已提交行（顺序决定输出顺序，因此必须稳定）。"""
    rows: list[dict[str, Any]] = []
    excluded: dict[str, str] = {}
    if not runs_dir.is_dir():
        return rows, excluded
    for run_dir in sorted(p for p in runs_dir.iterdir() if p.is_dir()):
        reason = exclusion_reason(run_dir)
        if reason is not None:
            excluded[run_dir.name] = reason
            continue
        path = run_dir / "decisions.jsonl"
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    print(f"[export] skip malformed row in {path}", file=sys.stderr)
    return rows, excluded


def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(row.get("run_id", "")),
        int(row.get("seq", 0) or 0),
        str(row.get("row_id", "")),
    )


def serialize_row(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"))


def partition(
    rows: list[dict[str, Any]], cfg: DatasetConfig
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, int], dict[str, int]]:
    kept, dropped = ds.filter_rows(
        rows,
        exclude_post_sl_rooms=cfg.exclude_post_sl_rooms,
        exclude_fallback_rows=cfg.exclude_fallback_rows,
        exclude_unmatched=cfg.exclude_unmatched,
    )
    buckets: dict[str, list[dict[str, Any]]] = {s: [] for s in SPLIT_FILES}
    for row in sorted(kept, key=sort_key):
        split = str(row.get("split") or "") or ds.split_of(
            str(row.get("run_id", "")),
            split_seed=cfg.split_seed,
            val_ratio=cfg.val_ratio,
            test_ratio=cfg.test_ratio,
        )
        buckets.setdefault(split, []).append(row)
    sources: dict[str, int] = {}
    for row in kept:
        key = str(row.get("source", "unknown"))
        sources[key] = sources.get(key, 0) + 1
    return buckets, dropped, sources


def render(lines: list[str]) -> bytes:
    body = "\n".join(lines)
    return (body + "\n").encode("utf-8") if body else b""


def split_runs(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for row in rows:
        split = str(row.get("split", ""))
        out.setdefault(split, [])
        run_id = str(row.get("run_id", ""))
        if run_id not in out[split]:
            out[split].append(run_id)
    return {key: sorted(value) for key, value in sorted(out.items())}


def build_outputs(runs_dir: Path, cfg: DatasetConfig) -> tuple[dict[str, bytes], dict[str, Any]]:
    rows, excluded = load_rows(runs_dir)
    buckets, dropped, sources = partition(rows, cfg)
    files: dict[str, bytes] = {}
    for split, filename in SPLIT_FILES.items():
        files[filename] = render([serialize_row(r) for r in buckets.get(split, [])])
    manifest = {
        "generator": "dataset/export.py",
        "version": 1,
        "split_rule": {
            "by": "run_id",
            "hash": "sha256(split_seed:run_id)",
            "split_seed": cfg.split_seed,
            "val_ratio": cfg.val_ratio,
            "test_ratio": cfg.test_ratio,
            "note": "split 在采集时写死在行内；导出只做分组，不重新切分",
        },
        "filters": {
            "exclude_post_sl_rooms": cfg.exclude_post_sl_rooms,
            "exclude_fallback_rows": cfg.exclude_fallback_rows,
            "exclude_unmatched": cfg.exclude_unmatched,
        },
        "counts": {
            "total_rows": len(rows),
            "kept_rows": sum(len(v) for v in buckets.values()),
            "dropped": dropped,
            "per_split": {s: len(buckets.get(s, [])) for s in SPLIT_FILES},
            "per_source": sources,
        },
        "runs": split_runs(rows),
        "excluded_runs": excluded,
        "files": {
            name: {
                "sha256": hashlib.sha256(blob).hexdigest(),
                "bytes": len(blob),
                "rows": blob.count(b"\n"),
            }
            for name, blob in sorted(files.items())
        },
    }
    return files, manifest


def write_outputs(out_dir: Path, files: dict[str, bytes], manifest: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, blob in files.items():
        (out_dir / name).write_bytes(blob)
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def verify_outputs(out_dir: Path, files: dict[str, bytes], manifest: dict[str, Any]) -> bool:
    ok = True
    for name, blob in files.items():
        path = out_dir / name
        if not path.exists():
            print(f"[verify] MISSING {path}")
            ok = False
        elif path.read_bytes() != blob:
            print(f"[verify] DIFFERS {path}")
            ok = False
    path = out_dir / "manifest.json"
    expected = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != expected:
        print(f"[verify] DIFFERS {path}")
        ok = False
    return ok


def write_noul_expansion(out_dir: Path, files: dict[str, bytes]) -> None:
    """把 choice 行无损展开成 N 条 noul 行（免费得到均衡的二分类样本）。"""
    for name in ("train.jsonl", "val.jsonl", "test.jsonl"):
        blob = files.get(name)
        if not blob:
            continue
        rows = [json.loads(line) for line in blob.decode("utf-8").splitlines() if line.strip()]
        expanded: list[str] = []
        for row in rows:
            expanded.extend(serialize_row(r) for r in ds.expand_choice_to_noul(row))
        out = out_dir / name.replace(".jsonl", ".noul.jsonl")
        out.write_bytes(render(expanded))
        print(f"[export] noul -> {out} ({len(expanded)} rows)")


def export_main(
    *,
    runs_dir: Path = Path("runs"),
    out_dir: Path = Path("dataset"),
    verify: bool = False,
    expand_noul: bool = False,
    cfg: DatasetConfig | None = None,
) -> int:
    cfg = cfg or DatasetConfig()
    files, manifest = build_outputs(runs_dir, cfg)
    if verify:
        ok = verify_outputs(out_dir, files, manifest)
        print("[verify]", "OK（导出是确定性的）" if ok else "FAILED")
        return 0 if ok else 1
    write_outputs(out_dir, files, manifest)
    if expand_noul:
        write_noul_expansion(out_dir, files)
    counts = manifest["counts"]
    print(
        f"[export] kept={counts['kept_rows']} of {counts['total_rows']} "
        f"rows, dropped={counts['dropped']} -> {out_dir}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--runs", default="runs")
    parser.add_argument("--out", default="dataset")
    parser.add_argument("--verify", action="store_true", help="只校验现有导出是否与重算结果一致")
    parser.add_argument("--expand-noul", action="store_true", help="额外产出 *.noul.jsonl")
    parser.add_argument("--keep-post-sl", action="store_true", help="不排除 post_sl 房间")
    parser.add_argument("--keep-fallback", action="store_true", help="不排除 fallback 行")
    parser.add_argument("--keep-unmatched", action="store_true", help="不排除未命中候选的行")
    args = parser.parse_args(argv)

    cfg = DatasetConfig(
        exclude_post_sl_rooms=not args.keep_post_sl,
        exclude_fallback_rows=not args.keep_fallback,
        exclude_unmatched=not args.keep_unmatched,
    )
    return export_main(
        runs_dir=Path(args.runs),
        out_dir=Path(args.out),
        verify=args.verify,
        expand_noul=args.expand_noul,
        cfg=cfg,
    )


if __name__ == "__main__":
    raise SystemExit(main())

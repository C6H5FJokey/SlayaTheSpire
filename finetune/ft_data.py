"""数据集行 -> Laya 训练记录 / 训练项。

**纯逻辑 + 只读 IO**：不 import torch，也不 import laya。只有真正做分词的那几个函数
才会 import `laya.common`（而它只依赖 transformers，不依赖 torch）。

为什么单独一层：`dataset/*.jsonl` 是本项目的**数据契约**（见 docs/08-dataset.md），
而 Laya 的训练项是**模型契约**（`state` + `questions` + 目标分布）。这一层负责把前者
无损翻译成后者，并且**和推理走同一个函数**（`laya.common.build_sequence` /
`laya.common.render_options`），所以训练看到的序列与服务端看到的逐字节相同 ——
这也是训练与部署必须共用同一个 venv 的原因。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Iterable

SPLIT_TRAIN = "train"
SPLIT_VAL = "val"
SPLIT_TEST = "test"
SPLIT_DEV = "dev"

DATASET_SPLITS = (SPLIT_TRAIN, SPLIT_VAL, SPLIT_TEST)

# 标签强度 -> 训练权重。人类标签是模仿学习主料；agent 自博弈是弱标签（见 docs/08-dataset.md）。
DEFAULT_WEIGHTS = {"human": 1.0, "agent": 0.25}

DEFAULT_MIN_OPTIONS = 2

# 跳过原因（写进 build_manifest.json，便于统计"为什么这一行没进训练集"）
SKIP_SOURCE = "source_not_selected"
SKIP_FALLBACK = "agent_fallback"
SKIP_UNMATCHED = "unmatched"
SKIP_POST_SL = "post_sl_room"
SKIP_NO_LABEL = "no_label"
SKIP_LABEL_UNKNOWN = "label_not_in_options"
SKIP_TOO_FEW_OPTIONS = "too_few_options"
SKIP_NO_QUESTION = "no_question"
SKIP_MULTI_LABEL = "multi_label"


class SkipRecord(Exception):
    """该行不该进训练集。`args[0]` 是上表里的原因码。"""


# ----------------------------------------------------------------------------- IO


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def read_jsonl(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_jsonl(path: str | os.PathLike[str], rows: Iterable[dict[str, Any]]) -> int:
    """写 JSONL：确定性（无时间戳、固定顺序由调用方保证），返回行数。"""
    n = 0
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            n += 1
    return n


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
    return (str(row.get("run_id", "")), int(row.get("seq", 0) or 0), str(row.get("row_id", "")))


def load_dataset_rows(dataset_dir: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """读出 dataset/{train,val,test}.jsonl 并全局排序（顺序决定输出顺序，必须稳定）。"""
    rows: list[dict[str, Any]] = []
    for split in DATASET_SPLITS:
        path = os.path.join(str(dataset_dir), f"{split}.jsonl")
        if os.path.exists(path):
            rows.extend(read_jsonl(path))
    rows.sort(key=row_sort_key)
    return rows


# ----------------------------------------------------------------------------- checkpoint 定位


def find_cached_checkpoint(
    repo: str = "convaiinnovations/laya",
    subfolder: str | None = None,
    *,
    search_roots: Iterable[str] | None = None,
) -> str | None:
    """在 HuggingFace 缓存里找已下好的 checkpoint 目录（**离线**，不发网络请求）。

    返回含 `rl_agent_config.json` 的目录；找不到返回 None。
    """
    slug = "models--" + repo.replace("/", "--")
    if search_roots is None:
        roots = []
        if os.environ.get("HF_HOME"):
            roots.append(os.environ["HF_HOME"])
        roots.append(os.path.join(os.getcwd(), ".cache", "huggingface"))
        roots.append(os.path.join(os.path.expanduser("~"), ".cache", "huggingface"))
    else:
        roots = list(search_roots)
    for root in roots:
        snapshots = os.path.join(root, "hub", slug, "snapshots")
        if not os.path.isdir(snapshots):
            continue
        for rev in sorted(os.listdir(snapshots), reverse=True):
            base = os.path.join(snapshots, rev)
            if subfolder:
                base = os.path.join(base, subfolder)
            if os.path.isfile(os.path.join(base, "rl_agent_config.json")):
                return base
    return None


def resolve_base_checkpoint(spec: str, subfolder: str | None = None) -> str:
    """把 `--base` 解析成一个本地 checkpoint 目录。

    - 已经是本地目录 -> 原样返回（含 `rl_agent_config.json` 才算数）；
    - 否则当成 HF repo id，去缓存里找；
    - 再找不到就抛错，并给出下载命令（**不偷偷联网**：远端机器上先离线把仓库下好）。
    """
    if os.path.isdir(spec):
        if not os.path.isfile(os.path.join(spec, "rl_agent_config.json")):
            raise SystemExit(
                f"{spec} 不是 Laya checkpoint（缺 rl_agent_config.json）。"
                "目录里应有 rl_agent_config.json / model.safetensors / encoder/ / tokenizer/。"
            )
        return spec
    found = find_cached_checkpoint(spec, subfolder)
    if found:
        return found
    raise SystemExit(
        f"找不到 checkpoint {spec!r}（subfolder={subfolder!r}）。\n"
        "  本地有 GPU 的机器上先下好，再把整个仓库目录拷过去：\n"
        '    python -c "from huggingface_hub import snapshot_download; snapshot_download(%r)"\n'
        "  或者用 --base 直接指一个目录。" % (spec,)
    )


# ----------------------------------------------------------------------------- 行 -> 记录


def room_key_of(row: dict[str, Any]) -> str:
    """房间标识（与 `spire_core.dataset.room_key` 同构，用于房间级留出）。"""
    room = row.get("room") or {}
    return "a%s_f%s_n%s_c%s" % (
        room.get("act", 0),
        room.get("floor", 0),
        room.get("node", 0),
        room.get("combat_instance", 1),
    )


def question_option_count(q: dict[str, Any]) -> int:
    t = q.get("type")
    if t == "choice":
        return len(q.get("criteria") or {})
    if t == "score":
        criteria = q.get("criteria") or []
        return len(criteria)
    return 2  # noul 永远是 [false, true]


def _noul_index(label: Any) -> int | None:
    """noul 的正负样本落在 [false, true] 的第几位。"""
    if isinstance(label, bool):
        return 1 if label else 0
    if isinstance(label, (int, float)) and not isinstance(label, bool):
        return 1 if int(label) else 0
    text = str(label).strip().lower()
    if text in ("true", "yes", "1", "t"):
        return 1
    if text in ("false", "no", "0", "f"):
        return 0
    return None


def question_to_internal(q: dict[str, Any], label: Any) -> tuple[dict[str, Any], list[str], int]:
    """Jev 形态的题 -> Laya 内部形态 `{t, ins, crit}` + 选项 key 列表 + 正确项下标。

    题面（state/questions）**原样保留**：`ins` 只做"非字符串则 JSON 化"这一件事，
    与 `laya.agent._to_internal` 完全一致，否则训练与推理的序列会分叉。
    """
    # 多选类决策点（select_card_must_k）落盘的是"这次点的那一张"，记成单元素列表。
    # 真正的多选（一次动作选多张）没法用一道 choice 表达，宁可跳过也不给错的答案空间。
    if isinstance(label, (list, tuple)):
        if len(label) != 1:
            raise SkipRecord(SKIP_MULTI_LABEL)
        label = label[0]

    qtype = q.get("type")
    if qtype not in ("choice", "score", "noul"):
        raise SkipRecord(SKIP_NO_QUESTION)
    instructions = q.get("instructions")
    ins = instructions if isinstance(instructions, str) else json.dumps(instructions, ensure_ascii=False)

    if qtype == "choice":
        criteria = q.get("criteria")
        if not isinstance(criteria, dict) or not criteria:
            raise SkipRecord(SKIP_NO_QUESTION)
        keys = list(criteria.keys())
        if label is None:
            raise SkipRecord(SKIP_NO_LABEL)
        if label not in criteria:
            raise SkipRecord(SKIP_LABEL_UNKNOWN)
        return {"t": "choice", "ins": ins, "crit": criteria}, keys, keys.index(label)

    if qtype == "score":
        criteria = q.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise SkipRecord(SKIP_NO_QUESTION)
        try:
            index = int(label)
        except (TypeError, ValueError):
            raise SkipRecord(SKIP_LABEL_UNKNOWN) from None
        if not 0 <= index < len(criteria):
            raise SkipRecord(SKIP_LABEL_UNKNOWN)
        return {"t": "score", "ins": ins, "crit": criteria}, [str(i) for i in range(len(criteria))], index

    index = _noul_index(label)
    if index is None:
        raise SkipRecord(SKIP_LABEL_UNKNOWN)
    crit = q.get("criteria")
    return {"t": "noul", "ins": ins, "crit": crit if isinstance(crit, dict) else None}, ["false", "true"], index


def _recorded_answers(row: dict[str, Any]) -> dict[str, Any]:
    """采集时**基础 checkpoint** 的答案（`observe_human.also_query_model` 落下的）。

    两个用途：`--soft-mix` 的软目标来源；eval 里"微调前 vs 微调后"的对照行。
    """
    out: dict[str, Any] = {}
    for qid, ans in (row.get("answers") or {}).items():
        if not isinstance(ans, dict):
            continue
        entry: dict[str, Any] = {}
        for key in ("choice", "score", "noul", "answer_confidence"):
            if key in ans:
                entry[key] = ans[key]
        if isinstance(ans.get("probabilities"), dict):
            entry["probabilities"] = {str(k): float(v) for k, v in ans["probabilities"].items()}
        if entry:
            out[qid] = entry
    return out


def skip_reason(
    row: dict[str, Any],
    *,
    sources: Iterable[str],
    min_options: int,
    include_degenerate: bool,
) -> str | None:
    """这一行为什么不能进训练集（None = 可以）。"""
    if row.get("source") not in set(sources):
        return SKIP_SOURCE
    meta = row.get("meta") or {}
    if meta.get("agent_fallback"):
        return SKIP_FALLBACK
    if meta.get("matched") is False:
        return SKIP_UNMATCHED
    if (row.get("room") or {}).get("post_sl"):
        return SKIP_POST_SL
    labels = row.get("labels") or {}
    if not labels or all(v is None for v in labels.values()):
        return SKIP_NO_LABEL
    if not include_degenerate:
        counts = [question_option_count(q) for q in (row.get("questions") or {}).values()]
        if counts and min(counts) < min_options:
            return SKIP_TOO_FEW_OPTIONS
    return None


def row_to_record(
    row: dict[str, Any],
    *,
    split: str,
    weight: float,
    soft_mix: float = 0.0,
) -> dict[str, Any]:
    """一行数据集 -> 一条训练记录（可读、可追溯、可复现）。

    `soft_mix = x` 时目标分布 = `(1-x) * one_hot + x * 采集时基础模型的分布`；
    分布不可用/对不齐 key 时**静默退回 one-hot**（并记在 `gold[qid].soft_mix_used`）。
    """
    questions = row.get("questions") or {}
    labels = row.get("labels") or {}
    recorded = _recorded_answers(row)
    gold: dict[str, Any] = {}
    for qid, q in questions.items():
        internal, options, index = question_to_internal(q, labels.get(qid))
        entry = {
            "type": internal["t"],
            "ins": internal["ins"],
            "crit": internal["crit"],
            "y": index,
            "key": labels.get(qid),
            "options": options,
            "option_count": len(options),
        }
        if soft_mix > 0.0:
            probs = (recorded.get(qid) or {}).get("probabilities")
            if isinstance(probs, dict) and set(probs) == set(options):
                total = sum(float(probs[k]) for k in options)
                if total > 0:
                    entry["soft"] = [
                        (1.0 - soft_mix) * (1.0 if i == index else 0.0)
                        + soft_mix * float(probs[key]) / total
                        for i, key in enumerate(options)
                    ]
                    entry["soft_mix_used"] = soft_mix
        gold[qid] = entry
    if not gold:
        raise SkipRecord(SKIP_NO_QUESTION)
    room = row.get("room") or {}
    context = row.get("context") or {}
    return {
        "record_id": row.get("row_id"),
        "run_id": row.get("run_id"),
        "seq": int(row.get("seq", 0) or 0),
        "decision_point": row.get("decision_point"),
        "room_key": room_key_of(row),
        "split": split,
        "source": row.get("source"),
        "weight": float(weight),
        "character": context.get("character"),
        "ascension": context.get("ascension"),
        "floor": room.get("floor"),
        "state": row.get("state"),
        "questions": questions,
        "gold": gold,
        "recorded": {qid: recorded[qid] for qid in questions if qid in recorded},
    }


def assign_dev_split(
    records: list[dict[str, Any]],
    *,
    frac: float,
    seed: int,
) -> dict[str, str]:
    """train 行里再切一份 `dev`（**按房间**，同一房间的行不拆开）。

    这是**训练期的开发留出**，用于"这一版到底有没有变好"：它不是 docs/08-dataset.md 里
    按 run 切分的 val/test（那份是数据契约，行内的 `split` 字段说了算）。当 val/test 非空
    时优先用它们；只有 train 时，`dev` 是唯一能给出不掺训练数据的数字的来源。
    """
    if frac <= 0:
        return {}
    assigned: dict[str, str] = {}
    for record in records:
        if record["split"] != SPLIT_TRAIN:
            continue
        digest = hashlib.sha256(f"{seed}:{record['room_key']}".encode("utf-8")).digest()
        if int.from_bytes(digest[:4], "big") / 2**32 < frac:
            assigned[record["record_id"]] = SPLIT_DEV
    return assigned


# ----------------------------------------------------------------------------- 记录 -> 训练项


def make_items(
    record: dict[str, Any],
    tok: Any,
    *,
    max_len: int,
    head_max_len: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """一条记录 -> 若干训练项（一题一项）。需要 tokenizer（`laya` + transformers）。

    返回 (items, stats)。stats 里数的是"这一条记录发生了什么"：
    `state_tokens` / `state_kept` 用来回答"模型有没有看到整份 state"。
    """
    from laya.common import build_sequence, render_options, serialize_state

    stats = {"options_truncated": 0, "state_truncated": 0, "questions": 0}
    state = record["state"]
    raw_state_tokens = len(tok(serialize_state(state), add_special_tokens=False)["input_ids"])
    items: list[dict[str, Any]] = []
    for qid, gold in record["gold"].items():
        internal = {"t": gold["type"], "ins": gold["ins"], "crit": gold["crit"]}
        ids, markers = build_sequence(tok, state, internal, max_len, head_max_len)
        stats["questions"] += 1
        if len(markers) != len(render_options(internal)):
            # 选项没塞进 head_max_len：宁可不训练，也不训练一个被截断的答案空间
            stats["options_truncated"] += 1
            continue
        # 头部长度的算法与 build_sequence 内部一致：ids = [CLS]+head+[SEP]+opts+[SEP]，state 占 room 个
        head_len = len(build_sequence(tok, "", internal, max_len, head_max_len)[0]) - 1
        state_kept = min(raw_state_tokens, max(0, max_len - head_len - 1))
        if state_kept < raw_state_tokens:
            stats["state_truncated"] += 1
        target = gold.get("soft") or [1.0 if i == gold["y"] else 0.0 for i in range(gold["option_count"])]
        items.append(
            {
                "record_id": record["record_id"],
                "qid": qid,
                "ids": ids,
                "markers": markers,
                "qtype": gold["type"],
                "target": [round(float(v), 6) for v in target],
                "label": int(gold["y"]),
                "weight": float(record["weight"]),
                "decision_point": record["decision_point"],
                "room_key": record["room_key"],
                "split": record["split"],
                "option_count": int(gold["option_count"]),
                "seq_len": len(ids),
                "state_tokens": raw_state_tokens,
                "state_kept": state_kept,
                "source": record["source"],
            }
        )
    return items, stats


def to_jev_questions(record: dict[str, Any]) -> dict[str, Any]:
    """还原成发往 Laya 的 `questions`（逐字节等于采集时那一份）。"""
    return record["questions"]


def item_length_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    if not items:
        return {"n": 0}
    seq = sorted(int(it["seq_len"]) for it in items)
    kept = [float(it["state_kept"]) / max(1, int(it["state_tokens"])) for it in items]
    return {
        "n": len(items),
        "state_tokens_total": sum(int(it["state_tokens"]) for it in items),
        "state_kept_total": sum(int(it["state_kept"]) for it in items),
        "seq_p50": seq[len(seq) // 2],
        "seq_p90": seq[min(len(seq) - 1, int(len(seq) * 0.9))],
        "seq_max": seq[-1],
        "state_kept_mean": round(sum(kept) / len(kept), 4),
        "state_kept_min": round(min(kept), 4),
    }

"""微调脚本的测试夹具（`docs/13-finetune.md`）。

分两档：

- `test_ft_data.py` 是**纯逻辑**，`.venv`（只有 httpx + pytest）里也能跑；
- 其余用例要 torch + laya + tokenizer，只能在 `.venv-laya` 里跑（`importorskip` 会跳过）。

`tiny_checkpoint` 造一份**几百 KB 的迷你 Laya checkpoint**：编码器配置照抄官方
ModernBERT-large（同一个 tokenizer，2 层 / hidden 64），权重随机初始化。于是
build -> train -> eval -> serve 四条路径都能真跑起来，CPU 上几秒钟出结果 ——
既能验证代码，又不需要 GPU、不需要官方权重（只有 tokenizer 得在 HF 缓存里）。
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import uuid

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FINETUNE = os.path.dirname(HERE)
REPO = pathlib.Path(FINETUNE).parent
if FINETUNE not in sys.path:
    sys.path.insert(0, FINETUNE)

import ft_data as fd  # noqa: E402

TINY_MAX_LEN = 256
TINY_HEAD_MAX_LEN = 96

# 与 `packages/spire-agent/tests/conftest.py` 同一个理由：本沙箱的系统临时目录**可建不可读**
# （`pytest-of-<user>` 建得出来、list 不了），所以把测试用的临时目录放在仓库内 `.pytest-tmp/`。
TMP_ROOT = REPO / ".pytest-tmp"


@pytest.fixture
def tmp_path():
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    path = TMP_ROOT / uuid.uuid4().hex
    path.mkdir()
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def tmp_dir():
    """会话级临时目录工厂：`tmp_dir("name") -> str`（同一个理由，见上）。"""

    def make(name: str) -> str:
        TMP_ROOT.mkdir(parents=True, exist_ok=True)
        path = TMP_ROOT / f"finetune-{name}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    return make


def base_snapshot() -> str:
    base = fd.find_cached_checkpoint()
    if not base:
        pytest.skip("没有本地 Laya checkpoint（先跑 tools/setup_laya.ps1 下好 convaiinnovations/laya）")
    return base


@pytest.fixture(scope="session")
def tokenizer_dir() -> str:
    pytest.importorskip("transformers")
    return os.path.join(base_snapshot(), "tokenizer")


@pytest.fixture(scope="session")
def tiny_checkpoint(tmp_dir) -> str:
    """迷你 checkpoint：结构与官方一致，参数量小两个数量级。"""
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    pytest.importorskip("safetensors")
    from laya.common import build_model
    from safetensors.torch import save_file

    base = base_snapshot()
    dst = tmp_dir("checkpoint")
    with open(os.path.join(base, "rl_agent_config.json"), encoding="utf-8") as handle:
        cfg = json.load(handle)
    cfg.update({"max_len": TINY_MAX_LEN, "head_max_len": TINY_HEAD_MAX_LEN, "model_name": "laya-tiny"})
    cfg["temperature"] = [1.0, 1.0, 1.0]
    # 故意留一个越界的桶温度（官方 english 的 choice:11+ 就是这个值）：训练必须清掉它，
    # 否则它在推理端优先级更高，会静默顶掉新拟合的题型温度。
    cfg["temperature_by_options"] = {"choice:11+": 0.10058280825614929}

    shutil.copytree(os.path.join(base, "tokenizer"), os.path.join(dst, "tokenizer"))
    os.makedirs(os.path.join(dst, "encoder"))
    with open(os.path.join(base, "encoder", "config.json"), encoding="utf-8") as handle:
        ecfg = json.load(handle)
    ecfg.update({
        "hidden_size": 64,
        "num_hidden_layers": 2,
        "num_attention_heads": 2,
        "intermediate_size": 128,
        "layer_types": ["full_attention", "sliding_attention"],
    })
    with open(os.path.join(dst, "encoder", "config.json"), "w", encoding="utf-8") as handle:
        json.dump(ecfg, handle, indent=1)
    with open(os.path.join(dst, "rl_agent_config.json"), "w", encoding="utf-8") as handle:
        json.dump(cfg, handle, ensure_ascii=False, indent=1)
    model = build_model(cfg, encoder_dir=os.path.join(dst, "encoder"))
    save_file({key: value.contiguous() for key, value in model.state_dict().items()},
              os.path.join(dst, "model.safetensors"))
    return dst


def make_row(row_id: str, seq: int, decision_point: str, state: dict, qid: str, question: dict,
             label, *, room: dict | None = None, source: str = "human",
             probabilities: dict | None = None, meta: dict | None = None) -> dict:
    """一条最小可用的数据集行（字段取自 `dataset/*.jsonl` 的真实形态）。"""
    room = dict(room or {"act": 1, "floor": 1, "node": 0, "type": "COMBAT", "combat_instance": 1,
                         "post_sl": False})
    answer = None
    if probabilities is not None:
        answer = {"type": question["type"], "probabilities": probabilities,
                  "answer_confidence": max(probabilities.values())}
    return {
        "row_id": row_id,
        "run_id": row_id.split("#")[0],
        "seq": seq,
        "split": "train",
        "decision_point": decision_point,
        "source": source,
        "state": state,
        "questions": {qid: question},
        "labels": {qid: label},
        "candidate_ids": list(label) if isinstance(label, list) else [label],
        "chosen": label,
        "chosen_ids": list(label) if isinstance(label, list) else [label],
        "context": {"character": "IRONCLAD", "ascension": 0, "fairness_mode": "strict"},
        "room": room,
        "outcome": {"run_won": None, "hp_after": None, "hp_delta_room": None, "floor_reached": 1},
        "meta": {"label_source": source, "agent_fallback": False, "matched": True,
                 "model_answer": None, "model_confidence": None, "agreement": None,
                 "checkpoint": "english", "latency_ms": 10, "usage": {"input_tokens": 100,
                                                                     "output_tokens": 0},
                 **(meta or {})},
        "answers": {qid: answer} if answer else {},
        "k_min": 0,
        "k_max": 0,
    }


def tiny_state(floor: int = 1, hp: int = 70) -> dict:
    return {
        "mode": "combat",
        "act": 1,
        "floor": floor,
        "room_type": "Combat",
        "ascension": 0,
        "player": {"character": "IRONCLAD", "hp": hp, "max_hp": 80, "block": 0, "energy": 3,
                   "gold": 99, "powers": [], "relics": [], "potions": [], "potion_slots": 3},
        "combat": {"turn": 1, "hand": ["Strike", "Defend", "Bash"], "monsters": [
            {"id": "m0", "name": "Jaw Worm", "hp": 44, "max_hp": 44, "block": 0, "intent": "ATTACK"}]},
    }


@pytest.fixture(scope="session")
def tiny_rows() -> list[dict]:
    """6 条真实形态的行，覆盖 choice / score / noul、退化候选、列表标签、以及各种跳过原因。"""
    rows = [
        make_row("run-t#c1#t0#s1", 1, "combat_play", tiny_state(1), "q_action",
                 {"type": "choice", "instructions": "Pick the best action.",
                  "criteria": {"play:h0": "Strike.", "play:h1": "Defend.", "end_turn": "End turn."}},
                 "play:h1", probabilities={"play:h0": 0.2, "play:h1": 0.5, "end_turn": 0.3}),
        make_row("run-t#c1#t0#s2", 2, "card_reward", tiny_state(1), "q_reward",
                 {"type": "choice", "instructions": "Take a card or skip.",
                  "criteria": {"card:0": "Bash.", "card:1": "Cleave.", "skip": "Skip."}},
                 "card:1", probabilities={"card:0": 0.1, "card:1": 0.2, "skip": 0.7}),
        make_row("run-t#c1#t1#s3", 3, "select_card_must_k", tiny_state(2), "q_pick",
                 {"type": "choice", "instructions": "Choose one card.",
                  "criteria": {"card:deck:0": "Strike.", "card:deck:1": "Defend."}},
                 ["card:deck:1"], probabilities={"card:deck:0": 0.4, "card:deck:1": 0.6}),
        make_row("run-t#c1#t2#s4", 4, "map_node", tiny_state(3), "q_map",
                 {"type": "choice", "instructions": "Choose a node.",
                  "criteria": {"n0_0": "Monster.", "n0_1": "Unknown.", "n0_2": "Elite."}},
                 "n0_2", room={"act": 1, "floor": 3, "node": 2, "type": "MAP", "combat_instance": 1,
                               "post_sl": False}),
        make_row("run-t#c1#t2#s5", 5, "rest_site", tiny_state(4), "q_rest",
                 {"type": "score", "instructions": "Rate this option.",
                  "criteria": ["bad", "ok", "good"]}, 2,
                 room={"act": 1, "floor": 4, "node": 3, "type": "REST", "combat_instance": 1,
                       "post_sl": False}),
        make_row("run-t#c1#t3#s6", 6, "shop", tiny_state(5), "q_shop",
                 {"type": "noul", "instructions": "Is buying this worth it?",
                  "criteria": {"false": "no", "true": "yes"}}, "true",
                 room={"act": 1, "floor": 5, "node": 4, "type": "SHOP", "combat_instance": 1,
                       "post_sl": False}),
    ]
    return rows


@pytest.fixture(scope="session")
def skipped_rows() -> list[dict]:
    """每一行都该被 `skip_reason` / `row_to_record` 拒掉，用来验证过滤口径。"""
    choice = {"type": "choice", "instructions": "Pick.",
              "criteria": {"a": "A.", "b": "B."}}
    rows = [
        make_row("run-s#c1#t0#s1", 1, "combat_play", tiny_state(), "q", choice, "a", source="agent"),
        make_row("run-s#c1#t0#s2", 2, "combat_play", tiny_state(), "q", choice, "a",
                 meta={"agent_fallback": True}),
        make_row("run-s#c1#t0#s3", 3, "combat_play", tiny_state(), "q", choice, "a",
                 meta={"matched": False}),
        make_row("run-s#c1#t0#s4", 4, "combat_play", tiny_state(), "q", choice, "a",
                 room={"act": 1, "floor": 1, "node": 0, "type": "COMBAT", "combat_instance": 1,
                       "post_sl": True}),
        make_row("run-s#c1#t0#s5", 5, "combat_play", tiny_state(), "q", choice, None),
        make_row("run-s#c1#t0#s6", 6, "combat_play", tiny_state(), "q", choice, "missing"),
        make_row("run-s#c1#t0#s7", 7, "combat_play", tiny_state(), "q", choice, ["a", "b"]),
        make_row("run-s#c1#t0#s8", 8, "combat_play", tiny_state(), "q",
                 {"type": "choice", "instructions": "Pick.", "criteria": {"a": "A."}}, "a"),
    ]
    return rows


@pytest.fixture(scope="session")
def tiny_dataset_dir(tmp_dir, tiny_rows, skipped_rows) -> str:
    """把上面的行写成 `dataset/{train,val,test}.jsonl`。"""
    dst = tmp_dir("dataset")
    fd.write_jsonl(os.path.join(dst, "train.jsonl"), tiny_rows + skipped_rows)
    for split in ("val", "test"):
        fd.write_jsonl(os.path.join(dst, f"{split}.jsonl"), [])
    return dst


@pytest.fixture(scope="session")
def tiny_build(tmp_dir, tiny_dataset_dir, tiny_checkpoint) -> str:
    """跑一次真的 `build.py`（用迷你 checkpoint 的 tokenizer），返回产物目录。"""
    pytest.importorskip("torch")
    import build as builder

    out = tmp_dir("build")
    code = builder.main([
        "--dataset", tiny_dataset_dir, "--out", out, "--base", tiny_checkpoint,
        "--max-len", str(TINY_MAX_LEN), "--head-max-len", str(TINY_HEAD_MAX_LEN),
        "--dev-frac", "0.34", "--dev-seed", "7",
    ])
    assert code == 0
    return out

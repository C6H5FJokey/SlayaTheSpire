"""build -> train -> eval 的小闭环：用迷你 checkpoint 在 CPU 上真跑一遍。

不进 GPU、不要官方权重（只有 tokenizer 来自 HF 缓存），但走的是**和远端训练完全一样的
代码路径**：`build.py` 的翻译规则、`train.py` 的 RLCD + 校准、`eval.py` 的部署口径概率。
"""

from __future__ import annotations

import json
import os

import pytest

import ft_data as fd

EXPECTED_SKIPS = {
    fd.SKIP_SOURCE: 1,          # source=agent
    fd.SKIP_FALLBACK: 1,        # agent_fallback
    fd.SKIP_UNMATCHED: 1,       # matched=false
    fd.SKIP_POST_SL: 1,         # 读档后重记的房间
    fd.SKIP_NO_LABEL: 1,        # 没有标签
    fd.SKIP_LABEL_UNKNOWN: 1,   # 标签不在候选里
    fd.SKIP_MULTI_LABEL: 1,     # 一次选了多张，没法用一道 choice 表达
    fd.SKIP_TOO_FEW_OPTIONS: 1, # 只有一个候选
}


def read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def test_build_translates_rows_and_reports_why_it_dropped_others(tiny_build, tiny_rows, skipped_rows):
    manifest = read_json(os.path.join(tiny_build, "build_manifest.json"))
    counts = manifest["counts"]
    assert counts["rows_read"] == len(tiny_rows) + len(skipped_rows)
    assert counts["records"] == len(tiny_rows)
    assert counts["skipped"] == EXPECTED_SKIPS
    assert counts["per_source"] == {"human": len(tiny_rows)}
    assert sum(counts["items_per_split"].values()) == len(tiny_rows)
    # train 里切出了 dev（房间级留出），两者加起来才是全部记录
    assert counts["per_split"].get("dev", 0) > 0
    assert counts["per_split"]["train"] + counts["per_split"]["dev"] == len(tiny_rows)
    for info in manifest["files"].values():
        assert len(info["sha256"]) == 64 and info["rows"] > 0


def test_build_records_keep_the_questions_verbatim(tiny_build, tiny_rows):
    records = fd.read_jsonl(os.path.join(tiny_build, "train_records.jsonl"))
    records += fd.read_jsonl(os.path.join(tiny_build, "dev_records.jsonl"))
    by_id = {record["record_id"]: record for record in records}
    for row in tiny_rows:
        record = by_id[row["row_id"]]
        assert record["questions"] == row["questions"]      # 逐字节等于采集时那一份
        assert record["state"] == row["state"]
        qid = next(iter(row["questions"]))
        question = row["questions"][qid]
        gold = record["gold"][qid]
        assert len(gold["options"]) == fd.question_option_count(question)
        if question["type"] == "choice":
            # choice 的选项 key 顺序 = criteria 的书写顺序（结果要能直接对回游戏动作）
            assert gold["options"] == list(question["criteria"])
            # 多选类决策点落盘的是单元素列表，记录里记的是解开后的那一个
            label = row["labels"][qid]
            assert gold["options"][gold["y"]] == (label[0] if isinstance(label, list) else label)
        elif question["type"] == "score":
            assert gold["options"] == [str(i) for i in range(len(question["criteria"]))]
        else:
            assert gold["options"] == ["false", "true"]


def test_build_items_carry_the_contract_train_and_serve_need(tiny_build):
    items = fd.read_jsonl(os.path.join(tiny_build, "train_items.jsonl"))
    assert items
    for item in items:
        assert item["ids"] and item["markers"]
        assert len(item["markers"]) == item["option_count"] == len(item["target"])
        assert 0 <= item["label"] < item["option_count"]
        assert item["weight"] > 0
        assert item["seq_len"] == len(item["ids"]) <= 256
        assert pytest.approx(sum(item["target"])) == 1.0
        assert item["state_kept"] <= item["state_tokens"]


def test_build_verify_confirms_determinism(tiny_build, tiny_dataset_dir, tiny_checkpoint):
    import build as builder

    code = builder.main([
        "--dataset", tiny_dataset_dir, "--out", tiny_build, "--base", tiny_checkpoint,
        "--max-len", "256", "--head-max-len", "96", "--dev-frac", "0.34", "--dev-seed", "7",
        "--verify",
    ])
    assert code == 0


def test_train_calibrates_and_writes_a_deployable_checkpoint(tiny_build, tiny_checkpoint, tmp_dir):
    import laya

    import train as trainer

    out = tmp_dir("train")
    code = trainer.main([
        "--items", os.path.join(tiny_build, "train_items.jsonl"),
        "--base", tiny_checkpoint,
        "--out", out,
        "--epochs", "2",
        "--save-every", "1",              # 逼出续跑点这条路径（Windows 上曾经在这里炸过）
        "--calib-frac", "0.5", "--min-calib", "1", "--min-bucket-n", "1",
        "--max-len", "256", "--head-max-len", "96",
        "--device", "cpu", "--seed", "5", "--log-every", "1",
    ])
    assert code == 0

    for name in ("rl_agent_config.json", "model.safetensors", "finetune_report.json"):
        assert os.path.isfile(os.path.join(out, name))
    assert os.path.isdir(os.path.join(out, "encoder")) and os.path.isdir(os.path.join(out, "tokenizer"))
    assert not os.path.exists(os.path.join(out, "resume.pt"))     # 收尾要把续跑点清掉

    cfg = read_json(os.path.join(out, "rl_agent_config.json"))
    assert cfg["fine_tuned"] is True
    assert len(cfg["temperature"]) == 3
    assert all(0.5 <= t <= 5.0 for t in cfg["temperature"])
    # 基座自带的越界桶温度必须被清掉（它在推理端优先级高于题型温度，会静默顶掉拟合值）
    assert cfg["temperature_by_options"]["choice:11+"] != 0.10058280825614929
    assert all(0.5 <= t <= 5.0 for t in cfg["temperature_by_options"].values())

    report = read_json(os.path.join(out, "finetune_report.json"))
    assert report["items"]["train_items"] > 0 and report["items"]["calibration_items"] > 0
    assert report["calibration"], "校准报告不该为空"
    assert report["environment"]["torch"] and report["base_checkpoint"] == tiny_checkpoint

    # 产物就是一份普通 Laya checkpoint：部署侧 `laya.load` 直接能读
    with laya.load(out, device="cpu") as agent:
        answers = agent.system_one("a small state", {"q": {
            "type": "choice", "instructions": "Pick.", "criteria": {"a": "A.", "b": "B."}}})
        assert answers["answers"]["q"]["choice"] in ("a", "b")
        assert 0.0 <= answers["answers"]["q"]["answer_confidence"] <= 1.0


def test_train_refuses_items_built_with_a_different_max_len(tiny_build, tiny_checkpoint, tmp_dir):
    import train as trainer

    with pytest.raises(SystemExit) as excinfo:
        trainer.main([
            "--items", os.path.join(tiny_build, "train_items.jsonl"),
            "--base", tiny_checkpoint, "--out", tmp_dir("train-mismatch"),
            "--max-len", "512", "--device", "cpu",
        ])
    assert "build" in str(excinfo.value)


def test_eval_reports_calibration_and_accuracy(tiny_build, tiny_checkpoint, tmp_dir):
    import eval as evaluator

    def run(records: str, name: str, *extra: str) -> dict:
        path = os.path.join(tmp_dir("eval"), f"{name}.json")
        code = evaluator.main([
            "--records", os.path.join(tiny_build, records),
            "--device", "cpu", "--json", path, *extra,
        ])
        assert code == 0
        return read_json(path)

    payload = run("dev_records.jsonl", "dev", "--checkpoint", "recorded",
                  "--checkpoint", "base=" + tiny_checkpoint)
    assert payload["max_len"] == 256 and payload["head_max_len"] == 96
    assert payload["records"]["n"] > 0 and len(payload["records"]["sha256"]) == 64
    assert set(payload["variants"]) == {"recorded", "base"}
    for variant in payload["variants"].values():
        if variant["n"] == 0:
            # dev 只有一两条记录，而"采集时基座答案"只在开了 also_query_model 的行上才有
            assert variant["skipped"], "n=0 时必须说明跳过了什么"
            continue
        assert 0.0 <= variant["accuracy"] <= 1.0
        assert 0.0 <= variant["ece"] <= 1.0
        assert variant["mean_nll"] > 0
        assert variant["by_decision_point"] and variant["by_bucket"] and variant["by_qtype"]
    assert payload["variants"]["base"]["n"] > 0
    assert "mean_nll_t1" in payload["variants"]["base"]         # 有 logits 才谈得上"温度这一层"
    assert "mean_nll_t1" not in payload["variants"]["recorded"]  # 只有概率，没有 logits

    # 训练片上记录更多，`recorded` 这条"不占显存"的路径一定能算出数来
    payload = run("train_records.jsonl", "train", "--checkpoint", "recorded",
                  "--checkpoint", "base=" + tiny_checkpoint)
    recorded = payload["variants"]["recorded"]
    assert recorded["n"] > 0
    assert "agreement_with_recorded" not in recorded        # 自己跟自己没得比
    assert payload["variants"]["base"]["agreement_n"] > 0   # 基座与"采集时基座"的对照也有数


def test_eval_needs_a_manifest_to_pick_the_sequence_length(tmp_path, tiny_build):
    """少了 build_manifest.json 就必须显式给长度，而不是猜一个。"""
    import eval as evaluator

    lone = tmp_path / "records.jsonl"
    fd.write_jsonl(lone, fd.read_jsonl(os.path.join(tiny_build, "dev_records.jsonl")))
    with pytest.raises(SystemExit) as excinfo:
        evaluator.main(["--records", str(lone), "--checkpoint", "recorded"])
    assert "--max-len" in str(excinfo.value)

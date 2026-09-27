"""探针必须一直过真 Laya 的题型校验 —— 这里把规则抄一份当回归闸门。

真校验器是 `laya.agent.Agent._check_question`（`laya` 只装在 `.venv-laya` 里，
agent 的 venv 不背这个依赖），所以这里不 import 它，而是把规则写成断言：`type`
必须是三种之一、**每题必须有非空 `instructions`**、choice 的 criteria 非空。
探针一旦退回老写法（缺 instructions），这里就红，不用等真机 422。
"""

from spire_agent import cli
from spire_agent.probe import (
    PROBE_OPTIONS,
    PROBE_QUESTION_ID,
    check_probe_answer,
    probe_payload,
)
from spire_core.config import MODE_AGENT, from_dict

QTYPES = {"choice", "score", "noul"}


def test_probe_payload_passes_the_laya_question_rules():
    payload = probe_payload()
    assert set(payload) == {"state", "questions"}
    questions = payload["questions"]
    assert questions

    for qid, qdef in questions.items():
        assert isinstance(qdef, dict), qid
        assert qdef["type"] in QTYPES, qid
        instructions = qdef.get("instructions")
        assert isinstance(instructions, str) and instructions.strip(), qid
        criteria = qdef.get("criteria")
        if qdef["type"] == "choice":
            assert isinstance(criteria, (dict, list)) and criteria, qid
        elif qdef["type"] == "score":
            assert isinstance(criteria, list) and criteria, qid
        elif criteria is not None:
            assert isinstance(criteria, dict), qid
            assert {str(k).lower() for k in criteria} <= {"true", "false"}, qid


def test_probe_asks_the_action_question_with_both_candidates():
    question = probe_payload()["questions"][PROBE_QUESTION_ID]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == set(PROBE_OPTIONS)


def test_check_probe_answer_accepts_the_real_response_shape():
    result = {
        "model": "laya-rl-agent",
        "answers": {
            PROBE_QUESTION_ID: {
                "type": "choice",
                "choice": PROBE_OPTIONS[0],
                "probabilities": {PROBE_OPTIONS[0]: 0.5256, PROBE_OPTIONS[1]: 0.4744},
                "answer_confidence": 0.5256,
            }
        },
        "usage": {"input_tokens": 207, "output_tokens": 0},
        "routing": {"model": "english", "reason": "default"},
    }
    ok, detail = check_probe_answer(result)
    assert ok is True
    assert detail == PROBE_OPTIONS[0]


def test_check_probe_answer_rejects_the_legacy_and_broken_shapes():
    legacy = {"answers": {PROBE_QUESTION_ID: {"answer": PROBE_OPTIONS[0]}}}
    ok, detail = check_probe_answer(legacy)
    assert ok is False
    assert "choice" in detail

    assert check_probe_answer(None)[0] is False
    assert check_probe_answer({"answers": {}})[0] is False
    assert check_probe_answer({"answers": {PROBE_QUESTION_ID: {"choice": "nope"}}})[0] is False


class RecordingLaya:
    """把请求体录下来，替掉真 HTTP。"""

    last_body = None
    reply = None

    def __init__(self, config):
        self.config = config

    def ask_raw(self, body):
        type(self).last_body = body
        return type(self).reply


def test_preflight_sends_the_shared_payload(monkeypatch):
    RecordingLaya.reply = {"answers": {PROBE_QUESTION_ID: {"choice": PROBE_OPTIONS[1]}}}
    monkeypatch.setattr(cli, "LayaClient", RecordingLaya)

    assert cli.preflight_laya(from_dict({"mode": MODE_AGENT})) is True
    assert RecordingLaya.last_body == probe_payload()


def test_preflight_fails_when_the_answer_shape_is_wrong(monkeypatch):
    # 200 + 老写法（没有 choice）不能算通过：这正是真机 200 但契约不对的那个坑。
    RecordingLaya.reply = {"answers": {PROBE_QUESTION_ID: {"answer": PROBE_OPTIONS[0]}}}
    monkeypatch.setattr(cli, "LayaClient", RecordingLaya)

    assert cli.preflight_laya(from_dict({"mode": MODE_AGENT})) is False

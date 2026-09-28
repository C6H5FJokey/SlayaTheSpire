"""`serve.py`：HTTP 契约（用假 agent，几毫秒）+ 进程内自检（用迷你 checkpoint）。

契约必须和 `laya.serve` 的 `/v1/systemone` 一模一样：`answers` / `usage` 原样透出，
`routing.model` 报 checkpoint 名（agent 侧要把它落进 `meta.json`），预算守卫照旧 413/401。
"""

from __future__ import annotations

import json
import os

import pytest

import serve as server

PROBE = {"q": {"type": "choice", "instructions": "Answer a.", "criteria": {"a": "a", "b": "b"}}}


class FakeAgent:
    """只实现 `serve.py` 真正用到的那几个属性/方法（不加载任何模型）。"""

    model_id = "C:/fake/checkpoint"
    device = "cpu"
    cfg = {"fine_tuned": True, "model_name": "laya-fake"}

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def system_one(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        qid = next(iter(questions))
        return {
            "model": "laya-rl-agent",
            "answers": {qid: {"type": "choice", "choice": "a", "answer_confidence": 0.9,
                              "probabilities": {"a": 0.9, "b": 0.1}}},
            "usage": {"input_tokens": 3, "output_tokens": 0},
        }


@pytest.fixture
def api_client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    agent = FakeAgent()
    app = server.create_app(agent, name="laya-fake", api_key="secret",
                            max_len=2048, head_max_len=320, fine_tuned=True)
    with TestClient(app) as client:
        yield client, agent


def test_health_reports_the_checkpoint(api_client):
    client, _ = api_client
    body = client.get("/health").json()
    assert body["status"] == "ok" and body["loaded"] is True
    assert body["model"] == "laya-fake" and body["fine_tuned"] is True


def test_systemone_answers_and_tags_the_checkpoint(api_client):
    client, agent = api_client
    response = client.post("/v1/systemone", json={"state": "s", "questions": PROBE},
                           headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "laya-rl-agent"
    assert body["answers"]["q"]["choice"] == "a"
    assert body["usage"] == {"input_tokens": 3, "output_tokens": 0}
    # routing.model 是 checkpoint 名；agent 侧靠它记 meta.json（见 docs/07-laya-contract.md）
    assert body["routing"]["model"] == "laya-fake"
    assert os.path.isabs(body["routing"]["checkpoint"])
    # 预算在服务端统一收口：序列长度按 checkpoint 的 max_len 走，不许客户端说了算
    state, questions, overrides = agent.calls[-1]
    assert state == "s" and questions == PROBE
    assert overrides == {"max_len": 2048, "head_max_len": 320}


def test_auth_is_required_when_a_key_is_configured(api_client):
    client, _ = api_client
    assert client.post("/v1/systemone", json={"state": "s", "questions": PROBE}).status_code == 401
    assert client.post("/v1/systemone", json={"state": "s", "questions": PROBE},
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_no_key_configured_means_open(api_client):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    app = server.create_app(FakeAgent(), name="laya-open", api_key=None, fine_tuned=True)
    with TestClient(app) as client:
        assert client.post("/v1/systemone", json={"state": "s", "questions": PROBE}).status_code == 200


def test_bad_requests_are_400_not_500(api_client):
    client, _ = api_client
    headers = {"Authorization": "Bearer secret"}
    assert client.post("/v1/systemone", content=b"{not json", headers=headers).status_code == 400
    assert client.post("/v1/systemone", json={"state": "s"}, headers=headers).status_code == 400
    assert client.post("/v1/systemone", json={"state": "s", "questions": []},
                       headers=headers).status_code == 400


def test_budget_guards_still_return_413(api_client):
    client, _ = api_client
    headers = {"Authorization": "Bearer secret"}
    huge = {"state": "x" * 50001, "questions": PROBE}
    assert client.post("/v1/systemone", json=huge, headers=headers).status_code == 413
    many = {"state": "s", "questions": {f"q{i}": PROBE["q"] for i in range(65)}}
    assert client.post("/v1/systemone", json=many, headers=headers).status_code == 413


def test_inference_failure_does_not_leak_internals():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    class Broken(FakeAgent):
        def system_one(self, state, questions, **kwargs):
            raise RuntimeError("C:/secret/weights.safetensors missing")

    app = server.create_app(Broken(), name="laya-broken", api_key=None, fine_tuned=True)
    with TestClient(app) as client:
        response = client.post("/v1/systemone", json={"state": "s", "questions": PROBE})
        assert response.status_code == 500
        assert "secret" not in response.text


def test_model_dir_must_be_a_local_checkpoint(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        server.resolve_model_dir(str(tmp_path / "nope"))
    assert "本地" in str(excinfo.value)
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SystemExit) as excinfo:
        server.resolve_model_dir(str(plain))
    assert "rl_agent_config.json" in str(excinfo.value)


def test_model_dir_accepts_the_fine_tuned_checkpoint(tiny_checkpoint):
    assert server.resolve_model_dir(tiny_checkpoint) == os.path.abspath(tiny_checkpoint)


def test_env_vars_supply_the_defaults(monkeypatch):
    monkeypatch.setenv("LAYA_MODEL", "custom/path")
    monkeypatch.setenv("LAYA_HOST", "0.0.0.0")
    monkeypatch.setenv("LAYA_PORT", "9001")
    monkeypatch.setenv("LAYA_DEVICE", "cpu")
    monkeypatch.setenv("LAYA_API_KEY", "k")
    monkeypatch.setenv("LAYA_MAX_LEN", "1024")
    args = server.parse_args([])
    assert args.model == "custom/path" and args.host == "0.0.0.0" and args.port == 9001
    assert args.device == "cpu" and args.api_key == "k" and args.max_len == 1024
    # 命令行优先于环境变量
    assert server.parse_args(["--port", "1234"]).port == 1234


def test_binding_a_public_address_requires_a_key(monkeypatch):
    monkeypatch.setenv("LAYA_MODEL", "whatever")
    with pytest.raises(SystemExit) as excinfo:
        server.main(["--host", "0.0.0.0", "--model", "nope"])
    assert "api-key" in str(excinfo.value)
    # 有 key 之后才会继续往下走（这里会在解析目录时失败，说明守卫已经放行）
    with pytest.raises(SystemExit) as excinfo:
        server.main(["--host", "0.0.0.0", "--api-key", "k", "--model", "nope"])
    assert "本地" in str(excinfo.value)


def test_in_process_check_answers_a_minimal_question(tiny_checkpoint):
    pytest.importorskip("torch")
    import laya

    with laya.load(tiny_checkpoint, device="cpu") as agent:
        ok, result = server.check_agent(agent, "laya-tiny", max_len=256, head_max_len=96)
        assert ok, result
        assert result["routing"]["model"] == "laya-tiny"
        assert result["answers"]["q"]["choice"] in ("a", "b")
        assert result["latency_ms"] >= 0

"""Laya 客户端：鉴权 / 重试 / 缓存 / 预算 / checkpoint 选择。

绝大部分用例走 `httpx.MockTransport`（快且能精确构造 401/503/超时），
最后一个用例跑真 loopback HTTP 往返，验证 `FakeLaya` 与客户端是同一份契约。
"""

import httpx
import pytest

from spire_agent.fake_laya import FakeLaya, answer_questions
from spire_agent.laya_client import LayaClient, check_budget, pick_model
from spire_core.config import LayaConfig
from spire_core.pipeline import Plan


def make_plan(criteria=None):
    criteria = criteria or {"play:h0->m0": "Play Strike.", "end_turn": "End your turn."}
    return Plan(
        decision_point="combat_play",
        question_id="q_action",
        candidates=[],
        state={"player": {"hp": 50, "energy": 3}},
        questions={
            "q_action": {
                "type": "choice",
                "instructions": "Choose the best action.",
                "criteria": criteria,
            }
        },
    )


def make_config(**overrides) -> LayaConfig:
    defaults = dict(
        base_url="http://laya.test",
        api_key="k",
        retries=3,
        backoff_base_sec=0.0,
        cache=True,
        timeout_sec=1.0,
    )
    defaults.update(overrides)
    return LayaConfig(**defaults)


def ok_response(request: httpx.Request) -> httpx.Response:
    body = request.read().decode("utf-8")
    import json

    payload = json.loads(body or "{}")
    model = payload.get("model") or "english"
    return httpx.Response(
        200,
        json={
            "model": "laya-rl-agent",
            "answers": answer_questions(payload.get("questions") or {}),
            "usage": {"input_tokens": 10, "output_tokens": 1},
            "routing": {"model": model, "reason": "explicit"},
        },
    )


def client_with(handler, config=None, client=None) -> LayaClient:
    config = config or make_config()
    if client is None:
        client = httpx.Client(transport=httpx.MockTransport(handler), timeout=config.timeout_sec)
    return LayaClient(config, client=client)


def test_ask_parses_the_contract_shape():
    client = client_with(ok_response)
    result = client.ask(make_plan())

    assert result is not None
    assert result.answers["q_action"]["choice"] == "end_turn"
    assert result.model == "laya-rl-agent"
    assert result.checkpoint == "english"
    assert result.usage == {"input_tokens": 10, "output_tokens": 1}
    assert result.cache_hit is False
    assert client.stats.calls == 1


def test_ask_sends_bearer_token_and_picked_model():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read().decode("utf-8")
        return ok_response(request)

    client_with(handler).ask(make_plan())
    import json

    assert seen["auth"] == "Bearer k"
    assert json.loads(seen["body"])["model"] == "english"


def test_cache_hit_avoids_second_network_call():
    calls = []

    def handler(request):
        calls.append(1)
        return ok_response(request)

    client = client_with(handler)
    first = client.ask(make_plan())
    second = client.ask(make_plan())

    assert len(calls) == 1
    assert client.stats.calls == 1
    assert client.stats.cache_hits == 1
    assert second is not None and second.cache_hit is True
    assert second.answers == first.answers


def test_cache_disabled_sends_every_time():
    calls = []

    def handler(request):
        calls.append(1)
        return ok_response(request)

    client = client_with(handler, make_config(cache=False))
    client.ask(make_plan())
    client.ask(make_plan())
    assert len(calls) == 2
    assert client.stats.cache_hits == 0


def test_different_questions_are_cached_separately():
    calls = []

    def handler(request):
        calls.append(1)
        return ok_response(request)

    client = client_with(handler)
    client.ask(make_plan())
    client.ask(make_plan(criteria={"a": "A", "b": "B"}))
    assert len(calls) == 2


def test_retries_transient_failures_then_succeeds():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) <= 2:
            return httpx.Response(503, json={"error": "busy"})
        return ok_response(request)

    client = client_with(handler)
    result = client.ask(make_plan())

    assert result is not None
    assert len(attempts) == 3
    assert client.stats.retries == 2
    assert client.stats.failures == 0


def test_gives_up_after_configured_attempts():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(503, json={"error": "busy"})

    client = client_with(handler, make_config(retries=2))
    assert client.ask(make_plan()) is None
    assert len(attempts) == 2
    assert client.stats.failures == 1
    assert client.stats.calls == 0
    assert client.stats.retries == 1


def test_auth_failure_is_not_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(401, json={"error": "unauthorized"})

    client = client_with(handler, make_config(retries=3))
    assert client.ask(make_plan()) is None
    assert len(attempts) == 1
    assert client.stats.failures == 1


def test_connection_error_is_retried_then_gives_up():
    attempts = []

    def handler(request):
        attempts.append(1)
        raise httpx.ConnectError("nope", request=request)

    client = client_with(handler, make_config(retries=2))
    assert client.ask(make_plan()) is None
    assert len(attempts) == 2
    assert client.stats.failures == 1


def test_non_json_body_is_a_failure():
    client = client_with(lambda request: httpx.Response(200, text="<html>oops</html>"))
    assert client.ask(make_plan()) is None
    assert client.stats.failures == 1


def test_missing_answers_object_is_a_failure():
    client = client_with(lambda request: httpx.Response(200, json={"model": "english"}))
    assert client.ask(make_plan()) is None
    assert client.stats.failures == 1


def test_over_budget_request_never_leaves_the_process():
    calls = []

    def handler(request):
        calls.append(1)
        return ok_response(request)

    client = client_with(handler)
    assert client.ask_raw({"state": {"blob": "x" * 60000}, "questions": {}}) is None
    assert client.stats.budget_violations == 1
    assert calls == []
    assert client.stats.calls == 0


def test_check_budget_accepts_a_small_payload():
    check_budget({"state": {"a": 1}, "questions": {"q": {"type": "choice", "criteria": {"a": "A"}}}})


def test_pick_model_switches_to_multilingual_for_long_state():
    short = LayaConfig(model="english", prefer_multilingual_over_chars=100)
    assert pick_model(short, {"state": {"blob": "x" * 10}}) == "english"
    assert pick_model(short, {"state": {"blob": "x" * 200}}) == "multilingual"
    assert pick_model(LayaConfig(model="multilingual"), {"state": {}}) == "multilingual"


def test_empty_model_lets_the_router_decide():
    seen = {}

    def handler(request):
        import json

        seen.update(json.loads(request.read().decode("utf-8") or "{}"))
        return ok_response(request)

    client = client_with(handler, make_config(model=""))
    result = client.ask(make_plan())
    assert result is not None
    assert "model" not in seen
    assert result.checkpoint == "english"


def test_real_loopback_round_trip_against_fake_laya():
    """FakeLaya 与客户端必须是同一份契约（真 socket，慢但这层必须真跑一次）。"""
    with FakeLaya() as fake:
        config = LayaConfig(
            base_url=fake.base_url,
            api_key=fake.api_key,
            backoff_base_sec=0.001,
            timeout_sec=5.0,
        )
        client = LayaClient(config)
        result = client.ask(make_plan())

        assert result is not None
        assert result.answers["q_action"]["choice"] == "end_turn"
        assert result.checkpoint == "english"
        assert len(fake.requests) == 1
        assert fake.requests[0].headers["authorization"] == f"Bearer {fake.api_key}"


# --------------------------------------------------------------------------- #
# on_exchange：面板要看到"原样发出去 / 原样拿回来"
# --------------------------------------------------------------------------- #


def test_exchange_hook_records_the_exact_request_and_response():
    """钩子拿到的请求体必须和线上真正发出去的那一份逐字段相同（含我们补的 model）。"""
    import json

    wire = {}

    def handler(request):
        wire["body"] = json.loads(request.read().decode("utf-8"))
        return ok_response(request)

    client = client_with(handler)
    records = []
    client.on_exchange = records.append
    client.ask(make_plan())

    assert len(records) == 1
    record = records[0]
    assert record["request"] == wire["body"]
    assert record["checkpoint"] == "english"
    assert record["ok"] is True and record["status"] == 200 and record["cached"] is False
    assert record["response"]["routing"]["model"] == "english"
    # Laya 的答案是富对象（type/choice/probabilities/confidence），原样保留。
    assert record["answer"]["choice"] == "end_turn"
    assert record["error"] is None
    assert record["request_bytes"] > 0 and record["response_bytes"] > 0
    assert record["id"] == 1


def test_exchange_hook_marks_cache_hits_and_keeps_the_original_response():
    client = client_with(ok_response)
    records = []
    client.on_exchange = records.append
    client.ask(make_plan())
    client.ask(make_plan())

    assert [r["cached"] for r in records] == [False, True]
    # 缓存那一次没有真的走网络，所以没有 status；返回体仍是当初服务器给的那份。
    assert records[1]["status"] is None
    assert records[1]["response"]["routing"]["model"] == "english"
    assert records[1]["request"] == records[0]["request"]


def test_exchange_hook_records_every_failed_attempt():
    def handler(request):
        return httpx.Response(503, text="busy")

    client = client_with(handler, make_config(retries=2))
    records = []
    client.on_exchange = records.append

    assert client.ask(make_plan()) is None
    assert [(r["attempt"], r["status"], r["ok"]) for r in records] == [
        (1, 503, False),
        (2, 503, False),
    ]
    assert records[0]["response_text"] == "busy"
    assert records[0]["error"] == "HTTP 503"
    assert records[0]["answer"] is None


def test_exchange_hook_records_connection_failures():
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    client = client_with(handler, make_config(retries=1))
    records = []
    client.on_exchange = records.append

    assert client.ask(make_plan()) is None
    assert records[0]["status"] is None and records[0]["ok"] is False
    assert "ConnectError" in records[0]["error"]
    # 请求体照样要记：连不上的时候，"到底想发什么"才是要看的。
    assert records[0]["request"]["questions"]


def test_exchange_hook_records_an_over_budget_request_that_was_never_sent():
    def handler(request):  # pragma: no cover - 超预算的请求根本不该发出去
        raise AssertionError("over-budget request must not reach the network")

    client = client_with(handler)
    records = []
    client.on_exchange = records.append
    plan = make_plan()
    plan.state = {"blob": "x" * 60000}

    assert client.ask(plan) is None
    assert records[0]["ok"] is False and records[0]["status"] is None
    assert "BudgetViolation" in records[0]["error"]
    assert records[0]["request_bytes"] > 60000


def test_a_broken_exchange_hook_cannot_break_a_decision():
    """面板是观测，不是决策链上的一环 —— 它炸了，这一局照样得走下去。"""
    client = client_with(ok_response)

    def boom(record):
        raise RuntimeError("panel exploded")

    client.on_exchange = boom
    result = client.ask(make_plan())
    assert result is not None
    assert result.answers["q_action"]["choice"] == "end_turn"

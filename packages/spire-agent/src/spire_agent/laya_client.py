"""远程 Laya 客户端（见 docs/07-laya-contract.md）。

契约要点，逐条对应实现：
- `POST {base_url}{endpoint}`，`Authorization: Bearer`，体 `{state, questions}`；
- 单次超时 5s、重试 3 次、指数退避；
- 按 `(state_hash, questions_hash)` 缓存（同局面重复问不重复花钱）；
- 硬预算在 core 的 `budget.enforce` 里已经压过一轮，这里**再断言一次**：
  超预算宁可报错也不发出去（发出去只会拿到 400，还浪费一次往返）；
- 长文本（> `prefer_multilingual_over_chars`）自动切 `multilingual` checkpoint；
- 失败**不抛异常**：返回 None，由 runner 走 `pipeline.fallback_decision` 兜底。
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from spire_core import budget as budget_mod
from spire_core.config import LayaConfig
from spire_core.pipeline import Plan
from spire_core.types import LayaResult

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_STATE_CHARS = 50000
MAX_QUESTIONS = 64
MAX_OPTIONS = 512


class BudgetViolation(RuntimeError):
    """构题超出硬预算。**这是 bug，不是网络问题**：不能靠重试解决。"""


def canonical_hash(value: Any) -> str:
    """确定性哈希：排序键 + 紧凑分隔符，与 dict 插入顺序无关。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def count_options(questions: dict[str, Any]) -> int:
    total = 0
    for q in questions.values():
        if not isinstance(q, dict):
            continue
        # choice: criteria 是 {key: 描述}；score: criteria 是有序等级列表
        criteria = q.get("criteria")
        if isinstance(criteria, (dict, list)):
            total += len(criteria)
            continue
        levels = q.get("levels")
        if isinstance(levels, list):
            total += len(levels)
    return total


def check_budget(payload: dict[str, Any]) -> None:
    """发出去之前最后一道闸（见 docs/07-laya-contract.md#预算）。"""
    state = payload.get("state")
    questions = payload.get("questions") or {}
    state_chars = len(json.dumps(state, ensure_ascii=False))
    body_bytes = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if state_chars > MAX_STATE_CHARS:
        raise BudgetViolation(f"state is {state_chars} chars > {MAX_STATE_CHARS}")
    if len(questions) > MAX_QUESTIONS:
        raise BudgetViolation(f"{len(questions)} questions > {MAX_QUESTIONS}")
    if count_options(questions) > MAX_OPTIONS:
        raise BudgetViolation(f"{count_options(questions)} options > {MAX_OPTIONS}")
    if body_bytes > MAX_BODY_BYTES:
        raise BudgetViolation(f"body is {body_bytes} bytes > {MAX_BODY_BYTES}")


def pick_model(config: LayaConfig, payload: dict[str, Any]) -> str:
    """长文本场景切 `multilingual`（`max_len=8192`）。空字符串表示让 Router 自选。"""
    if config.model != "english":
        return config.model
    state_chars = len(json.dumps(payload.get("state"), ensure_ascii=False))
    if state_chars > config.prefer_multilingual_over_chars:
        return "multilingual"
    return config.model


def answer_summary(response: Any) -> Any:
    """从返回体里挑出"模型答了什么"，给面板的请求列表当一行摘要。

    单题直接给答案本身（`"play:h2->m0"`），多题给 `{question_id: 答案}`。
    拿不到 `answers` 就返回 None —— 那不是错误，是"这条返回没什么可看"。
    """
    if not isinstance(response, dict):
        return None
    answers = response.get("answers")
    if not isinstance(answers, dict) or not answers:
        return None
    if len(answers) == 1:
        return next(iter(answers.values()))
    return answers


@dataclass
class LayaStats:
    calls: int = 0
    cache_hits: int = 0
    failures: int = 0
    retries: int = 0
    budget_violations: int = 0
    last_latency_ms: int = 0
    total_latency_ms: int = 0
    models: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out = dict(self.__dict__)
        out["avg_latency_ms"] = (
            round(self.total_latency_ms / self.calls, 1) if self.calls else 0.0
        )
        return out


class LayaClient:
    def __init__(self, config: LayaConfig, *, client: httpx.Client | None = None) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_sec)
        self._cache: dict[tuple[str, str], LayaResult] = {}
        self.stats = LayaStats()
        # 每一次往返（成功 / 缓存命中 / 失败 / 超预算拒发）都以**完整记录**回调一次，
        # 包括原样发出去的请求体和服务器原样返回的 JSON。面板靠它回答"决策时到底
        # 发了什么、拿回了什么"（见 docs/09-observability.md）。
        # 没人挂钩子时开销接近零：第一行就返回，不做任何序列化。
        self.on_exchange: Callable[[dict[str, Any]], None] | None = None
        self._exchange_seq = 0

    # ------------------------------------------------------------ 观测钩子

    def _emit(
        self,
        *,
        request: dict[str, Any] | None,
        checkpoint: str = "",
        ok: bool,
        status: int | None = None,
        cached: bool = False,
        error: str | None = None,
        response: Any = None,
        response_text: str | None = None,
        latency_ms: int = 0,
        attempt: int = 1,
    ) -> None:
        """把一次请求/返回交给 `on_exchange`。**绝不允许影响决策。**"""
        hook = self.on_exchange
        if hook is None:
            return
        payload = request if isinstance(request, dict) else {}
        try:
            request_bytes = len(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
        except (TypeError, ValueError) as exc:
            # 序列化不了也得把这条记录发出去 —— 面板上"大小 0 B + 这句 error"
            # 比"面板里干脆没这条"有用得多。
            request_bytes = 0
            error = error or f"request is not JSON-serializable: {exc}"
        if response is not None:
            try:
                text = json.dumps(response, ensure_ascii=False)
            except (TypeError, ValueError):
                text = repr(response)
            response_bytes = len(text.encode("utf-8"))
        else:
            response_bytes = len(response_text.encode("utf-8")) if response_text else 0
        self._exchange_seq += 1
        record: dict[str, Any] = {
            "id": self._exchange_seq,
            "at": time.time(),
            "url": self.config.base_url.rstrip("/") + self.config.endpoint,
            "attempt": attempt,
            "checkpoint": checkpoint,
            "request": payload,
            "request_bytes": request_bytes,
            "ok": bool(ok),
            "status": status,
            "cached": bool(cached),
            "error": error,
            "response": response,
            "response_text": response_text if response is None else None,
            "response_bytes": response_bytes,
            "latency_ms": int(latency_ms),
            "answer": answer_summary(response),
        }
        try:
            hook(record)
        except Exception as exc:  # noqa: BLE001 - 观测坏了也不能连累这一局
            log.warning("on_exchange hook failed: %s", exc)

    # ------------------------------------------------------------ 主入口

    def ask(self, plan: Plan) -> LayaResult | None:
        result = self.ask_raw(plan.to_payload())
        if result is None:
            return None
        return LayaResult(
            model=result.get("model", ""),
            answers=result.get("answers") or {},
            usage=result.get("usage") or {},
            routing=result.get("routing") or {},
            latency_ms=int(result.get("latency_ms", 0) or 0),
            cache_hit=bool(result.get("cache_hit", False)),
            raw=result.get("raw") or {},
        )

    def ask_raw(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        # 先算出真正要发出去的体（含 checkpoint），这样"缓存命中"和"真发出去"
        # 记录下来的 request 是同一份 —— 面板上的请求必须和线上跑的一模一样。
        model = pick_model(self.config, payload)
        body = dict(payload)
        if model:
            body["model"] = model

        try:
            check_budget(payload)
        except BudgetViolation as exc:
            self.stats.budget_violations += 1
            log.error("refusing to send an over-budget request: %s", exc)
            # 拒发的请求也要进面板：超预算常常正是"请求构造出了问题"的第一现场。
            self._emit(
                request=body,
                checkpoint=model,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
            )
            return None

        key = (canonical_hash(payload.get("state")), canonical_hash(payload.get("questions")))
        if self.config.cache and key in self._cache:
            self.stats.cache_hits += 1
            cached = self._cache[key]
            # 这一问没有真的花钱，所以 status 留空、cached=true。返回体仍然是当初
            # 服务器给的那一份原样 JSON。
            self._emit(
                request=body, checkpoint=model, ok=True, cached=True, response=cached.raw
            )
            return {
                "model": cached.model,
                "answers": cached.answers,
                "usage": cached.usage,
                "routing": cached.routing,
                "latency_ms": 0,
                "cache_hit": True,
                "raw": cached.raw,
            }

        result = self._post_with_retries(body, checkpoint=model)
        if result is None:
            return None

        self.stats.calls += 1
        # 按**服用的 checkpoint** 计数：真实 Laya 顶层 `model` 恒为架构名
        # （laya-rl-agent），只有 routing 才说明这一问落在哪个 checkpoint 上。
        served = (
            (result.get("routing") or {}).get("model")
            or result.get("model")
            or model
            or "router"
        )
        self.stats.models[served] = self.stats.models.get(served, 0) + 1
        self.stats.last_latency_ms = int(result.get("latency_ms", 0) or 0)
        self.stats.total_latency_ms += self.stats.last_latency_ms
        if self.config.cache:
            self._remember(key, result)
        return result

    # ------------------------------------------------------------ 内部

    def _remember(self, key: tuple[str, str], result: dict[str, Any]) -> None:
        if len(self._cache) >= self.config.cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = LayaResult(
            model=result.get("model", ""),
            answers=result.get("answers") or {},
            usage=result.get("usage") or {},
            routing=result.get("routing") or {},
            latency_ms=int(result.get("latency_ms", 0) or 0),
            raw=result.get("raw") or {},
        )

    def _post_with_retries(
        self, body: dict[str, Any], *, checkpoint: str = ""
    ) -> dict[str, Any] | None:
        url = self.config.base_url.rstrip("/") + self.config.endpoint
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        attempt = 0
        while attempt < max(1, self.config.retries):
            attempt += 1
            started = time.monotonic()
            try:
                resp = self._client.post(url, json=body, headers=headers)
            except httpx.HTTPError as exc:
                log.warning("laya call failed (attempt %d): %s", attempt, exc)
                self._emit(
                    request=body,
                    checkpoint=checkpoint,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    attempt=attempt,
                )
            else:
                latency_ms = int((time.monotonic() - started) * 1000)
                if resp.status_code == 200:
                    decoded = self._decode(resp, latency_ms)
                    self._emit(
                        request=body,
                        checkpoint=checkpoint,
                        ok=decoded is not None,
                        status=200,
                        error=None if decoded is not None else "response is not a {answers} object",
                        response=(decoded or {}).get("raw"),
                        response_text=resp.text[:4000],
                        latency_ms=latency_ms,
                        attempt=attempt,
                    )
                    return decoded
                if resp.status_code in (401, 403):
                    # 鉴权错误重试没有意义
                    self.stats.failures += 1
                    log.error("laya auth failed (%s): %s", resp.status_code, resp.text[:200])
                    self._emit(
                        request=body,
                        checkpoint=checkpoint,
                        ok=False,
                        status=resp.status_code,
                        error=f"HTTP {resp.status_code}（鉴权失败，不重试）",
                        response_text=resp.text[:4000],
                        latency_ms=latency_ms,
                        attempt=attempt,
                    )
                    return None
                self._emit(
                    request=body,
                    checkpoint=checkpoint,
                    ok=False,
                    status=resp.status_code,
                    error=f"HTTP {resp.status_code}",
                    response_text=resp.text[:4000],
                    latency_ms=latency_ms,
                    attempt=attempt,
                )
                log.warning(
                    "laya returned %s (attempt %d): %s",
                    resp.status_code,
                    attempt,
                    resp.text[:200],
                )
            if attempt < self.config.retries:
                self.stats.retries += 1
                time.sleep(self.config.backoff_base_sec * (2 ** (attempt - 1)))

        self.stats.failures += 1
        return None

    def _decode(self, resp: httpx.Response, latency_ms: int) -> dict[str, Any] | None:
        try:
            body = resp.json()
        except ValueError:
            self.stats.failures += 1
            log.error("laya returned non-JSON: %s", resp.text[:200])
            return None
        if not isinstance(body, dict):
            self.stats.failures += 1
            log.error("laya returned a non-object body: %r", type(body))
            return None
        answers = body.get("answers")
        if not isinstance(answers, dict):
            self.stats.failures += 1
            log.error("laya response has no answers object: %s", json.dumps(body)[:200])
            return None
        return {
            "model": str(body.get("model", "")),
            "answers": answers,
            "usage": body.get("usage") or {},
            "routing": body.get("routing") or {},
            "latency_ms": latency_ms,
            "cache_hit": False,
            "raw": body,
        }

"""本地只读观战面板（见 docs/09-observability.md）。

刻意只用**标准库** `http.server`：面板跑在玩游戏的同一台机器上，不应该为了看两眼
状态就引入 web 框架。页面每秒轮询一次 `/api/state`（比 WebSocket 好排查，也不需要
任何前端依赖）。

只读：没有任何写接口、没有"替 agent 决策"的后门 —— 面板能看到的一切都可以从
`runs/<run_id>/` 里复现。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)

LOOPBACK = ("127.0.0.1", "localhost", "::1")


def exchange_summary(event: dict[str, Any]) -> dict[str, Any]:
    """把一次 Laya 往返压成"列表能承受"的摘要 —— **不含请求体与返回体**。

    完整记录（可能几十 KB 甚至 2MB）留在 `PanelState.exchanges` 的环形缓冲里，
    只在 `GET /api/exchange/<id>` 时按需取。`/api/state` 每秒被轮询一次，
    绝不能扛着这些大块内容。
    """
    response = event.get("response")
    routing = response.get("routing") if isinstance(response, dict) else None
    return {
        "kind": "exchange",
        "id": event.get("id"),
        "at": event.get("at"),
        "seq": event.get("seq"),
        "decision_point": event.get("decision_point"),
        "attempt": event.get("attempt", 1),
        "checkpoint": event.get("checkpoint") or "",
        "served": (routing or {}).get("model") if isinstance(routing, dict) else None,
        "ok": bool(event.get("ok")),
        "status": event.get("status"),
        "cached": bool(event.get("cached")),
        "error": event.get("error"),
        "latency_ms": event.get("latency_ms", 0),
        "request_bytes": event.get("request_bytes", 0),
        "response_bytes": event.get("response_bytes", 0),
        "answer": event.get("answer"),
    }


class PanelState:
    """面板的唯一状态容器。`update()` 只追加，`snapshot()` 返回不可变视图。"""

    def __init__(self, timeline_size: int = 30, exchange_size: int = 20) -> None:
        self._lock = threading.Lock()
        self.timeline: deque[dict[str, Any]] = deque(maxlen=max(1, timeline_size))
        # 最近若干次"发给 Laya 的请求 + 拿回的原始返回"，按 id 递增。
        self.exchanges: deque[dict[str, Any]] = deque(maxlen=max(1, exchange_size))
        self.latest: dict[str, Any] = {}
        self.last_observation: dict[str, Any] = {}
        self.last_decision: dict[str, Any] = {}
        self.human_prompt: dict[str, Any] = {}
        self.counters: dict[str, int] = {}
        self.updated_at: float = 0.0

    def update(self, event: dict[str, Any]) -> None:
        kind = str(event.get("kind", "unknown"))
        with self._lock:
            self.updated_at = time.time()
            self.counters[kind] = self.counters.get(kind, 0) + 1
            if kind == "observation":
                self.last_observation = event
            elif kind == "decision":
                self.last_decision = event
            elif kind == "human_prompt":
                self.human_prompt = event
            elif kind == "human_action":
                self.human_prompt = {}
            if kind == "exchange":
                # 完整记录进环形缓冲；时间线上只留摘要，否则每秒一次的轮询会被
                # 几十 KB 的请求体撑爆。
                self.exchanges.append(event)
                entry = exchange_summary(event)
                if entry.get("at") is None:
                    entry["at"] = self.updated_at
            else:
                entry = {"at": self.updated_at, **event}
            self.timeline.append(entry)
            self.latest = entry

    def exchange(self, exchange_id: int) -> dict[str, Any] | None:
        """按 id 取完整往返记录（请求体 + 返回体）。已被挤出缓冲则返回 None。"""
        with self._lock:
            for event in self.exchanges:
                if event.get("id") == exchange_id:
                    return dict(event)
            return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "updated_at": self.updated_at,
                "observation": dict(self.last_observation),
                "decision": dict(self.last_decision),
                "human_prompt": dict(self.human_prompt),
                "counters": dict(self.counters),
                "exchanges": [exchange_summary(e) for e in self.exchanges],
                "timeline": list(self.timeline),
            }


class PanelServer:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8788,
        timeline_size: int = 30,
        exchange_size: int = 20,
    ) -> None:
        if host not in LOOPBACK:
            raise ValueError(f"panel must listen on loopback, got {host!r}")
        self.host = host
        self.port = port
        self.state = PanelState(timeline_size, exchange_size)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                return

            def _send(self, status: int, content_type: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/api/state":
                    body = json.dumps(state.snapshot(), ensure_ascii=False).encode("utf-8")
                    self._send(200, "application/json; charset=utf-8", body)
                elif path.startswith("/api/exchange/"):
                    self._send_exchange(path.rsplit("/", 1)[-1])
                elif path == "/health":
                    self._send(200, "application/json", b'{"ok":true}')
                elif path in ("/", "/index.html"):
                    self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                else:
                    self._send(404, "text/plain; charset=utf-8", b"not found")

            def _send_exchange(self, raw_id: str) -> None:
                """一次往返的完整原文（请求体 + 返回体）。被挤出缓冲就 404。"""
                try:
                    exchange_id = int(raw_id)
                except ValueError:
                    self._send(400, "text/plain; charset=utf-8", b"bad exchange id")
                    return
                record = state.exchange(exchange_id)
                if record is None:
                    self._send(
                        404,
                        "text/plain; charset=utf-8",
                        b"exchange id not in the panel buffer",
                    )
                    return
                body = json.dumps(record, ensure_ascii=False).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        url = f"http://{self.host}:{self.port}/"
        log.info("panel listening on %s", url)
        return url

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def update(self, event: dict[str, Any]) -> None:
        self.state.update(event)

    def __call__(self, event: dict[str, Any]) -> None:
        self.update(event)


PAGE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<title>SlayaTheSpire 观战面板</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 16px 20px; font: 13px/1.5 ui-monospace, Consolas, monospace;
         background: #14161a; color: #dfe3ea; }
  h1 { font-size: 15px; margin: 0 0 12px; color: #8fd3ff; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; }
  .card { background: #1b1f26; border: 1px solid #2a3039; border-radius: 6px; padding: 10px 12px; }
  .card h2 { font-size: 12px; margin: 0 0 8px; color: #9aa7b8; font-weight: 600;
             text-transform: uppercase; letter-spacing: .04em; }
  .big { font-size: 20px; color: #fff; }
  .kv { display: flex; justify-content: space-between; gap: 8px; }
  .kv span:last-child { color: #fff; }
  table { width: 100%; border-collapse: collapse; }
  td, th { text-align: left; padding: 3px 6px; border-bottom: 1px solid #232932; }
  th { color: #9aa7b8; font-weight: 600; }
  .ok { color: #6ee7a8; } .warn { color: #ffd166; } .bad { color: #ff7b72; }
  .muted { color: #77808f; }
  .stale .card { opacity: .5; }
  code { color: #ffd166; word-break: break-all; }
  /* 两行一条：上面是"什么时候、问了什么"，下面是模型答了什么。
     窄面板（in-app 浏览器可能只有 300px 宽）下也不至于把摘要挤成竖排。 */
  .exrow { display: grid; grid-template-columns: 52px 1fr; gap: 1px 8px; padding: 4px 6px;
           border-bottom: 1px solid #232932; cursor: pointer; align-items: baseline; }
  .exrow > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .exrow > div:first-child { grid-row: span 2; align-self: center; }
  .exrow:hover { background: #212734; }
  .exrow.sel { background: #232b36; box-shadow: inset 2px 0 0 #4d7fb0; }
  .tabs { display: flex; gap: 6px; align-items: center; margin-top: 10px; }
  .tab { background: #212734; border: 1px solid #2a3039; color: #9aa7b8; padding: 3px 10px;
         border-radius: 4px; cursor: pointer; font: inherit; }
  .tab.on { color: #fff; border-color: #4d7fb0; background: #26364a; }
  .follow { color: #9aa7b8; margin-left: auto; cursor: pointer; }
  pre { margin: 8px 0 0; padding: 8px 10px; background: #0f1115; border: 1px solid #2a3039;
        border-radius: 4px; max-height: 520px; overflow: auto; white-space: pre-wrap; }
</style>
<h1>SlayaTheSpire · 只读观战面板</h1>
<div class="grid">
  <div class="card">
    <h2>局面</h2>
    <div class="kv"><span>幕 / 层</span><span id="floor" class="big">-</span></div>
    <div class="kv"><span>HP</span><span id="hp" class="big">-</span></div>
    <div class="kv"><span>金币</span><span id="gold">-</span></div>
    <div class="kv"><span title="游戏自己的屏幕枚举 AbstractDungeon.screen：没有弹出式界面时（普通战斗、地图移动…）就是 NONE">界面</span><span id="screen">-</span></div>
    <div class="kv"><span>房间</span><span id="room">-</span></div>
  </div>
  <div class="card">
    <h2>最近决策</h2>
    <div class="kv"><span>决策点</span><span id="dp">-</span></div>
    <div class="kv"><span>选中</span><span id="chosen">-</span></div>
    <div class="kv"><span>置信度</span><span id="conf">-</span></div>
    <div class="kv"><span>兜底</span><span id="fallback">-</span></div>
    <div class="kv"><span>来源</span><span id="origin">-</span></div>
  </div>
  <div class="card">
    <h2>计数</h2>
    <div id="counters" class="muted">-</div>
  </div>
  <div class="card">
    <h2>等待人类（observe_human）</h2>
    <div id="human" class="muted">-</div>
  </div>
  <div class="card" style="grid-column: 1 / -1">
    <h2>牌局（模型看到的公平局面）</h2>
    <div id="board" class="muted">-</div>
  </div>
  <div class="card" style="grid-column: 1 / -1">
    <h2>Laya 请求 / 返回（点一条看原文）</h2>
    <div id="exchanges" class="muted">-</div>
    <div id="exchange-detail"></div>
  </div>
</div>
<div class="card" style="margin-top:12px">
  <h2>时间线（最近 30 条）</h2>
  <table><thead><tr><th>时间</th><th>类型</th><th>摘要</th></tr></thead>
  <tbody id="timeline"></tbody></table>
</div>
<script>
const $ = (id) => document.getElementById(id);
function esc(v) { return String(v == null ? "-" : v).replace(/[<>&]/g, (c) => ({"<":"&lt;",">":"&gt;","&":"&amp;"}[c])); }
function summarize(e) {
  if (e.kind === "observation") return `seq ${e.seq} ${e.screen} ${e.act}-${e.floor} hp ${e.hp}/${e.max_hp}`;
  if (e.kind === "decision") return `${e.decision_point} -> ${e.chosen || "-"}${e.fallback ? " (fallback)" : ""}`;
  if (e.kind === "human_prompt") return `${e.decision_point} 等人类（${(e.candidates||[]).length} 个候选）`
    + (e.model_answer ? ` 模型: ${e.model_answer}` : "");
  if (e.kind === "human_action") return `${e.action && e.action.kind} -> ${e.matched ? e.candidate : "未命中"}`;
  if (e.kind === "exchange") return `#${e.id} ${e.cached ? "缓存命中" : (e.ok ? "HTTP " + e.status : (e.error || "失败"))}`
    + ` ${e.decision_point || "-"} seq ${e.seq} ${e.latency_ms || 0}ms ${e.request_bytes || 0}->${e.response_bytes || 0}B`
    + (e.answer == null ? "" : ` -> ${JSON.stringify(e.answer)}`);
  return JSON.stringify(e).slice(0, 120);
}
const COMBAT_DECISIONS = ["combat_play", "select_target", "select_card_must_k", "select_card_any"];
function screenLabel(o, d) {
  const raw = o.screen || "-";
  if (raw !== "NONE") return raw;
  // NONE 是游戏的原话：没有弹出式界面。战斗里它同样是 NONE，所以补一句说清我们在哪。
  return COMBAT_DECISIONS.indexOf(d.decision_point) >= 0 ? "NONE（战斗内）" : "NONE（无特殊界面）";
}
// —— Laya 请求 / 返回 ——
// 列表只带摘要：/api/state 每秒轮询一次，扛不动几十 KB 的请求体。
// 完整原文（原样发出去 / 原样拿回来）按 id 从 /api/exchange/<id> 取。
let lastExchanges = [];
let exCache = {};
let shownExchange = null;
let pendingExchange = null;
let detailTab = "req";
let followLatest = true;

function kb(n) { return n >= 1024 ? (n / 1024).toFixed(1) + " KB" : (n || 0) + " B"; }

function exchangeHead(e) {
  const when = e.at ? new Date(e.at * 1000).toLocaleTimeString() : "-";
  const parts = [when, "seq " + (e.seq == null ? "-" : e.seq), e.decision_point || "-"];
  if (e.attempt > 1) parts.push("第 " + e.attempt + " 次尝试");
  parts.push(e.cached ? "缓存命中"
    : (e.ok ? "HTTP " + e.status : (e.status ? "HTTP " + e.status : "没连上")));
  let ck = e.checkpoint || "?";
  if (e.served && e.served !== e.checkpoint) ck += " -> " + e.served;
  parts.push(ck, (e.latency_ms || 0) + " ms", kb(e.request_bytes) + " -> " + kb(e.response_bytes));
  return parts.join(" · ");
}

function renderExchanges(list) {
  if (!list || !list.length) return '<span class="muted">（还没有向 Laya 发过请求）</span>';
  return list.slice().reverse().map((e) => {
    const cls = "exrow" + (e.id === shownExchange ? " sel" : "");
    const badge = e.ok
      ? '<span class="' + (e.cached ? "warn" : "ok") + '">' + (e.cached ? "缓存" : "OK") + "</span>"
      : '<span class="bad">失败</span>';
    const tail = e.answer != null ? JSON.stringify(e.answer) : (e.error || "-");
    return '<div class="' + cls + '" data-ex="' + e.id + '"><div>' + badge + "</div>"
      + '<div class="muted">' + esc(exchangeHead(e)) + "</div>"
      + "<div><code>" + esc(tail) + "</code></div></div>";
  }).join("");
}

function renderExchangeDetail(rec) {
  if (!rec) return '<div class="muted">点开上面任意一条，这里显示它原样发出去和原样拿回来的内容。</div>';
  const rows = [
    ["URL", rec.url],
    ["seq / 决策点", (rec.seq == null ? "-" : rec.seq) + " / " + (rec.decision_point || "-")],
    ["checkpoint", (rec.checkpoint || "-") + (rec.served ? " -> " + rec.served : "")],
    ["结果", rec.cached ? "缓存命中（没有真的发出去）"
      : (rec.ok ? "HTTP " + rec.status : (rec.error || "失败"))],
    ["耗时 / 大小", (rec.latency_ms || 0) + " ms / " + kb(rec.request_bytes) + " -> " + kb(rec.response_bytes)],
  ].map((r) => '<div class="kv"><span>' + esc(r[0]) + "</span><span>" + esc(r[1]) + "</span></div>").join("");
  const body = detailTab === "req"
    ? rec.request
    : (rec.response != null ? rec.response : (rec.response_text || "（没有返回体）"));
  const tabs = '<div class="tabs">'
    + '<button class="tab' + (detailTab === "req" ? " on" : "") + '" data-tab="req">请求（'
    + kb(rec.request_bytes) + "）</button>"
    + '<button class="tab' + (detailTab === "res" ? " on" : "") + '" data-tab="res">返回（'
    + kb(rec.response_bytes) + "）</button>"
    + '<label class="follow"><input type="checkbox" id="follow"'
    + (followLatest ? " checked" : "") + "> 跟随最新</label></div>";
  return rows + tabs + "<pre>" + esc(JSON.stringify(body, null, 2)) + "</pre>";
}

async function openExchange(id) {
  if (id == null) return;
  if (!exCache[id]) {
    pendingExchange = id;
    try {
      const resp = await fetch("/api/exchange/" + id);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      exCache[id] = await resp.json();
    } catch (err) {
      $("exchange-detail").innerHTML = '<div class="bad">取不到 #' + esc(id) + " 的原文："
        + esc(String(err && err.message || err)) + "</div>";
      pendingExchange = null;
      return;
    }
    pendingExchange = null;
    // 环形缓冲会挤掉老记录，本地缓存也跟着瘦身，别让面板自己拖着几十 MB。
    const keep = {};
    for (const e of lastExchanges) if (exCache[e.id]) keep[e.id] = exCache[e.id];
    if (shownExchange != null && exCache[shownExchange]) keep[shownExchange] = exCache[shownExchange];
    exCache = keep;
  }
  shownExchange = id;
  $("exchange-detail").innerHTML = renderExchangeDetail(exCache[id]);
  $("exchanges").innerHTML = renderExchanges(lastExchanges);
}

// 牌局区：渲染的就是送给模型的那份公平 state（agent 侧 serialize.state 的原样输出）。
function cardLine(c) {
  const up = c.up ? "+".repeat(c.up) : "";
  const target = c.target ? " -> " + esc(c.target) : "";
  return '<div>· <code>' + esc(c.id) + '</code> ' + esc(c.card) + esc(up)
    + " (" + esc(c.cost) + "E)" + target + "</div>";
}
function pileLine(name, zone) {
  if (!zone) return "";
  const stacks = (zone.stacks || [])
    .map((s) => esc(s.id) + (s.up ? "+" : "") + "\u00d7" + esc(s.n)).join(", ");
  return '<div>· <span class="muted">' + esc(name) + " (" + esc(zone.total) + ")</span> "
    + (stacks || '<span class="muted">空</span>') + "</div>";
}
function powerLine(list) {
  return (list || []).map((x) => esc(x.amount) + " " + esc(x.name || x.id)).join(", ");
}
function renderBoard(b) {
  if (!b || !b.player) return '<span class="muted">（还没有收到局面）</span>';
  const p = b.player, out = [];
  out.push('<div class="kv"><span>能量 / 格挡 / 金币</span><span>'
    + esc(p.energy) + " / " + esc(p.block) + " / " + esc(p.gold) + "</span></div>");
  const relics = (p.relics || [])
    .map((r) => esc(r.name || r.id) + (r.counter >= 0 ? "(" + esc(r.counter) + ")" : "")).join(", ");
  out.push('<div>· <span class="muted">遗物</span> ' + (relics || '<span class="muted">无</span>') + "</div>");
  const potions = (p.potions || [])
    .map((x) => esc(x.name || x.id) + (x.usable ? "" : ' <span class="warn">[不可用]</span>')).join(", ");
  out.push('<div>· <span class="muted">药水</span> '
    + (potions || '<span class="muted">无（' + esc(p.potion_slots) + ' 个空槽）</span>') + "</div>");
  if ((p.powers || []).length) {
    out.push('<div>· <span class="muted">能力</span> ' + powerLine(p.powers) + "</div>");
  }
  if (b.monsters && b.monsters.length) {
    out.push('<div class="muted" style="margin-top:6px">敌人</div>');
    b.monsters.forEach((m) => {
      let line = '<div>· ' + esc(m.name) + " <code>" + esc(m.id) + "</code> "
        + esc(m.hp) + "/" + esc(m.max_hp) + " HP";
      if (m.block) line += ", " + esc(m.block) + " block";
      if (m.intent) line += ', <span class="warn">' + esc(m.intent) + "</span>";
      if ((m.powers || []).length) line += " [" + powerLine(m.powers) + "]";
      out.push(line + "</div>");
    });
  }
  if (b.hand && b.hand.length) {
    out.push('<div class="muted" style="margin-top:6px">手牌</div>');
    b.hand.forEach((c) => out.push(cardLine(c)));
  }
  const z = b.zones || {};
  if (z.draw || z.discard || z.exhaust || z.deck) {
    out.push('<div class="muted" style="margin-top:6px">牌堆</div>');
    out.push(pileLine("抽牌堆", z.draw) + pileLine("弃牌堆", z.discard)
      + pileLine("消耗堆", z.exhaust) + pileLine("主牌组", z.deck));
  }
  if (b.map) {
    out.push('<div>· <span class="muted">地图</span> 当前 ' + esc(b.map.current)
      + "，可达 " + esc((b.map.reachable || []).join(", ")) + "</div>");
  }
  const s = b.screen || {};
  if ((s.options || []).length) {
    out.push('<div>· <span class="muted">界面选项</span> ' + s.options.map(esc).join(" | ") + "</div>");
  }
  // 选牌界面本身不带语义：升级/删牌/变形/事件都是同一块 UI，来由必须显示出来，
  // 否则"该选哪张"根本没法人工核对。
  if (s.origin) {
    out.push('<div>· <span class="muted">选牌来由</span> ' + esc(s.origin)
      + (s.event_name ? " · " + esc(s.event_name) : "") + "</div>");
  }
  if (s.event_text) {
    out.push('<div class="muted">' + esc(s.event_text) + "</div>");
  }
  if ((s.reward_details || []).length) {
    out.push('<div class="muted" style="margin-top:6px">奖励明细</div>');
    s.reward_details.forEach((d) => {
      const names = (d.cards || []).map((c) => c.card + (c.up ? "+" : "")).join(", ");
      out.push('<div>· ' + esc(d.text || d.kind)
        + (names ? ' <span class="muted">' + esc(names) + "</span>" : "")
        + (d.amount ? ' <span class="muted">' + esc(d.amount) + "g</span>" : "") + "</div>");
    });
  }
  if ((s.reward_cards || []).length) {
    out.push('<div class="muted" style="margin-top:6px">奖励卡</div>');
    s.reward_cards.forEach((c) => out.push(cardLine(c)));
  }
  if ((s.shop || []).length) {
    out.push('<div class="muted" style="margin-top:6px">商店</div>');
    s.shop.forEach((it) => out.push('<div>· ' + esc(it.name || it.id)
      + ' <span class="muted">' + esc(it.price) + "g</span>"
      + (it.affordable ? "" : ' <span class="warn">买不起</span>') + "</div>"));
  }
  return out.join("");
}
async function tick() {
  try {
    const s = await (await fetch("/api/state")).json();
    document.body.classList.remove("stale");
    const o = s.observation || {}, d = s.decision || {};
    $("floor").textContent = (o.act != null ? o.act : "-") + " / " + (o.floor != null ? o.floor : "-");
    $("hp").textContent = (o.hp != null ? o.hp : "-") + " / " + (o.max_hp != null ? o.max_hp : "-");
    $("gold").textContent = esc(o.gold);
    $("screen").textContent = screenLabel(o, d);
    const r = o.room || {};
    $("room").textContent = r.type ? `${r.type} #${r.combat_instance}${r.post_sl ? " (post-SL)" : ""}` : "-";
    $("dp").textContent = esc(d.decision_point);
    $("chosen").innerHTML = "<code>" + esc(d.chosen) + "</code>";
    $("conf").textContent = d.confidence != null ? Number(d.confidence).toFixed(3) : "-";
    const fb = $("fallback"); fb.textContent = d.fallback ? "是" : "否";
    fb.className = d.fallback ? "bad" : "ok";
    // 强制决策（只有一个合法动作，没问模型）和模型决策要能一眼分开。
    $("origin").textContent = d.forced ? "强制（唯一合法动作，未问模型）" : "模型";
    $("counters").textContent = Object.entries(s.counters || {}).map(([k, v]) => `${k}=${v}`).join("  ");
    const hp = s.human_prompt || {};
    $("human").textContent = hp.decision_point
    ? `${hp.decision_point}：${(hp.candidates || []).join(", ")}`
      + (hp.model_answer ? ` | 模型: ${hp.model_answer}` : "")
      : "（当前没有等待人类；agent 模式下这一栏一直是空的）";
    $("board").innerHTML = renderBoard(o.board);
    lastExchanges = s.exchanges || [];
    $("exchanges").innerHTML = renderExchanges(lastExchanges);
    // 默认跟随最新一条：开着游戏时不用手点，最新的请求自己滚进来。
    if (followLatest && lastExchanges.length) {
      const newest = lastExchanges[lastExchanges.length - 1].id;
      if (newest !== shownExchange && newest !== pendingExchange) await openExchange(newest);
    }
    $("timeline").innerHTML = (s.timeline || []).slice().reverse().map((e) =>
      `<tr><td class="muted">${new Date(e.at * 1000).toLocaleTimeString()}</td>`
      + `<td>${esc(e.kind)}</td><td>${esc(summarize(e))}</td></tr>`).join("");
  } catch (err) {
    // 这个页面由 agent 进程自己提供：agent 一退出就没人应答了，所以别把它说成"网络问题"。
    document.body.classList.add("stale");
    $("counters").innerHTML =
      '<span class="bad">agent 已停止</span>'
      + '<div class="muted">本页由 agent 进程直接提供，agent 退出后 API 就没了（fetch: '
      + esc(err && err.message || err) + '）。上面那些数字是退出前的最后一帧。'
      + '重启 agent 后本页会自动恢复，不用刷新。</div>';
  }
}
tick(); setInterval(tick, 1000);

// 事件委托挂在 document 上：列表和详情都是整块 innerHTML 重建的，
// 逐个绑监听会被下一次重绘丢掉。
document.addEventListener("click", (ev) => {
  const row = ev.target.closest("[data-ex]");
  if (row) {
    ev.preventDefault();
    followLatest = false;
    openExchange(Number(row.dataset.ex));
    return;
  }
  const tab = ev.target.closest("[data-tab]");
  if (tab) {
    ev.preventDefault();
    detailTab = tab.dataset.tab;
    if (shownExchange != null) $("exchange-detail").innerHTML = renderExchangeDetail(exCache[shownExchange]);
  }
});
document.addEventListener("change", (ev) => {
  if (!ev.target || ev.target.id !== "follow") return;
  followLatest = ev.target.checked;
  if (followLatest && lastExchanges.length) openExchange(lastExchanges[lastExchanges.length - 1].id);
});
</script>
</html>
"""

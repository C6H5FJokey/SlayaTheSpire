# 02 架构

## 进程与端口拓扑

三个进程，全部单机串行；只有 Laya 服务可以在另一台机器上。

```
SlayTheSpire.exe (内含 mod)
   └─ 监听 127.0.0.1:17777  (NDJSON over TCP)
              ▲
              │ agent 主动连入（唯一的连接方向）
              │
python -m spire_agent  (单进程，asyncio 单事件循环)
   ├─ ModBridge      连 mod，收发 observation / action
   ├─ LayaClient     连 Laya，POST /v1/systemone
   ├─ Recorder       写 runs/<run_id>/…（pending / commit / rollback）
   └─ PanelServer    监听 127.0.0.1:8788（HTTP + WebSocket，只读）
                              ▲
                              │ 浏览器
                              │
                         观战面板

Laya 服务（远端，GPU >= 8GB）
   └─ 监听 0.0.0.0:8000    POST /v1/systemone   GET /health
```

| 端口 | 默认 | 绑定 | 协议 | 谁连谁 |
|---|---|---|---|---|
| mod 桥接 | 17777 | 模组侧 `127.0.0.1` | NDJSON over TCP | agent -> mod |
| 观战面板 | 8788 | agent 侧 `127.0.0.1` | HTTP（标准库 `ThreadingHTTPServer`） | 浏览器 -> agent |
| Laya | 8000 | 远端 | HTTP | agent -> Laya |

**为什么桥接用 NDJSON/TCP 而不是 WebSocket**：模组侧零第三方依赖、无需把依赖 shade 进 jar（ModTheSpire 模组是单个 jar，引入 WS 库必须 shade）。本机点对点、单客户端，裸 TCP 足够。

**为什么 agent 主动连入 mod**：游戏生命周期不受 agent 影响；agent 崩溃或重启不会导致游戏起不来，mod 侧看门狗兜底（见 [03-mod-protocol](03-mod-protocol.md#看门狗)）。这与 CommunicationMod 的「游戏反向启动子进程」相反，是刻意的。

## 事件循环模型

`spire-agent` 是**单进程同步程序**（没有 asyncio、没有 fastapi/uvicorn —— 唯一外部依赖是 `httpx`）：

| 线程 | 职责 | 阻塞点 |
|---|---|---|
| 主循环（`AgentRunner.run`） | 读 observation -> 决策 -> 发 action -> 落盘 | 等 mod 的 observation；等 Laya 的 HTTP 响应 |
| `ModBridge._read_loop` | 只做"读一行 -> 塞队列"，绝不在读线程里做网络写 | `socket.recv` |
| 面板 `ThreadingHTTPServer` | 每个请求一个线程，只读地吐 `GET /api/state` 的 JSON 快照 | 等浏览器 |

所以：

- 主循环在等 Laya（最多几秒）时，面板仍能实时刷新，不会因为模型慢而卡住 UI。
- 面板只读，**绝不向 mod 发动作**，避免与主循环抢状态机。
- 跨线程共享的状态只有桥接内的 `queue.Queue` 与面板的快照字典，两者都用锁或原子操作保护。

并发多局留到后续版本。

## 一次决策的完整时序

以战斗内出一张牌为例：

1. **mod**：主更新循环检测到「动作队列空闲 + 无动画中的非 idle 界面」-> 判定稳定态。
2. **mod -> agent**：`observation{seq:412, raw:{...}, stable:true}`。`raw` 是**未过滤**观测。
3. **agent.Recorder**：把 `raw` 追加进 `runs/<run_id>/raw_states.jsonl`（若 `record.save_raw_states`）。
4. **agent.Runner**：`core.fairness.filter(raw)` -> 公平视图。
5. **agent.Runner**：`core.decision.identify(fair)` -> `DecisionPoint.COMBAT_PLAY`。
6. **agent.Runner**：`core.candidates.enumerate(fair, point)` -> 候选列表，例如
   `["play:h0->m0", "play:h1->m0", "play:h2", "potion:p0->m0", "end_turn"]`。
7. **agent.Runner**：`core.questions.build(fair, point, candidates)` -> `state` + `questions`
   （一道 `q_action` 的 `choice` 题，criteria 为各候选的英文描述）。
8. **agent.Runner**：`core.budget.enforce(state, questions)` -> 断言并压缩至 Laya 硬预算内。
9. **agent.LayaClient**：`POST /v1/systemone {state, questions}`（带 Bearer；带 `(state_hash, questions_hash)` 缓存）。
10. **Laya**：返回 `{model, answers:{q_action:{...}}, usage, routing}`。
11. **agent.Runner**：`core.arbitrate.pick_choice(...)` -> `"play:h1->m0"` + 置信度 + 全概率。
12. **agent.Recorder**：把这一行写进 `runs/<run_id>/pending/<room_key>.jsonl`（**未提交**）。
13. **agent.PanelServer**：通过 WebSocket 把这一步推给浏览器。
14. **agent -> mod**：`action{id:88, kind:"play_card", args:{hand_index:1, target:"m0"}}`。
15. **mod**：白名单校验 -> 执行（走游戏 `GameAction` 队列）-> 回 `action_result{id:88, ok:true}`。
16. 回到第 1 步。若模型选择 `end_turn`，则 mod 结束回合，随后是新一回合的稳定观测。

**注意第 4-7 步的统一性**：出牌后若游戏弹出「选择目标」或「选择要弃掉的牌」，那只是**下一个稳定观测**，会被识别成一个新的决策点（`select_target` / `select_card_*`），由同一个子选择裁决器处理。**不需要在出牌那一题里预判后续**。

## 失败与恢复

| 失败 | 表现 | 恢复 |
|---|---|---|
| Laya 超时 / 5xx | `LayaClient` 返回 `None` | 重试 3 次指数退避（单次 5s 超时）；仍失败则按 `laya.on_error`：**默认 `stop`，退出码 4 停跑**（不替模型猜）；配 `fallback` 才走保守规则并标 `agent_fallback: true` |
| agent 启动时连不上 Laya | `preflight` 探针失败 | 退出码 3，**不连游戏**（避免白烧一局）；`--skip-preflight` 跳过 |
| 模组对不上话（协议版本不符 / 拒绝 `configure`） | `IncompatibleMod` | **不重连**，退出码 5 停跑（`result=mod_incompatible`）。重试解决不了，提示重新构建安装模组 |
| agent 崩溃 | 不再发动作 | **mod 看门狗**超 `watchdog_sec`（默认 30s）-> 执行安全默认动作（战斗内结束回合；非战斗点继续/第一个选项），游戏不卡死 |
| agent 与 mod 断连 | 读流结束 | agent 指数退避重连（1s -> 2s -> 4s -> … 上限 30s），重连后继续当前局 |
| mod 收到非法动作 | `action_result.ok=false` | 同一决策点用**次优候选**重试一次；再失败走 mod 看门狗兜底 |
| 状态回退（人类读档） | **局内** `room_token` 重访，或**战斗中**（前后都在战斗且仍有活怪）`turn` 倒退；模组读档 hook 只在"本会话已进过房间"后算数；观测 `seq` 归零（游戏重启）+ 种子没变 ⇒ 续玩 | 丢弃当前房间 `pending`，`combat_instance` 自增，该房间标 `post_sl`，从头重记；记入 `meta.json.sl_events` 并推 `sl_event` 到面板 |

## 模块映射

```
packages/spire-core/src/spire_core/
    config.py      配置模型与加载（纯：只吃 dict）
    model.py       观测 / 公平视图 / 实体模型
    fairness.py    公平过滤器
    serialize.py   公平视图 -> 英文紧凑 state
    budget.py      Laya 硬预算断言与确定性压缩
    candidates.py  候选枚举
    decision.py    决策点识别
    questions.py   题面构造（读 decision_points.toml 模板；tomllib 是标准库）
    arbitrate.py   答案解析、choice / score-topk 裁决
    types.py       LayaResult / ActionResult / Decision 等值类型
    actions.py     候选 id <-> 语义动作 的双向映射
    dataset.py     训练行构造
    sl.py          状态回退 / SL 检测（纯逻辑）
    replay.py      从 decisions.jsonl 确定性重建构题现场

packages/spire-agent/src/spire_agent/
    bridge.py      mod 桥接（NDJSON 编解码、重连、心跳）
    laya_client.py Laya HTTP 客户端（重试/缓存/统计）
    recorder.py    落盘、pending/commit/rollback（有 IO）
    runner.py      主决策循环
    panel.py       只读观战面板（标准库 http.server + 每秒轮询的静态单页）
    fake_laya.py   假 Laya 服务（标准库 HTTP，供单测与 tools/laya_health.py --selftest）
    cli.py         命令行入口
    __main__.py    python -m spire_agent

mod/src/main/java/spireagent/
    SpireAgentMod.java        入口（@SpireInitializer）
    bridge/NdjsonServer.java  loopback TCP + NDJSON（纯 JDK）
    bridge/Envelope.java      信封编解码（纯）
    proto/ActionSpec.java     语义动作白名单与校验（纯）
    proto/Errors.java         错误码（纯）
    obs/StabilityGate.java    稳定态判定（纯逻辑部分）
    obs/Observer.java         观测采集（依赖游戏类）
    act/Actor.java            动作执行（依赖游戏类）
    act/HumanActionTap.java   人类动作捕获（依赖游戏类）
    Watchdog.java             看门狗（纯）
```

## 相关文档

- 线协议字段 -> [03-mod-protocol](03-mod-protocol.md)
- 决策点与候选 -> [06-decision-points](06-decision-points.md)

# 09 可观测性

## 观战面板

本地只读面板，默认 `http://127.0.0.1:8788`。**只读**——它永远不向 mod 发动作，避免与主循环抢状态机。

技术：标准库 `http.server`（`ThreadingHTTPServer`）+ 一个无构建步骤的静态单页（原生 JS，无 npm/bundler）。
所以除了 `httpx`，agent 不需要任何第三方依赖 —— 面板刻意不进依赖树。

### 布局

```
┌──────────────────────────────────────────────────────────────────────┐
│ 状态条：IRONCLAD A0 | Act 1 Floor 7 | HP 68/75 | 金币 142 | 药水 2/3  │
│         模式 agent | Laya: english ✓ 42ms | 连接: mod ✓ | SL 事件 0   │
├────────────────────────────┬─────────────────────────────────────────┤
│ 当前局面                    │ 本步决策                                │
│ ─ 手牌（含费用/描述）        │ 决策点: combat_play                      │
│ ─ 敌人（HP/意图/伤害）       │ 候选 + 概率条（降序，选中项高亮）        │
│ ─ 抽牌堆/弃牌堆（多重集）    │ Laya 置信度 / 是否 fallback / 延迟       │
│ ─ 遗物 / 药水                │ 最终动作 + mod 执行结果                  │
│ ─ 地图（当前可达高亮）       │                                         │
├────────────────────────────┴─────────────────────────────────────────┤
│ 决策时间线（最近 30 条，可点开看当时 state/questions 原文）             │
├──────────────────────────────────────────────────────────────────────┤
│ 曲线：HP / 楼层；计数：Laya 调用数、fallback 次数、缓存命中、平均置信度  │
└──────────────────────────────────────────────────────────────────────┘
```

### 事件流

上面那张图是**目标形态**。当前实现是六张卡片（局面 / 最近决策 / 计数 / 等待人类 / 牌局 /
Laya 请求返回）+ 一条时间线，还差概率条与曲线 —— 完整现场仍然在 `runs/<run_id>/`。

**牌局**那张卡片渲染的就是**送给模型的公平 `state` 原文**（agent 侧
`serialize.state(fair)` 的输出），所以观战者看到的不会比模型多、也不会比模型少：

- 能量 / 格挡 / 金币、遗物（含 `counter`）、药水（空槽只留槽位数）、powers
- 敌人（名称 / HP / 格挡 / 当前意图 + 伤害 / 其 powers）
- 手牌（位置短 id、名字、费用、类型、稀有度、目标类型）
- 抽牌堆 / 弃牌堆 / 消耗堆 / 主牌组（全部是**多重集**，与模型看到的一致）
- 地图可达节点、界面选项、奖励卡、商店（名字 + 价格 + 买不起标记）

"最近决策"多了一行**来源**：`模型` 或 `强制（唯一合法动作，未问模型）`
（见 [06](06-decision-points.md#强制决策短路)）。

### Laya 请求 / 返回（点开看原文）

**这张卡片回答的就是"这一步决策到底发了什么、拿回了什么"。** 每一行是一次真实的往返：
时间、`seq`、决策点、请求的 checkpoint、实际的 HTTP 结果（或"缓存命中"/"没连上"）、延迟、
请求体/返回体大小，以及模型答了什么。点一行，下面展开**原样的 JSON 原文**：

- **请求**标签页 = 线上真正 POST 出去的 body，也就是 `{state, questions, model}` 逐字节那一份；
- **返回**标签页 = 服务器原样返回的 JSON（`model` / `answers` / `usage` / `routing`）。

默认勾着**跟随最新**，跑局时最新的请求自己滚进来；点任意一行就停止跟随，可以慢慢读。
四种往返都会被记下来：**成功**、**缓存命中**（`status` 留空，`cached=true`，因为这一问没有真的花钱）、
**失败**（连不上 / 非 200 / 返回体解不开，带 `error` 与截断后的返回原文）、以及
**超预算拒发**（请求根本没发出去，但请求体照样记 —— 超预算往往正是"请求构造坏了"的第一现场）。
重试的每一次尝试各占一行。

存储上刻意分成两层，因为 `/api/state` 每秒被轮询一次：

- `/api/state` 里的 `exchanges` 只有**摘要**（上面那些字段，不含请求体与返回体）；
- 原文留在面板的环形缓冲里（`panel.exchange_size`，默认 20 条），按需取：
  `GET /api/exchange/<id>` → 该次往返的完整记录，被挤出缓冲则 404。

请求体上限是 2MB（见 [07](07-laya-contract.md#预算)），所以这两层必须分开 —— 否则光是轮询就能把
浏览器和 agent 一起拖死。**面板是观测，不在决策链上**：钩子炸了只写一行 warning，这一局照走。

两个容易误读的地方：

- **界面 `NONE`**：那是游戏自己的 `AbstractDungeon.screen`。普通战斗里它**就是** `NONE`
  （没有弹出式界面），所以面板显示 `NONE（战斗内）`；不在战斗里则显示 `NONE（无特殊界面）`。
  `NONE` 不代表"没读到局面"，房间那行（`MONSTER #1`）才说明在哪。
- **面板是 agent 进程自己提供的**：agent 一退出（Ctrl-C、关掉宿主终端/Codex 都一样），
  8788 就没了。页面会把数字变暗、在"计数"里写 `agent 已停止`，并说明重启后会自动恢复 ——
  那不是网络故障，别去查代理。

`observe_human` 模式下，"本步决策"区换成**人类提示**：候选集 + 模型对照答案（`| 模型: <answer>`），
人类一提交就清空。标签质量因此可以在面板上肉眼核对。

agent 把 JSON 事件写进内存环形缓冲（时间线最近 `panel.timeline_size` 条，请求原文最近
`panel.exchange_size` 条），浏览器每秒轮询 `GET /api/state` 拿快照，需要原文时再取一次
`GET /api/exchange/<id>`（单向，浏览器只发 GET、从不发动作）：

| 事件 | 时机 | 载荷 |
|---|---|---|
| `run_start` | 新局 | `{run_id, character, ascension, seed}` |
| `observation` | 收到稳定观测 | `{seq, fair_state}` |
| `decision` | 完成一次决策 | `{seq, decision_point, candidates, answers, chosen, confidence, fallback, latency_ms, cache_hit}` |
| `exchange` | 每一次 Laya 往返（成功 / 缓存 / 失败 / 超预算拒发，重试每次各一条） | `{id, seq, decision_point, checkpoint, ok, status, cached, error, latency_ms, request, response, request_bytes, response_bytes, answer}` —— 时间线里只留摘要，原文走 `/api/exchange/<id>` |
| `action_result` | mod 回报 | `{seq, id, ok, error}` |
| `sl_event` | 检测到读档（`seam` 区分 `state_rewind` / `mod:<seam>` / `session_restart` / `agent_start`） | `{room_key, dropped_rows, combat_instance, seam, detail}` |
| `watchdog` | mod 看门狗触发 | `{seq, default_action}` |
| `human_prompt` | observe 模式：进入等待人类动作 | `{seq, decision_point, candidates, questions, model_answer, model_confidence}` |
| `human_action` | observe 模式：人类已提交 | `{seq, decision_point, action, matched, candidate, rows}` |
| `run_end` | 局终 | `{run_id, won, floor, hp}` |

浏览器连上时先收到一次 `snapshot`（当前状态 + 最近 N 条决策），之后就增量。

### 降级行为

Laya 不可达时**默认直接停跑**（`laya.on_error = "stop"`），不替模型做决定：

- **启动预检**：`spire_agent run` 先发一道探针题；过了才开跑，没过则打印 `ERROR Laya 预检没过，已停止`，
  退出码 **3**，**不连游戏**（`--skip-preflight` 跳过，`preflight = false` 关闭）。
- **决策中途失败**：`laya_client` 单次超时 5s、重试 3 次、指数退避（0.5s / 1s）；全失败后返回 `None`，
  于是抛 `ModelUnavailable`，主循环捕获后退出码 **4**、`summary.json.result = "model_unavailable"`。
  **一个兜底动作都不发** —— 没模型参与的对局不值得跑完。
- `mode=agent` 且没有 Laya 客户端：`start()` 阶段就拒绝（退出码 4）。
- observe 模式下 `also_query_model=true` 而 Laya 挂掉时**不影响采集**：人类标签照记，只是那一步没有
  `model_answer` / `agreement` 对照字段（不会产生假的对照数据）。
- 想回到"规则兜底把这一局打完"的旧行为：`[laya] on_error = "fallback"`。那样产出的行
  `label_source=fallback`、`agent_fallback=true`、`answers={}`，**导出时默认排除**。

**退出码约定**（跑批脚本靠它判断"跑成没跑成"）：`0` 正常结束、`2` 连不上 mod、
`3` Laya 预检失败、`4` 模型不可用、`5` 模式不一致、`130` Ctrl-C。

**看门狗**：agent 超过 mod 的 `watchdog_sec`（默认 30s）没发动作，mod 自己执行默认动作并回
`action_result.watchdog=true`——这种行**不落盘**，只累加 `summary.json.watchdog_events`。
mod 连接断开时 client 指数退避重连。

**当前可见的降级信号**：`decision.fallback`（面板标红）、`summary.json.fallback_rate`、
日志里的 `laya call failed (attempt N)`。注意 `LayaClient.stats`（calls / cache_hits / retries /
failures）目前只在进程内，没有落进 `summary.json`。

> **尚未实现**（见 [12-roadmap](12-roadmap.md)）：`laya_status` 健康探测事件、状态条上的
> "Laya 连续 N 次失败 / 401" 红色告警、以及 `spire_agent run` 启动时的 Laya 预检。

## 日志

| 输出 | 内容 | 级别 |
|---|---|---|
| 控制台 | 主循环关键事件（新局、局终、重连、兜底） | INFO |
| `runs/<run_id>/decisions.jsonl` | 每次决策一行（含 `state`/`questions`/`answers` 原文） | 数据 |
| `runs/<run_id>/pending/<room_key>.jsonl` | 当前房间未提交的行（SL 回滚的作用域） | 数据 |
| `runs/<run_id>/raw_states.jsonl` | 原始未过滤观测 | 数据 |
| `runs/<run_id>/summary.json` | 局终汇总 | 数据 |
| `spire_agent.log` | 全量 DEBUG 日志（`--log-level`） | DEBUG |

游戏侧日志：ModTheSpire 控制台窗口 + 游戏日志文件。mod 的关键事件前缀 `[spire-agent]`，看门狗用 `[watchdog]`。

## 指标口径

写进 `runs/<run_id>/summary.json`：

| 指标 | 定义 |
|---|---|
| `rows_total` / `committed_rows` | 已提交行数 |
| `by_source` | human / agent 行数 |
| `by_decision_point` | 各决策点行数分布 |
| `match_rate` | observe 模式：人类动作命中候选集的比例（候选完备度） |
| `agreement_rate` | 有模型对照的行里，模型与人类一致的比例 |
| `fallback_rate` | `agent_fallback` 行数 / 总行数；默认策略下应恒为 0（只有配了 `on_error=fallback` 才会非 0） |
| `post_sl_rows` | 打上 `post_sl` 的行数（agent 模式验收要求 = 0） |
| `unique_fair_states` | 去重后的公平状态数（样本冗余度） |
| `floor_hp` | `[{floor, hp, max_hp}]`，面板底部 HP / 楼层曲线读它 |
| `watchdog_events` | mod 看门狗触发次数（验收要求 = 0） |
| `sl_events` | 读档事件列表（agent 模式验收要求为空） |
| `pending_rooms` | 局终仍未提交的房间（正常应为空，非空说明卡在房间里） |
| `floors_reached` / `result` | 到达楼层 / 局终原因 |

面板底部的曲线与计数直接读这些口径，保证**面板看到的数字与落盘的一致**。

## 相关文档

- 部署后的健康检查 -> [10-deployment](10-deployment.md#健康检查)
- 验收标准 -> [11-testing](11-testing.md#端到端验收)

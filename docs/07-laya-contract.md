# 07 Laya 调用契约

## 端点

Laya 自带 HTTP 服务（入口 `laya-serve`，等价于 `python -m laya.serve`），暴露
**TypeSafe Jev 的 `/v1/systemone` 线协议**。

```
POST {base_url}/v1/systemone
Authorization: Bearer {api_key}
Content-Type: application/json

{ "state": { ... }, "questions": { "q_action": { ... } } }
```

响应（**按 `laya 0.3.20` 的真实实现核对过**，源码是 `laya/serve.py` + `laya/agent.py`）：

```jsonc
{
  "model": "laya-rl-agent",            // 架构名，恒定 —— 不是 checkpoint
  "answers": {
    "q_action": {
      "type": "choice",
      "choice": "play:h1->m0",          // choice 题的答案：criterion 的 key
      "probabilities": { "play:h0->m0": 0.12, "play:h1->m0": 0.61, "end_turn": 0.27 },
      "confidence": 0.53,               // choice/score 是归一化熵
      "answer_confidence": 0.61,        // 校准后置信度 —— 统计一律用它
      "action": { "act_probability": 0.98 }
    }
  },
  "usage": { "input_tokens": 812, "output_tokens": 0 },
  "routing": { "model": "english", "reason": "explicit" }
}
```

答案键**按题型分派**，没有一个统一的 `answer` 字段：

| `type` | 答案键 | 说明 |
|---|---|---|
| `choice` | `choice` | criterion 的 key；`probabilities` 覆盖**全部** criterion —— 这正是我们每步都能拿到全候选分布的原因 |
| `score` | `score` | 期望等级（浮点）。裁决用 `probabilities`（按等级下标 `"0"`…`"n-1"`）自己算，不看这个字段 |
| `noul` | `noul` | P(true)。线上不给 `probabilities`，但它本来就是个二分类分布，core 补成 `{false: 1-p, true: p}` |

- **客户端必须同时读答案键和 `probabilities`**：答案键用于执行，`probabilities` 用于采集、缓存校验与后续分析。
- `confidence` 与 `answer_confidence` **含义不同**（前者对 `choice` / `score` 是归一化熵）；统计一律用 `answer_confidence`。
- `routing.model` 才是 checkpoint 名，要落盘进 `meta.json` —— 它决定了标签来自哪个 checkpoint。顶层 `model` 恒为 `laya-rl-agent`，没有信息量。
- `usage.output_tokens` 恒为 `0`：非自回归模型，不生成 token。

> 提醒：早期文档写的 `answers.q.answer` 和 `routing.checkpoint` **不是真实字段**，照着写会在
> 真机上解析失败。core 的 `arbitrate.selected_answer` 与 `LayaResult.checkpoint` 兼容这两种老写法
> （回放旧记录要能读），但新代码一律按上表来。

### 校准有适用范围（实测）

`english` checkpoint 在**选项数 >= 11** 的 `choice` 题上带的温度参数越界（落在 [0.5, 5] 之外），
`laya` 启动时会把它夹到 `0.5` 并打印：

```
RuntimeWarning: laya: this checkpoint ships invalid temperatures or values outside [0.5, 5];
using choice:11+=0.10058280825614929 -> 0.5. Treat confidence from the affected entries as uncalibrated.
```

我们的 `combat_play` 候选（每张可出牌 × 每个目标，再加结束回合）**经常超过 11 项**，所以：

- 大候选集上的 `answer_confidence` **不能当作校准概率**（它只是被夹过的温度算出来的）；
- 落盘照旧（`meta.confidence` / `meta.model_confidence`），但做阈值、比较或统计时要**按候选数分层**；
- 微调时（见 [12-roadmap](12-roadmap.md)）应重新拟合温度，这一步本来就在流程里。

## 三类题型

| type | 入参 | 语义 | 本项目用途 |
|---|---|---|---|
| `choice` | `criteria`: `{key: 描述}` | 多选一，返回每个 key 的概率 | 单选类决策（默认） |
| `score` | `criteria`: `[有序等级]` | 有序分级，返回等级分布 | 任意多选的逐候选打分 |
| `noul` | — | yes / no / unknown | 虚拟思考链的先导判断题 |

## 硬预算

来自 `laya/serve.py` 的实现，**客户端必须自己先断言**（不要让远端 413）：

| 项 | 上限 |
|---|---|
| `state` 序列化字符数 | 50000 |
| 单请求问题数 | 64 |
| 单个 choice 的选项数 | 100 |
| 所有问题选项总数 | 512 |
| `score` 等级数 | 32 |
| 请求体字节数 | 2 MiB |

`budget.enforce()` 在构题后立即校验并按 [05-state-schema](05-state-schema.md#预算与压缩) 的优先级压缩。压缩后仍超限 -> 抛 `BudgetExceeded`，该步记为异常行，**不静默截断**。

## checkpoint 选择

```toml
[laya]
model = "english"            # english | multilingual | typed-decisions | "" (让 Router 自动选)
prefer_multilingual_over_chars = 6000
```

| checkpoint | 参数量 | 上下文 | 何时用 |
|---|---|---|---|
| `english` | 421M (ModernBERT-large) | 512 | **默认**。状态已是英文，且短 |
| `multilingual` | 322M (mmBERT-base) | 1024（可开 8192） | state 超过 `prefer_multilingual_over_chars` 字符时切换，需要长上下文 |
| `typed-decisions` | 421M | 1024 | 任务偏"类型化决策"时（本项目可以整体切过去做 A/B） |

选择规则：若 `model` 显式配置则直接用；若为空，则由 `Router` 自动选。**不要**在请求里塞 `convaiinnovations/laya`（那等价于"让 Router 选"）。

> **本项目的取值**：`state` 单独就已有 **99% 超过 512 token**（中位数 1725），所以微调与部署统一按
> `max_len = 2048` 走（见 [13-finetune](13-finetune.md)）。`english` 自带的部署配置是
> `max_len=512` / `head_max_len=192`，微调时提到 2048 / 320 并在该长度上适配（ModernBERT 的
> `max_position_embeddings` 本来就是 8192）；也可以直接切 `multilingual` / `typed-decisions`（自带 1024）。

## 缓存

键：`sha256(fair_state_hash + questions_hash)`。命中则**直接复用上次的 `answers`**，不发网络请求。

- 用途：重试路径、同一状态的多问题拆分、回放。
- 缓存**仅进程内**（内存 LRU，容量可配 `laya.cache_size`，默认 512）。
- 缓存命中要落盘 `meta.cache_hit: true`，训练导出时可据此去重（同一 (state,questions) 只保留一条）。

## 重试与超时

```toml
[laya]
timeout_sec = 5
retries = 3
```

- 单次超时 5s；失败（超时 / 连接错 / 5xx / JSON 解析失败）后指数退避重试 3 次：0.5s、1s、2s。
- 4xx（除 429）**不重试**——那是我们的请求有问题，重试无意义，直接报错。
- 429 与 5xx 重试。
- 全部失败 -> 按 `laya.on_error` 处理：**默认 `stop`，立刻报错停跑**（见下）。

## 拿不到模型决策时：默认停跑，不替模型猜

```toml
[laya]
on_error = "stop"     # stop（默认）| fallback
preflight = true      # run 启动时先探一次 Laya，连不上就不开跑
```

- `on_error = "stop"`（默认）：连不上 / 解析失败 / 答案为空 / 子选择没凑够，都会
  **抛 `ModelUnavailable` 并结束这一局**。`run()` 返回退出码 **4**，`summary.json.result =
  "model_unavailable"`，日志里是那一条 `laya call failed (attempt N)`。
  **不发任何兜底动作**——产出一局"没有模型参与"的数据毫无意义，还会浪费一次真实对局。
  游戏侧不会卡死：agent 停止后由 mod 看门狗接管。
- `on_error = "fallback"`：才退回下面的保守规则策略，把这一局打完（用于实验/对比）。

`preflight = true` 时 `spire_agent run` 会先发一道最简单的探针题；失败直接退出（码 3），
**根本不会去连游戏**。`--skip-preflight` 可跳过。

探针**只有一份**（`spire_agent/probe.py`）：`run` 的预检、`doctor` 和
`tools/laya_health.py --probe` 都用它。它按真 Laya 的题型规则构题 —— 每题都带非空
`instructions`、choice 的 `criteria` 非空 —— 并且**同时校验应答形态**：choice 必须出现在
`answers[q]["choice"]` 且落在候选集里。所以"连得上但契约不对"（比如 200 却只回了老字段
`answer`）会在预检就红，而不是打到一半才发现。判定逻辑在 `test_probe.py`。

### 兜底规则策略（仅在 `on_error = "fallback"` 时启用）

**保守规则策略**（`spire-core` 里实现，纯逻辑，便于单测）：

| 决策点 | 兜底动作 |
|---|---|
| `combat_play` | 若有可出的攻击牌 -> 对**血量最低**的敌人出**伤害最高**的一张；否则若有防御牌 -> 出它；否则 `end_turn` |
| `select_target` | 血量最低的存活敌人 |
| `select_card_must_k` / `select_card_any` | 前 k 张（按 index 升序） |
| `map_node` | 第一个可达节点 |
| `card_reward` | `skip` |
| `event_option` / `generic_choice` | 第 0 项 |
| `shop` | `leave` |
| `rest_site` | `rest:heal`（HP 低于最大值的 60%），否则 `rest:smith` |
| `relic_select` / `neow_bonus` | 第 0 项 |

兜底产生的行：
- `meta.agent_fallback = true`、`meta.fallback_reason = "laya_timeout" | "budget_exceeded" | "parse_error" | ...`；
- **模型字段留空**（`model_answer = null`），因为确实没有模型参与；
- `dataset.exclude_fallback_rows = true`（默认）导出时排除。

## 健康检查

`tools/laya_health.py` 做两件事：

1. `GET {base}/health` -> 期望 200；
2. `POST {base}/v1/systemone` 发一道最简单的 `choice` 题（`{"state": "ping", "questions": {"q": {"type":"choice","instructions":"Answer a.","criteria":{"a":"a","b":"b"}}}}`）-> 期望 200 且 `answers.q.choice` 存在。

输出人类可读的延迟与 routing，用于部署验收（见 [10-deployment](10-deployment.md#健康检查)）。

## 与 Jev 协议的关系

Laya 的 `predict()` 输出与 TypeSafe Jev 的决策 API schema 兼容，`laya.serve` 只是补上了 HTTP 外壳。因此：

- 我们可以直接用现成的 Jev 客户端格式（如 `hs-jev`），但我们用自己的 `LayaClient`（Python，需要缓存/重试/统计）。
- **不要**在请求里传 Jev 的 `model: "jev-1"` 之类——`laya.serve` 会忽略未知 model 并交给 Router 自动选。

## 错误处理

| 情况 | 行为 |
|---|---|
| 连接被拒 / DNS 失败 | 重试 3 次 -> `on_error=stop` 时退出码 4 停跑；`fallback` 时用规则兜底 |
| 401（key 错） | **不重试**，直接致命错误：退出码 4（配置问题），不会降级 |
| 413（超预算） | 说明 `budget.enforce` 漏了，记 `E_BUDGET` 并**上报为 bug 级日志**（这是实现缺陷，不是运行时噪声） |
| 200 但 `answers` 缺该题 | 记 `parse_error` -> 同上（默认停跑） |
| 200 但 `choice` 不在 criteria 里 | 同上（**不要**做字符串近似匹配） |

## 相关文档

- 题怎么构造 -> [06-decision-points](06-decision-points.md)
- 部署与鉴权 -> [10-deployment](10-deployment.md)

## 看原文：每一次往返都留得下来

契约调试（尤其"我构造的请求到底长什么样"）不需要抓包：`LayaClient.on_exchange` 会把**每一次**往返
的原始记录回调出去（成功 / 缓存命中 / 每次重试失败 / 连不上 / 超预算拒发），带上原样 POST 出去的
body 与服务器原样返回的 JSON。观战面板把它做成"点开看原文"的卡片 —— 见
[09-observability](09-observability.md#laya-请求--返回点开看原文)。

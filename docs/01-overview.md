# 01 总览

## 一句话

用 **Laya**（非自回归判断模型）全自动打完一局《杀戮尖塔》一代 **v2.3.4**，并把每一步决策按「可直接微调」的形态落盘。

## 目标与非目标

### v1 目标

- **全自动**：自动开新局，打到死亡或通关，自动重开下一局，全程无人工干预。
- **严格公平**：模型只看到游戏内玩家可见的信息（见 [04-fairness](04-fairness.md)）。
- **数据可用**：每次决策落盘成 `(state, questions, labels)` 训练行，可直接用于 Laya 微调（见 [08-dataset](08-dataset.md)）。
- **可调试**：本地只读观战面板 + 结构化日志。

### 非目标（v1 不做）

| 不做 | 原因 |
|---|---|
| OCR / 无模组通道 | 只预留 `ObservationSource` / `ActionSink` 抽象；见 [12-roadmap](12-roadmap.md) |
| 在线微调 | v1 只产数据集 |
| 并发多局、多实例 | 单实例串行，先跑通 |
| TLS 终结 | 由部署方（反代 / 隧道）负责 |
| SL（save-scum） | **默认禁止**，见 [08-dataset](08-dataset.md#sl-语义) |
| 虚拟思考链 | 架构留钩子，v1 关闭 |

## 为什么是这个架构

**Laya 不生成动作**。它的输出只是「对给定问题的判断」（`choice` / `score` / `noul` 三类题，单次前向出概率）。因此系统必须自己承担三件事：

1. **观测**：把游戏状态变成结构化数据。
2. **构题**：枚举候选动作，把「该做什么」翻译成 Laya 能回答的类型化问题。
3. **执行**：把判断结果翻译回游戏动作。

这三件事是本项目的主要工程量，全部集中在 `spire-core`。

## 三段式

```
┌──────────────────────┐        ┌────────────────────────┐        ┌──────────────────┐
│  Java 模组            │ NDJSON │  Python 智能体          │  HTTP  │  Laya 服务        │
│  (ModTheSpire)        │◀──────▶│  (spire-agent)         │◀──────▶│  (/v1/systemone) │
│                       │  TCP   │                        │        │                  │
│  · 稳定态检测          │ 本机   │  · 桥接 / 重连 / 心跳    │        │  · 官方 checkpoint│
│  · 原始观测上报        │        │  · 采集（pending/commit）│        │  · Router 选模型  │
│  · 语义动作执行        │        │  · 观战面板             │        │                  │
│  · 动作白名单校验      │        │  · 主循环 / 自动重开     │        └──────────────────┘
│  · 看门狗兜底          │        │        │               │
└──────────────────────┘        │        ▼               │
                                │  ┌──────────────────┐  │
                                │  │  spire-core（纯逻辑）│ │
                                │  │  公平过滤 → 序列化   │ │
                                │  │  → 决策点 → 候选枚举 │ │
                                │  │  → 构题 → 解析 → 裁决│ │
                                │  └──────────────────┘  │
                                └────────────────────────┘
```

### 职责边界

| 组件 | 负责 | 不负责 |
|---|---|---|
| **mod**（Java 8） | 游戏内观测采集、语义动作执行与校验、看门狗兜底、人类动作捕获 | 任何"聪明"的判断；不 import 模型；不做状态过滤 |
| **spire-core**（纯逻辑） | 公平过滤、英文序列化、决策点识别、候选枚举、构题、答案解析、子选择裁决 | IO、网络、进程、文件 |
| **spire-agent**（运行体） | mod 桥接、Laya HTTP 客户端、主循环、采集落盘、SL 回滚、观战面板、自动重开 | 决策语义（全部委托 core） |
| **Laya 服务** | 对类型化问题返回带概率的答案 | 动作枚举、状态构造 |

`spire-core` **不 import httpx / socket / pathlib 写操作**，保证可以整包搬到远端复用。

## 数据流（一次决策）

```
mod 检测到稳定态
  └─▶ observation{seq, raw}  ──NDJSON──▶  agent
                                          ├─ core.fairness   : raw → 公平视图
                                          ├─ core.decision   : 识别决策点
                                          ├─ core.candidates : 枚举候选
                                          ├─ core.questions  : 构题
                                          ├─ core.budget     : 预算断言/压缩
                                          ├─ LayaClient      : POST /v1/systemone
                                          ├─ core.arbitrate  : 概率 → 选择
                                          └─ Runner          : 记录 + 组装动作
                                                                 │
                                          action{id, kind, args} ◀┘
  ┌────────────────────────────────────────────────────────────────┘
  ├─ 白名单校验 → 执行 → action_result
  └─ 循环
```

## 两种运行模式

两种模式**共用同一套观测 / 公平过滤 / 候选枚举 / 构题 / 记录管线**，只在「谁做决定」上分叉。

| | `agent`（默认） | `observe_human` |
|---|---|---|
| 谁操作游戏 | Laya（agent 发动作） | 人类 |
| agent 做什么 | 观测 → 构题 → 问 Laya → 执行 | 观测 → 构题 → **等人** → 记录人类选择 |
| 是否问 Laya | 必须 | 可选（`observe_human.also_query_model = true`，同时记录 `model_answer` 做对照） |
| 标签强度 | 弱（`label_source: agent`） | 强（`label_source: human`） |
| 用途 | 跑通闭环、产自博弈数据 | 产模仿学习主料 |

关键：两种模式产出的训练行**格式完全一致**，因此可以混训、可以按 `source` 过滤。

## 配置总览

全部配置集中在一个 TOML 文件（见 `agent/config.example.toml`）。关键项：

```toml
[agent]
mode = "agent"              # agent | observe_human
character = "IRONCLAD"
ascension = 0
allow_save_scum = false
watchdog_sec = 30
fairness_mode = "strict"    # strict | omniscient

[mod]   host/port          # mod 的 loopback TCP 服务地址（模组侧唯一需要配的东西）
[laya]  base_url/api_key/model
[panel] enabled/host/port   # 只读观战面板
[dataset] exclude_post_sl_rooms/exclude_fallback_rows/切分比例
```

**模式只有一个来源。** 模组没有自己的模式开关：agent 握手后把 `mode` 与
`watchdog_sec` 推给模组（协议 v2 的 `configure` 帧），在那之前模组是哑的。
所以切模式 = 换个 `mode` 重跑 agent，既不用改模组配置也不用重启游戏。

## 上游依赖

| 依赖 | 获取方式 |
|---|---|
| Slay the Spire v2.3.4 | Steam（本机已装：`C:\Program Files (x86)\Steam\steamapps\common\SlayTheSpire`） |
| ModTheSpire | Steam 创意工坊 `1605060445` |
| BaseMod | Steam 创意工坊 `1605833019` |
| 本项目建设物 | `tools\build_mod.ps1` → 复制到游戏 `mods\` |
| Laya | 本机或远端 `pip install laya[serve]`；本机一键脚本见 [10-deployment](10-deployment.md#本机部署windows) |

## 相关文档

- 组件职责与拓扑细节 → [02-architecture](02-architecture.md)
- 模型能看到什么 → [04-fairness](04-fairness.md)
- 训练行长什么样 → [08-dataset](08-dataset.md)

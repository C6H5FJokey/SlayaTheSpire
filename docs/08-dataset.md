# 08 数据集与采集规范

本文是**微调的契约**。v1 不训练，但采集格式必须一次做对——否则后期要重跑几百局。

## 训练单元

与 Laya 的可训单元严格对齐：

```
(英文 state, typed questions)  ->  answer
```

所以我们**不存游戏录像**，而是存**每次决策点的完整构题现场 + 标签**。训练时把 `state` 和 `questions` 原样喂进去，把 `labels` 当作目标即可，不需要任何重放或重建。

## 训练行格式

`dataset/*.jsonl`，一行一个样本（JSON，UTF-8，无缩进）：

```jsonc
{
  "row_id": "run-0007#c3#t2#q1",
  "run_id": "run-0007",
  "seq": 412,
  "decision_point": "combat_play",

  "room":    { "act": 1, "floor": 7, "node": 5, "type": "MONSTER",
               "combat_instance": 1, "post_sl": false },
  "context": { "character": "IRONCLAD", "ascension": 0,
               "game_version": "2.3.4", "fairness_mode": "strict" },

  "source": "human",          // human | agent
  "split":  "train",          // 按 run 切分，写死在行内

  "state":     { /* 送给 Laya 的英文公平 state，原样，逐字节可复现 */ },
  "questions": { "q_action": { "type": "choice",
                               "instructions": "...",
                               "criteria": { "play:h2->m0": "...", "end_turn": "..." } } },

  "candidate_ids": ["play:h2->m0", "play:h0->m0", "end_turn"],
  "labels":        { "q_action": "play:h2->m0" },

  "meta": {
    "label_source": "human",           // human(强) | agent(弱)
    "human_action": { "kind": "play_card", "args": { "hand_index": 2, "target": "m0" } },
    "matched": true,                   // 人类动作是否命中我们枚举出的候选集
    "unmatched_reason": null,          // matched=false 时填，见下
    "model_answer": "play:h0->m0",     // observe 模式可选：同时问模型做对照
    "model_confidence": 0.61,
    "agreement": false,                // model_answer == labels
    "latency_ms": 84,
    "usage": { "input_tokens": 812, "output_tokens": 0 },
    "routing": { "model": "english" },
    "cache_hit": false,
    "agent_fallback": false,
    "fallback_reason": null,
    "truncated_candidates": [],
    "degenerate": false                // 只有一个候选（无选择余地）
  },

  "outcome": { "run_won": null, "hp_after": 52, "hp_delta_room": -11, "floor_reached": null }
}
```

### 字段说明

| 字段 | 用途 |
|---|---|
| `row_id` | `{run_id}#c{combat_instance}#t{turn}#q{n}` 便于人读与去重 |
| `split` | **写死在行内**，按 run 切分，绝不按行随机切（见下） |
| `state` / `questions` | 训练输入，**逐字节可复现**（序列化必须是纯函数） |
| `candidate_ids` | 候选全集（含未被选中的），用于 `choice -> noul` 展开与负样本 |
| `labels` | 训练目标，与 `questions` 的答案空间一致 |
| `meta.label_source` | 决定训练权重与过滤 |
| `meta.matched` | 候选集完备性的直接反馈 |
| `outcome` | 局终回填；支持按"胜局/好局"加权 |

## 标签来源与分级

| 来源 | `label_source` | 强度 | 说明 |
|---|---|---|---|
| 人类操作 | `human` | **强** | 模仿学习主料；人类在公平信息下的选择就是我们要的行为 |
| Laya 自博弈 | `agent` | 弱 | 只能做自蒸馏/一致性训练，或配合 `outcome` 做加权的强化 |
| 兜底策略 | — | 无 | `agent_fallback=true`，**默认不进训练集** |

一条重要的用法：**`meta.agreement`**（模型与人类是否一致）是现成的评测信号。`observe_human.also_query_model = true` 时零成本获得——可以直接用它评估"未微调模型在某角色/某幕上的人类一致率"。

## 候选命中匹配

`mode=observe_human` 时，人类动作必须映射回候选集：

```python
label = actions.candidate_id_of(human_action, candidates)
matched = label is not None
```

- `matched = true` -> `labels` 填该候选 id。
- `matched = false` -> `labels` 留空，`meta.unmatched_reason` 记原因：

| `unmatched_reason` | 含义 | 说明这是 bug 还是一次有价值的发现 |
|---|---|---|
| `not_enumerated` | 人类的动作语义我们没枚举（例如某个我们没考虑的 `target_type`） | **发现**：候选枚举器有缺口 |
| `unknown_kind` | 人类动作类型不在我们的动作白名单里 | **发现**：协议漏了动作 |
| `stale_observation` | 人类在两次观测之间连做了多个动作 | 正常（如连续点选），记 `unmatched_reason` 但不视为缺口 |

`matched=false` 的行**不进训练集**，但它们写进 `runs/<run_id>/unmatched.jsonl` 单独保留。**这是改进候选枚举器的第一手资料，不能丢。**

## 一次采集，两种问答形态

`choice` 行可以在训练时**无损展开**成 N 条 `noul` 行（选中的 `true`，其余 `false`），不需要额外采集：

```
choice 行: labels.q_action = "play:h1->m0", candidate_ids = [h0->m0, h1->m0, end_turn]
  ↓ 展开（dataset.py 的 expand_choice_to_noul）
3 条 noul 行: "Is 'play:h0->m0' the right action?" -> false
              "Is 'play:h1->m0' the right action?" -> true
              "Is 'end_turn' the right action?"    -> false
```

价值：免费的均衡二分类样本，且与 `choice` 共享同一份 `state`/`questions` 缓存。采集时**只存紧凑的 `choice` 形态**，展开在训练侧做。

## 切分防泄漏

**按 run 切分，不按行切分。** 同一局的所有行必须落在同一个 split 里，否则相邻行几乎相同的状态会同时出现在 train 和 val，指标虚高。

```toml
[dataset]
val_ratio = 0.1
test_ratio = 0.1
split_seed = 20260927
```

算法（确定性）：

```python
def split_of(run_id: str) -> str:
    h = sha256(f"{split_seed}:{run_id}".encode()).digest()
    x = int.from_bytes(h[:4], "big") / 2**32
    if x < test_ratio: return "test"
    if x < test_ratio + val_ratio: return "val"
    return "train"
```

- 纯哈希、无状态、可重复：新增 run 不会打乱已有 run 的归属。
- `manifest.json` 记录：`split_seed`、各 split 的 run 数与行数、以及**每个 run 归属哪个 split**（便于审计）。

## SL 语义

- **agent 默认禁止 SL**：动作白名单里没有读档/重开，协议层就无法 SL（见 [03-mod-protocol](03-mod-protocol.md#语义动作白名单)）。
- **交易边界是「当前房间节点」**：未提交的行全在 `runs/<run_id>/pending/<room_key>.jsonl`。
- **提交**：**真的走进了另一个局内房间**时，把 pending 追加进 `decisions.jsonl` 并清空。
  局外观测（`act`/`floor` 都是 0：主菜单、选人、加载中、结算）**不算离开节点**——
  玩家在战斗中退到主菜单时 `current_room_key` 会漂到那个空桶上，真正的房间必须还留着 pending
  等着被 SL 回滚。
- **检测 SL**（`sl.py`）：
  - 主信号：mod 上报读档事件（hook 存档读入路径）。**只在"本会话已经进过局内房间"之后才算数**：
    `CardCrawlGame.loadPlayerSave` 就是"从主菜单开始/继续一局"那条路径，每次启动游戏都会响一次
    （实测一局里 10 次会话启动 = 10 笔假 SL），开局那一次必须忽略。
  - 兜底信号：**状态回退**，只有两条，且都必须是"没别的解释"才成立：
    1. **局内**的 `room_token=(act, floor, node)` 又出现了一次（节点不会被重复访问，重访只可能是读档）；
    2. `combat.turn` 相对上一次观测**倒退**，前提是两次观测**都在战斗中**且当前快照里**仍有活怪**。
- **`(0,0,0)` 不是房间**：主菜单/选人/结算画面的 `act`/`floor` 都是 0，而且在一局里反复出现。
  它绝不能进"见过的节点"集合，否则"回主菜单再开一局"就是一次假 SL（真机一局误报 9 次）。
  判断写在 `RoomToken.is_room`。
- **检测器的作用域是一个游戏会话**：观测 `seq` 归零（= 游戏重启）时 agent 换一个新的
  `RoomTracker`。跨会话留着 `_seen`，结算画面停在同一个 boss 节点上就会反复误报
  （真机一局 9 次）。同一次会话重启同时是**读档续玩**的判定点：种子没变、且上一局没结束
  ⇒ 玩家退出了游戏又继续，按 SL 处理。
- **agent 自己重启**（被杀/崩溃）时，同一 run 目录里剩下的 pending 整批作废
  （`seam=agent_start`）：那一局的轨迹已经断了，而且同一个 `room_key` 会让新会话的行
  追加进同一个文件。上一局还没结束 ⇒ 下一间房按"续玩"标 `post_sl`。
- **`turn` 倒退必须有那两个前提**（真机踩过的坑，别再简化）：游戏在**战斗刚结束那一帧**会把
  `GameActionManager.turn` 重置回 1，紧随其后才是奖励界面。裸比较 `turn` 会把"刚好打赢一场"
  误判成读档，整房 pending 被丢——实测一局里 11 次误报、丢 123 行，`combat_play` 一条不剩。
  同理**不能**拿玩家 HP（战斗内治疗、战后回血遗物会合法上升）或怪物 HP（召唤类敌人会让血量上升、
  按位置比较会被插入的召唤物打乱）当信号。
- **处理（按需求）**：丢弃当前房间的 pending 行，`combat_instance` 自增，从房间开头重新记录。前序房间的已提交数据不受影响。
- **附加标记**：重记的那一间房打 `room.post_sl = true`。

### 为什么 `post_sl` 要单独标记

读档后人类**已经知道该战斗的抽牌顺序和敌人后续行动**（他刚打过一遍）。此时的"人类决策"含非公平信息，作为模仿学习标签会污染模型。因此：

```toml
[dataset]
exclude_post_sl_rooms = true    # 默认：仅影响训练导出，不删原始记录
```

`exclude_post_sl_rooms = false` 可用于研究"被污染的程度"。

**agent 模式下若出现 `post_sl = true` 属于异常**——agent 不会 SL，出现即说明有人手动干预，agent 记 `WARN` 并写入面板。

## 落盘布局

```
runs/<run_id>/
    meta.json            版本、Laya 端点与 routing、角色/进阶/种子、sl_events、
                         起止时间、结果、watchdog_events、laya 调用统计
    pending/<room_key>.jsonl   当前房间未提交行（SL 回滚作用域）
    decisions.jsonl      已提交的实时决策日志（调试用；训练行在导出时生成）
    unmatched.jsonl      matched=false 的行（候选枚举器的改进素材）
    raw_states.jsonl     原始未过滤观测（离线全知实验用，绝不进训练集）
    summary.json         楼层/HP 曲线、调用数、fallback 率、平均置信度
    EXCLUDED.txt         存在 = 本局整局不参与导出（首行是原因，写进 manifest）

dataset/
    manifest.json        版本、切分规则与归属、行数、各 split 统计
    train.jsonl
    val.jsonl
    test.jsonl
    export.py            确定性导出：runs/ -> dataset/
```

`room_key` 的构造：

```python
room_key = f"a{act}_f{floor}_n{node}_c{combat_instance}"
```

即 `a1_f7_n5_c1`。`combat_instance` 从 1 开始，SL 时自增。

## 导出确定性

`dataset/export.py` 必须满足：

- **纯函数式**：`export(runs_dir) -> {split: [row, ...]}`，同样输入必然同样输出（逐字节）。
- 只读**已提交**的行（pending 一律忽略）。
- 按 `exclude_post_sl_rooms` / `exclude_fallback_rows` 过滤。
- 带 `runs/<run_id>/EXCLUDED.txt` 的局**整局跳过**，原因记进 `manifest.json` 的
  `excluded_runs`。用途：隔离"已知被污染"的局（例如检测器误报把某类决策整批丢掉），
  原始记录照留以便复现，导出层不再碰它。删掉标记即可重新纳入。
- 按需回填 `outcome`（局终结果、房间 HP 变化）。
- 支持 `--verify`：重跑一次并断言与已有 `dataset/*.jsonl` 逐字节一致（回归测试用）。

## 配置

```toml
[record]
runs_dir = "runs"
save_raw_states = true

[dataset]
dir = "dataset"
exclude_post_sl_rooms = true
exclude_fallback_rows = true
val_ratio = 0.1
test_ratio = 0.1
split_seed = 20260927

[observe_human]
also_query_model = true     # 同时问一次模型，记录 matched/agreement 对照
```

## 采集 runbook（observe_human 模式）

目标：**人类正常打，agent 只观察** —— 枚举候选、构造与模型逐字节相同的 `(state, questions)`，
把人类的真实选择记成强标签。agent 全程不发任何动作。

### 0. 先看数据长什么样（不连游戏、不连真 Laya）

```powershell
.\.venv\Scripts\python.exe tools\demo_observe_session.py --dump-row
```

它用进程内 `FakeLaya` 编一串"观察 -> 人类动作 -> 换房提交 -> 读档回滚"，落盘到 `.pytest-tmp\demo\`，
再跑真的 `dataset/export.py`。**先花一分钟确认 schema 符合预期，再上真机。**

### 1. 让 agent 进入 observe_human 模式

`spire.local.toml`（从 `config.example.toml` 复制）：

```toml
mode = "observe_human"      # 不接管操作，只观察

[observe_human]
also_query_model = true     # 每步顺手问一次模型，落 model_answer / agreement 对照
```

或者不改文件，直接在命令行覆盖：

```powershell
.\.venv\Scripts\python.exe -m spire_agent run --config spire.local.toml --mode observe_human
```

**模组侧不需要任何操作。** 协议 v2 起模组没有自己的模式：`mode` 与 `watchdog_sec`
在握手时由 agent 推送，所以切模式既不用改模组配置、也不用重启游戏。
（v1 的 `observe_human` 键已废弃；文件里留着不会被读，`doctor` 会提醒。）

- `also_query_model = false` 也能采集，只是没有模型对照列（`agreement_rate` 为 `null`）。
- 没有真 Laya 时用常驻假后端顶上：
  ```powershell
  .\.venv\Scripts\python.exe tools\fake_laya_server.py --port 18080
  # spire.local.toml 里 laya.base_url = "http://127.0.0.1:18080"
  ```
  它返回的答案是确定性的（字典序最小的候选）。**只用来验证管线通了，别当真模型的对照。**

### 2. 跑

```powershell
.\.venv\Scripts\python.exe -m spire_agent run --config spire.local.toml --mode observe_human
```

开局会打印 `运行模式：observe_human —— 人类玩，agent 只观察记录（不控制游戏）`；
`doctor` 会额外打印 `configure={"mode": "observe_human", ...}` 作为回执核对。

- 面板 <http://127.0.0.1:8788/> 在 observe 模式下显示候选集与模型对照答案（人类一提交就清空）；
  时间线里 `matched=false` 会记为"未命中"。
- 人正常玩即可。**别主动读档**：读了也不会污染已提交数据 —— 当前房间的 pending 行会被丢弃、
  `combat_instance` 自增、从房间开头重记，重记的那间房打 `post_sl=true`。
- 想停就 `Ctrl-C`（自己开的终端里一定送得到；被别人代跑的进程可能收不到，那就按 PID 停，
  见 `docs/10-deployment.md` 的《进程归属》）。observe 模式下 agent 只是旁观者，停掉它不卡游戏。

### 4. 导出

```powershell
.\.venv\Scripts\python.exe -m spire_agent export --runs runs --out dataset
.\.venv\Scripts\python.exe -m spire_agent export --runs runs --out dataset --verify   # 只校验，确定性自检
```

默认口径丢掉三类行（**原始记录都还在 `runs/`，只是不进训练集**）：

| 丢掉的行 | 原因 | 想留下就加 |
|---|---|---|
| `post_sl=true` 房间的行 | 人类已知这间房的抽牌顺序，标签被非公平信息污染 | `--keep-post-sl` |
| `agent_fallback=true` 的行 | 模型不可用时的规则兜底，不是模型决策 | `--keep-fallback` |
| `matched=false` 的行 | 人类动作没被候选枚举覆盖，没有正确的答案空间 | `--keep-unmatched` |

> `dataset/export.py` 同样支持这些开关，参数名一致，方便脱离 agent 单独跑。

> **默认根本不会产生 fallback 行**：`[laya] on_error = "stop"`（默认）时拿不到模型决策就直接停跑
> （见 [07-laya-contract](07-laya-contract.md#拿不到模型决策时默认停跑不替模型猜)）。上面那一行是给
> 显式配了 `on_error = "fallback"` 的实验准备的。

### 5. 采集质量怎么判断

看 `runs/<run_id>/summary.json`（导出时 `dataset/manifest.json` 记的是另一组：`counts.total_rows/kept_rows/dropped/per_source/per_split`）：

- `match_rate` —— **候选枚举完备度**。低于 ~99% 就该去补 `decision_points.yaml` 里的候选枚举器；
  `matched=false` 的行保留了 `human_action`，直接告诉你漏了什么动作。
- `agreement_rate` —— 模型与人类的一致率，微调前的基线。
- `by_decision_point` —— 采集偏差（比如全是战斗、几乎没有商店），据此补跑。
- `unique_fair_states` —— 去重后的状态数，判断数据是否大量重复。

`label_source = "human"` 是强标签（模仿学习主料）；agent 自己跑出来的 `"agent"` 是弱标签。
导出时务必别把 `matched=false` / `agent_fallback=true` 两类行混进训练集。

## 统计口径（写进 `runs/<run_id>/summary.json`）

| 指标 | 定义 |
|---|---|
| `rows_total` / `committed_rows` | 已提交行数 |
| `by_source` | human / agent 行数 |
| `by_decision_point` | 各决策点行数分布（据此发现采集偏斜，例如几乎没有商店样本） |
| `match_rate` | human 行里 `matched=true` 的占比（observe 模式的候选完备度） |
| `agreement_rate` | 有模型对照的行里 `agreement=true` 的占比（模型与人类一致率） |
| `fallback_rate` | 兜底行占比 |
| `post_sl_rows` | 打上 `post_sl` 的行数 |
| `unique_fair_states` | 去重后的公平状态数（判断样本冗余度） |
| `floor_hp` | `[{floor, hp, max_hp}]` 曲线 |
| `watchdog_events` / `sl_events` | 看门狗触发次数 / 读档事件列表 |
| `pending_rooms` | 局终仍未提交的房间（正常应为空） |

## 相关文档

- 公平性即数据契约 -> [04-fairness](04-fairness.md#5-与数据集的关系)
- 微调流程 -> [12-roadmap](12-roadmap.md#微调流程)

# 11 测试与验收

## 分层策略

| 层 | 范围 | 依赖 | 跑法 |
|---|---|---|---|
| L1 core 单测 | `spire-core` 全部纯逻辑 | 无（无网络、无游戏、无文件） | `pytest packages/spire-core` |
| L2 mod 单测 | mod 的纯逻辑部分（协议、校验、看门狗、稳定态） | JDK 21 | `tools\test_mod.ps1` |
| L3 agent 集成 | 桥接 + Laya 客户端 + 落盘，用假 mod / 假 Laya | 本地端口 | `pytest packages/spire-agent` |
| L4 端到端 | 真游戏 + 真 mod + 真 Laya | 全部 | 手动，见下 |

**主力是 L1**。核心逻辑必须能在没有游戏、没有模型、没有网络的情况下全部验证——这是把最重的逻辑放进 `spire-core` 的主要收益。

## L1：core 单测清单

### 无泄漏断言（最重要）

```python
def test_fair_view_has_no_hidden_info(raw_fixture):
    fair = fairness.filter_(raw_fixture)
    blob = json.dumps(serialize.state(fair))

    assert "run_seed" not in fair
    assert "seed" not in blob
    # 抽牌堆只留多重集
    assert fair.zones.draw == sorted(fair.zones.draw)
    # 怪物不给未结算伤害与未来行动
    assert all(m.intent.base_damage is None for m in fair.combat.monsters)
    assert all(m.upcoming_moves is None for m in fair.combat.monsters)
```

**规矩：任何新增字段都要补一条对应的负向断言。**

### 序列化确定性

- 同一 fixture 连续序列化 100 次，`json.dumps` 结果逐字节相同；
- 打乱 fixture 里 dict 的键顺序后，输出仍相同；
- 浮点统一 3 位小数（断言不存在 `0.30000000000000004` 这类值）。

### 预算

- 构造超大牌组 + 大量 `draw_pile` -> `budget.enforce` 折叠后仍在 50000 字符内；
- 构造 150 个候选 -> 截断到 100，且 `truncated_candidates` 记录了被丢的 50 个；
- 构造 70 道题 -> 抛 `BudgetExceeded`（不静默）；
- 断言选项总数 <= 512。

### 候选枚举边界

| 场景 | 期望 |
|---|---|
| 0 能量、手牌全是 1 费牌 | 候选只有 `end_turn`（+0 费牌） |
| 单敌人 | 需要目标的牌只有 1 个目标变体 |
| 敌人已死（`is_gone`） | 不出现在候选里 |
| 空手牌 | 只有 `end_turn` |
| 药水槽满 | 不出现 `potion:*` 之外的冲突；`discard_potion` 可用 |
| 候选 > 100 | 截断且顺序确定 |
| 空药水槽（`PotionSlot`，`canUse()` 说谎返回 true） | 不出现在 `potion:*` 里，也不计入"带了几瓶药水" |
| 只有一个候选 | 短路：直接执行，**不调 Laya**，行带 `meta.forced=true` 且 `label_source=agent` |

### 合法性 / 观测事实（`tests/test_legality.py`）

- **不变式**：对每个决策点、每套 fixture，`enumerate_candidates` 产出的候选
  `legality.legal_only` **一个都不摘**（摘了就是枚举器和执行对不上）；
- 空槽即便 `can_use=true` 也非法；真药水给/不给目标的两种非法形态；
- 打不出来的牌、越界手牌下标、打向不存在的敌人、给不需要目标的牌加目标；
- 地图节点必须可达；商店下标越界；`-1` 只有在商店里才是"离开"；
- 选牌候选必须落在当前选牌界面上；
- 英文文本：`name` / `text` / `type` / `rarity` / `potion_slots` / 商店 `name`+`text`
  确实进了 state；`!D!` 为 -1 时写 `?` 而不是 -1；
- 指令里的 `{character}` / `{ascension}` 按当前局面填（Watcher 的局面上**不能**出现 Ironclad）；
- 地图候选的"你带了几瓶药水"忽略空槽。

### 界面上下文与奖励明细（`tests/test_screen_context.py`）

选牌是**有状态**的（升级 / 删牌 / 变形 / 事件是同一块 UI），奖励明细决定"要不要再点进去"，
两者都有专门的断言：

- `origin` / `event_name` / `event_text` 能穿过 `fairness.filter_` 进到 `state.screen`；
  旧模组不报这些字段时 state 里**不凭空多出**它们；
- 选牌候选描述与 `select_card_*` 的指令里都带上 `{select_purpose}`（同一个函数，
  两条路径不会漂移）；
- `{event_name}` 拿不到时连括号一起吞掉，不留 "event ()." 残句；
- `COMBAT_REWARD` 的 `reward_details[]` 与 `options[]` 逐条对齐，卡牌奖励带牌名与稀有度；
- `card_reward` 的 `skip` 文案是"这三张都不想要"的口吻，不是中性的 "Skip."。

检索类选牌（头槌 / 全息影像 / 发掘 / 秘密技法 / 万知药水 / 观者的预见）的数据契约
单独列出来（`test_screen_context.py`）：

- `reason`（界面提示语）能穿过 `fairness.filter_` 进 `state.screen`，拿不到时**不出现**；
- `origin=combat_select` 的候选描述说明"这是战斗内的一次检索"；
- 预见：`draw_order` 1/2/3 原样进 `state.screen.selectable`，而候选 id 仍然是
  `<zone>:<index>`（`draw_order` 不参与 id，改动它不会打乱候选对齐）；
- 非预见界面**不能**凭空多出 `draw_order`（那是顺序信息，只有玩家看得见的那一屏才给）；
- `{select_reason}` 是"整句或空串"：有提示语时拼成 `The screen says: "…."`（不出现 `..`），
  没有时整个消失。

### 构题合规

- 每个决策点产出的 `questions` 满足：类型合法、`choice` 的 criteria 非空、`score` 的 criteria 是有序列表、题数 <= 64；
- criteria 的 key 与 `candidate_ids` 一一对应（无遗漏、无多余）。

### 答案解析与裁决

- `choice` 正常 -> 取 `answers[q]["choice"]`；
- 早期契约的 `answer` / `label` 写法仍能解析（老记录回放）；
- `choice` 不在 criteria 里 -> `parse_error` -> 兜底；
- `answers` 缺该题 -> `parse_error` -> 兜底；
- 概率并列 -> 取 criteria **插入顺序**靠前者（不是字典序）；
- `pick_topk`：`min_select` 强制下限、`max_select` 强制上限、低于 `neutral` 不选；
- `pick_topk` 的排序稳定（同分按候选顺序）。

### SL 回滚

构造"记录 -> 状态回退 -> 重记"序列，断言：

- pending 被清空；
- `combat_instance` 自增；
- 该房间 `post_sl=true`；
- `meta.json.sl_events` 增加一条且 `dropped_rows` 正确；
- 前序房间的已提交行**未被改动**。

### 数据集导出

- 同样输入导出两次，`train.jsonl` 逐字节相同；
- 同一 `run_id` 的所有行落在同一 split（随机抽 50 个 run 断言）；
- `exclude_post_sl_rooms` / `exclude_fallback_rows` 生效；
- `choice -> noul` 展开：N 个候选 -> N 条 noul 行，恰好 1 条 `true`；
- `manifest.json` 的统计与行数自洽。

### 回放

用 `decisions.jsonl` 回放：重建 `state`/`questions`，断言与记录中的**逐字节一致**（这同时验证了序列化的确定性）。

## L2：mod 单测清单

纯逻辑类可脱离游戏编译运行（不引用游戏类型）：

- `Envelope` 编解码往返（含中文、转义、超长帧拒绝）；
- `Configure` 解析/校验：合法 mode、缺 mode、拼错的 mode、`watchdog_sec` 越界，
  以及**校验失败时不泄漏半成品状态**（`mode` 必须为 null、秒数回落默认）；
- NDJSON 分帧：半包/粘包/超长行的行为；
- `ActionSpec` 白名单校验：非法 kind、越界 index、缺参、多余的 target；
  其中**击杀后下标不移位**是必测项：两个敌人、`m0` 已死，则 `m1` 必须放行、`m0` 必须拒
  （`play_card` / `use_potion` / `select_choice` 三条路径都要覆盖）。判据是"下标 i 上的敌人是否活着"，
  不是 `i < 活着的数量` —— 后者会在第一次击杀后把所有带目标的动作判成越界。
- `Watchdog`：用假时钟推进，断言超时触发与动作选择（战斗内 vs 非战斗界面）；
- `PotionFacts`：`PotionSlot.canUse()` 对**空槽**也返回 true，所以模组报的 `can_use` 必须
  过一层 `PotionFacts` 压成 false；漏了它，agent 会把空槽当成可用的药水（真机踩过：选一次被拒
  一次、state 不变，每秒多空转一轮，看门狗还被这串动作压着不触发）。
- `StabilityGate` 去抖：连续 N 帧才放行，抖动不放行；
- `ZoneGuess`（`SelfTest.zoneGuessTests`）：临时牌组的区域反查是"每个 uuid 都出现在某个
  牌堆里"的精确判据 —— 头槌（弃牌堆）/ 预见（抽牌堆）各自命中；一个都不命中 -> `offer`
  （药水那种当场造出来的牌）；混合来源 -> `offer`（一个牌实例只住一个堆）；uuid 缺失或为空
  **不能**当证据（宁可判不出来，也不编一个区域）；空候选回落 `hand`。
- `Eng` 英文文本（`SelfTest.engTests`，直接读游戏 jar 的 `localization/eng/*.json`）：
  卡牌/怪物/遗物/药水/power 的名字与描述；`!D!` 为 -1 时写出 `?` 而**不是** -1；
  `\bNL\b` 替换不误伤 `ONLY`；事件选项按 `[` 开头拼回碎片、被切开的数值位置留 `?`；
  事件资源是"显示名"键（`Big Fish`）而调用方只有类简单名（`BigFish`），归一化匹配要生效。
- **SL 回退检测**：喂入一组 `(act, floor, node, turn, hp, in_combat)` 序列，断言真回退被识别、
  误报被挡住。回归用例必须包含：战斗结束那一帧 `turn` 重置回 1（**不是** SL）、战斗内治疗、
  召唤物导致怪物 HP 上升、非战斗界面的 `turn` 变化、**局外 `(0,0,0)` 反复出现**（不是重访节点）。
- **会话边界**（agent 层）：观测 `seq` 归零时换新 `RoomTracker`（否则结算画面停在 boss 节点会
  被当成重访）、未提交 pending 一律作废、种子没变且上一局没结束 ⇒ 判为续玩读档、
  开局那次模组读档信号**不**记 `sl_events`。
- **提交边界**：局外观测（`act`/`floor` 都是 0）不算"离开节点"，战斗中的 pending 必须留住。
- **patch 形状**（`SelfTest.checkPatches`）：反射 `HumanActionPatches` 的每个嵌套类，
  按 MTS legacy `@SpirePatch` 的**位置对齐**规则核对 patch 形参是不是目标方法槽位
  （`$0`=this，之后是形参）的逐位前缀。MTS 不看形参名字，写错不会编译报错，而是
  加载模组时抛 `CannotCompileException: Prefix(...) not found`，**游戏直接起不来**——
  所以这条必须在打包阶段拦住。校验命令：`tools\build_mod.ps1`、`tools\test_mod.ps1`
  （两者都把 ModTheSpire + 游戏 jar 放进自检 classpath，缺了只跳过不误报）。

## L3：agent 集成

- 假 mod（本地 TCP 服务，按脚本发 `observation`）→ 断言 agent 发出的 `action` 序列正确；
- **握手配置**：agent 收到 `hello` 后必须立刻发 `configure{mode, watchdog_sec}` 并等回执；
  协议版本不符或回执被拒 → `IncompatibleMod` 冒泡，**不参与重连**（`test_bridge_loopback.py`）；
- 假 Laya（本地 HTTP，按脚本返回 `answers`）→ 断言缓存命中、重试次数、失败时按 `laya.on_error`
  **停跑**（默认）或兜底（`fallback`）；
- 探针契约（`test_probe.py`）：把真 Laya 的题型规则抄成断言（每题必须有非空 `instructions`），
  并断言应答形态不对时预检必须**失败**（老字段 `answer`、choice 不在候选里、没有 `answers`）。
  抄来的规则可能漂移，所以另有 `tools/check_probe.py` 在 `.venv-laya` 里**用真校验器**再对一遍
  （`Agent._check_question`，含"缺 instructions 必须被拒"的反例）。
- 落盘：断言 `pending` -> `commit` 的时机正确（只在房间切换时提交）；
- **拒绝风暴**（`test_runner.py`）：模组连续拒掉同一个 state 上的动作、而 state 一直没变
  （候选里有游戏执行不了的东西）时，agent 攒够 `REJECTION_LIMIT` 轮就**停发**，把这一屏
  交给看门狗；state 一变（看门狗结束回合、换回合、换房间）计数清零、重新开始决策。
- 面板：断言 `GET /api/state` 的快照与 `decisions.jsonl` 一致（每秒轮询，无 WebSocket）。
- **请求/返回原文**（`test_panel.py` / `test_laya_client.py` / `test_runner.py`）：钩子拿到的
  `request` 必须与线上真正 POST 出去的那一份逐字段相同（拿 `MockTransport` 里读到的 body 对）；
  成功 / 缓存命中 / 每次重试失败 / 连不上 / 超预算拒发五种情况各断言一次；缓存命中带
  `cached=true` 且 `status=None`。`/api/state` 的 `exchanges` **不许夹带请求体**（500 字符的
  blob 在原文里、不在摘要里 —— 轮询体一旦变胖，面板每秒一次的轮询就会拖死自己），
  原文只能从 `/api/exchange/<id>` 取，挤出缓冲后 404、id 不是整数 400。
  另断言**钩子抛异常不能影响决策**（它只是观测），以及**强制决策不产生 exchange 行**
  （唯一合法动作压根没问模型，面板上不该出现假的请求记录）。

控制台语义单独一个文件（`test_console.py`）：以 `CREATE_NEW_PROCESS_GROUP` 起的进程会带一个
可继承的"忽略 Ctrl-C"标记，`enable_ctrl_c()` 清掉后 Ctrl-C 必须能送达（退出码 `130`）；以及
GBK 流上 `enable_utf8_output()` 后中文不乱码。

## L4：端到端验收

v1 的成功标准（**全自动，无人工干预**）：

1. **连通**：MTS 启动游戏 -> agent 连上 mod -> 面板实时可见；
2. **跑通整局**：自动开一局 Ironclad / Ascension 0，全自动打到死亡或通关；局终自动重开；
3. **采集完整**：每个决策点都落盘，`summary.json` 的 `fallback_rate == 0`（默认 `on_error=stop`，出现即说明配成了 fallback）、`watchdog_events == 0`、`sl_events == 0`；
4. **抗崩溃**：中途 `kill` agent -> 游戏不卡死（看门狗在 30s 内接管）-> 重启 agent 能续接当前局；
5. **可复现**：用固定种子重放一局，决策序列一致（同一 `(state, questions)` 得同一答案）；
6. **observe_human 冒烟**：人类手动打一间房，`match_rate` 与 `labels` 可人工核对；中途读档一次，验证 pending 被丢弃并从房间开头重记、`post_sl` 置位。
   额外核对：同一张卡**只**上报一条 `select_reward`（闩锁生效），跳过只上报一条；
   `select_cards` / `select_reward` 提交后游戏**真的**推进到下一步（事件/篝火/商店删牌、
   拿卡牌奖励各验一次），而不是界面看着关了、事件还停在那儿。
7. **切模式不需要重启游戏**：`doctor --mode agent` / `doctor --mode observe_human` 各跑一次，
   `configure` 回执跟着变；再 `run --mode observe_human` 起一次、`run` 起一次，
   面板同样跟着变。模组侧的 properties 文件全程不用动（真机已验，2026-09-28）。
8. **检索类选牌不在中途卡死**（回归，真机必验）：铁甲战士打「头槌」（弃牌堆 >= 2 张时）
   -> 界面出现后 **agent 收到观测并给出决策**，选中的牌进抽牌堆顶，战斗继续（弃牌堆只剩
   1 张时游戏自己拿走，不出现界面）；观者的预见类卡牌 -> 出 `select_card_any`（`score`）
   决策，可以一张都不选；两种情况下 `summary.json` 的 `watchdog_events` 保持 0。

### 验收记录模板

```
日期:
游戏版本: 2.3.4
mod 版本:
agent commit:
Laya: <base_url> / checkpoint=<english|multilingual> / device=<cuda|cpu>
run_id:
结果: 通关 / 死于 Act<X> Floor<Y>
laya_calls:            cache_hits:
fallback_rate:         watchdog_events:
sl_events:             mean_confidence:
latency_p50/p95:
异常与观察:
```

## 本地跑法

先在仓库根目录建好 venv（`powershell -File tools\setup_dev.ps1`，装完之后不需要激活、不需要 `PYTHONPATH`）。
下面的命令都在**仓库根目录**执行。

```powershell
# L1（主力，无需任何外部依赖）
.\.venv\Scripts\python.exe -m pytest packages\spire-core\tests -q

# L2（需要 JDK；不需要游戏）
powershell -File tools\test_mod.ps1

# L3
.\.venv\Scripts\python.exe -m pytest packages\spire-agent\tests -q

# Laya 健康检查
.\.venv\Scripts\python.exe tools\laya_health.py --base-url http://127.0.0.1:8000 --api-key test
```

没有远端 Laya 时想跑 L4 的链路（桥接 → 构题 → 请求 → 仲裁 → 执行 → 落盘 → 面板），
另开一个终端常驻假 Laya 即可（确定性作答，不是模型）：

```powershell
.\.venv\Scripts\python.exe tools\fake_laya_server.py --port 8000 --api-key ""
```

想验证**采集 -> 数据集**这条支线，直接跑离线演示（同样不连游戏）：

```powershell
.\.venv\Scripts\python.exe tools\demo_observe_session.py --dump-row
```

它编一串"观察 → 人类动作 → 换房提交 → 读档回滚"落到 `.pytest-tmp\demo\`，再跑真的
`dataset/export.py`，用于核对 observe_human 的 schema、`matched`、`post_sl`、模型对照字段。

## 相关文档

- 指标口径 -> [09-observability](09-observability.md#指标口径)
- 数据集契约 -> [08-dataset](08-dataset.md)

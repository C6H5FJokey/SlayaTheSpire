# 06 决策点

**决策点**是「游戏在等一个输入」的时刻。每个决策点由四件东西构成：

1. 观测切片（从 fair 视图里取哪些字段）；
2. 候选枚举器（有哪些可选）；
3. 问题模板（怎么问 Laya）；
4. 解析器（答案怎么变回动作）。

全部注册在 `packages/spire-core/src/spire_core/decision_points.toml`（用 TOML 而不是 YAML：`tomllib` 是 Python 3.11+ 标准库，core 保持零第三方依赖）。

## 决策点识别

```python
def identify(fair) -> DecisionPoint
```

判定顺序（自上而下，命中即返回）：

> **前置约定（模组侧 `Observer.screenName()`）**：`AbstractDungeon.screen == NONE` **不等于**
> "没有界面"。事件房 / 篝火 / Neow / 宝箱都是**房间自己画**的，游戏不会把 `screen` 置成
> 任何值，它一直是 `NONE`。所以模组必须先按**房间类型**判出 `EVENT` / `REST` / `NEOW`，
> 判不出来才回 `NONE`。
>
> 这条踩过一次真机坑：`screenName()` 原先在 `switch` 里直接 `case NONE: return "NONE"`，
> 结果**事件房永远被识别成"无界面"**，`identify()` 只能抛 `UnknownDecisionPoint`，
> agent 从不下发决策 —— 表现为"事件不会做出选择"，只能等 30s 看门狗盲点第一个选项。

| 顺序 | 条件 | 决策点 |
|---|---|---|
| 1 | `screen == MAP` | `map_node` |
| 2 | `screen == CARD_REWARD` | `card_reward` |
| 3 | `screen == GRID` 且是"从牌组/手牌里选牌" | `select_card_must_k` 或 `select_card_any`（按 `screen_state.min_select == screen_state.max_select` 区分） |
| 4 | `screen == COMBAT_REWARD` | `relic_select`（若有遗物）/ `card_reward`（若有卡牌）/ 否则 `proceed` |
| 5 | `screen == EVENT` | `event_option` |
| 6 | `screen == SHOP_ROOM` | `shop` |
| 7 | `screen == REST` 且 `rest_options` 或完成后的 `options=["Proceed"]` 非空 | `rest_site` |
| 8 | `screen == NEOW` | `neow_bonus` |
| 9 | `screen == BOSS_RELIC` / 宝箱 | `relic_select` |
| 10 | `in_combat` 且可操作 | `combat_play` |
| 11 | 战斗内且存在待选目标 | `select_target` |
| 12 | 兜底 | `generic_choice`（把 `screen_state.options` 原样作为候选） |

**统一性原则**：出牌之后弹出「选目标 / 选牌」不是特殊流程，而是**新的稳定观测**命中第 3 或第 11 条。

## 候选 id 规范

候选 id 是**字符串主键**，贯穿「构题 -> 裁决 -> 执行 -> 记录」全链路，并与数据集 `candidate_ids` 一致。

| 决策点 | 候选 id 形态 | 例 |
|---|---|---|
| `combat_play` | `play:h<hand_index>[-&gt;<target>]` / `potion:p<slot>[-&gt;<target>]` / `end_turn` | `play:h2->m0`、`play:h0`、`potion:p0->m1`、`end_turn` |
| `select_target` | `target:m<monster_index>` | `target:m1` |
| `select_card_must_k` / `select_card_any` | `card:<zone>:<index>` | `card:hand:3`、`card:deck:11` |
| `map_node` | `node:<node_id>` | `node:n4_3` |
| `card_reward` | `reward:<index>` / `skip` | `reward:0`、`skip` |
| `relic_select` | `relic:<index>` | `relic:1` |
| `event_option` | `option:<index>` | `option:2` |
| `shop` | `buy:<index>` / `leave` | `buy:3`、`leave` |
| `rest_site` | `rest:heal` / `rest:smith` / `proceed` | 完成篝火后选择 `proceed` |
| `neow_bonus` | `neow:<index>` | `neow:0` |
| `generic_choice` | `choice:<index>` | `choice:0` |

约定：

- `index` 一律 0-based，对应**同一次观测**中该数组的下标。
- id 里不含任何需要转义的字符（不用空格、逗号）。
- id 一旦生成，解析回 `Action` 必须是无歧义的（`actions.parse(candidate_id) -> Action`）。
- `card_reward` 的两个候选解析出的动作 kind 不同：`reward:<i>` -> `select_reward{index:i}`，
  `skip` -> `select_card_reward{index:-1}`。两者都只在**卡牌奖励界面**（`CARD_REWARD`）成立 ——
  模组的 `select_reward` 按界面分派（`CARD_REWARD` 走 `CardRewardScreen`，`COMBAT_REWARD`
  走领奖列表），见 `docs/03-mod-protocol.md#语义动作白名单`。

## 各决策点的候选与题面

### `combat_play`

**候选**（顺序即 tie-break 顺序）：

1. 手牌中每张 `is_playable` 的牌 × 合法目标：
   - `target_type == NONE` / `SELF` / `ALL_ENEMY` -> `play:h<i>`
   - `target_type == ENEMY` -> 对每个存活敌人各一个 `play:h<i>->m<j>`
2. 每个可用药水 × 合法目标 -> `potion:p<i>` / `potion:p<i>->m<j>`
3. `end_turn`

**题面**（`choice`）：criteria 的 key 为候选 id，value 为英文描述，形如
`"Play Bash (2E) on Jaw Worm: deal 8 damage, apply 2 Vulnerable."`。
`instructions` 固定为：

> You are playing Slay the Spire as the Ironclad. Choose the single best action for this turn.

**解析**：取 `answers.q_action.answer`（候选 id）-> `actions.parse()` -> `Action`。

**边界**：0 能量时只有 `end_turn` 与 0 费牌；手牌为空时只有 `end_turn`；只剩 1 个候选时**仍然问模型**（保持数据一致），但记录 `meta.degenerate: true`。

### `select_target`

**候选**：每个存活敌人 `target:m<i>`。
**题面**：`choice`，criteria 为敌人摘要（HP/格挡/powers/意图）。
**解析**：`target:m1` -> `Action(select_choice, {"monster": 1})`。动作白名单里**没有**独立的 `select_target`，选目标统一走 `select_choice`（见 `actions.parse_candidate`）。

### `select_card_must_k`（必选 k 张）

**候选**：可选区域内每张牌 `card:<zone>:<index>`。
**题面**：`choice` 连问 k 次。每轮把**已选中的牌从候选里移除**，并已选列表拼进 state（`selection_picked`）。
**解析**：k 轮答案依次映射为 `select_cards(indices=[...])` 的组成部分，**最后一次性提交**。

**选牌界面是"有状态"的，题面必须带上"为什么在选"。** 同一块 "选一张牌" 界面
背后可能是升级神龛、篝火锻造、事件删牌、变形、商店删牌 —— 面对同一张 `Strike`，
答案分别是"升它""删它""留着"。光说"选一张牌"等于什么都没说。
所以 mod 在 `screen_state` 里给出：

| 字段 | 含义 |
|---|---|
| `origin` | 来由：`rest_smith` / `event` / `transform` / `purge` / `upgrade` / `confirm` / `combat_select` / `scry` / `select` / `hand_select` |
| `event_name` / `event_text` | 若在事件房里，事件的英文名与开场正文（玩家进入事件时就看得到） |
| `reason` | 游戏写在界面上的那句提示语（玩家抬头就读得到），例如头槌的 "Choose a Card to Put on Top of Your Draw Pile." |

core 侧由 `candidates.selection_purpose()` 把 `origin` 翻成一句人话，同时进
**候选描述**与**指令占位**（`{select_purpose}`），两条路径共用同一个定义；
`reason` 另走一个占位 `{select_reason}`（整句或空串，拿不到就整个消失），
说明"这一次检索具体在干什么"。判定顺序上 `origin` **先按房间判、再按界面标志位判**：
`forUpgrade` / `forPurge` 在"锻造"和"升级神龛"里取值相同，只有房间类型能区分。

**战斗内的牌堆检索（头槌 / 全息影像 / 发掘 / 秘密技法 / 万能药 / 观者的预见）走的就是
这一对决策点，没有单独的决策点。** 头槌是"必选 1 张"（`min=max=1`）→
`select_card_must_k`；观者的预见是"任意多选，可以一张都不选"（`min=0`）→
`select_card_any`，界面上的 `draw_order` 告诉模型哪张离抽牌堆顶最近。这两条路以前
**根本走不到决策点**：模组的稳定性判定把"选牌界面开着、动作还停在队列里"误判成不稳定，
一个观测都不发（病根与修法见 [03-mod-protocol](03-mod-protocol.md#稳定性判定)）。

### `select_card_any`（任意多选）

**候选**：同上。
**题面**：对**每张候选**发一道 `score` 题：

```jsonc
"q_card_hand_3": {
  "type": "score",
  "instructions": "Should this card be selected? Rate how good selecting it is.",
  "criteria": ["very bad", "bad", "neutral", "good", "very good"]
}
```

**解析**：把每个候选的期望分算出来（`sum(p_i * i)`），降序排序，按 `screen_state.min_select` / `max_select` 与阈值取前 k：

- `k` 至少 `min_select`；
- 期望分低于 `good`（等级 3，即 `arbitrate.SCORE_SELECT_THRESHOLD`）的候选默认不选 —— 否则任何多选界面都会无条件选满，等于放弃"可以跳过"这个选项；
- 至多 `max_select`。

**这是"任意多选"的核心**：一次请求里批量问 N 道 `score`，比循环问 N 次 `choice` 省 N 倍网络往返，而且天然给出可比较的序。

### `map_node`

**候选**：`map.reachable` 中每个节点 `node:<id>`。

**题面**（`choice`）：criteria 的 value 是**候选描述**，由 `serialize/map.py:describe_candidate()` 生成，必须包含：

1. **节点类型**（Elite / Rest / Shop / Event / Monster / Treasure / Unknown）；
2. **下游 2-3 层可达结构摘要**：从该节点出发 BFS 2 层，统计可达的节点类型计数，例如
   `"then reachable within 2 floors: 3 Monster, 1 Elite, 1 Shop, 2 Rest, 1 Event"`；
3. **精英 / Boss 距离**；
4. **当前资源**：HP/最大HP、金币、药水数、卡组强度摘要（卡组大小、攻击/技能/能力张数、升级张数）。

描述算法（`describe_candidate`）**必须是纯函数**且对同一 fair 输入稳定。所有下游统计都基于**公平视图里的完整地图**（地图本就全可见，不违反公平性）。

**解析**：`node:n4_3` -> `Action(select_map_node, node_id="n4_3")`。

### 其余决策点

均按「候选 = 界面选项，题面 = 一道 `choice`，criteria = 选项英文描述」的统一形态处理。商店是**循环**的：每次问「买什么 / 离开」，买完产生新的稳定观测，再次进入 `shop`，直到模型选 `leave` 或无法购买。

`card_reward`（三选一）同理，`skip` 的候选描述要写成"这三张都不想要"的口吻
（`Skip: none of these three cards is worth taking.`），而不是中性的
"Skip the card reward." —— 模型需要被明确告知"跳过是一个正当选项"。

### 领奖界面（`COMBAT_REWARD`）的"选项列表"定义点

战斗奖励界面的选项列表**只认界面自己那份 `AbstractDungeon.combatRewardScreen.rewards`**，不能读
`getCurrRoom().rewards`：

- `CombatRewardScreen.open()` 里是 `this.rewards = new ArrayList(getCurrRoom().rewards)`，即**开界面时的拷贝**；
- 游戏自己的领取路径是 `rewardViewUpdate()` 里的 `it.remove()` —— 领一件就从**界面**那份里删一件；
- `RewardItem.claimReward()` 自己**不做** remove，它只置 `isDone`。

真机事故（`runs/run-0003` seq 12→25）：模组读的是 `room.rewards`（那份永不缩短的拷贝），
又直接调 `RewardItem.claimReward()` 绕过了 `rewardViewUpdate()` 的删除，于是同一瓶
`Potion: Energy Potion` 连着领了 3 次（`player.potions` 从 `[空,空,空]` 填到 `[满,满,满]`），
选项列表却一直是 `["Gold (15)", "Potion: Energy Potion", "Proceed"]`，模型每 30s 被看门狗
推着重问一遍、每次都在 0.31/0.38/0.31 的概率上瞎猜。

因此实现上有两条硬约束：

1. `Observer.liveRewards()` 在 `screen == COMBAT_REWARD` 时必须读界面那份 `rewards`，
   `room.rewards` 只作为认不出界面时的兜底；
2. 执行 `claim_reward` **只置 `item.isDone = true`，剩下的全交给游戏**。

第 2 条是本轮修掉的 bug。`RewardItem.claimReward()` 的返回值**不是成功/失败**：
`CARD` 类型故意返回 `false`（它只是打开三选一界面），`POTION` 在槽满时返回 `false`。
早先 agent 自己调 `claimReward()` 并把 `false` 当失败，又把该项从界面里 `remove` 掉，
于是"拿卡牌奖励"永远报错、条目也删不干净。正确路径是照抄
`CombatRewardScreen.rewardViewUpdate()`：

```
item.update();
if (item.isDone) {
    if (item.claimReward()) { it.remove(); changed = true; }   // GOLD/STOLEN_GOLD
    else if (item.type == POTION) { item.isDone = false; flashRed(); }  // 槽满
    else { item.isDone = false; }                              // CARD：开了三选一，条目留着
}
```

### 奖励选项文案要"自解释"（否则模型会空转）

`COMBAT_REWARD` 的选项文本里，**卡牌奖励必须带上那三张牌的名字**：
`Card reward (Strike / Pommel Strike / Inflame)`。不给名字的话，模型只能靠
"点进去看一眼 → 不满意 → 退出 → 忘了里面是什么 → 再点进去"来获取信息，
真机上就是这么空转的。与此配套，mod 还把结构化的 `reward_details[]`
（与 `options[]` 逐条对齐）发给 core，省得 core 去解析文案。

**执行侧统一用「同一份定义」的下标**：`Observer.liveRewards()` 是奖励列表的唯一定义点，
`Observer.cardRewardCards()` 是三选一的唯一定义点；观测与执行必须共用，否则选的
和拿的会错位。

### 选牌界面的"关闭"语义（`closeSelectScreen`）

事件（`UpgradeShrine` 等）与篝火锻造（`CampfireSmithEffect`）、商店删牌
（`ShopRoom.updatePurge`）都是**轮询**取结果的，判据一模一样：

```
if (!AbstractDungeon.isScreenUp && !gridSelectScreen.selectedCards.isEmpty()) { ... }
```

而 `AbstractDungeon.closeCurrentScreen()` 只在 `previousScreen == null` 时才经
`genericScreenOverlayReset()` 把 `isScreenUp` 置假；`previousScreen` 被上个界面留着
（卡牌奖励界面会把 `previousScreen` 设回 `COMBAT_REWARD`）的时候，`isScreenUp`
会一直是 true —— **玩家的选择永远等不到那一刀，界面看着关了、事件却没反应**。
真机症状就是"选了不会进下一步"。

所以执行侧走 `Actor.closeSelectScreen()`：关界面后**若 `screen` 确实回到了 `NONE`**，
再强制 `isScreenUp = false`。判据取"关完之后 `screen` 是不是真的 `NONE`"，
这样绝不会覆盖游戏自己开出来的上一屏（商店删牌会回到 `SHOP`，那时不强制）。

### 「同一次操作重复上报」与「同一动作重复执行」

两条不同的重复，成因不同：

1. **人类动作重复上报** —— `HumanActionTap` 的 patch 是无条件的前缀，
   `CardRewardScreen.acquireCard` 会被 `cardSelectUpdate()` **每帧**调用（鼠标停在
   那张牌上就一直调），真机上同一张卡刷了 56 条 `select_reward`。修法是按 **对象
   identity** 加闩锁：同一张牌只报一次，界面清空（`Observer.cardRewardCards()` 变空）
   时由 `HumanActionTap.pollCardRewardLatch()` 复位。对话框的 `waitForInput`
   上升/下降沿闩锁是同一个道理。
2. **agent 动作重复执行** —— 靠上面那两处修掉：`closeSelectScreen()` 让
   "选完真的进下一步"，"只置 `isDone`"让"领取真的生效"。

#### 各执行路径必须"推进局面"的具体做法

同一个病根（执行了游戏的一半，另一半没做）在四条路径上都出现过，一条一条列在这里，
改执行器时照单核对：

| 动作 | 必须做的事 | 漏了会怎样 |
|---|---|---|
| `select_cards`（GRID 必选 k 张） | 铺 `selectedCards` → `closeSelectScreen()`（关屏 + 若 `screen==NONE` 强制 `isScreenUp=false`） | 事件/锻造靠 `!isScreenUp && !selectedCards.isEmpty()` 轮询，界面关了但事件没反应，下一帧又在同一界面重新问 |
| `select_reward`(CARD_REWARD) | `FastCardObtainEffect` + `takeReward()`（从**界面那份** rewards 里 remove）+ `closeCurrentScreen()` | 牌不进牌组、条目删不掉、界面卡在 CARD_REWARD |
| `select_choice`(COMBAT_REWARD) | 只置 `item.isDone = true`，交给 `rewardViewUpdate()` | 自己调 `claimReward()` 再把 `false` 当失败，条目反复出现 |
| `select_choice`(REST) | `useOption()` **且** `campfireUI.somethingSelected = true` | `CampfireUI.update()` 只有看到 `somethingSelected` 才会减 `hideStuffTimer` 并置 `hidden`；漏了它按钮永远可点，agent 会把"休息"反复选（金币/血量各扣一次的那种） |
| `select_choice`(SHOP 的 `purge` 槽) | 复刻 private 的 `ShopScreen.purchasePurge()`：`previousScreen = SHOP` + `gridSelectScreen.open(purgeable, 1, NAMES[13], false,false,true,true)` | `ShopScreen.purgeCard()` 是**结账**动作（扣钱、抬价）而不是入口；调错就是钱扣了、牌没删、`purgeAvailable` 还是 true，于是反复买同一个空操作 |

所有索引都走"同一份定义"：`Observer.liveRewards()`（奖励）、`Observer.cardRewardCards()`
（三选一）、`Actor.selectionPool()`（选牌）、`ShopSlots.list()`（货架）、
`CampfireSlots.list()`（篝火）。观测与执行必须共用，不允许在任一侧重新数一遍。

## 子选择裁决器

一个决策点回答完，常常还要"再选一次"（出牌要选目标、事件要选一张牌）。这些
**不是**在出牌那一题里预判，而是作为**新的稳定观测**到达 agent，自然成为新的
决策点 —— 这正是"统一性原则"：所有子选择都是普通决策点，没有特殊流程。

按候选的"选择形态"分三类裁决：

| 形态 | 题型 | 裁决 |
|---|---|---|
| **单选**（选目标、单张牌、单选项） | 一道 `choice` | 取概率最大的项 |
| **任意多选**（k 自由） | 每候选一道 `score`（5 级有序量级） | 按期望分排序取 top-k，k 由约束与阈值共同决定 |
| **必选 k 张** | `choice` 连问 k 次 | 每轮从剩余候选里取概率最大的项 |

**平票 / 缺字段**一律走**确定性 tie-break**（按候选枚举顺序），保证同一局面回放
得到同一答案。理由：数据集与复现都要求 `decisions.jsonl` 能逐字节重放，
随机 tie-break 会让"固定种子重放决策序列一致"这条验收不成立。

## 合法性：不合法的动作不给 Laya

**唯一的事实来源是 agent 侧的观测，不是游戏 API 的结论。** 血泪教训：空药水槽
（`PotionSlot`）的 `canUse()` 返回 true，照抄之后候选里出现 `potion:p0`
「Use potion Potion Slot.」，模型选一次被拒一次、局面又不变，于是无限空转
（见 [04-fairness](04-fairness.md#药水空槽不是药水)）。

两道闸门：

1. **枚举器只产出合法动作** —— 打不出来的牌、买不起的商品、空药水槽、已死敌人
   根本不进候选。模型看不到，也没有机会选。
2. **构题前再过滤一次**（`spire_core.legality.legal_only`）—— 拦枚举器的 bug，
   被摘掉的候选连同原因记进 `Plan.dropped_candidates`。正常永远是空的；非空就是
   信号（`pipeline` 会打 warning），说明候选枚举和实际执行对不上了。

模组侧还有第二层白名单（`ActionSpec.validate`），但那是防"agent 给的动作和当前
局面错位"的最后一道，不是第一道防线。

### 强制决策短路

`len(candidates) == 1` 时**没有可决策的东西**：直接执行，不问模型
（`runner.forced_choice`）。典型局面：0 能量且无药水（只剩 `end_turn`）、地图只有
一条路、商店只剩离开。省 budget，也避免模型在无选择余地的局面上乱答。

注意这不是"兜底"：行的 `meta.forced = true`，但 `label_source` 仍是 `agent`，
**照常进训练集**（它是"这个局面不需要模型"，不是"模型挂了"）。

例外：`select_card_any` 的"一张都不选"是一个真实选项，剩一个候选也不算没得选，
所以不短路。

`arbitrate.py` 提供两个原语，所有决策点共用：

```python
def pick_choice(parsed: dict[str, ParsedAnswer], question_id: str,
                allowed: list[str]) -> tuple[str, float, dict[str, float]]:
    """单选：返回 (候选 id, 答案置信度, 全概率)；`allowed` 的顺序即 tie-break 顺序"""

def pick_topk(expected: dict[str, float], k_min: int, k_max: int, *,
              threshold_level: float = SCORE_SELECT_THRESHOLD,
              order: list[str] | None = None) -> list[str]:
    """多选：按期望分降序，满足 k_min/k_max 与阈值，返回选中的 id 列表"""
```

规则：

- **单选一律 `choice`**（`select_target`、`card_reward`、`event_option`、`map_node`、`combat_play`、`shop`、`rest_site`…）。
- **任意多选一律 `score` + `pick_topk`**（`select_card_any`）。
- **必选 k 张一律 `choice` 连问 k 次**（`select_card_must_k`）。
- **平票**：按候选枚举顺序取先者（确定性）。`pick_choice` 在概率并列时按 criteria 的插入顺序，**不按字典序**。
- 答案缺失/非法（候选 id 不在本次 criteria 里）：记为 `parse_error`，走兜底。

## 虚拟思考链（v1 关闭，钩子已留）

`decision_points.toml` 里每个决策点可以有可选的 `prelude` 段（v1 全部是空列表）：

```toml
[decision_points.combat_play]
question_id = "q_action"
type = "choice"
instructions = "Given the state above, choose the single best action."

prelude = [
  { id = "q_threat", type = "choice", instructions = "Which enemy poses the greatest threat this turn?" },
  { id = "q_lethal", type = "noul",   instructions = "Can you kill all enemies this turn?" },
]
```

执行方式：先问 `prelude`（同一请求内，与主问题并行），把**带概率的答案**以 `prior: {"q_threat": "m0", "q_lethal": true}` 的形式**拼回 state**，再发第二个请求问 `q_action`。

v1 默认 `prelude: []`（关闭），因为要先用干净的单步数据把闭环跑通；开启后产生的行会多出 `prior` 字段，训练时可作为额外输入特征。见 [12-roadmap](12-roadmap.md#虚拟思考链)。

## 相关文档

- 预算与压缩 -> [05-state-schema](05-state-schema.md#预算与压缩)
- Laya 怎么被调用 -> [07-laya-contract](07-laya-contract.md)
- 采集行格式 -> [08-dataset](08-dataset.md)

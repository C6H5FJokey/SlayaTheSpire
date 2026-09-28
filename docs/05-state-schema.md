# 05 状态 schema

本文定义**送给 Laya 的 `state`**（公平视图序列化后的英文紧凑 JSON）与 **mod 上报的 `raw`**（未过滤观测）。

Laya 的 `state` 接受任意 JSON（内部 `json.dumps` 后送模型），因此我们只需要保证**紧凑、确定性、英文、预算内**。

## 原始观测（raw）

mod 上报的完整结构。**字段名与游戏对象一一对应**，不做美化。

```jsonc
{
  "game_version": "2.3.4",
  "screen": "NONE",                  // NONE | CARD_REWARD | MAP | EVENT | SHOP_ROOM | REST | GRID | ...
  "screen_state": { },               // 界面相关（选项文本、可选牌、商店库存、奖励明细、
                                     //              选牌来由 origin / event_name / event_text）
  "in_combat": true,
  "room": { "act": 1, "floor": 7, "node": 5, "type": "MONSTER", "room_id": 1234 },
  "act": 1, "floor": 7,
  "ascension": 0,
  "run_seed": -3047511808784702860,  // 公平模式下会被剥掉
  "player": {
    "character": "IRONCLAD",
    "hp": 68, "max_hp": 75, "block": 0, "energy": 3, "gold": 142,
    // name / text 是**英文**（模组从游戏 jar 的 localization/eng/*.json 按 ID 取，
    // 与客户端语言解耦，见 04-fairness#文本语言）。
    "powers": [ { "id": "Strength", "amount": 2, "name": "Strength",
                  "text": "Attacks deal 2 additional damage." } ],
    "relics": [ { "id": "BurningBlood", "counter": -1, "name": "Burning Blood",
                  "text": "At the end of combat, heal 6 HP." } ],
    // 空槽是 { "id": "Potion Slot", "can_use": false }。游戏里 PotionSlot.canUse() 同样
    // 返回 true（它判空槽从不看这个），所以模组显式压成 false，agent 才不会拿空槽当药水。
    // empty=true 表示空槽（老观测没有这个字段时按 id "Potion Slot" 兜底判）。
    "potions": [ { "id": "FirePotion", "can_use": true, "requires_target": true, "empty": false,
                   "name": "Fire Potion", "text": "Deal 20 damage." }, null ],
    "potion_slots": 3
  },
  "combat": {
    "turn": 3,
    "hand":  [ /* RawCard */ ],
    "draw_pile":   [ /* RawCard，顺序敏感 */ ],
    "discard_pile":[ /* RawCard */ ],
    "exhaust_pile":[ /* RawCard */ ],
    "monsters": [ /* RawMonster */ ]
  },
  "deck": [ /* RawCard */ ],
  "map": { /* 见下 */ }
}
```

### `RawCard`

```jsonc
{
  "index": 2,              // 所在区域的 0-based 下标（手牌/牌堆都适用）
  "id": "Strike_R",
  "name": "Strike",
  "type": "ATTACK",        // ATTACK | SKILL | POWER | STATUS | CURSE
  "cost": 1, "cost_for_turn": 1,
  "upgrades": 0,
  "rarity": "BASIC",
  "exhausts": false, "ethereal": false, "is_playable": true, "has_target": true,
  "target_type": "ENEMY",  // ENEMY | ALL_ENEMY | SELF | NONE | ...
  "uuid": "0560233c-...",
  "text": "Deal 6 damage.",   // 已升级后的实际描述文本（英文）
  "damage": 6, "block": 0, "magic_number": 0   // 关键数值，便于精确描述
}
```

### `RawMonster`

```jsonc
{
  "index": 0, "id": "JawWorm", "name": "Jaw Worm",
  "hp": 42, "max_hp": 46, "block": 0, "half_dead": false, "is_gone": false,
  "powers": [],
  "intent": {
    "id": "ATTACK",           // ATTACK | ATTACK_BUFF | ATTACK_DEBUFF | ATTACK_DEFEND | BUFF | DEBUFF |
                              // DEFEND | DEFEND_BUFF | DEFEND_DEBUFF | SLEEP | STUN | UNKNOWN | NONE
    "hits": 1,
    "base_damage": 12,        // 公平模式下会被剥掉
    "adjusted_damage": 12,    // 公平模式只保留这个
    "text": "Chomp for 12 damage."
  },
  "move_history": [1, 4, 1],  // 公平模式下会被剥掉
  "upcoming_moves": [1, 4]    // 公平模式下会被剥掉
}
```

### `map`

```jsonc
{
  "act": 1,
  "nodes": [
    { "id": "n3_2", "x": 3, "y": 2, "type": "MONSTER", "children": ["n2_3", "n4_3"] }
  ],
  "current": "n3_2",                 // 当前位置（还没选下一个时为已走过的节点）
  "reachable": ["n2_3", "n4_3"],     // 当前可选的下一层节点 id
  "boss": "n_0_15",
  "boss_relic_taken": false
}
```

`type` 取值：`MONSTER | ELITE | EVENT | REST | SHOP | TREASURE | BOSS | UNKNOWN`。

### `screen_state`

界面相关状态。未用到的界面留空即可；下面是全部字段与它们出现的界面。

| 字段 | 出现界面 | 说明 |
|---|---|---|
| `options[]` / `option_ids[]` | EVENT / NEOW / COMBAT_REWARD / REST / SHOP | 界面按钮文本与机器可读 id |
| `select_cards[]` | GRID / HAND_SELECT | 可选牌（`zone` + `index` + 可选的 `draw_order`），顺序即下标 |
| `min_select` / `max_select` | GRID / HAND_SELECT | `min == max > 0` 是"必选 k 张"，否则"任意多选" |
| `origin` | GRID / HAND_SELECT | 选牌的**来由**，见下 |
| `reason` | GRID / HAND_SELECT / 其它按钮界面 | 游戏写在界面上的那句提示语（英文），玩家抬头就能读到。拿不到时整个字段不出现 |
| `event_name` / `event_text` | 事件房里的任意界面 | 事件英文名与开场正文 |
| `reward_cards[]` | CARD_REWARD | 三选一的候选牌 |
| `reward_relics[]` | BOSS_RELIC / COMBAT_REWARD | Boss / 宝箱备选遗物 |
| `reward_details[]` | COMBAT_REWARD | 与 `options[]` **逐条对齐**的结构化奖励明细，见下 |
| `shop_items[]` | SHOP_ROOM | 货架（`kind`/`id`/`price`/`affordable`/卡面文本） |
| `rest_options[]` | REST | "Rest" / "Smith" 等按钮文本 |
| `neow_options[]` | NEOW | Neow 起始选项 |

`origin` 取值：`rest_smith | event | transform | purge | upgrade | confirm | combat_select | scry | select | hand_select`。

选牌界面本身不带语义，"同一张 `Strike` 该升、该删、还是留着"完全取决于谁开的这块界面，
所以来由必须进 state（详见 [06-decision-points](06-decision-points.md#select_card_must_k必选-k-张)）。
其中两个是**战斗内的牌堆检索**：

- `combat_select`：头槌（弃牌堆 -> 抽牌堆顶）、全息影像（弃牌堆 -> 手牌）、发掘（消耗堆）、
  秘密技法/秘密武器、万能药……具体效果由界面上的 `reason` 说明（"Choose a Card to Put on
  Top of Your Draw Pile."），`origin` 只负责说清"这是战斗里的一次挑牌"。
- `scry`：观者的预见。候选牌的 `zone` 与 `draw_order` 一起给出"这几张牌在抽牌堆顶的
  顺序"（`draw_order = 1` 是下一张会抽到的牌），这一小段顺序玩家在界面上看得见，
  见 [04-fairness](04-fairness.md#3-预见的例外只有雪片一样摊在界面上的那几张牌才有顺序)。

`zone` 的取值是 `hand | draw | discard | exhaust | deck | offer`。`offer` 表示"这组候选牌
不属于任何一个牌堆"—— 攻击/技能/能力药水那种当场造出来给你挑的牌（见 `ZoneGuess`，
纯逻辑、可离线单测）。临时牌组（预见 / 秘密技法 / 药水）跟玩家牌堆不是同一个对象，
只能按"这些牌现在住在哪个堆"反查；一个牌实例同时只住一个堆，所以判据是精确的。

`reward_details[]` 的每一项：

```jsonc
{ "kind": "card",   "text": "Card reward (Strike / Pommel Strike / Inflame)",
  "cards": [ { "card": "Strike_R", "up": 0, "rarity": "BASIC", "type": "ATTACK" }, ... ] }
{ "kind": "relic",  "text": "Relic: Anchor", "id": "Anchor" }
{ "kind": "potion", "text": "Potion: Fire Potion", "id": "FirePotion" }
{ "kind": "gold",   "text": "Gold (25)", "amount": 25 }
```

`cards` / `id` / `amount` 都是**玩家在奖励界面上真的看得到**的东西（公平信息，
见 [04-fairness](04-fairness.md#字段级规则)），存在的意义是让模型不必"点进去看一眼
再退出来"就能判断值不值得领。

## 公平视图（fair）

由 `fairness.filter(raw)` 构造。字段与 raw 同构，但：

- 删除 `run_seed`；
- 删除 `map` 之外的一切内部 id；
- `draw_pile` / `discard_pile` / `exhaust_pile` / `deck` 全部**按多重集排序**，条目压缩成 `{id, up, n}`（`CardStack.to_dict()`）；
- `hand` 保留顺序（保留 `index`）；
- 怪物只保留 `intent.adjusted_damage`，删除 `base_damage` / `move_history` / `upcoming_moves`。

## 序列化规则（fair -> state）

`serialize.state(fair) -> dict`，目标是**紧凑、英文、确定性**。

### 1. 实例短 id

| 前缀 | 含义 | 例 |
|---|---|---|
| `h<i>` | 手牌第 i 张 | `h0` |
| `m<i>` | 敌人第 i 个 | `m0` |
| `p<i>` | 药水槽第 i 个 | `p0` |
| `r<i>` | 遗物第 i 个 | `r2` |
| `n<x>_<y>` | 地图节点 | `n3_2` |

短 id 是**候选 id 的组成部分**（`play:h2->m0`），因此必须稳定：顺序来自游戏数组下标，不做排序。

敌人（`m<i>`）的下标是**游戏数组下标**（`room.monsters.monsters` 中的位置），**不是"活着的敌人里的第几个"**。
这个数组在整场战斗里只增不减，尸体继续占位，所以两个敌人杀掉 `m0` 之后，活着的那个仍然是 `m1`。
模组的观测（`combat.monsters[].index`）、人类目标上报与动作校验必须共用这一个坐标系 ——
任何"先过滤出活着的、再按下标取"的写法都会在**第一次击杀后整体错位**，让此后所有带目标的动作
被误判越界（真机症状：模型答了牌，agent 却不执行，回合被白白结束）。

### 2. 同名牌折叠

同一区域内 `(id, upgrades)` 相同的牌折叠：

```jsonc
"zones": {
  "draw":   { "total": 12, "stacks": [ {"id":"Strike_R","up":0,"n":4}, {"id":"Defend_R","up":0,"n":5} ] },
  "discard":{ "total": 3,  "stacks": [ ... ] },
  "exhaust":{ "total": 1,  "stacks": [ ... ] },
  "deck":   { "total": 22, "stacks": [ ... ] }
}
```

手牌**不折叠**（需要位置），但同名重复照常逐张列出。

### 3. 文本裁剪

- 卡牌文本取自游戏自带的英文资源（`localization/eng/cards.json`），`!D!/!B!/!M!` 用卡实例上的
  实际数值填充；数值还没算出来（奖励/牌组/商店里的卡，实测 `damage == -1`）时退回
  `baseDamage/baseBlock/baseMagicNumber`，都没有则写 `?`——**绝不把 -1 写进文本**。
- 单张卡文本上限 160 字符；超长截断并追加 `…`，但**必须保留所有数字**（实现时按句子裁剪，优先丢修饰语）。
- 描述相同的牌在折叠组里只出现一次。

### 4. 字段顺序与浮点

- 所有 dict 输出时 `sort_keys=True` 不利于阅读，因此**显式按固定顺序构造**（Python 3.7+ dict 保序），并在测试里断言 `json.dumps` 结果逐字节稳定。
- 凡是把概率/伤害写成浮点的，统一 `round(x, 3)`，避免 `0.30000000000000004` 污染哈希。

### 5. 确定性

`serialize` 必须是**纯函数**：同样的 `fair` 输入必然得到逐字节相同的输出。不依赖时间、随机数、dict 迭代顺序、环境。

## state 示例（战斗内）

```jsonc
{
  "mode": "combat",
  "act": 1, "floor": 7, "ascension": 0,
  "player": {
    "character": "IRONCLAD",
    "hp": 68, "max_hp": 75, "block": 0, "energy": 3, "gold": 142,
    "powers": [ { "id": "Strength", "amount": 2 } ],
    "relics": [ { "id": "BurningBlood" } ],
    "potions": [ { "slot": 0, "id": "FirePotion", "target": true } ]
  },
  "combat": { "turn": 3, "cards_discarded_this_turn": 0, "times_damaged": 0 },
  "hand": [
    { "id": "h0", "card": "Strike_R", "up": 0, "cost": 1, "playable": true,
      "target": "ENEMY", "text": "Deal 6 damage." },
    { "id": "h1", "card": "Bash", "up": 0, "cost": 2, "playable": true,
      "target": "ENEMY", "text": "Deal 8 damage. Apply 2 Vulnerable." }
  ],
  "monsters": [
    { "id": "m0", "name": "Jaw Worm", "hp": 42, "max_hp": 46, "block": 0,
      "powers": [], "intent": "Chomp for 12 damage.", "intent_damage": 12, "intent_hits": 1 }
  ],
  "zones": {
    "draw":    { "total": 8,  "stacks": [ { "id": "Strike_R", "up": 0, "n": 3 } ] },
    "discard": { "total": 4,  "stacks": [ ] },
    "exhaust": { "total": 0,  "stacks": [ ] },
    "deck":    { "total": 12, "stacks": [ { "id": "Strike_R", "up": 0, "n": 5 } ] }
  },
  "deck_summary": "12 cards (6 distinct, 0 upgraded)",   // compact_deck=true 时的卡组强度摘要
}
```

### 5. 哪些"人类看得见的事实"会进 state

只给 id 等于让模型猜（`BurningBlood` 是什么？这个 power 有什么效果？）。下面这些都在
state 里（可见性依据见 [04-fairness](04-fairness.md#字段级规则)）：

| 位置 | 字段 | 说明 |
|---|---|---|
| `player.relics[]` | `id` / `counter` / `name` / `text` | `counter` 是遗物内部计数（Burning Blood 回复过多少、Art of War 攒了几层），人类悬停看得到 |
| `player.potions[]` | `slot` / `id` / `name` / `text` / `target` / `usable` | **只含真药水**；空槽不进这个数组 |
| `player.potion_slots` | int | 槽位总数（人类看得见有几格） |
| `player.powers[]` | `id` / `amount` / `name` / `text` | 增益/减益的说明文本 |
| 手牌 / 奖励卡 / 可选牌 / 商店卡 | `type` / `rarity` | 攻击/技能/能力与稀有度，选卡时的第一眼信息 |
| `screen.selectable[]` | `draw_order` | 只有预见界面才有：`1` 是下一张会抽到的牌（界面上的先后顺序） |
| `monsters[].powers[]` | `id` / `amount` / `name` / `text` | 同上 |
| `screen.shop[]` | `index` / `kind` / `id` / `name` / `text` / `price` / `affordable` | 原来只有 kind/id/price，人类在货架上读得到卡面 |

## 预算与压缩

Laya 服务端硬限制（见 [07-laya-contract](07-laya-contract.md#硬预算)）：

| 限制 | 值 |
|---|---|
| `state` 序列化后字符数 | <= 50000 |
| 单请求问题数 | <= 64 |
| 单个 choice 的选项数 | <= 100 |
| 所有问题的选项总数 | <= 512 |
| 请求体字节数 | <= 2 MiB |

`budget.enforce()` 的压缩优先级（从先动手的开始）：

1. 折叠 `zones` 里的同名牌（通常是超大优势，先做）。
2. 卡牌文本截断到 160 字符。
3. `zones.deck` 折叠为**只给 `total` / `stacks` 的计数**，不给每张文本，另给顶层 `deck_summary`（如 `"12 cards (6 distinct, 0 upgraded)"`）；牌组内容对当前决策通常不重要，且不损失公平性。
4. 超出 100 个候选时，按**确定性粗排**截断（战斗内：可出牌按费用升序、目标按 index 升序），并把被截断项记进 `meta.truncated_candidates`。
5. 仍超限：抛 `BudgetExceeded`，由 agent 记为异常行（**不静默截断 state**，因为静默截断会破坏公平性与可复现性）。

## 相关文档

- 可见性规则 -> [04-fairness](04-fairness.md)
- 问题怎么构造 -> [06-decision-points](06-decision-points.md)

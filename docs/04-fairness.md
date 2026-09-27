# 04 公平视图

## 原则

**模型只能看到「游戏内玩家自己能看到的信息」。** 这条规则是数据可信度的基础：如果训练/推理时喂了隐藏信息，得到的胜率不可比，微调出来的模型也会依赖推理时不存在的特征。

实现上是**两层分离**：

- **mod 上报原始观测**（`raw`，未过滤，含隐藏信息）—— mod 不做判断。
- **`spire-core.fairness` 产出公平视图**（`FairObservation`）—— 唯一允许进入 `state` 的东西。

原始观测只用于落盘备查（`runs/<run_id>/raw_states.jsonl`），供日后做"全知模式"对照实验。

## 字段级规则

| 信息 | 可见性 | 说明 |
|---|---|---|
| 玩家 HP / 最大 HP / 格挡 / 能量 | 可见 | 界面直接显示 |
| 玩家 powers（增益/减益） | 可见 | 图标 + 数值 |
| 金币 / 层数 / 幕 / 进阶等级 | 可见 | 顶部栏 |
| 遗物（全部，含数值） | 可见 | 可随时查看 |
| 药水（全部，含数值） | 可见 | 可随时查看 |
| 遗物 / 药水 / powers 的**名字与效果文本** | 可见 | 鼠标悬停就能读到。只给 id 等于让模型猜"BurningBlood 是什么" |
| 卡牌的 `type` 与 `rarity` | 可见 | 牌面本身就画着（攻击/技能/能力、稀有度颜色），选卡奖励时的第一眼信息 |
| 商店商品的**名字与效果** | 可见 | 货架上的卡面就写着 |
| 事件选项文本 | 可见 | 按钮上的字 |
| 奖励界面每条奖励的**已揭晓内容** | 可见 | `COMBAT_REWARD` 里卡牌奖励那三张牌的名字（`reward_details[].cards`）、遗物/药水的 id。界面上本来就能看到，不给只会让模型"点进去看一眼再退出" |
| 选牌界面的 `origin` / 事件名与正文 | 可见 | 事件正文是进入事件时就显示的文字；`origin`（升级/删牌/变形/事件…）是"为什么在选牌"的语义，不给就无法判断该选哪张 |
| 手牌（全部内容） | 可见 | |
| 主牌组（全部内容） | 可见 | 可查看牌组 |
| 弃牌堆内容 | 可见 | 可查看弃牌堆 |
| 消耗堆内容 | 可见 | 可查看消耗堆 |
| **抽牌堆：仅多重集** | **顺序不可见** | STS 不允许查看抽牌堆顺序。只暴露「抽牌堆里有 Strike×4、Defend×3…」，**顺序必须剥掉** |
| 敌人名称 / HP / 最大 HP / 格挡 / powers | 可见 | |
| 敌人**当前意图**及其**结算后伤害值** | 可见 | 界面显示的就是结算后的数字，所以只给 `adjusted_damage` |
| 敌人 `base_damage`（未结算伤害） | **不可见** | 属于内部数值，反推力量/易伤会引入不公平优势 |
| 敌人的**未来行动序列** | **不可见** | 只给当前意图 |
| 整幕地图（全部节点与路径） | 可见 | 进一幕即可看到完整地图 |
| 当前房间类型 / 通道位置 | 可见 | |
| run seed | **不可见** | 可用于预测 RNG，属泄漏 |
| 事件结果 / 宝箱内容 / 未来商店库存 | **不可见** | |
| 洗牌顺序 / 抽牌顺序 | **不可见** | |
| 怪物 AI 的 RNG 状态 | **不可见** | |

### 文本语言：一律英文，且**与客户端语言无关**

游戏对象上的 `AbstractCard.name` / `rawDescription` 是**跟随界面语言**的：中文客户端里
实测写出过 `"Play 打击 (1E): 造成 6 点伤害"`。契约要求送进模型的 state 是英文（走英文
checkpoint），而且**同一个局面在中英文客户端上必须产出同一份 state** —— 否则数据集会被
玩家语言污染。

所以模组从游戏 jar 自带的 `localization/eng/{cards,monsters,relics,potions,powers,events}.json`
按 **ID** 取英文名与英文描述（`spireagent.obs.Eng`），游戏对象上的名字只作兜底。

两个坑记在这里：

- **`!D!` / `!B!` / `!M!` 的数值可能还没算出来。** 战斗中的手牌已经被 `applyPowers()`
  算过，但奖励/牌组/商店里的卡没有，`damage` / `block` 实测是 `-1`，于是写出过
  「造成 -1 点伤害」。修法是先用卡实例上的值，为负时退回 `baseDamage` / `baseBlock` /
  `baseMagicNumber`，两者都没有就写 `?` —— **绝不把 -1 写进文本**。
- **`NL` 是换行 token**，必须带词边界替换（`\bNL\b`），否则 `ONLY` 里的 `NL` 也会被吃掉。

事件选项资源是**按数值切开的碎片**（一项以 `[` 开头，后续碎片是同一项的续写）。拼回来时
拿不到运行时数值，就在拼接处写 `?`——与"宁可丢修饰语也不丢数字"同一条原则：不发明数值，
但标出这里有个数值。

### 药水：空槽不是药水

空药水槽在游戏里也是 `PotionSlot` 对象（id 就是 `"Potion Slot"`），而它的 **`canUse()` 返回 true**。
照抄这个结论的后果是实测发生的：候选里出现 `potion:p0`「Use potion Potion Slot.」，模型选一次
被拒一次、局面又不变，于是无限空转；地图候选上还写着"你带着 3 瓶药水"，而玩家一瓶都没有。

规则：**agent 侧的合法性判定以观测里的事实为准**（`RawPotion.is_empty`，模组用
`PotionFacts.EMPTY_SLOT_ID` 显式给 `empty`，老版本观测按 id 兜底），不信游戏 API 的结论。
空槽不进 `player.potions`，但 `player.potion_slots` 保留槽位总数（人类看得见有几格）。

## 两种模式

```toml
[agent]
fairness_mode = "strict"   # 默认
```

| 模式 | 行为 |
|---|---|
| `strict`（默认） | 严格执行上表，剥掉一切不可见字段 |
| `omniscient` | 保留全部原始字段（含抽牌堆顺序、seed、未来意图）。**仅用于离线对照实验**，产生的采集行会被标 `fairness_mode: omniscient`，训练导出时默认排除 |

`omniscient` 的存在意义是回答"如果模型全知，能强多少"——它是研究工具，不是产品的运行模式。

## 实现约定

### 1. 过滤是「删字段」，不是「置空」

`FairObservation` 是一份**独立的、字段更少的结构**，由 `filter(raw)` 显式构造。不要用「先复制再打码」的写法——那会让新增字段默认泄漏。

```python
# 正确：白名单式构造
def filter_card_in_zone(c: RawCard) -> ZoneCard: ...
def filter_monster(m: RawMonster) -> Monster:
    return Monster(name=m.name, hp=m.hp, ..., intent=Intent(...adjusted_damage...))
```

新增字段时的规矩：**默认不进 `FairObservation`，必须显式加进去并在此文档登记。**

### 2. 抽牌堆变多重集

```python
# raw.draw_pile 是 [Strike_R, Defend_R, Strike_R, Bash]（有顺序）
# fair.draw_pile 必须是排序后的多重集，且不保留顺序信息
draw_pile_multiset = sorted((c.id, c.upgrades) for c in raw.draw_pile)
```

注意：**牌组、弃牌堆、消耗堆也一律按多重集暴露**——虽然玩家理论上能看到弃牌堆顺序（游戏内可查看），但顺序在决策上无意义，而保留顺序会让模型学到"洗牌后第一张是什么"的伪特征。统一按多重集处理，避免噪声。

### 3. 手牌例外

**手牌必须保留顺序**：STS 里手牌的位置是玩家实际操作的依据（第几张牌），模型需要位置来给出稳定的候选 id（`h0`/`h1`/…）。这是玩家可见且必须可见的信息。

### 4. 哈希

`fair_state_hash = sha256(json.dumps(fair_state, sort_keys=True, ensure_ascii=False))[:16]`。

用途：
- Laya 调用缓存键；
- 数据集去重与"同一状态不同决策"的分析；
- 回放时校验重建结果一致。

### 5. 与数据集的关系

**数据集只含公平视图。** `dataset/*.jsonl` 里的 `state` 字段就是送给 Laya 的那份公平 state 原文。`raw_states.jsonl` 里的原始观测**不进入 dataset 导出**，只在需要做全知研究时单独处理。

这就是"公平性即数据契约"：训练分布 == 推理分布。

## 测试要求

见 [11-testing](11-testing.md#无泄漏断言)。核心是**负向断言**：

```python
def test_no_order_leak(fair):
    assert "draw_pile_order" not in fair
    # 断言多重集里没有任何顺序信息
    assert isinstance(fair.draw_pile, list)
    assert fair.draw_pile == sorted(fair.draw_pile)

def test_no_seed(fair, serialized):
    assert "seed" not in json.dumps(serialized)
```

任何新增字段都必须补一条对应的"不泄漏"断言。

## 相关文档

- 序列化后的样子 -> [05-state-schema](05-state-schema.md)
- 数据集字段 -> [08-dataset](08-dataset.md)

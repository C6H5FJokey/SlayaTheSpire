# 03 模组线协议

模组在 `127.0.0.1:17777` 上开一个裸 TCP 服务，agent 主动连入。**这是 mod 与 agent 之间唯一的通道。**

## 传输层

| 项 | 约定 |
|---|---|
| 地址 | 默认 `127.0.0.1:17777`。**这是模组侧唯一需要配的东西**（`host` / `port`），因为要先有端口才能连上；agent 侧对应 `[mod] host/port`，两边必须一致 |
| 绑定 | **仅 loopback**，绝不监听外网 |
| 连接数 | 同时只接受 1 个客户端；新连接到达时踢掉旧的（agent 重启场景） |
| 编码 | UTF-8 |
| 分帧 | **NDJSON**：一行一条 JSON，`\n` 结尾。JSON 内不允许裸换行 |
| 最大帧 | 4 MiB；超限断开连接并记日志 |
| 双方角色 | 消息可双向发送；mod 是"服务端"，agent 是"客户端" |

## 信封

所有消息统一信封：

```jsonc
{
  "v": 2,                 // 协议版本
  "type": "observation",  // 消息类型
  "id": 412,              // 发送方自增序号（用于配对 request/response）
  "reply_to": null,       // 响应类消息填被响应消息的 id
  "payload": { }          // 类型相关负载
}
```

## 消息清单

| type | 方向 | 说明 |
|---|---|---|
| `hello` | mod -> agent | 连接建立后 mod 发的第一条消息；能力协商 |
| `configure` | agent -> mod | **握手后 agent 发的第一条语义消息**（心跳 `ping` 可能更早）：推送 `mode` 与 `watchdog_sec` |
| `configured` | mod -> agent | `configure` 的回执（`reply_to` 指向 configure.id） |
| `observation` | mod -> agent | 稳定态下的完整观测 |
| `human_action` | mod -> agent | `mode=observe_human` 时，人类**已提交**的语义动作 |
| `action` | agent -> mod | 要求执行一个语义动作 |
| `action_result` | mod -> agent | 动作执行结果（`reply_to` 指向 action.id） |
| `ping` | 双向 | 心跳 |
| `pong` | 双向 | 心跳应答 |

### `hello`

```jsonc
{
  "game_version": "2.3.4",
  "mod_version": "0.1.0",
  "protocol": 2,
  "capabilities": ["observe", "act", "human_action", "watchdog", "configure"],
  "watchdog_sec": 30,        // 当前生效值；configure 之前是编译期默认
  "configured": false        // 永远是 false —— 这条连接还没被配置
}
```

agent 收到 `hello` 后校验 `protocol == 2`，不符则报错退出（不做降级兼容），
**然后立刻发 `configure`**。

### `configure`

模组**没有自己的模式**。所有行为开关都由这条消息推送：

```jsonc
{ "mode": "agent", "watchdog_sec": 30 }   // mode: agent | observe_human
```

回执：

```jsonc
{ "ok": true, "mode": "agent", "watchdog_sec": 30 }
{ "ok": false, "error": "unknown mode: observe" }   // 校验失败，状态不变
```

规则：

- 校验失败**不改变当前状态**，只回错误 —— 半配置的模组比没配置的模组更危险。
- 每条 TCP 连接都要重新 `configure`（重连可能换模式，不能继承上一条连接的）。`onDisconnect` 会把模组打回未配置态。
- **在收到 `configure` 之前，模组是哑的**：不发 `observation`、不 arm 看门狗、不执行任何 `action`（回 `E_NOT_CONFIGURED`）、不上报 `human_action`。
- `watchdog_sec` 允许范围 `[5, 3600]`；不传则用 30。

这条设计换来了一个直接后果：**切模式不需要碰游戏、不需要重启游戏**，只要用不同的 `mode` 重跑 agent：

```powershell
python -m spire_agent run    --mode observe_human      # 直接这么跑
python -m spire_agent doctor --mode observe_human      # 只验握手 + configure 回执，不开跑
```

### `observation`

```jsonc
{
  "seq": 412,
  "stable": true,
  "room": { "act": 1, "floor": 7, "node": 5, "type": "MONSTER" },
  "raw": { /* 未过滤观测，schema 见 05-state-schema.md#原始观测 */ }
}
```

- `raw` 是**未过滤**的：包含隐藏信息（抽牌堆顺序、敌人完整行动序列等）。过滤责任在 agent 侧的 `spire-core.fairness`，**不在 mod**。
- 同一个 `seq` 只会出现一次；agent 可按 `seq` 单调递增检测丢帧。
- `seq` 在 mod 进程内全局自增，跨 run 不重置。
- `raw.screen_state` 里有两个"执行侧必需"的分组（schema 见
  [05-state-schema](05-state-schema.md#screen_state)）：
  - **选牌界面的来由**：`origin`（`rest_smith`/`event`/`transform`/`purge`/`upgrade`/`confirm`/`combat_select`/`scry`/`select`/`hand_select`）、
    界面提示语 `reason`、以及事件上下文 `event_name`/`event_text` —— 选牌是**有状态**的，
    没有这些就无法判断该选哪张、这次检索在干什么；
  - **奖励明细** `reward_details[]`：与 `options[]` 逐条对齐，卡牌奖励带上那三张牌的名字，
    免得模型"点进去看一眼再退出来"。

### `human_action`

仅 `mode=observe_human`（且已 `configure`）时上报。人类在游戏里完成一次操作后：

```jsonc
{
  "seq": 412,
  "kind": "play_card",
  "args": { "hand_index": 2, "target": "m0" },
  "screen": "NONE"
}
```

**只上报已提交的动作**，不上报中间点击（人类可以在确认前反复点选/取消牌，那些不构成决策）。

### `action`

```jsonc
{
  "kind": "play_card",
  "args": { "hand_index": 1, "target": "m0" }
}
```

### `action_result`

```jsonc
{ "ok": true, "error": null, "code": null }
{ "ok": false, "error": "card index 9 out of range", "code": "E_ILLEGAL_ACTION" }
```

## 语义动作白名单

**mod 只接受下表动作。** 没有 `quit`、`load`、`restart` 之类的动作，所以 **agent 在协议层就无法 SL**（对应 `allow_save_scum = false`）。所有索引均为 0-based。

| kind | args | 前置条件 | 说明 |
|---|---|---|---|
| `play_card` | `hand_index`, `target?` | 战斗内、该牌可出 | `target` 为怪物 id，如 `m0`；无需目标时省略 |
| `use_potion` | `potion_index`, `target?` | 该槽真有药水（空槽的 `can_use` 是 `false`） | `target` 为怪物 id 或空 |
| `discard_potion` | `potion_index` | 药水槽非空 | |
| `end_turn` | — | 战斗内且结束回合按钮可用 | |
| `select_choice` | `index` | 当前有选项 | 通用：事件选项、奖励、篝火、商店项、Neow 等 |
| `select_reward` | `index` | 战斗奖励界面**或**卡牌奖励界面 | **按界面分派**：`COMBAT_REWARD` 下 `index` 是奖励列表下标；`CARD_REWARD` 下 `index` 是三选一里第几张牌（`-1` = 跳过） |
| `proceed` | — | 右侧按钮存在 | 继续 / 确认 |
| `return` | — | 左侧按钮存在 | 返回 / 取消 / 跳过 |
| `select_cards` | `indices[]` | 选牌界面 | **一次性提交**多张（含空数组表示跳过）；解决"任意多选"与"必选 k 张"两种形态 |
| `select_card_reward` | `index` | 卡牌奖励界面 | `index == -1` 表示跳过 |
| `select_map_node` | `node` | 地图界面 | `node` 为节点 id，如 `n3_12`（格式 `n<x>_<y>`） |

约定：

- 索引一律 0-based，对应**同一次 `observation` 中 `raw` 数组的下标**。若 agent 基于过期的 `seq` 发动作，mod 直接拒绝（见下）。
- 敌人 id `m<i>` 的 `i` 是 `room.monsters.monsters` 里的**绝对下标**，不是"活着敌人中的序号"：
  **死亡不移位**（尸体继续占位）。两个敌人杀掉 `m0` 之后，活着的那个仍然是 `m1`。
  因此校验判据必须是"下标 `i` 上的敌人是否活着"，而**不是** `i < 活着的数量` ——
  后者会让第一次击杀之后所有带目标的动作被判越界，真机症状是"模型答了牌、agent 不执行"。
- `end_turn` **只写 `endTurnQueued`**，绝不同时写 `isEndingTurn`。这是游戏自己的分工：
  `AbstractPlayer.updateInput()` 要等到 `cardQueue` 清空、且 `actionManager` 交还控制权
  （`!hasControl`）之后，才把 `endTurnQueued` 换成 `isEndingTurn`；而 `AbstractRoom.update()`
  只要看到 `isEndingTurn` 为真，**下一帧**就把 `EndTurnAction + WaitAction + MonsterStartTurnAction`
  整排入队。绕过那道闸门 = 出牌还没结算完敌人回合就已经排进去（回合结束效果错序甚至丢失），
  而且 `endTurnQueued` 没走 `updateInput` 的分支、会一直留在 `true`，等控制权回来时再触发一次 ——
  真机表现就是"连续弹出两次敌人回合"。
- 每个动作携带 `seq`：agent 必须声明它是基于哪个观测做的决策。mod 校验 `seq` 是否等于最近一次发出的 `observation.seq`，不等则回 `E_STALE_SEQ`。**这防止 agent 用旧状态做出越权动作。**
- `select_cards` 的 `indices` 必须满足当前界面的约束（最少/最多张数），由 mod 校验。
- 同名动作在不同界面语义不同的只有 `select_reward` 一个（上表已注明）。**之所以共用一个名字**：core 的 `card_reward` 决策点用同一个候选前缀 `reward:<i>` 表示"拿第 i 项"，解析出来就是 `select_reward`。曾经这里被写成"只允许战斗奖励界面"，真机后果是**卡牌奖励永远被拒**（`E_SCREEN_MISMATCH`），游戏只能靠人类点或 30s 看门狗推过去。
- `select_choice(index)` 在事件 / Neow 界面上**不写** `pressed`，而是写 `hb.clicked`：`LargeDialogOptionButton.update()` 只有看到 `hb.clicked` 才会 `clicked=false; pressed=true`（反编译确认），写它才是游戏自己的"点到了"入口，且不会被残留成哑火。被禁用的选项（`isDisabled`）一律拒绝，不假装点得动。
- `select_cards` / `select_reward`(卡牌) 提交后由 `Actor.closeSelectScreen()` 关闭界面：
  它在 `closeCurrentScreen()` 之后，若 `screen` 确实回到了 `NONE` 就**强制
  `AbstractDungeon.isScreenUp = false`**。原因见
  [06-decision-points](06-decision-points.md#选牌界面的关闭语义closeselectscreen)：
  事件与篝火锻造都靠 `!isScreenUp && !selectedCards.isEmpty()` 轮询取结果，
  而 `closeCurrentScreen()` 在 `previousScreen != null` 时不会把 `isScreenUp`
  置假 —— 不补这一刀，就会"界面关了、事件没反应、下一帧又在同一界面重新问一遍"。
  判据取"关完之后 `screen` 是否真的 `NONE`"，所以不会覆盖游戏自己开出来的上一屏。

## 错误码

| code | 含义 | agent 应对 |
|---|---|---|
| `E_STALE_SEQ` | 动作基于过期观测 | 丢弃该决策，用新观测重新决策 |
| `E_ILLEGAL_ACTION` | 动作不在白名单或不满足前置条件 | 用次优候选重试一次；再失败走兜底 |
| `E_SCREEN_MISMATCH` | 当前界面与动作不匹配（例如在奖励界面发 `play_card`） | 重新识别决策点 |
| `E_INDEX_RANGE` | 索引越界 | 重新枚举候选 |
| `E_CARD_NOT_PLAYABLE` | 该牌当前不可出 | 重新枚举候选 |
| `E_NOT_CONFIGURED` | agent 还没发 `configure`，模组不知道自己是哪种模式 | 先发 `configure`（正常流程里握手后立刻发，收到这条说明 agent 有 bug） |
| `E_INTERNAL` | mod 内部异常 | 记日志，走兜底 |

（`proto/Errors.java` 里还声明了 `E_NOT_CONNECTED`，当前流程不会发出，属于保留项。）

## agent 侧配置文件（`SlayaTheSpire.properties`）

模组只读两个键，其余一概不看：

| 键 | 默认 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址；配合 agent 的 `[mod] host` |
| `port` | `17777` | 监听端口；配合 agent 的 `[mod] port` |

文件在 `%LOCALAPPDATA%\ModTheSpire\spireagent\SlayaTheSpire.properties`（Windows）。
**模组只读、从不写任何键**（MTS 的 `SpireConfig` 在首次加载时可能建一个空文件，
但里面不会有内容）；文件不存在也照样跑（全默认值）。

协议 v1 曾有 `observe_human` / `watchdog_sec` 两个键，v2 已废弃 ——
它们在功能上被 `configure` 完全取代。文件里留着不会被读；`python -m spire_agent doctor`
和 `mod-config` 会点出来。`mod-config --set` 只接受 `host` / `port`，写别的直接拒绝。

## 稳定性判定

mod 只在**稳定态**发 `observation`。稳定态的定义：

1. `AbstractDungeon.actionManager` 的动作队列为空且不在处理阶段；
2. 当前 `AbstractDungeon.screen` 处于**等待输入**的状态（`NONE` 战斗内可操作、`CARD_REWARD`、`MAP`、`EVENT`、`SHOP_ROOM` 等）；
3. 不存在正在播放的、会改变状态的非 idle 界面（如牌飞行动画、遗物弹出动画）；
4. **例外：`GRID` 选牌界面（含 `HAND_SELECT`）开着时直接判为稳定**，哪怕动作队列非空。

第 4 条是本项目踩过的最深的坑，改动前请先读完这段。**选牌界面会自己把
`isScreenUp` 置真**（`GridCardSelectScreen.callOnOpen` / `HandCardSelectScreen.prep`
都写 `AbstractDungeon.screen = GRID`、`isScreenUp = true`），而玩法循环是

```java
// AbstractRoom.update()
if (!AbstractDungeon.isScreenUp) { actionManager.update(); player.updateInput(); }
```

于是**驱动这个界面的动作会一直停在队列里**：铁甲战士的「头槌」
（`DiscardPileToTopOfDeckAction`）与观者的「预见」（`ScryAction`）都是
`actionType = CARD_MANIPULATION` 的动作，它们在 `update()` 里 `open()` 出选牌界面
后就把 `isDone` 留成 false，等着玩家挑牌 —— 只要界面开着，`actionManager.update()`
就不再被调用，`phase` 永远是 `EXECUTING_ACTIONS`、`currentAction` 永远非空、
`actions/cardQueue/monsterQueue` 都不空。

只按 1–3 条判定的后果是：这类界面**一个 `observation` 都发不出去**，而动作执行
成功时 `watchdog.disarm()` 已经把看门狗关了 —— 真机症状就是"打完头槌界面卡死、
模组静默"。所以稳定性判定必须把"选牌界面"当作**等待人类输入**的一种，而不是
"队列没空"。

实现方式：在主更新循环末尾采样上述条件，并加一个**去抖窗口**（连续 N 帧满足才发），避免动画最后一帧的抖动造成重复观测。

**不做**「每次状态变化都发」——agent 只关心「轮到它做决定」的时刻。

## 看门狗

mod 侧的看门狗是**游戏不卡死的唯一保证**。

- 每当 mod 发出 `observation` 后开始计时，等待 agent 的 `action`。
- 超时（`watchdog_sec`，由 `configure` 推送，默认 30s）仍未收到合法动作，mod 执行**安全默认动作**：
  - **选牌界面开着**（`GRID` / `HAND_SELECT`）：只有在"可以一张都不选"的界面上才有安全默认动作 —— 什么都不选直接确认（`select_cards(indices=[])`，预见 = 什么都不丢）。必选 k 张的界面（头槌 / 锻造 / 删牌 / 澄明）替人类挑一张是有后果的决策，**宁可不做**，只记录。注意这一条必须排在"战斗内"前面：选牌界面开着时 `end_turn` 会带着界面把一步走掉。
  - 战斗内（没有选牌界面）：`end_turn`
  - 有选项界面：`select_choice(0)`
  - 有继续/确认按钮：`proceed`
  - 其他：不做任何事，仅记录
- 每次触发写一条 `[watchdog]` 到游戏日志（ModTheSpire 控制台窗口可见）。
- 看门狗触发的动作**不产生采集行**（没有模型判断），但会在 run 元数据里累加 `watchdog_events`。
- `mode=observe_human` 时**不 arm 看门狗**：人类在思考，不该被默认动作打断。
- 未 `configure` 时也不 arm（模组此时是哑的）。

agent 侧：`ping` 每 5s 一次；连续 3 次无 `pong` 判定连接失效并重连。

## 版本协商

- 协议版本固定为 `2`。协议变更加版本号。
- `hello.protocol` 不等于 `2`：agent 抛 `IncompatibleMod`，**不参与指数退避重连**（旧 jar 重试一万次也一样），
  `run` 记 `result=mod_incompatible` 并以退出码 `5` 结束，提示"重新构建并安装模组后重启游戏"。
- `configure` 被拒或不回执：同样按 `IncompatibleMod` 处理。

## 人类动作捕获

`mode=observe_human` 时上报（`act/HumanActionTap.java` 采集 + `act/HumanActionPatches.java` 挂钩 + `SpireAgentMod` 发消息）：

- hook **游戏自身的动作提交点**，而不是模拟层。当前覆盖：
  - 出牌：`AbstractPlayer.playCard` 前缀 —— 此刻 `hoveredCard` / `hoveredMonster` 都还在，是唯一能同时读到"哪张牌 + 打谁"的时机；
  - 用药水：BaseMod 的 `PostPotionUseSubscriber`；
  - 地图选节点：`MapRoomNode.update`；
  - 事件选项：`GenericEventDialog.update` / `RoomEventDialog.update`；
  - 卡牌奖励的拿牌与跳过：`CardRewardScreen.acquireCard` / `skippedCards`；
  - 商店买卡 / 买遗物 / 买药水 / 删牌：`ShopScreen.purchaseCard` / `StoreRelic.purchaseRelic` / `StorePotion.purchasePotion` / `ShopScreen.purgeCard`；
  - 篝火休息 / 锻造：`RestOption.useOption` / `SmithOption.useOption`；
  - 选牌界面的确认（`GRID`）：`AbstractDungeon.closeCurrentScreen` 前缀 —— 单张必选（"点牌 -> 确认"）与任意多选（"点若干张 -> 确认"）两条路都汇到这里；该前缀里 `selectedCards` 还没被清空，所以读到的就是人类刚选的那组牌，按**提交顺序**上报成 `select_cards{indices=[[zone,index],…]}`。
- 只上报"已提交"：出牌需等到 `playCard` 真正被调用；人类在确认前反复点选不会产生记录。
- 上报的 `kind`/`args` 与 `action` 消息**完全同构**，这样数据集里 `human_action` 才能直接和 agent 的候选集做匹配（见 [08-dataset](08-dataset.md#候选命中匹配)）。
- 游戏内语言不影响语义捕获（我们读的是对象与索引，不是文本）。
- **加钩子时的坑**：MTS 的 legacy `@SpirePatch`（非 `@SpirePatch2`）**按位置**把 patch 形参
  喂给目标方法的 `$0, $1, ...`（`$0` 对实例方法就是 `this`），形参名字完全不参与匹配。
  所以带参目标方法必须从 `this` 同类型占位参数开始逐个写全前缀（例如
  `ShopScreen.purchaseCard(AbstractCard)` 要写成 `Prefix(ShopScreen __instance, AbstractCard card)`），
  写错会在加载模组时抛 `CannotCompileException: Prefix(...) not found` 而让**游戏起不来**。
  `SelfTest.checkPatches` 会在 `tools\build_mod.ps1` 阶段拦住这类错误（见 [11-testing](11-testing.md#l2mod-单测清单)）。
- **已知缺口**：宝箱房、Boss 遗物三选一、Neow 起始奖励、商店"离开"、战斗奖励界面的"继续"，以及 `HandCardSelectScreen` 的确认 —— 这些界面的人类操作目前**不上报**，会记成 `matched=false`（这正是候选枚举器与捕获面的改进信号，不要丢）。`HandCardSelectScreen` 覆盖不了的原因很具体：它选一张牌就从 `hand` 里摘一张，等界面关闭时已经回推不出"当时的下标"，只能等后续版本改成在 `update()` 里跟踪。

## 相关文档

- 观测 schema -> [05-state-schema](05-state-schema.md)
- 动作如何从候选产生 -> [06-decision-points](06-decision-points.md)

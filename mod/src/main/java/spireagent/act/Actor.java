package spireagent.act;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.cards.CardGroup;
import com.megacrit.cardcrawl.cards.CardQueueItem;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.core.AbstractCreature;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.map.MapEdge;
import com.megacrit.cardcrawl.map.MapRoomNode;
import com.megacrit.cardcrawl.monsters.AbstractMonster;
import com.megacrit.cardcrawl.potions.AbstractPotion;
import com.megacrit.cardcrawl.relics.AbstractRelic;
import com.megacrit.cardcrawl.rewards.RewardItem;
import com.megacrit.cardcrawl.rooms.RestRoom;
import com.megacrit.cardcrawl.rooms.AbstractRoom;
import com.megacrit.cardcrawl.rooms.CampfireUI;
import com.megacrit.cardcrawl.screens.CardRewardScreen;
import com.megacrit.cardcrawl.screens.CombatRewardScreen;
import com.megacrit.cardcrawl.screens.select.GridCardSelectScreen;
import com.megacrit.cardcrawl.screens.select.HandCardSelectScreen;
import com.megacrit.cardcrawl.shop.ShopScreen;
import com.megacrit.cardcrawl.shop.StorePotion;
import com.megacrit.cardcrawl.shop.StoreRelic;
import com.megacrit.cardcrawl.ui.buttons.LargeDialogOptionButton;
import com.megacrit.cardcrawl.ui.campfire.AbstractCampfireOption;
import com.megacrit.cardcrawl.vfx.FastCardObtainEffect;

import spireagent.Log;
import spireagent.Reflect;
import spireagent.bridge.Json;
import spireagent.obs.CampfireSlots;
import spireagent.obs.Observer;
import spireagent.obs.ShopSlots;
import spireagent.proto.ActionSpec;
import spireagent.proto.Errors;

/**
 * 语义动作执行（见 docs/03-mod-protocol.md#语义动作白名单）。
 *
 * 三条纪律：
 *   1. **不模拟鼠标**：一律走游戏自己的 `GameAction` / 动作队列 / 界面对象，
 *      因为坐标会随分辨率、语言、遗物数量漂移；
 *   2. **不猜**：任何取不到的对象都返回失败而不是抛异常，agent 侧据此重试或兜底；
 *   3. **同源**：所有"下标"都复用 `Observer` / `ShopSlots` / `CampfireSlots`
 *      的定义，绝不在这里重新数一遍 —— 否则模型选的和实际发生会错位。
 *
 * 有些动作（确认按钮、点地图节点、点继续）游戏要等下一帧 `update()` 才真正生效，
 * 所以 `SpireAgentMod` 用 `EchoGate` 挡住这中间的空档（见 obs/EchoGate.java）。
 */
public final class Actor {

    private Actor() {
    }

    /** 执行一个已通过 `ActionSpec.validate` 的动作。 */
    public static Map<String, Object> execute(String kind, Map<String, Object> args) {
        Map<String, Object> a = args == null
                ? new LinkedHashMap<String, Object>() : args;
        try {
            if (ActionSpec.PLAY_CARD.equals(kind)) {
                return playCard(a);
            }
            if (ActionSpec.USE_POTION.equals(kind)) {
                return usePotion(a);
            }
            if (ActionSpec.DISCARD_POTION.equals(kind)) {
                return discardPotion(a);
            }
            if (ActionSpec.END_TURN.equals(kind)) {
                return endTurn();
            }
            if (ActionSpec.SELECT_CHOICE.equals(kind)) {
                return selectChoice(a);
            }
            if (ActionSpec.SELECT_REWARD.equals(kind)) {
                return selectReward(Json.asInt(a.get("index"), -1));
            }
            if (ActionSpec.PROCEED.equals(kind)) {
                return clickProceed();
            }
            if (ActionSpec.RETURN.equals(kind)) {
                return clickReturn();
            }
            if (ActionSpec.SELECT_CARDS.equals(kind)) {
                return selectCards(a);
            }
            if (ActionSpec.SELECT_CARD_REWARD.equals(kind)) {
                return cardReward(Json.asInt(a.get("index"), -1));
            }
            if (ActionSpec.SELECT_MAP_NODE.equals(kind)) {
                return selectMapNode(Json.asString(a.get("node"), null));
            }
            return Errors.fail(Errors.ILLEGAL_ACTION, "unhandled action kind: " + kind);
        } catch (Throwable t) {
            Log.error("action " + kind + " threw", t);
            return Errors.fail(Errors.INTERNAL, String.valueOf(t));
        }
    }

    // ------------------------------------------------------------- 战斗

    private static Map<String, Object> playCard(Map<String, Object> args) {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null || p.hand == null || p.hand.group == null) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "not in combat");
        }
        int handIndex = Json.asInt(args.get("hand_index"), -1);
        if (handIndex < 0 || handIndex >= p.hand.group.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "hand_index " + handIndex + " out of range");
        }
        AbstractCard card = p.hand.group.get(handIndex);
        AbstractMonster target = null;
        String targetId = Json.asString(args.get("target"), null);
        if (targetId != null) {
            Integer mi = ActionSpec.monsterIndex(targetId);
            target = mi == null ? null : Observer.monsterAt(mi.intValue());
            if (target == null) {
                return Errors.fail(Errors.INDEX_RANGE, "bad target: " + targetId);
            }
        }
        // 游戏自己的出牌入口：CardQueueItem 会把目标一并带进牌的效果结算，
        // 因此不需要"先点牌再点敌人"那套鼠标流程。
        AbstractDungeon.actionManager.addCardQueueItem(new CardQueueItem(card, target), true);
        return Errors.ok();
    }

    private static Map<String, Object> endTurn() {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "not in combat");
        }
        // 只置 endTurnQueued —— 这是"结束回合"按钮按下去之后游戏自己做的唯一一件事
        // （EndTurnButton.disable(true) 也只做这一件，外加音效和按钮文案）。
        //
        // 真正的闸门在 AbstractPlayer.updateInput()：它要等到 cardQueue 清空、且 actionManager
        // 交还控制权（!hasControl）之后，才把 endTurnQueued 换成 isEndingTurn。而 AbstractRoom.update()
        // 只要看到 isEndingTurn 为真，下一帧就把整套 EndTurnAction + WaitAction +
        // MonsterStartTurnAction 排进队列（AbstractRoom$1）。
        //
        // 顺手把 isEndingTurn 也置真就等于绕过那道闸门：出牌还没结算完，敌人回合就已经排进去了
        // （回合结束效果错序甚至丢失）；而且 endTurnQueued 没走 updateInput 的分支、会一直留在 true，
        // 等控制权回来时再触发一次 —— 表现就是"连续弹出两次敌人回合"。
        p.endTurnQueued = true;
        return Errors.ok();
    }

    private static Map<String, Object> usePotion(Map<String, Object> args) {
        int slot = Json.asInt(args.get("potion_index"), -1);
        AbstractPotion pot = potion(slot);
        if (pot == null) {
            return Errors.fail(Errors.INDEX_RANGE, "empty potion slot " + slot);
        }
        AbstractCreature target = pot.targetRequired ? monsterTarget(args) : null;
        if (pot.targetRequired && target == null) {
            return Errors.fail(Errors.INDEX_RANGE, "bad potion target");
        }
        pot.use(target);
        AbstractDungeon.topPanel.destroyPotion(slot);
        return Errors.ok();
    }

    private static Map<String, Object> discardPotion(Map<String, Object> args) {
        int slot = Json.asInt(args.get("potion_index"), -1);
        if (potion(slot) == null) {
            return Errors.fail(Errors.INDEX_RANGE, "empty potion slot " + slot);
        }
        AbstractDungeon.topPanel.destroyPotion(slot);
        return Errors.ok();
    }

    private static AbstractPotion potion(int slot) {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null || p.potions == null || slot < 0 || slot >= p.potions.size()) {
            return null;
        }
        AbstractPotion pot = p.potions.get(slot);
        return pot == null || pot.ID == null ? null : pot;
    }

    private static AbstractMonster monsterTarget(Map<String, Object> args) {
        Integer mi = ActionSpec.monsterIndex(Json.asString(args.get("target"), null));
        if (mi == null) {
            return null;
        }
        return Observer.monsterAt(mi.intValue());
    }

    // ------------------------------------------------------------- 通用选项

    private static Map<String, Object> selectChoice(Map<String, Object> args) {
        if (args.get("monster") != null) {
            return selectTarget(Json.asInt(args.get("monster"), -1));
        }
        int index = Json.asInt(args.get("index"), Integer.MIN_VALUE);
        String screen = Observer.screenName();

        if (Observer.SCREEN_SHOP.equals(screen)) {
            return shopChoice(index);
        }
        if (Observer.SCREEN_REST.equals(screen)) {
            return restChoice(index);
        }
        if (Observer.SCREEN_COMBAT_REWARD.equals(screen)) {
            List<RewardItem> rewards = Observer.liveRewards();
            if (index == rewards.size()) {
                return clickProceed();
            }
            return claimReward(index);
        }
        if (Observer.SCREEN_BOSS_RELIC.equals(screen)) {
            return bossRelic(index);
        }
        if (Observer.SCREEN_CARD_REWARD.equals(screen)) {
            // 兼容：core 在卡牌奖励界面用的是 `select_reward`，但协议里两者语义
            // 相同，这里放宽，避免因为动作名不同而把合法决策判成非法。
            return cardReward(index);
        }
        // 事件 / Neow：点按钮的真实路径是把它标成 pressed，游戏自己的 update 会
        // 读它并调 buttonEffect（比直接调 effect 更完整：有音效、有按钮动画）。
        //
        // 这里走 `hb.clicked` 而不是直接写 `pressed`：`LargeDialogOptionButton.update()`
        // 只有看到 `hb.clicked` 才会 `clicked=false; pressed=true`（反编译确认），
        // 也就是说 `hb.clicked` 才是游戏自己的"点到了"入口，写它不会残留状态。
        // `pressed` 也可以直接写（对话框的 update 会读），但它不被消费方复位，
        // 一旦对话框这一帧不更新就会留下一颗哑火 —— 用游戏路径更稳。
        List<LargeDialogOptionButton> buttons = Observer.optionButtons();
        if (index < 0 || index >= buttons.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "option index " + index + " out of range [0," + buttons.size() + ")");
        }
        LargeDialogOptionButton button = buttons.get(index);
        if (button.isDisabled) {
            return Errors.fail(Errors.ILLEGAL_ACTION,
                    "option " + index + " is disabled: " + button.msg);
        }
        // 两条都给：`hb.clicked` 是游戏自己的"点到了"入口（`hoverAndClickLogic()` 看到它
        // 就 `clicked=false; pressed=true`），`pressed` 是对话框真正读的那个标志。
        // 只写 `hb.clicked` 依赖按钮这一帧的 `update()` 真的跑到（它由对话框驱动，
        // 一般都会跑）；把 `pressed` 一起写，就算那一帧按钮没更新，对话框自己的
        // `update()` 也会消费它。两条不会重复触发：消费方把 `pressed` 复位。
        button.hb.clicked = true;
        button.pressed = true;
        return Errors.ok();
    }

    /**
     * 独立的"选目标"界面：v1 里出牌/用药水都在动作里直接带目标，所以这个分支
     * 只在游戏自己进入单目标模式（手柄/键盘瞄准）时才会用到。这时把"正被瞄准的
     * 那张牌"按同一个 `CardQueueItem` 入口打出，避免模拟鼠标。
     */
    private static Map<String, Object> selectTarget(int monsterIndex) {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null || !p.inSingleTargetMode || p.hoveredCard == null) {
            return Errors.fail(Errors.SCREEN_MISMATCH,
                    "no card is being aimed; put the target in play_card/use_potion instead");
        }
        AbstractMonster chosen = Observer.monsterAt(monsterIndex);
        if (chosen == null) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "monster m" + monsterIndex + " is not a live enemy");
        }
        AbstractCard card = p.hoveredCard;
        if (p.hand == null || p.hand.group == null || !p.hand.group.contains(card)) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "the aimed card is no longer in hand");
        }
        p.inSingleTargetMode = false;
        Reflect.set(p, AbstractPlayer.class, "hoveredMonster", null);
        p.hoveredCard = null;
        AbstractDungeon.actionManager.addCardQueueItem(new CardQueueItem(card, chosen), true);
        return Errors.ok();
    }

    /** 商店：index == -1 表示离开（= 点"离开商店"按钮），否则按槽位下标购买。 */
    private static Map<String, Object> shopChoice(int index) {
        if (index == -1) {
            return clickProceed();
        }
        List<ShopSlots.Slot> slots = ShopSlots.list();
        if (index < 0 || index >= slots.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "shop index " + index + " out of range [0," + slots.size() + ")");
        }
        ShopSlots.Slot slot = slots.get(index);
        if (ShopSlots.KIND_PURGE.equals(slot.kind)) {
            return buyPurge();
        }
        if (slot.payload instanceof StoreRelic) {
            ((StoreRelic) slot.payload).purchaseRelic();
            return Errors.ok();
        }
        if (slot.payload instanceof StorePotion) {
            ((StorePotion) slot.payload).purchasePotion();
            return Errors.ok();
        }
        if (slot.payload instanceof AbstractCard) {
            Object ok = Reflect.call(AbstractDungeon.shopScreen, ShopScreen.class,
                    "purchaseCard", new Class<?>[] {AbstractCard.class},
                    new Object[] {slot.payload});
            return ok == null
                    ? Errors.fail(Errors.INTERNAL, "purchaseCard failed")
                    : Errors.ok();
        }
        return Errors.fail(Errors.ILLEGAL_ACTION, "unknown shop slot: " + slot.kind);
    }

    /** 篝火：调按钮自己的 `useOption()`（与玩家点击等价的唯一入口）。 */
    /**
     * 商店删牌：入口**不是** `ShopScreen.purgeCard()`。
     *
     * 那是个 static 的"结账"动作：扣金币、把 `purgeCost` 再抬 25、播音效 ——
     * 它**不打开选牌界面**。真正的"开始删牌"入口是 private 的 `purchasePurge()`：
     *
     *     if (player.gold >= actualPurgeCost) {
     *         previousScreen = SHOP;                      // 选完要回到商店
     *         gridSelectScreen.open(getGroupWithoutBottledCards(
     *                 masterDeck.getPurgeableCards()), 1, NAMES[13],
     *                 false, false, forPurge=true, true);
     *     } else { playCantBuySfx(); }
     *
     * 结账由游戏自己完成：`ShopRoom.updatePurge()` 每帧检查
     * `!gridSelectScreen.selectedCards.isEmpty()`，非空就调 `purgeCard()` 扣钱、
     * 遍历 `selectedCards` 往 `masterDeck.removeCard` + 播 `PurgeCardEffect`、
     * 最后 `selectedCards.clear()` 并把 `purgeAvailable` 置假。
     *
     * 早先这里直接调 `purgeCard()`，后果是**钱扣了、牌没删、`purgeAvailable` 还是 true**：
     * agent 看到"删牌"这个选项还在货架上，就会反复买同一个空操作
     * （真机症状：金币莫名其妙变少，牌组一张没少）。
     */
    private static Map<String, Object> buyPurge() {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null || p.masterDeck == null) {
            return Errors.fail(Errors.INTERNAL, "no player deck");
        }
        if (p.gold < ShopScreen.actualPurgeCost) {
            return Errors.fail(Errors.ILLEGAL_ACTION,
                    "not enough gold to purge (" + p.gold + " < "
                            + ShopScreen.actualPurgeCost + ")");
        }
        CardGroup purgeable =
                CardGroup.getGroupWithoutBottledCards(p.masterDeck.getPurgeableCards());
        if (purgeable == null || purgeable.size() == 0) {
            return Errors.fail(Errors.ILLEGAL_ACTION, "no purgeable cards in the deck");
        }
        AbstractDungeon.previousScreen = AbstractDungeon.CurrentScreen.SHOP;
        AbstractDungeon.gridSelectScreen.open(purgeable, 1, ShopScreen.NAMES[13],
                false, false, true, true);
        return Errors.ok();
    }

    private static Map<String, Object> restChoice(int index) {
        List<CampfireSlots.Slot> slots = CampfireSlots.list();
        if (index < 0 || index >= slots.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "rest index " + index + " out of range [0," + slots.size() + ")");
        }
        Object payload = slots.get(index).payload;
        if (!(payload instanceof AbstractCampfireOption)) {
            return Errors.fail(Errors.INTERNAL, "campfire slot is not usable");
        }
        AbstractCampfireOption option = (AbstractCampfireOption) payload;
        // 光调 `useOption()` 是不够的：游戏自己的鼠标路径在
        // `AbstractCampfireOption.update()` 里做**两件事**：
        //
        //     if (this.hb.clicked || (controllerSelect && hovered)) {
        //         this.hb.clicked = false;
        //         if (!Settings.isTouchScreen) {
        //             this.useOption();
        //             ((RestRoom)getCurrRoom()).campfireUI.somethingSelected = true;
        //         }
        //     }
        //
        // `somethingSelected` 才是"这个界面已经选过了"的旗标：`CampfireUI.update()`
        // 只有看到它才会去减 `hideStuffTimer` 并把 `hidden` 置真，之后按钮才不再更新。
        // 早先只调 `useOption()`，于是界面永远不隐藏、按钮永远可点 —— agent 拿到
        // 的还是 REST 界面，就会**反复选同一个选项**（真机症状：同一个休息连点好几次）。
        // 这里照抄非触屏分支的两步，且不复位 `used`，语义与鼠标点击完全一致。
        option.useOption();
        AbstractRoom room = AbstractDungeon.getCurrRoom();
        if (room instanceof RestRoom) {
            CampfireUI ui = ((RestRoom) room).campfireUI;
            if (ui != null) {
                ui.somethingSelected = true;
            }
        }
        return Errors.ok();
    }

    /** Boss 遗物：`isObtained` 是屏幕自己轮询的标志位（见 BossRelicSelectScreen.update）。 */
    private static Map<String, Object> bossRelic(int index) {
        List<AbstractRelic> relics = Observer.relicChoices();
        if (index == -1) {
            AbstractDungeon.bossRelicScreen.noPick();
            return Errors.ok();
        }
        if (index < 0 || index >= relics.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "relic index " + index + " out of range [0," + relics.size() + ")");
        }
        AbstractRelic relic = relics.get(index);
        relic.hb.clicked = true;
        relic.isObtained = true;
        return Errors.ok();
    }

    // ------------------------------------------------------------- 战斗奖励

    /**
     * `select_reward` 的两种形态，按当前界面分派：
     *
     * - 战斗奖励界面（COMBAT_REWARD）：`index` 是活奖励列表下标 -> {@link #claimReward}；
     * - 卡牌奖励界面（CARD_REWARD）：`index` 是三选一里第几张牌 -> {@link #cardReward}。
     *
     * core 的 `card_reward` 决策点用 `reward:<i>` 表示"拿第 i 张"，解析出来就是
     * `select_reward`。原先这里无条件走 `claimReward`，而 `claimReward` 读的是
     * **战斗奖励列表** —— 在卡牌奖励界面上它拿到的是 `room.rewards`（不是那三张牌），
     * 于是每次都 `claimReward returned false`。真机日志：
     * `WARN action select_reward rejected by executor: claimReward returned false`。
     */
    private static Map<String, Object> selectReward(int index) {
        if (Observer.SCREEN_CARD_REWARD.equals(Observer.screenName())) {
            return cardReward(index);
        }
        return claimReward(index);
    }

    /**
     * 战斗奖励：领取第 index 条。
     *
     * **只置 `isDone = true`，剩下的全交给游戏自己**。`CombatRewardScreen.update()`
     * 每帧会跑 `rewardViewUpdate()`：
     *
     *     item.update();
     *     if (item.isDone) {
     *         if (item.claimReward()) { it.remove(); changed = true; }
     *         else if (item.type == POTION) { item.isDone = false; flashRed(); tip(...); }
     *         else { item.isDone = false; }        // CARD：开卡牌界面，条目留在列表里
     *     }
     *
     * 也就是说 `claimReward()` 的返回值**不是成功/失败**：卡牌奖励故意返回 false
     * （它只是打开三选一界面，条目要等 `CardRewardScreen.takeReward()` 才移走）。
     * 早先这里自己调 `claimReward()` 并把 false 当失败，于是"拿卡牌奖励"永远报错，
     * 条目也从界面上删不掉 —— 真机日志里同一条奖励被反复列出来、反复领。
     */
    private static Map<String, Object> claimReward(int index) {
        List<RewardItem> rewards = Observer.liveRewards();
        if (index < 0 || index >= rewards.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "reward index " + index + " out of range [0," + rewards.size() + ")");
        }
        RewardItem item = rewards.get(index);
        item.isDone = true;
        return Errors.ok();
    }

    /**
     * 卡牌奖励（`CARD_REWARD` 界面）：index == -1 跳过，否则拿第 index 张。
     *
     * **不能靠模拟点击**：`CardRewardScreen.cardSelectUpdate()` 只认"当前 hovered 的
     * 那张牌"上的 `hb.clicked`，而 `hb.hovered` 是每帧按鼠标位置算出来的 ——
     * 我们写不进那个位置（真机验证过：`card.hb.clicked = true` 完全无效，
     * 一屏刷了 40 多条 `acquireCard unavailable` 然后原地空转）。
     *
     * 也不走反射调 `CardRewardScreen.acquireCard`（实测 `getDeclaredMethod` 拿不到，
     * 静默返回 null）。改成**照抄游戏那两个私有方法的公开等价物**：
     *   - `acquireCard(card)` = `effectsQueue.add(new FastCardObtainEffect(card, x, y))`
     *     （`FastCardObtainEffect` 是 public，这才是"牌真的进牌组"的那一步）；
     *   - `takeReward()` = 从**界面自己的** rewards 里 remove 这条 + `positionRewards()`，
     *     空了就 `hasTakenAll = true` + 亮出 Proceed（全是 public 字段 / public 方法）；
     *   - 然后 `closeCurrentScreen()`：`CARD_REWARD` 的 previousScreen 是
     *     `COMBAT_REWARD`（`RewardItem.claimReward()` 里设的），会自动退回去。
     */
    private static Map<String, Object> cardReward(int index) {
        CardRewardScreen crs = AbstractDungeon.cardRewardScreen;
        if (crs == null) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "no card reward screen");
        }
        if (index == -1) {
            crs.skippedCards();
            takeCardReward(crs);
            AbstractDungeon.closeCurrentScreen();
            return Errors.ok();
        }
        List<AbstractCard> cards = Observer.cardRewardCards();
        if (index < 0 || index >= cards.size()) {
            return Errors.fail(Errors.INDEX_RANGE,
                    "card reward index " + index + " out of range [0," + cards.size() + ")");
        }
        AbstractCard card = cards.get(index);
        // 这一步是"牌真的进牌组"：游戏自己的 acquireCard 也只是这一句。
        AbstractDungeon.effectsQueue.add(
                new FastCardObtainEffect(card, card.current_x, card.current_y));
        takeCardReward(crs);
        AbstractDungeon.closeCurrentScreen();
        return Errors.ok();
    }

    /** `CardRewardScreen.takeReward()` 的等价物（那个方法自己只有这几行）。 */
    private static void takeCardReward(CardRewardScreen crs) {
        RewardItem item = crs.rItem;
        CombatRewardScreen screen = AbstractDungeon.combatRewardScreen;
        if (item == null || screen == null || screen.rewards == null) {
            return;
        }
        screen.rewards.remove(item);
        screen.positionRewards();
        if (screen.rewards.isEmpty()) {
            screen.hasTakenAll = true;
            if (AbstractDungeon.overlayMenu != null) {
                AbstractDungeon.overlayMenu.proceedButton.show();
            }
        }
    }

    // ------------------------------------------------------------- 继续 / 返回

    private static Map<String, Object> clickProceed() {
        if (proceedHidden()) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "no proceed button");
        }
        Object hb = Reflect.get(AbstractDungeon.overlayMenu.proceedButton,
                ProceedButtonClass(), "hb");
        if (hb instanceof com.megacrit.cardcrawl.helpers.Hitbox) {
            ((com.megacrit.cardcrawl.helpers.Hitbox) hb).clicked = true;
            return Errors.ok();
        }
        AbstractDungeon.overlayMenu.proceedButton.hide();
        return Errors.fail(Errors.INTERNAL, "proceed button has no hitbox");
    }

    private static Map<String, Object> clickReturn() {
        try {
            if (AbstractDungeon.overlayMenu == null
                    || AbstractDungeon.overlayMenu.cancelButton == null) {
                return Errors.fail(Errors.SCREEN_MISMATCH, "no return button");
            }
            AbstractDungeon.overlayMenu.cancelButton.hb.clicked = true;
            return Errors.ok();
        } catch (RuntimeException e) {
            return Errors.fail(Errors.INTERNAL, String.valueOf(e));
        }
    }

    private static Class<?> ProceedButtonClass() {
        return AbstractDungeon.overlayMenu.proceedButton.getClass();
    }

    /** progress 按钮是否可见（`isHidden` 是私有字段，只能反射读）。 */
    public static boolean proceedHidden() {
        try {
            if (AbstractDungeon.overlayMenu == null
                    || AbstractDungeon.overlayMenu.proceedButton == null) {
                return true;
            }
            Object hidden = Reflect.get(AbstractDungeon.overlayMenu.proceedButton,
                    ProceedButtonClass(), "isHidden");
            return !(hidden instanceof Boolean) || ((Boolean) hidden).booleanValue();
        } catch (RuntimeException e) {
            return true;
        }
    }

    // ------------------------------------------------------------- 选牌

    /**
     * 选牌界面的可选池（**顺序即观测里的下标**）。
     *
     * 刻意保留 null 占位：观测侧的下标是"在 group 里的位置"，执行侧必须用同一
     * 个位置回查，否则 null 占位之后的牌会整体错位。
     */
    public static List<AbstractCard> selectionPool() {
        List<AbstractCard> out = new ArrayList<AbstractCard>();
        try {
            if (AbstractDungeon.screen == AbstractDungeon.CurrentScreen.HAND_SELECT) {
                HandCardSelectScreen hs = AbstractDungeon.handCardSelectScreen;
                if (hs == null) {
                    return out;
                }
                CardGroup hand = (CardGroup) Reflect.get(hs,
                        HandCardSelectScreen.class, "hand");
                addGroup(hand, out);
                return out;
            }
            GridCardSelectScreen gs = AbstractDungeon.gridSelectScreen;
            addGroup(gs == null ? null : gs.targetGroup, out);
        } catch (RuntimeException e) {
            Log.warn("selectionPool failed: " + e);
        }
        return out;
    }

    private static void addGroup(CardGroup group, List<AbstractCard> out) {
        if (group == null || group.group == null) {
            return;
        }
        for (AbstractCard c : group.group) {
            out.add(c);
        }
    }

    private static Map<String, Object> selectCards(Map<String, Object> args) {
        Object raw = args.get("indices");
        if (!(raw instanceof List)) {
            return Errors.fail(Errors.ILLEGAL_ACTION, "select_cards needs args.indices");
        }
        List<AbstractCard> pool = selectionPool();
        if (pool.isEmpty()) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "no card-selection screen");
        }
        List<AbstractCard> picks = new ArrayList<AbstractCard>();
        for (Object item : (List<?>) raw) {
            List<?> pair = Json.asList(item);
            if (pair.size() != 2) {
                return Errors.fail(Errors.ILLEGAL_ACTION,
                        "each entry of args.indices must be [zone, index]");
            }
            int idx = Json.asInt(pair.get(1), -1);
            if (idx < 0 || idx >= pool.size() || pool.get(idx) == null) {
                return Errors.fail(Errors.INDEX_RANGE, "no selectable card at index " + idx);
            }
            picks.add(pool.get(idx));
        }
        if (AbstractDungeon.screen == AbstractDungeon.CurrentScreen.HAND_SELECT) {
            return commitHandSelect(picks);
        }
        return commitGridSelect(picks);
    }

    private static Map<String, Object> commitHandSelect(List<AbstractCard> picks) {
        HandCardSelectScreen hs = AbstractDungeon.handCardSelectScreen;
        if (hs == null) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "no hand-select screen");
        }
        CardGroup hand = (CardGroup) Reflect.get(hs, HandCardSelectScreen.class, "hand");
        hs.selectedCards.group.clear();
        for (AbstractCard c : picks) {
            if (hand != null) {
                hand.removeCard(c);
            }
            c.setAngle(0f, false);
            hs.selectedCards.addToTop(c);
        }
        if (hand != null) {
            hand.refreshHandLayout();
        }
        hs.numSelected = hs.selectedCards.size();
        Reflect.call(hs, HandCardSelectScreen.class, "refreshSelectedCards",
                new Class<?>[0], new Object[0]);
        // 交回游戏自己的确认分支：它会处理 closeCurrentScreen、forTransform 的
        // 延迟变形、以及"可以一张都不选"的跳过路径。
        hs.button.hb.clicked = true;
        return Errors.ok();
    }

    private static Map<String, Object> commitGridSelect(List<AbstractCard> picks) {
        GridCardSelectScreen gs = AbstractDungeon.gridSelectScreen;
        if (gs == null) {
            return Errors.fail(Errors.SCREEN_MISMATCH, "no grid-select screen");
        }
        if (gs.isJustForConfirming) {
            // "给你看一组牌，确认就行"：游戏不读 selectedCards，自己遍历 targetGroup。
            gs.confirmButton.hb.clicked = true;
            return Errors.ok();
        }
        gs.selectedCards.clear();
        for (AbstractCard c : picks) {
            gs.selectedCards.add(c);
            c.beginGlowing();
            c.targetDrawScale = 0.75f;
            c.drawScale = 0.875f;
        }
        Reflect.set(gs, GridCardSelectScreen.class, "cardSelectAmount",
                Integer.valueOf(picks.size()));
        Reflect.set(gs, GridCardSelectScreen.class, "hoveredCard",
                picks.isEmpty() ? null : picks.get(picks.size() - 1));
        if (gs.anyNumber || gs.forClarity) {
            // 任意多选：游戏的确认分支只做 closeCurrentScreen。
            gs.confirmButton.hb.clicked = true;
            return Errors.ok();
        }
        // 必选 k 张：把选中的牌铺好、直接关界面。之后的收尾（锻造/删牌/拿牌）
        // 由游戏自己的轮询者读取 selectedCards 完成（CampfireSmithEffect、
        // ShopRoom.updatePurge、DeckToHandAction 等都是这个模式）。
        for (AbstractCard c : picks) {
            c.stopGlowing();
        }
        closeSelectScreen();
        return Errors.ok();
    }

    /**
     * 关掉选牌界面，并把"有界面打开"的旗标落回 false。
     *
     * 为什么不能只调 `closeCurrentScreen()`：事件（`UpgradeShrine` 等）与篝火锻造
     * （`CampfireSmithEffect`）都靠**轮询**拿结果：
     *
     *     if (!AbstractDungeon.isScreenUp && !gridSelectScreen.selectedCards.isEmpty()) { ... }
     *
     * 而 `closeCurrentScreen()` 只在 `previousScreen == null`（且玩家没死）时
     * 才经 `genericScreenOverlayReset()` 把 `isScreenUp` 置假；`previousScreen`
     * 被上一个界面（战斗奖励界面在 `rewardTime` 那一支里会把它设回
     * `COMBAT_REWARD`）留着的时候，`isScreenUp` 会一直是 true —— 玩家的选择
     * 就永远等不到那一刀，界面看着关了、事件却没反应。真机症状："选了不会进下一步"。
     *
     * 判据取"关完之后 `screen` 是不是真的回到了 NONE"：只有确实没有界面回来时
     * 才强制落假，绝不覆盖游戏自己开出来的上一屏（商店删牌会回到 SHOP）。
     */
    private static void closeSelectScreen() {
        AbstractDungeon.closeCurrentScreen();
        if (AbstractDungeon.screen == AbstractDungeon.CurrentScreen.NONE) {
            AbstractDungeon.isScreenUp = false;
        }
    }

    // ------------------------------------------------------------- 地图

    private static Map<String, Object> selectMapNode(String nodeId) {
        if (nodeId == null) {
            return Errors.fail(Errors.ILLEGAL_ACTION, "select_map_node needs args.node");
        }
        MapRoomNode target = findNode(nodeId);
        if (target == null) {
            return Errors.fail(Errors.INDEX_RANGE, "unknown map node " + nodeId);
        }
        if (!Observer.reachableNodeIds().contains(nodeId)) {
            return Errors.fail(Errors.ILLEGAL_ACTION, "node " + nodeId + " is not reachable");
        }
        MapRoomNode cur = AbstractDungeon.currMapNode;
        if (cur != null && cur.y >= 0) {
            MapEdge edge = cur.getEdgeConnectedTo(target);
            if (edge != null) {
                edge.markAsTaken();
            }
        }
        AbstractDungeon.nextRoom = target;
        // 让游戏自己的下一帧动画与房间切换完成剩下的链路（含 firstRoomChosen、
        // 路径绘制、存档）。直接搬这几步比调 nextRoomTransitionStart 更安全。
        Reflect.set(target, MapRoomNode.class, "animWaitTimer", Float.valueOf(0.01f));
        return Errors.ok();
    }

    private static MapRoomNode findNode(String nodeId) {
        if (nodeId == null || !nodeId.startsWith("n")) {
            return null;
        }
        int sep = nodeId.indexOf('_');
        if (sep < 0) {
            return null;
        }
        int x;
        int y;
        try {
            x = Integer.parseInt(nodeId.substring(1, sep));
            y = Integer.parseInt(nodeId.substring(sep + 1));
        } catch (NumberFormatException e) {
            return null;
        }
        ArrayList<ArrayList<MapRoomNode>> rows = AbstractDungeon.map;
        if (rows == null || y < 0 || y >= rows.size()) {
            return null;
        }
        List<MapRoomNode> row = rows.get(y);
        if (row == null || x < 0 || x >= row.size()) {
            return null;
        }
        return row.get(x);
    }

    // ------------------------------------------------------------- 未使用占位

    static {
        // 保留 RestRoom 引用以说明"篝火选项来自 RestRoom.campfireUI"（见 CampfireSlots）
        Class<?> c = RestRoom.class;
        if (c == null) {
            Log.warn("unreachable");
        }
    }
}

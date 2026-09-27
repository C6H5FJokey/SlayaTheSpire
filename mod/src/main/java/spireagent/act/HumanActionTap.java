package spireagent.act;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.map.MapRoomNode;
import com.megacrit.cardcrawl.monsters.AbstractMonster;
import com.megacrit.cardcrawl.potions.AbstractPotion;
import com.megacrit.cardcrawl.ui.buttons.LargeDialogOptionButton;

import spireagent.Reflect;
import spireagent.obs.CampfireSlots;
import spireagent.obs.Observer;
import spireagent.obs.ShopSlots;
import spireagent.proto.ActionSpec;

/**
 * 人类动作捕获（`observe_human` 模式，见 docs/03-mod-protocol.md#人类动作捕获）。
 *
 * 三条纪律：
 *   1. **只报已提交的动作**：出牌等 `receiveCardUsed`、事件选项等对话框真正读走
 *      `pressed`，而不是人类在确认前反复点选的那一刻；
 *   2. **语义而不是坐标**：产出的 `kind`/`args` 与 `action` 消息完全同构，这样
 *      数据集里的人类标签才能直接和 agent 的候选集做匹配；
 *   3. **不认识就不猜**：任何取不到的字段都留空（例如 `hand_index = -1`），
 *      由 agent 侧的 core 记成 `matched=false` 而不是编一个假下标。
 *
 * 这里不碰网络：捕获到的动作交给 `Sink`（`SpireAgentMod` 注册），
 * 因此本类可以在没有游戏的机器上单测（见 src/test/java/spireagent/SelfTest.java）。
 */
public final class HumanActionTap {

    /** 捕获结果的出口。实现在 `SpireAgentMod` 里负责补 `seq`/`screen` 并发出去。 */
    public interface Sink {
        void onHumanAction(Map<String, Object> action);
    }

    private static volatile Sink sink;

    private static final Object LOCK = new Object();
    private static Map<String, Object> pendingCard;
    private static String lastMapNode;
    private static int lastEventOption = -1;
    private static Object lastEventButton;
    /**
     * 卡牌奖励的去重闩锁。
     *
     * `CardRewardScreen.acquireCard` 是**每帧**被 `cardSelectUpdate()` 调的（鼠标停在
     * 那张牌上就一直调），前缀捕获因此会一屏刷几十条一模一样的 `select_reward`。
     * 人类只可能"拿一次"，所以同一张牌对象只报一次；跳过同理按屏只报一次。
     * 界面清空（`Observer.cardRewardCards()` 变空）时由 {@link #pollCardRewardLatch()}
     * 复位，下一次奖励才能重新上报。
     */
    private static Object lastCardRewardCard;
    private static boolean lastCardRewardSkip;
    /** 对话框 `update()` 前缀看到的"正在等输入"，用来分辨"刚好提交"与"一直显示着"。 */
    private static boolean dialogWaitingBefore;
    /** 地图 `update()` 前缀看到的 `DungeonMapScreen.clicked`。 */
    private static boolean mapClickBefore;

    private HumanActionTap() {
    }

    public static void setSink(Sink s) {
        sink = s;
    }

    // ------------------------------------------------------- 出牌

    /**
     * `AbstractPlayer.playCard()` 的前缀：此刻 `hoveredCard` / `hoveredMonster`
     * 还在，牌也还没离开手牌，是唯一能同时读到"哪张牌 + 打谁"的时机。
     */
    public static void noteCardAim(AbstractPlayer player) {
        AbstractCard card = player == null ? null : player.hoveredCard;
        if (card == null) {
            return;
        }
        Object aim = Reflect.get(player, AbstractPlayer.class, "hoveredMonster");
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        int handIndex = Observer.handIndexOf(card);
        args.put("hand_index", Integer.valueOf(handIndex));
        if (aim instanceof AbstractMonster) {
            int monsterIndex = Observer.monsterIndexOf((AbstractMonster) aim);
            if (monsterIndex >= 0) {
                args.put("target", "m" + monsterIndex);
            }
        }
        synchronized (LOCK) {
            pendingCard = payload(ActionSpec.PLAY_CARD, args);
        }
    }

    /** `receiveCardUsed` 时调用：牌真的被打出了，这时候才算已提交。 */
    public static Map<String, Object> drainCard() {
        synchronized (LOCK) {
            Map<String, Object> out = pendingCard;
            pendingCard = null;
            return out;
        }
    }

    // ------------------------------------------------------- 药水

    /**
     * BaseMod 的 `receivePrePotionUse` 里调用：药水已确认使用、还没从槽位移除，
     * 因此 `potions.indexOf` 还能拿到真实槽位下标。
     */
    public static void notePotionUse(AbstractPotion potion) {
        if (potion == null) {
            return;
        }
        AbstractPlayer p = AbstractDungeon.player;
        int slot = -1;
        if (p != null && p.potions != null) {
            slot = p.potions.indexOf(potion);
        }
        if (slot < 0) {
            slot = potion.slot;
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("potion_index", Integer.valueOf(slot));
        if (potion.targetRequired) {
            int monsterIndex = hoveredMonsterIndex();
            if (monsterIndex >= 0) {
                args.put("target", "m" + monsterIndex);
            }
        }
        emit(payload(ActionSpec.USE_POTION, args));
    }

    /** 鼠标停在哪个敌人身上（游戏自己的选目标规则，不模拟鼠标）。 */
    private static int hoveredMonsterIndex() {
        List<AbstractMonster> alive = Observer.aliveMonsters();
        for (AbstractMonster m : alive) {
            if (m.hb != null && m.hb.hovered) {
                return Observer.monsterIndexOf(m);
            }
        }
        AbstractPlayer p = AbstractDungeon.player;
        Object aim = Reflect.get(p, AbstractPlayer.class, "hoveredMonster");
        if (aim instanceof AbstractMonster) {
            return Observer.monsterIndexOf((AbstractMonster) aim);
        }
        return -1;
    }

    // ------------------------------------------------------- 地图

    /**
     * `MapRoomNode.update()` 的**前缀**：记下这一帧 `DungeonMapScreen.clicked` 的值。
     *
     * 为什么非要前缀不可：`nextRoom` 不是点击那一刻设置的。反编译 `MapRoomNode.update()`
     * 可以看到两条互斥的路径 ——
     *
     *   1. 点击路径（`hb.hovered && screen == MAP && dungeonMapScreen.clicked && animWaitTimer <= 0`）：
     *      播放音效、`clicked = false`、`animWaitTimer = 0.25f`；
     *   2. 定时器路径（下一帧起 `animWaitTimer` 递减到 0 以下）：
     *      `AbstractDungeon.nextRoom = this` 然后 `nextRoomTransitionStart()`。
     *
     * 也就是说 `nextRoom == this` 要到点击后约 0.25 秒才成立，而那时
     * `MapRoomNode.update()` 早就跑完了别的格子 —— 原来的
     * `node.taken && nextRoom == node` 判据**永远为假**，一个地图动作都抓不到。
     *
     * 唯一可靠的判据是"这一次 `update()` 把 `clicked` 从 true 变成了 false"：
     * 只有被点中的那一格会走点击路径，也只有那条路径会清 `clicked`。
     */
    public static void noteMapClickBefore(boolean clicked) {
        mapClickBefore = clicked;
    }

    /** `MapRoomNode.update()` 的**后缀**：`clicked` 由真变假 = 人类点了这一格。 */
    public static void noteMapNode(int x, int y, boolean clickedNow) {
        boolean accepted = mapClickBefore && !clickedNow;
        mapClickBefore = false;
        if (!accepted) {
            return;
        }
        String id = Observer.nodeId(x, y);
        synchronized (LOCK) {
            if (id.equals(lastMapNode)) {
                return;
            }
            lastMapNode = id;
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("node", id);
        emit(payload(ActionSpec.SELECT_MAP_NODE, args));
    }

    // ------------------------------------------------------- 事件 / Neow

    /**
     * 事件对话框 `update()` 的**前缀**：记下这一帧 `waitForInput` 的值。
     *
     * 反编译里两条对话框（`GenericEventDialog` / `RoomEventDialog`）的提交点一模一样：
     *
     *     if (optionList.get(i).pressed && waitForInput) {
     *         selectedOption = i;
     *         optionList.get(i).pressed = false;
     *         waitForInput = false;      // <- 只有这里会把它置假
     *     }
     *
     * 所以"`waitForInput` 由真变假"就是**人类真的选了**那一帧。以前只判
     * `selectedOption` 合法就上报，而 `selectedOption` 是**静态字段、提交后一直留着**，
     * 于是只要还有别的界面带着非空 `optionList`（Neow 选完之后的残留就是），
     * 每帧都会重新上报一次 —— 真机上刷了 401 条 `select_choice index:0`。
     */
    public static void noteDialogWaitingBefore(boolean waitForInput) {
        dialogWaitingBefore = waitForInput;
    }

    /** 事件 / Neow 对话框 `update()` 的**后缀**：`waitForInput` 由真变假 = 人类提交了选项。 */
    public static void noteEventOption(
            List<LargeDialogOptionButton> buttons, int selected, boolean waitForInputNow) {
        boolean committed = dialogWaitingBefore && !waitForInputNow;
        dialogWaitingBefore = false;
        if (!committed || buttons == null || selected < 0 || selected >= buttons.size()) {
            return;
        }
        Object button = buttons.get(selected);
        synchronized (LOCK) {
            if (selected == lastEventOption && button == lastEventButton) {
                return;
            }
            lastEventOption = selected;
            lastEventButton = button;
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("index", Integer.valueOf(selected));
        emit(payload(ActionSpec.SELECT_CHOICE, args));
    }

    // ------------------------------------------------------- 结束回合

    /**
     * 人类结束回合。**没有用 patch**：`SpireAgentMod` 每帧轮询
     * `AbstractPlayer.endTurnQueued`（游戏自己在 `EndTurnButton.disable(true)` 里把它
     * 置真），好处是点击按钮、按键、长按三条路径一次全覆盖。
     */
    public static void noteEndTurn() {
        emit(payload(ActionSpec.END_TURN, new LinkedHashMap<String, Object>()));
    }

    // ------------------------------------------------------- 奖励 / 商店 / 篝火

    /** 卡牌奖励：`index` 为奖励候选下标（`Observer.cardRewardCards()` 的下标）。 */
    public static void noteCardReward(AbstractCard card) {
        List<AbstractCard> cards = Observer.cardRewardCards();
        int index = card == null ? -1 : cards.indexOf(card);
        if (index < 0) {
            return;
        }
        synchronized (LOCK) {
            if (card == lastCardRewardCard) {
                return;
            }
            lastCardRewardCard = card;
            lastCardRewardSkip = false;
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("index", Integer.valueOf(index));
        emit(payload(ActionSpec.SELECT_REWARD, args));
    }

    /** 卡牌奖励跳过。 */
    public static void noteCardRewardSkip() {
        synchronized (LOCK) {
            if (lastCardRewardSkip) {
                return;
            }
            lastCardRewardSkip = true;
            lastCardRewardCard = null;
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("index", Integer.valueOf(-1));
        emit(payload(ActionSpec.SELECT_CARD_REWARD, args));
    }

    /** 每帧调用：卡牌奖励界面消失（没有候选牌了）时清闩锁。 */
    public static void pollCardRewardLatch() {
        if (Observer.cardRewardCards().isEmpty()) {
            synchronized (LOCK) {
                lastCardRewardCard = null;
                lastCardRewardSkip = false;
            }
        }
    }

    /** 商店：按 `ShopSlots.list()` 的槽位下标购买（离开商店走 `proceed`，不在这里）。 */
    public static void noteShopPurchase(Object payload) {
        List<ShopSlots.Slot> slots = ShopSlots.list();
        int index = -1;
        for (int i = 0; i < slots.size(); i++) {
            if (slots.get(i).payload == payload) {
                index = i;
                break;
            }
        }
        if (index < 0 && ShopSlots.KIND_PURGE.equals(payload)) {
            for (int i = 0; i < slots.size(); i++) {
                if (ShopSlots.KIND_PURGE.equals(slots.get(i).kind)) {
                    index = i;
                    break;
                }
            }
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("index", Integer.valueOf(index));
        emit(payload(ActionSpec.SELECT_CHOICE, args));
    }

    /** 篝火：按 `CampfireSlots.list()` 的下标（`useOption` 的调用者是那两个按钮之一）。 */
    public static void noteCampfireOption(Object option) {
        List<CampfireSlots.Slot> slots = CampfireSlots.list();
        int index = -1;
        for (int i = 0; i < slots.size(); i++) {
            if (slots.get(i).payload == option) {
                index = i;
                break;
            }
        }
        Map<String, Object> args = new LinkedHashMap<String, Object>();
        args.put("index", Integer.valueOf(index));
        emit(payload(ActionSpec.SELECT_CHOICE, args));
    }

    // ------------------------------------------------------- 内部

    /** 供 SelfTest 与 `SpireAgentMod` 复用的载荷构造函数。 */
    public static Map<String, Object> payload(String kind, Map<String, Object> args) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("kind", kind);
        out.put("args", args == null ? new LinkedHashMap<String, Object>() : args);
        return out;
    }

    private static void emit(Map<String, Object> action) {
        Sink s = sink;
        if (s == null) {
            return;
        }
        try {
            s.onHumanAction(action);
        } catch (RuntimeException e) {
            spireagent.Log.warn("human action sink failed: " + e);
        }
    }

    /** 测试/重开局用：清掉去重闩锁。 */
    public static void resetLatches() {
        synchronized (LOCK) {
            pendingCard = null;
            lastMapNode = null;
            lastEventOption = -1;
            lastEventButton = null;
            lastCardRewardCard = null;
            lastCardRewardSkip = false;
        }
    }
}

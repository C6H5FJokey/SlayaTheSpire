package spireagent.act;

import com.evacipated.cardcrawl.modthespire.lib.SpirePatch;
import com.evacipated.cardcrawl.modthespire.lib.SpirePostfixPatch;
import com.evacipated.cardcrawl.modthespire.lib.SpirePrefixPatch;

import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.core.CardCrawlGame;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.events.GenericEventDialog;
import com.megacrit.cardcrawl.events.RoomEventDialog;
import com.megacrit.cardcrawl.map.MapRoomNode;
import com.megacrit.cardcrawl.screens.CardRewardScreen;
import com.megacrit.cardcrawl.screens.DungeonMapScreen;
import com.megacrit.cardcrawl.shop.ShopScreen;
import com.megacrit.cardcrawl.shop.StorePotion;
import com.megacrit.cardcrawl.shop.StoreRelic;
import com.megacrit.cardcrawl.ui.campfire.RestOption;
import com.megacrit.cardcrawl.ui.campfire.SmithOption;

import spireagent.SlDetector;

/**
 * 人类动作与读档（SL）的 hook 点（见 docs/03-mod-protocol.md#人类动作捕获）。
 *
 * 一张表说明"哪些人类提交点被覆盖了"，全部走游戏自己的提交入口，
 * **不模拟鼠标、不模拟按键**：
 *
 * | 提交点 | hook | 上报 |
 * |---|---|---|
 * | 出牌 | `AbstractPlayer.playCard` 前缀 + `receiveCardUsed` | `play_card` |
 * | 用药水 | BaseMod `receivePrePotionUse` | `use_potion` |
 * | 结束回合 | 轮询 `AbstractPlayer.endTurnQueued` | `end_turn` |
 * | 点地图节点 | `MapRoomNode.update` 前缀读 `clicked` + 后缀比对 | `select_map_node` |
 * | 事件 / Neow 选项 | `GenericEventDialog.update` / `RoomEventDialog.update` 后缀 | `select_choice` |
 * | 拿卡牌奖励 | `CardRewardScreen.acquireCard` 前缀 | `select_reward` |
 * | 跳过卡牌奖励 | `CardRewardScreen.skippedCards` 前缀 | `select_card_reward(-1)` |
 * | 商店购买 | `ShopScreen.purchaseCard` / `StoreRelic.purchaseRelic` / `StorePotion.purchasePotion` / `ShopScreen.purgeCard` | `select_choice` |
 * | 篝火休息 / 锻造 | `RestOption.useOption` / `SmithOption.useOption` | `select_choice` |
 * | 读档（SL） | `CardCrawlGame.loadPlayerSave` 前缀 | `observation.sl` |
 *
 * 未覆盖（v1 已知缺口，agent 侧会记成 `matched=false`）：Boss 遗物点选、
 * 宝箱、商店"离开"、战斗奖励的"继续"按钮、以及 `select_cards` 类选牌确认 ——
 * 这些走的是通用按钮回调，后续版本再补。
 *
 * **参数对齐规则**：MTS 的 legacy `@SpirePatch`（非 `@SpirePatch2`）把 patch 方法的
 * 形参**按位置**逐个喂给目标方法的 `$0, $1, ...`，而 `$0` 对实例方法就是 `this`。
 * 所以目标方法只要带参数，patch 形参就必须从 `this` 的同类型占位参数开始逐个写全；
 * 无参目标方法则只写 `this` 一个（或干脆不写）。写错会在加载模组时报
 * `CannotCompileException: Prefix(...) not found` 而**让游戏起不来**，
 * `SelfTest` 的 `checkPatches` 会在打包时就拦住这类错误。
 */
public final class HumanActionPatches {

    private HumanActionPatches() {
    }

    /** 出牌：前缀里 `hoveredCard` 还没被清空，是唯一读得到"哪张牌 + 打谁"的时机。 */
    @SpirePatch(clz = AbstractPlayer.class, method = "playCard")
    public static class CardPlay {
        @SpirePrefixPatch
        public static void Prefix(AbstractPlayer __instance) {
            HumanActionTap.noteCardAim(__instance);
        }
    }

    /**
     * 地图：`DungeonMapScreen.clicked` 被这一格由真置假 = 人类点了这一格。
     *
     * 判据为什么不是 `taken && nextRoom == this`：`nextRoom` 是点击后约 0.25 秒
     * 由 `animWaitTimer` 到期那条分支设置的，而 `taken` 要等**离开**这一格才置真 ——
     * 两个条件在点击那一帧都不成立，所以原来一个地图动作都抓不到（真机验证过）。
     */
    @SpirePatch(clz = MapRoomNode.class, method = "update")
    public static class MapNode {
        @SpirePrefixPatch
        public static void Prefix(MapRoomNode __instance) {
            DungeonMapScreen screen = AbstractDungeon.dungeonMapScreen;
            HumanActionTap.noteMapClickBefore(screen != null && screen.clicked);
        }

        @SpirePostfixPatch
        public static void Postfix(MapRoomNode __instance) {
            DungeonMapScreen screen = AbstractDungeon.dungeonMapScreen;
            HumanActionTap.noteMapNode(
                    __instance.x, __instance.y, screen != null && screen.clicked);
        }
    }

    /** 事件 / Neow 对话框：`waitForInput` 由真变假的那一帧才是人类提交的选项。 */
    @SpirePatch(clz = GenericEventDialog.class, method = "update")
    public static class EventDialog {
        @SpirePrefixPatch
        public static void Prefix(GenericEventDialog __instance) {
            HumanActionTap.noteDialogWaitingBefore(GenericEventDialog.waitForInput);
        }

        @SpirePostfixPatch
        public static void Postfix(GenericEventDialog __instance) {
            HumanActionTap.noteEventOption(
                    __instance.optionList,
                    GenericEventDialog.selectedOption,
                    GenericEventDialog.waitForInput);
        }
    }

    @SpirePatch(clz = RoomEventDialog.class, method = "update")
    public static class RoomDialog {
        @SpirePrefixPatch
        public static void Prefix(RoomEventDialog __instance) {
            HumanActionTap.noteDialogWaitingBefore(RoomEventDialog.waitForInput);
        }

        @SpirePostfixPatch
        public static void Postfix(RoomEventDialog __instance) {
            HumanActionTap.noteEventOption(
                    RoomEventDialog.optionList,
                    RoomEventDialog.selectedOption,
                    RoomEventDialog.waitForInput);
        }
    }

    /** 卡牌奖励：拿某一张。 */
    @SpirePatch(clz = CardRewardScreen.class, method = "acquireCard")
    public static class AcquireCard {
        @SpirePrefixPatch
        public static void Prefix(CardRewardScreen __instance, AbstractCard card) {
            HumanActionTap.noteCardReward(card);
        }
    }

    /** 卡牌奖励：跳过。 */
    @SpirePatch(clz = CardRewardScreen.class, method = "skippedCards")
    public static class SkipCardReward {
        @SpirePrefixPatch
        public static void Prefix() {
            HumanActionTap.noteCardRewardSkip();
        }
    }

    @SpirePatch(clz = ShopScreen.class, method = "purchaseCard")
    public static class BuyCard {
        @SpirePrefixPatch
        public static void Prefix(ShopScreen __instance, AbstractCard card) {
            HumanActionTap.noteShopPurchase(card);
        }
    }

    @SpirePatch(clz = StoreRelic.class, method = "purchaseRelic")
    public static class BuyRelic {
        @SpirePrefixPatch
        public static void Prefix(StoreRelic __instance) {
            HumanActionTap.noteShopPurchase(__instance);
        }
    }

    @SpirePatch(clz = StorePotion.class, method = "purchasePotion")
    public static class BuyPotion {
        @SpirePrefixPatch
        public static void Prefix(StorePotion __instance) {
            HumanActionTap.noteShopPurchase(__instance);
        }
    }

    @SpirePatch(clz = ShopScreen.class, method = "purgeCard")
    public static class BuyPurge {
        @SpirePrefixPatch
        public static void Prefix() {
            HumanActionTap.noteShopPurchase(spireagent.obs.ShopSlots.KIND_PURGE);
        }
    }

    @SpirePatch(clz = RestOption.class, method = "useOption")
    public static class RestAtCampfire {
        @SpirePrefixPatch
        public static void Prefix(RestOption __instance) {
            HumanActionTap.noteCampfireOption(__instance);
        }
    }

    @SpirePatch(clz = SmithOption.class, method = "useOption")
    public static class SmithAtCampfire {
        @SpirePrefixPatch
        public static void Prefix(SmithOption __instance) {
            HumanActionTap.noteCampfireOption(__instance);
        }
    }

    /**
     * 读档（SL）的**主信号**：这条路径就是"从主菜单继续一局"。
     *
     * 不用 `SaveAndContinue.loadSaveString`：主菜单要用它判断按钮是否可用，
     * 挂在那里每次启动都会误报。
     */
    @SpirePatch(clz = CardCrawlGame.class, method = "loadPlayerSave")
    public static class LoadPlayerSave {
        @SpirePrefixPatch
        public static void Prefix() {
            SlDetector.noteLoad("CardCrawlGame.loadPlayerSave");
        }
    }

}

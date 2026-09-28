package spireagent.act;

import java.util.ArrayList;
import java.util.List;

import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.cards.CardGroup;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.potions.AbstractPotion;
import com.megacrit.cardcrawl.relics.AbstractRelic;
import com.megacrit.cardcrawl.screens.select.GridCardSelectScreen;
import com.megacrit.cardcrawl.screens.select.HandCardSelectScreen;

import spireagent.Reflect;
import spireagent.obs.CampfireSlots;
import spireagent.obs.Observer;
import spireagent.obs.ShopSlots;
import spireagent.proto.ActionContext;

/**
 * `ActionContext` 的游戏侧实现：把动作校验需要的量从真实游戏对象里读出来。
 *
 * 与 `Observer` 的**同源约束**：这里的每个计数都必须与观测里同名数组的下标
 * 一一对应（尤其是商店槽位、事件按钮、篝火按钮）。所以凡是"下标从哪来"的
 * 问题一律复用 `Observer` / `ShopSlots` / `CampfireSlots` 的定义，
 * 不在这里重新数一遍。
 */
public final class GameActionContext implements ActionContext {

    private static final GameActionContext INSTANCE = new GameActionContext();

    public static GameActionContext get() {
        return INSTANCE;
    }

    private GameActionContext() {
    }

    // ------------------------------------------------------------- 战斗

    public boolean inCombat() {
        return Observer.inCombat();
    }

    public int handSize() {
        CardGroup hand = AbstractDungeon.player == null ? null : AbstractDungeon.player.hand;
        return hand == null || hand.group == null ? 0 : hand.group.size();
    }

    public boolean canPlayHandCard(int handIndex) {
        AbstractCard card = handCard(handIndex);
        return card != null && Observer.isPlayable(card);
    }

    public boolean handCardNeedsTarget(int handIndex) {
        AbstractCard card = handCard(handIndex);
        return card != null && card.target == AbstractCard.CardTarget.ENEMY;
    }

    public boolean isLiveMonster(int index) {
        return Observer.monsterAt(index) != null;
    }

    // ------------------------------------------------------------- 药水

    public int potionSlots() {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null) {
            return 0;
        }
        if (p.potions != null && p.potions.size() > p.potionSlots) {
            return p.potions.size();
        }
        return p.potionSlots;
    }

    public boolean potionEmpty(int slot) {
        AbstractPotion pot = potion(slot);
        return pot == null || pot.ID == null;
    }

    public boolean canUsePotion(int slot) {
        AbstractPotion pot = potion(slot);
        if (pot == null) {
            return false;
        }
        try {
            return pot.canUse();
        } catch (RuntimeException e) {
            return false;
        }
    }

    public boolean potionNeedsTarget(int slot) {
        AbstractPotion pot = potion(slot);
        return pot != null && pot.targetRequired;
    }

    // ------------------------------------------------------------- 界面

    /**
     * 通用选项数量（`select_choice` 的上界）。按界面分派、与观测同源：
     * COMBAT_REWARD = 活奖励数 + 1（最后一项是"继续"）。
     */
    public int optionCount() {
        String screen = Observer.screenName();
        if (Observer.SCREEN_EVENT.equals(screen) || Observer.SCREEN_NEOW.equals(screen)) {
            if (Observer.SCREEN_EVENT.equals(screen)
                    && Observer.specialEventOptionCount() > 0) {
                return Observer.specialEventOptionCount();
            }
            return Observer.optionButtons().size();
        }
        if (Observer.SCREEN_SHOP.equals(screen)) {
            return ShopSlots.list().size();
        }
        if (Observer.SCREEN_REST.equals(screen)) {
            return CampfireSlots.list().size();
        }
        if (Observer.SCREEN_COMBAT_REWARD.equals(screen)) {
            return Observer.liveRewards().size() + 1;
        }
        if (Observer.SCREEN_BOSS_RELIC.equals(screen)) {
            return Observer.relicChoices().size();
        }
        return 0;
    }

    public int rewardCount() {
        return Observer.liveRewards().size();
    }

    public int cardRewardCount() {
        return Observer.cardRewardCards().size();
    }

    public boolean cardRewardScreen() {
        return Observer.SCREEN_CARD_REWARD.equals(Observer.screenName());
    }

    public boolean combatRewardScreen() {
        return Observer.SCREEN_COMBAT_REWARD.equals(Observer.screenName());
    }

    public boolean hasProceedButton() {
        return !Actor.proceedHidden();
    }

    public boolean hasReturnButton() {
        try {
            return AbstractDungeon.overlayMenu != null
                    && AbstractDungeon.overlayMenu.cancelButton != null
                    && !AbstractDungeon.overlayMenu.cancelButton.isHidden;
        } catch (RuntimeException e) {
            return false;
        }
    }

    // ------------------------------------------------------------- 选牌

    public int selectableCardCount() {
        List<AbstractCard> pool = Actor.selectionPool();
        return pool == null ? 0 : pool.size();
    }

    /** `{min, max}`；非选牌界面返回 null。 */
    public int[] selectionBounds() {
        if (!Observer.SCREEN_GRID.equals(Observer.screenName())) {
            return null;
        }
        int total = selectableCardCount();
        if (AbstractDungeon.screen == AbstractDungeon.CurrentScreen.HAND_SELECT) {
            HandCardSelectScreen hs = AbstractDungeon.handCardSelectScreen;
            if (hs == null) {
                return null;
            }
            boolean any = hs.canPickZero || hs.upTo;
            int need = Math.max(0, hs.numCardsToSelect);
            return any ? new int[] {0, total} : new int[] {need, need};
        }
        GridCardSelectScreen gs = AbstractDungeon.gridSelectScreen;
        if (gs == null) {
            return null;
        }
        if (gs.isJustForConfirming) {
            return new int[] {0, 0};
        }
        Integer num = (Integer) Reflect.get(gs, GridCardSelectScreen.class, "numCards");
        int need = num == null ? 1 : Math.max(0, num.intValue());
        return gs.anyNumber ? new int[] {0, total} : new int[] {need, need};
    }

    // ------------------------------------------------------------- 地图

    public boolean mapScreen() {
        return Observer.SCREEN_MAP.equals(Observer.screenName());
    }

    public boolean reachableNode(String nodeId) {
        if (nodeId == null) {
            return false;
        }
        for (String id : Observer.reachableNodeIds()) {
            if (nodeId.equals(id)) {
                return true;
            }
        }
        return false;
    }

    // ------------------------------------------------------------- 内部

    private static AbstractCard handCard(int index) {
        CardGroup hand = AbstractDungeon.player == null ? null : AbstractDungeon.player.hand;
        if (hand == null || hand.group == null || index < 0 || index >= hand.group.size()) {
            return null;
        }
        return hand.group.get(index);
    }

    private static AbstractPotion potion(int slot) {
        AbstractPlayer p = AbstractDungeon.player;
        if (p == null || p.potions == null || slot < 0 || slot >= p.potions.size()) {
            return null;
        }
        return p.potions.get(slot);
    }

}

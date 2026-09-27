package spireagent.proto;

/**
 * 动作校验需要的"世界状态"接口。
 *
 * 刻意做成接口：`ActionSpec` 因此完全不依赖游戏类，可以在没有游戏的机器上
 * 用假实现单测（见 mod/src/test/java/spireagent/SelfTest.java）。
 */
public interface ActionContext {

    boolean inCombat();

    int handSize();

    /** 该手牌当前是否可出（能量、状态、目标可用性等）。 */
    boolean canPlayHandCard(int handIndex);

    /** 该手牌是否必须指定目标（单体攻击/单体技能）。 */
    boolean handCardNeedsTarget(int handIndex);

    /**
     * 游戏数组下标 index 上的敌人是否在场且活着 —— 这就是 mN 的合法判据。
     *
     * 判据**不是** "index < 活着的敌人数"：room.monsters.monsters 在整场战斗里只增不减，
     * 尸体继续占位，所以杀掉 m0 之后活着的那个仍然是 m1。拿"活着的数量"当上界，
     * 会让这次击杀之后所有带目标的动作全部被判越界。
     */
    boolean isLiveMonster(int index);

    int potionSlots();

    boolean potionEmpty(int slot);

    boolean canUsePotion(int slot);

    boolean potionNeedsTarget(int slot);

    /** 当前界面上的可选项数量（事件、篝火、商店、Neow、通用选项）。 */
    int optionCount();

    /** COMBAT_REWARD 界面上的奖励条目数。 */
    int rewardCount();

    /** CARD_REWARD 界面上的可选卡数。 */
    int cardRewardCount();

    boolean cardRewardScreen();

    boolean combatRewardScreen();

    boolean hasProceedButton();

    boolean hasReturnButton();

    /** 选牌界面（GRID / HAND_SELECT）的可选牌数；非选牌界面返回 0。 */
    int selectableCardCount();

    /** 选牌界面的 {min, max} 张数约束；非选牌界面返回 null。 */
    int[] selectionBounds();

    boolean mapScreen();

    boolean reachableNode(String nodeId);
}
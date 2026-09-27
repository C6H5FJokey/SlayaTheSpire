package spireagent.obs;

/**
 * 空药水槽的判定。纯逻辑，可离线自检（不碰游戏类）。
 *
 * 坑：空槽是 {@code PotionSlot}，它的 {@code canUse()} 直接继承 {@code AbstractPotion}
 * 并返回 **true** —— 游戏自己判空槽从来不看它，看的是 id（{@code PotionSlot.POTION_ID}
 * 就是 "Potion Slot"）。模组照抄 {@code canUse()} 的后果：agent 把"用药水"当成可选项，
 * 选一次被拒一次、状态又不变，于是无限空转，看门狗也被这串动作压着不触发。
 */
public final class PotionFacts {

    /** 空槽的 id（等于 `PotionSlot.POTION_ID`）。 */
    public static final String EMPTY_SLOT_ID = "Potion Slot";

    private PotionFacts() {
    }

    /** 这个槽对 agent 来说能不能用：空槽一律不能用，哪怕 `canUse()` 说能用。 */
    public static boolean usable(String potionId, boolean canUse) {
        if (potionId == null || EMPTY_SLOT_ID.equals(potionId)) {
            return false;
        }
        return canUse;
    }
}

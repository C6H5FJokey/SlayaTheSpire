package spireagent.obs;

import java.util.HashSet;
import java.util.List;
import java.util.Set;

/**
 * "这组牌住在哪个牌堆"的纯逻辑判定（`Observer.zoneOf` 的第二级判据）。
 *
 * 游戏会为"临时挑牌"当场造一个 `new CardGroup(CardGroupType.UNSPECIFIED)`：
 * 观者的预见（`ScryAction`）、秘密技法/秘密武器、全知、以及攻击/技能/能力药水。
 * 这种牌组跟玩家任何一个牌堆都**不是同一个对象**，引用比较必然落空，只能按
 * "这些牌现在住在哪"反查。
 *
 * 为什么内容比较在这里是安全的：一个 `AbstractCard` 实例同时只存在于一个牌堆里
 * （游戏自己靠 `removeCard` 搬牌），所以"每个 uuid 都出现在 X 堆里"是精确判据，
 * 不是启发式。判据只看**成员**，不看顺序，也不参与候选下标（下标一律取自游戏
 * 数组下标，见 docs/05-state-schema.md）。
 *
 * 纯逻辑：只依赖 `List<String>`，可以离线单测（见 SelfTest）。
 */
public final class ZoneGuess {

    public static final String HAND = "hand";
    public static final String DRAW = "draw";
    public static final String DISCARD = "discard";
    public static final String EXHAUST = "exhaust";
    public static final String DECK = "deck";
    /** 哪都不住：药水当场造出来的候选（"三张随机能力牌"），只存在于界面上。 */
    public static final String OFFER = "offer";

    private ZoneGuess() {
    }

    /**
     * @param target  选牌界面的候选（uuid，按界面顺序）
     * @param hand/draw/discard/exhaust/deck 各牌堆的 uuid 集合
     * @return 候选全都住在哪个牌堆；都不住 -> `offer`；空 / 脏数据 -> `hand`
     */
    public static String of(List<String> target, List<String> hand, List<String> draw,
            List<String> discard, List<String> exhaust, List<String> deck) {
        if (target == null || target.isEmpty()) {
            return HAND;
        }
        if (containsAll(hand, target)) {
            return HAND;
        }
        if (containsAll(draw, target)) {
            return DRAW;
        }
        if (containsAll(discard, target)) {
            return DISCARD;
        }
        if (containsAll(exhaust, target)) {
            return EXHAUST;
        }
        if (containsAll(deck, target)) {
            return DECK;
        }
        return OFFER;
    }

    private static boolean containsAll(List<String> pile, List<String> target) {
        if (pile == null || pile.isEmpty()) {
            return false;
        }
        Set<String> have = new HashSet<String>(pile);
        for (String uuid : target) {
            if (uuid == null || uuid.isEmpty()) {
                // 拿不到 uuid 的牌不能当证据：宁可判不出来，也不编一个区域。
                return false;
            }
            if (!have.contains(uuid)) {
                return false;
            }
        }
        return true;
    }
}
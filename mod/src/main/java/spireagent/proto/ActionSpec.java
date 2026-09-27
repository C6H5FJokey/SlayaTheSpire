package spireagent.proto;

import java.util.List;
import java.util.Map;

import spireagent.bridge.Json;

/**
 * 语义动作白名单与校验（见 docs/03-mod-protocol.md#语义动作白名单）。
 *
 * 白名单里**没有**读档、重开、退出这类动作，所以 agent 在协议层就无法 SL。
 * 校验分两层：结构（参数类型/必填/范围）与语义（交给 `ActionContext`）。
 */
public final class ActionSpec {

    public static final String PLAY_CARD = "play_card";
    public static final String USE_POTION = "use_potion";
    public static final String DISCARD_POTION = "discard_potion";
    public static final String END_TURN = "end_turn";
    public static final String SELECT_CHOICE = "select_choice";
    public static final String SELECT_REWARD = "select_reward";
    public static final String PROCEED = "proceed";
    public static final String RETURN = "return";
    public static final String SELECT_CARDS = "select_cards";
    public static final String SELECT_CARD_REWARD = "select_card_reward";
    public static final String SELECT_MAP_NODE = "select_map_node";

    public static final String[] KINDS = {
        PLAY_CARD,
        USE_POTION,
        DISCARD_POTION,
        END_TURN,
        SELECT_CHOICE,
        SELECT_REWARD,
        PROCEED,
        RETURN,
        SELECT_CARDS,
        SELECT_CARD_REWARD,
        SELECT_MAP_NODE,
    };

    public static final class Result {
        public final String code;
        public final String message;

        Result(String code, String message) {
            this.code = code;
            this.message = message;
        }

        public boolean ok() {
            return code == null;
        }
    }

    private static final Result OK = new Result(null, null);

    private ActionSpec() {
    }

    public static boolean known(String kind) {
        for (String k : KINDS) {
            if (k.equals(kind)) {
                return true;
            }
        }
        return false;
    }

    public static Result validate(String kind, Map<String, Object> args, ActionContext ctx) {
        if (kind == null || !known(kind)) {
            return new Result(Errors.ILLEGAL_ACTION, "unknown action kind: " + kind);
        }
        if (args == null) {
            args = new java.util.LinkedHashMap<String, Object>();
        }
        if (PLAY_CARD.equals(kind)) {
            return playCard(args, ctx);
        }
        if (USE_POTION.equals(kind)) {
            return usePotion(args, ctx);
        }
        if (DISCARD_POTION.equals(kind)) {
            return discardPotion(args, ctx);
        }
        if (END_TURN.equals(kind)) {
            return ctx.inCombat() ? OK : mismatch("end_turn outside combat");
        }
        if (SELECT_CHOICE.equals(kind)) {
            // 两种形态：
            //   {"index": i}   —— 选项/遗物/奖励/篝火/商店槽位的下标；
            //                     index == -1 表示"离开/取消"（目前只有商店用）。
            //   {"monster": i} —— v1 里出牌/用药水都在动作里直接带目标，摸不到
            //                     独立的"选目标"界面；这里仍按契约放行，由 Actor
            //                     在真正遇到该状态时决定能否执行。
            if (args.get("monster") != null) {
                Integer mi = intArg(args, "monster");
                if (mi == null) {
                    return new Result(Errors.ILLEGAL_ACTION,
                            "select_choice args.monster must be an int");
                }
                if (!ctx.inCombat()) {
                    return mismatch("target selection outside combat");
                }
                if (!ctx.isLiveMonster(mi.intValue())) {
                    return new Result(Errors.INDEX_RANGE, "monster m" + mi + " is not a live enemy");
                }
                return OK;
            }
            Integer index = intArg(args, "index");
            if (index == null) {
                return new Result(Errors.ILLEGAL_ACTION,
                        "select_choice requires int args.index or int args.monster");
            }
            if (index.intValue() == -1) {
                return ctx.hasProceedButton() ? OK
                        : mismatch("select_choice(-1) but there is nothing to leave/cancel");
            }
            int count = ctx.optionCount();
            if (count <= 0) {
                return mismatch("select_choice but no options are shown");
            }
            if (index.intValue() < 0 || index.intValue() >= count) {
                return new Result(Errors.INDEX_RANGE,
                        "option index " + index + " out of range [0," + count + ")");
            }
            return OK;
        }
        if (SELECT_REWARD.equals(kind)) {
            // 两种界面共用一个动作名，语义由界面决定（见 Actor#selectReward）：
            //   COMBAT_REWARD -> index 是奖励列表下标；
            //   CARD_REWARD   -> index 是三选一里第几张牌（-1 = 跳过）。
            Integer index = intArg(args, "index");
            if (index == null) {
                return new Result(Errors.ILLEGAL_ACTION, "select_reward requires int args.index");
            }
            if (ctx.combatRewardScreen()) {
                if (index.intValue() < 0 || index.intValue() >= ctx.rewardCount()) {
                    return new Result(Errors.INDEX_RANGE,
                            "reward index " + index + " out of range [0," + ctx.rewardCount() + ")");
                }
                return OK;
            }
            if (ctx.cardRewardScreen()) {
                if (index.intValue() == -1) {
                    return OK;
                }
                if (index.intValue() < 0 || index.intValue() >= ctx.cardRewardCount()) {
                    return new Result(Errors.INDEX_RANGE,
                            "card reward index " + index + " out of range [0,"
                                    + ctx.cardRewardCount() + ")");
                }
                return OK;
            }
            return mismatch("select_reward but neither the combat reward nor the card reward"
                    + " screen is shown");
        }
        if (PROCEED.equals(kind)) {
            return ctx.hasProceedButton() ? OK : mismatch("no proceed button is shown");
        }
        if (RETURN.equals(kind)) {
            return ctx.hasReturnButton() ? OK : mismatch("no return/cancel button is shown");
        }
        if (SELECT_CARDS.equals(kind)) {
            return selectCards(args, ctx);
        }
        if (SELECT_CARD_REWARD.equals(kind)) {
            Integer index = intArg(args, "index");
            if (index == null) {
                return new Result(Errors.ILLEGAL_ACTION,
                        "select_card_reward requires int args.index (-1 = skip)");
            }
            if (!ctx.cardRewardScreen()) {
                return mismatch("select_card_reward but not on the card reward screen");
            }
            if (index.intValue() == -1) {
                return OK;
            }
            if (index.intValue() < 0 || index.intValue() >= ctx.cardRewardCount()) {
                return new Result(Errors.INDEX_RANGE,
                        "card reward index " + index + " out of range [0,"
                                + ctx.cardRewardCount() + ")");
            }
            return OK;
        }
        if (SELECT_MAP_NODE.equals(kind)) {
            String node = stringArg(args, "node");
            if (node == null) {
                return new Result(Errors.ILLEGAL_ACTION,
                        "select_map_node requires string args.node");
            }
            if (!ctx.mapScreen()) {
                return mismatch("select_map_node but the map is not shown");
            }
            if (!ctx.reachableNode(node)) {
                return new Result(Errors.ILLEGAL_ACTION,
                        "node " + node + " is not reachable from here");
            }
            return OK;
        }
        return new Result(Errors.ILLEGAL_ACTION, "unhandled action kind: " + kind);
    }

    private static Result playCard(Map<String, Object> args, ActionContext ctx) {
        if (!ctx.inCombat()) {
            return mismatch("play_card outside combat");
        }
        Integer hand = intArg(args, "hand_index");
        if (hand == null) {
            return new Result(Errors.ILLEGAL_ACTION, "play_card requires int args.hand_index");
        }
        int size = ctx.handSize();
        if (hand.intValue() < 0 || hand.intValue() >= size) {
            return new Result(Errors.INDEX_RANGE,
                    "hand_index " + hand + " out of range [0," + size + ")");
        }
        if (!ctx.canPlayHandCard(hand.intValue())) {
            return new Result(Errors.CARD_NOT_PLAYABLE, "card at hand_index " + hand
                    + " cannot be played right now");
        }
        String target = stringArg(args, "target");
        boolean needs = ctx.handCardNeedsTarget(hand.intValue());
        if (needs && target == null) {
            return new Result(Errors.ILLEGAL_ACTION,
                    "card at hand_index " + hand + " requires args.target");
        }
        if (target != null) {
            Integer mi = monsterIndex(target);
            if (mi == null) {
                return new Result(Errors.ILLEGAL_ACTION, "target must look like \"m0\": " + target);
            }
            if (!ctx.isLiveMonster(mi.intValue())) {
                return new Result(Errors.INDEX_RANGE, "target " + target + " is not a live enemy");
            }
        }
        return OK;
    }

    private static Result usePotion(Map<String, Object> args, ActionContext ctx) {
        Integer slot = intArg(args, "potion_index");
        if (slot == null) {
            return new Result(Errors.ILLEGAL_ACTION, "use_potion requires int args.potion_index");
        }
        int slots = ctx.potionSlots();
        if (slot.intValue() < 0 || slot.intValue() >= slots) {
            return new Result(Errors.INDEX_RANGE,
                    "potion_index " + slot + " out of range [0," + slots + ")");
        }
        if (ctx.potionEmpty(slot.intValue())) {
            return new Result(Errors.ILLEGAL_ACTION, "potion slot " + slot + " is empty");
        }
        if (!ctx.canUsePotion(slot.intValue())) {
            return new Result(Errors.ILLEGAL_ACTION, "potion " + slot + " cannot be used now");
        }
        String target = stringArg(args, "target");
        if (ctx.potionNeedsTarget(slot.intValue())) {
            if (target == null) {
                return new Result(Errors.ILLEGAL_ACTION,
                        "potion " + slot + " requires args.target");
            }
            Integer mi = monsterIndex(target);
            if (mi == null || !ctx.isLiveMonster(mi.intValue())) {
                return new Result(Errors.INDEX_RANGE, "bad potion target: " + target);
            }
        } else if (target != null) {
            return new Result(Errors.ILLEGAL_ACTION,
                    "potion " + slot + " does not take a target");
        }
        return OK;
    }

    private static Result discardPotion(Map<String, Object> args, ActionContext ctx) {
        Integer slot = intArg(args, "potion_index");
        if (slot == null) {
            return new Result(Errors.ILLEGAL_ACTION,
                    "discard_potion requires int args.potion_index");
        }
        if (slot.intValue() < 0 || slot.intValue() >= ctx.potionSlots()) {
            return new Result(Errors.INDEX_RANGE, "potion_index " + slot + " out of range");
        }
        if (ctx.potionEmpty(slot.intValue())) {
            return new Result(Errors.ILLEGAL_ACTION, "potion slot " + slot + " is empty");
        }
        return OK;
    }

    private static Result selectCards(Map<String, Object> args, ActionContext ctx) {
        Object raw = args.get("indices");
        if (!(raw instanceof List)) {
            return new Result(Errors.ILLEGAL_ACTION,
                    "select_cards requires list args.indices");
        }
        List<?> items = (List<?>) raw;
        int selectable = ctx.selectableCardCount();
        if (selectable <= 0) {
            return mismatch("select_cards but no card-selection screen is open");
        }
        int[] bounds = ctx.selectionBounds();
        int min = bounds == null ? 0 : bounds[0];
        int max = bounds == null ? selectable : bounds[1];
        if (items.size() < min || (max > 0 && items.size() > max)) {
            return new Result(Errors.ILLEGAL_ACTION, "select_cards picked " + items.size()
                    + " cards but this screen wants [" + min + "," + max + "]");
        }
        for (Object item : items) {
            List<?> pair = Json.asList(item);
            if (pair.size() != 2) {
                return new Result(Errors.ILLEGAL_ACTION,
                        "each entry of args.indices must be [zone, index]");
            }
            int idx = Json.asInt(pair.get(1), -1);
            if (idx < 0 || idx >= selectable) {
                return new Result(Errors.INDEX_RANGE, "card index " + idx + " out of range [0,"
                        + selectable + ")");
            }
        }
        return OK;
    }

    private static Result mismatch(String message) {
        return new Result(Errors.SCREEN_MISMATCH, message);
    }

    private static Integer intArg(Map<String, Object> args, String key) {
        Object v = args.get(key);
        if (v instanceof Number) {
            return Integer.valueOf(((Number) v).intValue());
        }
        if (v instanceof String) {
            try {
                return Integer.valueOf(Integer.parseInt((String) v));
            } catch (NumberFormatException ignored) {
                return null;
            }
        }
        return null;
    }

    private static String stringArg(Map<String, Object> args, String key) {
        Object v = args.get(key);
        return v instanceof String ? (String) v : null;
    }

    /** "m3" -> 3；其他形式返回 null。 */
    public static Integer monsterIndex(String target) {
        if (target == null || target.length() < 2 || target.charAt(0) != 'm') {
            return null;
        }
        try {
            return Integer.valueOf(Integer.parseInt(target.substring(1)));
        } catch (NumberFormatException e) {
            return null;
        }
    }
}

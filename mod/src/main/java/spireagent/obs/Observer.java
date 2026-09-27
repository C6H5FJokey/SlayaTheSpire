package spireagent.obs;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import com.megacrit.cardcrawl.actions.GameActionManager;
import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.cards.CardGroup;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.core.CardCrawlGame;
import com.megacrit.cardcrawl.core.Settings;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.events.AbstractEvent;
import com.megacrit.cardcrawl.events.RoomEventDialog;
import com.megacrit.cardcrawl.map.MapEdge;
import com.megacrit.cardcrawl.map.MapRoomNode;
import com.megacrit.cardcrawl.monsters.AbstractMonster;
import com.megacrit.cardcrawl.neow.NeowRoom;
import com.megacrit.cardcrawl.potions.AbstractPotion;
import com.megacrit.cardcrawl.powers.AbstractPower;
import com.megacrit.cardcrawl.relics.AbstractRelic;
import com.megacrit.cardcrawl.rewards.RewardItem;
import com.megacrit.cardcrawl.rooms.AbstractRoom;
import com.megacrit.cardcrawl.rooms.EventRoom;
import com.megacrit.cardcrawl.rooms.MonsterRoom;
import com.megacrit.cardcrawl.rooms.MonsterRoomBoss;
import com.megacrit.cardcrawl.rooms.MonsterRoomElite;
import com.megacrit.cardcrawl.rooms.RestRoom;
import com.megacrit.cardcrawl.rooms.ShopRoom;
import com.megacrit.cardcrawl.rooms.TreasureRoom;
import com.megacrit.cardcrawl.rooms.VictoryRoom;
import com.megacrit.cardcrawl.screens.select.BossRelicSelectScreen;
import com.megacrit.cardcrawl.screens.select.GridCardSelectScreen;
import com.megacrit.cardcrawl.screens.select.HandCardSelectScreen;
import com.megacrit.cardcrawl.ui.buttons.LargeDialogOptionButton;

import spireagent.Reflect;

/**
 * 观测采集（见 docs/05-state-schema.md#原始观测）。
 *
 * 三条纪律：
 *   1. 这里产出**未过滤**观测（含隐藏信息）；过滤责任在 agent 侧的 core.fairness。
 *   2. 任何一段失败都不能让整份观测消失 —— 每段都单独 try/catch，缺字段用默认值。
 *   3. 不做任何"美化"：字段名与游戏对象一一对应，agent 侧才知道怎么解释。
 */
public final class Observer {

    public static final String SCREEN_NONE = "NONE";
    public static final String SCREEN_MAP = "MAP";
    public static final String SCREEN_CARD_REWARD = "CARD_REWARD";
    public static final String SCREEN_COMBAT_REWARD = "COMBAT_REWARD";
    public static final String SCREEN_GRID = "GRID";
    public static final String SCREEN_EVENT = "EVENT";
    public static final String SCREEN_SHOP = "SHOP_ROOM";
    public static final String SCREEN_REST = "REST";
    public static final String SCREEN_BOSS_RELIC = "BOSS_RELIC";
    public static final String SCREEN_NEOW = "NEOW";
    public static final String SCREEN_GAME_OVER = "GAME_OVER";

    private Observer() {
    }

    /**
     * 稳定态判定（见 docs/03-mod-protocol.md#稳定性判定）。
     *
     * 条件 1（动作队列空闲）与条件 2（界面在等待输入）在这里精确判定；
     * 条件 3（动画未结束）由 `StabilityGate` 的去抖窗口近似 —— 连续若干帧
     * 都满足才算稳定，动画最后一帧的抖动自然被过滤掉。
     */
    public static boolean stable() {
        try {
            if (CardCrawlGame.dungeon == null || AbstractDungeon.player == null) {
                return false;
            }
            if (AbstractDungeon.isFadingIn || AbstractDungeon.isFadingOut
                    || AbstractDungeon.screenSwap) {
                return false;
            }
            GameActionManager am = AbstractDungeon.actionManager;
            if (am == null || am.phase != GameActionManager.Phase.WAITING_ON_USER) {
                return false;
            }
            if (!am.actions.isEmpty() || !am.cardQueue.isEmpty() || !am.monsterQueue.isEmpty()) {
                return false;
            }
            if (am.currentAction != null) {
                return false;
            }
            String screen = screenName();
            return screen != null && !screen.startsWith("UNKNOWN:");
        } catch (RuntimeException e) {
            return false;
        }
    }

    public static String screenName() {
        try {
            AbstractDungeon.CurrentScreen screen = AbstractDungeon.screen;
            switch (screen == null ? AbstractDungeon.CurrentScreen.NONE : screen) {
                case MAP:
                    return SCREEN_MAP;
                case CARD_REWARD:
                    return SCREEN_CARD_REWARD;
                case COMBAT_REWARD:
                    return SCREEN_COMBAT_REWARD;
                case GRID:
                case HAND_SELECT:
                    return SCREEN_GRID;
                case SHOP:
                    return SCREEN_SHOP;
                case BOSS_REWARD:
                    return SCREEN_BOSS_RELIC;
                case DEATH:
                case VICTORY:
                    return SCREEN_GAME_OVER;
                default:
                    break;
            }
            // `NONE` 不等于"没有界面"：事件房 / 篝火 / Neow / 宝箱都是**房间自己画**的，
            // 游戏不会把 `AbstractDungeon.screen` 置成任何值，它一直是 NONE。
            // 所以必须先按房间类型判，再判 NONE。
            //
            // 这里踩过一次真机坑：原来在 switch 里直接 `case NONE: return SCREEN_NONE;`，
            // 后果是**事件房永远被识别成"无界面"**（core 的 identify 只能抛
            // UnknownDecisionPoint）—— agent 从不下发决策，只能等 30s 看门狗盲点第一个
            // 选项。真机日志：`unknown decision point ... screen='NONE'` 后面紧跟
            // `[watchdog] no action within 30s`。篝火 / Neow 同理。
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (room instanceof NeowRoom) {
                return SCREEN_NEOW;
            }
            if (room instanceof RestRoom) {
                return SCREEN_REST;
            }
            if (room instanceof EventRoom || room != null && room.event != null) {
                return SCREEN_EVENT;
            }
            if (screen == null || screen == AbstractDungeon.CurrentScreen.NONE) {
                return SCREEN_NONE;
            }
            return "UNKNOWN:" + screen.name();
        } catch (RuntimeException e) {
            return SCREEN_NONE;
        }
    }

    public static boolean inCombat() {
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            return room != null && room.phase == AbstractRoom.RoomPhase.COMBAT
                    && room.monsters != null;
        } catch (RuntimeException e) {
            return false;
        }
    }

    public static Map<String, Object> observe() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        try {
            out.put("game_version", CardCrawlGame.VERSION_NUM);
            String screen = screenName();
            out.put("screen", screen);
            out.put("in_combat", Boolean.valueOf(inCombat()));
            out.put("language", Settings.language == null ? "" : Settings.language.name());
            out.put("act", Integer.valueOf(AbstractDungeon.actNum));
            out.put("floor", Integer.valueOf(AbstractDungeon.floorNum));
            out.put("ascension", Integer.valueOf(AbstractDungeon.isAscensionMode
                    ? AbstractDungeon.ascensionLevel : 0));
            out.put("run_seed", Long.valueOf(seed()));
            out.put("room", roomJson());
            out.put("player", safePlayer());
            out.put("combat", combatJson());
            out.put("deck", safeDeck());
            out.put("map", mapJson());
            out.put("screen_state", screenStateJson(screen));
            out.put("victory", Boolean.valueOf(screen == SCREEN_GAME_OVER && isVictory()));
        } catch (RuntimeException e) {
            out.put("observer_error", String.valueOf(e));
        }
        return out;
    }

    /** 局终是赢了还是死了（`GAME_OVER` 时需要区分，agent 侧据此写 run 结果）。 */
    private static boolean isVictory() {
        try {
            return AbstractDungeon.screen == AbstractDungeon.CurrentScreen.VICTORY;
        } catch (RuntimeException e) {
            return false;
        }
    }

    private static long seed() {
        try {
            if (CardCrawlGame.saveFile != null) {
                return CardCrawlGame.saveFile.seed;
            }
            return Settings.seed == null ? 0L : Settings.seed.longValue();
        } catch (RuntimeException e) {
            return 0L;
        }
    }

    private static Map<String, Object> roomJson() {
        Map<String, Object> room = new LinkedHashMap<String, Object>();
        try {
            MapRoomNode node = AbstractDungeon.currMapNode;
            int x = node == null ? 0 : node.x;
            int y = node == null ? 0 : node.y;
            room.put("act", Integer.valueOf(AbstractDungeon.actNum));
            room.put("floor", Integer.valueOf(AbstractDungeon.floorNum));
            room.put("node", Integer.valueOf(y * AbstractDungeon.MAP_WIDTH + x));
            room.put("x", Integer.valueOf(x));
            room.put("y", Integer.valueOf(y));
            room.put("type", roomType());
        } catch (RuntimeException e) {
            room.put("type", "UNKNOWN");
        }
        return room;
    }

    public static String roomType() {
        try {
            return typeOfRoom(AbstractDungeon.getCurrRoom());
        } catch (RuntimeException e) {
            return "UNKNOWN";
        }
    }

    private static Map<String, Object> safePlayer() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        try {
            AbstractPlayer p = AbstractDungeon.player;
            if (p == null) {
                return out;
            }
            out.put("character", p.chosenClass == null ? "IRONCLAD" : p.chosenClass.name());
            out.put("hp", Integer.valueOf(p.currentHealth));
            out.put("max_hp", Integer.valueOf(p.maxHealth));
            out.put("block", Integer.valueOf(p.currentBlock));
            out.put("energy", Integer.valueOf(energy(p)));
            out.put("gold", Integer.valueOf(p.gold));
            out.put("powers", powersJson(p.powers));
            List<Object> relics = new ArrayList<Object>();
            if (p.relics != null) {
                for (AbstractRelic r : p.relics) {
                    if (r == null) {
                        continue;
                    }
                    Map<String, Object> rj = new LinkedHashMap<String, Object>();
                    rj.put("id", r.relicId);
                    rj.put("counter", Integer.valueOf(r.counter));
                    rj.put("name", Eng.relicName(r.relicId));
                    rj.put("text", Eng.relicDesc(r.relicId));
                    relics.add(rj);
                }
            }
            out.put("relics", relics);
            List<Object> potions = new ArrayList<Object>();
            if (p.potions != null) {
                for (AbstractPotion pot : p.potions) {
                    potions.add(potionJson(pot));
                }
            }
            out.put("potions", potions);
            out.put("potion_slots", Integer.valueOf(p.potionSlots));
        } catch (RuntimeException e) {
            out.put("observer_error", String.valueOf(e));
        }
        return out;
    }

    private static int energy(AbstractPlayer p) {
        try {
            return p.energy == null ? 0 : p.energy.energy;
        } catch (RuntimeException e) {
            return 0;
        }
    }

    private static Object potionJson(AbstractPotion pot) {
        if (pot == null || pot.ID == null) {
            return null;
        }
        Map<String, Object> pj = new LinkedHashMap<String, Object>();
        pj.put("id", pot.ID);
        // 空槽的 canUse() 也返回 true，所以要过一层 PotionFacts（见那个类的注释）。
        pj.put("can_use", Boolean.valueOf(PotionFacts.usable(pot.ID, pot.canUse())));
        pj.put("requires_target", Boolean.valueOf(pot.targetRequired));
        pj.put("empty", Boolean.valueOf(PotionFacts.EMPTY_SLOT_ID.equals(pot.ID)));
        pj.put("name", Eng.potionName(pot.ID));
        pj.put("text", Eng.potionDesc(pot.ID));
        return pj;
    }

    private static Map<String, Object> combatJson() {
        AbstractRoom room = AbstractDungeon.getCurrRoom();
        if (room == null || room.monsters == null) {
            return null;
        }
        Map<String, Object> combat = new LinkedHashMap<String, Object>();
        try {
            AbstractPlayer p = AbstractDungeon.player;
            combat.put("turn", Integer.valueOf(GameActionManager.turn));
            combat.put("hand", groupJson(p == null ? null : p.hand));
            combat.put("draw_pile", groupJson(p == null ? null : p.drawPile));
            combat.put("discard_pile", groupJson(p == null ? null : p.discardPile));
            combat.put("exhaust_pile", groupJson(p == null ? null : p.exhaustPile));
            combat.put("monsters", monstersJson());
            combat.put("cards_discarded_this_turn",
                    Integer.valueOf(GameActionManager.totalDiscardedThisTurn));
            combat.put("times_damaged",
                    Integer.valueOf(GameActionManager.damageReceivedThisTurn));
        } catch (RuntimeException e) {
            combat.put("observer_error", String.valueOf(e));
        }
        return combat;
    }

    private static List<Object> monstersJson() {
        List<Object> out = new ArrayList<Object>();
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (room == null || room.monsters == null || room.monsters.monsters == null) {
                return out;
            }
            int i = 0;
            for (AbstractMonster m : room.monsters.monsters) {
                if (m != null) {
                    out.add(monsterJson(m, i));
                }
                i++;
            }
        } catch (RuntimeException e) {
            // 保留已采集的部分
        }
        return out;
    }

    public static Map<String, Object> monsterJson(AbstractMonster m, int index) {
        Map<String, Object> mj = new LinkedHashMap<String, Object>();
        mj.put("index", Integer.valueOf(index));
        mj.put("id", m.id);
        mj.put("name", monsterName(m));
        mj.put("hp", Integer.valueOf(Math.max(0, m.currentHealth)));
        mj.put("max_hp", Integer.valueOf(m.maxHealth));
        mj.put("block", Integer.valueOf(m.currentBlock));
        mj.put("half_dead", Boolean.valueOf(m.halfDead));
        mj.put("is_gone", Boolean.valueOf(m.isDead || m.isEscaping));
        mj.put("powers", powersJson(m.powers));
        mj.put("move_history", bytesToJson(m.moveHistory));
        mj.put("upcoming_moves", upcoming(m));
        Map<String, Object> intent = new LinkedHashMap<String, Object>();
        try {
            intent.put("id", m.intent == null ? "UNKNOWN" : m.intent.name());
            intent.put("hits", Integer.valueOf(intentHits(m)));
            intent.put("base_damage", Integer.valueOf(m.getIntentBaseDmg()));
            intent.put("adjusted_damage", Integer.valueOf(m.getIntentDmg()));
            intent.put("text", intentText(m));
        } catch (RuntimeException e) {
            intent.put("id", "UNKNOWN");
        }
        mj.put("intent", intent);
        return mj;
    }

    /** 英文怪名；eng 资源缺失时退回游戏自带名字。 */
    private static String monsterName(AbstractMonster m) {
        String eng = Eng.monsterName(m.id);
        return eng.isEmpty() ? (m.name == null ? "" : m.name) : eng;
    }

    private static int intentHits(AbstractMonster m) {
        try {
            return m.damage == null ? 0 : m.damage.size();
        } catch (RuntimeException e) {
            return 0;
        }
    }

    /**
     * 意图文本：游戏没有通用的"意图描述"取词接口（每个怪的招式名在各自的
     * MonsterStrings 里），所以这里按**玩家实际看得到的部分**合成英文：意图类别
     * + 结算后伤害。这既是公平的，也是模型真正需要的。
     */
    private static String intentText(AbstractMonster m) {
        try {
            if (m.intent == null) {
                return "";
            }
            StringBuilder sb = new StringBuilder(intentLabel(m.intent.name()));
            int dmg = m.getIntentDmg();
            if (dmg > 0) {
                sb.append(' ').append(dmg);
                int hits = intentHits(m);
                if (hits > 1) {
                    sb.append('x').append(hits);
                }
            }
            return sb.toString();
        } catch (RuntimeException e) {
            return "";
        }
    }

    private static String intentLabel(String intent) {
        if (intent == null) {
            return "Unknown";
        }
        if (intent.startsWith("ATTACK")) {
            String rest = intent.substring("ATTACK".length());
            if (rest.isEmpty()) {
                return "Attack";
            }
            if ("_DEFEND".equals(rest)) {
                return "Attack + Defend";
            }
            if ("_BUFF".equals(rest)) {
                return "Attack + Buff";
            }
            if ("_DEBUFF".equals(rest)) {
                return "Attack + Debuff";
            }
            if ("_HEAL".equals(rest)) {
                return "Attack + Heal";
            }
            return "Attack";
        }
        if (intent.startsWith("DEFEND")) {
            String rest = intent.substring("DEFEND".length());
            if ("_BUFF".equals(rest)) {
                return "Defend + Buff";
            }
            if ("_DEBUFF".equals(rest)) {
                return "Defend + Debuff";
            }
            return "Defend";
        }
        if ("BUFF".equals(intent)) {
            return "Buff";
        }
        if ("DEBUFF".equals(intent) || "STRONG_DEBUFF".equals(intent)) {
            return "Debuff";
        }
        if ("ESCAPE".equals(intent)) {
            return "Escape";
        }
        if ("MAGIC".equals(intent)) {
            return "Magic";
        }
        if ("SLEEP".equals(intent)) {
            return "Sleeping";
        }
        if ("STUN".equals(intent)) {
            return "Stunned";
        }
        if ("NONE".equals(intent)) {
            return "";
        }
        return "Unknown";
    }

    private static List<Object> upcoming(AbstractMonster m) {
        List<Object> out = new ArrayList<Object>();
        try {
            // v1 不枚举敌人完整行动序列（需要每个敌人各自的反编译知识）。
            // 字段留着：公平过滤器会剥掉它，离线全知实验可以填。
            out.add(Integer.valueOf(m.nextMove));
        } catch (RuntimeException e) {
            // 忽略
        }
        return out;
    }

    private static List<Object> bytesToJson(List<Byte> bytes) {
        List<Object> out = new ArrayList<Object>();
        if (bytes == null) {
            return out;
        }
        for (Byte b : bytes) {
            out.add(Integer.valueOf(b == null ? 0 : b.intValue()));
        }
        return out;
    }

    private static List<Object> powersJson(List<AbstractPower> powers) {
        List<Object> out = new ArrayList<Object>();
        if (powers == null) {
            return out;
        }
        for (AbstractPower p : powers) {
            if (p == null) {
                continue;
            }
            Map<String, Object> pj = new LinkedHashMap<String, Object>();
            pj.put("id", p.ID == null ? p.name : p.ID);
            pj.put("amount", Integer.valueOf(p.amount));
            // 人类悬停能看到能力的名字与说明，这是公平信息且对决策很关键
            // （例如 Art of War 的攒能量、Vulnerable 的增伤倍率）。
            String pid = p.ID == null ? p.name : p.ID;
            pj.put("name", Eng.powerName(pid));
            pj.put("text", Eng.powerDesc(pid, p.amount));
            out.add(pj);
        }
        return out;
    }

    public static List<Object> groupJson(CardGroup group) {
        List<Object> out = new ArrayList<Object>();
        if (group == null || group.group == null) {
            return out;
        }
        int i = 0;
        for (AbstractCard c : group.group) {
            if (c != null) {
                out.add(cardJson(c, i));
            }
            i++;
        }
        return out;
    }

    public static Map<String, Object> cardJson(AbstractCard c, int index) {
        Map<String, Object> cj = new LinkedHashMap<String, Object>();
        cj.put("index", Integer.valueOf(index));
        cj.put("id", c.cardID);
        // 名字与描述一律取英文（见 Eng 的注释）：界面语言不该污染 state。
        cj.put("name", cardName(c));
        cj.put("type", c.type == null ? "SKILL" : c.type.name());
        cj.put("cost", Integer.valueOf(c.cost));
        cj.put("cost_for_turn", Integer.valueOf(c.costForTurn));
        cj.put("upgrades", Integer.valueOf(c.timesUpgraded));
        cj.put("rarity", c.rarity == null ? "SPECIAL" : c.rarity.name());
        cj.put("exhausts", Boolean.valueOf(c.exhaust));
        cj.put("ethereal", Boolean.valueOf(c.isEthereal));
        cj.put("is_playable", Boolean.valueOf(isPlayable(c)));
        cj.put("target_type", c.target == null ? "NONE" : c.target.name());
        cj.put("uuid", c.uuid == null ? "" : c.uuid.toString());
        cj.put("text", cardText(c));
        cj.put("damage", Integer.valueOf(c.damage));
        cj.put("base_damage", Integer.valueOf(c.baseDamage));
        cj.put("block", Integer.valueOf(c.block));
        cj.put("base_block", Integer.valueOf(c.baseBlock));
        cj.put("magic_number", Integer.valueOf(c.magicNumber));
        cj.put("base_magic_number", Integer.valueOf(c.baseMagicNumber));
        return cj;
    }

    /** 英文牌名；eng 资源缺失时退回游戏自带名字（至少不是空的）。 */
    private static String cardName(AbstractCard c) {
        String eng = Eng.cardName(c.cardID);
        return eng.isEmpty() ? (c.name == null ? "" : c.name) : eng;
    }

    /**
     * 英文卡面描述。
     *
     * 战斗中的手牌 `damage`/`block` 已被 `applyPowers()` 算好，直接用；
     * 奖励/牌组/商店里的卡没算过（实测是 -1），退回 `baseXxx`。两者都没有
     * 时由 Eng 写成 `?`，不会再把 -1 写进文本。
     */
    private static String cardText(AbstractCard c) {
        String eng = Eng.cardText(
                c.cardID,
                pick(c.damage, c.baseDamage),
                pick(c.block, c.baseBlock),
                pick(c.magicNumber, c.baseMagicNumber));
        return eng.isEmpty() ? description(c) : eng;
    }

    private static int pick(int current, int base) {
        return current >= 0 ? current : base;
    }

    /**
     * 用游戏自己的 `canUse` 判定可出性（与玩家点牌时的判定完全一致）。
     * 单体牌要给一个活着的目标才判得准，所以优先拿第一个活敌人试。
     */
    public static boolean isPlayable(AbstractCard c) {
        try {
            AbstractPlayer p = AbstractDungeon.player;
            if (p == null) {
                return false;
            }
            AbstractMonster target = firstAliveMonster();
            if (c.target == AbstractCard.CardTarget.ENEMY && target == null) {
                return false;
            }
            return c.canUse(p, target);
        } catch (RuntimeException e) {
            return false;
        }
    }

    public static AbstractMonster firstAliveMonster() {
        List<AbstractMonster> alive = aliveMonsters();
        return alive.isEmpty() ? null : alive.get(0);
    }

    public static List<AbstractMonster> aliveMonsters() {
        List<AbstractMonster> out = new ArrayList<AbstractMonster>();
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (room == null || room.monsters == null || room.monsters.monsters == null) {
                return out;
            }
            for (AbstractMonster m : room.monsters.monsters) {
                if (m != null && !m.isDead && !m.isEscaping && m.currentHealth > 0) {
                    out.add(m);
                }
            }
        } catch (RuntimeException e) {
            return out;
        }
        return out;
    }

    /**
     * 按**游戏数组下标**取一个还在场上的敌人。
     *
     * 这个坐标系必须和 {@link #monstersJson} 发出去的 index、以及 {@link #monsterIndexOf}
     * 报出去的人类目标保持一致。它的关键性质是**死亡不移位**：room.monsters.monsters
     * 在整场战斗里只增不减，尸体继续占位，所以杀掉 m0 之后剩下的那个敌人仍然是 m1。
     * 反过来说，任何"先过滤出活着的、再按下标取"的写法都会在第一次击杀后整体错位。
     *
     * @return 越界、还没出生、或已经死亡/逃跑/空血的，一律返回 null
     */
    public static AbstractMonster monsterAt(int index) {
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (room == null || room.monsters == null || room.monsters.monsters == null) {
                return null;
            }
            if (index < 0 || index >= room.monsters.monsters.size()) {
                return null;
            }
            AbstractMonster m = room.monsters.monsters.get(index);
            if (m == null || m.isDead || m.isEscaping || m.currentHealth <= 0) {
                return null;
            }
            return m;
        } catch (RuntimeException e) {
            return null;
        }
    }
    public static int monsterIndexOf(AbstractMonster target) {
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (room == null || room.monsters == null || room.monsters.monsters == null) {
                return -1;
            }
            return room.monsters.monsters.indexOf(target);
        } catch (RuntimeException e) {
            return -1;
        }
    }

    public static int handIndexOf(AbstractCard card) {
        try {
            AbstractPlayer p = AbstractDungeon.player;
            if (p == null || p.hand == null || p.hand.group == null) {
                return -1;
            }
            return p.hand.group.indexOf(card);
        } catch (RuntimeException e) {
            return -1;
        }
    }

    /** 退回路径：用游戏自带（界面语言）的描述，但至少把 `!D!` 之类的数值填对。 */
    public static String description(AbstractCard c) {
        String text = c.rawDescription == null ? "" : c.rawDescription;
        text = text.replace("!D!", num(pick(c.damage, c.baseDamage)));
        text = text.replace("!B!", num(pick(c.block, c.baseBlock)));
        text = text.replace("!M!", num(pick(c.magicNumber, c.baseMagicNumber)));
        text = text.replaceAll("![A-Za-z]+!", "");
        return Eng.clean(text);
    }

    private static String num(int n) {
        return n < 0 ? "?" : String.valueOf(n);
    }

    private static List<Object> safeDeck() {
        try {
            AbstractPlayer p = AbstractDungeon.player;
            return groupJson(p == null ? null : p.masterDeck);
        } catch (RuntimeException e) {
            return new ArrayList<Object>();
        }
    }

    private static Map<String, Object> screenStateJson(String screen) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        try {
            if (SCREEN_CARD_REWARD.equals(screen)) {
                cardRewardState(out);
            } else if (SCREEN_COMBAT_REWARD.equals(screen)) {
                combatRewardState(out);
            } else if (SCREEN_GRID.equals(screen)) {
                gridState(out);
            } else if (SCREEN_EVENT.equals(screen) || SCREEN_NEOW.equals(screen)) {
                eventState(out);
            } else if (SCREEN_SHOP.equals(screen)) {
                shopState(out);
            } else if (SCREEN_REST.equals(screen)) {
                restState(out);
            } else if (SCREEN_BOSS_RELIC.equals(screen)) {
                bossRelicState(out);
            }
        } catch (RuntimeException e) {
            out.put("observer_error", String.valueOf(e));
        }
        return out;
    }

    private static void cardRewardState(Map<String, Object> out) {
        List<Object> cards = new ArrayList<Object>();
        if (AbstractDungeon.cardRewardScreen != null
                && AbstractDungeon.cardRewardScreen.rewardGroup != null) {
            int i = 0;
            for (AbstractCard c : AbstractDungeon.cardRewardScreen.rewardGroup) {
                if (c != null) {
                    cards.add(cardJson(c, i));
                }
                i++;
            }
        }
        out.put("reward_cards", cards);
    }

    private static void combatRewardState(Map<String, Object> out) {
        // 战斗奖励界面：把每条奖励 + "Proceed" 当成一组选项。
        // 选第 i 项 = 领取第 i 条奖励；选最后一项 = 点继续。
        //
        // 卡牌奖励的选项文本必须**带上那三张牌的名字**：跳过一次之后界面会退回
        // 这里，模型只能靠文本判断值不值得再点进去。不给名字的话它会"点进去→
        // 看一眼→不满意→退出→忘了里面是什么→再点进去"，真机上就是这么空转的。
        List<Object> options = new ArrayList<Object>();
        List<Object> optionIds = new ArrayList<Object>();
        List<Object> details = new ArrayList<Object>();
        for (RewardItem r : liveRewards()) {
            options.add(rewardText(r));
            optionIds.add(rewardKind(r));
            details.add(rewardDetail(r));
        }
        options.add("Proceed");
        optionIds.add("proceed");
        out.put("options", options);
        out.put("option_ids", optionIds);
        // `reward_details[i]` 与 `options[i]` 一一对应（最后那个 Proceed 不在里面），
        // 这样 core 不必去解析选项文案就能拿到结构化内容。
        out.put("reward_details", details);
    }

    /** 一条奖励的结构化内容。`cards` 只有卡牌奖励才有。 */
    private static Map<String, Object> rewardDetail(RewardItem r) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("kind", rewardKind(r));
        out.put("text", rewardText(r));
        if (r.type == RewardItem.RewardType.CARD) {
            List<Object> cards = new ArrayList<Object>();
            if (r.cards != null) {
                int i = 0;
                for (AbstractCard c : r.cards) {
                    if (c != null) {
                        cards.add(cardJson(c, i));
                    }
                    i++;
                }
            }
            out.put("cards", cards);
        } else if (r.type == RewardItem.RewardType.RELIC) {
            out.put("id", relicId(r));
        } else if (r.type == RewardItem.RewardType.POTION) {
            out.put("id", r.potion == null ? "" : String.valueOf(r.potion.ID));
        } else {
            out.put("amount", Integer.valueOf(r.goldAmt + r.bonusGold));
        }
        return out;
    }

    /**
     * 战斗奖励列表（**观测与执行的唯一定义点**）：过滤掉 null，顺序即下标。
     *
     * `Observer` 与 `Actor` 都必须用这一份，否则模型选的奖励和实际领的会错位。
     *
     * 读的是**奖励界面自己那份** `rewards`，不是 `room.rewards`：界面在 `open()` 里
     * 把 `room.rewards` 拷了一份（反编译已确认是 `new ArrayList(room.rewards)`），
     * 领取之后只从自己这份里 `remove` —— `room.rewards` 整个界面期间原封不动。
     * 读错那份的后果真机实测过：同一瓶药水被反复列出来、反复领取，三个槽全填满同一瓶，
     * 而且永远等不到"只剩 Proceed"。认不出的界面才退回 `room.rewards`。
     */
    public static List<RewardItem> liveRewards() {
        List<RewardItem> out = new ArrayList<RewardItem>();
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            List<RewardItem> source = null;
            if (SCREEN_COMBAT_REWARD.equals(screenName()) && AbstractDungeon.combatRewardScreen != null) {
                source = AbstractDungeon.combatRewardScreen.rewards;
            }
            if (source == null && room != null) {
                source = room.rewards;
            }
            if (source == null) {
                return out;
            }
            for (RewardItem r : source) {
                if (r != null) {
                    out.add(r);
                }
            }
        } catch (RuntimeException e) {
            // 观测降级：返回已收集的部分
        }
        return out;
    }

    /**
     * 卡牌奖励候选（**观测与执行的唯一定义点**）。
     *
     * 过滤掉 null，顺序即下标；`Actor.cardReward` 必须用同一份，
     * 否则模型选的牌与实际拿到的会错位。
     */
    public static List<AbstractCard> cardRewardCards() {
        List<AbstractCard> out = new ArrayList<AbstractCard>();
        try {
            if (AbstractDungeon.cardRewardScreen != null
                    && AbstractDungeon.cardRewardScreen.rewardGroup != null) {
                for (AbstractCard c : AbstractDungeon.cardRewardScreen.rewardGroup) {
                    if (c != null) {
                        out.add(c);
                    }
                }
            }
        } catch (RuntimeException e) {
            // 观测降级：返回已收集的部分
        }
        return out;
    }

    /**
     * Boss / 宝箱遗物备选（**观测与执行的唯一定义点**）。
     */
    public static List<AbstractRelic> relicChoices() {
        List<AbstractRelic> out = new ArrayList<AbstractRelic>();
        try {
            if (AbstractDungeon.bossRelicScreen != null
                    && AbstractDungeon.bossRelicScreen.relics != null) {
                for (AbstractRelic r : AbstractDungeon.bossRelicScreen.relics) {
                    if (r != null) {
                        out.add(r);
                    }
                }
            }
        } catch (RuntimeException e) {
            // 观测降级：返回已收集的部分
        }
        return out;
    }

    private static String rewardText(RewardItem r) {
        if (r.type == null) {
            return "Reward";
        }
        switch (r.type) {
            case GOLD:
                return "Gold (" + r.goldAmt + ")";
            case STOLEN_GOLD:
                return "Stolen gold (" + r.goldAmt + ")";
            case RELIC:
                return "Relic: " + relicName(r);
            case POTION:
                return "Potion: " + potionName(r);
            case CARD:
                return "Card reward (" + cardRewardNames(r) + ")";
            case EMERALD_KEY:
                return "Emerald key";
            case SAPPHIRE_KEY:
                return "Sapphire key";
            default:
                return r.type.name();
        }
    }

    /** 卡牌奖励的三张牌名（英文），空列表时给 `?`，绝不返回空串。 */
    private static String cardRewardNames(RewardItem r) {
        if (r.cards == null || r.cards.isEmpty()) {
            return "?";
        }
        StringBuilder sb = new StringBuilder();
        for (AbstractCard c : r.cards) {
            if (c == null) {
                continue;
            }
            if (sb.length() > 0) {
                sb.append(" / ");
            }
            String name = Eng.cardName(c.cardID);
            sb.append(name.isEmpty() ? c.cardID : name);
        }
        return sb.length() == 0 ? "?" : sb.toString();
    }

    /** 遗物的英文显示名（`relicId` 是资源键，界面语言无关）。 */
    private static String relicName(RewardItem r) {
        String id = relicId(r);
        if (id.isEmpty()) {
            return "?";
        }
        String name = Eng.relicName(id);
        return name.isEmpty() ? id : name;
    }

    private static String relicId(RewardItem r) {
        return r.relic == null || r.relic.relicId == null ? "" : r.relic.relicId;
    }

    private static String potionName(RewardItem r) {
        String id = r.potion == null || r.potion.ID == null ? "" : r.potion.ID;
        if (id.isEmpty()) {
            return "?";
        }
        String name = Eng.potionName(id);
        return name.isEmpty() ? id : name;
    }

    private static String rewardKind(RewardItem r) {
        return r.type == null ? "reward" : r.type.name().toLowerCase();
    }

    // ----- 选牌界面（GRID / HAND_SELECT） -----

    /**
     * 选牌界面统一成"每次选一张"。
     *
     * `select_cards` 的每一项带 `zone`，下标是它在列表里的**位置**；执行侧用
     * 同一个下标回查目标牌（Actor.resolveSelection），所以两边的顺序必须一致。
     * `min_select/max_select` 让 core 的 `decision.grid_kind` 自然判出
     * must_k（min==max>0）还是 any（min=0）。
     */
    private static void gridState(Map<String, Object> out) {
        if (AbstractDungeon.screen == AbstractDungeon.CurrentScreen.HAND_SELECT) {
            HandCardSelectScreen hs = AbstractDungeon.handCardSelectScreen;
            if (hs == null) {
                return;
            }
            CardGroup hand = (CardGroup) Reflect.get(hs, HandCardSelectScreen.class, "hand");
            List<Object> cards = new ArrayList<Object>();
            if (hand != null && hand.group != null) {
                int i = 0;
                for (AbstractCard c : hand.group) {
                    if (c != null) {
                        cards.add(zonedCardJson(c, i, ZONE_HAND));
                    }
                    i++;
                }
            }
            out.put("select_cards", cards);
            boolean any = hs.canPickZero || hs.upTo;
            int need = Math.max(0, hs.numCardsToSelect);
            out.put("min_select", Integer.valueOf(any ? 0 : need));
            out.put("max_select", Integer.valueOf(any ? cards.size() : need));
            out.put("reason", englishText(hs.selectionReason));
            out.put("origin", "hand_select");
            addEventContext(out);
            return;
        }

        GridCardSelectScreen gs = AbstractDungeon.gridSelectScreen;
        if (gs == null) {
            return;
        }
        List<Object> cards = new ArrayList<Object>();
        String zone = zoneOf(gs.targetGroup);
        if (gs.targetGroup != null && gs.targetGroup.group != null) {
            int i = 0;
            for (AbstractCard c : gs.targetGroup.group) {
                if (c != null) {
                    cards.add(zonedCardJson(c, i, zone));
                }
                i++;
            }
        }
        out.put("select_cards", cards);
        Integer num = (Integer) Reflect.get(gs, GridCardSelectScreen.class, "numCards");
        int need = num == null ? 1 : Math.max(0, num.intValue());
        // `isJustForConfirming` 是"给你看一组牌，点确认就行"（游戏不读 selectedCards，
        // 而是自己遍历 targetGroup 发牌），此时 numCards 是脏值，必须报成 0/0，
        // 这样 core 会裁出"选 0 张"，执行侧也走"直接确认"分支。
        int min = gs.isJustForConfirming ? 0 : (gs.anyNumber ? 0 : need);
        int max = gs.isJustForConfirming ? 0 : (gs.anyNumber ? cards.size() : need);
        out.put("min_select", Integer.valueOf(min));
        out.put("max_select", Integer.valueOf(max));
        Object tip = Reflect.get(gs, GridCardSelectScreen.class, "tipMsg");
        out.put("reason", englishText(tip instanceof String ? (String) tip : null));
        out.put("origin", gridOrigin(gs));
        addEventContext(out);
    }

    /**
     * 选牌界面的来由。**先按房间判，再按界面自己的标志位判** —— 因为
     * `forUpgrade` / `forPurge` 在"锻造"和"升级神龛"里是同一组取值，
     * 只有房间类型能区分它们。
     */
    private static String gridOrigin(GridCardSelectScreen gs) {
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (room instanceof RestRoom) {
                return "rest_smith";
            }
            if (room != null && room.event != null) {
                return "event";
            }
            if (gs.forTransform) {
                return "transform";
            }
            if (gs.forPurge) {
                return "purge";
            }
            if (gs.forUpgrade) {
                return "upgrade";
            }
            if (gs.isJustForConfirming) {
                return "confirm";
            }
            return "select";
        } catch (RuntimeException e) {
            return "select";
        }
    }

    /**
     * 事件房的上下文：事件英文名 + 开场正文。
     *
     * 选牌是**有状态**的：同一块选牌界面对应"升级一张牌"/"删掉一张牌"/
     * "变形一张牌"三种完全不同的题目。只告诉模型"选一张牌"等于什么都没说，
     * 它没法判断该选哪张（升级想升核心牌、删牌想删废牌、变形想变掉诅咒）。
     * 所以事件名与开场正文必须一起进 state —— 这两样都是玩家进入事件时就
     * 看得到的公平信息。
     */
    private static void addEventContext(Map<String, Object> out) {
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            AbstractEvent event = room == null ? null : room.event;
            if (event == null) {
                return;
            }
            String simple = event.getClass().getSimpleName();
            String name = Eng.eventName(simple);
            if (!name.isEmpty()) {
                out.put("event_name", name);
            }
            String intro = Eng.eventIntro(simple);
            if (!intro.isEmpty()) {
                out.put("event_text", intro);
            }
        } catch (RuntimeException e) {
            // 观测降级：事件上下文拿不到就算了
        }
    }

    /** 界面语言文本 -> 英文。查不到就原样返回（绝不返回 null）。 */
    public static String englishText(String text) {
        if (text == null || text.isEmpty()) {
            return "";
        }
        String en = Eng.localizedEnglish(text);
        return en == null ? text : en;
    }

    /** 带 `zone` 的牌（core 的 ZonedCard 要 zone；cardJson 已带 index）。 */
    private static Map<String, Object> zonedCardJson(AbstractCard c, int index, String zone) {
        Map<String, Object> cj = cardJson(c, index);
        cj.put("zone", zone);
        return cj;
    }

    /** 反查一个 CardGroup 属于哪个区域（用引用比较，绝不用内容比较）。 */
    private static String zoneOf(CardGroup group) {
        AbstractPlayer p = AbstractDungeon.player;
        if (group == null || p == null) {
            return ZONE_HAND;
        }
        if (group == p.hand) {
            return ZONE_HAND;
        }
        if (group == p.drawPile) {
            return ZONE_DRAW;
        }
        if (group == p.discardPile) {
            return ZONE_DISCARD;
        }
        if (group == p.exhaustPile) {
            return ZONE_EXHAUST;
        }
        if (group == p.masterDeck) {
            return ZONE_DECK;
        }
        return ZONE_HAND;
    }
    private static final String ZONE_HAND = "hand";
    private static final String ZONE_DRAW = "draw";
    private static final String ZONE_DISCARD = "discard";
    private static final String ZONE_EXHAUST = "exhaust";
    private static final String ZONE_DECK = "deck";

    // ----- 事件 / Neow -----

    /**
     * 事件与 Neow 都走"一组按钮"。选项顺序**原样保留**（下标就是执行侧用的
     * 下标），被禁用的选项加 `[disabled] ` 前缀，让模型能看见但不会选。
     */
    private static void eventState(Map<String, Object> out) {
        List<LargeDialogOptionButton> buttons = optionButtons();
        // 界面按钮文本是**界面语言**的（中文客户端就是中文），这里尽量换成英文。
        List<String> eng = englishOptions(buttons);
        List<Object> options = new ArrayList<Object>();
        for (int i = 0; i < buttons.size(); i++) {
            LargeDialogOptionButton b = buttons.get(i);
            String msg = i < eng.size() ? eng.get(i) : (b.msg == null ? "" : b.msg);
            options.add(b.isDisabled ? "[disabled] " + msg : msg);
        }
        out.put("options", options);
        if (AbstractDungeon.getCurrRoom() instanceof NeowRoom) {
            out.put("neow_options", options);
        }
        addEventContext(out);
    }

    /**
     * 事件选项的英文文本，与 `buttons` **逐条对齐**（长度恒等于按钮数）。
     *
     * 两条路，按可靠性排序：
     *   1. 事件资源 `OPTIONS` 按**位置**取英文 —— 事件类简单名查 `events.json`
     *      （键是显示名，`Eng` 内部归一化匹配）。条数对得上就用它，精确且不会
     *      受运行时数值影响；
     *   2. 位置对不上（事件改过选项、或不在资源里）时按"中文原文 → 英文"逐条
     *      反查（`Eng.localizedEnglish`）。
     * 两条都不行就原样返回界面语言 —— 宁可中文，也不把选项和文本错位。
     */
    private static List<String> englishOptions(List<LargeDialogOptionButton> buttons) {
        List<String> out = new ArrayList<String>();
        if (buttons == null || buttons.isEmpty()) {
            return out;
        }
        List<String> positional = positionalEventOptions(buttons);
        for (int i = 0; i < buttons.size(); i++) {
            LargeDialogOptionButton b = buttons.get(i);
            String raw = b.msg == null ? "" : b.msg;
            String en = positional != null && i < positional.size() ? positional.get(i) : null;
            if (en == null) {
                en = Eng.localizedEnglish(raw);
            }
            out.add(en == null ? raw : Eng.clean(en));
        }
        return out;
    }

    private static List<String> positionalEventOptions(List<LargeDialogOptionButton> buttons) {
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            AbstractEvent event = room == null ? null : room.event;
            if (event == null) {
                return null;
            }
            List<String> list = Eng.eventOptions(event.getClass().getSimpleName());
            return list.size() == buttons.size() ? list : null;
        } catch (RuntimeException e) {
            return null;
        }
    }

    /**
     * 当前事件/Neow 的按钮列表（**观测与执行的唯一定义点**）。
     *
     * 游戏有两种对话框（`imageEventText` / `roomEventText`），同一时刻只有一种
     * 有按钮。执行侧用同一个下标点按钮，所以这里的挑选顺序不能被别处复制。
     */
    public static List<LargeDialogOptionButton> optionButtons() {
        List<LargeDialogOptionButton> out = new ArrayList<LargeDialogOptionButton>();
        try {
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            AbstractEvent event = room == null ? null : room.event;
            if (event == null) {
                return out;
            }
            addButtons(event.imageEventText == null ? null : event.imageEventText.optionList, out);
            if (out.isEmpty()) {
                addButtons(event.roomEventText == null ? null : event.roomEventText.optionList, out);
            }
        } catch (RuntimeException e) {
            // 观测降级：返回已收集的部分
        }
        return out;
    }

    private static void addButtons(List<LargeDialogOptionButton> buttons,
                                   List<LargeDialogOptionButton> out) {
        if (buttons == null) {
            return;
        }
        for (LargeDialogOptionButton b : buttons) {
            if (b != null) {
                out.add(b);
            }
        }
    }

    private static void collectOptions(List<LargeDialogOptionButton> buttons, List<Object> out) {
        if (buttons == null) {
            return;
        }
        for (LargeDialogOptionButton b : buttons) {
            if (b == null) {
                continue;
            }
            String msg = b.msg == null ? "" : b.msg;
            out.add(b.isDisabled ? "[disabled] " + msg : msg);
        }
    }

    // ----- 商店 -----

    /** 商店货架取自 `ShopSlots`（观测与执行的唯一定义点）。 */
    private static void shopState(Map<String, Object> out) {
        List<ShopSlots.Slot> slots = ShopSlots.list();
        List<Object> items = new ArrayList<Object>();
        int gold = playerGold();
        int i = 0;
        for (ShopSlots.Slot s : slots) {
            Map<String, Object> it = new LinkedHashMap<String, Object>();
            it.put("index", Integer.valueOf(i));
            it.put("kind", s.kind);
            it.put("id", s.id);
            it.put("price", Integer.valueOf(s.price));
            it.put("affordable", Boolean.valueOf(s.price <= gold));
            // 人类在商店里会读每件商品的名字与效果，这里补上（原来只有 id）。
            it.put("name", shopName(s));
            it.put("text", shopText(s));
            items.add(it);
            i++;
        }
        out.put("shop_items", items);
    }

    private static String shopName(ShopSlots.Slot s) {
        if (ShopSlots.KIND_RELIC.equals(s.kind)) {
            return Eng.relicName(s.id);
        }
        if (ShopSlots.KIND_POTION.equals(s.kind)) {
            return Eng.potionName(s.id);
        }
        if (ShopSlots.KIND_PURGE.equals(s.kind)) {
            return "Card Removal";
        }
        return Eng.cardName(s.id);
    }

    private static String shopText(ShopSlots.Slot s) {
        try {
            if (ShopSlots.KIND_RELIC.equals(s.kind)) {
                return Eng.relicDesc(s.id);
            }
            if (ShopSlots.KIND_POTION.equals(s.kind)) {
                return Eng.potionDesc(s.id);
            }
            if (s.payload instanceof AbstractCard) {
                return cardText((AbstractCard) s.payload);
            }
        } catch (RuntimeException e) {
            return "";
        }
        return "";
    }

    private static int playerGold() {
        try {
            AbstractPlayer p = AbstractDungeon.player;
            return p == null ? 0 : p.gold;
        } catch (RuntimeException e) {
            return 0;
        }
    }

    // ----- 篝火 -----

    /** 篝火选项取自 `CampfireSlots`；`rest_options` 是按钮文本（界面语言）。 */
    private static void restState(Map<String, Object> out) {
        List<Object> names = new ArrayList<Object>();
        List<Object> labels = new ArrayList<Object>();
        for (CampfireSlots.Slot s : CampfireSlots.list()) {
            names.add(s.name);
            labels.add(campfireLabel(s.name));
        }
        out.put("options", labels);
        out.put("option_ids", names);
        out.put("rest_options", labels);
    }

    private static String campfireLabel(String name) {
        if ("Rest".equals(name)) {
            return "Rest (heal)";
        }
        if ("Smith".equals(name)) {
            return "Smith (upgrade a card)";
        }
        if ("Lift".equals(name)) {
            return "Lift (Girya)";
        }
        if ("Toke".equals(name)) {
            return "Toke (remove a card)";
        }
        if ("Dig".equals(name)) {
            return "Dig (Shovel)";
        }
        if ("Recall".equals(name)) {
            return "Recall (Ruby Key)";
        }
        return name;
    }

    // ----- Boss 遗物 -----

    private static void bossRelicState(Map<String, Object> out) {
        List<Object> relics = new ArrayList<Object>();
        BossRelicSelectScreen bs = AbstractDungeon.bossRelicScreen;
        if (bs != null && bs.relics != null) {
            for (AbstractRelic r : bs.relics) {
                if (r == null) {
                    continue;
                }
                Map<String, Object> rj = new LinkedHashMap<String, Object>();
                rj.put("id", r.relicId);
                rj.put("counter", Integer.valueOf(r.counter));
                relics.add(rj);
            }
        }
        out.put("reward_relics", relics);
    }
    // ----- 地图 -----

    /**
     * 整幕地图。地图在游戏内本就全可见，所以这里不违反公平性。
     * 节点 id 与 core 的 `ids.node_id` 一致：`n<x>_<y>`。
     */
    private static Map<String, Object> mapJson() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        try {
            ArrayList<ArrayList<MapRoomNode>> rows = AbstractDungeon.map;
            if (rows == null || rows.isEmpty()) {
                return null;
            }
            List<Object> nodes = new ArrayList<Object>();
            MapRoomNode boss = null;
            for (List<MapRoomNode> row : rows) {
                if (row == null) {
                    continue;
                }
                for (MapRoomNode n : row) {
                    if (n == null) {
                        continue;
                    }
                    Map<String, Object> nj = new LinkedHashMap<String, Object>();
                    nj.put("id", nodeId(n.x, n.y));
                    nj.put("x", Integer.valueOf(n.x));
                    nj.put("y", Integer.valueOf(n.y));
                    nj.put("type", nodeType(n));
                    List<Object> children = new ArrayList<Object>();
                    for (MapRoomNode child : childNodes(n)) {
                        children.add(nodeId(child.x, child.y));
                    }
                    nj.put("children", children);
                    nodes.add(nj);
                    if (boss == null || n.y > boss.y) {
                        boss = n;
                    }
                }
            }
            MapRoomNode cur = AbstractDungeon.currMapNode;
            out.put("act", Integer.valueOf(AbstractDungeon.actNum));
            out.put("nodes", nodes);
            out.put("current", cur == null || cur.y < 0 ? null : nodeId(cur.x, cur.y));
            out.put("reachable", reachableNodeIds());
            out.put("boss", boss == null ? null : nodeId(boss.x, boss.y));
            out.put("boss_relic_taken", Boolean.valueOf(bossRelicTaken()));
        } catch (RuntimeException e) {
            return null;
        }
        return out;
    }

    /** 地图节点 id（**观测与执行的唯一定义点**）：`n<x>_<y>`。 */
    public static String nodeId(int x, int y) {
        return "n" + x + "_" + y;
    }

    private static String nodeType(MapRoomNode n) {
        return n == null ? "UNKNOWN" : typeOfRoom(n.room);
    }

    private static String typeOfRoom(AbstractRoom room) {
        if (room instanceof MonsterRoomBoss) {
            return "BOSS";
        }
        if (room instanceof MonsterRoomElite) {
            return "ELITE";
        }
        if (room instanceof MonsterRoom) {
            return "MONSTER";
        }
        if (room instanceof EventRoom) {
            return "EVENT";
        }
        if (room instanceof RestRoom) {
            return "REST";
        }
        if (room instanceof ShopRoom) {
            return "SHOP";
        }
        if (room instanceof TreasureRoom) {
            return "TREASURE";
        }
        if (room instanceof VictoryRoom) {
            return "BOSS";
        }
        return "UNKNOWN";
    }

    private static List<MapRoomNode> childNodes(MapRoomNode n) {
        List<MapRoomNode> out = new ArrayList<MapRoomNode>();
        ArrayList<ArrayList<MapRoomNode>> rows = AbstractDungeon.map;
        if (n == null || rows == null) {
            return out;
        }
        List<MapEdge> edges;
        try {
            edges = n.getEdges();
        } catch (RuntimeException e) {
            return out;
        }
        if (edges == null) {
            return out;
        }
        for (MapEdge e : edges) {
            if (e == null || e.dstY < 0 || e.dstY >= rows.size()) {
                continue;
            }
            List<MapRoomNode> row = rows.get(e.dstY);
            if (row == null || e.dstX < 0 || e.dstX >= row.size()) {
                continue;
            }
            MapRoomNode dst = row.get(e.dstX);
            if (dst != null) {
                out.add(dst);
            }
        }
        return out;
    }

    /**
     * 当前可走的节点。幕开始时游戏把 `currMapNode` 设成 `(0, -1)` 的哑节点，
     * 这时可走的是最下一排**有连边**的节点（见 AbstractDungeon 的反编译）。
     */
    /**
     * 当前可走的节点 id（**观测与执行的唯一定义点**）：`Actor` 校验
     * `select_map_node` 时用的就是这一份。
     */
    public static List<String> reachableNodeIds() {
        List<String> out = new ArrayList<String>();
        MapRoomNode cur = AbstractDungeon.currMapNode;
        if (cur == null || cur.y < 0) {
            ArrayList<ArrayList<MapRoomNode>> rows = AbstractDungeon.map;
            if (rows == null || rows.isEmpty()) {
                return out;
            }
            List<MapRoomNode> bottom = rows.get(0);
            if (bottom == null) {
                return out;
            }
            for (MapRoomNode n : bottom) {
                if (n != null && hasEdges(n)) {
                    out.add(nodeId(n.x, n.y));
                }
            }
            return out;
        }
        for (MapRoomNode n : childNodes(cur)) {
            out.add(nodeId(n.x, n.y));
        }
        return out;
    }

    private static boolean hasEdges(MapRoomNode n) {
        try {
            return n.hasEdges();
        } catch (RuntimeException e) {
            return false;
        }
    }

    /** 是否已拿过 Boss 遗物：主牌组里出现 BOSS 档遗物即为真。 */
    private static boolean bossRelicTaken() {
        try {
            AbstractPlayer p = AbstractDungeon.player;
            if (p == null || p.relics == null) {
                return false;
            }
            for (AbstractRelic r : p.relics) {
                if (r != null && r.tier != null && "BOSS".equals(r.tier.name())) {
                    return true;
                }
            }
            return false;
        } catch (RuntimeException e) {
            return false;
        }
    }
    // ----- 稳定性签名 -----

    /**
     * 观测签名：只包含**影响决策的量**（不含动画、坐标、计时器、颜色）。
     *
     * `StabilityGate` 用它判断"这是不是同一个局面"：签名相同就不重复发
     * observation。宁可多算一点，也不能漏掉任何会改变候选集的量。
     */
    public static String signature() {
        StringBuilder sb = new StringBuilder(320);
        try {
            sb.append(screenName()).append('~');
            sb.append(AbstractDungeon.actNum).append('.').append(AbstractDungeon.floorNum).append('~');
            MapRoomNode node = AbstractDungeon.currMapNode;
            sb.append(node == null ? "-" : nodeId(node.x, node.y)).append('~');
            AbstractPlayer p = AbstractDungeon.player;
            if (p != null) {
                sb.append(p.currentHealth).append('/').append(p.maxHealth).append('/')
                        .append(p.currentBlock).append('/').append(p.gold).append('/')
                        .append(energy(p)).append('~');
                if (p.hand != null && p.hand.group != null) {
                    for (AbstractCard c : p.hand.group) {
                        if (c == null) {
                            continue;
                        }
                        sb.append(c.cardID).append(':').append(c.costForTurn).append(':')
                                .append(isPlayable(c) ? 1 : 0).append(',');
                    }
                }
                sb.append('~');
                if (p.potions != null) {
                    for (AbstractPotion pot : p.potions) {
                        sb.append(pot == null ? "-" : pot.ID).append(',');
                    }
                }
                sb.append('~');
            }
            if (AbstractDungeon.getCurrRoom() != null
                    && AbstractDungeon.getCurrRoom().monsters != null
                    && AbstractDungeon.getCurrRoom().monsters.monsters != null) {
                for (AbstractMonster m : AbstractDungeon.getCurrRoom().monsters.monsters) {
                    if (m == null) {
                        continue;
                    }
                    sb.append(m.id).append(':').append(m.currentHealth).append(':')
                            .append(m.currentBlock).append(':')
                            .append(m.getIntentDmg()).append(':')
                            .append(m.isDead || m.isEscaping ? 1 : 0).append(',');
                }
            }
            sb.append('~').append(GameActionManager.turn).append('~');
            sb.append(zoneCount(p == null ? null : p.drawPile)).append('/')
                    .append(zoneCount(p == null ? null : p.discardPile)).append('/')
                    .append(zoneCount(p == null ? null : p.exhaustPile)).append('/')
                    .append(zoneCount(p == null ? null : p.masterDeck)).append('~');
            Map<String, Object> ss = screenStateJson(screenName());
            sb.append(ss.get("min_select")).append('/').append(ss.get("max_select")).append('/');
            sb.append(size(ss.get("options"))).append('/')
                    .append(size(ss.get("select_cards"))).append('/')
                    .append(size(ss.get("reward_cards"))).append('/')
                    .append(size(ss.get("reward_relics"))).append('/')
                    .append(size(ss.get("shop_items")));
        } catch (RuntimeException e) {
            sb.append("~err");
        }
        return sb.toString();
    }

    private static int zoneCount(CardGroup group) {
        if (group == null || group.group == null) {
            return 0;
        }
        return group.group.size();
    }

    private static int size(Object list) {
        return list instanceof List ? ((List<?>) list).size() : 0;
    }
}

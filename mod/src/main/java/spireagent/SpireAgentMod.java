package spireagent;

import java.io.IOException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.LinkedBlockingQueue;

import com.evacipated.cardcrawl.modthespire.lib.SpireConfig;
import com.evacipated.cardcrawl.modthespire.lib.SpireInitializer;

import com.megacrit.cardcrawl.cards.AbstractCard;
import com.megacrit.cardcrawl.actions.GameActionManager;
import com.megacrit.cardcrawl.characters.AbstractPlayer;
import com.megacrit.cardcrawl.core.CardCrawlGame;
import com.megacrit.cardcrawl.dungeons.AbstractDungeon;
import com.megacrit.cardcrawl.potions.AbstractPotion;
import com.megacrit.cardcrawl.rooms.AbstractRoom;
import com.megacrit.cardcrawl.rooms.RestRoom;
import com.megacrit.cardcrawl.rooms.CampfireUI;

import basemod.BaseMod;
import basemod.interfaces.OnCardUseSubscriber;
import basemod.interfaces.PostInitializeSubscriber;
import basemod.interfaces.PostPotionUseSubscriber;
import basemod.interfaces.PostUpdateSubscriber;
import basemod.interfaces.StartGameSubscriber;

import spireagent.act.Actor;
import spireagent.act.GameActionContext;
import spireagent.act.HumanActionTap;
import spireagent.bridge.Envelope;
import spireagent.bridge.Configure;
import spireagent.bridge.Json;
import spireagent.bridge.NdjsonServer;
import spireagent.obs.EchoGate;
import spireagent.obs.Observer;
import spireagent.obs.StabilityGate;
import spireagent.proto.ActionSpec;
import spireagent.proto.Errors;

/**
 * 模组入口（见 docs/02-architecture.md、docs/03-mod-protocol.md）。
 *
 * 每帧只做三件事，顺序不能变：
 * 1. 看门狗：agent 连着却迟迟不发动作 -> 执行安全默认动作（游戏不卡死）；
 * 2. 回声门 + 去抖门：只有"轮到 agent 做决定"的新局面才发观测；
 * 3. 发观测并重新武装看门狗。
 *
 * **模组没有自己的模式。** `mode` 与 `watchdog_sec` 的唯一来源是 agent 在握手后
 * 发来的 `configure` 帧；在那之前模组是哑的（不发观测、不接管、不报人类动作）。
 * SpireConfig 里只留 `host` / `port` —— 监听地址有先后依赖，推不了。
 *
 * 线程约定：**所有游戏状态只在游戏主线程碰**。`NdjsonServer.Handler` 的回调在
 * 网络线程，它只负责解析帧并塞进 `inbox`（`ping` 例外，就地回）；真正的
 * `configure` / 动作执行都在 `receivePostUpdate` 里按帧排干（见 drainInbox）。
 *
 * 早先动作是在网络线程上直接执行的，等于一边跑游戏 update 一边改游戏对象 ——
 * 真机上崩过一次（`GameActionManager.getNextAction` 读到 null 的怪物组）。
 * 跨线程的可变状态现在只剩 `inbox`（并发队列）与 `seq` / `lastEmittedSeq` /
 * `configured` / `observeHuman` 这几个 volatile 计数器。
 */
@SpireInitializer
public final class SpireAgentMod implements
        PostInitializeSubscriber,
        PostUpdateSubscriber,
        StartGameSubscriber,
        OnCardUseSubscriber,
        PostPotionUseSubscriber {

    public static final String MOD_ID = "spireagent";
    public static final String MOD_VERSION = "0.1.0";

    private static final String CONFIG_FILE = "SlayaTheSpire";
    private static final String DEFAULT_HOST = "127.0.0.1";
    private static final int DEFAULT_PORT = 17777;
    private static final int MAX_FRAME_BYTES = 4 * 1024 * 1024;
    /** 一帧最多落几个控制帧（agent 是串行的，正常只有 1 个）。 */
    private static final int MAX_ACTIONS_PER_FRAME = 4;
    private static final long ECHO_FALLBACK_MILLIS = 1500L;
    private static final int STABLE_FRAMES = 6;
    private static final long STABLE_MILLIS = 250L;
    /**
     * `observe_human` 用更松的去抖阈值。人类会在一屏里连点：出牌 -> 再出牌 ->
     * 结束回合，中间那几个"轮到他操作"的局面才是要记的样本；用 250ms 的门槛
     * 实测会把它们整个吞掉（真机上 f1 第一轮只留下 1 个观测）。
     */
    private static final int OBSERVE_STABLE_FRAMES = 2;
    private static final long OBSERVE_STABLE_MILLIS = 60L;

    private static SpireAgentMod instance;

    private final StabilityGate gates = new StabilityGate(STABLE_FRAMES, STABLE_MILLIS);
    private final EchoGate echo = new EchoGate(ECHO_FALLBACK_MILLIS);
    private final Watchdog watchdog;
    private final NdjsonServer server;
    /** 网络线程 -> 游戏主线程。见类注释的线程约定。 */
    private final BlockingQueue<Envelope> inbox = new LinkedBlockingQueue<Envelope>();

    /** 收到 `configure` 之前，模组不观测、不动作、不报人类动作。 */
    private volatile boolean configured;
    private volatile boolean observeHuman;
    /** `endTurnQueued` 的上升沿闩锁（见 {@link #pollEndTurn()}）。 */
    private boolean endTurnSeen;

    private volatile long seq;
    private volatile long lastEmittedSeq = -1L;
    private long nextOutboundId;
    private int watchdogEvents;

    /** MTS 的入口：启动器扫描到 `@SpireInitializer` 后会调用这个静态方法。 */
    public static void initialize() {
        instance = new SpireAgentMod();
        BaseMod.subscribe(instance);
        Log.info("SlayaTheSpire agent mod " + MOD_VERSION
                + " loaded (protocol " + Envelope.PROTOCOL + ", awaiting configure)");
    }

    public static SpireAgentMod instance() {
        return instance;
    }

    private SpireAgentMod() {
        String host = DEFAULT_HOST;
        int port = DEFAULT_PORT;
        try {
            SpireConfig cfg = new SpireConfig(MOD_ID, CONFIG_FILE);
            if (cfg.has("host")) {
                host = cfg.getString("host");
            }
            if (cfg.has("port")) {
                port = cfg.getInt("port");
            }
        } catch (IOException e) {
            Log.warn("config unreadable, using defaults: " + e);
        } catch (RuntimeException e) {
            Log.warn("config unreadable, using defaults: " + e);
        }
        this.watchdog = new Watchdog(Configure.DEFAULT_WATCHDOG_SEC);
        this.server = new NdjsonServer(host, port, MAX_FRAME_BYTES, new Handler());
    }

    // ------------------------------------------------------------- BaseMod

    public void receivePostInitialize() {
        HumanActionTap.setSink(new HumanActionTap.Sink() {
            public void onHumanAction(Map<String, Object> action) {
                // 只在 observe_human 下上报：patch 是无条件挂着的，agent 自己执行
                // 动作时也会穿过那些前缀（例如 Actor 调 `skippedCards()`），
                // 不加这道门就会把自己的动作当"人类动作"发回去。
                if (configured && observeHuman) {
                    sendHumanAction(action);
                }
            }
        });
        try {
            server.start();
        } catch (IOException e) {
            Log.error("cannot bind bridge port", e);
        }
    }

    public void receiveStartGame() {
        seq = 0L;
        lastEmittedSeq = -1L;
        gates.reset();
        echo.clear();
        watchdog.disarm();
        HumanActionTap.resetLatches();
        SlDetector.reset();
        Log.info("new run started; counters reset");
    }

    public void receivePostUpdate() {
        long now = System.nanoTime();
        try {
            // 顺序要紧：先落控制帧（`configure` 必须在这里生效），再判连接状态，
            // 否则"刚连上还没 configure"的早退会把 configure 帧永远压在队列里。
            drainInbox();
            guardStaleTurnHasEnded();
            HumanActionTap.pollCardRewardLatch();
            HumanActionTap.pollGridCommitLatch();
            if (!server.connected() || !configured) {
                // 没连 agent（或还没 configure）就不接管：
                // 人类照常玩，游戏不会被看门狗抢走控制权，也不会发观测。
                watchdog.disarm();
                return;
            }
            pollEndTurn();
            if (watchdog.armed() && watchdog.expired(now)) {
                fireWatchdog(now);
            }
            String signature = Observer.signature();
            if (!echo.allow(signature, now)) {
                return;
            }
            if (echo.forced()) {
                gates.invalidate();
            }
            if (!gates.shouldEmit(Observer.stable(), signature, now)) {
                return;
            }
            emitObservation(now);

            GameActionManager am = AbstractDungeon.actionManager;
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            CampfireUI ui = room instanceof RestRoom
                    ? ((RestRoom) room).campfireUI
                    : null;

            if (room instanceof RestRoom) {
                Log.info("[DBG-CAMPFIRE-FRAME]"
                        + " screenName=" + Observer.screenName()
                        + " dungeonScreen=" + AbstractDungeon.screen
                        + " phase=" + (room == null ? null : room.phase)
                        + " selected=" + (ui == null ? null : ui.somethingSelected)
                        + " hidden=" + CampfireUI.hidden
                        + " hideTimer=" + (ui == null ? null
                                : Reflect.get(ui, CampfireUI.class, "hideStuffTimer"))
                        + " stable=" + Observer.stable()
                        + " signature=" + Observer.signature()
                        + " actionPhase=" + (am == null ? null : am.phase)
                        + " actions=" + (am == null || am.actions == null ? -1 : am.actions.size())
                        + " cardQueue=" + (am == null || am.cardQueue == null ? -1 : am.cardQueue.size())
                        + " monsterQueue=" + (am == null || am.monsterQueue == null ? -1 : am.monsterQueue.size())
                        + " currentAction=" + (am == null || am.currentAction == null
                                ? "-"
                                : am.currentAction.getClass().getName()));
            }
        } catch (RuntimeException e) {
            Log.error("postUpdate failed", e);
        }
    }

    public void receiveCardUsed(AbstractCard card) {
        Map<String, Object> action = HumanActionTap.drainCard();
        if (configured && observeHuman && action != null) {
            sendHumanAction(action);
        }
    }

    public void receivePostPotionUse(AbstractPotion potion) {
        if (configured && observeHuman) {
            HumanActionTap.notePotionUse(potion);
        }
    }
    // ------------------------------------------------------------- 网络

    private final class Handler implements NdjsonServer.Handler {

        public void onConnect() {
            gates.invalidate();
            echo.clear();
            watchdog.disarm();
            // 每条连接都要重新 configure：重连可能换模式，不能继承上一条连接的。
            configured = false;
            observeHuman = false;
            seq = 0L;
            lastEmittedSeq = -1L;
            Log.info("agent connected");
            Map<String, Object> hello = new LinkedHashMap<String, Object>();
            hello.put("game_version", CardCrawlGame.VERSION_NUM);
            hello.put("mod_version", MOD_VERSION);
            hello.put("protocol", Integer.valueOf(Envelope.PROTOCOL));
            List<Object> caps = new ArrayList<Object>();
            caps.add("observe");
            caps.add("act");
            caps.add("human_action");
            caps.add("watchdog");
            caps.add("configure");
            hello.put("capabilities", caps);
            hello.put("watchdog_sec", Long.valueOf(watchdog.timeoutSeconds()));
            hello.put("configured", Boolean.FALSE);
            server.send(Envelope.of(Envelope.HELLO, nextId(), hello).toLine());
        }

        public void onLine(String line) {
            enqueueLine(line);
        }

        public void onDisconnect(String reason) {
            Log.info("agent disconnected: " + reason);
            watchdog.disarm();
            configured = false;
            observeHuman = false;
        }
    }

    /**
     * 网络线程：只解析、只入队。**这里绝不能碰任何游戏对象。**
     *
     * `ping` 是唯一的例外：它只回一个 `seq`，不读游戏状态，就地回省一次跨帧往返。
     */
    private void enqueueLine(String line) {
        Envelope env = Envelope.parse(line);
        if (env == null) {
            Log.warn("dropped malformed frame");
            return;
        }
        if (Envelope.PING.equals(env.type)) {
            Map<String, Object> pong = new LinkedHashMap<String, Object>();
            pong.put("seq", Long.valueOf(lastEmittedSeq));
            server.send(Envelope.reply(Envelope.PONG, nextId(), env.id, pong).toLine());
            return;
        }
        inbox.add(env);
    }

    /** 游戏主线程：把这一帧攒下的控制帧落下去。 */
    private void drainInbox() {
        for (int i = 0; i < MAX_ACTIONS_PER_FRAME; i++) {
            Envelope env = inbox.poll();
            if (env == null) {
                return;
            }
            dispatch(env);
        }
    }

    private void dispatch(Envelope env) {
        if (Envelope.CONFIGURE.equals(env.type)) {
            handleConfigure(env);
            return;
        }
        if (!Envelope.ACTION.equals(env.type)) {
            Log.warn("unexpected frame type: " + env.type);
            return;
        }
        handleAction(env);
    }

    /**
     * 游戏自己的地雷：`GameActionManager.getNextAction()` 里那段 `turnHasEnded`
     * 分支会无条件读 `AbstractDungeon.getMonsters()`，也就是 `getCurrRoom().monsters`。
     * 非战斗房间（事件 / 篝火 / 宝箱 / 商店）**从来不初始化这个字段**
     * （`AbstractRoom()` 不给它赋值，只有 MonsterRoom 系列赋），所以只要
     * `turnHasEnded` 在非战斗房间里还是 true，下一帧就是硬崩：
     *
     * NullPointerException
     * at GameActionManager.getNextAction(GameActionManager.java:429)
     * at AbstractRoom.update(AbstractRoom.java:463)
     * at EventRoom.update(EventRoom.java:28)
     *
     * 真机崩过一次（Upgrade Shrine，2026-09-28）。`turnHasEnded` 只在"玩家下一回合
     * 开始"那段里被复位，战斗若在敌人回合里结束就永远走不到那里 —— 标志漏进下一个
     * 房间。我们没有别的缝可以补，只能在自己每帧的钩子里擦掉：房间根本没有怪物组时，
     * 谈"回合"没有意义，复位它不改变任何合法流程。
     */
    private void guardStaleTurnHasEnded() {
        try {
            GameActionManager am = AbstractDungeon.actionManager;
            AbstractRoom room = AbstractDungeon.getCurrRoom();
            if (am == null || room == null || !am.turnHasEnded || room.monsters != null) {
                return;
            }
            am.turnHasEnded = false;
            Log.warn("[guard] cleared stale turnHasEnded in a monster-less room ("
                    + room.getClass().getSimpleName()
                    + "); GameActionManager would NPE on the next frame");
        } catch (RuntimeException e) {
            // 主菜单 / 开局装载期 `getCurrRoom()` 自己就会抛 NPE。这里没有房间可
            // 保护，吞掉即可——之前每帧往上抛，ERROR 刷屏把后面的 emitObservation
            // 一起吃掉了（run-0005 的 `floors_reached: 0` 就是这么来的）。
        }
    }

    /**
     * `mode` / `watchdog_sec` 的唯一入口。校验失败不改变当前状态，只回错误——
     * 半配置的模组比没配置的模组更危险。
     */
    private void handleConfigure(Envelope env) {
        Configure cfg = Configure.parse(env.payload);
        Map<String, Object> reply = new LinkedHashMap<String, Object>();
        if (!cfg.ok()) {
            Log.warn("configure rejected: " + cfg.error);
            reply.put("ok", Boolean.FALSE);
            reply.put("error", cfg.error);
        } else {
            observeHuman = cfg.observeHuman();
            gates.setThresholds(
                    observeHuman ? OBSERVE_STABLE_FRAMES : STABLE_FRAMES,
                    observeHuman ? OBSERVE_STABLE_MILLIS : STABLE_MILLIS);
            watchdog.setTimeoutSeconds(cfg.watchdogSec);
            watchdog.disarm();
            echo.clear();
            gates.invalidate();
            endTurnSeen = false;
            configured = true;
            Log.info("configured: mode=" + cfg.mode + " watchdog_sec=" + cfg.watchdogSec);
            reply.put("ok", Boolean.TRUE);
            reply.put("mode", cfg.mode);
            reply.put("watchdog_sec", Integer.valueOf(cfg.watchdogSec));
        }
        server.send(Envelope.reply(Envelope.CONFIGURED, nextId(), env.id, reply).toLine());
    }

    private void handleAction(Envelope env) {
        Map<String, Object> result;
        long actionSeq = asLong(env.payload.get("seq"));
        String kind = Json.asString(env.payload.get("kind"), null);
        Map<String, Object> args = Json.asObject(env.payload.get("args"));

        if (!configured) {
            result = Errors.fail(Errors.NOT_CONFIGURED, "mod is not configured yet");
        } else if (observeHuman) {
            result = Errors.fail(Errors.ILLEGAL_ACTION,
                    "mod is in observe_human mode; actions are ignored");
        } else if (actionSeq != lastEmittedSeq) {
            result = Errors.fail(Errors.STALE_SEQ, "action is based on seq " + actionSeq
                    + " but the latest observation is " + lastEmittedSeq);
        } else {
            ActionSpec.Result verdict = ActionSpec.validate(kind, args, GameActionContext.get());
            if (!verdict.ok()) {
                result = Errors.fail(verdict.code, verdict.message);
            } else {
                result = Actor.execute(kind, args);
                if (Boolean.TRUE.equals(result.get("ok"))) {
                    // 动作已落地：暂时闭嘴，等游戏推进出**新局面**再发观测。
                    watchdog.disarm();
                    echo.noteAction(Observer.signature(), System.nanoTime());
                } else {
                    Log.warn("action " + kind + " rejected by executor: " + result.get("error"));
                }
            }
        }
        server.send(Envelope.reply(Envelope.ACTION_RESULT, nextId(), env.id, result).toLine());
    }

    private void sendHumanAction(Map<String, Object> action) {
        Map<String, Object> payload = new LinkedHashMap<String, Object>();
        payload.put("seq", Long.valueOf(lastEmittedSeq));
        payload.put("kind", action.get("kind"));
        payload.put("args", action.get("args"));
        payload.put("screen", Observer.screenName());
        server.send(Envelope.of(Envelope.HUMAN_ACTION, nextId(), payload).toLine());
        // 人类的每一次提交也要顶出一次新观测：拿当前签名当回声基线，等游戏推进到
        // 新局面（手牌变了 / 换回合了）立刻发。不做这一步，一轮里的第 2、3 张牌
        // 只能等下一次"稳定且签名不同"，往往就等不到了 —— 用旧 seq 去对旧构题
        // 现场，`hand_index` 全是错的。
        echo.noteAction(Observer.signature(), System.nanoTime());
    }

    /**
     * 人类结束回合。
     *
     * 游戏在 `EndTurnButton.disable(true)` 里把 `AbstractPlayer.endTurnQueued` 置真
     * （点击按钮 / 按键 / 长按三条路径都汇到这里），下一回合开始时清掉。轮询它比
     * 再挂一个 patch 稳，也少一个"形参对齐写错就让游戏起不来"的坑。
     */
    private void pollEndTurn() {
        AbstractPlayer player = AbstractDungeon.player;
        if (player == null) {
            return;
        }
        if (!player.endTurnQueued) {
            endTurnSeen = false;
            return;
        }
        if (observeHuman && !endTurnSeen) {
            endTurnSeen = true;
            HumanActionTap.noteEndTurn();
        }
    }

    // ------------------------------------------------------------- 观测 / 看门狗

    private void emitObservation(long now) {
        seq++;
        Map<String, Object> raw = Observer.observe();
        Map<String, Object> payload = new LinkedHashMap<String, Object>();
        payload.put("seq", Long.valueOf(seq));
        payload.put("stable", Boolean.TRUE);
        payload.put("room", raw.get("room"));
        payload.put("raw", raw);
        Map<String, Object> sl = SlDetector.drain();
        long loads = asLong(sl.get("count"));
        if (loads > 0) {
            payload.put("sl", sl);
        }
        if (watchdogEvents > 0) {
            payload.put("watchdog_events", Integer.valueOf(watchdogEvents));
        }
        server.send(Envelope.of(Envelope.OBSERVATION, nextId(), payload).toLine());
        lastEmittedSeq = seq;
        if (!observeHuman) {
            watchdog.arm(now, seq);
        }
    }

    /**
     * 安全默认动作（docs/03-mod-protocol.md#看门狗）：
     * 战斗内结束回合；地图上随便走一格；有选项选第一个；有继续按钮就继续。
     */
    private void fireWatchdog(long now) {
        watchdog.noteTrigger(now);
        watchdogEvents++;
        GameActionContext ctx = GameActionContext.get();
        Map<String, Object> result;
        int[] bounds = ctx.selectionBounds();
        if (bounds != null) {
            // 选牌界面开着时**绝不能** end_turn：那会带着界面把一步走掉。只有
            // "可以一张都不选"的界面（预见的任意多选、`confirm` 确认屏）才存在
            // 安全的默认动作 —— 什么都不选直接确认（预见=什么都不丢）。必选 k 张
            // 的界面（头槌 / 锻造 / 删牌 / 澄明）替人类挑一张是有后果的决策，
            // 宁可不做，只记录（见 docs/03-mod-protocol.md#看门狗）。
            if (bounds[0] == 0) {
                Map<String, Object> args = new LinkedHashMap<String, Object>();
                args.put("indices", new ArrayList<Object>());
                result = Actor.execute(ActionSpec.SELECT_CARDS, args);
            } else {
                result = Errors.fail(Errors.SCREEN_MISMATCH,
                        "card selection needs a real choice");
            }
        } else if (ctx.inCombat()) {
            result = Actor.execute(ActionSpec.END_TURN, empty());
        } else if (ctx.mapScreen() && !Observer.reachableNodeIds().isEmpty()) {
            Map<String, Object> args = new LinkedHashMap<String, Object>();
            args.put("node", Observer.reachableNodeIds().get(0));
            result = Actor.execute(ActionSpec.SELECT_MAP_NODE, args);
        } else if (ctx.optionCount() > 0) {
            Map<String, Object> args = new LinkedHashMap<String, Object>();
            args.put("index", Integer.valueOf(0));
            result = Actor.execute(ActionSpec.SELECT_CHOICE, args);
        } else if (ctx.hasProceedButton()) {
            result = Actor.execute(ActionSpec.PROCEED, empty());
        } else {
            result = Errors.fail(Errors.SCREEN_MISMATCH, "nothing safe to do");
        }
        Log.warn("[watchdog] no action within " + watchdog.timeoutSeconds()
                + "s; executed default: " + result.get("ok") + " " + result.get("error"));
        Map<String, Object> payload = new LinkedHashMap<String, Object>();
        payload.put("ok", result.get("ok"));
        payload.put("error", result.get("error"));
        payload.put("code", result.get("code"));
        payload.put("watchdog", Boolean.TRUE);
        payload.put("seq", Long.valueOf(lastEmittedSeq));
        server.send(Envelope.of(Envelope.ACTION_RESULT, nextId(), payload).toLine());
    }

    // ------------------------------------------------------------- 杂项

    private synchronized long nextId() {
        return ++nextOutboundId;
    }

    private static Map<String, Object> empty() {
        return new LinkedHashMap<String, Object>();
    }

    private static long asLong(Object v) {
        return v instanceof Number ? ((Number) v).longValue() : -1L;
    }
}

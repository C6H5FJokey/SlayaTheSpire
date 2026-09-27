package spireagent;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.lang.reflect.Method;
import java.lang.reflect.Modifier;

import spireagent.bridge.Envelope;
import spireagent.bridge.Configure;
import spireagent.bridge.Json;
import spireagent.obs.EchoGate;
import spireagent.obs.Eng;
import spireagent.obs.PotionFacts;
import spireagent.obs.StabilityGate;
import spireagent.proto.ActionContext;
import spireagent.proto.ActionSpec;
import spireagent.proto.Errors;

/**
 * 无 JUnit 的自检程序（离线环境里没有 JUnit jar）。
 *
 * 只覆盖**纯逻辑**类：Json / Envelope / ActionSpec / ShopSlots / CampfireSlots /
 * StabilityGate / EchoGate / Watchdog / SlDetector，外加 `checkPatches` 用反射
 * 校验 patch 形参与目标方法签名对齐（需要 ModTheSpire + 游戏 jar，缺了会跳过）。
 * 依赖游戏类的部分只能真机冒烟（见 docs/11-testing.md）。
 *
 * 运行：java -cp mod/build/pure;mod/build/classes;<desktop-1.0.jar>;<ModTheSpire.jar>;<BaseMod.jar> spireagent.SelfTest
 */
public final class SelfTest {

    private static int passed;
    private static int failed;

    public static void main(String[] args) {
        jsonTests();
        envelopeTests();
        configureTests();
        actionSpecTests();
        stabilityGateTests();
        watchdogTests();
        echoGateTests();
        potionFactsTests();
        engTests();
        checkPatches();

        System.out.println();
        System.out.println("passed=" + passed + " failed=" + failed);
        if (failed > 0) {
            System.exit(1);
        }
    }

    // ------------------------------------------------------------- Json

    private static void jsonTests() {
        Map<String, Object> nested = new LinkedHashMap<String, Object>();
        nested.put("mode", "combat");
        nested.put("turn", Long.valueOf(3));
        nested.put("hp_ratio", Double.valueOf(0.5));
        nested.put("playable", Boolean.TRUE);
        nested.put("nothing", null);
        nested.put("cards", Arrays.asList("Strike", "Defend"));
        nested.put("damage", Double.valueOf(6.0004));

        String line = Json.write(nested);
        eq("json/order", "{\"mode\":\"combat\",\"turn\":3,\"hp_ratio\":0.5,"
                + "\"playable\":true,\"nothing\":null,"
                + "\"cards\":[\"Strike\",\"Defend\"],\"damage\":6}", line);

        Map<String, Object> back = Json.parseObject(line);
        eq("json/roundtrip-mode", "combat", Json.asString(back.get("mode"), null));
        eq("json/roundtrip-turn", 3, Json.asInt(back.get("turn"), -1));
        eq("json/roundtrip-list", 2, Json.asList(back.get("cards")).size());

        eq("json/escapes", "\"a\\\"b\\\\c\\nd\"", Json.write("a\"b\\c\nd"));
        eq("json/unicode", "\"铁甲战士\"", Json.write("铁甲战士"));
        eq("json/control", "\"\\u0001\"", Json.write("\u0001"));
        eq("json/empty-object", "{}", Json.write(new LinkedHashMap<String, Object>()));
        eq("json/empty-array", "[]", Json.write(new ArrayList<Object>()));

        eq("json/parse-empty-obj", 0, Json.parseObject("{}").size());
        eq("json/parse-ws", 7, Json.asInt(Json.parseObject("  {  \"a\" : 7 }  ").get("a"), -1));
        eq("json/parse-negative", -12, Json.asInt(Json.parseObject("{\"a\":-12}").get("a"), 0));
        eq("json/parse-exponent",
                Double.valueOf(100.0),
                Double.valueOf(((Number) Json.parseObject("{\"a\":1e2}").get("a")).doubleValue()));

        boolean threw = false;
        try {
            Json.parse("{oops}");
        } catch (RuntimeException e) {
            threw = true;
        }
        ok("json/parse-rejects-bad", threw);

        threw = false;
        try {
            Json.parse("{} trailing");
        } catch (RuntimeException e) {
            threw = true;
        }
        ok("json/parse-rejects-trailing", threw);
    }

    // --------------------------------------------------------- Envelope

    private static void envelopeTests() {
        Map<String, Object> payload = new LinkedHashMap<String, Object>();
        payload.put("seq", Long.valueOf(412));
        payload.put("stable", Boolean.TRUE);

        Envelope env = Envelope.of(Envelope.OBSERVATION, 9L, payload);
        Map<String, Object> parsed = Json.parseObject(env.toLine());
        eq("env/version", 2, Json.asInt(parsed.get("v"), 0));
        eq("env/protocol-constant", 2, Envelope.PROTOCOL);
        eq("env/type", "observation", Json.asString(parsed.get("type"), null));
        eq("env/id", 9, Json.asInt(parsed.get("id"), 0));
        eq("env/reply-to-null", null, parsed.get("reply_to"));
        eq("env/payload-seq", 412,
                Json.asInt(Json.asObject(parsed.get("payload")).get("seq"), 0));

        Envelope reply = Envelope.reply(Envelope.ACTION_RESULT, 10L, 9L, Errors.ok());
        Envelope reparsed = Envelope.parse(reply.toLine());
        ok("env/parse-reply", reparsed != null);
        eq("env/parse-reply-to", Long.valueOf(9L), reparsed.replyTo);
        eq("env/parse-ok", Boolean.TRUE, reparsed.payload.get("ok"));

        eq("env/parse-garbage", null, Envelope.parse("not json"));
        eq("env/parse-no-type", null, Envelope.parse("{\"v\":1}"));
        eq("env/parse-empty", null, Envelope.parse("{}"));
    }

    // --------------------------------------------------------- Configure

    private static void configureTests() {
        Map<String, Object> agent = new LinkedHashMap<String, Object>();
        agent.put("mode", "agent");
        agent.put("watchdog_sec", Long.valueOf(45));
        Configure a = Configure.parse(agent);
        ok("cfg/agent-ok", a.ok());
        eq("cfg/agent-mode", "agent", a.mode);
        eq("cfg/agent-sec", 45, a.watchdogSec);
        ok("cfg/agent-not-observe", !a.observeHuman());

        Map<String, Object> human = new LinkedHashMap<String, Object>();
        human.put("mode", "observe_human");
        Configure h = Configure.parse(human);
        ok("cfg/human-ok", h.ok());
        ok("cfg/human-observe", h.observeHuman());
        eq("cfg/human-default-sec", Configure.DEFAULT_WATCHDOG_SEC, h.watchdogSec);

        eq("cfg/reject-missing", false, Configure.parse(new LinkedHashMap<String, Object>()).ok());
        eq("cfg/reject-null", false, Configure.parse(null).ok());

        Map<String, Object> typo = new LinkedHashMap<String, Object>();
        typo.put("mode", "observe");
        ok("cfg/reject-unknown-mode", !Configure.parse(typo).ok());

        Map<String, Object> tooFast = new LinkedHashMap<String, Object>();
        tooFast.put("mode", "agent");
        tooFast.put("watchdog_sec", Long.valueOf(1));
        ok("cfg/reject-too-fast", !Configure.parse(tooFast).ok());

        Map<String, Object> tooSlow = new LinkedHashMap<String, Object>();
        tooSlow.put("mode", "agent");
        tooSlow.put("watchdog_sec", Long.valueOf(99999));
        ok("cfg/reject-too-slow", !Configure.parse(tooSlow).ok());

        // 失败时不得泄漏半成品状态：mode 必须是 null、秒数回落默认。
        Configure bad = Configure.parse(typo);
        eq("cfg/failed-mode-null", null, bad.mode);
        eq("cfg/failed-sec-default", Configure.DEFAULT_WATCHDOG_SEC, bad.watchdogSec);
        ok("cfg/failed-has-error", bad.error != null && !bad.error.isEmpty());
        ok("cfg/failed-not-observe", !bad.observeHuman());
    }

    // ------------------------------------------------------- ActionSpec

    private static void actionSpecTests() {
        Fake ctx = new Fake();
        ctx.combat = true;
        ctx.hand = 3;
        ctx.playable = new boolean[] {true, false, true};
        ctx.needsTarget = new boolean[] {true, false, false};
        ctx.monsters = 2;
        ctx.potions = 4;
        ctx.potionEmpty = new int[] {0, 1, 0, 0};        // 1 = 空槽
        ctx.potionUsable = new int[] {1, 0, 1, 0};        // slot3 有药水但当前不可用
        ctx.potionNeedsTarget = new int[] {1, 0, 0, 0};

        eq("spec/play-ok", null,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 0, "target", "m1"), ctx).code);
        eq("spec/play-no-target", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 0), ctx).code);
        eq("spec/play-needs-no-target", null,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 2), ctx).code);
        eq("spec/play-unplayable", Errors.CARD_NOT_PLAYABLE,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 1), ctx).code);
        eq("spec/play-out-of-range", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 7), ctx).code);
        eq("spec/play-bad-target", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 0, "target", "x1"), ctx).code);
        eq("spec/play-dead-target", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 0, "target", "m5"), ctx).code);
        eq("spec/play-missing-arg", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args(), ctx).code);

        ctx.combat = false;
        eq("spec/play-outside-combat", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.PLAY_CARD, args("hand_index", 0, "target", "m0"), ctx).code);
        eq("spec/end-turn-outside-combat", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.END_TURN, args(), ctx).code);
        ctx.combat = true;
        eq("spec/end-turn", null, ActionSpec.validate(ActionSpec.END_TURN, args(), ctx).code);

        eq("spec/potion-target-ok", null,
                ActionSpec.validate(ActionSpec.USE_POTION, args("potion_index", 0, "target", "m0"), ctx).code);
        eq("spec/potion-target-missing", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.USE_POTION, args("potion_index", 0), ctx).code);
        eq("spec/potion-empty", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.USE_POTION, args("potion_index", 1), ctx).code);
        eq("spec/potion-unusable", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.USE_POTION, args("potion_index", 3), ctx).code);
        eq("spec/potion-no-target", null,
                ActionSpec.validate(ActionSpec.USE_POTION,
                        args("potion_index", 0, "target", "m1"), ctx).code);
        eq("spec/potion-non-targeted-ok", null,
                ActionSpec.validate(ActionSpec.USE_POTION,
                        args("potion_index", 2), ctx).code);
        eq("spec/potion-unexpected-target", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.USE_POTION,
                        args("potion_index", 2, "target", "m0"), ctx).code);
        eq("spec/discard-potion-ok", null,
                ActionSpec.validate(ActionSpec.DISCARD_POTION, args("potion_index", 2), ctx).code);
        eq("spec/discard-potion-empty", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.DISCARD_POTION, args("potion_index", 1), ctx).code);

        ctx.options = 0;
        eq("spec/choice-none", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.SELECT_CHOICE, args("index", 0), ctx).code);
        ctx.options = 3;
        eq("spec/choice-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_CHOICE, args("index", 2), ctx).code);
        eq("spec/choice-oob", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.SELECT_CHOICE, args("index", 3), ctx).code);

        eq("spec/proceed-absent", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.PROCEED, args(), ctx).code);
        ctx.proceed = true;
        eq("spec/proceed-ok", null, ActionSpec.validate(ActionSpec.PROCEED, args(), ctx).code);
        eq("spec/return-absent", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.RETURN, args(), ctx).code);
        ctx.back = true;
        eq("spec/return-ok", null, ActionSpec.validate(ActionSpec.RETURN, args(), ctx).code);

        eq("spec/reward-wrong-screen", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", 0), ctx).code);
        ctx.combatReward = true;
        ctx.rewards = 2;
        eq("spec/reward-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", 1), ctx).code);
        eq("spec/reward-oob", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", 2), ctx).code);

        eq("spec/card-reward-wrong-screen", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.SELECT_CARD_REWARD, args("index", 0), ctx).code);
        ctx.cardReward = true;
        ctx.cardRewards = 3;
        eq("spec/card-reward-skip", null,
                ActionSpec.validate(ActionSpec.SELECT_CARD_REWARD, args("index", -1), ctx).code);
        eq("spec/card-reward-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_CARD_REWARD, args("index", 2), ctx).code);
        eq("spec/card-reward-oob", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.SELECT_CARD_REWARD, args("index", 3), ctx).code);

        // `select_reward` 是**按界面分派**的：战斗奖励界面看奖励列表，卡牌奖励界面看三选一。
        // core 的 `card_reward` 决策点发的就是 `select_reward`（候选 `reward:<i>`），
        // 曾经这里被写成"只允许战斗奖励界面"，真机后果是卡牌奖励永远被拒。
        ctx.combatReward = false;
        ctx.cardReward = false;
        eq("spec/reward-neither-screen", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", 0), ctx).code);
        ctx.cardReward = true;
        ctx.cardRewards = 3;
        eq("spec/reward-card-screen-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", 2), ctx).code);
        eq("spec/reward-card-screen-skip", null,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", -1), ctx).code);
        eq("spec/reward-card-screen-oob", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.SELECT_REWARD, args("index", 3), ctx).code);
        ctx.cardReward = false;
        ctx.cardRewards = 0;

        eq("spec/select-cards-no-screen", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.SELECT_CARDS, args("indices", new ArrayList<Object>()), ctx).code);
        ctx.selectable = 4;
        ctx.bounds = new int[] {2, 2};
        eq("spec/select-cards-too-few", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.SELECT_CARDS,
                        args("indices", pairs(0)), ctx).code);
        eq("spec/select-cards-too-many", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.SELECT_CARDS,
                        args("indices", pairs(0, 1, 2)), ctx).code);
        eq("spec/select-cards-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_CARDS, args("indices", pairs(1, 3)), ctx).code);
        eq("spec/select-cards-oob-card", Errors.INDEX_RANGE,
                ActionSpec.validate(ActionSpec.SELECT_CARDS, args("indices", pairs(0, 9)), ctx).code);
        eq("spec/select-cards-bad-pair", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.SELECT_CARDS,
                        args("indices", Arrays.asList("not-a-pair")), ctx).code);
        ctx.bounds = new int[] {0, 3};
        eq("spec/select-cards-empty-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_CARDS, args("indices", new ArrayList<Object>()), ctx).code);

        eq("spec/map-wrong-screen", Errors.SCREEN_MISMATCH,
                ActionSpec.validate(ActionSpec.SELECT_MAP_NODE, args("node", "n3_4"), ctx).code);
        ctx.map = true;
        ctx.reachable = new ArrayList<String>(Arrays.asList("n3_4", "n4_4"));
        eq("spec/map-ok", null,
                ActionSpec.validate(ActionSpec.SELECT_MAP_NODE, args("node", "n3_4"), ctx).code);
        eq("spec/map-unreachable", Errors.ILLEGAL_ACTION,
                ActionSpec.validate(ActionSpec.SELECT_MAP_NODE, args("node", "n9_9"), ctx).code);

        eq("spec/unknown-kind", Errors.ILLEGAL_ACTION,
                ActionSpec.validate("load_game", args(), ctx).code);
        ok("spec/known-kinds", ActionSpec.known(ActionSpec.END_TURN)
                && !ActionSpec.known("quit"));
        eq("spec/monster-index", Integer.valueOf(3), ActionSpec.monsterIndex("m3"));
        eq("spec/monster-index-bad", null, ActionSpec.monsterIndex("m"));
    }

    // ---------------------------------------------------- StabilityGate

    private static void stabilityGateTests() {
        StabilityGate gate = new StabilityGate(3, 100);
        long t = 0L;
        ok("gate/first-frame-no", !gate.shouldEmit(true, "sig", t));
        t += 10000000L;
        ok("gate/second-frame-no", !gate.shouldEmit(true, "sig", t));
        t += 200000000L;
        ok("gate/third-frame-yes", gate.shouldEmit(true, "sig", t));
        t += 10000000L;
        ok("gate/same-signature-suppressed", !gate.shouldEmit(true, "sig", t));
        t += 10000000L;
        ok("gate/new-signature", gate.shouldEmit(true, "sig2", t));

        gate.invalidate();
        t += 10000000L;
        ok("gate/invalidate-frame-1", !gate.shouldEmit(true, "sig2", t));
        t += 10000000L;
        ok("gate/invalidate-frame-2", !gate.shouldEmit(true, "sig2", t));
        t += 200000000L;
        ok("gate/invalidate-reemits-same-signature", gate.shouldEmit(true, "sig2", t));

        gate.reset();
        t += 10000000L;
        ok("gate/unstable-resets", !gate.shouldEmit(false, "sig3", t));
        t += 10000000L;
        ok("gate/after-unstable-1", !gate.shouldEmit(true, "sig3", t));
        t += 10000000L;
        ok("gate/after-unstable-2", !gate.shouldEmit(true, "sig3", t));
        t += 200000000L;
        ok("gate/after-unstable-yes", gate.shouldEmit(true, "sig3", t));
    }

    // --------------------------------------------------------- Watchdog

    private static void watchdogTests() {
        Watchdog wd = new Watchdog(30);
        eq("watchdog/timeout", 30L, wd.timeoutSeconds());
        ok("watchdog/initially-disarmed", !wd.armed());
        ok("watchdog/disarmed-never-expires", !wd.expired(1000000000L));
        wd.arm(0L, 412L);
        eq("watchdog/seq", 412L, wd.lastSeq());
        ok("watchdog/not-yet", !wd.expired(29L * 1000000000L));
        ok("watchdog/expired", wd.expired(30L * 1000000000L));
        wd.noteTrigger(30L * 1000000000L);
        eq("watchdog/triggers", 1, wd.triggers());
        ok("watchdog/rearmed", !wd.expired(59L * 1000000000L));
        ok("watchdog/expires-again", wd.expired(60L * 1000000000L));
        wd.disarm();
        ok("watchdog/disarmed-again", !wd.expired(1000L * 1000000000L));
    }

    // ------------------------------------------------------------ helpers

    /**
     * 复现 MTS legacy `@SpirePatch` 的**位置对齐**规则并逐个校验 patch 类。
     *
     * MTS 不看 patch 形参的名字，而是把第 j 个形参直接喂给目标方法的第 j 个槽位
     * （`$0`、`$1`……，实例方法的 `$0` 就是 `this`）。所以：
     * <ul>
     *   <li>无参目标方法 → patch 形参只能是 `this` 那一个，或不写；</li>
     *   <li>带参目标方法 → patch 形参必须**从 `this` 占位开始**逐个写全前缀，
     *       例如 `ShopScreen.purchaseCard(AbstractCard)` 要写成
     *       `Prefix(ShopScreen __instance, AbstractCard card)`。</li>
     * </ul>
     * 写错不会编译报错，而是加载模组时抛
     * `CannotCompileException: Prefix(...) not found`，**游戏直接起不来**。
     * 这里把同一套规则提前到打包阶段：目标方法名找不到、或形参类型不是目标槽位的前缀
     * 都算失败。
     */
    private static void checkPatches() {
        Class<?> patchAnno;
        Class<?> host;
        try {
            patchAnno = Class.forName("com.evacipated.cardcrawl.modthespire.lib.SpirePatch");
            host = Class.forName("spireagent.act.HumanActionPatches");
        } catch (Throwable t) {
            System.out.println("[patches] skipped (需要 ModTheSpire 与游戏 jar 在 classpath 上): " + t);
            return;
        }
        try {
            Method clz = patchAnno.getMethod("clz");
            Method method = patchAnno.getMethod("method");
            for (Class<?> patcher : host.getDeclaredClasses()) {
                String label = "patch/" + patcher.getSimpleName();
                Object ann = patcher.getAnnotation((Class) patchAnno);
                if (ann == null) {
                    continue;
                }
                Class<?> target = (Class<?>) clz.invoke(ann);
                String targetName = (String) method.invoke(ann);
                // 同一个 patch 类可能同时挂 Prefix 与 Postfix（例如地图节点、事件对话框），
                // 必须逐个校验；只取最后一个会漏掉另一个，自检结果就不可信了。
                List<Method> patchMethods = new ArrayList<Method>();
                for (Method m : patcher.getDeclaredMethods()) {
                    if ("Prefix".equals(m.getName()) || "Postfix".equals(m.getName())) {
                        patchMethods.add(m);
                    }
                }
                if (patchMethods.isEmpty()) {
                    failed++;
                    System.out.println("FAIL " + label + ": 没有 Prefix/Postfix 方法");
                    continue;
                }
                List<Method> candidates = new ArrayList<Method>();
                for (Method m : target.getDeclaredMethods()) {
                    if (m.getName().equals(targetName)) {
                        candidates.add(m);
                    }
                }
                if (candidates.isEmpty()) {
                    failed++;
                    System.out.println("FAIL " + label + ": " + target.getName()
                            + " 里没有方法 " + targetName);
                    continue;
                }
                for (Method patchMethod : patchMethods) {
                    Class<?>[] want = patchMethod.getParameterTypes();
                    boolean matched = false;
                    StringBuilder seen = new StringBuilder();
                    for (Method m : candidates) {
                        if (slotsPrefix(target, m, want)) {
                            matched = true;
                        }
                        if (seen.length() > 0) {
                            seen.append(" | ");
                        }
                        seen.append(slotNames(target, m));
                    }
                    ok(label + "#" + patchMethod.getName(), matched);
                    if (!matched) {
                        System.out.println("     patch 形参 = (" + namesOf(want) + ")");
                        System.out.println("     " + targetName + " 的槽位 = " + seen);
                    }
                }
            }
        } catch (Throwable t) {
            failed++;
            System.out.println("FAIL patch/shape: " + t);
        }
    }

    /** patch 形参类型必须是目标方法槽位（`$0`=this，之后是形参）的逐位前缀。 */
    private static boolean slotsPrefix(Class<?> target, Method targetMethod, Class<?>[] patchArgs) {
        List<Class<?>> slots = slotsOf(target, targetMethod);
        if (patchArgs.length > slots.size()) {
            return false;
        }
        for (int i = 0; i < patchArgs.length; i++) {
            if (!patchArgs[i].isAssignableFrom(slots.get(i))) {
                return false;
            }
        }
        return true;
    }

    private static List<Class<?>> slotsOf(Class<?> target, Method targetMethod) {
        List<Class<?>> slots = new ArrayList<Class<?>>();
        if (!Modifier.isStatic(targetMethod.getModifiers())) {
            slots.add(target);
        }
        slots.addAll(Arrays.asList(targetMethod.getParameterTypes()));
        return slots;
    }

    private static String slotNames(Class<?> target, Method targetMethod) {
        return targetMethod.getName() + "(" + namesOf(slotsOf(target, targetMethod).toArray(new Class<?>[0])) + ")";
    }

    private static String namesOf(Class<?>[] types) {
        StringBuilder sb = new StringBuilder();
        for (Class<?> t : types) {
            if (sb.length() > 0) {
                sb.append(", ");
            }
            sb.append(t.getSimpleName());
        }
        return sb.toString();
    }

    private static Map<String, Object> args(Object... kv) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        for (int i = 0; i + 1 < kv.length; i += 2) {
            out.put(String.valueOf(kv[i]), kv[i + 1]);
        }
        return out;
    }

    private static List<Object> pairs(int... indices) {
        List<Object> out = new ArrayList<Object>();
        for (int i : indices) {
            out.add(Arrays.asList("hand", Integer.valueOf(i)));
        }
        return out;
    }

    // ----------------------------------------------------------- EchoGate

    /**
     * 回声门：动作刚做完时签名还没变，必须挡住重复观测；一旦签名变了立刻放行；
     * 游戏迟迟不推进就超时兜底放行一次（`forced()`），免得 agent 永久卡死。
     */
    private static void echoGateTests() {
        EchoGate gate = new EchoGate(1000);
        long t = 0L;
        ok("echo/disarmed-allows", gate.allow("a", t));

        gate.noteAction("a", t);
        ok("echo/armed", gate.armed());
        t += 1000000L;
        ok("echo/same-signature-blocked", !gate.allow("a", t));
        ok("echo/blocked-not-forced", !gate.forced());
        t += 1000000L;
        ok("echo/new-signature-allows", gate.allow("b", t));
        ok("echo/disarmed-after-progress", !gate.armed());

        gate.noteAction("c", t);
        t += 2000000000L;
        ok("echo/fallback-allows", gate.allow("c", t));
        ok("echo/fallback-forced", gate.forced());
        ok("echo/disarmed-after-fallback", !gate.armed());

        gate.noteAction("d", t);
        gate.clear();
        ok("echo/cleared-allows", gate.allow("d", t));
        ok("echo/cleared-not-forced", !gate.forced());
    }

    // ------------------------------------------------------------- PotionFacts

    private static void potionFactsTests() {
        ok("potion/empty-slot-never-usable", !PotionFacts.usable("Potion Slot", true));
        ok("potion/real-potion-stays-usable", PotionFacts.usable("Fire Potion", true));
        ok("potion/real-potion-blocked-stays-blocked", !PotionFacts.usable("Fire Potion", false));
        ok("potion/null-id-never-usable", !PotionFacts.usable(null, true));
    }

    // ------------------------------------------------------------------ Eng

    //
    // 依赖游戏 jar 里的 localization/eng/*.json —— SelfTest 的 classpath 里
    // 一直有 desktop-1.0.jar，所以这几条在离线环境也能跑。

    private static void engTests() {
        eq("eng/card-name-strike", "Strike", Eng.cardName("Strike_R"));
        eq("eng/card-name-defend-watcher", "Defend", Eng.cardName("Defend_P"));
        eq("eng/card-text-fills-damage", "Deal 6 damage.", Eng.cardText("Strike_R", 6, 0, -1));
        // 关键回归：奖励界面上的卡 damage 是 -1，绝不能再写出 "Deal -1 damage."
        eq("eng/card-text-never-writes-minus-one", "Deal ? damage.",
                Eng.cardText("Strike_R", -1, -1, -1));
        eq("eng/card-text-fills-block", "Gain 5 Block.", Eng.cardText("Defend_P", 0, 5, -1));
        eq("eng/unknown-card-is-empty", "", Eng.cardName("NoSuchCardAtAll"));

        eq("eng/monster-name-jaw-worm", "Jaw Worm", Eng.monsterName("JawWorm"));
        eq("eng/relic-name-pure-water", "Pure Water", Eng.relicName("PureWater"));
        ok("eng/relic-desc-pure-water", Eng.relicDesc("PureWater").contains("Miracle"));
        eq("eng/potion-name-block-potion", "Block Potion", Eng.potionName("Block Potion"));
        ok("eng/potion-desc-block-potion", Eng.potionDesc("Block Potion").length() > 0);
        ok("eng/power-desc-inserts-amount", Eng.powerDesc("Vigor", 3).contains("3"));
        eq("eng/unknown-power-is-empty", "", Eng.powerName("NoSuchPower"));

        // NL 是换行 token，必须带词边界替换，不能误伤 ONLY。
        eq("eng/clean-nl", "a b", Eng.clean("a NL b"));
        eq("eng/clean-does-not-eat-only", "ONLY x", Eng.clean("ONLY NL x"));
        eq("eng/clean-strips-color-code", "Gain Block.", Eng.clean("#gGain #yBlock."));

        // 事件：资源键是显示名，代码里只有类简单名，靠归一化匹配。
        eq("eng/normalize-event-key", "bigfish", Eng.normalize("Big Fish"));
        eq("eng/event-name-by-class", "Big Fish", Eng.eventName("BigFish"));
        eq("eng/event-options-by-class", 4, Eng.eventOptions("BigFish").size());
        eq("eng/event-options-by-display-name", 4, Eng.eventOptions("Big Fish").size());
        ok("eng/event-option-keeps-english",
                Eng.eventOptions("BigFish").get(0).contains("Banana"));
        // 数值被切开的碎片位置要留下 "?"，不能凭空造数。
        ok("eng/event-option-marks-elided-number",
                Eng.eventOptions("BigFish").get(0).contains("?"));
    }

    private static void ok(String name, boolean condition) {
        if (condition) {
            passed++;
        } else {
            failed++;
            System.out.println("FAIL " + name);
        }
    }

    private static void eq(String name, Object expected, Object actual) {
        boolean same = expected == null ? actual == null : expected.equals(actual);
        if (same) {
            passed++;
        } else {
            failed++;
            System.out.println("FAIL " + name + ": expected <" + expected + "> got <" + actual + ">");
        }
    }

    private static final class Fake implements ActionContext {
        boolean combat;
        int hand;
        boolean[] playable = new boolean[0];
        boolean[] needsTarget = new boolean[0];
        int monsters;
        int potions;
        int[] potionEmpty = new int[] {};
        int[] potionUsable = new int[] {};
        int[] potionNeedsTarget = new int[] {};
        int options;
        int rewards;
        int cardRewards;
        boolean cardReward;
        boolean combatReward;
        boolean proceed;
        boolean back;
        int selectable;
        int[] bounds;
        boolean map;
        List<String> reachable = new ArrayList<String>();

        public boolean inCombat() {
            return combat;
        }

        public int handSize() {
            return hand;
        }

        public boolean canPlayHandCard(int handIndex) {
            return handIndex >= 0 && handIndex < playable.length && playable[handIndex];
        }

        public boolean handCardNeedsTarget(int handIndex) {
            return handIndex >= 0 && handIndex < needsTarget.length && needsTarget[handIndex];
        }

        public int aliveMonsterCount() {
            return monsters;
        }

        public int potionSlots() {
            return potions;
        }

        public boolean potionEmpty(int slot) {
            return slot < 0 || slot >= potions || slot >= potionEmpty.length || potionEmpty[slot] != 0;
        }

        public boolean canUsePotion(int slot) {
            return slot < potionUsable.length && potionUsable[slot] != 0;
        }

        public boolean potionNeedsTarget(int slot) {
            return slot < potionNeedsTarget.length && potionNeedsTarget[slot] != 0;
        }

        public int optionCount() {
            return options;
        }

        public int rewardCount() {
            return rewards;
        }

        public int cardRewardCount() {
            return cardRewards;
        }

        public boolean cardRewardScreen() {
            return cardReward;
        }

        public boolean combatRewardScreen() {
            return combatReward;
        }

        public boolean hasProceedButton() {
            return proceed;
        }

        public boolean hasReturnButton() {
            return back;
        }

        public int selectableCardCount() {
            return selectable;
        }

        public int[] selectionBounds() {
            return bounds;
        }

        public boolean mapScreen() {
            return map;
        }

        public boolean reachableNode(String nodeId) {
            return reachable.contains(nodeId);
        }
    }
}

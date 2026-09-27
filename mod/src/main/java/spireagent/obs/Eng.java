package spireagent.obs;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

import spireagent.bridge.Json;

/**
 * 英文文本解析：从游戏 jar 的 `localization/eng/*.json` 里按 **ID** 取英文名与英文描述。
 *
 * 为什么不能用游戏对象上的 `name` / `rawDescription`：那是**跟随界面语言**的。
 * Steam 中文版里 `AbstractCard.name` 就是中文，实测会写出
 * `"Play 打击 (1E): 造成 6 点伤害"` 这种东西。而契约要求送进模型的 state 是英文
 * （走英文 checkpoint），并且**同一局面在中英文客户端上必须产出同一份 state** ——
 * 否则数据集会被玩家语言污染。所以名字与描述一律回到 eng 资源里查。
 *
 * 纯逻辑（只依赖 Json + classpath 资源），SelfTest 可离线覆盖。
 */
public final class Eng {

    public static final String DIR = "localization/eng/";
    /** 中文资源目录。只用来建"同位置配对"索引，绝不直接进 state。 */
    public static final String DIR_ZHS = "localization/zhs/";

    /**
     * 参与"中英同位置配对"的资源文件。
     *
     * 只列结构与文本一一对应的那些：`zhs` 与 `eng` 的 key 集合、数组长度、
     * 切分点完全一致（已逐个事件核对过），所以 (文件, key, 字段, 下标) 就是
     * 一个精确的配对坐标。
     */
    private static final String[] PAIRED_FILES = {
        "cards.json",
        "relics.json",
        "potions.json",
        "powers.json",
        "monsters.json",
        "events.json",
        "ui.json",
    };

    private static final Map<String, Map<String, Object>> CACHE =
            new HashMap<String, Map<String, Object>>();

    /** 归一化索引（去掉非字母数字并小写）→ 条目，惰性建。 */
    private static final Map<String, Map<String, Map<String, Object>>> NORMALIZED =
            new HashMap<String, Map<String, Map<String, Object>>>();

    /** 本地化（中文）文本 → 英文文本；多义或空值记为 `""`（不可用）。惰性建。 */
    private static Map<String, String> localized;

    private Eng() {
    }

    // ---------------------------------------------------------------- 读取

    private static Map<String, Object> table(String file) {
        return tableAt(DIR, file);
    }

    private static Map<String, Object> tableAt(String dir, String file) {
        String key = dir + file;
        synchronized (CACHE) {
            Map<String, Object> cached = CACHE.get(key);
            if (cached != null) {
                return cached;
            }
            Map<String, Object> parsed = parse(readAt(dir, file));
            CACHE.put(key, parsed);
            return parsed;
        }
    }

    private static Map<String, Object> zhsTable(String file) {
        return tableAt(DIR_ZHS, file);
    }

    private static Map<String, Object> parse(String text) {
        if (text == null || text.isEmpty()) {
            return new HashMap<String, Object>();
        }
        try {
            return Json.parseObject(text);
        } catch (RuntimeException e) {
            return new HashMap<String, Object>();
        }
    }

    private static String read(String file) {
        return readAt(DIR, file);
    }

    private static String readAt(String dir, String file) {
        InputStream in = null;
        try {
            ClassLoader cl = Eng.class.getClassLoader();
            if (cl == null) {
                return null;
            }
            in = cl.getResourceAsStream(dir + file);
            if (in == null) {
                return null;
            }
            ByteArrayOutputStream buf = new ByteArrayOutputStream(1 << 16);
            byte[] chunk = new byte[8192];
            int n;
            while ((n = in.read(chunk)) > 0) {
                buf.write(chunk, 0, n);
            }
            return new String(buf.toByteArray(), "UTF-8");
        } catch (Exception e) {
            return null;
        } finally {
            if (in != null) {
                try {
                    in.close();
                } catch (Exception ignored) {
                    // 关不掉就算了，只读流
                }
            }
        }
    }

    /** 只给测试用：清掉缓存，强制重新读资源。 */
    public static void clearCache() {
        synchronized (CACHE) {
            CACHE.clear();
            NORMALIZED.clear();
            localized = null;
        }
    }

    /** 去掉非字母数字并小写：`Big Fish` 与 `BigFish` 归一到同一个键。 */
    public static String normalize(String s) {
        if (s == null) {
            return "";
        }
        return s.replaceAll("[^A-Za-z0-9]", "").toLowerCase(Locale.ENGLISH);
    }

    /**
     * 折叠：去掉颜色码、`NL` 换行 token、星号、空白与方括号，保留其余字符原样
     * （中文一个字也不动）。只用于"这段中文是不是资源里那一句"的比对。
     */
    public static String fold(String s) {
        if (s == null) {
            return "";
        }
        String t = s;
        t = t.replaceAll("\\bNL\\b", " ");
        t = t.replaceAll("#[a-zA-Z]", "");
        t = t.replace('*', ' ');
        t = t.replaceAll("[\\s\\[\\]\\{\\}\"'`]", "");
        return t.toLowerCase(Locale.ENGLISH);
    }

    /**
     * 中文原文 → 英文原文。查不到或多义时返回 null。
     *
     * 为什么需要它：模组跑在**中文客户端**上，`GridCardSelectScreen.tipMsg`、
     * 事件对话框正文这些运行时字符串是中文的，而送进模型的 state 必须是英文
     * （走英文 checkpoint，且同一局面在中英客户端上必须产出同一份 state）。
     * 用 zhs/eng 的"同位置配对"索引反查，比任何模糊匹配都准 —— 因为这两份
     * 资源的结构完全一致。
     */
    public static String localizedEnglish(String text) {
        String key = fold(text);
        if (key.isEmpty()) {
            return null;
        }
        String hit = index().get(key);
        return hit == null || hit.isEmpty() ? null : hit;
    }

    private static Map<String, String> index() {
        synchronized (CACHE) {
            if (localized != null) {
                return localized;
            }
            Map<String, String> built = new HashMap<String, String>();
            for (String file : PAIRED_FILES) {
                pair(table(file), zhsTable(file), built);
            }
            localized = built;
            return built;
        }
    }

    /** 同位置配对：字符串对字符串、数组按下标、对象按键。 */
    private static void pair(Object eng, Object zhs, Map<String, String> index) {
        if (eng instanceof String && zhs instanceof String) {
            String key = fold((String) zhs);
            String value = (String) eng;
            if (key.isEmpty() || value.isEmpty()) {
                return;
            }
            String prev = index.get(key);
            if (prev == null) {
                index.put(key, value);
            } else if (!prev.equals(value)) {
                // 同一段中文对应多段英文：多义宁可不给，也不给错的。
                index.put(key, "");
            }
            return;
        }
        if (eng instanceof List && zhs instanceof List) {
            List<?> e = (List<?>) eng;
            List<?> z = (List<?>) zhs;
            int n = Math.min(e.size(), z.size());
            for (int i = 0; i < n; i++) {
                pair(e.get(i), z.get(i), index);
            }
            return;
        }
        if (eng instanceof Map && zhs instanceof Map) {
            Map<?, ?> e = (Map<?, ?>) eng;
            Map<?, ?> z = (Map<?, ?>) zhs;
            for (Map.Entry<?, ?> entry : e.entrySet()) {
                pair(entry.getValue(), z.get(entry.getKey()), index);
            }
        }
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> entry(String file, String id) {
        if (id == null || id.isEmpty()) {
            return null;
        }
        Object v = table(file).get(id);
        return v instanceof Map ? (Map<String, Object>) v : null;
    }

    private static String str(Map<String, Object> entry, String key) {
        if (entry == null) {
            return null;
        }
        Object v = entry.get(key);
        return v instanceof String ? (String) v : null;
    }

    /**
     * 先按原键查，查不到再按归一化键查。
     *
     * 事件资源是个例外：`events.json` 的键是**英文显示名**（`"Big Fish"`），
     * 而代码里只有事件类的简单名（`BigFish`），所以这里必须有归一化回退。
     */
    @SuppressWarnings("unchecked")
    private static Map<String, Object> entryFuzzy(String file, String id) {
        Map<String, Object> exact = entry(file, id);
        if (exact != null || id == null || id.isEmpty()) {
            return exact;
        }
        Map<String, Map<String, Object>> index;
        synchronized (NORMALIZED) {
            index = NORMALIZED.get(file);
            if (index == null) {
                index = new HashMap<String, Map<String, Object>>();
                for (Map.Entry<String, Object> e : table(file).entrySet()) {
                    if (e.getValue() instanceof Map) {
                        String key = normalize(e.getKey());
                        if (!key.isEmpty() && !index.containsKey(key)) {
                            index.put(key, (Map<String, Object>) e.getValue());
                        }
                    }
                }
                NORMALIZED.put(file, index);
            }
        }
        return index.get(normalize(id));
    }

    // ---------------------------------------------------------------- 清洗

    /**
     * 去掉游戏富文本标记。`NL` 是换行 token（**必须带词边界**，否则 `ONLY` 里的
     * `NL` 也会被吃掉）；`#y`/`#b` 之类是颜色码；`*` 是关键词加粗标记。
     */
    public static String clean(String text) {
        if (text == null) {
            return "";
        }
        String t = text;
        t = t.replaceAll("\\bNL\\b", " ");
        t = t.replaceAll("#[a-zA-Z]", "");
        t = t.replace('*', ' ');
        t = t.replaceAll("\\s+", " ").trim();
        return t;
    }

    private static String value(int n) {
        return n < 0 ? "?" : String.valueOf(n);
    }

    // ---------------------------------------------------------------- 卡牌

    public static String cardName(String id) {
        return clean(str(entry("cards.json", id), "NAME"));
    }

    /** 英文卡面描述原文，`!D!/!B!/!M!` 保留给 {@link #cardText} 填。 */
    public static String cardDesc(String id) {
        String v = str(entry("cards.json", id), "DESCRIPTION");
        return v == null ? "" : v;
    }

    /**
     * 英文卡面描述 + 数值填充。
     *
     * 数值可能还没算出来：奖励/牌组/商店里的卡不是战斗中的手牌，游戏没跑
     * `applyPowers()`，`damage`/`block` 常是 -1。调用方先用 `baseXxx` 兜一层，
     * 两边都没有时这里写 `?` —— **绝不允许把 -1 写进文本**，那会让模型看到
     * "deal -1 damage"（实测真的发生过）。
     */
    public static String cardText(String id, int damage, int block, int magic) {
        String t = cardDesc(id);
        if (t.isEmpty()) {
            return "";
        }
        t = t.replace("!D!", value(damage));
        t = t.replace("!B!", value(block));
        t = t.replace("!M!", value(magic));
        t = t.replaceAll("![A-Za-z]+!", "");
        return clean(t);
    }

    // ---------------------------------------------------------------- 怪物

    public static String monsterName(String id) {
        return clean(str(entry("monsters.json", id), "NAME"));
    }

    // ---------------------------------------------------------------- 遗物

    public static String relicName(String id) {
        return clean(str(entry("relics.json", id), "NAME"));
    }

    public static String relicDesc(String id) {
        return joinParts(entry("relics.json", id), -1);
    }

    // ---------------------------------------------------------------- 药水

    public static String potionName(String id) {
        return clean(str(entry("potions.json", id), "NAME"));
    }

    public static String potionDesc(String id) {
        return joinParts(entry("potions.json", id), -1);
    }

    // ---------------------------------------------------------------- 能力

    public static String powerName(String id) {
        return clean(str(entry("powers.json", id), "NAME"));
    }

    /** 能力描述：`DESCRIPTIONS` 是"数值插在中间"的碎片，`amount` 填进第一段之后。 */
    public static String powerDesc(String id, int amount) {
        return joinParts(entry("powers.json", id), amount);
    }

    // ---------------------------------------------------------------- 事件

    public static String eventName(String id) {
        return clean(str(entryFuzzy("events.json", id), "NAME"));
    }

    /**
     * 事件的英文选项文本。
     *
     * `OPTIONS` 不是"一项一行"，而是**按数值切开的碎片**：一项以 `[` 开头，
     * 后续不以 `[` 开头的碎片是同一项的续写（中间那个位置留给运行时数值）。
     * 例如 Big Fish 是 `"[Banana] #gHeal #g"` + `" #gHP."` 两项碎片拼成一项。
     * 我们拿不到那个运行时数值，就在拼接处写 `?` —— 与 core 的"宁可丢修饰语
     * 也不丢数字"同一条原则：不发明数值，但要标出这里有个数值。
     *
     * 数量与界面对不上时由调用方放弃替换（见 `Observer.englishOptions`）。
     */
    public static List<String> eventOptions(String id) {
        List<String> out = new ArrayList<String>();
        Map<String, Object> e = entryFuzzy("events.json", id);
        Object raw = e == null ? null : e.get("OPTIONS");
        if (!(raw instanceof List)) {
            return out;
        }
        StringBuilder cur = null;
        for (Object o : (List<?>) raw) {
            String fragment = o instanceof String ? (String) o : "";
            if (fragment.trim().isEmpty()) {
                continue;
            }
            if (fragment.startsWith("[")) {
                if (cur != null) {
                    out.add(clean(cur.toString()));
                }
                cur = new StringBuilder(fragment);
            } else if (cur != null) {
                cur.append(" ? ").append(fragment);
            }
        }
        if (cur != null) {
            out.add(clean(cur.toString()));
        }
        return out;
    }

    /**
     * 事件的开场正文（`DESCRIPTIONS[0]`）：玩家进入事件时看到的那段，属于公平信息。
     *
     * 事件进入后续屏时用 `imageEventText.updateBodyText` 换正文，但
     * `AbstractEvent.body` 不会跟着变（反编译确认：它只在构造时赋值一次），
     * 所以这里**不做**"读运行时正文"：宁可只给开场，也不把后续（很多事件把
     * 结局写在 `DESCRIPTIONS[1]`）的文本混进来。
     */
    public static String eventIntro(String id) {
        Map<String, Object> e = entryFuzzy("events.json", id);
        Object raw = e == null ? null : e.get("DESCRIPTIONS");
        if (raw instanceof List && !((List<?>) raw).isEmpty()) {
            Object first = ((List<?>) raw).get(0);
            if (first instanceof String) {
                return clean((String) first);
            }
        }
        return "";
    }

    // ---------------------------------------------------------------- 拼装

    private static String joinParts(Map<String, Object> entry, int amount) {
        if (entry == null) {
            return "";
        }
        Object raw = entry.get("DESCRIPTIONS");
        if (raw == null) {
            raw = entry.get("DESCRIPTION");
        }
        StringBuilder sb = new StringBuilder();
        if (raw instanceof List) {
            List<?> parts = (List<?>) raw;
            for (int i = 0; i < parts.size(); i++) {
                Object p = parts.get(i);
                if (!(p instanceof String)) {
                    continue;
                }
                sb.append((String) p);
                if (amount >= 0 && i == 0 && parts.size() > 1) {
                    sb.append(amount);
                }
            }
        } else if (raw instanceof String) {
            sb.append((String) raw);
        }
        return clean(sb.toString());
    }
}

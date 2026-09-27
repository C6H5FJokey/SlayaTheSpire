package spireagent.bridge;

import java.util.Map;

/**
 * `configure` 帧的解析与校验（agent -> 模组，握手后立刻发）。
 *
 * 模组**没有自己的模式**：`mode` 与 `watchdog_sec` 只有 agent 一个来源，
 * 因此这里纯粹是校验。纯逻辑，不依赖游戏类，可在无游戏的机器上单测。
 */
public final class Configure {

    public static final String MODE_AGENT = "agent";
    public static final String MODE_OBSERVE_HUMAN = "observe_human";

    public static final String MODE_KEY = "mode";
    public static final String WATCHDOG_KEY = "watchdog_sec";

    public static final int DEFAULT_WATCHDOG_SEC = 30;
    public static final int MIN_WATCHDOG_SEC = 5;
    public static final int MAX_WATCHDOG_SEC = 3600;

    public final String mode;
    public final int watchdogSec;
    /** 非 null 表示校验失败，内容可直接回给 agent。 */
    public final String error;

    private Configure(String mode, int watchdogSec, String error) {
        this.mode = mode;
        this.watchdogSec = watchdogSec;
        this.error = error;
    }

    public boolean ok() {
        return error == null;
    }

    public boolean observeHuman() {
        return MODE_OBSERVE_HUMAN.equals(mode);
    }

    public static Configure parse(Map<String, Object> payload) {
        Map<String, Object> body = payload == null
                ? java.util.Collections.<String, Object>emptyMap() : payload;
        String mode = Json.asString(body.get(MODE_KEY), null);
        if (mode == null || mode.isEmpty()) {
            return fail("configure needs a mode (" + MODE_AGENT + " | " + MODE_OBSERVE_HUMAN + ")");
        }
        if (!MODE_AGENT.equals(mode) && !MODE_OBSERVE_HUMAN.equals(mode)) {
            return fail("unknown mode: " + mode);
        }
        int sec = Json.asInt(body.get(WATCHDOG_KEY), -1);
        if (body.get(WATCHDOG_KEY) != null && (sec < MIN_WATCHDOG_SEC || sec > MAX_WATCHDOG_SEC)) {
            return fail("watchdog_sec out of range [" + MIN_WATCHDOG_SEC + ", "
                    + MAX_WATCHDOG_SEC + "]: " + sec);
        }
        return new Configure(mode, sec < 0 ? DEFAULT_WATCHDOG_SEC : sec, null);
    }

    private static Configure fail(String message) {
        return new Configure(null, DEFAULT_WATCHDOG_SEC, message);
    }
}

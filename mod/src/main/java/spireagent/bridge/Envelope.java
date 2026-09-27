package spireagent.bridge;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 线协议信封：`{v, type, id, reply_to, payload}`（见 docs/03-mod-protocol.md）。
 *
 * 纯数据 + 纯函数，不依赖游戏类，因此可以在没有游戏的机器上单测。
 */
public final class Envelope {

    /**
     * 协议版本。v2 起：模组**没有自己的模式**，`mode` / `watchdog_sec` 由 agent 在
     * 握手后通过 `configure` 帧推送（见 docs/03-mod-protocol.md）。
     */
    public static final int PROTOCOL = 2;

    public static final String HELLO = "hello";
    public static final String CONFIGURE = "configure";
    public static final String CONFIGURED = "configured";
    public static final String OBSERVATION = "observation";
    public static final String HUMAN_ACTION = "human_action";
    public static final String ACTION = "action";
    public static final String ACTION_RESULT = "action_result";
    public static final String PING = "ping";
    public static final String PONG = "pong";

    public final String type;
    public final long id;
    public final Long replyTo;
    public final Map<String, Object> payload;

    public Envelope(String type, long id, Long replyTo, Map<String, Object> payload) {
        this.type = type;
        this.id = id;
        this.replyTo = replyTo;
        this.payload = payload == null ? new LinkedHashMap<String, Object>() : payload;
    }

    public static Envelope of(String type, long id, Map<String, Object> payload) {
        return new Envelope(type, id, null, payload);
    }

    public static Envelope reply(String type, long id, long replyTo, Map<String, Object> payload) {
        return new Envelope(type, id, Long.valueOf(replyTo), payload);
    }

    public String toLine() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("v", Long.valueOf(PROTOCOL));
        out.put("type", type);
        out.put("id", Long.valueOf(id));
        out.put("reply_to", replyTo);
        out.put("payload", payload);
        return Json.write(out);
    }

    /** 解析一行 NDJSON。畸形输入返回 null（调用方记日志并忽略）。 */
    public static Envelope parse(String line) {
        Object parsed;
        try {
            parsed = Json.parse(line);
        } catch (RuntimeException e) {
            return null;
        }
        Map<String, Object> obj = Json.asObject(parsed);
        if (obj.isEmpty()) {
            return null;
        }
        String type = Json.asString(obj.get("type"), null);
        if (type == null) {
            return null;
        }
        long id = Json.asInt(obj.get("id"), 0);
        Object replyRaw = obj.get("reply_to");
        Long replyTo = replyRaw instanceof Number ? Long.valueOf(((Number) replyRaw).longValue()) : null;
        Map<String, Object> payload = Json.asObject(obj.get("payload"));
        return new Envelope(type, id, replyTo, payload);
    }

    public static int protocolOf(Map<String, Object> obj) {
        return Json.asInt(obj.get("v"), 0);
    }
}

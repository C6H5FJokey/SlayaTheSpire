package spireagent.proto;

import java.util.LinkedHashMap;
import java.util.Map;

/** 协议错误码（见 docs/03-mod-protocol.md#错误码）。 */
public final class Errors {

    public static final String NONE = null;
    public static final String STALE_SEQ = "E_STALE_SEQ";
    public static final String ILLEGAL_ACTION = "E_ILLEGAL_ACTION";
    public static final String SCREEN_MISMATCH = "E_SCREEN_MISMATCH";
    public static final String INDEX_RANGE = "E_INDEX_RANGE";
    public static final String CARD_NOT_PLAYABLE = "E_CARD_NOT_PLAYABLE";
    public static final String INTERNAL = "E_INTERNAL";
    public static final String NOT_CONNECTED = "E_NOT_CONNECTED";
    /** agent 还没发 `configure`：模组不知道自己是哪种模式，拒绝一切动作。 */
    public static final String NOT_CONFIGURED = "E_NOT_CONFIGURED";

    private Errors() {
    }

    public static Map<String, Object> ok() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("ok", Boolean.TRUE);
        out.put("error", null);
        out.put("code", null);
        return out;
    }

    public static Map<String, Object> fail(String code, String message) {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("ok", Boolean.FALSE);
        out.put("error", message == null ? code : message);
        out.put("code", code);
        return out;
    }
}

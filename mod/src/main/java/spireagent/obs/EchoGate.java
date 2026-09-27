package spireagent.obs;

/**
 * "回声"抑制（动作与下一帧观测之间的竞态）。
 *
 * 有些动作（选牌确认、点地图节点、点继续）不是立即生效的：模组改了游戏对象，
 * 但真正关闭界面/切换房间发生在**游戏自己的下一帧 update()** 里。这段时间里
 * 观测签名可能还是旧的，若立刻重发观测，agent 会对着同一个局面再决策一次
 * （重复选牌、重复点节点）。
 *
 * 因此：执行动作后记下"动作刚做完时的签名"当基线，只有当签名**离开**基线
 * 才允许再发观测。万一游戏因为某种原因没有推进状态，也必须有出路 ——
 * 超过 {@code fallbackMillis} 就放行一次（并把 {@code forced()} 置真，
 * 让调用方强制 invalidate 去抖器），宁可多发一次也不让 agent 永久卡死。
 *
 * 纯逻辑，不依赖游戏类（见 mod/src/test/java/spireagent/SelfTest.java）。
 */
public final class EchoGate {

    private final long fallbackNanos;

    private boolean armed;
    private String baseline;
    private long armedAtNanos;
    private boolean forced;

    public EchoGate(long fallbackMillis) {
        this.fallbackNanos = Math.max(1L, fallbackMillis) * 1000000L;
    }

    /** 动作刚执行完（游戏对象已改），用当前签名作基线。 */
    public void noteAction(String signature, long nowNanos) {
        this.armed = true;
        this.baseline = signature;
        this.armedAtNanos = nowNanos;
    }

    public void clear() {
        armed = false;
        baseline = null;
    }

    public boolean armed() {
        return armed;
    }

    /** 上一次 allow() 是否因为超时兜底而放行。 */
    public boolean forced() {
        return forced;
    }

    /**
     * 这一帧的观测是否可以继续往下走（去抖 -> 发送）。
     *
     * @return true 表示允许；未布防时恒为 true
     */
    public boolean allow(String signature, long nowNanos) {
        forced = false;
        if (!armed) {
            return true;
        }
        if (signature == null) {
            return false;
        }
        if (!signature.equals(baseline)) {
            armed = false;
            baseline = null;
            return true;
        }
        if ((nowNanos - armedAtNanos) >= fallbackNanos) {
            armed = false;
            baseline = null;
            forced = true;
            return true;
        }
        return false;
    }
}
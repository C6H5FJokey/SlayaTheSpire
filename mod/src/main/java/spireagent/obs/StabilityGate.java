package spireagent.obs;

/**
 * 稳定态去抖（见 docs/03-mod-protocol.md#稳定性判定）。
 *
 * 输入每帧算出的"现在是否可决策"与观测签名，输出"这一帧该不该发 observation"。
 * 三条规则：
 *   1. 连续 N 帧且持续 >= M 毫秒都稳定，才算稳定（挡住动画最后一帧的抖动）；
 *   2. 签名与上次**相同**就不重发（agent 只关心"轮到我做决定"的新局面）；
 *   3. `invalidate()` 强制下一次稳定时重发（agent 重连、执行动作后需要新观测）。
 *
 * 纯逻辑，不依赖游戏类。
 */
public final class StabilityGate {

    private int minFrames;
    private long minNanos;

    private int streak;
    private long sinceNanos;
    private String lastSignature;

    public StabilityGate(int minFrames, long minMillis) {
        this.minFrames = Math.max(1, minFrames);
        this.minNanos = Math.max(0L, minMillis) * 1000000L;
    }

    public void reset() {
        streak = 0;
        sinceNanos = 0L;
        lastSignature = null;
    }

    /**
     * 改阈值。`observe_human` 模式要用更松的值：人类会在一次稳定态里连点几下，
     * 250ms 的门槛会把中间那几个真正需要记录的局面整个吞掉。
     */
    public void setThresholds(int minFrames, long minMillis) {
        this.minFrames = Math.max(1, minFrames);
        this.minNanos = Math.max(0L, minMillis) * 1000000L;
        reset();
    }

    /** 让下一次稳定的观测无论是否重复都发出去。 */
    public void invalidate() {
        streak = 0;
        lastSignature = null;
    }

    public boolean shouldEmit(boolean stable, String signature, long nowNanos) {
        if (!stable) {
            streak = 0;
            sinceNanos = nowNanos;
            return false;
        }
        if (streak == 0) {
            sinceNanos = nowNanos;
        }
        streak++;
        if (streak < minFrames || (nowNanos - sinceNanos) < minNanos) {
            return false;
        }
        if (signature != null && signature.equals(lastSignature)) {
            return false;
        }
        lastSignature = signature;
        return true;
    }

    public String lastSignature() {
        return lastSignature;
    }
}

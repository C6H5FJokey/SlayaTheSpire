package spireagent;

/**
 * 看门狗（见 docs/03-mod-protocol.md#看门狗）。
 *
 * 游戏不卡死的唯一保证：发出 observation 后开始计时，超时仍未收到合法动作就
 * 执行安全默认动作。纯计时逻辑，不依赖游戏类。
 */
public final class Watchdog {

    private long timeoutNanos;
    private long armedAtNanos;
    private boolean armed;
    private long lastSeq;
    private int triggers;

    public Watchdog(int timeoutSec) {
        setTimeoutSeconds(timeoutSec);
    }

    /** 由 `configure` 帧推送：agent 是超时时长的唯一来源。 */
    public void setTimeoutSeconds(int timeoutSec) {
        this.timeoutNanos = Math.max(1L, timeoutSec) * 1000000000L;
    }

    public void arm(long nowNanos, long seq) {
        this.armed = true;
        this.armedAtNanos = nowNanos;
        this.lastSeq = seq;
    }

    public void disarm() {
        this.armed = false;
    }

    public boolean armed() {
        return armed;
    }

    public long lastSeq() {
        return lastSeq;
    }

    public boolean expired(long nowNanos) {
        return armed && (nowNanos - armedAtNanos) >= timeoutNanos;
    }

    /** 超时并触发一次安全动作后调用：重新计时，避免每帧都触发。 */
    public void noteTrigger(long nowNanos) {
        triggers++;
        armedAtNanos = nowNanos;
    }

    public int triggers() {
        return triggers;
    }

    public long timeoutSeconds() {
        return timeoutNanos / 1000000000L;
    }
}

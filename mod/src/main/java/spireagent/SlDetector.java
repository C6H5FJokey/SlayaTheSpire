package spireagent;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 读档（SL）检测的**主信号**：hook 游戏的存档读入入口。
 *
 * 兜底的"状态回退"检测在 agent 侧（core.sl.RoomTracker），因为那需要跨观测
 * 的历史；这里只负责报告"这一刻发生了读档"，agent 收到后丢弃当前房间的
 * pending 行并从头重记（见 docs/08-dataset.md#sl-语义）。
 *
 * 线程安全：纯计数器 + 时间戳，patch 可能在渲染线程被调。
 */
public final class SlDetector {

    private static long loadCount;
    private static String lastSeam;
    private static long lastMillis;

    private SlDetector() {
    }

    public static synchronized void noteLoad(String seam) {
        loadCount++;
        lastSeam = seam;
        lastMillis = System.currentTimeMillis();
        Log.warn("[sl] save load detected via " + seam + " (count=" + loadCount + ")");
    }

    public static synchronized void reset() {
        loadCount = 0;
        lastSeam = null;
        lastMillis = 0L;
    }

    public static synchronized long loads() {
        return loadCount;
    }

    /** 取走自上次调用以来新增的读档事件（并把计数清零）。 */
    public static synchronized Map<String, Object> drain() {
        Map<String, Object> out = new LinkedHashMap<String, Object>();
        out.put("count", Long.valueOf(loadCount));
        out.put("seam", lastSeam);
        out.put("at_millis", Long.valueOf(lastMillis));
        loadCount = 0;
        lastSeam = null;
        return out;
    }

    public static synchronized boolean seen() {
        return lastSeam != null;
    }
}
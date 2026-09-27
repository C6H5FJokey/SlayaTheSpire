package spireagent;

import java.io.PrintStream;
import java.text.SimpleDateFormat;
import java.util.Date;

/**
 * 模组日志。
 *
 * 玩家看不到模组的 stdout，所以同时写进 ModTheSpire 控制台（System.out）。
 * 看门狗触发会走 `warn`，方便在日志里搜 `[watchdog]`。
 */
public final class Log {

    private static final SimpleDateFormat FMT = new SimpleDateFormat("HH:mm:ss.SSS");

    private Log() {
    }

    public static void info(String msg) {
        write("INFO ", msg);
    }

    public static void warn(String msg) {
        write("WARN ", msg);
    }

    public static void error(String msg, Throwable t) {
        write("ERROR", msg + (t == null ? "" : " :: " + t));
        if (t != null) {
            t.printStackTrace(System.out);
        }
    }

    private static void write(String level, String msg) {
        PrintStream out = System.out;
        synchronized (Log.class) {
            out.println("[" + FMT.format(new Date()) + "] [spire-agent] " + level + " " + msg);
            out.flush();
        }
    }
}
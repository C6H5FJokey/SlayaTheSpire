package spireagent.bridge;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.net.SocketException;
import java.nio.charset.Charset;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.TimeUnit;

import spireagent.Log;

/**
 * loopback TCP + NDJSON 服务端（见 docs/03-mod-protocol.md#传输层）。
 *
 * 只用 JDK：模组 jar 不能带第三方依赖。三条硬约束：
 *   1. 只绑 loopback，绝不监听外网；
 *   2. 同时只服务 1 个客户端，新连接踢掉旧的（agent 重启场景）；
 *   3. 发送走独立线程 + 有界队列，**绝不阻塞游戏主线程**。
 */
public final class NdjsonServer {

    /** 收线上来的回调。全部在网络线程上调用，实现方必须自己保证线程安全。 */
    public interface Handler {
        void onConnect();

        void onLine(String line);

        void onDisconnect(String reason);
    }

    private static final Charset UTF8 = Charset.forName("UTF-8");

    private final String host;
    private final int port;
    private final int maxFrameBytes;
    private final Handler handler;
    private final BlockingQueue<String> outgoing = new ArrayBlockingQueue<String>(64);

    private volatile ServerSocket serverSocket;
    private volatile Socket client;
    private volatile boolean running;
    private volatile boolean connected;
    private volatile String disconnectReason;
    private Thread acceptThread;

    public NdjsonServer(String host, int port, int maxFrameBytes, Handler handler) {
        this.host = host;
        this.port = port;
        this.maxFrameBytes = maxFrameBytes;
        this.handler = handler;
    }

    public synchronized void start() throws IOException {
        if (running) {
            return;
        }
        ServerSocket ss = new ServerSocket();
        ss.setReuseAddress(true);
        ss.bind(new InetSocketAddress(InetAddress.getByName(host), port), 1);
        serverSocket = ss;
        running = true;
        acceptThread = new Thread(new Runnable() {
            public void run() {
                acceptLoop();
            }
        }, "spire-agent-accept");
        acceptThread.setDaemon(true);
        acceptThread.start();
        Log.info("bridge listening on " + host + ":" + port);
    }

    public synchronized void stop() {
        running = false;
        closeQuietly(client);
        client = null;
        closeQuietly(serverSocket);
        serverSocket = null;
    }

    public boolean connected() {
        return connected;
    }

    public String disconnectReason() {
        return disconnectReason;
    }

    /** 非阻塞入队；队列满或未连接时丢帧并记日志（宁可丢观测也不能卡游戏）。 */
    public void send(String line) {
        if (!connected) {
            Log.warn("drop outgoing frame: no client connected");
            return;
        }
        if (!outgoing.offer(line)) {
            Log.warn("drop outgoing frame: send queue full (" + outgoing.size() + ")");
        }
    }

    // --------------------------------------------------------------- 内部

    private void acceptLoop() {
        while (running) {
            Socket sock;
            try {
                sock = serverSocket.accept();
            } catch (IOException e) {
                if (running) {
                    Log.error("accept failed", e);
                }
                return;
            }
            adopt(sock);
        }
    }

    private void adopt(Socket sock) {
        Socket previous = client;
        if (previous != null) {
            Log.info("kicking previous client");
            closeQuietly(previous);
        }
        try {
            sock.setTcpNoDelay(true);
        } catch (SocketException ignored) {
            // 尽力而为
        }
        client = sock;
        connected = true;
        disconnectReason = null;
        Thread reader = new Thread(new Runnable() {
            public void run() {
                readLoop(sock);
            }
        }, "spire-agent-read");
        reader.setDaemon(true);
        reader.start();
        Thread writer = new Thread(new Runnable() {
            public void run() {
                writeLoop(sock);
            }
        }, "spire-agent-write");
        writer.setDaemon(true);
        writer.start();
        try {
            handler.onConnect();
        } catch (RuntimeException e) {
            Log.error("onConnect handler failed", e);
        }
    }

    private void readLoop(Socket sock) {
        String reason = "eof";
        try {
            InputStream in = sock.getInputStream();
            while (running && !sock.isClosed()) {
                String line = readLine(in);
                if (line == null) {
                    break;
                }
                if (line.isEmpty()) {
                    continue;
                }
                try {
                    handler.onLine(line);
                } catch (RuntimeException e) {
                    Log.error("onLine handler failed", e);
                }
            }
        } catch (IOException e) {
            reason = e.getMessage() == null ? e.toString() : e.getMessage();
        } finally {
            closeQuietly(sock);
            if (client == sock) {
                client = null;
                connected = false;
                disconnectReason = reason;
            }
            outgoing.clear();
            try {
                handler.onDisconnect(reason);
            } catch (RuntimeException e) {
                Log.error("onDisconnect handler failed", e);
            }
        }
    }

    /**
     * 读一行，超过 maxFrameBytes 就断开（不做无界缓冲）。
     * 返回 null 表示流结束。
     */
    private String readLine(InputStream in) throws IOException {
        ByteArrayOutputStream buffer = new ByteArrayOutputStream(256);
        while (true) {
            int b = in.read();
            if (b < 0) {
                return buffer.size() == 0 ? null : new String(buffer.toByteArray(), UTF8);
            }
            if (b == '\n') {
                return new String(buffer.toByteArray(), UTF8);
            }
            if (b == '\r') {
                continue;
            }
            if (buffer.size() >= maxFrameBytes) {
                throw new IOException("frame exceeds " + maxFrameBytes + " bytes");
            }
            buffer.write(b);
        }
    }

    private void writeLoop(Socket sock) {
        try {
            OutputStream out = sock.getOutputStream();
            while (running && !sock.isClosed()) {
                String line = outgoing.poll(200L, TimeUnit.MILLISECONDS);
                if (line == null) {
                    continue;
                }
                out.write(line.getBytes(UTF8));
                out.write('\n');
                out.flush();
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } catch (IOException e) {
            Log.info("write loop ended: " + e);
        } finally {
            closeQuietly(sock);
        }
    }

    private static void closeQuietly(java.io.Closeable c) {
        if (c == null) {
            return;
        }
        try {
            c.close();
        } catch (IOException ignored) {
            // 忽略
        }
    }
}
package cn.rokid.bridge;

import android.content.Context;
import android.net.Credentials;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.util.AtomicFile;
import org.json.JSONObject;
import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.Timer;
import java.util.TimerTask;

/** One short, adb-forwarded provisioning session. The device writes its own private config. */
final class SetupChannel {
    static void open(final Context context, final String session, final Runnable ready) {
        if (session == null || !session.matches("[0-9a-f]{32}")) return;
        new Thread(new Runnable() {
            public void run() {
                LocalServerSocket server = null;
                Timer timer = new Timer(true);
                try {
                    server = new LocalServerSocket("rokid.setup." + session);
                    final LocalServerSocket listener = server;
                    timer.schedule(new TimerTask() {
                        public void run() { try { listener.close(); } catch (Exception ignored) {} }
                    }, 20000);
                    LocalSocket peer = server.accept();
                    try {
                        Credentials credentials = peer.getPeerCredentials();
                        if (credentials.getUid() != 0 && credentials.getUid() != 2000) return;
                        peer.setSoTimeout(5000);
                        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
                        InputStream input = peer.getInputStream();
                        int b;
                        while ((b = input.read()) >= 0 && b != '\n' && bytes.size() < 2048) bytes.write(b);
                        if (b != '\n') return;
                        JSONObject config = new JSONObject(bytes.toString("UTF-8"));
                        if (!config.getString("host").matches("http://[0-9.]+:[0-9]{1,5}") ||
                            !config.getString("token").matches("[A-Za-z0-9_-]{32,128}")) return;
                        config.put("enabled", true);
                        AtomicFile file = new AtomicFile(new File(context.getFilesDir(), "config.json"));
                        FileOutputStream output = file.startWrite();
                        try {
                            output.write(config.toString().getBytes("UTF-8"));
                            file.finishWrite(output);
                        } catch (Exception e) {
                            file.failWrite(output);
                            throw e;
                        }
                        ready.run();
                        peer.getOutputStream().write("OK\n".getBytes("UTF-8"));
                    } finally { peer.close(); }
                } catch (Exception ignored) {
                    // The Mac reports an incomplete handshake and can start a fresh session.
                } finally {
                    timer.cancel();
                    if (server != null) try { server.close(); } catch (Exception ignored) {}
                }
            }
        }, "bridge-setup").start();
    }
}

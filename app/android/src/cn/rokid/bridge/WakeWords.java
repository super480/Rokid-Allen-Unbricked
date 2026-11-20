package cn.rokid.bridge;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.ServiceConnection;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Parcel;
import android.os.SystemClock;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.HashMap;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * Register the local wake words on the speaker itself.
 *
 * The factory could change its wake word through its cloud account, which
 * is gone. The engine still takes words locally: com.rokid.openvoice's
 * VoiceService hands out a BearKid binder whose addVTWord/removeVTWord/
 * getVTWords calls take a Chinese word plus tone-marked pinyin. Nothing is
 * written to /system and nothing is signed: a reboot, or turning the
 * switch off, puts the factory word back in charge.
 *
 * The wire format below was read out of the openvoice APK: the interface
 * token is "com.rokid.voicerec.BearKid", the transaction numbers are
 * 6 = add (int type, String word, String pinyin) -> int,
 * 7 = remove (String word) -> int, 8 = list () -> JSON String,
 * and VT_TYPE_AWAKE is 1. Each assistant registers one pipe-separated
 * alias table ("巴蒂|巴帝|...") as its vt_word: the pinyin drives the
 * acoustic match, and the cloud accepts a wake when its transcription
 * contains any alias -- so a misheard name ("巴地") is not cut off as a
 * false trigger. The host uses the reported label to pick the assistant.
 *
 * Only words whose vt_word is one of the configured names or aliases
 * are ever removed. "若琪|洛奇" and "没事了" belong to the factory and
 * stay.
 */
final class WakeWords {

    interface Logger {
        void log(String tag, String message);
    }

    private static final String DESCRIPTOR = "com.rokid.voicerec.BearKid";
    private static final int TRANSACTION_ADD = 6;
    private static final int TRANSACTION_REMOVE = 7;
    private static final int TRANSACTION_LIST = 8;
    private static final int VT_TYPE_AWAKE = 1;
    private static final String SERVICE_ACTION = "com.rokid.openvoice.VoiceService";
    private static final String SERVICE_PACKAGE = "com.rokid.openvoice";

    /* The engine may restart and forget words that were only in memory. */
    private static final long RESYNC_MS = 45000;
    private static final long RETRY_MS = 5000;

    private final Context context;
    private final Logger logger;
    private final Handler main = new Handler(Looper.getMainLooper());
    private volatile IBinder binder;
    private volatile String signature;
    private volatile long lastSyncAt = 0;
    private volatile long lastRetryAt = 0;
    private volatile boolean binding = false;

    private final ServiceConnection connection = new ServiceConnection() {
        public void onServiceConnected(ComponentName name, IBinder service) {
            binder = service;
            signature = null; // a fresh service needs the words again
            binding = false;
            logger.log("wake", "voice service connected");
        }

        public void onServiceDisconnected(ComponentName name) {
            binder = null;
            signature = null;
            binding = false;
            logger.log("wake", "voice service disconnected");
        }
    };

    WakeWords(Context context, Logger logger) {
        this.context = context;
        this.logger = logger;
    }

    /** Bind once. The bind has to be started on a thread with a Looper. */
    void connect() {
        main.post(new Runnable() {
            public void run() {
                if (binding || binder != null) return;
                binding = true;
                try {
                    Intent intent = new Intent(SERVICE_ACTION).setPackage(SERVICE_PACKAGE);
                    if (!context.bindService(intent, connection, Context.BIND_AUTO_CREATE)) {
                        binding = false;
                        logger.log("wake", "bind refused");
                    }
                } catch (Exception error) {
                    binding = false;
                    logger.log("wake", "bind failed: " + error);
                }
            }
        });
    }

    /**
     * Bring the engine's word list in line with the settings from the host.
     * Called from the polling thread; Binder calls are synchronous, so the
     * HTTP poll is not left waiting on a lock held by the UI.
     */
    void sync(JSONObject wake) {
        if (wake == null) return;
        IBinder remote = binder;
        long now = SystemClock.elapsedRealtime();
        if (remote == null || !remote.isBinderAlive()) {
            if (now - lastRetryAt > RETRY_MS) {
                lastRetryAt = now;
                connect();
            }
            return;
        }
        String sig = wake.toString();
        if (sig.equals(signature) && now - lastSyncAt < RESYNC_MS) return;
        try {
            apply(remote, wake);
            signature = sig;
            lastSyncAt = now;
        } catch (Exception error) {
            logger.log("wake", "sync failed: " + error);
        }
    }

    private void apply(IBinder remote, JSONObject wake) throws Exception {
        boolean enabled = wake.optBoolean("enabled", false);
        JSONObject agents = wake.optJSONObject("agents");
        if (agents == null) agents = new JSONObject();

        Map<String, String> desired = new LinkedHashMap<String, String>();
        Set<String> managed = new HashSet<String>();
        Iterator<String> keys = agents.keys();
        while (keys.hasNext()) {
            JSONObject row = agents.optJSONObject(keys.next());
            if (row == null) continue;
            // One pipe-separated alias table ("巴蒂|巴帝|...") registered
            // as a single vt_word, name first. The cloud accepts a wake
            // when its transcription contains any alias, so a misheard
            // name is not cut off as a false trigger.
            String name = row.optString("vt_word", row.optString("name", ""));
            if (name.length() == 0) continue;
            managed.add(name); // ours, whether on or off
            // Older builds registered the bare name. Every part stays
            // managed so that stale entry is removed when the table
            // takes its place.
            for (String part : name.split("\\|")) {
                if (part.length() > 0) managed.add(part);
            }
            String pinyin = row.optString("pinyin", "");
            if (enabled && row.optBoolean("enabled", false) && pinyin.length() > 0) {
                desired.put(name, pinyin);
            }
        }

        Map<String, String> existing = new HashMap<String, String>();
        String listed = listWords(remote);
        logger.log("wake", "engine words: " + listed);
        if (listed != null && listed.length() > 0) {
            JSONArray rows = new JSONArray(listed);
            for (int i = 0; i < rows.length(); i++) {
                JSONObject item = rows.optJSONObject(i);
                if (item == null) continue;
                String word = item.optString("vt_word", "");
                if (managed.contains(word)) {
                    existing.put(word, item.optString("vt_pinyin", ""));
                }
            }
        }

        int added = 0, removed = 0;
        // A changed pinyin is a new word to the engine: take the old one out.
        for (String word : managed) {
            String have = existing.get(word);
            if (have != null && !have.equals(desired.get(word))) {
                int result = removeWord(remote, word);
                logger.log("wake", "remove " + word + " -> " + result);
                removed++;
            }
        }
        // The engine keeps every vt_word it was ever handed, so words from
        // older configurations linger and keep false-triggering. Dropping
        // "gpt" from an alias table, for example, leaves "GPT|gpt|计屁替"
        // registered forever because the bare name "GPT" is still managed
        // while the removed alias "gpt" is not. Any engine word whose
        // leading name is a current assistant but that is not the desired
        // word is stale and must be taken out. Factory words (若琪|洛奇,
        // 贾维斯, 没事了, …) are not assistant names, so they are left alone.
        Set<String> agentNames = new HashSet<String>();
        for (String key : desired.keySet()) {
            int bar = key.indexOf('|');
            agentNames.add(bar >= 0 ? key.substring(0, bar) : key);
        }
        if (listed != null && listed.length() > 0) {
            JSONArray rows = new JSONArray(listed);
            for (int i = 0; i < rows.length(); i++) {
                JSONObject item = rows.optJSONObject(i);
                if (item == null) continue;
                String word = item.optString("vt_word", "");
                if (word.length() == 0 || desired.containsKey(word)) continue;
                int bar = word.indexOf('|');
                String head = bar >= 0 ? word.substring(0, bar) : word;
                if (agentNames.contains(head)) {
                    int result = removeWord(remote, word);
                    logger.log("wake", "remove stale " + word + " -> " + result);
                    removed++;
                }
            }
        }
        for (Map.Entry<String, String> entry : desired.entrySet()) {
            if (!entry.getValue().equals(existing.get(entry.getKey()))) {
                int result = addWord(remote, entry.getKey(), entry.getValue());
                logger.log("wake", "add " + entry.getKey() + " "
                        + entry.getValue() + " -> " + result);
                added++;
            }
        }
        if (added > 0 || removed > 0) {
            logger.log("wake", "words now: +" + added + " -" + removed);
        }
    }

    private int addWord(IBinder remote, String word, String pinyin) throws Exception {
        Parcel data = Parcel.obtain(), reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeInt(VT_TYPE_AWAKE);
            data.writeString(word);
            data.writeString(pinyin);
            if (!remote.transact(TRANSACTION_ADD, data, reply, 0)) {
                throw new IllegalStateException("addVTWord was refused");
            }
            reply.readException();
            return reply.readInt();
        } finally {
            data.recycle();
            reply.recycle();
        }
    }

    private int removeWord(IBinder remote, String word) throws Exception {
        Parcel data = Parcel.obtain(), reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            data.writeString(word);
            if (!remote.transact(TRANSACTION_REMOVE, data, reply, 0)) {
                throw new IllegalStateException("removeVTWord was refused");
            }
            reply.readException();
            return reply.readInt();
        } finally {
            data.recycle();
            reply.recycle();
        }
    }

    private String listWords(IBinder remote) throws Exception {
        Parcel data = Parcel.obtain(), reply = Parcel.obtain();
        try {
            data.writeInterfaceToken(DESCRIPTOR);
            if (!remote.transact(TRANSACTION_LIST, data, reply, 0)) {
                throw new IllegalStateException("getVTWords was refused");
            }
            reply.readException();
            return reply.readString();
        } finally {
            data.recycle();
            reply.recycle();
        }
    }

    void close() {
        try {
            context.unbindService(connection);
        } catch (Exception ignored) {
        }
        binder = null;
    }
}

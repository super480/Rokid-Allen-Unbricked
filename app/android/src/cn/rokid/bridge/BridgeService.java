package cn.rokid.bridge;

import android.app.Service;
import android.content.ComponentName;
import android.content.Intent;
import android.content.ServiceConnection;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.media.AudioManager;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Parcel;
import android.os.PowerManager;
import android.os.SystemClock;
import android.os.Vibrator;
import android.util.Log;
import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FileWriter;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/**
 * The always-on bridge between the speaker and the host.
 *
 * Responsibilities:
 * <ul>
 *   <li>watch the Rokid event bus for wake and recognition events;</li>
 *   <li>forward the user's sentence to the host;</li>
 *   <li>poll the host for commands and act on them;</li>
 *   <li>keep the factory from speaking over the assistant.</li>
 * </ul>
 *
 * The service is deliberately a plain background service: it never takes
 * focus and never opens a window of its own. A foreground Activity holding the
 * screen makes the factory's PortalActivity call terminate(), which leaves the
 * speaker deaf after the next wake.
 */
public final class BridgeService extends Service {

    static final String TAG = "RokidBridge";

    /**
     * Factory card apps whose own UI must stay visible: while one of them
     * holds the foreground, the character overlay steps aside (it paints a
     * full-screen background and would cover the card). The idle clock is
     * ours but has the same claim on the screen.
     */
    private static final String[] CARD_PACKAGES = {
            "com.rokid.weather1",
    };

    /** Screen state, agent id and the line shown under the character. */
    static volatile String uiState = "idle";
    static volatile String uiText = "喊若琪，直接问";
    static volatile String uiAgent = "";
    static volatile String hostStatus = "未连接";

    private RokidBus bus;
    private RokidTts tts;
    private FactorySpeechGuard speechGuard;
    private WakeWords wakeWords;
    private AvatarView overlay;
    private android.view.WindowManager windows;
    private final Handler main = new Handler();
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    /** A card app holds the foreground; the character stays off the screen. */
    private volatile boolean cardForeground = false;

    private volatile boolean alive = true, enabled = false;
    private volatile String base = "", token = "";
    private volatile long cursor = -1;
    private volatile int currentTtsId = 0;
    private Runnable pendingCardSpeech;

    /**
     * The speaker's own audio channel. A clip is downloaded into the cache
     * directory over the authenticated command channel and then handed to
     * MediaPlayer as a local file: Android 4.4 cannot be trusted to stream
     * a LAN URL itself, and a network URL cannot carry the host token.
     *
     * Deliberately no AudioRecord: opening a capture stream on this device
     * deadlocks the whole audio subsystem, factory TTS included.
     */
    private volatile MediaPlayer audioPlayer = null;
    private volatile File audioFile = null;
    private volatile String audioPlayId = "";
    private volatile boolean audioPlaying = false;
    /** A loaded clip held at its current position by a pause command. */
    private volatile boolean audioPaused = false;
    /** The STREAM_MUSIC level to come back to after "unmute". */
    private volatile int unmutedVolume = -1;
    /** Replaces the character overlay for the duration of one song. */
    private NowPlayingView nowPlaying = null;
    /** General query-app card: opaque, drawn natively, always on top. */
    private MiniAppCardView miniCard = null;
    private String miniCardId = "";
    private long miniCardGeneration = 0L;
    private long miniCardExpiresAtMs = 0L;
    private String miniCardInstance = "";
    private Runnable miniExpireTask = null;
    private PowerManager.WakeLock miniCardWakeLock = null;
    private static final long MIN_CARD_WAKE_MS = 2000L;
    private static final long MAX_CARD_WAKE_MS = 12000L;
    /** Roon is display-only: no MediaPlayer, audio leaves via its AirPlay zone. */
    private volatile boolean roonShowing = false;
    private volatile String roonCoverPath = "";
    /** Lyrics found by the host for the Roon track. Sent once per song,
     *  then reused while the 5-second seek refreshes redraw the screen. */
    private volatile long[] roonLyricTimes = new long[0];
    private volatile String[] roonLyricLines = new String[0];
    /**
     * When Roon is paused or stopped the screen still shows the last track,
     * but the speaker must be allowed to fall back to its character and go
     * to sleep. Ten seconds after playback stops the display-only screen is
     * torn down. A resume within the window cancels the pending hide.
     */
    private static final long ROON_IDLE_HIDE_MS = 10000L;
    private final Handler roonHandler = new Handler(Looper.getMainLooper());
    private Runnable roonHideTask = null;
    // Lossless local tracks are routinely 30-100 MB. The old 24 MB cut-off
    // was meant for online clips and silently killed most of the library.
    private static final long MAX_AUDIO_BYTES = 256L * 1024 * 1024;
    private static final long MAX_IMAGE_BYTES = 8L * 1024 * 1024;
    private String bootId = "";
    private final String deviceSession = UUID.randomUUID().toString();
    private long eventNumber = 0;

    /**
     * The text that failed to play.
     *
     * The factory's TTS engine can wedge: once a request is cancelled while
     * it is already in flight, every later request times out after exactly
     * twenty seconds and the engine never recovers on its own. The text is
     * kept so it can be replayed once the engine works again.
     */
    private volatile String pendingSpeech = "";
    private volatile int ttsFailures = 0;
    private volatile long lastTtsFailureAt = 0;

    public IBinder onBind(Intent intent) {
        return null;
    }

    public void onCreate() {
        super.onCreate();
        readConfig();

        // The wake words are registered on the speaker's own engine. Bind
        // once; the polling loop below keeps the list in step with the
        // settings on the computer.
        wakeWords = new WakeWords(this, new WakeWords.Logger() {
            public void log(String tag, String message) {
                BridgeService.this.log(tag, message);
            }
        });
        wakeWords.connect();

        tts = new RokidTts(this, new RokidTts.Listener() {
            public void state(String state, int id, int error) {
                log("tts", state + " id=" + id + " error=" + error);
                if ("speaking".equals(state)) uiState = "speaking";
                if ("playback_done".equals(state) || "playback_stopped".equals(state)) {
                    uiState = "idle";
                    if (id == currentTtsId) {
                        currentTtsId = 0;
                        pendingSpeech = "";
                        ttsFailures = 0;
                    }
                }
                if ("tts_error".equals(state)) {
                    uiState = "error";
                    if (id == currentTtsId) currentTtsId = 0;
                    onTtsFailed(error);
                }
                if ("tts_disconnected".equals(state)) {
                    // The service process is gone. It comes back on its own
                    // (PERSISTENT), so rebind and replay what was lost.
                    onTtsServiceRestarted();
                }
                if ("tts_connected".equals(state)) {
                    onTtsConnected();
                }
                if (state.startsWith("playback") || "tts_error".equals(state)) {
                    JSONObject event = new JSONObject();
                    try {
                        event.put("type", state);
                        event.put("tts_id", id);
                        event.put("error", error);
                    } catch (Exception ignored) {
                    }
                    post(event);
                }
            }
        });
        try {
            tts.connect();
        } catch (Exception error) {
            log("error", "TTS bind: " + error);
        }

        speechGuard = new FactorySpeechGuard(this, tts, new FactorySpeechGuard.Listener() {
            public void record(String message) {
                log("guard", message);
            }
        });
        speechGuard.start();

        bus = new RokidBus(new RokidBus.Listener() {
            public void event(String action, int flags, android.os.Bundle bundle) {
                handleEvent(action, flags, bundle);
            }
        });
        try {
            String descriptor = bus.connect(new String[]{
                    "com.rokid.server.Siren.Event",
                    "com.rokid.server.rkspeech.event"});
            log("bus", "registered " + descriptor);
        } catch (Exception error) {
            log("error", "bus: " + error);
        }

        new Thread(new Runnable() {
            public void run() {
                pollLoop();
            }
        }, "rokid-poll").start();

        // Cards come and go without asking the bridge: watch the foreground
        // so the character never covers a factory card's own UI.
        main.post(cardWatcher);
    }

    public int onStartCommand(Intent intent, int flags, int id) {
        if (intent != null) {
            String action = intent.getStringExtra("command");
            if ("reload".equals(action)) {
                readConfig();
                cursor = -1;
                bootId = "";
            }
            if ("show_overlay".equals(action)) showAvatar();
            if ("hide_overlay".equals(action)) hideAvatar();
            if ("speak".equals(action)) {
                final String text = intent.getStringExtra("text");
                if (text != null) main.post(new Runnable() {
                    public void run() {
                        speak(text);
                    }
                });
            }
            // Debug/inspection entry, like show_overlay and speak above:
            // adb can push one card command without asking the host or the
            // speaker to say anything. Used to verify rendering on the panel.
            if ("miniapp_card".equals(action)) {
                final String raw = intent.getStringExtra("card");
                if (raw != null) main.post(new Runnable() {
                    @Override
                    public void run() {
                        try {
                            handleCommand(new JSONObject(raw));
                        } catch (Exception error) {
                            log("error", "miniapp debug: " + error);
                        }
                    }
                });
            }
            // Write the current card as a PNG in the app's private files
            // dir. screencap cannot see overlay windows on this firmware.
            // Replay the ring entrance animation on the card already up.
            // No data change, no sound: adb can show the motion again.
            if ("miniapp_replay".equals(action)) {
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (miniCard == null) return;
                        miniCard.replay();
                        log("miniapp", "animation replayed");
                    }
                });
            }
            // Freeze the spring at a normalised progress for frame-by-frame
            // inspection: --es command miniapp_seek --es progress 0.24
            if ("miniapp_seek".equals(action)) {
                final String rawProgress = intent.getStringExtra("progress");
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (miniCard == null) return;
                        float u = 0f;
                        try {
                            if (rawProgress != null) {
                                u = Float.parseFloat(rawProgress);
                            }
                        } catch (NumberFormatException ignored) {
                        }
                        miniCard.seek(u);
                        log("miniapp", "seek " + u);
                    }
                });
            }
            if ("miniapp_snapshot".equals(action)) {
                main.post(new Runnable() {
                    @Override
                    public void run() {
                        if (miniCard == null) return;
                        android.graphics.Bitmap bitmap = miniCard.snapshot();
                        java.io.FileOutputStream out = null;
                        try {
                            out = openFileOutput("miniapp_snapshot.png",
                                    MODE_PRIVATE);
                            bitmap.compress(
                                    android.graphics.Bitmap.CompressFormat.PNG,
                                    100, out);
                            log("miniapp", "snapshot written");
                        } catch (Exception error) {
                            log("error", "miniapp snapshot: " + error);
                        } finally {
                            if (out != null) {
                                try {
                                    out.close();
                                } catch (Exception ignored) {
                                }
                            }
                            bitmap.recycle();
                        }
                    }
                });
            }
            if ("stop".equals(action)) {
                stopSelf();
                return START_NOT_STICKY;
            }
        }
        // Survive a device reboot or a low-memory kill: the factory wake word
        // must keep working.
        return START_STICKY;
    }

    private void readConfig() {
        try {
            File file = new File(getFilesDir(), "config.json");
            if (!file.exists()) {
                log("config", "observer mode; no config.json");
                return;
            }
            JSONObject config = new JSONObject(readAll(new FileInputStream(file)));
            base = config.optString("host", "").replaceAll("/+$", "");
            token = config.optString("token", "");
            enabled = config.optBoolean("enabled", false);
            if (!base.isEmpty() && (!base.startsWith("http://") && !base.startsWith("https://"))) {
                throw new IOException("host must be http(s) URL");
            }
            // The host and the port may arrive separately. Without the port
            // every request would go to port 80 and silently fail.
            int port = config.optInt("port", 0);
            if (port > 0 && !base.matches(".*:\\d+$")) base = base + ":" + port;
            log("config", "loaded; forwarding=" + enabled + " base=" + base);
        } catch (Exception error) {
            enabled = false;
            log("error", "config: " + error);
        }
    }

    private void handleEvent(String action, int flags, android.os.Bundle bundle) {
        if (action.equals("com.rokid.server.Siren.Event")) {
            int prop = bundle.getInt("prop", -1);
            if (prop == 108 || prop == 109) {
                if (speechGuard != null) speechGuard.wake(prop);
                uiState = "listening";
                post(obj("type", "wake"));
            }
            return;
        }
        if (!action.equals("com.rokid.server.rkspeech.event")) return;
        if (bundle.getInt("event", -1) != 1) return;

        // The host watches the log for the final sentence; the bus often
        // carries only the factory's answer, so we do not forward it twice.
        String raw = bundle.getString("nlp");
        String text = findText(raw);
        if (text.isEmpty()) text = findText(bundleToJson(bundle));
        if (text.isEmpty()) {
            log("asr", "final event has no recognised text");
            return;
        }
        sendText(text, action);
    }

    /** Walk a JSON tree looking for the recognised sentence. */
    static String findText(Object value) {
        return findText(value, 0);
    }

    private static String findText(Object value, int depth) {
        if (value == null || depth > 6) return "";
        if (value instanceof String) {
            String text = ((String) value).trim();
            if (text.startsWith("{") || text.startsWith("[")) {
                try {
                    return findText(new org.json.JSONTokener(text).nextValue(), depth + 1);
                } catch (Exception ignored) {
                }
            }
            return "";
        }
        if (value instanceof JSONObject) {
            JSONObject object = (JSONObject) value;
            for (String key : new String[]{"asr", "recognizedString", "recognized_text", "query"}) {
                Object found = object.opt(key);
                if (found instanceof String && !((String) found).trim().isEmpty()) {
                    return ((String) found).trim();
                }
            }
            for (String key : new String[]{"request", "input", "slots", "nlp", "data", "response"}) {
                String found = findText(object.opt(key), depth + 1);
                if (!found.isEmpty()) return found;
            }
        } else if (value instanceof JSONArray) {
            JSONArray array = (JSONArray) value;
            for (int i = 0; i < array.length(); i++) {
                String found = findText(array.opt(i), depth + 1);
                if (!found.isEmpty()) return found;
            }
        }
        return "";
    }

    private void sendText(String text, String source) {
        JSONObject event = obj("type", "asr_final");
        // Whether the factory owns this turn. The host needs it to decide
        // who answers: in hybrid mode the factory keeps its skills, and
        // when it owns the turn the host has to stay quiet.
        try {
            event.put("factory_owns", speechGuard != null
                    && speechGuard.ownsFactoryTurn(text));
        } catch (Exception ignored) {
        }
        try {
            event.put("text", text);
            event.put("source", source);
        } catch (Exception ignored) {
        }
        post(event);
    }

    private synchronized void post(final JSONObject event) {
        if (!enabled || base.isEmpty() || token.isEmpty()) return;
        try {
            event.put("request_id", deviceSession + "-" + (++eventNumber));
        } catch (Exception ignored) {
        }
        worker.execute(new Runnable() {
            public void run() {
                for (int attempt = 0; attempt < 3 && alive; attempt++) {
                    try {
                        request("POST", "/api/event", event.toString());
                        hostStatus = "已连接";
                        return;
                    } catch (Exception error) {
                        hostStatus = "连接失败";
                        if (attempt == 2) {
                            log("error", "host POST failed: " + error.getClass().getSimpleName());
                            uiState = "error";
                            uiText = "主机连接中断，请稍后重试";
                            return;
                        }
                    }
                    try {
                        Thread.sleep(700L * (attempt + 1));
                    } catch (InterruptedException ignored) {
                        return;
                    }
                }
            }
        });
    }

    private void pollLoop() {
        while (alive) {
            if (enabled && !base.isEmpty() && !token.isEmpty()) {
                try {
                    JSONObject response = new JSONObject(request("GET",
                            "/api/commands?after=" + (cursor < 0 ? "latest" : String.valueOf(cursor)),
                            null));
                    String server = response.optString("server_instance_id");
                    if (!bootId.isEmpty() && !server.isEmpty() && !bootId.equals(server)) {
                        cursor = -1;
                        bootId = server;
                        continue;
                    }
                    bootId = server;

                    // Wake words come with every poll too: flipping a
                    // switch on the computer registers or removes the
                    // word on the speaker within one poll.
                    if (wakeWords != null) {
                        wakeWords.sync(response.optJSONObject("wake"));
                    }

                    // The label must follow the real assistant even before the
                    // next display command arrives.
                    // optString() turns a JSON null into the literal text
                    // "null", so that value has to be treated as empty --
                    // otherwise the device believes an assistant is selected
                    // and silences the factory for every question.
                    String selected = response.optString("selected_agent", "");
                    if ("null".equals(selected)) selected = "";
                    uiAgent = selected;

                    if (speechGuard != null) speechGuard.selected(!selected.isEmpty());

                    // The direct mode decides whether the factory is
                    // silenced for a turn that no assistant claimed.
                    JSONObject direct = response.optJSONObject("direct");
                    if (direct != null && speechGuard != null) {
                        speechGuard.direct(
                                SpeechGate.directModeOf(direct.optString("mode", "off")));
                        speechGuard.rule(direct.optString("factory_owns", ""));
                        // Home-control turns (HA bridge): empty = HA off.
                        speechGuard.haRule(direct.optString("ha_rule", ""));
                    }

                    JSONArray commands = response.optJSONArray("commands");
                    if (commands != null) {
                        for (int i = 0; i < commands.length(); i++) {
                            final JSONObject command = commands.getJSONObject(i);
                            long seq = command.optLong("seq", 0);
                            if (seq <= cursor) continue;
                            cursor = seq;
                            main.post(new Runnable() {
                                public void run() {
                                    handleCommand(command);
                                }
                            });
                        }
                    }
                    cursor = Math.max(cursor, response.optLong("last_seq", cursor));
                    hostStatus = "已连接";
                } catch (Exception ignored) {
                    hostStatus = "等待主机";
                }
            }
            try {
                Thread.sleep(700);
            } catch (InterruptedException ignored) {
                return;
            }
        }
    }

    private void handleCommand(JSONObject command) {
        String type = command.optString("type");
        if ("state".equals(type) || "display".equals(type)) {
            uiState = command.optString("state", "idle");
            uiAgent = command.optString("agent", uiAgent);
            if (command.has("text")) uiText = command.optString("text");
            if ("display".equals(type)) {
                // No assistant character on duty ("none", nothing selected,
                // or the direct line) means the native screen: the
                // factory's own character shows and this overlay must not
                // cover it with anything.
                if (isNativeAgent(uiAgent)) hideAvatar();
                else showAvatar();
            }
        }
        if ("speak".equals(type)) {
            String text = command.optString("text", "");
            uiText = text;
            String cardId = command.optString("card_id", "");
            if (!cardId.isEmpty()) {
                speakForCard(text, cardId, command.optLong("expires_at_ms"));
                return;
            }
            speak(text);
            // A broadcast that belongs to no assistant character must not
            // bring the overlay back -- the native screen stays.
            String agent = command.optString("agent", uiAgent);
            if (!isNativeAgent(agent)) showAvatar();
        }
        if ("stop_speaking".equals(type)) stopSpeaking();
        if ("audio".equals(type)) {
            playAudio(command.optString("url", ""),
                      command.optString("title", ""),
                      command.optString("artist", ""),
                      command.optString("cover", ""),
                      command.optJSONArray("lyrics"));
        }
        if ("stop_audio".equals(type)) stopAudio(true);
        if ("pause_audio".equals(type)) pauseAudio();
        if ("resume_audio".equals(type)) resumeAudio();
        if ("set_volume".equals(type)) applyVolume(command);
        if ("roon_now".equals(type)) showRoonNowPlaying(command);
        if ("miniapp_card".equals(type)) handleMiniAppCard(command);
        if ("notification".equals(type)) uiText = command.optString("text", "");
    }


    // -- the speaker's own audio channel ----------------------------------

    /**
     * Queue one clip. The download runs on the worker thread; MediaPlayer is
     * created and driven on the main thread, where its callbacks arrive.
     */
    private void playAudio(String path, final String title,
                           final String artist, final String coverPath,
                           final JSONArray lyrics) {
        // A new song replaces the current clip but reuses the now-playing
        // view, so switching tracks never tears down the overlay.
        // Our own MediaPlayer takes the screen back from a Roon display.
        roonShowing = false;
        roonCoverPath = "";
        roonLyricTimes = new long[0];
        roonLyricLines = new String[0];
        audioPlayId = "";
        releaseAudio();
        // The host only mints /api/audio/<hex>.<ext>. Accepting an arbitrary
        // URL here would let a command make the device fetch anywhere with
        // its host token in the header.
        if (path == null || !path.startsWith("/api/audio/") ||
                path.contains("..") || path.length() > 96) {
            log("audio", "rejected path");
            return;
        }
        final String safePath = path;
        final String safeCover = coverPath;
        // MediaPlayer sniffs the content, but on API 19 a real extension
        // makes the format hint reliable. Only the host's minted suffixes
        // are accepted, so the filename stays inside the cache dir.
        String ext = ".clip";
        for (String allowed : new String[]{".mp3", ".wav", ".ogg",
                ".m4a", ".aac", ".flac"}) {
            if (path.endsWith(allowed)) ext = allowed;
        }
        final String suffix = ext;
        final String playId = UUID.randomUUID().toString();
        final long[] times = lyricTimes(lyrics);
        final String[] lines = lyricLines(lyrics);
        audioPlayId = playId;
        uiState = "speaking";
        uiText = title.isEmpty() ? "正在播放音乐" : "正在播放：" + title;
        log("audio", "start title=" + title + " lyrics=" + times.length);
        // The now-playing screen goes up immediately, with the artwork
        // filled in once its download finishes.
        showNowPlaying(title, artist, times, lines);
        worker.execute(new Runnable() {
            public void run() {
                final File target = new File(getCacheDir(),
                        "audio-" + playId + suffix);
                try {
                    downloadAudio(safePath, target, MAX_AUDIO_BYTES);
                    Bitmap art = null;
                    if (safeCover != null && safeCover.startsWith("/api/art/")
                            && !safeCover.contains("..")
                            && safeCover.length() <= 96) {
                        File artFile = new File(getCacheDir(),
                                "cover-" + playId + ".img");
                        try {
                            downloadAudio(safeCover, artFile, MAX_IMAGE_BYTES);
                            art = decodeCover(artFile);
                            log("audio", "cover decoded=" + (art != null));
                        } catch (Exception error) {
                            log("audio", "cover: " + error);
                        }
                        artFile.delete();
                    }
                    final Bitmap decoded = art;
                    if (!alive || !playId.equals(audioPlayId)) {
                        target.delete();
                        return;
                    }
                    main.post(new Runnable() {
                        public void run() {
                            if (decoded != null && nowPlaying != null) {
                                nowPlaying.setCover(decoded);
                            }
                            startAudio(target, playId);
                        }
                    });
                } catch (Exception error) {
                    target.delete();
                    log("error", "audio download: " + error);
                    if (playId.equals(audioPlayId)) {
                        main.post(new Runnable() {
                            public void run() {
                                audioFinished(playId, "playback_error");
                            }
                        });
                    }
                }
            }
        });
    }

    private static long[] lyricTimes(JSONArray lyrics) {
        if (lyrics == null) return new long[0];
        int count = Math.min(lyrics.length(), 400);
        long[] out = new long[count];
        for (int i = 0; i < count; i++) {
            JSONObject row = lyrics.optJSONObject(i);
            out[i] = row == null ? 0 : row.optLong("t", 0);
        }
        return out;
    }

    private static String[] lyricLines(JSONArray lyrics) {
        if (lyrics == null) return new String[0];
        int count = Math.min(lyrics.length(), 400);
        String[] out = new String[count];
        for (int i = 0; i < count; i++) {
            JSONObject row = lyrics.optJSONObject(i);
            out[i] = row == null ? "" : row.optString("text", "");
        }
        return out;
    }

    private static Bitmap decodeCover(File file) {
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeFile(file.getAbsolutePath(), bounds);
        int sample = 1;
        while (bounds.outWidth / sample > 512 || bounds.outHeight / sample > 512) {
            sample *= 2;
        }
        BitmapFactory.Options options = new BitmapFactory.Options();
        options.inSampleSize = Math.max(1, sample);
        return BitmapFactory.decodeFile(file.getAbsolutePath(), options);
    }

    /** Swap the character overlay for the now-playing screen. */
    private void showNowPlaying(String title, String artist,
                                long[] times, String[] lines) {
        // A screen going up means playback is live again: drop any pending
        // Roon idle-hide so it cannot tear this screen down a moment later.
        cancelRoonHide();
        // While our MediaPlayer owns the screen, playback control words
        // belong to the host: the factory must not answer them about its
        // own (empty) player.
        if (speechGuard != null) speechGuard.bridgePlaying(true);
        hideAvatar();
        if (nowPlaying == null) {
            try {
                windows = (android.view.WindowManager)
                        getSystemService(WINDOW_SERVICE);
                nowPlaying = new NowPlayingView(this);
                android.view.WindowManager.LayoutParams params =
                        new android.view.WindowManager.LayoutParams(
                                -1, -1,
                                android.view.WindowManager.LayoutParams.TYPE_SYSTEM_OVERLAY,
                                android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                                        | android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                                        | android.view.WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                                android.graphics.PixelFormat.TRANSLUCENT);
                params.setTitle("RokidBridgeNowPlaying");
                windows.addView(nowPlaying, params);
                bringMiniCardToFront();
            } catch (Exception error) {
                nowPlaying = null;
                log("error", "now playing overlay: " + error);
                showAvatar();   // never leave the screen without a character
                return;
            }
        }
        nowPlaying.setMeta(title, artist, times, lines);
        log("audio", "now playing screen up lyric_lines=" + lines.length);
    }

    /**
     * Roon display-only mode. The audio itself leaves the Core through its
     * AirPlay zone, so this never starts a MediaPlayer: the same overlay
     * simply shows the track, zone and a locally-ticking progress line.
     */
    private void showRoonNowPlaying(JSONObject command) {
        boolean active = command.optBoolean("active", false);
        final String state = command.optString("state", "playing");
        // "playing" is the only state that keeps the screen up for good.
        // paused / stopped / loading all start the idle countdown.
        boolean isPlaying = active && "playing".equals(state);
        if (!isPlaying) {
            // Roon is not advancing audio. Give it a grace window, then
            // tear the screen down so the speaker returns to its character
            // and is free to sleep. A later "playing" frame cancels this.
            scheduleRoonHide();
            return;
        }
        // Actively playing: cancel any pending hide and show the screen.
        cancelRoonHide();
        // Our own music always takes precedence over a display-only screen.
        if (audioPlaying) return;

        final String title = command.optString("title", "");
        final String artist = command.optString("artist", "");
        final String zone = command.optString("zone", "Roon");
        final String album = command.optString("album", "");
        final double seek = command.optDouble("seek_position", 0);
        final double length = command.optDouble("length", 0);
        String cover = command.optString("cover", "");
        if (!cover.startsWith("/api/art/") || cover.contains("..") ||
                cover.length() > 96) {
            cover = "";
        }

        // The host only sends lyrics when the track changes. Keep the last
        // sheet for the periodic seek refresh, otherwise showNowPlaying()
        // would wipe the words every five seconds.
        if (command.has("lyrics")) {
            JSONArray sheet = command.optJSONArray("lyrics");
            roonLyricTimes = lyricTimes(sheet);
            roonLyricLines = lyricLines(sheet);
        }
        // Reuse the same overlay creation/path as a local song.
        showNowPlaying(title, artist, roonLyricTimes, roonLyricLines);
        roonShowing = true;
        if (nowPlaying != null) {
            nowPlaying.setRoon(state, zone, album, seek, length,
                               "paused".equals(state));
        }
        if (!cover.isEmpty() && !cover.equals(roonCoverPath)) {
            roonCoverPath = cover;
            final String safeCover = cover;
            new Thread(new Runnable() {
                public void run() {
                    File artFile = null;
                    try {
                        artFile = File.createTempFile("roon-art-", ".jpg",
                                                      getCacheDir());
                        downloadAudio(safeCover, artFile, MAX_IMAGE_BYTES);
                        final Bitmap decoded = decodeCover(artFile);
                        main.post(new Runnable() {
                            public void run() {
                                if (roonShowing && nowPlaying != null &&
                                        decoded != null) {
                                    nowPlaying.setCover(decoded);
                                }
                            }
                        });
                    } catch (Exception error) {
                        log("roon", "cover: " + error);
                    } finally {
                        if (artFile != null) artFile.delete();
                    }
                }
            }).start();
        }
    }

    /** Start (or leave running) the 10-second idle hide for the Roon screen. */
    private void scheduleRoonHide() {
        if (!roonShowing) return;
        if (roonHideTask != null) return;   // already counting down
        roonHideTask = new Runnable() {
            @Override
            public void run() {
                roonHideTask = null;
                if (roonShowing) {
                    log("roon", "idle " + ROON_IDLE_HIDE_MS / 1000
                            + "s, hiding Roon screen");
                    hideNowPlaying();
                }
            }
        };
        roonHandler.postDelayed(roonHideTask, ROON_IDLE_HIDE_MS);
    }

    /** Cancel a pending idle hide (Roon resumed, or our own audio took over). */
    private void cancelRoonHide() {
        if (roonHideTask != null) {
            roonHandler.removeCallbacks(roonHideTask);
            roonHideTask = null;
        }
    }

    /** Bring the character overlay back after a song ends. */
    private void hideNowPlaying() {
        cancelRoonHide();
        roonShowing = false;
        roonCoverPath = "";
        roonLyricTimes = new long[0];
        roonLyricLines = new String[0];
        if (speechGuard != null) speechGuard.bridgePlaying(false);
        if (nowPlaying == null) return;
        nowPlaying.stop();
        try {
            windows.removeView(nowPlaying);
        } catch (Exception ignored) {
        }
        nowPlaying = null;
        showAvatar();
        log("audio", "character overlay restored");
    }

    /**
     * One command type serves every query app: the host sends a validated
     * layout object and the device draws it. No picture download, no auth
     * header on an image URL, no generation race with a renderer service.
     */
    private void handleMiniAppCard(JSONObject command) {
        String action = command.optString("action", "");
        String id = command.optString("card_id", "");
        long generation = command.optLong("generation", 0);
        if ("hide".equals(action)) {
            // A delayed hide for an older card must not close the new one.
            if (!id.isEmpty() && !id.equals(miniCardId)) return;
            hideMiniCard();
            return;
        }
        if (!"show".equals(action)) return;
        // A restarted host starts its own sequence again. Accept its cards
        // by resetting the baseline, but still reject genuine out-of-order
        // commands within one host session.
        String instance = command.optString("server_instance_id", "");
        if (!instance.isEmpty() && !instance.equals(miniCardInstance)) {
            miniCardInstance = instance;
            miniCardGeneration = 0L;
            miniCardId = "";
        }
        long expires = command.optLong("expires_at_ms", 0);
        // A command that sat in the queue past its deadline is never shown.
        if (expires > 0 && System.currentTimeMillis() >= expires) return;
        if (generation < miniCardGeneration) return;
        JSONObject card = command.optJSONObject("card");
        if (card == null) return;
        boolean wakeScreen = command.optBoolean("wake_screen", false)
                || "mac_notify".equals(command.optString("app_id", ""))
                || "notify_carousel".equals(card.optString("layout", ""));
        showMiniCard(id, generation, expires,
                     command.optLong("ttl_ms", 20000), card, wakeScreen);
    }

    private void showMiniCard(String id, long generation, long expires,
                              long ttlMs, JSONObject card, boolean wakeScreen) {
        boolean sameCard = miniCard != null && id.equals(miniCardId);
        miniCardId = id;
        miniCardGeneration = generation;
        // Refreshing the same card does not extend its lifetime.
        if (!sameCard || miniCardExpiresAtMs <= 0) {
            miniCardExpiresAtMs = expires > 0
                    ? expires
                    : System.currentTimeMillis() + Math.max(1000L, ttlMs);
        }
        try {
            if (windows == null) {
                windows = (android.view.WindowManager)
                        getSystemService(WINDOW_SERVICE);
            }
            if (miniCard == null) {
                miniCard = new MiniAppCardView(this);
                android.view.WindowManager.LayoutParams params =
                        new android.view.WindowManager.LayoutParams(
                                -1, -1,
                                android.view.WindowManager.LayoutParams.TYPE_SYSTEM_OVERLAY,
                                android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                                        | android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                                        | android.view.WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                                android.graphics.PixelFormat.TRANSLUCENT);
                params.setTitle("RokidBridgeMiniAppCard");
                windows.addView(miniCard, params);
            }
            miniCard.apply(card, sameCard);
            bringMiniCardToFront();
            scheduleMiniCardHide();
            if (wakeScreen) {
                long remainMs = miniCardExpiresAtMs - System.currentTimeMillis();
                wakeScreenForMiniCard(id, remainMs);
            }
            log("miniapp", "card shown id=" + id + " gen=" + generation);
        } catch (Exception error) {
            log("error", "miniapp card: " + error);
            hideMiniCard();
        }
    }

    @SuppressWarnings("deprecation")
    private void wakeScreenForMiniCard(String id, long remainMs) {
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm == null || pm.isScreenOn()) return;
            if (miniCardWakeLock == null) {
                miniCardWakeLock = pm.newWakeLock(
                        PowerManager.SCREEN_BRIGHT_WAKE_LOCK
                                | PowerManager.ACQUIRE_CAUSES_WAKEUP,
                        "RokidBridge:NotifyCard");
                miniCardWakeLock.setReferenceCounted(false);
            }
            long holdMs = Math.max(MIN_CARD_WAKE_MS,
                    Math.min(MAX_CARD_WAKE_MS, remainMs));
            miniCardWakeLock.acquire(holdMs);
            log("miniapp", "screen wake acquired ms=" + holdMs + " id=" + id);
        } catch (Exception error) {
            log("error", "miniapp screen wake: " + error);
        }
    }

    private void releaseMiniCardWakeLock() {
        try {
            if (miniCardWakeLock != null && miniCardWakeLock.isHeld()) {
                miniCardWakeLock.release();
                log("miniapp", "screen wake released");
            }
        } catch (Exception ignored) {
        }
    }

    private void scheduleMiniCardHide() {
        if (miniExpireTask != null) main.removeCallbacks(miniExpireTask);
        final String id = miniCardId;
        miniExpireTask = new Runnable() {
            @Override
            public void run() {
                miniExpireTask = null;
                // Only the card this timer belongs to may be closed.
                if (id.equals(miniCardId)) hideMiniCard();
            }
        };
        long delay = Math.max(1000L,
                miniCardExpiresAtMs - System.currentTimeMillis());
        main.postDelayed(miniExpireTask, delay);
    }

    /**
     * The card is opaque and sits above everything. A display/speak/music
     * command may add a new view underneath; re-adding our own keeps the
     * card on top without stopping music or changing assistant state.
     */
    private void bringMiniCardToFront() {
        if (miniCard == null || windows == null) return;
        try {
            android.view.WindowManager.LayoutParams params =
                    (android.view.WindowManager.LayoutParams)
                            miniCard.getLayoutParams();
            windows.removeView(miniCard);
            windows.addView(miniCard, params);
        } catch (Exception ignored) {
        }
    }

    private void hideMiniCard() {
        if (miniExpireTask != null) {
            main.removeCallbacks(miniExpireTask);
            miniExpireTask = null;
        }
        if (miniCard != null) {
            miniCard.stop();
            try {
                windows.removeView(miniCard);
            } catch (Exception ignored) {
            }
            miniCard = null;
            log("miniapp", "card hidden");
        }
        miniCardId = "";
        miniCardExpiresAtMs = 0L;
        releaseMiniCardWakeLock();
        // Nothing is restored explicitly: removing the opaque card simply
        // reveals whatever is really underneath right now (music screen,
        // character overlay, or the factory's own eyes).
    }

    private void downloadAudio(String path, File target, long limit) throws Exception {
        HttpURLConnection connection = (HttpURLConnection)
                new URL(base + path).openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(30000);
        connection.setRequestMethod("GET");
        connection.setRequestProperty("Authorization", "Bearer " + token);
        try {
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                throw new IOException("HTTP " + status);
            }
            InputStream in = connection.getInputStream();
            FileOutputStream out = new FileOutputStream(target);
            try {
                byte[] buffer = new byte[64 * 1024];
                int read;
                long total = 0;
                while ((read = in.read(buffer)) > 0) {
                    out.write(buffer, 0, read);
                    total += read;
                    if (total > limit) {
                        throw new IOException("clip too large: " + total + " > " + limit);
                    }
                }
                if (total < 64) throw new IOException("empty clip");
            } finally {
                try { out.close(); } catch (Exception ignored) { }
                try { in.close(); } catch (Exception ignored) { }
            }
        } finally {
            connection.disconnect();
        }
    }

    private void startAudio(final File file, final String playId) {
        if (!alive || !playId.equals(audioPlayId)) {
            file.delete();
            return;
        }
        MediaPlayer player = null;
        try {
            player = new MediaPlayer();
            player.setAudioStreamType(AudioManager.STREAM_MUSIC);
            player.setDataSource(file.getAbsolutePath());
            player.setOnCompletionListener(new MediaPlayer.OnCompletionListener() {
                public void onCompletion(MediaPlayer p) {
                    audioFinished(playId, "playback_done");
                }
            });
            player.setOnErrorListener(new MediaPlayer.OnErrorListener() {
                public boolean onError(MediaPlayer p, int what, int extra) {
                    log("error", "audio player what=" + what + " extra=" + extra);
                    audioFinished(playId, "playback_error");
                    return true;
                }
            });
            player.prepare();
            player.start();
            audioPlayer = player;
            audioFile = file;
            audioPlaying = true;
            audioPaused = false;
            // The lyric clock reads this player's own position, so it is
            // exact without any host round trip.
            if (nowPlaying != null) {
                nowPlaying.setPaused(false);
                nowPlaying.start(player);
            }
            log("audio", "playing " + file.getName());
        } catch (Exception error) {
            if (player != null) {
                try { player.release(); } catch (Exception ignored) { }
            }
            log("error", "audio start: " + error);
            file.delete();
            audioFinished(playId, "playback_error");
        }
    }

    /**
     * A clip ended. The host must hear about it: its console shows the
     * channel's state, and the device's own uiState has to leave "speaking".
     * The event is tagged source=audio so the host keeps it out of the TTS
     * error counter and the echo-suppression window.
     */
    private void audioFinished(String playId, String state) {
        if (!playId.equals(audioPlayId)) return;
        audioPlayId = "";
        releaseAudio();
        uiState = "idle";
        uiText = "喊若琪，直接问";
        hideNowPlaying();
        log("audio", state);
        postAudioEvent(state);
    }

    private void stopAudio(boolean report) {
        boolean wasPlaying = audioPlaying;
        audioPlayId = "";
        releaseAudio();
        uiState = "idle";
        uiText = "喊若琪，直接问";
        // Also covers the case where the user stopped while the clip was
        // still downloading: the now-playing screen is already up then,
        // even though audioPlaying is still false.
        hideNowPlaying();
        if (report && wasPlaying) {
            log("audio", "playback_stopped");
            postAudioEvent("playback_stopped");
        }
    }

    /** Hold the current clip; the now-playing screen stays up. */
    private synchronized void pauseAudio() {
        MediaPlayer player = audioPlayer;
        if (player == null || !audioPlaying || audioPaused) return;
        try {
            if (player.isPlaying()) player.pause();
            audioPaused = true;
            if (nowPlaying != null) nowPlaying.setPaused(true);
            log("audio", "paused at " + player.getCurrentPosition() + "ms");
        } catch (Exception error) {
            log("error", "pause: " + error);
        }
    }

    /** Let a held clip continue from exactly where it stopped. */
    private synchronized void resumeAudio() {
        MediaPlayer player = audioPlayer;
        if (player == null || !audioPlaying || !audioPaused) return;
        try {
            player.start();
            audioPaused = false;
            if (nowPlaying != null) nowPlaying.setPaused(false);
            log("audio", "resumed at " + player.getCurrentPosition() + "ms");
        } catch (Exception error) {
            log("error", "resume: " + error);
        }
    }

    /** Apply a spoken/console volume command to our own STREAM_MUSIC. */
    private void applyVolume(JSONObject command) {
        AudioManager audio = (AudioManager) getSystemService(AUDIO_SERVICE);
        if (audio == null) return;
        final int stream = AudioManager.STREAM_MUSIC;
        int max = audio.getStreamMaxVolume(stream);
        int current = audio.getStreamVolume(stream);
        String action = command.optString("action", "");
        int step = Math.max(1, Math.round(max / 10f));
        try {
            if ("mute".equals(action)) {
                if (current > 0) unmutedVolume = current;
                audio.setStreamVolume(stream, 0, 0);
            } else if ("unmute".equals(action)) {
                int restore = unmutedVolume > 0 ? unmutedVolume
                        : Math.max(1, max / 2);
                audio.setStreamVolume(stream, Math.min(max, restore), 0);
            } else if ("up".equals(action)) {
                audio.setStreamVolume(stream, Math.min(max, current + step), 0);
            } else if ("down".equals(action)) {
                audio.setStreamVolume(stream, Math.max(0, current - step), 0);
            } else if ("set".equals(action)) {
                int percent = Math.max(0, Math.min(100,
                        command.optInt("value", 50)));
                audio.setStreamVolume(stream,
                        Math.round(max * percent / 100f), 0);
            } else {
                log("error", "unknown volume action: " + action);
                return;
            }
            log("audio", "volume " + action + " -> "
                    + audio.getStreamVolume(stream) + "/" + max);
        } catch (Exception error) {
            log("error", "volume: " + error);
            return;
        }
        postVolumeEvent(audio);
    }

    /** Report the level actually set, so the console shows the truth. */
    private void postVolumeEvent(AudioManager audio) {
        JSONObject event = new JSONObject();
        try {
            event.put("type", "volume_state");
            event.put("source", "audio");
            event.put("volume",
                    audio.getStreamVolume(AudioManager.STREAM_MUSIC));
            event.put("max",
                    audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC));
            event.put("muted",
                    audio.getStreamVolume(AudioManager.STREAM_MUSIC) == 0);
        } catch (Exception ignored) {
        }
        post(event);
    }

    private void postAudioEvent(String state) {
        JSONObject event = new JSONObject();
        try {
            event.put("type", state);
            event.put("source", "audio");
            event.put("tts_id", 0);
            event.put("error", 0);
        } catch (Exception ignored) {
        }
        post(event);
    }

    private synchronized void releaseAudio() {
        MediaPlayer player = audioPlayer;
        audioPlayer = null;
        audioPlaying = false;
        audioPaused = false;
        if (speechGuard != null) speechGuard.bridgePlaying(false);
        if (player != null) {
            try { if (player.isPlaying()) player.stop(); }
            catch (Exception ignored) { }
            try { player.release(); } catch (Exception ignored) { }
        }
        audioFile = null;
        // Any download left behind by a replaced or cancelled clip.
        File[] leftovers = getCacheDir().listFiles();
        if (leftovers != null) {
            for (File file : leftovers) {
                String name = file.getName();
                if (name.startsWith("audio-") || name.startsWith("cover-")) {
                    file.delete();
                }
            }
        }
    }

    private void stopSpeaking() {
        if (pendingCardSpeech != null) {
            main.removeCallbacks(pendingCardSpeech);
            pendingCardSpeech = null;
        }
        // "Stop talking" covers the music too: two sounds at once on a
        // single speaker is never what the user asked for.
        stopAudio(true);
        int id = currentTtsId;
        currentTtsId = 0;
        if (id > 0) {
            try {
                tts.cancelRequest(id);
                log("tts", "stopped id=" + id);
            } catch (Exception error) {
                log("error", "stop: " + error);
            }
        }
        uiState = "idle";
    }

    /**
     * Agents that leave the screen to the factory's own character: nothing
     * selected, "none", and the direct line all play the native expressions
     * -- only a real assistant (hermes/dsh/workbuddy/codex) has a character
     * of its own to show.
     */
    private static boolean isNativeAgent(String agent) {
        return agent == null || agent.isEmpty()
                || "none".equals(agent) || "direct".equals(agent);
    }

    private void showAvatar() {
        if (overlay != null) return;
        // A factory card owns the screen right now: stay off it. The
        // watcher brings the character back the moment the card leaves.
        if (cardForeground) return;
        try {
            windows = (android.view.WindowManager) getSystemService(WINDOW_SERVICE);
            overlay = new AvatarView(this);
            android.view.WindowManager.LayoutParams params =
                    new android.view.WindowManager.LayoutParams(
                            -1, -1,
                            android.view.WindowManager.LayoutParams.TYPE_SYSTEM_OVERLAY,
                            android.view.WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE
                                    | android.view.WindowManager.LayoutParams.FLAG_NOT_TOUCHABLE
                                    | android.view.WindowManager.LayoutParams.FLAG_LAYOUT_IN_SCREEN,
                            android.graphics.PixelFormat.TRANSLUCENT);
            params.setTitle("RokidBridgeAvatar");
            windows.addView(overlay, params);
            bringMiniCardToFront();
        } catch (Exception error) {
            overlay = null;
            log("error", "overlay: " + error);
        }
    }

    private void hideAvatar() {
        if (overlay == null) return;
        try {
            windows.removeView(overlay);
        } catch (Exception ignored) {
        }
        overlay = null;
    }

    /**
     * Foreground watch: factory cards (weather, ...) get the screen to
     * themselves. Every second the top activity is checked; a transition
     * hides or restores the character overlay accordingly.
     */
    private final Runnable cardWatcher = new Runnable() {
        public void run() {
            if (!alive) return;
            boolean up = isCardForeground();
            if (up != cardForeground) {
                cardForeground = up;
                if (up) {
                    hideAvatar();
                    log("overlay", "card in front: character hidden");
                } else if (!isNativeAgent(uiAgent) && nowPlaying == null) {
                    showAvatar();
                    log("overlay", "card gone: character restored");
                } else {
                    log("overlay", "card gone: character stays off");
                }
            }
            main.postDelayed(this, 1000L);
        }
    };

    private boolean isCardForeground() {
        try {
            android.app.ActivityManager manager = (android.app.ActivityManager)
                    getSystemService(ACTIVITY_SERVICE);
            java.util.List<android.app.ActivityManager.RunningTaskInfo> tasks =
                    manager.getRunningTasks(1);
            if (tasks == null || tasks.isEmpty()) return false;
            android.content.ComponentName top = tasks.get(0).topActivity;
            if (top == null) return false;
            for (String card : CARD_PACKAGES) {
                if (card.equals(top.getPackageName())) return true;
            }
        } catch (Exception error) {
            log("error", "card watch: " + error);
        }
        return false;
    }

    /**
     * The engine refused to play.
     *
     * Error 2 is the engine's own timeout: it sent the request and got no
     * answer within twenty seconds. The text is kept so it can be replayed
     * once the engine works again, and the failure is reported so the host
     * can restart the engine.
     */
    private void onTtsFailed(int error) {
        // The text stays in pendingSpeech: it never reached the speaker and
        // has to be replayed once the engine works again.
        String text = pendingSpeech;
        long now = System.currentTimeMillis();
        // Only count failures that are close together; an old error says
        // nothing about the current state of the engine.
        if (now - lastTtsFailureAt > 60000L) ttsFailures = 0;
        lastTtsFailureAt = now;
        ttsFailures++;
        log("tts", "failure #" + ttsFailures + " error=" + error
                + (text.isEmpty() ? "" : " text=" + text.length()));
        if (ttsFailures >= 2) {
            // Report it as a dedicated event so the host can act on it.
            JSONObject event = new JSONObject();
            try {
                event.put("type", "tts_stuck");
                event.put("error", error);
                event.put("failures", ttsFailures);
            } catch (Exception ignored) {
            }
            post(event);
        }
    }

    /** The service process died and came back; rebind to it. */
    private void onTtsServiceRestarted() {
        main.postDelayed(new Runnable() {
            public void run() {
                if (!alive) return;
                try {
                    tts.reconnect();
                } catch (Exception error) {
                    log("error", "tts reconnect: " + error);
                }
            }
        }, 800L);
    }

    /**
     * The engine is bound again.
     *
     * Whatever was lost while the engine was gone is replayed now. The
     * replay waits for the binding: the fresh service needs a moment before
     * it can take a request.
     */
    private void onTtsConnected() {
        String text = pendingSpeech;
        if (text.isEmpty()) return;
        pendingSpeech = "";
        main.postDelayed(new Runnable() {
            public void run() {
                if (!alive) return;
                log("tts", "replaying after restart");
                speak(text);
            }
        }, 600L);
    }

    private void speakForCard(String text, final String cardId, final long expiresAt) {
        if (pendingCardSpeech != null) main.removeCallbacks(pendingCardSpeech);
        pendingCardSpeech = new Runnable() {
            @Override public void run() {
                long now = System.currentTimeMillis();
                if (!alive || now >= expiresAt || !cardId.equals(miniCardId)) {
                    pendingCardSpeech = null;
                    return;
                }
                long sinceCancel = speechGuard == null ? Long.MAX_VALUE
                        : now - speechGuard.lastCancelAt();
                if (currentTtsId > 0 || sinceCancel < 600) {
                    main.postDelayed(this, 250L);
                    return;
                }
                pendingCardSpeech = null;
                submitSpeak(text);
            }
        };
        main.post(pendingCardSpeech);
    }

    private void speak(String text) {
        if (text == null || text.isEmpty()) return;
        // The guard may have cancelled a factory request a moment ago; the
        // engine is still tearing that session down and submitting right now
        // can wedge it for good. Wait for it to settle.
        long sinceCancel = speechGuard == null ? Long.MAX_VALUE
                : System.currentTimeMillis() - speechGuard.lastCancelAt();
        if (sinceCancel < 600) {
            final long wait = 600 - sinceCancel;
            log("tts", "deferring " + wait + "ms after factory cancel");
            main.postDelayed(new Runnable() {
                public void run() {
                    submitSpeak(text);
                }
            }, wait);
            return;
        }
        submitSpeak(text);
    }

    private void submitSpeak(String text) {
        try {
            // The TTS engine keeps one active request per client. The factory's
            // own helper stops the previous request before starting a new one;
            // skipping that leaves a stale session behind and every later
            // request ends in error 2 and never plays again.
            int previous = currentTtsId;
            if (previous > 0) {
                try {
                    tts.cancelRequest(previous);
                    log("tts", "stopped previous id=" + previous);
                } catch (Exception error) {
                    log("error", "stop previous: " + error);
                }
                currentTtsId = 0;
            }
            int id = tts.speak(text);
            currentTtsId = id;
            // Remember it: if the engine wedges, this is the text that never
            // reached the speaker and has to be replayed.
            pendingSpeech = text;
            log("tts", "submitted id=" + id);
            uiText = text;
        } catch (Exception error) {
            log("error", "TTS speak: " + error);
            uiState = "error";
        }
    }

    private String request(String method, String path, String body) throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(base + path).openConnection();
        connection.setConnectTimeout(4000);
        connection.setReadTimeout(5000);
        connection.setRequestMethod(method);
        connection.setRequestProperty("Authorization", "Bearer " + token);
        try {
            if (body != null) {
                connection.setDoOutput(true);
                connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
                byte[] bytes = body.getBytes("UTF-8");
                connection.setFixedLengthStreamingMode(bytes.length);
                OutputStream out = connection.getOutputStream();
                out.write(bytes);
                out.close();
            }
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) throw new IOException("HTTP " + status);
            return readAll(connection.getInputStream());
        } finally {
            connection.disconnect();
        }
    }

    static String readAll(InputStream in) throws IOException {
        try {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buffer = new byte[8192];
            int read, total = 0;
            while ((read = in.read(buffer)) != -1) {
                total += read;
                if (total > 1024 * 1024) throw new IOException("response too large");
                out.write(buffer, 0, read);
            }
            return out.toString("UTF-8");
        } finally {
            in.close();
        }
    }

    private static JSONObject obj(String key, String value) {
        JSONObject object = new JSONObject();
        try {
            object.put(key, value);
        } catch (Exception ignored) {
        }
        return object;
    }

    private static JSONObject bundleToJson(android.os.Bundle bundle) {
        JSONObject object = new JSONObject();
        try {
            for (String key : bundle.keySet()) {
                Object value = bundle.get(key);
                object.put(key, value instanceof android.os.Bundle
                        ? bundleToJson((android.os.Bundle) value)
                        : value == null ? JSONObject.NULL : String.valueOf(value));
            }
        } catch (Exception ignored) {
        }
        return object;
    }

    private synchronized void log(String type, String message) {
        Log.i(TAG, type + ": " + message);
        try {
            File file = new File(getFilesDir(), "events.jsonl");
            if (file.length() > 5 * 1024 * 1024) {
                File old = new File(getFilesDir(), "events.previous.jsonl");
                old.delete();
                file.renameTo(old);
            }
            JSONObject entry = new JSONObject();
            entry.put("at", System.currentTimeMillis());
            entry.put("type", type);
            entry.put("message", message);
            FileWriter writer = new FileWriter(file, true);
            writer.write(entry.toString() + "\n");
            writer.close();
        } catch (Exception ignored) {
        }
    }

    public void onDestroy() {
        alive = false;
        stopAudio(false);
        hideMiniCard();
        releaseMiniCardWakeLock();
        hideAvatar();
        if (speechGuard != null) speechGuard.close();
        bus.close();
        tts.close();
        if (wakeWords != null) wakeWords.close();
        worker.shutdownNow();
        main.removeCallbacksAndMessages(null);
        super.onDestroy();
    }
}

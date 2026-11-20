package cn.rokid.clock;

import android.content.Context;
import android.graphics.Typeface;
import android.os.Environment;

import org.json.JSONObject;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.nio.charset.Charset;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * Built-in faces plus optional sdcard faces, and the single config file
 * that says which one is active. The host switches faces over a broadcast
 * (SET_FACE) or by pushing config.json; both land here.
 */
public final class FaceRegistry {

    public static final String ROOT_DIR = "RokidClock";
    public static final String CONFIG_NAME = "config.json";
    public static final String FACES_DIR = "faces";

    private static final List<ClockFace> BUILTINS = new ArrayList<ClockFace>();
    private static final List<ClockFace> ALL = new ArrayList<ClockFace>();
    private static Typeface typeface;
    private static Context appContext;

    private FaceRegistry() {
    }

    public static synchronized void init(Context context) {
        if (typeface == null) {
            appContext=context.getApplicationContext();
            typeface = Typeface.create("sans-serif", Typeface.BOLD);
            BUILTINS.add(new GridFace("grid", typeface));
            BUILTINS.add(new DigitalFace("digital", typeface));
            BUILTINS.add(new OrbitFace("orbit"));
            BUILTINS.add(new WeekdayFace("weekday"));
            BUILTINS.add(new MinimalFace("minimal"));
            BUILTINS.add(new LagoonFace("lagoon", typeface));
            BUILTINS.add(new MilkFace("milk", appContext, typeface));
            BUILTINS.add(new ForestFace("forest", appContext, typeface));
            BUILTINS.add(new RomanFace("roman", appContext));
        }
        rescan();
    }

    /** Re-read the sdcard face directory; built-ins always stay first. */
    public static synchronized void rescan() {
        ALL.clear();
        ALL.addAll(BUILTINS);
        File faces = new File(baseDir(), FACES_DIR);
        File[] dirs = faces.listFiles();
        if (dirs != null) {
            List<File> sorted = new ArrayList<File>();
            for (File dir : dirs) {
                if (dir.isDirectory()) {
                    sorted.add(dir);
                }
            }
            Collections.sort(sorted);
            for (File dir : sorted) {
                ClockFace face = loadJsonFace(dir);
                if (face != null) {
                    ALL.add(face);
                }
            }
        }
    }

    public static synchronized List<String> names() {
        List<String> out = new ArrayList<String>();
        for (ClockFace face : ALL) {
            out.add(face.name());
        }
        return out;
    }

    public static synchronized ClockFace get(String name) {
        for (ClockFace face : ALL) {
            if (face.name().equals(name)) {
                return face;
            }
        }
        return BUILTINS.get(0);
    }

    public static synchronized String next(String current) {
        int index = -1;
        for (int i = 0; i < ALL.size(); i++) {
            if (ALL.get(i).name().equals(current)) {
                index = i;
                break;
            }
        }
        return ALL.get((index + 1 + ALL.size()) % ALL.size()).name();
    }

    // -- config.json ------------------------------------------------------

    public static synchronized String readCurrentFace() {
        try {
            JSONObject json = readJson(new File(baseDir(), CONFIG_NAME));
            String face = json.optString("face", "");
            if (!face.equals("")) {
                return face;
            }
        } catch (Exception ignored) {
        }
        return "grid";
    }

    public static synchronized void writeCurrentFace(String face) {
        try {
            JSONObject json = new JSONObject();
            json.put("face", face);
            writeJson(new File(baseDir(), CONFIG_NAME), json);
        } catch (Exception error) {
            android.util.Log.w("ClockFace", "config write failed: " + error);
        }
    }

    public static File baseDir() {
        return new File(Environment.getExternalStorageDirectory(), ROOT_DIR);
    }

    /** mtime of config.json (or -1); the activity polls it every second. */
    public static long configStamp() {
        File file = new File(baseDir(), CONFIG_NAME);
        return file.exists() ? file.lastModified() : -1L;
    }

    // -- face.json --------------------------------------------------------

    private static ClockFace loadJsonFace(File dir) {
        File file = new File(dir, "face.json");
        if (!file.exists()) {
            return null;
        }
        try {
            JSONObject json = readJson(file);
            String style = json.optString("style", "digital");
            String name = json.optString("name", dir.getName());
            JSONObject palette = json.optJSONObject("palette");
            int bg = color(palette, "bg", GridFace.BG);
            int text = color(palette, "text", 0xFFF2F2F4);
            int accent = color(palette, "hands", GridFace.MINT);
            int fontPx = json.optInt("fontPx", 180);
            boolean showDate = json.optJSONObject("digital") == null
                    || json.optJSONObject("digital").optBoolean("showDate", true);
            if ("roman".equals(style)) {
                return new RomanFace(name, appContext);
            }
            if ("forest".equals(style)) {
                return new ForestFace(name, appContext, typeface);
            }
            if ("milk".equals(style)) {
                return new MilkFace(name, appContext, typeface);
            }
            if ("lagoon".equals(style)) {
                return new LagoonFace(name, typeface);
            }
            if ("minimal".equals(style)) {
                return new MinimalFace(name);
            }
            if ("weekday".equals(style)) {
                return new WeekdayFace(name);
            }
            if ("orbit".equals(style)) {
                return new OrbitFace(name);
            }
            return new DigitalFace(name, typeface, bg, text, accent, fontPx, showDate);
        } catch (Exception error) {
            android.util.Log.w("ClockFace", "bad face.json in " + dir + ": " + error);
            return null;
        }
    }

    private static int color(JSONObject palette, String key, int fallback) {
        if (palette == null) {
            return fallback;
        }
        String value = palette.optString(key, "");
        if (value.matches("#[0-9a-fA-F]{6}")) {
            return (int) (Long.parseLong(value.substring(1), 16) | 0xFF000000L);
        }
        return fallback;
    }

    private static JSONObject readJson(File file) throws Exception {
        FileInputStream in = new FileInputStream(file);
        try {
            byte[] data = new byte[(int) file.length()];
            int read = 0;
            while (read < data.length) {
                int n = in.read(data, read, data.length - read);
                if (n < 0) {
                    break;
                }
                read += n;
            }
            return new JSONObject(new String(data, 0, read, Charset.forName("UTF-8")));
        } finally {
            in.close();
        }
    }

    private static void writeJson(File file, JSONObject json) throws Exception {
        File dir = file.getParentFile();
        if (dir != null && !dir.exists()) {
            dir.mkdirs();
        }
        FileOutputStream out = new FileOutputStream(file);
        try {
            out.write(json.toString().getBytes(Charset.forName("UTF-8")));
        } finally {
            out.close();
        }
    }
}

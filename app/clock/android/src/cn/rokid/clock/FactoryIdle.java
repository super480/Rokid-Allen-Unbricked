package cn.rokid.clock;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import org.json.JSONException;
import org.json.JSONObject;

/** Delegates to the original idle app installed in the firmware. */
final class FactoryIdle {
    private static final String PACKAGE = "com.rokid.idle";
    private static final ComponentName ACTIVITY =
            new ComponentName(PACKAGE, PACKAGE + ".DefaultIdleActivity");
    private static final Map<String, Integer> TYPES;

    static {
        Map<String, Integer> types = new LinkedHashMap<String, Integer>();
        types.put("factory_sunset", 3);
        types.put("factory_fantasy", 0);
        types.put("factory_bluesky", 1);
        types.put("factory_jellyfish", 2);
        TYPES = Collections.unmodifiableMap(types);
    }

    static Iterable<String> names() { return TYPES.keySet(); }

    static boolean handles(String name) { return TYPES.containsKey(name); }

    static boolean available(Context context) {
        try {
            return context.getPackageManager().getActivityInfo(ACTIVITY, 0).enabled;
        } catch (PackageManager.NameNotFoundException missing) {
            return false;
        }
    }

    private static Intent intent() {
        return new Intent("com.rokid.app.idle").setComponent(ACTIVITY);
    }

    static void show(Context context, String name) throws JSONException {
        final String action = "idle.start";
        JSONObject slots = new JSONObject();
        slots.put("type", String.valueOf(TYPES.get(name)));
        slots.put("idle.app.intent", action);
        slots.put("params", new JSONObject());
        JSONObject nlp = new JSONObject();
        nlp.put("domain", PACKAGE);
        nlp.put("intent", action);
        nlp.put("slots", slots);
        context.startActivity(intent().putExtra("nlp", nlp.toString())
                .putExtra("idle.app.intent", action));
    }

    static void close(Context context) {
        context.sendBroadcast(new Intent(ClockActivity.ACTION_FINISH).setPackage(PACKAGE));
    }
}

package cn.rokid.clock;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Instant face switching from the host:
 *   am broadcast -a <package>.SET_FACE --es face grid|digital|next|<sdcard name>
 * Writes config.json so the running activity (mtime poll) and the next
 * idle start both see the change.
 */
public class FaceReceiver extends BroadcastReceiver {

    public static final String ACTION_SET_FACE =
            "com.rokid.app.r5ef240ab9f64416fa0c9c69efd8e347e.SET_FACE";

    @Override
    public void onReceive(Context context, Intent intent) {
        String wanted = intent != null ? intent.getStringExtra("face") : null;
        if (wanted == null || wanted.equals("")) {
            return;
        }
        FaceRegistry.init(context);
        String current = FaceRegistry.readCurrentFace();
        String resolved = "next".equals(wanted) ? FaceRegistry.next(current) : wanted;
        if (!FaceRegistry.names().contains(resolved)) {
            return;
        }
        FaceRegistry.writeCurrentFace(resolved);
    }
}

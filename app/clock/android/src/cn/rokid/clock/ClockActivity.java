package cn.rokid.clock;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.Bundle;
import android.os.Handler;
import android.view.Window;
import android.view.WindowManager;

/**
 * The idle activity the RK idle service starts. Contract:
 *   in  - action = package name, extras nlp / idle.app.intent
 *   out - finish() on com.rokid.service.idle.FINISH_IDLE_ACTIVITY
 */
public class ClockActivity extends Activity {

    public static final String ACTION_IDLE =
            "com.rokid.app.r5ef240ab9f64416fa0c9c69efd8e347e";
    public static final String ACTION_FINISH =
            "com.rokid.service.idle.FINISH_IDLE_ACTIVITY";

    private final Handler handler = new Handler();
    private ClockView view;
    private BroadcastReceiver exitReceiver;
    private Runnable ticker;
    private long configStamp = -1L;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        requestWindowFeature(Window.FEATURE_NO_TITLE);
        getWindow().setFlags(WindowManager.LayoutParams.FLAG_FULLSCREEN,
                WindowManager.LayoutParams.FLAG_FULLSCREEN);

        FaceRegistry.init(this);
        view = new ClockView(this);
        setContentView(view);

        exitReceiver = new BroadcastReceiver() {
            @Override
            public void onReceive(Context context, Intent intent) {
                finish();
            }
        };
        registerReceiver(exitReceiver, new IntentFilter(ACTION_FINISH));

        ticker = new Runnable() {
            private int frame = 0;

            @Override
            public void run() {
                if (frame++ % 30 == 0) {
                    long stamp = FaceRegistry.configStamp();
                    if (stamp != configStamp) {
                        configStamp = stamp;
                        FaceRegistry.rescan();
                    }
                }
                view.invalidate();
                handler.postDelayed(this, 33L);
            }
        };
        handler.post(ticker);
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(ticker);
        if (exitReceiver != null) {
            unregisterReceiver(exitReceiver);
            exitReceiver = null;
        }
        super.onDestroy();
    }
}

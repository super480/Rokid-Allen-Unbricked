package cn.rokid.bridge;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

/**
 * A plain launcher screen: start the bridge, show the character, test the
 * speech. It never holds focus for long and never plays the role of the
 * character itself -- the character lives in the overlay.
 */
public final class MainActivity extends Activity {

    public void onCreate(Bundle state) {
        super.onCreate(state);
        dispatch(getIntent());
        render();
    }

    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        dispatch(intent);
    }

    private void dispatch(Intent intent) {
        if ("cn.rokid.bridge.PAIR".equals(intent.getAction())) {
            SetupChannel.open(getApplicationContext(), intent.getStringExtra("session"), new Runnable() {
                public void run() { runOnUiThread(new Runnable() {
                    public void run() { serviceCommand("reload", null); finish(); }
                }); }
            });
            return;
        }
        Intent service = new Intent(this, BridgeService.class);
        // The exported launcher cannot inject arbitrary tasks or speech.
        if ("reload".equals(intent.getStringExtra("command"))) {
            service.putExtra("command", "reload");
        }
        if (intent.getBooleanExtra("avatar", false)) {
            service.putExtra("command", "show_overlay");
        }
        startService(service);
    }

    private void render() {
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(1);
        layout.setPadding(24, 16, 24, 16);

        TextView title = new TextView(this);
        title.setText("若琪助手 · 设置与验证");
        title.setTextSize(24);
        layout.addView(title);

        TextView info = new TextView(this);
        info.setText("喊「若琪」→ 说「找小赫」→ 说任务。\n"
                + "主机配置写入应用私有 config.json 后生效。");
        layout.addView(info);

        button(layout, "显示形象", new View.OnClickListener() {
            public void onClick(View view) {
                serviceCommand("show_overlay", null);
                finish();
            }
        });
        button(layout, "播报测试", new View.OnClickListener() {
            public void onClick(View view) {
                serviceCommand("speak", "你好，我是若琪助手。语音播报测试。");
            }
        });
        button(layout, "重新读取主机配置", new View.OnClickListener() {
            public void onClick(View view) {
                serviceCommand("reload", null);
            }
        });
        button(layout, "停止助手", new View.OnClickListener() {
            public void onClick(View view) {
                stopService(new Intent(MainActivity.this, BridgeService.class));
                finish();
            }
        });

        setContentView(layout);
    }

    private void serviceCommand(String command, String text) {
        Intent intent = new Intent(this, BridgeService.class);
        intent.putExtra("command", command);
        if (text != null) intent.putExtra("text", text);
        startService(intent);
    }

    private void button(LinearLayout layout, String text, View.OnClickListener listener) {
        Button button = new Button(this);
        button.setText(text);
        button.setOnClickListener(listener);
        layout.addView(button);
    }
}

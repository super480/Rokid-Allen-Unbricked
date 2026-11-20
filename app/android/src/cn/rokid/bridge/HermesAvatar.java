package cn.rokid.bridge;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.os.SystemClock;

/**
 * Hermes on the 640x480 screen.
 *
 * The drawing is a direct port of the approved native reference in
 * {@code design/motion/hermes/reference/android/PreviewActivity.java}. The
 * motion model, eye geometry and layout anchors are unchanged; only the host
 * is different: this runs inside the non-focus overlay instead of a demo
 * Activity.
 *
 * The character is one transparent 512x512 plate ({@code hermes-blank-512.png})
 * plus eyes drawn on the canvas, which keeps the APK small and lets the eyes
 * animate.
 */
final class HermesAvatar {

    /** Screen states, in the order the contract defines them. */
    static final String[] STATES = {
            "idle", "listening", "thinking", "working",
            "speaking", "confirmation", "done", "error"
    };

    /** Caption and hint text per state, matching design/motion/hermes/states.json. */
    static final String[] CAPTIONS = {
            "小赫 在这里",
            "我在听，请说",
            "让我想一想",
            "正在处理你的任务",
            "正在向你汇报",
            "需要你确认一下",
            "任务完成啦",
            "遇到一点问题",
    };

    static final String[] DETAILS = {
            "随时可以叫我",
            "",
            "正在理清思路",
            "",
            "",
            "请在电脑上查看",
            "",
            "我们换个办法试试",
    };

    /** Background of the Hermes character, as approved on the device. */
    static final int BACKGROUND = Color.BLACK;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Path path = new Path();
    private final RectF rect = new RectF();
    private final Bitmap plate;

    /** Bob amplitude and squash amount per state. */
    private static final float[] AMPLITUDES = {8, 3, 4, 2, 4, 1.5f, 7, 2};
    private static final float[] SQUASHES = {.016f, .008f, .009f, .007f, .012f, 0, .021f, .006f};

    HermesAvatar(Context context) {
        Bitmap loaded = null;
        try {
            loaded = BitmapFactory.decodeStream(context.getAssets().open("hermes-blank-512.png"));
        } catch (Exception ignored) {
        }
        plate = loaded;
    }

    boolean ready() {
        return plate != null;
    }

    /** Map a screen state name to its index; unknown states fall back to idle. */
    static int indexOf(String state) {
        if (state == null) return 0;
        for (int i = 0; i < STATES.length; i++) {
            if (STATES[i].equals(state)) return i;
        }
        return 0;
    }

    /**
     * Draw the character.
     *
     * @param canvas   already scaled to the 640x480 coordinate space
     * @param state    one of {@link #STATES}
     * @param epochMs  when the current state started, so the animation loops
     *                 from the beginning on every change
     */
    void draw(Canvas canvas, String state, long epochMs) {
        if (plate == null) return;
        final int index = indexOf(state);

        float time = (SystemClock.uptimeMillis() - epochMs) / 1000f;
        float t = time % 6;
        float p = (float) (2 * Math.PI * t / 6);

        float bob = -AMPLITUDES[index] * sin(2 * p) + 1.4f * sin(4 * p);
        float q = SQUASHES[index] * (float) Math.cos(2 * p + .3f);
        if (index == 6) {
            float b = pulse(t, .8f, .45f) + .35f * pulse(t, 1.6f, .35f);
            bob -= 14 * b;
            q += .025f * b;
        }
        if (index == 7) bob += 1.8f * pulse(t, 1.1f, .14f) - 1.8f * pulse(t, 1.3f, .14f);

        float scale = index == 1 ? .765f : .75f;

        canvas.save();
        canvas.translate(320 + (index == 0 ? 3 * sin(p) : 0), 202 + bob * .6f);
        canvas.scale(scale * (1 + q), scale * (1 - q * .85f));
        canvas.translate(-256, -256);

        paint.setColor(Color.WHITE);
        paint.setStyle(Paint.Style.FILL);
        canvas.drawBitmap(plate, 0, 0, paint);

        float gx = 2.5f * sin(p), gy = .8f * sin(2 * p);
        float w = 22, h1 = 52, h2 = 52;

        if (index == 0) {
            // The idle clock is the continuous time in this state, not the
            // 6-second loop time, so the 15-second glance keeps its rhythm.
            float[] look = look(time);
            gx = look[0];
            gy = look[1];
        }
        if (index == 3) {
            gx = 7 * (float) Math.tanh(3 * sin(3 * p));
            gy = 1;
            w = 23;
            h1 = h2 = 35;
        }
        if (index == 4) {
            gx = 1.5f * sin(p);
            gy = -1;
            h1 = 43 + 7 * sin(9 * p);
            h2 = 43 + 7 * sin(9 * p + .2f);
        }

        // Idle blinks once every 6 seconds; the other states keep the
        // original two blinks per 6-second loop.
        float blink = 1 - .93f * (index == 0
                ? pulse(t, 3, .105f)
                : Math.max(pulse(t, 1.85f, .105f), pulse(t, 5.08f, .105f)));

        paint.setColor(Color.rgb(41, 37, 31));
        if (index == 6) {
            arc(canvas, 179, 328);
            arc(canvas, 289, 338);
        } else if (index == 1) {
            eye(canvas, 179, 329, 24, Math.max(4, 60 * blink));
            eye(canvas, 289, 339, 24, Math.max(4, 60 * blink));
        } else if (index == 5) {
            oval(canvas, 179, 328, 14, 14 * blink);
            oval(canvas, 289, 338, 14, 14 * blink);
        } else if (index == 7) {
            float k = 1 + .07f * sin(2 * p), j = 1.4f * sin(6 * p);
            chevron(canvas, 189 + j, 330, k, false);
            chevron(canvas, 279 + j, 340, k, true);
        } else if (index == 2) {
            float shrink = smooth((t - .1f) / .45f) * (1 - smooth((t - 5.5f) / .5f));
            float join = smooth((t - .65f) / .5f) * (1 - smooth((t - 5.1f) / .4f));
            dot(canvas, 179 + 22 * join, 330 + 3 * join, 0, join, shrink, p);
            dot(canvas, 289 - 20 * join, 340 - 3 * join, 2, join, shrink, p);
            paint.setAlpha(Math.round(255 * join));
            dot(canvas, 235, 335, 1, join, shrink, p);
            paint.setAlpha(255);
        } else {
            eye(canvas, 179 + gx, 330 + gy, w, Math.max(4, h1 * blink));
            eye(canvas, 289 + gx, 340 + gy, w, Math.max(4, h2 * blink));
        }

        canvas.restore();
        paint.setStyle(Paint.Style.FILL);
    }

    // -- shape helpers --------------------------------------------------------

    private void eye(Canvas canvas, float x, float y, float w, float h) {
        rect.set(x - w / 2, y - h / 2, x + w / 2, y + h / 2);
        float r = Math.min(w, h) / 2;
        canvas.drawRoundRect(rect, r, r, paint);
    }

    private void oval(Canvas canvas, float x, float y, float rx, float ry) {
        rect.set(x - rx, y - ry, x + rx, y + ry);
        canvas.drawOval(rect, paint);
    }

    private void dot(Canvas canvas, float x, float y, int i, float join, float shrink, float p) {
        float v = Math.max(0, sin(3 * p - i * 2 * Math.PI / 3));
        v *= v;
        float d = 15 + 2 * join * (1 + 1.3f * v);
        eye(canvas, x, y - 7 * v * join, 22 + (d - 22) * shrink, 52 + (d - 52) * shrink);
    }

    private void chevron(Canvas canvas, float x, float y, float k, boolean right) {
        float sign = right ? -1 : 1;
        path.reset();
        path.moveTo(x - sign * 13 * k, y - 14 * k);
        path.lineTo(x + sign * 10 * k, y);
        path.lineTo(x - sign * 13 * k, y + 14 * k);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(8);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
        canvas.drawPath(path, paint);
        paint.setStyle(Paint.Style.FILL);
    }

    private void arc(Canvas canvas, float x, float y) {
        path.reset();
        path.moveTo(x - 17.5f, y + 6.5f);
        path.quadTo(x, y - 16.9f, x + 17.5f, y + 6.5f);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(10);
        paint.setStrokeCap(Paint.Cap.ROUND);
        canvas.drawPath(path, paint);
        paint.setStyle(Paint.Style.FILL);
    }

    // -- motion helpers -------------------------------------------------------

    private static float sin(double v) {
        return (float) Math.sin(v);
    }

    private static float pulse(float t, float c, float w) {
        float d = Math.abs(t - c) / w;
        return d < 1 ? (1 + (float) Math.cos(Math.PI * d)) / 2 : 0;
    }

    private static float smooth(float v) {
        v = Math.max(0, Math.min(1, v));
        return v * v * (3 - 2 * v);
    }

    /** Idle gaze path: face forward, glance left every 15 seconds, return. */
    private static float[] look(float t) {
        t = timeModulo(t, 15);
        float[][] keys = {
                {0, 0, 0}, {13, 0, 0}, {13.4f, -14, 0}, {14.2f, -14, 0},
                {14.6f, 0, 0}, {15, 0, 0}
        };
        for (int i = 1; i < keys.length; i++) {
            if (t <= keys[i][0]) {
                float a = smooth((t - keys[i - 1][0]) / (keys[i][0] - keys[i - 1][0]));
                return new float[]{
                        keys[i - 1][1] + (keys[i][1] - keys[i - 1][1]) * a,
                        keys[i - 1][2] + (keys[i][2] - keys[i - 1][2]) * a
                };
            }
        }
        return new float[]{0, 0};
    }

    /** A positive modulo, so a negative time still lands inside the period. */
    private static float timeModulo(float t, float period) {
        return (t % period + period) % period;
    }
}

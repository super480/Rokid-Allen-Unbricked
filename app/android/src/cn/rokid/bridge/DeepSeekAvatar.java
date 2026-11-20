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
 * The DeepSeek character ("大肥鱼").
 *
 * The geometry was measured pixel by pixel from the eight state sheets in
 * design/avatars/deepseek/states/, so the eyes sit exactly where the
 * designer put them. The blank plate already carries the hair, the head
 * dress and the bow -- only the eyes are drawn live, which keeps the
 * packaged asset down to a single image.
 *
 * The state system is the same template the Hermes character uses, so the
 * anchors are shared: the idle eyes are at (178,330) and (288,340) in the
 * plate's own coordinates, and the listening, working, speaking and
 * confirmation shapes match as well. Only "thinking" and "error" differ,
 * and they are drawn from their own measurements.
 */
class DeepSeekAvatar {
    static final String[] STATES = {
            "idle", "listening", "thinking", "working",
            "speaking", "confirmation", "done", "error"
    };

    /** Caption and hint text per state, matching the DeepSeek design. */
    static final String[] CAPTIONS = {
            "大肥鱼在这里",
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

    static final int BACKGROUND = Color.rgb(12, 14, 18);

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Path path = new Path();
    private final RectF rect = new RectF();
    private final Bitmap plate;

    /** Bob amplitude and squash amount per state. */
    private static final float[] AMPLITUDES = {7, 3, 4, 2.5f, 4, 1.5f, 6, 2};
    private static final float[] SQUASHES = {.014f, .008f, .009f, .007f, .012f, 0, .019f, .006f};

    DeepSeekAvatar(Context context) {
        Bitmap loaded = null;
        try {
            loaded = BitmapFactory.decodeStream(
                    context.getAssets().open("deepseek-blank-512.png"));
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
            bob -= 12 * b;
            q += .022f * b;
        }
        if (index == 7) bob += 1.8f * pulse(t, 1.1f, .14f) - 1.8f * pulse(t, 1.3f, .14f);

        float scale = index == 1 ? .765f : .75f;

        canvas.save();
        canvas.translate(320, 202 + bob * .6f);
        canvas.scale(scale * (1 + q), scale * (1 - q * .85f));
        canvas.translate(-256, -256);

        paint.setColor(Color.WHITE);
        paint.setStyle(Paint.Style.FILL);
        canvas.drawBitmap(plate, 0, 0, paint);

        paint.setColor(Color.rgb(41, 37, 31));
        drawEyes(canvas, index, time, t, p);

        canvas.restore();
        paint.setStyle(Paint.Style.FILL);
    }

    // -- the eyes -------------------------------------------------------------

    /**
     * The eyes of one state.
     *
     * The anchors are the ones measured from the design sheets. The idle
     * gaze drifts and blinks; the working eyes sweep sideways while a task
     * runs; the speaking eyes move with the answer.
     */
    private void drawEyes(Canvas canvas, int index, float time, float t, float p) {
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
            w = 22;
            h1 = h2 = 34;
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

        if (index == 2) {
            // Thinking: three upright spindles, drifting slowly sideways.
            float drift = 2.5f * sin(p);
            spindle(canvas, 199.5f + drift, 333, 14, 17);
            spindle(canvas, 233f - drift * .5f, 333, 14, 17);
            spindle(canvas, 268.5f - drift, 333, 14, 17);
            return;
        }
        if (index == 5) {
            // Confirmation: a solid rounded shape, both eyes the same size.
            oval(canvas, 178, 328, 14, 14);
            oval(canvas, 288, 338, 14, 14);
            return;
        }
        if (index == 6) {
            // Done: an upward arch, like a happy eye.
            arch(canvas, 178, 316, 19, 25);
            arch(canvas, 288, 326, 19, 25);
            return;
        }
        if (index == 7) {
            // Error: a cross, leaning in towards the middle.
            cross(canvas, 186, 329, 16, 19);
            cross(canvas, 278, 339, 16, 19);
            return;
        }

        eye(canvas, 178 + gx, 330 + gy, w, Math.max(4, h1 * blink));
        eye(canvas, 288 + gx, 340 + gy, w, Math.max(4, h2 * blink));
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

    /** An upright spindle: narrow at the ends, wider in the middle. */
    private void spindle(Canvas canvas, float x, float y, float w, float h) {
        rect.set(x - w / 2, y - h / 2, x + w / 2, y + h / 2);
        canvas.drawOval(rect, paint);
    }

    /** An upward arch, drawn as two strokes meeting at the top. */
    private void arch(Canvas canvas, float x, float y, float halfWidth, float height) {
        path.reset();
        path.moveTo(x - halfWidth, y + height);
        path.quadTo(x, y - 4, x + halfWidth, y + height);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(9);
        paint.setStrokeCap(Paint.Cap.ROUND);
        canvas.drawPath(path, paint);
        paint.setStyle(Paint.Style.FILL);
    }

    /** A cross of two strokes. */
    private void cross(Canvas canvas, float x, float y, float halfWidth, float halfHeight) {
        path.reset();
        path.moveTo(x - halfWidth, y - halfHeight);
        path.lineTo(x + halfWidth, y + halfHeight);
        path.moveTo(x + halfWidth, y - halfHeight);
        path.lineTo(x - halfWidth, y + halfHeight);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(7);
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

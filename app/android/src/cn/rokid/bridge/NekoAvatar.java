package cn.rokid.bridge;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.os.SystemClock;

/**
 * The catgirl ("猫娘"), drawn from Codex's approved V10 artwork.
 *
 * Like the other assistant characters, only a single blank plate is shipped;
 * the eyes are drawn live so the eight states keep blinking, glancing and
 * bouncing. Two things are specific to this character and come straight
 * from design/motion/neko/character.json (version 2.0.0, user-approved):
 *
 *  - eye_transform [0.872, 0, 0, 0.966, 3.7, 11.3] maps the shared
 *    template-v2 eye paths onto the approved face. It is applied to the
 *    eyes only; the plate itself is never scaled or moved by it.
 *  - stage_scale 1.15 enlarges the whole stage, to compensate for the long
 *    twin tails reducing the face's share of the picture.
 *
 * The state model is a direct port of design/motion/template-v2/motion.js,
 * so the on-device animation matches the exported posters and GIFs.
 */
class NekoAvatar {
    static final String[] STATES = {
            "idle", "listening", "thinking", "working",
            "speaking", "confirmation", "done", "error"
    };

    static final String[] CAPTIONS = {
            "N.E.K.O. 在这里",
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

    /** The approved plate sits on a pure black stage. */
    static final int BACKGROUND = Color.rgb(0, 0, 0);

    private static final float STAGE_SCALE = 1.15f;
    private static final float[] AMPLITUDES = {8, 3, 4, 2, 4, 1.5f, 7, 2};
    private static final float[] SQUASHES = {
            .016f, .008f, .009f, .007f, .012f, 0, .021f, .006f
    };

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Path path = new Path();
    private final RectF rect = new RectF();
    private final Matrix eyeTransform = new Matrix();
    private final Bitmap plate;

    NekoAvatar(Context context) {
        Bitmap loaded = null;
        try {
            loaded = BitmapFactory.decodeStream(
                    context.getAssets().open("neko-blank-512.png"));
        } catch (Exception ignored) {
        }
        plate = loaded;
        // SVG matrix(a,b,c,d,e,f) in the same order: x'=a*x+c*y+e, y'=b*x+d*y+f.
        eyeTransform.setValues(new float[]{
                .872f, 0, 3.7f,
                0, .966f, 11.3f,
                0, 0, 1
        });
        android.util.Log.i("RokidBridge", "avatar: neko plate "
                + (plate != null ? "ok" : "missing"));
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

        float y = -AMPLITUDES[index] * sin(2 * p) + 1.4f * sin(4 * p);
        float squash = SQUASHES[index] * (float) Math.cos(2 * p + .3f);
        if (index == 6) {
            float b = pulse(t, .8f, .45f) + .35f * pulse(t, 1.6f, .35f);
            y -= 14 * b;
            squash += .025f * b;
        }
        if (index == 7) {
            y += 1.8f * pulse(t, 1.1f, .14f) - 1.8f * pulse(t, 1.3f, .14f);
        }
        float x = index == 0 ? 3 * sin(p) : 0;
        float scale = (index == 1 ? .765f : .75f) * STAGE_SCALE;

        canvas.save();
        canvas.translate(320 + x, 202 + y * .6f);
        canvas.scale(scale * (1 + squash), scale * (1 - squash * .85f));
        canvas.translate(-256, -256);

        paint.setColor(Color.WHITE);
        paint.setStyle(Paint.Style.FILL);
        paint.setAlpha(255);
        canvas.drawBitmap(plate, 0, 0, paint);

        // Only the eye paths are transformed, not the face plate.
        canvas.save();
        canvas.concat(eyeTransform);
        paint.setColor(Color.rgb(41, 37, 31));
        drawEyes(canvas, index, time, t, p);
        canvas.restore();

        canvas.restore();
        paint.setStyle(Paint.Style.FILL);
        paint.setAlpha(255);
    }

    // -- the eyes -------------------------------------------------------------

    /** Port of WorkBuddyMotion.frame(): shared eye anchors are (179,330)/(289,340). */
    private void drawEyes(Canvas canvas, int index, float time, float t, float p) {
        float gx = 2.5f * sin(p), gy = .8f * sin(2 * p);
        float w1 = 22, w2 = 22, h1 = 52, h2 = 52;

        if (index == 0) {
            float[] look = look(time);
            gx = look[0];
            gy = look[1];
        }
        if (index == 1) {
            gx = 0;
            gy = -1;
            w1 = w2 = 24;
            h1 = h2 = 60;
        }
        if (index == 3) {
            gx = 7 * (float) Math.tanh(3 * sin(3 * p));
            gy = 1;
            w1 = w2 = 23;
            h1 = h2 = 35;
        }
        if (index == 4) {
            gx = 1.5f * sin(p);
            gy = -1;
            h1 = 43 + 7 * sin(9 * p);
            h2 = 43 + 7 * sin(9 * p + .2f);
        }

        float blink = 1 - .93f * (index == 0
                ? pulse(t, 3, .105f)
                : Math.max(pulse(t, 1.85f, .105f), pulse(t, 5.08f, .105f)));

        if (index == 6) {           // done: two upward arcs
            arc(canvas, 179, 328, 35, 13);
            arc(canvas, 289, 338, 35, 13);
            return;
        }
        if (index == 5) {           // confirmation: two round eyes
            oval(canvas, 179, 328, 14, Math.max(4, 14 * blink));
            oval(canvas, 289, 338, 14, Math.max(4, 14 * blink));
            return;
        }
        if (index == 7) {           // error: two inward chevrons
            float k = 1 + .07f * sin(2 * p), j = 1.4f * sin(6 * p);
            chevron(canvas, 189 - 13 * k + j, 330, 189 + 10 * k + j,
                    330 - 14 * k, 330 + 14 * k);
            chevron(canvas, 279 + 13 * k + j, 340, 279 - 10 * k + j,
                    340 - 14 * k, 340 + 14 * k);
            return;
        }
        if (index == 2) {           // thinking: three dots that merge and bounce
            float shrink = smooth((t - .1f) / .45f) * (1 - smooth((t - 5.5f) / .5f));
            float join = smooth((t - .65f) / .5f) * (1 - smooth((t - 5.1f) / .4f));
            thinkingDot(canvas, 179 + 22 * join, 330 + 3 * join, 0,
                        join, shrink, beat(0, p), 1f);
            thinkingDot(canvas, 289 - 20 * join, 340 - 3 * join, 2,
                        join, shrink, beat(2, p), 1f);
            thinkingDot(canvas, 235, 335, 1, join, shrink, beat(1, p), join);
            return;
        }

        capsule(canvas, 179 + gx, 330 + gy, w1, Math.max(4, h1 * blink));
        capsule(canvas, 289 + gx, 340 + gy, w2, Math.max(4, h2 * blink));
    }

    private void thinkingDot(Canvas canvas, float cx, float cy, int i,
                             float join, float shrink, float beat, float opacity) {
        float diameter = 15 + 2 * join * (1 + 1.3f * beat);
        float w = 22 + (diameter - 22) * shrink;
        float h = 52 + (diameter - 52) * shrink;
        paint.setAlpha(Math.round(255 * Math.max(0, Math.min(1, opacity))));
        capsule(canvas, cx, cy - 7 * beat * join, w, Math.max(4, h));
        paint.setAlpha(255);
    }

    private static float beat(int i, float p) {
        float v = (float) Math.max(0, Math.sin(3 * p - i * 2 * Math.PI / 3));
        return v * v;
    }

    // -- shape helpers --------------------------------------------------------

    /** A vertical capsule: the shared template's basic eye. */
    private void capsule(Canvas canvas, float cx, float cy, float w, float h) {
        rect.set(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2);
        float r = Math.min(w, h) / 2;
        canvas.drawRoundRect(rect, r, r, paint);
    }

    private void oval(Canvas canvas, float cx, float cy, float rx, float ry) {
        rect.set(cx - rx, cy - ry, cx + rx, cy + ry);
        canvas.drawOval(rect, paint);
    }

    /** An upward quadratic arc (happy eye), drawn as a stroke. */
    private void arc(Canvas canvas, float cx, float cy, float w, float h) {
        path.reset();
        path.moveTo(cx - w / 2, cy + h / 2);
        path.quadTo(cx, cy - 1.3f * h, cx + w / 2, cy + h / 2);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(10);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
        canvas.drawPath(path, paint);
        paint.setStyle(Paint.Style.FILL);
    }

    /** A two-stroke chevron, used for the confused error eyes. */
    private void chevron(Canvas canvas, float tipX, float cy, float backX,
                         float topY, float bottomY) {
        path.reset();
        path.moveTo(tipX, topY);
        path.lineTo(backX, cy);
        path.lineTo(tipX, bottomY);
        paint.setStyle(Paint.Style.STROKE);
        paint.setStrokeWidth(8);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStrokeJoin(Paint.Join.ROUND);
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

    private static float timeModulo(float t, float period) {
        return (t % period + period) % period;
    }
}

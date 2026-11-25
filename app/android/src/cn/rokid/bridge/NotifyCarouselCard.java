package cn.rokid.bridge;

import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RadialGradient;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.util.Base64;

import org.json.JSONArray;
import org.json.JSONObject;

import java.util.ArrayList;
import java.util.List;

/**
 * 1:1 Android Canvas port of design/mac-notification-carousel/index.html.
 *
 * 640x480 logical coordinate space:
 * - Dual radial dark gradient backdrop + "有" (left) and "通知" (right) words
 * - Vertical spring-eased icon reel (106x106 icons, 116px pitch) where
 *   slot 0 is always Calendar and slot 1 is always Reminders, scrolling into
 *   the center frame so the active notification's app lands inside the frame
 * - 4-corner pulsing reticle frame (#929aff, 124x124)
 * - Bottom pill label with #929aff dot and cross-fading app name
 */
final class NotifyCarouselCard {

    static final float WIDTH = 640f;
    static final float HEIGHT = 480f;
    static final long BEAT_MS = 680L;
    static final long FINAL_TRAVEL_MS = 720L;
    private static final float PULSE_MS = 470f;

    interface AssetLoader {
        Bitmap load(String assetName);
    }

    private static final class Slot {
        final String name;
        final Bitmap bitmap;
        final boolean owned;

        Slot(String name, Bitmap bitmap, boolean owned) {
            this.name = name;
            this.bitmap = bitmap;
            this.owned = owned;
        }
    }

    private final AssetLoader assetLoader;
    private final List<Slot> slots = new ArrayList<Slot>();
    private int targetIndex = 0;
    private int totalSteps = 1;
    private long totalDurationMs = FINAL_TRAVEL_MS;

    private final Paint wordPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint iconPaint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Paint framePaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint frameGlowPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint pillBgPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint pillBorderPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint dotPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint labelPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fallbackBgPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint fallbackTextPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF iconRect = new RectF();
    private final RectF pillRect = new RectF();

    NotifyCarouselCard(AssetLoader assetLoader) {
        this.assetLoader = assetLoader;

        wordPaint.setColor(0xFFF5F6FC);
        wordPaint.setTextSize(46f);
        wordPaint.setTypeface(Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD));

        framePaint.setStyle(Paint.Style.STROKE);
        framePaint.setStrokeWidth(4f);
        framePaint.setStrokeCap(Paint.Cap.ROUND);
        framePaint.setStrokeJoin(Paint.Join.ROUND);
        framePaint.setColor(0xFF929AFF);

        frameGlowPaint.setStyle(Paint.Style.STROKE);
        frameGlowPaint.setStrokeWidth(9f);
        frameGlowPaint.setStrokeCap(Paint.Cap.ROUND);
        frameGlowPaint.setStrokeJoin(Paint.Join.ROUND);
        frameGlowPaint.setColor(0x38858DFF);

        pillBgPaint.setStyle(Paint.Style.FILL);
        pillBgPaint.setColor(0xF517181E);

        pillBorderPaint.setStyle(Paint.Style.STROKE);
        pillBorderPaint.setStrokeWidth(1.2f);
        pillBorderPaint.setColor(0xFF434553);

        dotPaint.setStyle(Paint.Style.FILL);
        dotPaint.setColor(0xFF929AFF);

        labelPaint.setColor(0xFFD9DAE7);
        labelPaint.setTextSize(17f);
        labelPaint.setTypeface(Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD));

        fallbackBgPaint.setStyle(Paint.Style.FILL);
        fallbackBgPaint.setColor(0xFF2B3152);

        fallbackTextPaint.setColor(0xFFF5F6FC);
        fallbackTextPaint.setTextSize(42f);
        fallbackTextPaint.setTextAlign(Paint.Align.CENTER);
        fallbackTextPaint.setTypeface(Typeface.create(Typeface.SANS_SERIF, Typeface.BOLD));
    }

    void prepare(Canvas backdropCanvas, JSONObject card) {
        release();
        drawBackdrop(backdropCanvas);

        JSONArray items = card != null ? card.optJSONArray("items") : null;
        if (items != null) {
            for (int i = 0; i < items.length(); i++) {
                JSONObject it = items.optJSONObject(i);
                if (it == null) continue;
                String name = it.optString("name", "");
                String asset = it.optString("asset", "");
                String b64 = it.optString("icon_b64", "");
                Bitmap bmp = null;
                boolean owned = false;
                if (!b64.isEmpty()) {
                    try {
                        byte[] bytes = Base64.decode(b64, Base64.DEFAULT);
                        bmp = BitmapFactory.decodeByteArray(bytes, 0, bytes.length);
                        owned = bmp != null;
                    } catch (Throwable ignored) {
                    }
                }
                if (bmp == null && !asset.isEmpty() && assetLoader != null) {
                    bmp = assetLoader.load(asset);
                    owned = bmp != null;
                }
                slots.add(new Slot(name, bmp, owned));
            }
        }
        if (slots.isEmpty()) {
            slots.add(new Slot("日历", assetLoader != null ? assetLoader.load("notify_calendar.png") : null, true));
            slots.add(new Slot("提醒事项", assetLoader != null ? assetLoader.load("notify_reminders.png") : null, true));
        }
        int n = slots.size();
        int rawTarget = card != null ? card.optInt("target_index", 0) : 0;
        targetIndex = ((rawTarget % n) + n) % n;
        // 只滚动一次（1 步，720ms）：从 targetIndex - 1 弹入 targetIndex，停稳在中间选框。
        totalSteps = 1;
        totalDurationMs = FINAL_TRAVEL_MS;
    }

    long durationMs() {
        return totalDurationMs;
    }

    void drawForeground(Canvas canvas, long elapsedMs) {
        int n = slots.size();
        if (n == 0) return;

        long clamped = Math.max(0L, Math.min(elapsedMs, totalDurationMs));
        float startPos = (float) (targetIndex - 1);
        float position;
        float pulse = 0f;
        if (clamped >= totalDurationMs) {
            position = (float) targetIndex;
            pulse = 0f;
        } else {
            float t = Math.min(1f, Math.max(0f, clamped / (float) FINAL_TRAVEL_MS));
            position = startPos + spring(t);
            float pulseT = Math.min(1f, clamped / PULSE_MS);
            pulse = (float) Math.sin(Math.PI * pulseT);
        }

        // 绘制垂直滚动图标（与设计稿一致：106x106、步距 116px、缩放与淡出随距离衰减）
        for (int i = 0; i < n; i++) {
            float diff = i - position;
            float offset = diff - n * Math.round(diff / (float) n);
            float dist = Math.abs(offset);
            float alpha = Math.max(0f, 1f - dist * 0.67f);
            if (alpha <= 0.01f) continue;
            float scale = Math.max(0.5f, 1f - dist * 0.38f);
            float cy = 240f + offset * 116f;
            float half = 53f * scale;
            Slot s = slots.get(i);
            int a = Math.round(alpha * 255f);
            iconRect.set(320f - half, cy - half, 320f + half, cy + half);
            if (s.bitmap != null && !s.bitmap.isRecycled()) {
                iconPaint.setAlpha(a);
                canvas.drawBitmap(s.bitmap, null, iconRect, iconPaint);
                iconPaint.setAlpha(255);
            } else {
                drawFallbackIcon(canvas, s.name, iconRect, a, scale);
            }
        }

        // 中间四角选框（inset: 178px 258px -> 124x124，随切换脉冲放大 1.07x）
        drawReticle(canvas, 1f + 0.07f * pulse);

        // 底部胶囊标签（top 321px，蓝紫圆点 + 居中文字，切换淡入淡出）
        int nearest = Math.round(position);
        int labelIdx = ((nearest % n) + n) % n;
        float labelAlpha = Math.max(0f, 1f - Math.abs(nearest - position) * 2.2f);
        if (labelAlpha > 0.01f) {
            drawPillLabel(canvas, slots.get(labelIdx).name, labelAlpha);
        }
    }

    private void drawBackdrop(Canvas canvas) {
        canvas.drawColor(0xFF050608);

        Paint p2 = new Paint(Paint.ANTI_ALIAS_FLAG);
        p2.setShader(new RadialGradient(
                WIDTH * 0.58f, HEIGHT * 0.66f, 420f,
                new int[] { 0xFF17192B, 0xFF08090C, 0xFF050608 },
                new float[] { 0f, 0.67f, 1f },
                Shader.TileMode.CLAMP));
        canvas.drawRect(0f, 0f, WIDTH, HEIGHT, p2);

        Paint p1 = new Paint(Paint.ANTI_ALIAS_FLAG);
        p1.setShader(new RadialGradient(
                WIDTH * 0.44f, HEIGHT * 0.32f, 360f,
                new int[] { 0xFF222531, 0x00222531 },
                new float[] { 0f, 0.61f },
                Shader.TileMode.CLAMP));
        canvas.drawRect(0f, 0f, WIDTH, HEIGHT, p1);

        // 左右两侧固定文字「有」和「通知」
        Paint.FontMetrics fm = wordPaint.getFontMetrics();
        float baselineY = 240f - (fm.ascent + fm.descent) * 0.5f;
        wordPaint.setTextAlign(Paint.Align.RIGHT);
        canvas.drawText("有", 640f - 406f, baselineY, wordPaint);
        wordPaint.setTextAlign(Paint.Align.LEFT);
        canvas.drawText("通知", 406f, baselineY, wordPaint);
    }

    private void drawReticle(Canvas canvas, float scale) {
        canvas.save();
        canvas.scale(scale, scale, 320f, 240f);
        float l = 258f, t = 178f, r = 382f, b = 302f;
        float arm = 32f;
        drawCorners(canvas, l, t, r, b, arm, frameGlowPaint);
        drawCorners(canvas, l, t, r, b, arm, framePaint);
        canvas.restore();
    }

    private static void drawCorners(Canvas c, float l, float t, float r, float b, float arm, Paint p) {
        // 左上
        c.drawLine(l, t + arm, l, t, p);
        c.drawLine(l, t, l + arm, t, p);
        // 右上
        c.drawLine(r - arm, t, r, t, p);
        c.drawLine(r, t, r, t + arm, p);
        // 右下
        c.drawLine(r, b - arm, r, b, p);
        c.drawLine(r, b, r - arm, b, p);
        // 左下
        c.drawLine(l + arm, b, l, b, p);
        c.drawLine(l, b, l, b - arm, p);
    }

    private void drawPillLabel(Canvas canvas, String text, float alpha) {
        if (text == null || text.isEmpty()) return;
        int a = Math.round(alpha * 255f);
        float textW = labelPaint.measureText(text);
        float dotD = 6f;
        float gap = 8f;
        float padH = 14f;
        float pillW = padH * 2f + dotD + gap + textW;
        float pillH = 30f;
        float left = 320f - pillW * 0.5f;
        float top = 321f;
        pillRect.set(left, top, left + pillW, top + pillH);

        pillBgPaint.setAlpha(Math.round(alpha * 0xF5));
        pillBorderPaint.setAlpha(a);
        dotPaint.setAlpha(a);
        labelPaint.setAlpha(a);

        canvas.drawRoundRect(pillRect, 20f, 20f, pillBgPaint);
        canvas.drawRoundRect(pillRect, 20f, 20f, pillBorderPaint);

        float cy = top + pillH * 0.5f;
        canvas.drawCircle(left + padH + dotD * 0.5f, cy, dotD * 0.5f, dotPaint);

        Paint.FontMetrics fm = labelPaint.getFontMetrics();
        float textY = cy - (fm.ascent + fm.descent) * 0.5f;
        labelPaint.setTextAlign(Paint.Align.LEFT);
        canvas.drawText(text, left + padH + dotD + gap, textY, labelPaint);
    }

    private void drawFallbackIcon(Canvas canvas, String name, RectF rect, int alpha, float scale) {
        fallbackBgPaint.setAlpha(alpha);
        canvas.drawRoundRect(rect, 22f * scale, 22f * scale, fallbackBgPaint);
        String ch = (name != null && !name.isEmpty()) ? name.substring(0, 1) : "?";
        fallbackTextPaint.setAlpha(alpha);
        fallbackTextPaint.setTextSize(42f * scale);
        Paint.FontMetrics fm = fallbackTextPaint.getFontMetrics();
        float cy = rect.centerY() - (fm.ascent + fm.descent) * 0.5f;
        canvas.drawText(ch, rect.centerX(), cy, fallbackTextPaint);
    }

    private static float spring(float t) {
        if (t >= 1f) return 1f;
        if (t <= 0f) return 0f;
        return (float) (1.0 - Math.exp(-6.0 * t) * (Math.cos(8.0 * t) + 0.75 * Math.sin(8.0 * t)));
    }

    void release() {
        for (Slot s : slots) {
            if (s.owned && s.bitmap != null && !s.bitmap.isRecycled()) {
                s.bitmap.recycle();
            }
        }
        slots.clear();
    }
}

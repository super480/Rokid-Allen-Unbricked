package cn.rokid.bridge;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffColorFilter;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.text.TextPaint;
import android.text.TextUtils;

import org.json.JSONObject;

/** One foreground-only feedback page centred on the 640×480 projection. */
final class HomeFeedbackCard {
    private static final float CENTER_X = 320f;
    private static final float CENTER_Y = 240f;
    private static final float ICON_SIZE = 147f;
    private static final float CAPTION_WIDTH = 308f;
    private static final int CYAN = 0xFF54FFFF;
    private static final int OFF = 0xFF89B6C6;
    private static final int PENDING = 0xFFFFD18F;
    private static final int ERROR = 0xFFFFAAAA;
    private static final int CAPTION = 0xFFC6DBE3;
    private static final Typeface MEDIUM = Typeface.create("sans-serif-medium", Typeface.NORMAL);

    private final Paint shape = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint image = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final TextPaint text = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final RectF bounds = new RectF();
    private final RectF meter = new RectF();
    private float level = 0f;
    private int accent = CYAN;

    void draw(Canvas canvas, JSONObject page, String status, Bitmap icon) {
        String headline = page.optString("value");
        String unit = page.optString("unit");
        String caption = page.optString("label");
        String swatch = page.optString("swatch");
        boolean off = "off".equals(page.optString("tone"));
        boolean appleTv = "ha-apple-tv.png".equals(page.optString("icon"));
        accent = statusColor(status, off);
        level = Math.max(0f, Math.min(100f, (float) page.optDouble("level", 0))) / 100f;
        meter.setEmpty();

        configureText(23f, CAPTION, Typeface.DEFAULT);
        int captionEnd = text.breakText(caption, true, CAPTION_WIDTH, null);
        String captionFirst = caption.substring(0, captionEnd).trim();
        String captionSecond = TextUtils.ellipsize(caption.substring(captionEnd).trim(),
                text, CAPTION_WIDTH - 16f, TextUtils.TruncateAt.END).toString();
        float captionHeight = captionSecond.isEmpty() ? 28f : 54f;
        float headlineHeight = unit.isEmpty() ? 44f : 63f;
        float meterHeight = page.has("level") ? 12f : 0f;
        float height = ICON_SIZE + 8f + headlineHeight + meterHeight + 8f + captionHeight;
        float top = CENTER_Y - height / 2f;

        if (appleTv) {
            shape.setColor(off ? 0xFFB0BAC8 : 0xFFFFFFFF);
            bounds.set(CENTER_X - 61f, top + 12.5f, CENTER_X + 61f, top + 134.5f);
            canvas.drawRoundRect(bounds, 28f, 28f, shape);
            drawIcon(canvas, icon, top + ICON_SIZE / 2f, 84f, 0xFF2D3445);
        } else {
            drawIcon(canvas, icon, top + ICON_SIZE / 2f, ICON_SIZE, accent);
        }

        float headlineY = top + ICON_SIZE + 8f;
        drawHeadline(canvas, headline, unit, swatch, headlineY + headlineHeight / 2f);

        float lower = headlineY + headlineHeight + meterHeight;
        if (page.has("level")) {
            meter.set(CENTER_X - 70f, lower - 4f, CENTER_X + 70f, lower);
            shape.setColor(0xFF233139);
            canvas.drawRoundRect(meter, 2f, 2f, shape);
        }
        configureText(23f, CAPTION, Typeface.DEFAULT);
        canvas.drawText(captionFirst, CENTER_X, lower + 8f + 14f + baselineOffset(), text);
        if (!captionSecond.isEmpty()) {
            canvas.drawText(captionSecond, CENTER_X, lower + 8f + 40f + baselineOffset(), text);
        }
    }

    void drawMeter(Canvas canvas, float progress) {
        if (meter.isEmpty() || level <= 0f) return;
        bounds.set(meter.left, meter.top,
                meter.left + meter.width() * level * progress, meter.bottom);
        shape.setColor(accent);
        canvas.drawRoundRect(bounds, 2f, 2f, shape);
    }

    private void drawHeadline(Canvas canvas, String value, String unit, String swatch, float y) {
        if (unit.isEmpty()) {
            configureText(38f, accent, MEDIUM);
            float swatchWidth = swatch.isEmpty() ? 0f : 31f;
            String line = TextUtils.ellipsize(value, text, CAPTION_WIDTH - swatchWidth,
                    TextUtils.TruncateAt.END).toString();
            canvas.drawText(line, CENTER_X - swatchWidth / 2f, y + baselineOffset(), text);
            if (!swatch.isEmpty()) {
                shape.setColor("warm".equals(swatch) ? 0xFFFFAB62 : 0xFF8EBBFF);
                canvas.drawCircle(CENTER_X + (text.measureText(line) + swatchWidth) / 2f - 9f,
                        y, 9f, shape);
            }
            return;
        }

        configureText(64f, accent, Typeface.DEFAULT);
        float valueWidth = text.measureText(value);
        float baseline = y + baselineOffset();
        configureText(29f, accent, MEDIUM);
        float unitWidth = text.measureText(unit);
        float start = CENTER_X - (valueWidth + 5f + unitWidth) / 2f;
        canvas.drawText(unit, start + valueWidth + 5f + unitWidth / 2f, baseline, text);
        configureText(64f, accent, Typeface.DEFAULT);
        canvas.drawText(value, start + valueWidth / 2f, baseline, text);
    }

    private void drawIcon(Canvas canvas, Bitmap bitmap, float y, float size, int color) {
        if (bitmap == null) return;
        float halfWidth = size / 2f;
        float halfHeight = halfWidth * bitmap.getHeight() / bitmap.getWidth();
        bounds.set(CENTER_X - halfWidth, y - halfHeight, CENTER_X + halfWidth, y + halfHeight);
        image.setColorFilter(new PorterDuffColorFilter(color, PorterDuff.Mode.SRC_IN));
        canvas.drawBitmap(bitmap, null, bounds, image);
    }

    private void configureText(float size, int color, Typeface face) {
        text.setTextAlign(Paint.Align.CENTER);
        text.setTypeface(face);
        text.setTextSize(size);
        text.setColor(color);
    }

    private float baselineOffset() {
        return -(text.ascent() + text.descent()) / 2f;
    }

    private static int statusColor(String status, boolean off) {
        if ("error".equals(status)) return ERROR;
        if ("stale".equals(status) || "partial".equals(status)) return PENDING;
        return off ? OFF : CYAN;
    }
}

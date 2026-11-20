package cn.rokid.bridge;

import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.RectF;
import android.graphics.Typeface;
import org.json.JSONObject;

/** Single status snapshot, drawn at the bridge's 640 × 480 reference size. */
final class PrinterCardRenderer {
    private static final int GREEN = 0xff18f000;
    private static final int TRACK = 0xff606060;
    private static final float SCALE = 0.84f;
    private final Path logo = makeLogo();
    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF cap = new RectF();
    private final RectF meter = new RectF(111, 31, 529, 449);

    void draw(Canvas canvas, JSONObject card, JSONObject fields, float progress, float wobble) {
        canvas.drawColor(Color.BLACK);
        canvas.save();
        canvas.scale(SCALE, SCALE, 320, 240);
        drawCard(canvas, card, fields, progress, wobble);
        canvas.restore();
    }

    private void drawCard(Canvas canvas, JSONObject card, JSONObject fields,
                          float progress, float wobble) {
        paint.setStrokeWidth(24);
        paint.setStrokeCap(Paint.Cap.ROUND);
        paint.setStyle(Paint.Style.STROKE);
        if (progress >= 100) {
            paint.setColor(GREEN);
            canvas.drawCircle(320, 240, 209, paint);
        } else {
            paint.setColor(TRACK);
            canvas.drawCircle(320, 240, 209, paint);
            if (progress > 0) {
                paint.setColor(GREEN);
                float sweep = 3.6f * progress;
                canvas.drawArc(meter, -90, sweep, false, paint);
                if (wobble != 0f) drawCap(canvas, sweep, wobble);
            }
        }
        paint.setStyle(Paint.Style.FILL);

        String notice = card.optString("notice", "");
        label(canvas, "拓竹打印进度", 320, 86, 18, Color.WHITE, false, 280);
        if (!notice.isEmpty()) {
            label(canvas, fields.optString("state"), 320, 211, 37, Color.WHITE, true, 305);
            label(canvas, notice, 320, 271, 18, Color.WHITE, false, 335);
            return;
        }
        label(canvas, fields.optString("progress_text"), 320, 158, 63, GREEN, true, 300);
        label(canvas, fields.optString("state"), 320, 192, 22, Color.WHITE, false, 270);

        label(canvas, "喷嘴", 213, 222, 15, Color.WHITE, false, 90);
        label(canvas, fields.optString("nozzle"), 213, 251, 26, Color.WHITE, true, 93);
        paint.setColor(GREEN);
        canvas.drawPath(logo, paint);
        label(canvas, "热床", 427, 222, 15, Color.WHITE, false, 90);
        label(canvas, fields.optString("bed"), 427, 251, 26, Color.WHITE, true, 93);

        String layers = fields.optString("layers");
        if (!"—".equals(layers)) label(canvas, layers, 320, 297, 23, Color.WHITE, false, 285);
        label(canvas, fields.optString("task"), 320, 329, 17, Color.WHITE, false, 310);
        String remaining = fields.optString("remaining");
        if (!"—".equals(remaining)) {
            label(canvas, "剩余 " + remaining, 320, 380, 28, Color.WHITE, false, 255);
        }
    }

    private void drawCap(Canvas canvas, float sweep, float wobble) {
        double angle = Math.toRadians(sweep);
        float x = 320f + 209f * (float) Math.sin(angle);
        float y = 240f - 209f * (float) Math.cos(angle);
        float rx = 12f * (1f + wobble);
        float ry = 12f * (1f - wobble * 0.75f);
        cap.set(x - rx, y - ry, x + rx, y + ry);
        paint.setStyle(Paint.Style.FILL);
        canvas.save();
        canvas.rotate(sweep, x, y);
        canvas.drawOval(cap, paint);
        canvas.restore();
    }

    private static Path makeLogo() {
        final Path logo = new Path();
        panel(logo, 300, 209, 318.5f, 209, 318.5f, 234.3f, 300, 241.6f);
        panel(logo, 300, 244.8f, 318.5f, 237.4f, 318.5f, 261, 300, 261);
        panel(logo, 321.4f, 209, 340, 209, 340, 232.6f, 321.4f, 225.3f);
        panel(logo, 321.4f, 228.4f, 340, 235.8f, 340, 261, 321.4f, 261);
        return logo;
    }

    private static void panel(Path logo, float x1, float y1, float x2, float y2,
                              float x3, float y3, float x4, float y4) {
        logo.moveTo(x1, y1);
        logo.lineTo(x2, y2);
        logo.lineTo(x3, y3);
        logo.lineTo(x4, y4);
        logo.close();
    }

    private void label(Canvas canvas, String text, float x, float y, float size,
                       int color, boolean bold, float maxWidth) {
        paint.setTypeface(bold ? Typeface.DEFAULT_BOLD : Typeface.DEFAULT);
        paint.setTextAlign(Paint.Align.CENTER);
        paint.setTextSize(size);
        paint.setColor(color);
        if (paint.measureText(text) > maxWidth) {
            int end = paint.breakText(text, true, maxWidth - paint.measureText("…"), null);
            if (end > 0 && Character.isHighSurrogate(text.charAt(end - 1))) end--;
            text = text.substring(0, end) + "…";
        }
        canvas.drawText(text, x, y, paint);
    }
}

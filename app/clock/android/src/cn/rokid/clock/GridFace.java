package cn.rokid.clock;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Rect;
import android.graphics.Typeface;

/**
 * The first face: four huge rounded digits in a 2x2 grid, HH over MM,
 * checkerboard mint / periwinkle on dark charcoal. Pure Canvas, no bitmaps.
 */
public class GridFace implements ClockFace {

    public static final int BG = 0xFF28262E;
    public static final int MINT = 0xFF9EFFE8;
    public static final int PERI = 0xFFAEB3FF;

    /** Per-digit phase offsets so the four bob out of step. */
    private static final float[] PHASE = {0.0f, 1.9f, 3.7f, 5.3f};

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Rect bounds = new Rect();
    private final String name;

    public GridFace(String name, Typeface typeface) {
        this.name = name;
        paint.setTypeface(typeface);
        paint.setTextAlign(Paint.Align.LEFT);
    }

    @Override
    public String name() {
        return name;
    }

    @Override
    public void draw(Canvas canvas, int width, int height, long nowMillis) {
        // Transparent background: only the digits glow, no grey panel.
        canvas.drawColor(0, android.graphics.PorterDuff.Mode.CLEAR);
        java.util.Calendar cal = java.util.Calendar.getInstance(
                java.util.TimeZone.getDefault());
        cal.setTimeInMillis(nowMillis);
        int hour = cal.get(java.util.Calendar.HOUR_OF_DAY);
        int minute = cal.get(java.util.Calendar.MINUTE);
        String[] digits = {
                String.valueOf(hour / 10), String.valueOf(hour % 10),
                String.valueOf(minute / 10), String.valueOf(minute % 10)};

        // Fit one glyph height into its cell; both cells share one size.
        float target = height * 0.38f;
        paint.setTextSize(100f);
        paint.getTextBounds("8", 0, 1, bounds);
        float ratio = bounds.height() > 0 ? target / bounds.height() : 1f;
        paint.setTextSize(100f * ratio);

        // One tight cluster in the middle, digits slightly overlapping.
        float halfX = width * 0.08f;
        float halfY = height * 0.175f;

        for (int i = 0; i < 4; i++) {
            float cx = width / 2f + (i % 2 == 0 ? -halfX : halfX);
            float cy = height / 2f + (i < 2 ? -halfY : halfY);
            float phase = PHASE[i];

            // Q-bounce float: gentle bob, squash-stretch pop that lags
            // the bob, and a small lazy sway.
            float bob = (float) Math.sin(nowMillis / 650.0 + phase) * height * 0.007f;
            float pop = 1f + (float) Math.sin(nowMillis / 520.0 + phase - 0.9f) * 0.02f;
            float tilt = (float) Math.sin(nowMillis / 820.0 + phase * 0.7f) * 1.2f;

            paint.setColor((i == 0 || i == 3) ? MINT : PERI);
            String d = digits[i];
            paint.getTextBounds(d, 0, d.length(), bounds);
            float x = cx - bounds.exactCenterX();
            float y = cy - bounds.exactCenterY();

            canvas.save();
            canvas.translate(0f, bob);
            canvas.rotate(tilt, cx, cy);
            canvas.scale(pop, pop, cx, cy);
            canvas.drawText(d, x, y, paint);
            canvas.restore();
        }
    }
}

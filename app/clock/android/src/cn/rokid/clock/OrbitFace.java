package cn.rokid.clock;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.Typeface;
import java.util.Calendar;
import java.util.Locale;

/** Two counter-reading drums, a fixed hour, and an open minute aperture.
 * All geometry is original Canvas drawing; no photograph/bitmap watch parts.
 * Rings point at 3 o'clock: angle(value) = (current - value) * 6 degrees.
 * No corner complications, decorative arcs, date or watch-case bezel.
 */
public final class OrbitFace implements ClockFace {
    private final String name;
    private final Calendar clock = Calendar.getInstance();
    private final Typeface regular = Typeface.create("sans-serif-condensed", Typeface.NORMAL);
    private final Typeface bold = Typeface.create("sans-serif-condensed", Typeface.BOLD);
    private final Paint ink = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Rect bounds = new Rect();
    private final Path path = new Path();
    private static final int MARK = 0xFF68686A;
    private static final int LIGHT = 0xFFF5F5F5;
    private String hourText = "00", minuteText = "00";
    private long lastMinute = Long.MIN_VALUE;
    private static final String[] LABELS = {
        "00", "05", "10", "15", "20", "25",
        "30", "35", "40", "45", "50", "55"};

    public OrbitFace(String name) {
        this.name = name;
        text.setTypeface(regular);
        text.setTextAlign(Paint.Align.CENTER);
        ink.setStrokeCap(Paint.Cap.ROUND);
        ink.setStrokeJoin(Paint.Join.ROUND);
    }

    @Override public String name() { return name; }

    @Override public void draw(Canvas canvas, int width, int height, long nowMillis) {
        canvas.drawColor(0xFF000000);
        float scale = Math.min(width / 640f, height / 480f);
        canvas.save();
        // Critical time and dial stay above the bridge's bottom subtitle strip.
        canvas.translate(width / 2f, height / 2f);
        canvas.scale(scale, scale);
        clock.setTimeZone(java.util.TimeZone.getDefault());
        clock.setTimeInMillis(nowMillis);
        long minuteKey = nowMillis / 60000L;
        if (lastMinute != minuteKey) {
            hourText = String.format(Locale.US, "%02d", clock.get(Calendar.HOUR_OF_DAY));
            minuteText = String.format(Locale.US, "%02d", clock.get(Calendar.MINUTE));
            lastMinute = minuteKey;
        }
        float seconds = clock.get(Calendar.SECOND)
                + clock.get(Calendar.MILLISECOND) / 1000f;
        float minutes = clock.get(Calendar.MINUTE) + seconds / 60f;
        drawDrum(canvas, 172f, 145f, seconds, 22f);
        drawDrum(canvas, 126f, 100f, minutes, 25f);
        // An opaque hub keeps both rings from touching the large hour.
        ink.setStyle(Paint.Style.FILL);
        ink.setColor(0xFF000000);
        canvas.drawCircle(-18f, 0f, 81f, ink);
        text.setTypeface(bold);
        text.setTextSize(100f);
        text.setColor(LIGHT);
        centered(canvas, hourText, -28f, 0f);
        // Minute aperture: a rounded LEFT edge with open parallel rails to right.
        // Its black backing masks ring labels underneath, as on the reference.
        ink.setColor(0xFF000000);
        path.reset();
        path.moveTo(192f, -37f); path.lineTo(88f, -37f);
        path.cubicTo(37f, -37f, 37f, 37f, 88f, 37f);
        path.lineTo(192f, 37f); path.close();
        canvas.drawPath(path, ink);
        ink.setStyle(Paint.Style.STROKE);
        ink.setColor(LIGHT); ink.setStrokeWidth(2.4f);
        path.reset();
        path.moveTo(192f, -37f); path.lineTo(88f, -37f);
        path.cubicTo(37f, -37f, 37f, 37f, 88f, 37f);
        path.lineTo(192f, 37f);
        canvas.drawPath(path, ink);
        text.setTextSize(42f);
        centered(canvas, minuteText, 94f, 0f);
        canvas.restore();
    }

    private void centered(Canvas c, String value, float x, float y) {
        text.getTextBounds(value, 0, value.length(), bounds);
        c.drawText(value, x, y - bounds.exactCenterY(), text);
    }

    private void drawDrum(Canvas c, float tickRadius, float labelRadius,
                          float current, float fontSize) {
        text.setTypeface(regular);
        text.setTextSize(fontSize); text.setColor(0xFF9B9B9E);
        ink.setStyle(Paint.Style.STROKE); ink.setColor(MARK);
        for (int i = 0; i < 60; i++) {
            c.save();
            c.rotate((current - i) * 6f);
            boolean major = i % 5 == 0;
            ink.setStrokeWidth(major ? 2.1f : 1.6f);
            c.drawLine(tickRadius - (major ? 10f : 5f), 0f,
                    tickRadius, 0f, ink);
            if (major) centered(c, LABELS[i / 5], labelRadius, 0f);
            c.restore();
        }
    }

}

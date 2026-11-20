package cn.rokid.clock;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Rect;
import android.graphics.Typeface;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * Fallback / simple face: big HH:MM centred, date underneath.
 * Colours can be customised from a sdcard face.json.
 */
public class DigitalFace implements ClockFace {

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Rect bounds = new Rect();
    private final String name;
    private final int bg;
    private final int text;
    private final int accent;
    private final int fontPx;
    private final boolean showDate;

    public DigitalFace(String name, Typeface typeface) {
        this(name, typeface, 0, 0xFFF2F2F4, GridFace.MINT, 180, true);
    }

    public DigitalFace(String name, Typeface typeface, int bg, int text,
                       int accent, int fontPx, boolean showDate) {
        this.name = name;
        this.bg = bg;
        this.text = text;
        this.accent = accent;
        this.fontPx = fontPx;
        this.showDate = showDate;
        paint.setTypeface(typeface);
        paint.setTextAlign(Paint.Align.LEFT);
    }

    @Override
    public String name() {
        return name;
    }

    @Override
    public void draw(Canvas canvas, int width, int height, long nowMillis) {
        if (bg == 0) {
            canvas.drawColor(0, android.graphics.PorterDuff.Mode.CLEAR);
        } else {
            canvas.drawColor(bg);
        }
        Date now = new Date(nowMillis);
        String time = new SimpleDateFormat("HH:mm", Locale.US).format(now);

        paint.setTextSize(fontPx);
        paint.setColor(text);
        paint.getTextBounds(time, 0, time.length(), bounds);
        float x = width / 2f - bounds.exactCenterX();
        float y = height / 2f - bounds.exactCenterY();
        canvas.drawText(time, x, y, paint);

        if (showDate) {
            String date = new SimpleDateFormat("M月d日 EEEE", Locale.CHINA).format(now);
            paint.setTextSize(fontPx / 5.5f);
            paint.setColor(accent);
            paint.getTextBounds(date, 0, date.length(), bounds);
            float dx = width / 2f - bounds.exactCenterX();
            float dy = y + bounds.height() * 2.2f;
            canvas.drawText(date, dx, dy, paint);
        }
    }
}

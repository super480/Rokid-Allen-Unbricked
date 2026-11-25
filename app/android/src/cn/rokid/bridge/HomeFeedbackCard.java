package cn.rokid.bridge;

import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffColorFilter;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.os.SystemClock;
import android.text.TextPaint;
import android.text.TextUtils;

import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Locale;

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
    private static final int APPLE_ON = 0xFFFFFFFF;
    private static final int APPLE_OFF = 0xFFB0BAC8;
    private static final int LAST_FRAME = 16;
    private static final int FRAME_SIZE = 144;
    private static final long READING_AGE_MS = 600_000L;
    private static final Typeface MEDIUM = Typeface.create("sans-serif-medium", Typeface.NORMAL);
    private static final String[][] MORPH_PAIRS = {
        {"curtains-closed", "curtains"},
        {"power-plug-off-outline", "power-plug"},
        {"toggle-switch-off-outline", "toggle-switch"},
        {"lock", "lock-open-variant-outline"},
        {"movie-off-outline", "movie-open-outline"}
    };
    // The overlay can be recreated between responses. Remember only recent
    // confirmed numeric values for the same named device, metric and unit.
    // All access happens on the bridge's main thread.
    private static final LinkedHashMap<String, Reading> READINGS =
            new LinkedHashMap<>(24, 0.75f, true);

    interface AssetLoader {
        Bitmap load(String name);
    }

    private static final class Reading {
        final float value;
        final float level;
        final long time;

        Reading(float value, float level, long time) {
            this.value = value;
            this.level = level;
            this.time = time;
        }
    }

    private final AssetLoader assets;
    private final Paint shape = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint image = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final TextPaint text = new TextPaint(Paint.ANTI_ALIAS_FLAG);
    private final RectF bounds = new RectF();
    private final RectF meter = new RectF();
    private final Rect tile = new Rect();
    private final String[] readout = new String[LAST_FRAME + 1];
    private Bitmap icon;
    private Bitmap atlas;
    private boolean reverse;
    private boolean pulse;
    private boolean appleTv;
    private boolean off;
    private int accent;
    private float iconY;
    private float headlineY;
    private float level;
    private float fromLevel;
    private String headline = "";
    private String unit = "";
    private int swatch;

    HomeFeedbackCard(AssetLoader assets) {
        this.assets = assets;
    }

    void prepare(Canvas canvas, JSONObject page, String status) {
        headline = page.optString("value");
        unit = page.optString("unit");
        String caption = page.optString("label");
        String iconName = page.optString("icon");
        String swatchName = page.optString("swatch");
        swatch = "warm".equals(swatchName) ? 0xFFFFAB62 :
                ("cool".equals(swatchName) ? 0xFF8EBBFF : 0);
        off = "off".equals(page.optString("tone"));
        appleTv = "ha-apple-tv.png".equals(iconName);
        accent = statusColor(status, off);
        if (unit.isEmpty()) {
            configureText(38f, accent, MEDIUM);
            headline = TextUtils.ellipsize(headline, text, CAPTION_WIDTH - (swatch == 0 ? 0f : 31f),
                    TextUtils.TruncateAt.END).toString();
        }
        image.setColorFilter(new PorterDuffColorFilter(
                appleTv ? 0xFF2D3445 : accent, PorterDuff.Mode.SRC_IN));
        level = Math.max(0f, Math.min(100f, (float) page.optDouble("level", 0))) / 100f;
        meter.setEmpty();
        icon = assets.load(iconName);
        if ("ok".equals(status)) loadMorph(iconName);
        pulse = atlas == null && !appleTv && unit.isEmpty() && swatch == 0;
        prepareReadout(iconName, caption, status);

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
        iconY = top + ICON_SIZE / 2f;
        headlineY = top + ICON_SIZE + 8f + headlineHeight / 2f;

        float lower = headlineY + headlineHeight / 2f + meterHeight;
        if (page.has("level")) {
            meter.set(CENTER_X - 70f, lower - 4f, CENTER_X + 70f, lower);
            shape.setColor(0xFF233139);
            canvas.drawRoundRect(meter, 2f, 2f, shape);
        }
        canvas.drawText(captionFirst, CENTER_X, lower + 22f + baselineOffset(), text);
        if (!captionSecond.isEmpty()) {
            canvas.drawText(captionSecond, CENTER_X, lower + 48f + baselineOffset(), text);
        }
    }

    private void loadMorph(String iconName) {
        for (String[] pair : MORPH_PAIRS) {
            boolean atStart = iconName.equals("ha-" + pair[0] + ".png");
            if (atStart || iconName.equals("ha-" + pair[1] + ".png")) {
                atlas = assets.load("ha-morph-" + pair[0] + "--" + pair[1] + ".png");
                if (atlas != null && (atlas.getWidth() != FRAME_SIZE * 4
                        || atlas.getHeight() != FRAME_SIZE * 5)) {
                    atlas.recycle();
                    atlas = null;
                }
                reverse = atStart;
                return;
            }
        }
    }

    private void prepareReadout(String iconName, String caption, String status) {
        fromLevel = 0f;
        for (int i = 0; i <= LAST_FRAME; i++) readout[i] = headline;
        if (unit.isEmpty() || !hasReadingIdentity(caption) || !"ok".equals(status)) return;
        float next;
        try {
            next = Float.parseFloat(headline);
        } catch (NumberFormatException ignored) {
            return;
        }
        if (Float.isInfinite(next) || Float.isNaN(next)) return;
        // Both battery-charge icons share the same measurement.
        String key = (iconName.startsWith("ha-battery-") ? "battery" : iconName)
                + '\n' + caption + '\n' + unit;
        long now = SystemClock.elapsedRealtime();
        Reading previous = READINGS.put(key, new Reading(next, level, now));
        if (READINGS.size() > 24) READINGS.remove(READINGS.keySet().iterator().next());
        if (previous == null || now - previous.time > READING_AGE_MS) return;
        fromLevel = previous.level;
        if (next == previous.value) return;
        int decimal = headline.indexOf('.');
        int places = decimal == -1 ? 0 : Math.min(2, headline.length() - decimal - 1);
        String format = "%." + places + "f";
        for (int i = 0; i < LAST_FRAME; i++) {
            float value = previous.value + (next - previous.value) * i / LAST_FRAME;
            readout[i] = String.format(Locale.ROOT, format, value);
        }
    }

    private static boolean hasReadingIdentity(String caption) {
        // The host uses these captions when it has no room or device name.
        return !caption.isEmpty() && !"温度".equals(caption) && !"湿度".equals(caption)
                && !"电量查询".equals(caption) && !"灯光 · 亮度".equals(caption)
                && !"空调 · 设定温度".equals(caption);
    }

    void drawForeground(Canvas canvas, float progress, int opacity) {
        float eased = progress * progress * (3f - 2f * progress);
        int frame = Math.round(eased * LAST_FRAME);
        image.setAlpha(opacity);
        if (appleTv) {
            int base = off ? APPLE_ON : APPLE_OFF;
            int target = off ? APPLE_OFF : APPLE_ON;
            shape.setColor(fade(mix(base, target, eased), opacity));
            bounds.set(CENTER_X - 61f, iconY - 61f, CENTER_X + 61f, iconY + 61f);
            canvas.drawRoundRect(bounds, 28f, 28f, shape);
            drawIcon(canvas, 84f);
        } else if (atlas != null && progress < 1f) {
            int cell = reverse ? LAST_FRAME - frame : frame;
            int x = (cell % 4) * FRAME_SIZE;
            int y = (cell / 4) * FRAME_SIZE;
            tile.set(x, y, x + FRAME_SIZE, y + FRAME_SIZE);
            setIconBounds(ICON_SIZE, ICON_SIZE);
            canvas.drawBitmap(atlas, tile, bounds, image);
        } else {
            if (pulse) image.setAlpha(Math.round(opacity * (0.35f + 0.65f * eased)));
            drawIcon(canvas, pulse ? ICON_SIZE * (0.9f + 0.1f * eased) : ICON_SIZE);
        }

        float reveal = unit.isEmpty() ? 1f : 0.65f + 0.35f * eased;
        drawHeadline(canvas, readout[frame], headlineY + (1f - reveal) * 10f,
                eased, Math.round(opacity * reveal));
        float meterLevel = fromLevel + (level - fromLevel) * eased;
        if (!meter.isEmpty() && meterLevel > 0f) {
            bounds.set(meter.left, meter.top, meter.left + meter.width() * meterLevel, meter.bottom);
            shape.setColor(fade(accent, opacity));
            canvas.drawRoundRect(bounds, 2f, 2f, shape);
        }
    }

    private void drawHeadline(Canvas canvas, String value, float y, float progress, int opacity) {
        int color = fade(accent, opacity);
        if (unit.isEmpty()) {
            configureText(38f, color, MEDIUM);
            float swatchWidth = swatch == 0 ? 0f : 31f;
            canvas.drawText(value, CENTER_X - swatchWidth / 2f, y + baselineOffset(), text);
            if (swatch != 0) {
                shape.setColor(fade(mix(0xFFAFC4CC, swatch, progress), opacity));
                canvas.drawCircle(CENTER_X + (text.measureText(value) + swatchWidth) / 2f - 9f,
                        y, 9f, shape);
            }
            return;
        }

        configureText(64f, color, Typeface.DEFAULT);
        float valueWidth = text.measureText(value);
        float baseline = y + baselineOffset();
        configureText(29f, color, MEDIUM);
        float unitWidth = text.measureText(unit);
        float start = CENTER_X - (valueWidth + 5f + unitWidth) / 2f;
        canvas.drawText(unit, start + valueWidth + 5f + unitWidth / 2f, baseline, text);
        configureText(64f, color, Typeface.DEFAULT);
        canvas.drawText(value, start + valueWidth / 2f, baseline, text);
    }

    private void drawIcon(Canvas canvas, float size) {
        if (icon == null) return;
        setIconBounds(size, size * icon.getHeight() / icon.getWidth());
        canvas.drawBitmap(icon, null, bounds, image);
    }

    private void setIconBounds(float width, float height) {
        bounds.set(CENTER_X - width / 2f, iconY - height / 2f,
                CENTER_X + width / 2f, iconY + height / 2f);
    }

    void release() {
        if (icon != null) icon.recycle();
        if (atlas != null) atlas.recycle();
        icon = null;
        atlas = null;
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

    private static int fade(int color, int opacity) {
        return (color & 0x00FFFFFF) | (opacity << 24);
    }

    private static int mix(int from, int to, float progress) {
        return 0xFF000000 | (interpolateChannel(from, to, 16, progress) << 16)
                | (interpolateChannel(from, to, 8, progress) << 8)
                | interpolateChannel(from, to, 0, progress);
    }

    private static int interpolateChannel(int from, int to, int shift, float progress) {
        int a = (from >> shift) & 0xFF;
        int b = (to >> shift) & 0xFF;
        return Math.round(a + (b - a) * progress);
    }

    private static int statusColor(String status, boolean off) {
        if ("error".equals(status)) return ERROR;
        if ("stale".equals(status) || "partial".equals(status)) return PENDING;
        return off ? OFF : CYAN;
    }
}

package cn.rokid.bridge;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapShader;
import android.graphics.Canvas;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.PorterDuff;
import android.graphics.PorterDuffXfermode;
import android.graphics.RadialGradient;
import android.graphics.RectF;
import android.graphics.Shader;
import android.media.MediaPlayer;
import android.os.Handler;
import android.view.View;

/**
 * The screen the speaker shows while its own MediaPlayer is running.
 *
 * Only three things are on it: two heading lines pinned to the very top,
 * a large circular cover in the middle, and two or three scrolling lyric
 * lines underneath. The cover is drawn as a circle with a feathered rim
 * (plus a very soft halo), so it blends into the background instead of
 * ending in a hard, aliased step. No progress bar, no clock, no footer.
 *
 * Both heading lines use the same colour and size, and the circle's top
 * and bottom edges sit close to the text above and below it.
 *
 * The lyric line comes from MediaPlayer's own playback position, so it
 * stays in step with the audio without a host round trip.
 *
 * Everything is drawn in the same 640x480 logical space as AvatarView; the
 * canvas is scaled to the real view size, so the layout does not depend on
 * the panel's physical resolution.
 */
public final class NowPlayingView extends View {

    private static final int ACCENT = 0xFF4AC1FF;
    private static final int TEXT = 0xFFF2F6FF;
    private static final int MUTED = 0xFF8A93A8;

    private static final float CENTER_X = 320f;
    private static final float HEAD_SIZE = 21f;
    private static final float ARTIST_Y = 21f;
    private static final float TITLE_Y = 46f;
    private static final float COVER_CY = 224f;
    private static final float COVER_RADIUS = 168f;
    private static final float LYRIC_WIDTH = 580f;
    private static final float LYRIC_PREV_Y = 412f;
    private static final float LYRIC_PREV_SIZE = 17f;
    private static final float LYRIC_NOW_Y = 440f;
    private static final float LYRIC_NOW_SIZE = 23f;
    private static final float LYRIC_NEXT_Y = 468f;
    private static final float LYRIC_NEXT_SIZE = 17f;

    // The circular bitmap is rendered once per cover, at slightly more than
    // its on-screen size, so scaling never reintroduces jagged edges.
    private static final int CIRCLE_PIXELS = 384;
    private static final float FEATHER_FROM = 0.93f;

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint halo = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint placeholder = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Handler handler = new Handler();
    private final RectF box = new RectF();
    private final Runnable tick = new Runnable() {
        public void run() {
            invalidate();
            handler.postDelayed(this, 200);
        }
    };

    private Bitmap roundCover = null;
    private String title = "", artist = "";
    private long[] times = new long[0];
    private String[] lines = new String[0];
    private MediaPlayer player = null;
    private boolean running = false;
    private boolean paused = false;
    // Roon is display-only: no MediaPlayer, so the position comes from the
    // last seek value plus the time elapsed since the host sent it.
    private boolean roonMode = false;
    private String roonZone = "";
    private String roonAlbum = "";
    private long roonSeek = 0;
    private long roonLength = 0;
    private long roonAt = 0;

    public NowPlayingView(Context context) {
        super(context);
        // A faint glow just outside the rim keeps the feathered edge from
        // looking like a cut-out pasted onto the dark panel.
        halo.setShader(new RadialGradient(
                CENTER_X, COVER_CY, COVER_RADIUS + 14f,
                new int[]{0x00000000, 0x00000000, 0x1F4AC1FF, 0x00000000},
                new float[]{0f, 0.86f, 0.95f, 1f},
                Shader.TileMode.CLAMP));
        // The no-artwork disc gets the same soft rim as a real cover.
        placeholder.setShader(new RadialGradient(
                CENTER_X, COVER_CY, COVER_RADIUS,
                new int[]{0xFF1B2233, 0xFF1B2233, 0x001B2233},
                new float[]{0f, FEATHER_FROM, 1f},
                Shader.TileMode.CLAMP));
    }

    /** Attach the playback source and start refreshing the lyric line. */
    public void start(MediaPlayer source) {
        // A real local clip takes the screen back from a Roon display.
        roonMode = false;
        player = source;
        if (!running) {
            running = true;
            handler.post(tick);
        }
        invalidate();
    }

    /** Show a Roon zone. The audio is elsewhere; only the screen is ours. */
    public void setRoon(String state, String zone, String album,
                        double seekSeconds, double lengthSeconds,
                        boolean isPaused) {
        roonMode = true;
        roonZone = zone == null ? "" : zone;
        roonAlbum = album == null ? "" : album;
        roonSeek = (long) (Math.max(0, seekSeconds) * 1000);
        roonLength = (long) (Math.max(0, lengthSeconds) * 1000);
        roonAt = android.os.SystemClock.elapsedRealtime();
        player = null;
        paused = isPaused || "paused".equals(state) || "stopped".equals(state);
        if (!running) {
            running = true;
            handler.post(tick);
        }
        invalidate();
    }

    /** Show a small badge while the clip is held by a pause command. */
    public void setPaused(boolean value) {
        paused = value;
        invalidate();
    }

    public void stop() {
        running = false;
        paused = false;
        roonMode = false;
        handler.removeCallbacks(tick);
        player = null;
        releaseRoundCover();
    }

    public void setCover(Bitmap bitmap) {
        releaseRoundCover();
        if (bitmap != null && !bitmap.isRecycled()) {
            try {
                roundCover = circularCover(bitmap);
            } catch (Exception ignored) {
                roundCover = null;   // fall back to the placeholder disc
            }
        }
        invalidate();
    }

    private void releaseRoundCover() {
        if (roundCover != null && !roundCover.isRecycled()) {
            roundCover.recycle();
        }
        roundCover = null;
    }

    /** Center-crop into an anti-aliased circle whose outer rim fades out. */
    private static Bitmap circularCover(Bitmap source) {
        int width = source.getWidth();
        int height = source.getHeight();
        if (width <= 0 || height <= 0) return null;

        int size = CIRCLE_PIXELS;
        float center = size / 2f;
        Bitmap out = Bitmap.createBitmap(size, size, Bitmap.Config.ARGB_8888);
        Canvas canvas = new Canvas(out);
        Paint brush = new Paint(Paint.ANTI_ALIAS_FLAG);

        float scale = Math.max((float) size / width, (float) size / height);
        Matrix matrix = new Matrix();
        matrix.setScale(scale, scale);
        matrix.postTranslate(center - width * scale / 2f,
                             center - height * scale / 2f);
        BitmapShader shader = new BitmapShader(source,
                Shader.TileMode.CLAMP, Shader.TileMode.CLAMP);
        shader.setLocalMatrix(matrix);
        brush.setShader(shader);
        canvas.drawCircle(center, center, center, brush);

        // DST_IN with an alpha gradient feathers the last ~7% of the radius.
        brush.setShader(new RadialGradient(center, center, center,
                new int[]{0xFFFFFFFF, 0xFFFFFFFF, 0x00FFFFFF},
                new float[]{0f, FEATHER_FROM, 1f},
                Shader.TileMode.CLAMP));
        brush.setXfermode(new PorterDuffXfermode(PorterDuff.Mode.DST_IN));
        canvas.drawCircle(center, center, center, brush);
        brush.setXfermode(null);
        return out;
    }

    public void setMeta(String songTitle, String songArtist,
                        long[] lyricTimes, String[] lyricLines) {
        title = songTitle == null ? "" : songTitle;
        artist = songArtist == null ? "" : songArtist;
        times = lyricTimes == null ? new long[0] : lyricTimes;
        lines = lyricLines == null ? new String[0] : lyricLines;
        invalidate();
    }

    private int position() {
        if (roonMode) {
            long elapsed = paused ? 0
                    : android.os.SystemClock.elapsedRealtime() - roonAt;
            long value = roonSeek + Math.max(0, elapsed);
            if (roonLength > 0 && value > roonLength) value = roonLength;
            return (int) Math.min(value, Integer.MAX_VALUE);
        }
        if (player == null) return 0;
        try {
            return player.getCurrentPosition();
        } catch (Exception ignored) {
            return 0;
        }
    }

    /** The last line whose timestamp has passed. */
    private int currentLine(int pos) {
        int found = -1;
        for (int i = 0; i < times.length && i < lines.length; i++) {
            if (times[i] <= pos) found = i;
            else break;
        }
        return found;
    }

    private String fit(String text, float size, float maxWidth) {
        paint.setTextSize(size);
        if (paint.measureText(text) <= maxWidth) return text;
        StringBuilder kept = new StringBuilder();
        for (int i = 0; i < text.length(); i++) {
            if (paint.measureText(kept + "…") > maxWidth) break;
            kept.append(text.charAt(i));
        }
        return kept.toString().trim() + "…";
    }

    private void text(String value, float x, float y, float size,
                      int color, boolean bold, boolean center) {
        paint.setShader(null);
        paint.setColor(color);
        paint.setTextSize(size);
        paint.setFakeBoldText(bold);
        paint.setTextAlign(center ? Paint.Align.CENTER : Paint.Align.LEFT);
        currentCanvas.drawText(value, x, y, paint);
    }

    // drawText needs the canvas; it is set for the duration of onDraw.
    private Canvas currentCanvas;

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        currentCanvas = canvas;
        canvas.save();
        canvas.scale(getWidth() / 640f, getHeight() / 480f);

        canvas.drawColor(0xF20E1220);
        paint.setStyle(Paint.Style.FILL);
        paint.setShader(null);
        paint.setXfermode(null);

        // Two heading lines, identical colour and size, artist at the top.
        if (!artist.isEmpty()) {
            text(fit(artist, HEAD_SIZE, LYRIC_WIDTH), CENTER_X,
                 ARTIST_Y, HEAD_SIZE, TEXT, true, true);
        }
        text(fit(title.isEmpty() ? "音乐播放中" : title, HEAD_SIZE,
                 LYRIC_WIDTH), CENTER_X, TITLE_Y, HEAD_SIZE,
             TEXT, true, true);

        // Circular cover: soft halo, then the feathered bitmap (or a disc).
        canvas.drawCircle(CENTER_X, COVER_CY, COVER_RADIUS + 14f, halo);
        if (roundCover != null && !roundCover.isRecycled()) {
            box.set(CENTER_X - COVER_RADIUS, COVER_CY - COVER_RADIUS,
                    CENTER_X + COVER_RADIUS, COVER_CY + COVER_RADIUS);
            paint.setShader(null);
            paint.setXfermode(null);
            paint.setColor(0xFFFFFFFF);
            paint.setAlpha(255);
            canvas.drawBitmap(roundCover, null, box, paint);
        } else {
            canvas.drawCircle(CENTER_X, COVER_CY, COVER_RADIUS, placeholder);
            paint.setShader(null);
            paint.setColor(ACCENT);
            paint.setTextSize(110);
            paint.setTextAlign(Paint.Align.CENTER);
            paint.setFakeBoldText(false);
            canvas.drawText("♪", CENTER_X, 262, paint);
        }

        // A held clip keeps the screen up; the badge makes a voice pause
        // visible on a device whose overlay cannot be captured.
        if (paused && !roonMode) {
            paint.setShader(null);
            paint.setXfermode(null);
            paint.setStyle(Paint.Style.FILL);
            paint.setAlpha(255);
            paint.setColor(0xC00E1220);
            canvas.drawCircle(CENTER_X, COVER_CY, 64f, paint);
            paint.setStyle(Paint.Style.STROKE);
            paint.setStrokeWidth(2f);
            paint.setColor(0x804AC1FF);
            canvas.drawCircle(CENTER_X, COVER_CY, 64f, paint);
            paint.setStyle(Paint.Style.FILL);
            text("已暂停", CENTER_X, COVER_CY + 8f, 21f,
                 TEXT, true, true);
        }

        if (roonMode) {
            // Roon sends no words. The host looks up a timed sheet once per
            // track; the middle line follows the zone's own clock, so no
            // play time is shown here any more.
            String zoneLine = roonZone.isEmpty() ? "Roon" : roonZone;
            if (paused) zoneLine = zoneLine + " · 已暂停";
            text(fit(zoneLine, LYRIC_PREV_SIZE, LYRIC_WIDTH), CENTER_X,
                 LYRIC_PREV_Y, LYRIC_PREV_SIZE, MUTED, false, true);
            int index = currentLine(position());
            if (lines.length == 0) {
                text("暂无歌词", CENTER_X, LYRIC_NOW_Y, LYRIC_NOW_SIZE,
                     MUTED, false, true);
            } else {
                int active = index >= 0 ? index : 0;
                boolean beforeStart = index < 0;
                text(fit(lines[active], LYRIC_NOW_SIZE, LYRIC_WIDTH),
                     CENTER_X, LYRIC_NOW_Y, LYRIC_NOW_SIZE,
                     beforeStart ? MUTED : ACCENT,
                     !beforeStart && !paused, true);
            }
            text(fit(roonAlbum.isEmpty() ? "Roon 输出" : roonAlbum,
                     LYRIC_NEXT_SIZE, LYRIC_WIDTH), CENTER_X, LYRIC_NEXT_Y,
                 LYRIC_NEXT_SIZE, MUTED, false, true);
        } else {
            // Previous / active (highlighted) / next lyric, close under the art.
            int index = currentLine(position());
            if (lines.length == 0) {
                text("暂无歌词", CENTER_X, LYRIC_NOW_Y, 20, MUTED, false, true);
            } else {
                int active = index >= 0 ? index : 0;
                boolean beforeStart = index < 0;
                if (active > 0) {
                    text(fit(lines[active - 1], LYRIC_PREV_SIZE, LYRIC_WIDTH),
                         CENTER_X, LYRIC_PREV_Y, LYRIC_PREV_SIZE,
                         MUTED, false, true);
                }
                text(fit(lines[active], LYRIC_NOW_SIZE, LYRIC_WIDTH),
                     CENTER_X, LYRIC_NOW_Y, LYRIC_NOW_SIZE,
                     beforeStart ? MUTED : ACCENT, !beforeStart, true);
                if (active + 1 < lines.length) {
                    text(fit(lines[active + 1], LYRIC_NEXT_SIZE, LYRIC_WIDTH),
                         CENTER_X, LYRIC_NEXT_Y, LYRIC_NEXT_SIZE,
                         MUTED, false, true);
                }
            }
        }

        canvas.restore();
        currentCanvas = null;
    }
}

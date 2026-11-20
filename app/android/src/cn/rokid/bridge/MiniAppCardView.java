package cn.rokid.bridge;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.RadialGradient;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.os.Handler;
import android.os.SystemClock;
import android.util.Log;
import android.view.View;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.InputStream;

/**
 * One general-purpose card renderer for every query app.
 *
 * The host sends a small JSON layout (rings, rows, colours, absolute reset
 * timestamps), never a picture. This view draws the V6 double-ring design
 * natively and re-draws on its own clock: the ring values spring from each
 * ring's own halfway point to its target with an overshoot/rebound (V6
 * spring knots), and the reset countdown ticks down every second for the
 * whole time the card is up. The background, gradient, tracks, logo and
 * footer are cached in one offscreen bitmap (D038): software-rendering
 * that gradient on every frame measured only 23-26 fps, while blitting
 * the cached layer reaches ~51 fps. A second query app only needs a
 * different layout object. The home feedback layouts use a separate
 * renderer and slide their cached artwork into place once.
 *
 * D040 card-level extensions (all optional, all renderer-agnostic):
 *   ring.track_color  per-ring dark track (falls back to card.track_color)
 *   ring.value_text   a direction icon (↓ / ↑) drawn UPRIGHT in the ARC
 *                     START cap ("首部"); suppresses the endpoint "N%"
 *                     caption. 2026-11-16: moved back from the arc end
 *                     at the user's request; it no longer travels with
 *                     the spring animation.
 *   rows[i].value     shown verbatim unless the matching ring has a real
 *                     reset_at_ms, in which case it becomes a countdown
 *   card.icon         switches the bundled asset between apps
 *
 * The background is fully opaque so the card covers the avatar, the music
 * screen and Roon without tearing any of them down. Removing the card later
 * reveals whatever is really underneath at that moment.
 *
 * Geometry comes from token-usage-ui-v6 (640x480, centre 320x240, 270
 * degree arcs starting at 135 degrees, outer blue / inner cyan). The
 * preview's ES6 motion script is intentionally not used: this firmware is
 * Android 4.4 with no WebView package installed.
 */
public final class MiniAppCardView extends View {

    private final PrinterCardRenderer printerRenderer = new PrinterCardRenderer();

    private static final String TAG = "MiniAppCard";
    private static final float WIDTH = 640f;
    private static final float HEIGHT = 480f;
    private static final float CX = 320f;
    private static final float CY = 240f;
    private static final float FACE_RADIUS = 223f;
    private static final float START_ANGLE = 135f;
    private static final float SWEEP = 270f;

    private static final int BACKGROUND = 0xFF000000;
    // V6 used #0d254f, which disappeared in a bright room. The host can
    // override it per card with "track_color".
    private static final int TRACK = 0xFF315A96;
    // User decision 2026-11-15 (D037): 900ms. The earlier 1200ms run hid
    // its own first half because the crest arrived after only ~290ms; the
    // shorter duration is paired with a longer, linear first leg below.
    private static final long ANIMATION_MS = 900L;
    private static final long HOME_DROP_MS = 300L;
    private static final long HOME_PAGE_MS = 2100L;
    // The entrance starts at HALF of each ring's own value and fills the
    // rest. Only the last half of an arc moves.
    private static final float START_FRACTION = 0.5f;
    // V6 motion.js spring() knots, adapted. Starting at 50% halves the
    // absolute travel, so the relative knots are doubled (1.035 -> 1.070,
    // 0.955 -> 0.910, ...) to keep the Q-bounce equally visible in percent
    // points. D037: the crest moves 0.24 -> 0.34 and the first leg is
    // LINEAR instead of 1-(1-q)^3. That cubic rushed ~86% of the travel
    // into the first 10% of the timeline, so the "filling up" part was
    // invisible at any frame rate. Later legs stay smoothstep.
    private static final float[][] SPRING = {
        {0.00f, 0.000f},
        {0.34f, 1.070f},
        {0.55f, 0.910f},
        {0.72f, 1.024f},
        {0.87f, 0.988f},
        {1.00f, 1.000f},
    };
    // D038 second revision (user screenshot 2026-11-15 22:44): there is NO
    // inner-ring special case any more. Both numbers use one rule -- same
    // 20px type, same ring centreline, same endpoint angle + rotation --
    // so they line up radially. Earlier attempts (28px inset, then 4px
    // inset + 15deg back-off + 18px type) made the inner number drift into
    // the dark gap or up the middle of the arc, and shrank its type.
    private static final float NUMBER_SIZE = 20f;
    private static final int TEXT = 0xFFF4F5FA;
    private static final int MUTED = 0xFF626F82;
    private static final int WARNING = 0xFFFF8A8A;

    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    // Dedicated bitmap paint: FILTER_BITMAP_FLAG is what removes the jagged
    // "furry" edge when the 256px logo is scaled down to the 94px slot.
    private final Paint iconPaint = new Paint(
            Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Paint trackPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint arcPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint textPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint smallPaint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final RectF oval = new RectF();
    private final RectF capBox = new RectF();
    private final RectF iconBox = new RectF();
    // Built once: allocating a RadialGradient on every animation frame used
    // to add GC churn and lengthen the real frame interval.
    private final Shader faceShader = new RadialGradient(
            CX, CY, FACE_RADIUS * 1.06f,
            new int[]{0xFF18202C, 0xFF10161F, 0x00000000},
            new float[]{0f, 0.72f, 1f}, Shader.TileMode.CLAMP);
    private final Handler handler = new Handler();

    private JSONObject card = null;
    private Bitmap icon = null;
    private String iconName = "";
    // Static layer (opaque background, radial gradient, both track arcs,
    // centre logo and footer). Drawn once per card instead of on every
    // animation frame: software-rendering the radial gradient was the
    // single most expensive part of each frame (measured 23-26 fps before).
    private Bitmap backdrop = null;
    private final float[] shown = new float[2];
    private final float[] from = new float[2];
    private final float[] target = new float[2];
    private long animationStart = 0L;
    private long animationDuration = 900L;
    private boolean animating = false;
    private boolean running = false;
    private boolean homeFeedback = false;
    private final HomeFeedbackCard homeRenderer = new HomeFeedbackCard();
    private int homePage = 0;
    private long homePageAt = 0L;
    private float homeProgress = 1f;
    private float wobble = 0f;
    // Frame instrumentation. The device is software-rendered, so gfxinfo
    // reports no display lists; this log is the only trustworthy fps source.
    private int frameCount = 0;
    private long frameFirstAt = 0L;
    private long framePrevAt = 0L;
    private long frameGapSum = 0L;
    private long frameGapMax = 0L;

    private void resetFrames() {
        frameCount = 0;
        frameFirstAt = framePrevAt = frameGapSum = frameGapMax = 0L;
    }

    /**
     * Piecewise spring from V6 motion.js: a LINEAR fill to the crest (so the
     * first half of the entrance is actually visible), then smoothstep
     * between damped crests/troughs. The crest is above 1 so the arc really
     * passes its target before springing back.
     */
    private static float spring(float u) {
        u = Math.min(1f, Math.max(0f, u));
        for (int i = 1; i < SPRING.length; i++) {
            float[] a = SPRING[i - 1], b = SPRING[i];
            if (u <= b[0]) {
                float q = Math.min(1f, Math.max(0f,
                        (u - a[0]) / (b[0] - a[0])));
                if (i > 1) q = q * q * (3f - 2f * q);   // smoothstep
                return a[1] + (b[1] - a[1]) * q;
            }
        }
        return 1f;
    }

    private final Runnable frame = new Runnable() {
        @Override
        public void run() {
            if (!running) return;
            long now = SystemClock.uptimeMillis();
            if (frameCount == 0) frameFirstAt = now;
            else {
                long gap = now - framePrevAt;
                frameGapSum += gap;
                if (gap > frameGapMax) frameGapMax = gap;
            }
            framePrevAt = now;
            frameCount++;
            float u = (now - animationStart) / (float) animationDuration;
            if (homeFeedback) homeProgress = clamp(u, 0f, 1f);
            if (u >= 1f) {
                animating = false;
                wobble = 0f;
                shown[0] = target[0];
                shown[1] = target[1];
                long span = now - frameFirstAt;
                Log.d(TAG, "anim frames=" + frameCount + " span=" + span
                        + "ms avg=" + (frameCount > 1
                                ? frameGapSum / (frameCount - 1) : 0)
                        + "ms max=" + frameGapMax + "ms fps="
                        + (span > 0 ? (int) (frameCount * 1000f / span) : 0));
            } else if (!homeFeedback) {
                float eased = spring(u);
                for (int i = 0; i < 2; i++) {
                    shown[i] = from[i] + (target[i] - from[i]) * eased;
                }
                // V6 endpoint squash/stretch: only after the crest, damped.
                wobble = u > 0.45f
                        ? (float) (Math.sin((u - 0.45f) * Math.PI * 8)
                                   * Math.exp(-(u - 0.45f) * 7) * 0.18f)
                        : 0f;
            }
            invalidate();
            if (animating) {
                // Align to an absolute 16ms grid instead of "wait 16ms after
                // this frame finishes". With the old form a 25ms software
                // render became a 41ms frame interval; now it stays at 32ms.
                long elapsed = SystemClock.uptimeMillis() - animationStart;
                long delay = 16L - (elapsed % 16L);
                handler.postDelayed(this, Math.max(0L, delay));
            }
        }
    };

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            if (!running) return;
            invalidate();                    // live countdown, not a snapshot
            handler.postDelayed(this, 1000);
        }
    };

    private final Runnable nextHomePage = new Runnable() {
        @Override
        public void run() {
            if (!running || !homeFeedback || homePageCount() < 2) return;
            homePage = 1;
            handler.removeCallbacks(frame);
            dropBackdrop();
            backdrop();
            startAnimation();
        }
    };

    public MiniAppCardView(Context context) {
        super(context);
        trackPaint.setStyle(Paint.Style.STROKE);
        trackPaint.setStrokeCap(Paint.Cap.ROUND);
        trackPaint.setColor(TRACK);
        arcPaint.setStyle(Paint.Style.STROKE);
        arcPaint.setStrokeCap(Paint.Cap.ROUND);
        textPaint.setColor(TEXT);
        textPaint.setTypeface(Typeface.create(Typeface.DEFAULT, Typeface.BOLD));
        textPaint.setTextAlign(Paint.Align.CENTER);
        smallPaint.setColor(TEXT);
        smallPaint.setTypeface(Typeface.create(Typeface.DEFAULT, Typeface.BOLD));
        smallPaint.setTextAlign(Paint.Align.CENTER);
    }

    /** Replace the whole card. Missing rings explicitly clear old values. */
    public void apply(JSONObject next) {
        apply(next, false);
    }

    /**
     * A fresh card_id replays the whole entrance animation from each ring's
     * halfway point; a refresh of the same card eases from its current
     * value instead.
     */
    public void apply(JSONObject next, boolean sameCard) {
        handler.removeCallbacks(frame);
        handler.removeCallbacks(tick);
        handler.removeCallbacks(nextHomePage);
        boolean keepHomePage = sameCard && homeFeedback;
        card = next;
        dropBackdrop();                 // layout may have changed
        running = card != null;
        animating = false;
        if (!running) {
            recycleIcon();
            invalidate();
            return;
        }
        String layout = str(next, "layout");
        homeFeedback = "lines".equals(str(next, "template"))
                && "home_feedback".equals(layout) && homePageCount() > 0;
        if (homeFeedback) {
            if (!keepHomePage) {
                homePage = 0;
                homePageAt = SystemClock.uptimeMillis() + HOME_PAGE_MS;
            }
            homePage = Math.min(homePage, homePageCount() - 1);
            recycleIcon();
            homeProgress = 1f;
            backdrop();
            if (!keepHomePage) startAnimation();
            else invalidate();
            if (homePage == 0 && homePageCount() > 1) {
                handler.postAtTime(nextHomePage, homePageAt);
            }
            return;
        }
        // Cards from different apps carry different bundled icons. Reload
        // when the name changes; the old bitmap must be recycled first.
        String wantIcon = str(next, "icon");
        if (icon == null || !wantIcon.equals(iconName)) {
            recycleIcon();
            icon = loadIcon(wantIcon);
            iconName = wantIcon;
        }
        JSONObject printer = "bambu_print".equals(layout) ? next.optJSONObject("printer") : null;
        JSONArray rings = next.optJSONArray("rings");
        for (int i = 0; i < 2; i++) {
            target[i] = 0f;
            if (printer != null && i == 0) {
                target[i] = clamp((float) printer.optDouble("progress", 0), 0f, 100f);
                if (Float.isNaN(target[i])) target[i] = 0f;
            } else if (rings != null && i < rings.length() && i < 2) {
                JSONObject ring = rings.optJSONObject(i);
                if (ring != null && !ring.isNull("value")) {
                    target[i] = (float) ring.optDouble("value", 0);
                    if (target[i] < 0) target[i] = 0;
                }
            }
            // A new card starts halfway up its own arc; a refresh of the
            // same card keeps easing from what is currently on screen.
            from[i] = sameCard ? shown[i] : target[i] * START_FRACTION;
            if (!sameCard) shown[i] = from[i];
        }
        if (printer != null) {
            if (target[0] > 0f) startAnimation();
            else {
                shown[0] = 0f;
                wobble = 0f;
                invalidate();
            }
            return;
        }
        startAnimation();
        handler.postDelayed(tick, 1000);
    }

    /** Replay the entrance animation without changing any card data. */
    public void replay() {
        if (card == null) return;
        handler.removeCallbacks(frame);
        for (int i = 0; i < 2; i++) {
            from[i] = target[i] * START_FRACTION;
            shown[i] = from[i];
        }
        startAnimation();
    }

    private void startAnimation() {
        animationStart = SystemClock.uptimeMillis();
        animationDuration = homeFeedback ? HOME_DROP_MS : ANIMATION_MS;
        homeProgress = 0f;
        wobble = 0f;
        animating = true;
        resetFrames();
        invalidate();
        handler.post(frame);
    }

    private static float homeDropOffset(float progress) {
        if (progress < 0.69f) {
            float fall = progress / 0.69f;
            return -34f + 38f * fall * fall;
        }
        float remaining = (1f - progress) / 0.31f;
        return 4f * remaining * remaining;
    }

    private int homePageCount() {
        JSONArray pages = card.optJSONArray("rows");
        return pages == null ? 0 : Math.min(pages.length(), 2);
    }

    /**
     * Debug only: freeze the entrance at normalised progress u, so the
     * overshoot and rebound can be exported frame by frame. Silent, and
     * replay() resumes a normal animation afterwards.
     */
    public void seek(float u) {
        if (card == null) return;
        handler.removeCallbacks(frame);
        animating = false;
        float p = Math.min(1f, Math.max(0f, u));
        if (homeFeedback) {
            homeProgress = p;
            invalidate();
            return;
        }
        float eased = spring(p);
        for (int i = 0; i < 2; i++) {
            shown[i] = from[i] + (target[i] - from[i]) * eased;
        }
        wobble = p > 0.45f
                ? (float) (Math.sin((p - 0.45f) * Math.PI * 8)
                           * Math.exp(-(p - 0.45f) * 7) * 0.18f)
                : 0f;
        invalidate();
    }

    /** Stop every timer and release the bitmap; the host removes the view. */
    public void stop() {
        running = false;
        animating = false;
        handler.removeCallbacks(frame);
        handler.removeCallbacks(tick);
        handler.removeCallbacks(nextHomePage);
        card = null;
        recycleIcon();
        dropBackdrop();
    }

    @Override
    protected void onDraw(Canvas canvas) {
        canvas.save();
        canvas.scale(getWidth() / WIDTH, getHeight() / HEIGHT);
        drawContent(canvas);
        canvas.restore();
    }

    /**
     * Offscreen render through the exact same drawing path. This firmware's
     * screencap cannot see overlay windows, so this is the only way to
     * inspect a card without asking the user to look at the projection.
     */
    public Bitmap snapshot() {
        Bitmap bitmap = Bitmap.createBitmap((int) WIDTH, (int) HEIGHT,
                Bitmap.Config.ARGB_8888);
        drawContent(new Canvas(bitmap));
        return bitmap;
    }

    private void drawContent(Canvas canvas) {
        if (card == null) {
            canvas.drawColor(BACKGROUND);
            drawFace(canvas);
            return;
        }
        if ("bambu_print".equals(str(card, "layout"))) {
            JSONObject printer = card.optJSONObject("printer");
            if (printer != null) {
                printerRenderer.draw(canvas, card, printer, shown[0], wobble);
                return;
            }
        }
        if (homeFeedback) {
            canvas.drawColor(BACKGROUND);
            canvas.save();
            canvas.translate(0f, homeDropOffset(homeProgress));
            iconPaint.setAlpha(Math.round(51f + 204f * clamp(homeProgress / 0.69f, 0f, 1f)));
            canvas.drawBitmap(backdrop(), 0f, 0f, iconPaint);
            iconPaint.setAlpha(255);
            float unfilled = 1f - clamp((homeProgress - 0.2f) / 0.8f, 0f, 1f);
            homeRenderer.drawMeter(canvas, 1f - unfilled * unfilled);
            canvas.restore();
            return;
        }
        if (!"rings".equals(str(card, "template"))) {
            // Text cards have no animation; keep their simple path intact.
            canvas.drawColor(BACKGROUND);
            drawFace(canvas);
            drawLines(canvas);
            drawFooter(canvas);
            return;
        }
        // One full-screen blit replaces the gradient, both tracks, the logo
        // and the footer. Only the live arcs/labels/countdown are redrawn.
        canvas.drawBitmap(backdrop(), 0f, 0f, null);
        drawRings(canvas);
    }

    private Bitmap backdrop() {
        if (backdrop == null) {
            Bitmap layer = Bitmap.createBitmap((int) WIDTH, (int) HEIGHT,
                    Bitmap.Config.ARGB_8888);
            Canvas canvas = new Canvas(layer);
            if (homeFeedback) {
                JSONObject page = card.optJSONArray("rows").optJSONObject(homePage);
                if (page == null) page = new JSONObject();
                Bitmap glyph = loadIcon(str(page, "icon"));
                try {
                    homeRenderer.draw(canvas, page, str(card, "status"), glyph);
                } finally {
                    if (glyph != null) glyph.recycle();
                }
            } else {
                canvas.drawColor(BACKGROUND);
                drawFace(canvas);
                drawTrack(canvas);
                drawIcon(canvas, 47f, CY);
                drawFooter(canvas);
            }
            backdrop = layer;
        }
        return backdrop;
    }

    private void dropBackdrop() {
        if (backdrop != null) {
            backdrop.recycle();
            backdrop = null;
        }
    }

    /** The two unchanging track arcs; the coloured arcs stay per-frame. */
    private void drawTrack(Canvas canvas) {
        JSONArray rings = card.optJSONArray("rings");
        if (rings == null) return;
        int fallback = colour(str(card, "track_color"), TRACK);
        int count = Math.min(2, rings.length());
        for (int i = 0; i < count; i++) {
            JSONObject ring = rings.optJSONObject(i);
            if (ring == null) continue;
            // A per-ring track colour lets one card use two different dark
            // tracks (orange/brown + purple/violet, green/teal + blue/steel).
            trackPaint.setColor(colour(str(ring, "track_color"), fallback));
            float radius = clamp((float) ring.optDouble("radius", 120), 20f, 260f);
            float width = clamp((float) ring.optDouble("width", 24), 4f, 60f);
            oval.set(CX - radius, CY - radius, CX + radius, CY + radius);
            trackPaint.setStrokeWidth(width);
            canvas.drawArc(oval, START_ANGLE, SWEEP, false, trackPaint);
        }
    }

    private void drawFace(Canvas canvas) {
        fill.setShader(faceShader);
        canvas.drawCircle(CX, CY, FACE_RADIUS * 1.06f, fill);
        fill.setShader(null);
    }

    private void drawRings(Canvas canvas) {
        JSONArray rings = card.optJSONArray("rings");
        if (rings == null) return;
        int count = Math.min(2, rings.length());
        for (int i = 0; i < count; i++) {
            JSONObject ring = rings.optJSONObject(i);
            if (ring == null) continue;
            float radius = clamp((float) ring.optDouble("radius", 120), 20f, 260f);
            float width = clamp((float) ring.optDouble("width", 24), 4f, 60f);
            oval.set(CX - radius, CY - radius, CX + radius, CY + radius);
            arcPaint.setStrokeWidth(width);
            arcPaint.setColor(colour(str(ring, "color"), 0xFF0987FF));
            float raw = shown[i];
            // Allow a little past 100% so the crest is visible as a real
            // overshoot instead of being flattened into a plateau.
            float fraction = Math.min(1.06f, Math.max(0f, raw / 100f));
            if (fraction > 0.0001f) {
                canvas.drawArc(oval, START_ANGLE, SWEEP * fraction, false, arcPaint);
                drawCap(canvas, radius, width, fraction,
                        colour(str(ring, "color"), 0xFF0987FF));
            }
            drawRingLabels(canvas, ring, radius, raw, fraction);
        }
        drawRingRows(canvas, rings, count);
    }

    /**
     * V6 endpoint squash/stretch. The arc's ROUND cap stays underneath; this
     * oval stretches and flattens it while the spring rebounds.
     */
    private void drawCap(Canvas canvas, float radius, float width,
                         float fraction, int colour) {
        if (wobble == 0f) return;
        float angle = START_ANGLE + SWEEP * fraction;
        double rad = Math.toRadians(angle);
        float x = (float) (CX + radius * Math.cos(rad));
        float y = (float) (CY + radius * Math.sin(rad));
        float rx = width / 2f * (1f + wobble);
        float ry = width / 2f * (1f - wobble * 0.75f);
        fill.setShader(null);
        fill.setColor(colour);
        capBox.set(x - rx, y - ry, x + rx, y + ry);
        canvas.save();
        canvas.rotate(angle + 90f, x, y);
        canvas.drawOval(capBox, fill);
        canvas.restore();
    }

    private void drawRingLabels(Canvas canvas, JSONObject ring, float radius,
                                float raw, float fraction) {
        String label = str(ring, "label");
        String valueText = str(ring, "value_text");
        double startRad = Math.toRadians(START_ANGLE);
        float sx = (float) (CX + radius * Math.cos(startRad));
        float sy = (float) (CY + radius * Math.sin(startRad));
        textPaint.setTextSize(19f);
        textPaint.setColor(TEXT);
        if (!label.isEmpty()) canvas.drawText(label, sx, sy + 7f, textPaint);

        // 2026-11-16 (user): direction icons (↓ / ↑) belong in the ROUND
        // CAP AT THE ARC START ("首部"), upright and fixed. They used to
        // travel with the growing arc end, where the tangent rotation
        // turned them into diagonal dashes. Keeping value_text here also
        // preserves the D040 rule "no percent on this ring".
        if (!valueText.isEmpty()) {
            textPaint.setTextSize(NUMBER_SIZE);
            canvas.drawText(valueText, sx, sy + 7f, textPaint);
            return;
        }

        // The spring may briefly pass 100%; never show an impossible number.
        // ONE rule for every value: the caption sits at the arc end. The old
        // "below 19% move it beside the start label" branch put a low CPU
        // reading at (222,338) -- 18px above the two rows at y=356 -- so a
        // 14% card read as "14%CPU 14%". An almost-empty arc has no end to
        // label, so skip the caption there; the bottom row carries the value.
        float displayed = Math.min(100f, Math.max(0f, raw));
        String number = Math.round(displayed) + "%";
        float end = START_ANGLE + SWEEP * Math.min(fraction, 1f) - 5f;
        if (fraction < 0.02f) return;
        // ONE rule for both rings: the number sits on the ring centreline
        // at the very end of the arc, with the same size and rotation. The
        // 30px band holds a 20px glyph radially; the last few pixels of a
        // centred "94%" may rest on the dark track just past the coloured
        // cap, but never leave the progress bar. Keeping both rings on the
        // same angle is what makes the two numbers line up radially.
        double endRad = Math.toRadians(end);
        float px = (float) (CX + radius * Math.cos(endRad));
        float py = (float) (CY + radius * Math.sin(endRad));
        float rotation = end + 90f;
        while (rotation > 90f) rotation -= 180f;
        while (rotation < -90f) rotation += 180f;
        canvas.save();
        canvas.rotate(rotation, px, py);
        textPaint.setTextSize(NUMBER_SIZE);
        canvas.drawText(number, px, py + 7f, textPaint);
        canvas.restore();
    }

    private void drawRingRows(Canvas canvas, JSONArray rings, int count) {
        JSONArray rows = card.optJSONArray("rows");
        int total = Math.min(6, rows == null ? 0 : rows.length());
        for (int i = 0; i < total; i++) {
            JSONObject row = rows.optJSONObject(i);
            if (row == null) continue;
            String label = str(row, "label");
            String value = str(row, "value");
            // ONLY a real reset timestamp turns this row into a live
            // countdown. Local metrics cards have no reset, so their host
            // text (e.g. "CPU 36%", "down 12.8 MB/s") stays verbatim.
            if (i < count) {
                JSONObject ring = rings.optJSONObject(i);
                if (ring != null) {
                    long resetAt = ring.optLong("reset_at_ms", 0);
                    if (resetAt > 0) value = resetText(resetAt);
                }
            }
            // V6 draws each reset line as one centred string. The two
            // labels/values have the same width, so both lines line up.
            // D037: both lines move up 16px and the gap shrinks 32 -> 26.
            float y = 356f + i * 26f;
            textPaint.setTextSize(20f);
            textPaint.setColor(TEXT);
            textPaint.setTextAlign(Paint.Align.CENTER);
            canvas.drawText(label + "   " + value, CX, y, textPaint);
        }
        textPaint.setTextAlign(Paint.Align.CENTER);
    }

    private void drawLines(Canvas canvas) {
        textPaint.setTextAlign(Paint.Align.CENTER);
        textPaint.setTextSize(27f);
        textPaint.setColor(TEXT);
        canvas.drawText(str(card, "title"), CX, 104f, textPaint);
        drawIcon(canvas, 36f, 173f);
        JSONArray rows = card.optJSONArray("rows");
        int total = Math.min(6, rows == null ? 0 : rows.length());
        for (int i = 0; i < total; i++) {
            JSONObject row = rows.optJSONObject(i);
            if (row == null) continue;
            float y = 272f + i * 37f;
            textPaint.setTextSize(19f);
            textPaint.setTextAlign(Paint.Align.LEFT);
            textPaint.setColor(MUTED);
            canvas.drawText(str(row, "label"), 84f, y, textPaint);
            textPaint.setTextAlign(Paint.Align.RIGHT);
            textPaint.setColor(TEXT);
            canvas.drawText(str(row, "value"), 556f, y, textPaint);
        }
        textPaint.setTextAlign(Paint.Align.CENTER);
    }

    private void drawIcon(Canvas canvas, float half, float centerY) {
        if (icon == null) return;
        iconBox.set(CX - half, centerY - half, CX + half, centerY + half);
        canvas.drawBitmap(icon, null, iconBox, iconPaint);
    }

    private void drawFooter(Canvas canvas) {
        String notice = str(card, "notice");
        if (!notice.isEmpty()) {
            smallPaint.setTextSize(14f);
            smallPaint.setColor("error".equals(str(card, "status"))
                    ? WARNING : MUTED);
            canvas.drawText(notice, CX, 428f, smallPaint);
        }
        smallPaint.setTextSize(13f);
        smallPaint.setColor(MUTED);
        canvas.drawText(str(card, "footer"), CX, 454f, smallPaint);
    }

    private String resetText(long atMs) {
        if (atMs <= 0) return "—";
        long left = (atMs - System.currentTimeMillis()) / 1000;
        if (left <= 0) return "等待更新";
        long days = left / 86400;
        long hours = (left % 86400) / 3600;
        long minutes = (left % 3600) / 60;
        if (days > 0) return days + "d " + pad(hours) + "h";
        return hours + "h " + pad(minutes) + "m";
    }

    private static String pad(long value) {
        return value < 10 ? "0" + value : String.valueOf(value);
    }

    private static float clamp(float value, float low, float high) {
        return value < low ? low : (value > high ? high : value);
    }

    private static int colour(String value, int fallback) {
        try {
            return Color.parseColor(value);
        } catch (Exception ignored) {
            return fallback;
        }
    }

    /** optString turns JSON null into the literal "null" on API 19. */
    private static String str(JSONObject object, String key) {
        String value = object.optString(key, "");
        return "null".equals(value) ? "" : value;
    }

    private Bitmap loadIcon(String name) {
        if (name == null || !name.endsWith(".png") || name.contains("/")) return null;
        InputStream stream = null;
        try {
            stream = getContext().getAssets().open(name);
            return BitmapFactory.decodeStream(stream);
        } catch (Exception ignored) {
            return null;
        } finally {
            if (stream != null) {
                try {
                    stream.close();
                } catch (Exception ignored) {
                }
            }
        }
    }

    private void recycleIcon() {
        if (icon != null) {
            icon.recycle();
            icon = null;
        }
        iconName = "";
    }
}

package cn.rokid.clock;

import android.graphics.Canvas;

/**
 * One clock skin. Implementations draw into the full window canvas
 * (device pixels, 640x480) and must stay silent and allocation-light.
 */
public interface ClockFace {
    String name();

    void draw(Canvas canvas, int width, int height, long nowMillis);
}

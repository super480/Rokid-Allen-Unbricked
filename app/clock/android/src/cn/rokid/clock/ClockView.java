package cn.rokid.clock;

import android.content.Context;
import android.graphics.Canvas;
import android.view.View;

/** Full-window view that paints whichever face the registry points at. */
public class ClockView extends View {

    public ClockView(Context context) {
        super(context);
    }

    @Override
    protected void onDraw(Canvas canvas) {
        super.onDraw(canvas);
        ClockFace face = FaceRegistry.get(FaceRegistry.readCurrentFace());
        face.draw(canvas, getWidth(), getHeight(), System.currentTimeMillis());
    }
}

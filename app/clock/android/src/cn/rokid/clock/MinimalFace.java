package cn.rokid.clock;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Rect;
import android.graphics.Typeface;
import java.util.Calendar;
import java.util.Locale;

/** A centred light dial with two live satellite time indicators. */
public final class MinimalFace implements ClockFace {
    private final String name;
    private final Calendar clock=Calendar.getInstance();
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Rect bounds=new Rect();
    private static final String[] DAYS={"sun","mon","tue","wed","thu","fri","sat"};
    private static final String[] NUM=new String[60];
    static { for(int i=0;i<60;i++) NUM[i]=String.format(Locale.US,"%02d",i); }
    public MinimalFace(String name) {
        this.name=name;
        paint.setTypeface(Typeface.create("sans-serif",Typeface.NORMAL));
        paint.setStrokeCap(Paint.Cap.BUTT);
    }
    @Override public String name() { return name; }
    @Override public void draw(Canvas c,int w,int h,long now) {
        c.drawColor(0xFF000000);
        float s=Math.min(w/640f,h/480f);
        c.save(); c.translate(w/2f,h/2f); c.scale(s*0.90f,s*0.90f);
        paint.setStyle(Paint.Style.STROKE); paint.setStrokeWidth(1.2f);
        paint.setColor(0xFFAAAAA8); c.drawCircle(0,0,205f,paint);
        paint.setStyle(Paint.Style.FILL); paint.setColor(0xFFE8E8E6);
        c.drawCircle(0,0,200,paint);
        clock.setTimeZone(java.util.TimeZone.getDefault()); clock.setTimeInMillis(now);
        paint.setTextAlign(Paint.Align.LEFT); paint.setTextSize(28f); paint.setColor(0xFF171916);
        c.drawText(DAYS[clock.get(Calendar.DAY_OF_WEEK)-1],-112,-116,paint);
        c.drawText(NUM[clock.get(Calendar.DAY_OF_MONTH)],-112,-82,paint);
        int hour=clock.get(Calendar.HOUR_OF_DAY), minute=clock.get(Calendar.MINUTE);
        float sec=clock.get(Calendar.SECOND)+clock.get(Calendar.MILLISECOND)/1000f;
        indicator(c,25,25,0xFF000000,(hour%12+minute/60f)*30f);
        indicator(c,25,94,0xFF54FFFF,(minute+sec/60f)*6f);
        paint.setColor(0xFF171916); paint.setTextSize(28f);
        label(c,NUM[hour],77,25); label(c,NUM[minute],77,94);
        c.restore();
    }
    private void label(Canvas c,String value,float x,float y) {
        paint.getTextBounds(value,0,value.length(),bounds);
        c.drawText(value,x,y-bounds.exactCenterY(),paint);
    }
    private void indicator(Canvas c,float x,float y,int color,float angle) {
        paint.setColor(color); paint.setStyle(Paint.Style.FILL); c.drawCircle(x,y,29,paint);
        double a=Math.toRadians(angle-90);
        float dx=(float)Math.cos(a),dy=(float)Math.sin(a);
        paint.setColor(0xFFF8F8F6); paint.setStrokeWidth(3f);
        c.drawLine(x+dx*14,y+dy*14,x+dx*28,y+dy*28,paint);
    }
}

package cn.rokid.clock;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Typeface;
import java.io.InputStream;
import java.util.Calendar;
import java.util.Locale;

/** Illustration and live clock text are independent, so the art never freezes time. */
public final class MilkFace implements ClockFace {
    private final String name;
    private final Context context;
    private final Typeface digits;
    private final Typeface dateFont=Typeface.create("sans-serif",Typeface.BOLD);
    private final Calendar clock=Calendar.getInstance();
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Path clip=new Path();
    private final Rect bounds=new Rect();
    private final RectF target=new RectF(-184,-184,184,184);
    private Bitmap background;
    private boolean tried=false;
    private long minuteKey=Long.MIN_VALUE;
    private String time="00:00",date="";
    private static final String[] WEEK={"日","一","二","三","四","五","六"};
    public MilkFace(String name,Context context,Typeface digits) {
        this.name=name;this.context=context.getApplicationContext();this.digits=digits;
        paint.setTextAlign(Paint.Align.CENTER);
        clip.addCircle(0,0,184,Path.Direction.CW);
    }
    @Override public String name() { return name; }
    private void load() {
        if(tried)return;tried=true;
        InputStream in=null;
        try {
            in=context.getAssets().open("faces/milk-background.png");
            BitmapFactory.Options options=new BitmapFactory.Options();
            options.inScaled=false;
            background=BitmapFactory.decodeStream(in,null,options);
        } catch(Exception e) {
            android.util.Log.w("ClockFace","milk background unavailable",e);
        } finally {
            if(in!=null)try {in.close();}catch(Exception ignored){}
        }
    }
    @Override public void draw(Canvas c,int w,int h,long now) {
        c.drawColor(0xFF000000);load();
        float s=Math.min(w/640f,h/480f);
        c.save();c.translate(w/2f,h/2f);c.scale(s,s);
        c.save();c.clipPath(clip);
        paint.setStyle(Paint.Style.FILL);paint.setColor(0xFF4BAAD1);
        c.drawCircle(0,0,184,paint);
        if(background!=null)c.drawBitmap(background,null,target,paint);
        c.restore();
        paint.setStyle(Paint.Style.STROKE);paint.setColor(0xFFFBEAD0);
        paint.setStrokeWidth(8f);c.drawCircle(0,0,180,paint);
        paint.setStyle(Paint.Style.FILL);
        clock.setTimeZone(java.util.TimeZone.getDefault());clock.setTimeInMillis(now);
        if(minuteKey!=now/60000L) {
            minuteKey=now/60000L;
            time=String.format(Locale.US,"%02d:%02d",clock.get(Calendar.HOUR_OF_DAY),clock.get(Calendar.MINUTE));
            date=(clock.get(Calendar.MONTH)+1)+"月"+clock.get(Calendar.DAY_OF_MONTH)
                +"日  周"+WEEK[clock.get(Calendar.DAY_OF_WEEK)-1];
        }
        paint.setColor(0xFFFBEAD0);paint.setTypeface(dateFont);paint.setTextSize(16.5f);
        centered(c,date,0,-125);
        paint.setTypeface(digits);paint.setTextSize(66f);
        float width=paint.measureText(time);
        if(width>190)paint.setTextSize(66f*190/width);
        centered(c,time,0,-80);
        c.restore();
    }
    private void centered(Canvas c,String value,float x,float y) {
        paint.getTextBounds(value,0,value.length(),bounds);
        c.drawText(value,x,y-bounds.exactCenterY(),paint);
    }
}

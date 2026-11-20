package cn.rokid.clock;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.Typeface;
import java.io.InputStream;
import java.util.Calendar;
import java.util.Locale;

/** Layered night landscape, live gradient clock, foliage in front of digits. */
public final class ForestFace implements ClockFace {
    private final String name;
    private final Context context;
    private final Typeface digits;
    private final Typeface plain=Typeface.create("sans-serif",Typeface.NORMAL);
    private final Calendar clock=Calendar.getInstance();
    private final Paint paint=new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
    private final Rect bounds=new Rect();
    private final RectF target=new RectF(-184,-184,184,184);
    private final RectF rim=new RectF(-182,-182,182,182);
    private final Path clip=new Path();
    private final Shader gradient=new LinearGradient(-150,0,145,0,
        0xFFD7F6AA,0xFF98EED0,Shader.TileMode.CLAMP);
    private Bitmap background,foreground;
    private boolean tried=false;
    private long minuteKey=Long.MIN_VALUE;
    private String time="12:00",date="",period="AM";
    private static final String[] WEEK={"SUN","MON","TUE","WED","THU","FRI","SAT"};
    public ForestFace(String name,Context context,Typeface digits) {
        this.name=name;this.context=context.getApplicationContext();this.digits=digits;
        paint.setTextAlign(Paint.Align.CENTER);
        clip.addCircle(0,0,184,Path.Direction.CW);

    }
    @Override public String name() {return name;}
    private void load() {
        if(tried)return;tried=true;
        InputStream in=null;
        try {
            in=context.getAssets().open("faces/forest-background.png");
            BitmapFactory.Options options=new BitmapFactory.Options();options.inScaled=false;
            background=BitmapFactory.decodeStream(in,null,options);
            if(background!=null) makeForeground();
        } catch(Exception e) {android.util.Log.w("ClockFace","forest art unavailable",e);}
        finally {if(in!=null)try{in.close();}catch(Exception ignored){}}
    }
    private void makeForeground() {
        // Derive a foliage matte once from this flat illustration's green palette.
        // It uses the exact source pixels, avoiding an approximate hand-drawn edge.
        int w=background.getWidth(),h=background.getHeight();
        int[] pixels=new int[w*h];background.getPixels(pixels,0,w,0,0,w,h);
        for(int y=0;y<h;y++)for(int x=0;x<w;x++) {
            int i=y*w+x,v=pixels[i];
            int r=(v>>16)&255,g=(v>>8)&255,b=v&255;
            boolean canopy=x>w*0.16f && x<w*0.85f && y>h*0.40f && y<h*0.82f;
            if(!canopy || g<45 || g<r*0.80f || g<b*1.06f) pixels[i]=0;
        }
        foreground=Bitmap.createBitmap(pixels,w,h,Bitmap.Config.ARGB_8888);
    }
    @Override public void draw(Canvas c,int w,int h,long now) {
        c.drawColor(0xFF000000);load();
        float s=Math.min(w/640f,h/480f);
        c.save();c.translate(w/2f,h/2f);c.scale(s,s);
        paint.setShader(null);paint.setStyle(Paint.Style.FILL);paint.setColor(0xFF050C12);
        c.drawCircle(0,0,184,paint);
        c.save();c.clipPath(clip);
        if(background!=null)c.drawBitmap(background,null,target,paint);
        c.restore();
        clock.setTimeZone(java.util.TimeZone.getDefault());clock.setTimeInMillis(now);
        if(minuteKey!=now/60000L) {
            minuteKey=now/60000L;
            int hour=clock.get(Calendar.HOUR);if(hour==0)hour=12;
            time=String.format(Locale.US,"%d:%02d",hour,clock.get(Calendar.MINUTE));
            date=WEEK[clock.get(Calendar.DAY_OF_WEEK)-1]+" "+clock.get(Calendar.DAY_OF_MONTH);
            period=clock.get(Calendar.AM_PM)==Calendar.AM?"AM":"PM";
        }
        paint.setTypeface(plain);paint.setTextSize(24f);paint.setColor(0xFFCCE99A);
        centered(c,date,0,-112);
        paint.setTypeface(digits);paint.setTextSize(108f);paint.setShader(gradient);
        float width=paint.measureText(time);
        if(width>285)paint.setTextSize(108f*285/width);
        centered(c,time,-8,-48);
        paint.setShader(null);paint.setTypeface(plain);paint.setTextSize(13f);
        paint.setColor(0xFF9EA9A5);centered(c,period,153,-9);
        // Cached canopy is composited above digits, with its original pixel edges.
        if(foreground!=null) {
            c.save();c.clipPath(clip);
            paint.setColor(0xFFFFFFFF);c.drawBitmap(foreground,null,target,paint);c.restore();
        }
        paint.setStyle(Paint.Style.STROKE);paint.setStrokeWidth(1.8f);
        paint.setColor(0xFF2B3B3A);c.drawCircle(0,0,182,paint);
        // A fixed decorative highlight, not invented fitness/progress data.
        paint.setColor(0xFFB7E7C4);paint.setStrokeCap(Paint.Cap.ROUND);
        c.drawArc(rim,180,114,false,paint);
        paint.setStyle(Paint.Style.FILL);c.restore();
    }
    private void centered(Canvas c,String value,float x,float y) {
        paint.getTextBounds(value,0,value.length(),bounds);
        c.drawText(value,x,y-bounds.exactCenterY(),paint);
    }
}

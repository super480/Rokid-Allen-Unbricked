package cn.rokid.clock;

import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Typeface;
import android.os.BatteryManager;
import android.os.SystemClock;
import java.util.Calendar;
import java.util.Locale;

/** Black segmented Roman dial with open diamond hands and real power state. */
public final class RomanFace implements ClockFace {
    private final String name;
    private final Context context;
    private final Calendar clock=Calendar.getInstance();
    private final Paint fill=new Paint(Paint.ANTI_ALIAS_FLAG),line=new Paint(Paint.ANTI_ALIAS_FLAG),text=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path path=new Path();
    private final Rect bounds=new Rect();
    private final RectF oval=new RectF();
    private static final int CYAN=0xFF54FFFF,WHITE=0xFFF1F3F2;
    private static final String[] ROMAN={"XII","I","II","III","IV","V","VI","VII","VIII","IX","X","XI"};
    private static final String[] WEEK={"SUNDAY","MONDAY","TUESDAY","WEDNESDAY","THURSDAY","FRIDAY","SATURDAY"};
    private static final String[] MONTH={"JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"};
    private static final String[] TICKS={"","5","10","","20","25","","35","40","","50","55"};
    private long minuteKey=Long.MIN_VALUE,powerAt=-60000L;
    private String day="",weekday="",month="",digital="",date="",power="--";
    private float charge=-1;
    private boolean plugged=false;
    public RomanFace(String name,Context context) {
        this.name=name;this.context=context.getApplicationContext();
        text.setTypeface(Typeface.create("sans-serif-condensed",Typeface.NORMAL));
        text.setTextAlign(Paint.Align.CENTER);
        line.setStyle(Paint.Style.STROKE);line.setStrokeCap(Paint.Cap.ROUND);line.setStrokeJoin(Paint.Join.ROUND);
    }
    @Override public String name(){return name;}
    private void power() {
        long now=SystemClock.elapsedRealtime();if(now-powerAt<60000)return;powerAt=now;
        charge=-1;power="--";plugged=false;
        try {
            Intent b=context.registerReceiver(null,new IntentFilter(Intent.ACTION_BATTERY_CHANGED));
            if(b==null)return;
            plugged=b.getIntExtra(BatteryManager.EXTRA_PLUGGED,0)!=0;
            boolean present=b.getBooleanExtra(BatteryManager.EXTRA_PRESENT,false);
            int level=b.getIntExtra(BatteryManager.EXTRA_LEVEL,-1),scale=b.getIntExtra(BatteryManager.EXTRA_SCALE,-1);
            charge=present && level>=0 && scale>0?Math.max(0,Math.min(1,level/(float)scale)):-1;
            power=charge>=0?String.valueOf(Math.round(charge*100)):(plugged?"AC":"--");
        }catch(Exception e){charge=-1;power="--";}
    }
    @Override public void draw(Canvas c,int w,int h,long now) {
        c.drawColor(0xFF000000);power();
        float s=Math.min(w/640f,h/480f);c.save();c.translate(w/2f,h/2f);c.scale(s,s);
        arc(c,184,0,360,0xFF141616,1);
        // Fine outer minute indices and the quarter-hour triangles.
        for(int i=0;i<60;i++) {
            c.save();c.rotate(i*6f);
            line.setColor(i%5==0?0xFF999C9B:0xFF545958);line.setStrokeWidth(i%5==0?1.5f:1f);
            c.drawLine(0,-181,0,-(i%5==0?173:176),line);
            if(i%15==0) {
                fill.setColor(CYAN);path.reset();path.moveTo(-6,-181);path.lineTo(6,-181);path.lineTo(0,-170);path.close();c.drawPath(path,fill);
            }
            c.restore();
        }
        text.setTextSize(12);text.setColor(0xFFB2B6B4);
        for(int i=0;i<12;i++)if(!TICKS[i].equals("")) {
            c.save();c.rotate(i*30);c.translate(0,-174);if(i>3&&i<9)c.rotate(180);
            centered(c,TICKS[i],0,0);c.restore();
        }
        for(int i=0;i<4;i++)arc(c,148,i*90+9,72,0xFF272A29,20);
        text.setTextSize(21);
        for(int i=0;i<12;i++) {
            c.save();c.rotate(i*30);c.translate(0,-151);if(i>3&&i<9)c.rotate(180);
            text.setColor(i%3==0?WHITE:CYAN);centered(c,ROMAN[i],0,0);c.restore();
        }
        clock.setTimeZone(java.util.TimeZone.getDefault());clock.setTimeInMillis(now);
        if(minuteKey!=now/60000L) {
            minuteKey=now/60000L;
            day=String.valueOf(clock.get(Calendar.DAY_OF_MONTH));
            weekday=WEEK[clock.get(Calendar.DAY_OF_WEEK)-1];
            month=MONTH[clock.get(Calendar.MONTH)]+" "+clock.get(Calendar.YEAR);
            digital=String.format(Locale.US,"%02d:%02d",clock.get(Calendar.HOUR_OF_DAY),clock.get(Calendar.MINUTE));
            date=String.format(Locale.US,"%02d/%02d",clock.get(Calendar.MONTH)+1,clock.get(Calendar.DAY_OF_MONTH));
        }
        fill.setColor(CYAN);c.drawCircle(0,-113,16,fill);
        text.setTextSize(20);text.setColor(0xFF080B0A);centered(c,day,0,-113);
        text.setColor(WHITE);text.setTextSize(20);centered(c,weekday,0,-79);
        text.setTextSize(18);centered(c,month,0,-52);
        text.setTextSize(21);centered(c,digital,-72,29);centered(c,date,72,29);
        line.setColor(CYAN);line.setStrokeWidth(1.6f);c.drawCircle(-72,53,6,line);
        c.drawLine(-72,53,-72,49,line);c.drawLine(-72,53,-68,55,line);
        oval.set(66,47,78,59);c.drawRoundRect(oval,1,1,line);
        c.drawLine(66,51,78,51,line);c.drawLine(69,45,69,49,line);c.drawLine(75,45,75,49,line);
        arc(c,122,138,20,0xFF292D2C,2);arc(c,122,22,20,0xFF292D2C,2);
        powerDial(c);
        float sec=clock.get(Calendar.SECOND)+clock.get(Calendar.MILLISECOND)/1000f;
        float min=clock.get(Calendar.MINUTE)+sec/60;
        diamondHand(c,(clock.get(Calendar.HOUR)+min/60)*30,96,7);
        diamondHand(c,min*6,138,8);
        c.save();c.rotate(sec*6);line.setColor(WHITE);line.setStrokeWidth(1.4f);
        c.drawLine(0,13,0,-166,line);c.restore();
        fill.setColor(0xFF000000);c.drawCircle(0,0,5,fill);
        line.setColor(WHITE);line.setStrokeWidth(2);c.drawCircle(0,0,5,line);
        c.restore();
    }
    private void arc(Canvas c,float radius,float start,float sweep,int color,float width) {
        oval.set(-radius,-radius,radius,radius);line.setColor(color);line.setStrokeWidth(width);
        c.drawArc(oval,start,sweep,false,line);
    }
    private void centered(Canvas c,String value,float x,float y) {
        text.getTextBounds(value,0,value.length(),bounds);c.drawText(value,x,y-bounds.exactCenterY(),text);
    }
    private void diamondHand(Canvas c,float angle,float length,float width) {
        c.save();c.rotate(angle);line.setColor(WHITE);line.setStrokeWidth(2.7f);
        c.drawLine(0,13,0,-length+28,line);
        path.reset();path.moveTo(0,-length);path.lineTo(width,-length+17);
        path.lineTo(0,-length+31);path.lineTo(-width,-length+17);path.close();c.drawPath(path,line);
        c.restore();
    }
    private void powerDial(Canvas c) {
        c.save();c.translate(0,93);
        arc(c,39,180,180,0xFF39403E,3);
        if(charge>=0)arc(c,39,180,180*charge,CYAN,3);
        for(int i=0;i<=10;i++) {
            c.save();c.rotate(-90+i*18);line.setColor(0xFF8B9691);line.setStrokeWidth(1.5f);
            c.drawLine(0,-33,0,-29,line);c.restore();
        }
        if(charge>=0) {
            double a=Math.toRadians(180+charge*180);
            line.setColor(WHITE);line.setStrokeWidth(2);
            c.drawLine(0,0,(float)Math.cos(a)*27,(float)Math.sin(a)*27,line);
        }
        line.setColor(WHITE);line.setStrokeWidth(1.5f);c.drawCircle(0,0,5,line);
        if(plugged) {
            fill.setColor(CYAN);path.reset();path.moveTo(0,-33);path.lineTo(-6,-23);
            path.lineTo(-1,-23);path.lineTo(-4,-15);path.lineTo(7,-27);path.lineTo(2,-27);path.close();c.drawPath(path,fill);
        }
        text.setTextSize(23);text.setColor(WHITE);centered(c,power,0,22);c.restore();
    }
}

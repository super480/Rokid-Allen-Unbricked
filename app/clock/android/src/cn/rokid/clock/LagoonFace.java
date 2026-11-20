package cn.rokid.clock;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.Typeface;
import java.util.Calendar;

/** Blue-violet dial, broad minute band and a small vector crocodile. */
public final class LagoonFace implements ClockFace {
    private final String name;
    private final Calendar clock=Calendar.getInstance();
    private final Paint fill=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint line=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint text=new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Rect bounds=new Rect();
    private final Path croc=new Path(),mouth=new Path(),teeth=new Path(),title=new Path();
    private final Typeface numbers;
    private final Typeface serif=Typeface.create("serif",Typeface.BOLD);
    private static final String[] HOURS={"12","1","2","3","4","5","6","7","8","9","10","11"};
    private static final int BLUE=0xFF92A9E1, WHITE=0xFFE8EEFF, CYAN=0xFF54FFFF;
    public LagoonFace(String name) { this(name,null); }
    public LagoonFace(String name,Typeface typeface) {
        this.name=name;
        numbers=typeface!=null?typeface:Typeface.create("sans-serif-condensed",Typeface.BOLD);
        buildTitle();
        text.setTextAlign(Paint.Align.CENTER);
        line.setStyle(Paint.Style.STROKE); line.setStrokeCap(Paint.Cap.ROUND);
        line.setStrokeJoin(Paint.Join.ROUND);
        // Original vector tracing of the reference's cartoon ornament.
        croc.moveTo(8,42); croc.lineTo(3,35); croc.lineTo(10,33);
        croc.lineTo(9,23); croc.lineTo(18,25);
        croc.cubicTo(19,12,29,10,34,16); croc.cubicTo(40,8,48,12,49,20);
        croc.lineTo(68,23); croc.cubicTo(72,17,79,20,80,24);
        croc.cubicTo(90,21,93,27,90,34); croc.lineTo(88,40);
        croc.cubicTo(84,47,67,47,56,50); croc.lineTo(56,57);
        croc.cubicTo(36,63,16,56,8,49); croc.close();
        mouth.moveTo(37,35); mouth.cubicTo(49,42,72,40,86,35);
        teeth.moveTo(46,39); teeth.lineTo(49,43); teeth.lineTo(52,40);
        teeth.moveTo(62,40); teeth.lineTo(65,43); teeth.lineTo(68,39);
        teeth.moveTo(77,38); teeth.lineTo(80,41); teeth.lineTo(82,37);
    }
    private void box(float x,float y,float w,float h) {
        title.addRect(x,y,x+w,y+h,Path.Direction.CW);
    }
    private void buildTitle() {
        // Hand-drawn serif capitals; avoids missing serif aliases on the R2.
        title.moveTo(16,1);title.lineTo(16,8);title.lineTo(14.5f,8);
        title.cubicTo(13,0,4,0,4,12);title.cubicTo(4,24,12,24,16,18);
        title.lineTo(17,19);title.cubicTo(10,29,-1,25,0,12);
        title.cubicTo(0,1,9,-3,16,1);title.close();
        box(20,0,3.2f,24);box(32,0,3.2f,24);box(23,11,9,1.5f);
        box(18,0,7,1.2f);box(30,0,7,1.2f);box(18,22.8f,7,1.2f);box(30,22.8f,7,1.2f);
        box(42,0,3.2f,24);box(39,0,9,1.2f);box(39,22.8f,9,1.2f);
        box(52,0,1.4f,24);box(66,0,1.4f,24);
        box(50,0,5,1.2f);box(64,0,5,1.2f);box(50,22.8f,5,1.2f);
        title.moveTo(52,0);title.lineTo(55.5f,0);title.lineTo(67.4f,24);
        title.lineTo(64,24);title.close();
        title.moveTo(77,0);title.lineTo(79,0);title.lineTo(87,24);
        title.lineTo(83,24);title.lineTo(76.6f,4.5f);title.lineTo(70.5f,24);
        title.lineTo(69,24);title.close();
        box(73,15,9,1.4f);box(67,22.8f,6,1.2f);box(81,22.8f,8,1.2f);
    }
    @Override public String name() { return name; }
    private void disc(Canvas c,float r,int color) { fill.setColor(color); c.drawCircle(0,0,r,fill); }
    @Override public void draw(Canvas c,int w,int h,long now) {
        c.drawColor(0xFF000000);
        float s=Math.min(w/640f,h/480f);
        c.save();c.translate(w/2f,h/2f);c.scale(s,s);
        disc(c,184,0xFF485171); disc(c,176,0xFF000000);
        disc(c,174,BLUE); disc(c,138,0xFF2D324F);
        for(int i=0;i<60;i++) {
            c.save();c.rotate(i*6f);
            if(i%5==0) {
                fill.setColor(i%15==0?WHITE:0xFFBCD0FF);
                c.drawCircle(0,-158,i%15==0?8.2f:6.2f,fill);
            } else {
                line.setColor(0xFFC4D5FF);line.setStrokeWidth(1.3f);
                c.drawLine(0,-162,0,-153,line);
            }
            c.restore();
        }
        text.setTypeface(numbers);text.setTextSize(24f);text.setColor(WHITE);
        for(int i=0;i<12;i++) {
            double a=Math.toRadians(i*30-90);
            centered(c,HOURS[i],(float)Math.cos(a)*115,(float)Math.sin(a)*115);
        }
        text.setTypeface(serif);text.setTextSize(24f);text.setColor(0xFFB4C1EB);
        fill.setColor(0xFFB4C1EB);
        c.save();c.translate(-44.5f,-79f);c.drawPath(title,fill);c.restore();
        text.setTextSize(10f);text.setColor(0xFF5B6484);
        centered(c,"LUCKY TIME",0,-46);
        c.save();c.translate(5,2);
        fill.setColor(CYAN);c.drawPath(croc,fill);
        line.setColor(0xFF061513);line.setStrokeWidth(2.8f);
        c.drawPath(croc,line);c.drawPath(mouth,line);c.drawPath(teeth,line);
        fill.setColor(0xFF061513);
        c.drawCircle(30,20,2.4f,fill);c.drawCircle(42,19,2.4f,fill);
        c.drawCircle(79,27,1.5f,fill);c.drawCircle(85,27,1.5f,fill);
        c.restore();
        clock.setTimeZone(java.util.TimeZone.getDefault());clock.setTimeInMillis(now);
        float second=clock.get(Calendar.SECOND)+clock.get(Calendar.MILLISECOND)/1000f;
        float minute=clock.get(Calendar.MINUTE)+second/60f;
        float hour=(clock.get(Calendar.HOUR)+minute/60f)*30f;
        // Hub backing sits beneath hands; hour is shorter and near-white.
        disc(c,19,BLUE);
        hand(c,hour,95,12,WHITE,8);
        hand(c,minute*6f,115,12,BLUE,8);
        hand(c,second*6f,120,1.8f,CYAN,41);
        disc(c,12,WHITE);
        c.restore();
    }
    private void centered(Canvas c,String value,float x,float y) {
        text.getTextBounds(value,0,value.length(),bounds);
        c.drawText(value,x,y-bounds.exactCenterY(),text);
    }
    private void hand(Canvas c,float angle,float length,float width,int color,float tail) {
        c.save();c.rotate(angle);line.setColor(color);line.setStrokeWidth(width);
        c.drawLine(0,tail,0,-length,line);c.restore();
    }
}

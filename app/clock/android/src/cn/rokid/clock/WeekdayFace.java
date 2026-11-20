package cn.rokid.clock;

import android.graphics.Canvas;
import android.graphics.Paint;
import android.graphics.Path;
import android.graphics.Rect;
import android.graphics.RectF;
import android.graphics.Typeface;
import java.util.Calendar;
import java.util.Locale;

/** Heavy geometric HH/MM, a weekday rim, live seconds badge and date.
 * Custom vector digits preserve the reference's block shapes without a font
 * dependency. Only the dial is drawn: no white bezel or outer watch case.
 */
public final class WeekdayFace implements ClockFace {
    // Light cyan sampled from the factory Nebula basic_design palette.
    private static final int BLUE = 0xFF54FFFF;
    private static final String[] DAYS = {"MONDAY", "TUESDAY", "WEDNESDAY",
        "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"};
    private static final String[] MONTHS = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};
    private final String name;
    private final Calendar clock = Calendar.getInstance();
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Path[] digits = new Path[10];
    private final Rect bounds = new Rect();
    private final RectF oval = new RectF();
    private String date = "";
    private int cachedDay = -1, cachedMonth = -1;
    private static final String[] SECONDS = new String[60];
    static {
        for (int i=0;i<60;i++) SECONDS[i]=String.format(Locale.US,"%02d",i);
    }

    public WeekdayFace(String name) {
        this.name = name;
        text.setTypeface(Typeface.create("sans-serif", Typeface.BOLD));
        text.setTextAlign(Paint.Align.CENTER);
        fill.setStrokeCap(Paint.Cap.ROUND);
        buildDigits();
    }

    @Override public String name() { return name; }

    @Override public void draw(Canvas c, int width, int height, long nowMillis) {
        c.drawColor(0xFF000000);
        float s = Math.min(width / 640f, height / 480f);
        c.save();
        c.translate(width/2f, height/2f);
        c.scale(s,s);
        clock.setTimeZone(java.util.TimeZone.getDefault());
        clock.setTimeInMillis(nowMillis);
        int hour=clock.get(Calendar.HOUR_OF_DAY), minute=clock.get(Calendar.MINUTE);
        int day=clock.get(Calendar.DAY_OF_MONTH), month=clock.get(Calendar.MONTH);
        fill.setStyle(Paint.Style.FILL);
        drawPair(c,hour,-124f,BLUE);
        drawPair(c,minute,6f,0xFF777779);
        // The badge sits in front of the lower-right digit, with a fine black
        // separating edge, not a simulated progress ring or battery indicator.
        fill.setColor(0xFF151515); c.drawCircle(94f,120f,29f,fill);
        fill.setColor(BLUE); c.drawCircle(94f,120f,26.5f,fill);
        text.setColor(0xFF080808); text.setTextSize(23f);
        centered(c,SECONDS[clock.get(Calendar.SECOND)],94f,120f);
        if(day!=cachedDay || month!=cachedMonth) {
            cachedDay=day; cachedMonth=month;
            date=MONTHS[month]+String.format(Locale.US," %02d",day);
        }
        text.setColor(0xFFA0A0A2); text.setTextSize(14f);
        centered(c,date,0f,182f);
        c.restore();
    }

    private void centered(Canvas c,String value,float x,float y) {
        text.getTextBounds(value,0,value.length(),bounds);
        c.drawText(value,x,y-bounds.exactCenterY(),text);
    }

    private float digitWidth(int digit) { return digit==1?76f:112f; }

    private void drawPair(Canvas c,int value,float y,int color) {
        int a=value/10,b=value%10;
        float left=-(digitWidth(a)+12f+digitWidth(b))/2f;
        fill.setStyle(Paint.Style.FILL); fill.setColor(color);
        c.save(); c.translate(left,y); c.drawPath(digits[a],fill); c.restore();
        c.save(); c.translate(left+digitWidth(a)+12f,y);
        c.drawPath(digits[b],fill); c.restore();
    }

    private void round(Path p,float x,float y,float w,float h,float r) {
        oval.set(x,y,x+w,y+h);
        p.addRoundRect(oval,r,r,Path.Direction.CW);
    }

    private void buildDigits() {
        for(int i=0;i<10;i++) { digits[i]=new Path(); digits[i].setFillType(Path.FillType.EVEN_ODD); }
        Path p=digits[0]; round(p,0,0,112,118,32);
        round(p,55.2f,32,1.6f,54,0.8f);
        // 1 has a wide cap and a strong right-hand stem, as in the reference.
        p=digits[1]; p.moveTo(0,0); p.lineTo(47,0);
        p.cubicTo(65,0,76,11,76,29); p.lineTo(76,118);
        p.lineTo(32,118); p.lineTo(32,28); p.lineTo(0,28); p.close();
        // Solid folded 2, with a thin horizontal incision at the waist.
        p=digits[2]; p.moveTo(0,40);
        p.cubicTo(0,15,23,0,55,0); p.cubicTo(91,0,112,16,112,42);
        p.cubicTo(112,64,94,73,69,73); p.lineTo(34,73);
        p.lineTo(34,76); p.lineTo(112,76); p.lineTo(112,118);
        p.lineTo(42,118); p.cubicTo(13,118,0,105,0,84);
        p.cubicTo(0,61,18,44,48,44); p.lineTo(80,44);
        p.lineTo(80,41); p.lineTo(0,41); p.close();
        p=digits[3]; p.moveTo(0,0); p.lineTo(75,0);
        p.cubicTo(121,0,122,51,86,59); p.cubicTo(124,66,121,118,75,118);
        p.lineTo(0,118); p.lineTo(0,84); p.lineTo(66,84);
        p.lineTo(66,81); p.lineTo(27,81); p.lineTo(27,37);
        p.lineTo(66,37); p.lineTo(66,34); p.lineTo(0,34); p.close();
        p=digits[4]; p.moveTo(39,0); p.lineTo(91,0); p.lineTo(91,63);
        p.lineTo(112,63); p.lineTo(112,96); p.lineTo(91,96);
        p.lineTo(91,118); p.lineTo(52,118); p.lineTo(52,96);
        p.lineTo(0,96); p.lineTo(0,63); p.close();
        p.moveTo(52,34); p.lineTo(33,63); p.lineTo(52,63); p.close();
        p=digits[5]; p.moveTo(0,0); p.lineTo(112,0); p.lineTo(112,34);
        p.lineTo(40,34); p.lineTo(40,44); p.lineTo(74,44);
        p.cubicTo(124,44,124,118,74,118); p.lineTo(0,118);
        p.lineTo(0,84); p.lineTo(72,84); p.lineTo(72,81);
        p.lineTo(0,81); p.close();
        p=digits[6]; round(p,0,0,112,118,31);
        round(p,56,69,1.8f,25,0.9f);
        round(p,50,30,62,6,1f);
        p=digits[7]; p.moveTo(0,0); p.lineTo(112,0); p.lineTo(112,32);
        p.lineTo(66,118); p.lineTo(17,118); p.lineTo(63,35);
        p.lineTo(0,35); p.close();
        p=digits[8]; round(p,0,0,112,118,30);
        round(p,55,22,2,23,1); round(p,55,73,2,23,1);
        p=digits[9]; round(p,0,0,112,118,31);
        round(p,54,24,1.8f,25,0.9f); round(p,0,82,62,6,1f);
    }
}

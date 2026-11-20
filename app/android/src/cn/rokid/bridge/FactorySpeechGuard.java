package cn.rokid.bridge;

import android.app.ActivityManager;
import android.content.Context;
import android.content.pm.PackageManager;
import java.io.*;
import java.util.*;
import java.util.regex.*;

/** Cancels only verified factory callers' TTS request IDs during the selected-agent mode. */
final class FactorySpeechGuard {
    interface Listener { void record(String message); }
    private final Context context;private final RokidTts tts;private final Listener listener;
    private volatile boolean running=true;private volatile Process process;
    private final SpeechGate gate=new SpeechGate();
    private final LinkedHashSet<Integer> cancelled=new LinkedHashSet<Integer>();
    // When we cancel a factory request the engine tears that session down. A
    // request submitted in the same instant can end up queued behind a session
    // that is being cleared and never plays (the engine then answers every
    // later request with error 2). Callers wait a moment after a cancel.
    private volatile long lastCancelAt=0;
    private static final Pattern PID=Pattern.compile("doRequest: pid = (\\d+), req = ");
    private static final Pattern ID=Pattern.compile("tts\\.WSThread: seqno: \\\"(\\d+)\\\"\\s*$");
    private static final Pattern ASR=Pattern.compile("toStringResult: \\+ type 4 action null nlp null.*? asr (.*?) energy [-\\d.]+$");
    /** The canned "music partnership expired, use Bluetooth" notice. */
    private static final Pattern NOTICE=Pattern.compile(
        "合作到期|用蓝牙播放|请用蓝牙|酷狗|酷哥|酷我");
    FactorySpeechGuard(Context c,RokidTts t,Listener l){context=c;tts=t;listener=l;}
    void selected(boolean value){gate.selected(value,android.os.SystemClock.elapsedRealtime());}
    void direct(int mode){gate.direct(mode);}
    /** The bridge's own MediaPlayer is on screen; control words are ours. */
    void bridgePlaying(boolean value){gate.bridgePlaying(value);}
    /** Take the factory-skill rule the host sends down. */
    void rule(String regex){gate.rule(regex);}
    /** Take the home-control (HA) rule the host sends down; empty resets. */
    void haRule(String regex){gate.haRule(regex);}
    /** True when the factory handles this turn on its own. */
    boolean ownsFactoryTurn(String text){return gate.ownsFactoryTurn(text);}
    void wake(int prop){gate.wake();listener.record("wake prop="+prop);}
    void start(){
        if(context.checkCallingOrSelfPermission("android.permission.READ_LOGS")!=PackageManager.PERMISSION_GRANTED){listener.record("READ_LOGS not granted; guard disabled");return;}
        new Thread(new Runnable(){public void run(){loop();}},"factory-tts-guard").start();
    }
    private boolean factory(int pid){
        if(pid==android.os.Process.myPid())return false;
        List<ActivityManager.RunningAppProcessInfo> ps=((ActivityManager)context.getSystemService(Context.ACTIVITY_SERVICE)).getRunningAppProcesses();
        if(ps==null)return false;
        for(ActivityManager.RunningAppProcessInfo p:ps)if(p.pid==pid){String n=p.processName;return n.equals("com.rokid.centauri")||n.equals("com.rokid.rkexecutor")||n.equals("com.rokid.cloudappclient")||n.startsWith("com.rokid.cloudappclient:")||n.equals("com.rokid.system.cloudappclient")||n.startsWith("com.rokid.system.cloudappclient:");}
        return false;
    }
    private void loop(){
        // This KitKat logcat splits filters at the first colon. Logger's '(L:46)' tag
        // cannot be selected exactly; filter immediately on read, before storing any history.
        String[] filter={"logcat","-v","threadtime","*:D"};
        try{
            HashSet<String> historical=new HashSet<String>();
            Process snapshot=Runtime.getRuntime().exec(new String[]{"logcat","-d","-v","threadtime","*:D"});
            BufferedReader old=new BufferedReader(new InputStreamReader(snapshot.getInputStream(),"UTF-8"));String line;
            while((line=old.readLine())!=null)if(relevant(line))historical.add(line);old.close();snapshot.waitFor();
            process=Runtime.getRuntime().exec(filter);BufferedReader reader=new BufferedReader(new InputStreamReader(process.getInputStream(),"UTF-8"));
            int pendingPid=-1;String pendingStamp="";boolean submissionObserved=false;listener.record("ready");
            while(running&&(line=reader.readLine())!=null){
                if(!relevant(line))continue;
                if(historical.contains(line))continue;
                // If the notice text is visible in the log, widen the
                // suppression window around it as well. A music turn
                // already opened one; this covers a notice that arrives
                // on its own or with different wording.
                if(NOTICE.matcher(line).find()){
                    gate.suppress(android.os.SystemClock.elapsedRealtime(),6000L);
                    listener.record("factory notice seen, suppressing");
                }
                Matcher asr=ASR.matcher(line);
                if(line.contains("SirenJava")&&asr.find()){
                    String heard=asr.group(1);
                    // The recogniser prints the word "null" when it heard
                    // nothing usable. That is not a sentence: forwarding it
                    // would make the host answer a bare word.
                    if(heard!=null&&!heard.trim().isEmpty()&&!"null".equals(heard.trim())){
                        gate.asr(heard,android.os.SystemClock.elapsedRealtime());
                        listener.record("asr heard: "+heard);
                    }
                }
                int[] submitted=SpeechGate.submission(line);
                if(submitted!=null){
                    if(!submissionObserved){submissionObserved=true;listener.record("submission observer ready");}
                    consider(submitted[0],submitted[1],"submission");continue;
                }
                Matcher p=PID.matcher(line);if(line.contains("tts.WSThread")&&p.find()){pendingPid=Integer.parseInt(p.group(1));pendingStamp=line.length()>=18?line.substring(0,18):"";continue;}
                Matcher id=ID.matcher(line);
                if(id.find()){
                    int origin=pendingPid;pendingPid=-1;
                    if(origin>0&&line.startsWith(pendingStamp))consider(origin,Integer.parseInt(id.group(1)),"synthesis");
                    else listener.record("uncorrelated synthesis request="+id.group(1));
                }
            }
            reader.close();
        }catch(Exception e){if(running)listener.record("stopped: "+e.getClass().getSimpleName());}
        finally{if(process!=null)process.destroy();}
    }
    private static boolean relevant(String line){
        return line.contains("SirenJava:")||line.contains("tts.WSThread:")
            ||line.contains("CloudAppClient - TTSHelper.speakTTS(")
            ||line.contains("合作到期")||line.contains("蓝牙播放")
            ||line.contains("酷狗")||line.contains("酷哥")||line.contains("酷我");
    }
    private void consider(int origin,int request,String phase)throws Exception{
        if(cancelled.contains(request))return;
        if(!factory(origin)){listener.record("skip non-factory pid="+origin+" request="+request+" phase="+phase);return;}
        long now=android.os.SystemClock.elapsedRealtime();
        String reason=gate.reason(now);
        boolean suppressed=gate.suppressFactory(now);
        if(!reason.equals("eligible")&&!suppressed){
            listener.record("pass factory request="+request+" pid="+origin+" phase="+phase+" reason="+reason);return;
        }
        if(tts.cancelRequest(request)){
            cancelled.add(request);while(cancelled.size()>128)cancelled.remove(cancelled.iterator().next());
            lastCancelAt=System.currentTimeMillis();
            listener.record("cancel submitted request="+request+" pid="+origin+" phase="+phase
                +(suppressed?" suppressed":""));
        }else listener.record("cancel unavailable request="+request+" phase="+phase);
    }
    /** Wall-clock time of the last successful factory cancel, or 0. */
    long lastCancelAt(){return lastCancelAt;}
    void close(){running=false;if(process!=null)process.destroy();}
}

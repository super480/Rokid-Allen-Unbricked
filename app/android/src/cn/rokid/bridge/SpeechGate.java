package cn.rokid.bridge;

import java.util.regex.*;

/** Firmware-specific, testable policy; clock supplied by the caller. */
final class SpeechGate {
    private boolean active;
    private long hostUntil,replyUntil,localUntil;
    /**
     * Direct mode: the speaker answers on its own.
     *
     *   OFF     the factory keeps every turn
     *   HYBRID  the factory keeps the skills it owns, the model takes the
     *           rest
     *   FULL    the model takes every turn, the factory is always silenced
     */
    static final int DIRECT_OFF=0,DIRECT_HYBRID=1,DIRECT_FULL=2;
    private int directMode=DIRECT_OFF;
    private static final Pattern SUBMISSION=Pattern.compile("^\\S+\\s+\\S+\\s+(\\d+)\\s+\\d+\\s+[VDIWEF]\\s+CloudAppClient - TTSHelper\\.speakTTS\\(L:-?\\d+\\):\\s*speak TTS ttiId (\\d+)\\s*$");
    /**
     * Playback controls for the bridge's own MediaPlayer. The host owns
     * these turns (see intent.music_control), so while a clip is running
     * the factory must not answer them -- in off/hybrid mode it would
     * otherwise talk about its own (empty) music player.
     */
    private static final Pattern CONTROL=Pattern.compile(
        "(暂停|暂定|停一停|继续播放|继续放|接着放|恢复播放|继续听"
        +"|下一首|下一曲|换一首|换首歌|切歌|跳过"
        +"|上一首|上一曲|切上一首|回到上一首"
        +"|停止播放|停止音乐|别放了|不听了|关掉音乐|关闭音乐)");
    /** A question containing a music word is not a music command. */
    private static final Pattern QUESTION=Pattern.compile(
        ".*(什么|怎么|为什么|是谁|意思|讲讲|说说|解释|查一下|吗|呢"
        +"|故事|建议|意见|如何|能不能|可以吗|是不是|有没有).*");
    /** Asking for a song by name; must reach the host, not the factory. */
    private static final Pattern REQUEST=Pattern.compile(
        "^(给我放一首|给我来一首|给我放|给我来|我想听|我要听|想听|要听"
        +"|随便放一首|随便来一首|随便放|随便来"
        +"|来一首|来首|放一首|放首|播一首|点一首|点首|找一首|找首"
        +"|听一下|听首|搜一下|听歌|放点音乐|来点音乐|播放).*");
    /** Volume words now control our MediaPlayer's STREAM_MUSIC. */
    private static final Pattern VOLUME=Pattern.compile(
        "(音量|声音.*(大|小)|大声|小声|静音|取消静音|解除静音|恢复声音|打开声音"
        +"|调大|调小|调高|调低|大点声|小点声|大一点|小一点|调到|百分之)");
    private volatile boolean bridgePlaying=false;
    /** The bridge's MediaPlayer has a clip on the screen (or loading one). */
    synchronized void bridgePlaying(boolean value){bridgePlaying=value;}

    /**
     * The factory's music skill has a canned "合作到期，请用蓝牙播放"
     * notice. It can answer during a music turn even when the gate did
     * not take that turn (off mode, or a phrase the host did not parse),
     * so for a few seconds after any music turn every factory TTS request
     * is cancelled regardless of the usual eligibility check. Our own TTS
     * comes from this process and is never a factory request.
     */
    private long suppressUntil=0;
    private static final long SUPPRESS_MILLIS=6000L;
    synchronized void suppress(long now,long millis){
        long until=now+millis;
        if(until>suppressUntil)suppressUntil=until;
    }
    synchronized boolean suppressFactory(long now){return now<suppressUntil;}

    private static boolean musicTurn(String text){
        if(QUESTION.matcher(text).matches())return false;
        return (text.length()<=24&&REQUEST.matcher(text).find())
            || (text.length()<=12&&(CONTROL.matcher(text).find()
                                    ||VOLUME.matcher(text).find()));
    }
    /**
     * The turns the factory handles on its own. The host sends its own
     * copy of this rule with the status, so the two sides cannot disagree
     * about who answers a turn; the pattern here is only the fallback for
     * the case where the status has not arrived yet.
     *
     * Music is entirely the bridge's now: playback controls AND asking
     * for a song must reach the host. The factory would otherwise drive
     * its own (empty) player or its closed cloud catalogue instead of
     * our local library. A bare "播放" used to be in the rule and also
     * swallowed "继续播放" / "停止播放", so it is gone as well.
     */
    private Pattern factoryOwns=Pattern.compile(
        "(几点|点啦|报时|报个时间|什么时间|现在时间|今天.*(号|日|几)|日期|星期[几一二三四五六日天]|周[几一二三四五六日天]"
        +"|闹钟|提醒我|叫我|计时|倒计时"
        +"|关机|重启|飞行模式|wifi|无线网|蓝牙|亮度|连网"
        +"|天气|气温|下雨)");

    /** Take the rule the host sends; keep the old one if it does not compile. */
    synchronized void rule(String regex){
        if(regex==null||regex.isEmpty())return;
        try{factoryOwns=Pattern.compile(regex);}catch(Exception ignored){}
    }

    /**
     * Home-control turns ("打开茶几灯") belong to the host's Home
     * Assistant bridge in every direct mode: the factory would answer
     * "没找到该设备" while the host is already talking to HA. The host
     * sends one gate regex; an empty rule (HA off) never matches and
     * behaviour is exactly as before.
     */
    private Pattern haOwns=Pattern.compile("(?!)");

    /** Take the home-control rule the host sends. Empty resets to never. */
    synchronized void haRule(String regex){
        if(regex==null||regex.isEmpty()){haOwns=Pattern.compile("(?!)");return;}
        try{haOwns=Pattern.compile(regex);}catch(Exception ignored){}
    }

    /** True when the factory handles this turn on its own. */
    boolean ownsFactoryTurn(String raw){
        return factoryOwns.matcher(normalise(raw)).find();
    }

    /** The wake word removed, punctuation dropped, lower case. */
    private static String normalise(String raw){
        if(raw==null)return "";
        return raw.replaceFirst("^(若琪|若弃|洛奇|罗琪)","").replaceAll("[\\s，。！？,.!?：:、]","").toLowerCase();
    }
    synchronized void selected(boolean value,long now){
        hostUntil=now+3000;
        // A poll started before final ASR may return the old mode. Wait briefly for its acknowledgement.
        if(value==active||now>=localUntil){active=value;localUntil=0;}
    }
    /** Direct mode: the host answers unnamed turns, so we must silence the factory. */
    synchronized void direct(int mode){directMode=mode;}

    /** The mode name the host uses, mapped onto the values above. */
    static int directModeOf(String name){
        if("full".equals(name))return DIRECT_FULL;
        if("hybrid".equals(name))return DIRECT_HYBRID;
        return DIRECT_OFF;
    }
    synchronized void wake(){replyUntil=0;}
    synchronized void asr(String raw,long now){
        String text=normalise(raw);
        if(text.isEmpty()||text.equals("null"))return;
        // Any music turn silences the factory's canned notice for a few
        // seconds, even when this turn was not taken by the host.
        if(musicTurn(text))suppress(now,SUPPRESS_MILLIS);
        // Our own MediaPlayer is running: a control word belongs to the
        // host, whichever direct mode is on. Take the turn before the
        // factory starts talking about a player that plays nothing.
        if(bridgePlaying&&text.length()<=10&&!QUESTION.matcher(text).matches()
                &&CONTROL.matcher(text).find()){
            active=true;localUntil=now+2000;replyUntil=now+15000;return;
        }
        // "我想听晴天" and friends: our library plays it, so the factory
        // must not answer even in off mode where it owns unnamed turns.
        if(text.length()<=24&&!QUESTION.matcher(text).matches()
                &&REQUEST.matcher(text).find()){
            active=true;localUntil=now+2000;replyUntil=now+15000;return;
        }
        // "调大音量" / "静音" belong to our STREAM_MUSIC as well.
        if(text.length()<=12&&!QUESTION.matcher(text).matches()
                &&VOLUME.matcher(text).find()){
            active=true;localUntil=now+2000;replyUntil=now+15000;return;
        }
        // Home-control turns go to the host's HA bridge before anything
        // else claims them -- including factoryOwns ("把灯亮度调到一半"
        // contains 亮度 but is a lamp turn, not a screen one). Empty rule
        // (HA off) never matches, so nothing changes when HA is disabled.
        if(haOwns.matcher(text).find()){
            active=true;localUntil=now+2000;replyUntil=now+15000;return;
        }
        // The assistant names the user may say. Hermes is the only one
        // enabled in this build; the rest are kept so the device still
        // recognises them once the host enables them.
        // The recogniser picks a different character for the same syllable
        // ("小赫" arrives as "小鹤" or "小贺"), so the common mis-hearings
        // are listed too. A name the device does not know leaves the
        // factory free to answer, which produces two replies at once.
        if(text.matches("(找|切换到|切换成|切换|叫|请找)?(hermes|小赫|小鹤|小贺|小喝|小盒|小合|小荷|小赫默斯|小赫墨斯|小鹤默斯|小鹤墨斯|小和|赫尔墨斯|赫墨斯|小家|小佳|小嘉|小加|克劳德|柯劳德|科劳德|刻劳德|克劳得|克老德|克劳的|claude|cloud|gpt|chatgpt|老寇|老扣|老蔻|老k|codex|dsh|deepseek|大肥鱼|大非鱼|肥鱼|飞鱼|buddy|workbuddy|巴蒂|巴帝|把弟|沃克巴迪)")){active=true;localUntil=now+2000;}
        else if(text.matches("退出助手|退出小赫|退出小家|退出克劳德|退出hermes|退出老寇|退出gpt|退出巴蒂|退出大肥鱼|结束对话|返回待机")){active=false;localUntil=now+2000;}
        // Unnamed turn under direct mode. In full mode every turn is taken;
        // in hybrid mode the factory keeps the skills it owns.
        else if(directMode==DIRECT_FULL){active=true;localUntil=now+2000;}
        else if(directMode==DIRECT_HYBRID&&!ownsFactoryTurn(text)){active=true;localUntil=now+2000;}
        replyUntil=active?now+15000:0;
    }
    synchronized String reason(long now){
        return !active?"unselected":now>=hostUntil?"host_stale":now>=replyUntil?"outside_reply_window":"eligible";
    }
    static int[] submission(String line){
        Matcher m=SUBMISSION.matcher(line);if(!m.matches())return null;
        try{int pid=Integer.parseInt(m.group(1)),id=Integer.parseInt(m.group(2));return pid>0&&id>0?new int[]{pid,id}:null;}
        catch(NumberFormatException e){return null;}
    }
}

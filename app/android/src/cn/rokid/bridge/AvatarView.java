package cn.rokid.bridge;

import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.os.SystemClock;
import android.view.Choreographer;
import android.view.View;

/**
 * The floating character on the speaker screen.
 *
 * When an assistant is selected its own avatar is drawn here. When
 * nothing is on duty -- no assistant chosen, or the chosen artwork is not
 * ready -- the overlay is left clear so the device's native factory Rokid
 * eyes show through. The overlay never takes focus, so the factory wake
 * word keeps working.
 */
public final class AvatarView extends View {

    private static final int[] STATE_ORDER = {
            HermesAvatar.indexOf("idle"),
            HermesAvatar.indexOf("listening"),
            HermesAvatar.indexOf("thinking"),
            HermesAvatar.indexOf("working"),
            HermesAvatar.indexOf("speaking"),
            HermesAvatar.indexOf("confirmation"),
            HermesAvatar.indexOf("done"),
            HermesAvatar.indexOf("error"),
    };

    private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final HermesAvatar hermes;
    private final DeepSeekAvatar deepseek;
    private final WorkBuddyAvatar workbuddy;
    private final ChatGptAvatar chatgpt;
    private final ChatGptAvatar xiaojia;
    private final ChatGptAvatar claude;
    private final ChatGptAvatar gemini;
    private final NekoAvatar neko;

    private Choreographer scheduler;
    private boolean attached, scheduled;
    private long lastFrameNanos;

    private String drawnState = "", drawnText = "", drawnAgent = "";
    private long stateEpoch = SystemClock.uptimeMillis();

    private final String[] subtitles = {"", ""};
    private String subtitleSource = null;

    private final Choreographer.FrameCallback tick = new Choreographer.FrameCallback() {
        public void doFrame(long frameTimeNanos) {
            scheduled = false;
            if (!attached || getWindowVisibility() != VISIBLE) return;
            // Every other vsync, rather than a drifting Handler timer.
            if (lastFrameNanos == 0 || frameTimeNanos - lastFrameNanos >= 30000000L) {
                lastFrameNanos = frameTimeNanos;
                String state = BridgeService.uiState == null ? "idle" : BridgeService.uiState;
                String text = BridgeService.uiText == null ? "" : BridgeService.uiText;
                String agent = BridgeService.uiAgent == null ? "" : BridgeService.uiAgent;
                if (!state.equals(drawnState) || !text.equals(drawnText) || !agent.equals(drawnAgent)) {
                    invalidate();
                } else {
                    invalidate(0, 0, getWidth(), getHeight());
                }
            }
            schedule();
        }
    };

    public AvatarView(android.content.Context context) {
        super(context);
        hermes = new HermesAvatar(context);
        deepseek = new DeepSeekAvatar(context);
        workbuddy = new WorkBuddyAvatar(context);
        chatgpt = new ChatGptAvatar(context);
        xiaojia = new ChatGptAvatar(context, "xiaojia-blank-512.png");
        claude = new ChatGptAvatar(context, "claude-blank-512.png");
        gemini = new ChatGptAvatar(context, "gemini-blank-512.png");
        neko = new NekoAvatar(context);
    }

    private void schedule() {
        if (attached && getWindowVisibility() == VISIBLE && !scheduled) {
            scheduled = true;
            scheduler.postFrameCallback(tick);
        }
    }

    protected void onAttachedToWindow() {
        super.onAttachedToWindow();
        attached = true;
        scheduler = Choreographer.getInstance();
        lastFrameNanos = 0;
        schedule();
    }

    protected void onWindowVisibilityChanged(int visibility) {
        super.onWindowVisibilityChanged(visibility);
        if (!attached) return;
        if (visibility == VISIBLE) {
            lastFrameNanos = 0;
            invalidate();
            schedule();
        } else {
            scheduler.removeFrameCallback(tick);
            scheduled = false;
        }
    }

    protected void onDetachedFromWindow() {
        attached = false;
        if (scheduler != null) scheduler.removeFrameCallback(tick);
        scheduled = false;
        super.onDetachedFromWindow();
    }

    protected void onDraw(Canvas canvas) {
        canvas.save();
        canvas.scale(getWidth() / 640f, getHeight() / 480f);

        String state = BridgeService.uiState == null ? "idle" : BridgeService.uiState;
        String text = BridgeService.uiText == null ? "" : BridgeService.uiText;
        String agent = BridgeService.uiAgent == null ? "" : BridgeService.uiAgent;

        // The shared state may change after a body-only invalidation was queued.
        // Only a change of state restarts the animation clock: the host may
        // repeat the same state with new text, and resetting the clock then
        // would break the idle rhythm (a glance every 15 seconds).
        if (!state.equals(drawnState)) {
            stateEpoch = SystemClock.uptimeMillis();
        }
        drawnState = state;
        drawnText = text;
        drawnAgent = agent;

        boolean useHermes = "hermes".equals(agent);
        boolean useDeepSeek = "dsh".equals(agent);
        boolean useWorkBuddy = "workbuddy".equals(agent);
        boolean useChatGpt = "codex".equals(agent) || "gpt".equals(agent);
        boolean useNeko = "neko".equals(agent);
        ChatGptAvatar extra = "xiaojia".equals(agent) ? xiaojia
                : "claude".equals(agent) ? claude
                : "gemini".equals(agent) ? gemini : null;

        // A character is on duty when the chosen assistant's artwork is
        // ready. When nothing is on duty the overlay is left clear (it is a
        // TRANSLUCENT window), so the device's native factory Rokid eyes
        // show through -- the standby look is the original factory eyes.
        boolean hasCharacter = (useHermes && hermes.ready())
                || (useDeepSeek && deepseek.ready())
                || (useWorkBuddy && workbuddy.ready())
                || (useChatGpt && chatgpt.ready())
                || (useNeko && neko.ready())
                || (extra != null && extra.ready());

        if (hasCharacter) {
            canvas.drawColor(useHermes ? HermesAvatar.BACKGROUND
                    : useDeepSeek ? DeepSeekAvatar.BACKGROUND
                    : useWorkBuddy ? WorkBuddyAvatar.BACKGROUND
                    : useChatGpt ? ChatGptAvatar.BACKGROUND
                    : useNeko ? NekoAvatar.BACKGROUND : ChatGptAvatar.BACKGROUND);
        }

        boolean drawn = false;
        if (useHermes && hermes.ready()) {
            hermes.draw(canvas, state, stateEpoch);
            drawn = true;
        } else if (useDeepSeek && deepseek.ready()) {
            deepseek.draw(canvas, state, stateEpoch);
            drawn = true;
        } else if (useWorkBuddy && workbuddy.ready()) {
            workbuddy.draw(canvas, state, stateEpoch);
            drawn = true;
        } else if (useChatGpt && chatgpt.ready()) {
            chatgpt.draw(canvas, state, stateEpoch);
            drawn = true;
        } else if (useNeko && neko.ready()) {
            neko.draw(canvas, state, stateEpoch);
            drawn = true;
        } else if (extra != null && extra.ready()) {
            extra.draw(canvas, state, stateEpoch);
            drawn = true;
        }
        // else: nothing to draw -- the native factory eyes show through.

        if (drawn) {
            drawCaptions(canvas, state, text, agent, useHermes, useDeepSeek);
        }
        canvas.restore();
    }

    private void drawCaptions(Canvas canvas, String state, String text, String agent,
                              boolean useHermes, boolean useDeepSeek) {
        paint.setTextAlign(Paint.Align.CENTER);
        paint.setTypeface(android.graphics.Typeface.DEFAULT);

        boolean useWorkBuddy = "workbuddy".equals(agent);
        boolean useChatGpt = "codex".equals(agent) || "gpt".equals(agent);
        boolean useNeko = "neko".equals(agent);
        boolean useExtra = "xiaojia".equals(agent) || "claude".equals(agent)
                || "gemini".equals(agent);
        String caption;
        String detail;
        if (useHermes) {
            int index = HermesAvatar.indexOf(state);
            caption = HermesAvatar.CAPTIONS[index];
            detail = HermesAvatar.DETAILS[index];
        } else if (useDeepSeek) {
            int index = DeepSeekAvatar.indexOf(state);
            caption = DeepSeekAvatar.CAPTIONS[index];
            detail = DeepSeekAvatar.DETAILS[index];
        } else if (useWorkBuddy) {
            int index = WorkBuddyAvatar.indexOf(state);
            caption = WorkBuddyAvatar.CAPTIONS[index];
            detail = WorkBuddyAvatar.DETAILS[index];
        } else if (useChatGpt || useExtra) {
            int index = ChatGptAvatar.indexOf(state);
            caption = ChatGptAvatar.CAPTIONS[index];
            detail = ChatGptAvatar.DETAILS[index];
            if (useExtra && index == 0) caption = agentName(agent) + " 在这里";
        } else if (useNeko) {
            int index = NekoAvatar.indexOf(state);
            caption = NekoAvatar.CAPTIONS[index];
            detail = NekoAvatar.DETAILS[index];
        } else {
            caption = label(state, agent);
            detail = "";
        }

        boolean character = useHermes || useDeepSeek || useWorkBuddy
                || useChatGpt || useNeko || useExtra;
        paint.setColor(character ? Color.rgb(235, 245, 242) : Color.rgb(167, 171, 178));
        paint.setTextSize(22);
        canvas.drawText(caption, 320, 413, paint);

        String body = text.replace('\n', ' ').trim();
        if (!body.isEmpty()) {
            detail = body;
        }
        if (!detail.isEmpty()) {
            paint.setColor(character ? Color.rgb(184, 207, 204) : Color.rgb(200, 204, 210));
            paint.setTextSize(20);
            canvas.drawText(ellipsize(detail, 380), 320, 439, paint);
        }
    }

    /** The one-line label under the character, which has no per-state captions. */
    private static String label(String state, String agent) {
        if ("listening".equals(state)) return "我在听";
        if ("thinking".equals(state)) return "让我想一想";
        if ("working".equals(state) || "queued".equals(state)) return "正在处理";
        if ("speaking".equals(state)) return "正在回答";
        if ("error".equals(state)) return "遇到一点问题";
        String name = agentName(agent);
        if (name.isEmpty()) return "喊若琪，直接问";
        return name + " · 待机";
    }

    /** The assistant currently in charge, for the idle label. */
    static String agentName(String agent) {
        if (agent == null || agent.isEmpty()) return "";
        if ("hermes".equals(agent)) return "小赫";
        if ("xiaojia".equals(agent)) return "小家";
        if ("claude".equals(agent)) return "克劳德";
        if ("gemini".equals(agent)) return "Gemini";
        if ("direct".equals(agent)) return "直连";
        if ("workbuddy".equals(agent)) return "巴蒂";
        if ("dsh".equals(agent)) return "大肥鱼";
        if ("codex".equals(agent) || "gpt".equals(agent)) return "GPT";
        if ("neko".equals(agent)) return "猫娘";
        // "direct" and "none" mean no assistant is named.
        return "";
    }

    private String ellipsize(String value, int maxWidth) {
        if (paint.measureText(value) <= maxWidth) return value;
        String line = value;
        while (!line.isEmpty() && paint.measureText(line + "…") > maxWidth) {
            int n = line.length() - 1;
            if (n > 0 && Character.isLowSurrogate(line.charAt(n))) n--;
            line = line.substring(0, n);
        }
        return line + "…";
    }
}

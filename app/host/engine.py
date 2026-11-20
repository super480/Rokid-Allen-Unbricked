"""The engine: what happens to a spoken turn.

Owns the session state (which assistant is selected), the work queue, and the
command queue the device polls. The HTTP layer only moves bytes; all the
decisions live here.
"""
from __future__ import annotations

import collections
import difflib
import json
import os
import queue
import re
import shutil
import socket
import threading
import time
import urllib.parse
import uuid

import config as config_module
import intent
import mac_speech
import speech
from backends import load_backend
from backends.direct import FULL, HYBRID, OFF, DirectBackend, normalise_mode
from backends.workbuddy_oauth import WorkBuddyOAuth
from config import REGISTRY, agent_avatar, agent_design_dir, agent_name, agent_settings
from history import HistoryStore, MEMORY_WINDOW
from tts_recovery import TtsRecovery
from app_store import AppStore
from miniapps import MiniAppService
from weather import FACTORY_WEATHER_WORDS, WeatherService
from clock import ClockService
from homeassistant import HomeAssistantService

# Proactive chat (B plan, route R3). The N.E.K.O. server delivers its own
# proactive stream only to the desktop client's "current" WS connection, so
# the host starts a short turn itself instead. The prompt is deliberately
# marked as a system request: it still goes through the character, memory
# and persona, but must never be mistaken for something the user said.
PROACTIVE_PROMPT = (
    "（系统请求，不是用户在说话）请按你自己的性格，主动说一句简短自然的话，"
    "像忽然想到什么想跟我说。一到两句，30 字以内。只说这一句，"
    "话题只限眼前的小事和此刻的感受，不用发链接或贴图，"
    "无需问候或解释，也不引用上文。"
)
# N.E.K.O. scans stream_data for spoken refusals: the old wording contained
# "不要推荐歌曲" and was read as the user declining music recommendations,
# which paused its music suggestions for 20 minutes on every unsolicited
# turn (10 false positives on 2026-11-10). It also sends every user turn to
# fact extraction, so this long instruction ended up in the catgirl's memory.
# memory_text separates what the model sees from what is stored: the model
# still gets PROACTIVE_PROMPT, long-term memory only sees a neutral filler.
PROACTIVE_MEMORY_TEXT = "嗯。"


def hhmm_to_minutes(value, fallback=0):
    """'23:30' -> 1410. '24:00' is midnight, i.e. 0."""
    match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(value or ""))
    if not match:
        return fallback
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 24 or minute > 59:
        return fallback
    return ((hour % 24) * 60 + minute) % 1440


def in_quiet_window(now_minutes, start, end):
    """Is now inside [start, end)? start > end means it crosses midnight."""
    if start == end:
        return False
    if start < end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


# Only 若琪 and 小家 may control the house. Another assistant that hears
# a home-control sentence answers with this fixed hint instead: no HA
# call, no model turn, no device action. Keeping it a constant makes the
# wording easy to change without touching the routing decision.
HOME_REDIRECT_HINT = "控制家居设备请找小家"

# The same utterance can reach the host twice: once over the Rokid event bus
# and once from the logcat watcher. Each report carries a fresh request id,
# so the per-request-id guard cannot collapse them. Any repeated wording
# inside this window is treated as one utterance.
DEDUP_WINDOW = 3.0

# The speaker hears itself. When the host speaks "先说找小赫，我才能接你的
# 任务。", the microphone picks that up again and the recogniser returns
# "找小鹤" -- which then looks like a fresh command and starts the whole
# thing over. An utterance that overlaps a recent broadcast is an echo and
# is dropped.
#
# The window opens when the broadcast is queued and closes when the device
# reports it finished. The tail covers the recogniser that was still busy
# with the audio when the report arrived.
ECHO_TAIL = 4.0
ECHO_SIMILARITY = 0.75
ECHO_MIN_SPOKEN = 3

# How long a broadcast stays open waiting for the device to report that it
# finished. A missing acknowledgement must not leave the window open for
# ever.
ECHO_OPEN_TIMEOUT = 30.0

# A broadcast longer than this is treated as finished even without the
# device's report, so a lost acknowledgement cannot swallow the user's
# words.
ECHO_MAX_PLAYBACK = 8.0

# Minimum gap between two wake-driven switches, whatever the assistant.
# The wake engine re-reports its own output in short bursts with different
# labels; a same-agent window alone lets those bursts flip the screen every
# second. Naming two different assistants inside this gap is not a real
# interaction.
WAKE_BURST_GAP = 1.5

# Two consecutive TTS failures mean the device's synthesis engine is wedged.
TTS_RECOVER_THRESHOLD = 2

# How many turns may wait for an answer at once. A question is never
# dropped while there is room in the queue; beyond this the queue is
# full and the newest question is turned away with an explanation.
MAX_PENDING_TURNS = 8


# One short line when a spoken song request cannot be fulfilled. Success
# needs no announcement: the song starting is the answer. Keep these under
# ten characters so the speaker never talks over the music for long.
MUSIC_FAILURE_HINTS = (
    ("连不上", "网上暂时放不了。"),
    ("没有找到", "没找到这首歌。"),
    ("曲库是空的", "本地曲库是空的。"),
    ("歌名", "没听清歌名。"),
    ("音频", "这首歌暂时放不了。"),
    ("放不了", "这首歌暂时放不了。"),
)


def _music_failure_hint(reason):
    for needle, hint in MUSIC_FAILURE_HINTS:
        if needle in reason:
            return hint
    return "没找到这首歌。"


class Engine:
    def __init__(self, config, state_file=None):
        self.config = config
        self.state_file = state_file
        self.lock = threading.RLock()
        self.commands = collections.deque(maxlen=200)
        self.jobs = queue.Queue()
        self.transcript = collections.deque(maxlen=MEMORY_WINDOW)
        # The number the last transcript entry got. The console asks for
        # everything after it, so it only draws what is new. It is kept
        # on disk as well, so a restart does not start counting again.
        self.transcript_seq = 0
        # The conversation is one append-only file per assistant. Each
        # line is flushed as it is written, so the record is never
        # truncated and no assistant can push another one's words out.
        self.history = HistoryStore(state_file)
        self._import_legacy_transcript()
        with self.lock:
            for item in self.history.window(MEMORY_WINDOW):
                self.transcript.append(item)
            self.transcript_seq = self.history.max_seq
        self.seen = set()
        self.cancelled = set()
        # Normalised text of the last utterance, with the time it arrived.
        # Used to collapse the same sentence reported by both ASR channels.
        self.last_utterance = ("", 0.0)
        # The last assistant picked by a wake word, and when. The wake log
        # prints the same hit twice (engine + ASR options), so a short
        # window keeps one wake from switching and logging twice.
        self.last_wake_route = ("", 0.0)
        # Broadcasts we sent recently, so an echo of our own voice can be
        # recognised and dropped instead of answered.
        self.recent_speeches = collections.deque(maxlen=16)
        # Ear-only mode's other half: the Mac says the answers the speaker no
        # longer reads. Built once and reconfigured per call, so the voice in
        # the config can change without a restart.
        self.mac_speaker = mac_speech.MacSpeech()
        # Relay mode has no device TTS completion callback to reset the
        # speaker's avatar. Timers scheduled here bring it back to idle when
        # the desktop finishes reading; kept so close() can cancel them.
        self._avatar_idle_timers = set()
        self.selected = None
        # The state each assistant was last seen in. The console shows the
        # matching face.
        self.agent_states = {}
        self.busy = False
        self.active_request_id = ""
        self.active_text = ""
        self.queued_texts = []
        self.seq = 0
        self.instance_id = uuid.uuid4().hex[:12]
        self.tts_errors = 0
        # What the speaker's own MediaPlayer is doing. Timed TTS replies and
        # music both use the audio channel, so the console needs one place
        # to show "playing / finished / failed" without polling the device.
        self.audio_state = "idle"   # idle | playing | paused | error
        self.audio_title = ""
        # Filled in by the HTTP layer: it owns the music library, so
        # "next"/"prev" need a callback that can queue another track.
        # A spoken song request needs the same, with local-first lookup.
        self.music_hook = None
        self.music_request_hook = None
        # Set by the HTTP layer: the Roon sidecar client. It decides whether
        # a spoken playback command drives our own MediaPlayer or the Roon
        # zone. None means Roon is not installed/authorised.
        self.roon_client = None
        # STREAM_MUSIC volume, reported back by the device after each
        # command. None until the first acknowledgement arrives.
        self.music_volume = None
        self.music_volume_max = None
        self.music_muted = False
        # The device reports a wedged TTS engine; the host restarts it.
        self.tts_recovery = TtsRecovery(config)
        # Mini-apps own their own modules; this is the whole host-side wiring.
        self.miniapps = MiniAppService(self, config=config.get("miniapps") or {})
        # The rebuilt weather skill: the native card plus a local data
        # backend. Disabled by default; see weather.py for the story.
        self.weather = WeatherService(self, config=config.get("weather") or {})

        # The standby clock card (设置 · 小应用 · 待机时钟): voice turns
        # like 「换个表盘」switch the device-side face over adb.
        self.clock = ClockService(self, config=config.get("clock") or {})
        self.app_store = AppStore(self)
        # Home Assistant (功能三). No local intent parsing: a home turn's
        # whole sentence goes to HA's conversation agent. The gate only
        # decides *whether* the sentence is a home turn (routing.py).
        self.homeassistant = HomeAssistantService(
            config=self.config.get("homeassistant") or {},
            save=self._save_config, engine=self)
        # The replacement data API lives in server.py (it owns the port);
        # the reference lets the console's override switch start / stop
        # it at runtime without a host restart.
        self.weather_api = None
        self.backends = {}
        # Browser sign-ins in progress, and the helper that drives them.
        # Both live in memory: a restart cancels a sign-in.
        self._logins = {}
        self._oauth_cache = {}
        self.closed = False
        self.worker = threading.Thread(target=self._work, name="engine", daemon=True)
        self.worker.start()
        # Proactive chat: a timer that only submits a job. The single worker
        # above still runs it, so an unsolicited turn can never overlap a
        # question the user just asked (one NekoBackend WS, one pending turn).
        self._proactive_stop = threading.Event()
        self._proactive_last_at = time.time()
        self._proactive_thread = threading.Thread(
            target=self._proactive_loop, name="neko-proactive", daemon=True)
        self._proactive_thread.start()
        self._restore_selection()

    def resync_display(self):
        """Tell a device that just connected what the screen should show.

        The device builds its overlay only when a display command arrives.
        A device that was restarted -- or one that just connected to a
        host that had already been running -- asks for the newest command
        and never sees the one that built the overlay before. Without this
        the screen stays empty until the next turn.

        The state is re-sent, not replayed: the command queue keeps every
        command of the session, and replaying it would repeat old speech.
        """
        with self.lock:
            agent = self.selected or "none"
            state = self.agent_states.get(agent, "idle")
        return self.emit("display", agent=agent, state=state)

    def _restore_selection(self):
        """Pick the assistant back up after a restart.

        The choice is kept in the state file, so a restart of the host
        does not silently drop the user back to the factory. An assistant
        that is no longer enabled is not restored either: it would answer
        nothing.
        """
        saved = self.store.get("selected_agent")
        if not saved:
            return
        item = REGISTRY.get(saved)
        if not item or not item.get("enabled"):
            return
        with self.lock:
            self.selected = saved
        self.emit("display", agent=saved, state="idle")

    # -- persistent state -----------------------------------------------------

    def _load_state(self):
        import json
        import os
        from pathlib import Path
        if not self.state_file or not Path(self.state_file).exists():
            return {}
        try:
            return json.loads(Path(self.state_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self):
        if not self.state_file:
            return
        import json
        import os
        from pathlib import Path
        path = Path(self.state_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as handle:
                os.chmod(temp, 0o600)
                json.dump(self.store, handle, ensure_ascii=False, indent=2)
            os.replace(temp, path)
        except OSError:
            pass

    @property
    def store(self):
        """Mutable dict persisted between restarts (session ids, ...)."""
        if not hasattr(self, "_store"):
            self._store = self._load_state()
        return self._store

    # -- command queue --------------------------------------------------------

    def emit(self, kind, **payload):
        """Queue a command for the device to pick up."""
        with self.lock:
            self.seq += 1
            command = dict(payload, type=kind, seq=self.seq, timestamp=time.time())
            # Without an assistant the device shows the idle ball, not the
            # character of whichever assistant happens to be the default.
            command.setdefault("agent", self.selected or "none")
            self.commands.append(command)
            # The state each assistant is in. The console shows the
            # matching face, so both ends agree on what is happening.
            state = payload.get("state")
            agent = command.get("agent") or "none"
            if kind in ("display", "state") and state:
                self.agent_states[agent] = str(state)
            if kind == "speak":
                text = payload.get("text") or ""
                if text:
                    self.recent_speeches.append({
                        "text": intent.normalize(text),
                        "at": time.time(),
                        "done": False,
                    })
        return command

    def pending(self, after):
        with self.lock:
            items = [c for c in self.commands if c["seq"] > after]
        return items

    def play_audio(self, url, title="", artist="", cover="", lyrics=None,
                   note=""):
        """Queue one clip for the speaker's own MediaPlayer.

        This is the single exit for the audio output channel, just as
        ``deliver_speech`` is the single exit for spoken replies. A future
        TTS server with the catgirl's own voice plugs in here, and music
        uses the same path today. Artwork and a timed lyric sheet travel
        with the command, so the speaker can draw its own now-playing view.
        """
        with self.lock:
            self.audio_state = "playing"
            self.audio_title = str(title or "")
        return self.emit("audio", url=str(url), title=self.audio_title,
                         artist=str(artist or ""), cover=str(cover or ""),
                         lyrics=list(lyrics or []), note=str(note or ""))

    def stop_audio(self):
        with self.lock:
            self.audio_state = "idle"
        return self.emit("stop_audio")

    # -- playback controls ----------------------------------------------------

    def apply_music_control(self, action, request_id=""):
        """Run one playback control on the speaker's own MediaPlayer.

        This is deliberately not an assistant turn: no model call, no
        spoken reply, no user entry in the conversation history. The
        factory used to swallow these words, so the host has to act on
        them itself. ``music_hook`` (set by the HTTP layer) supplies the
        next/previous track, since the library lives there.
        """
        valid = ("pause", "resume", "next", "prev", "stop")
        if action not in valid:
            return {"accepted": True, "intent": "music_control",
                    "action": action, "applied": False,
                    "reason": "unknown action"}

        outcome = None

        def result(applied, **extra):
            payload = {"accepted": True, "intent": "music_control",
                       "action": action, "applied": applied}
            with self.lock:
                payload["state"] = self.audio_state
            payload.update(extra)
            return payload

        if action == "pause":
            with self.lock:
                if self.audio_state not in ("playing", "loading"):
                    return result(False)
                self.audio_state = "paused"
            self.emit("pause_audio", request_id=request_id)
        elif action == "resume":
            with self.lock:
                if self.audio_state != "paused":
                    return result(False)
                self.audio_state = "playing"
            self.emit("resume_audio", request_id=request_id)
        elif action == "stop":
            self.stop_audio()
        else:
            hook = self.music_hook
            if hook is None:
                return result(False, reason="music library not ready")
            try:
                outcome = hook(action)
            except Exception as error:
                self.log_transcript("system", "music",
                                    "换歌失败：" + str(error), request_id)
                return result(False, reason=str(error))
            if not outcome or not outcome.get("ok"):
                reason = (outcome or {}).get("error", "换歌失败")
                self.log_transcript("system", "music", reason, request_id)
                return result(False, reason=reason)

        label = {"pause": "暂停播放", "resume": "继续播放",
                 "next": "下一首", "prev": "上一首",
                 "stop": "停止播放"}[action]
        self.log_transcript("system", "music", label, request_id)
        return result(True, title=(outcome or {}).get("title", "")
                      if action in ("next", "prev") else "")

    # -- spoken song requests -------------------------------------------------

    def apply_music_request(self, request, request_id=""):
        """Play a song named in speech: local library first, online second.

        The library lives in the HTTP layer, so ``music_request_hook`` does
        the lookup and queues the clip. Success is its own feedback (the
        song starts); failure gets one very short spoken hint, which the
        user explicitly asked for. Nothing here reaches an assistant or
        the conversation history.
        """
        hook = self.music_request_hook
        if hook is None:
            outcome = {"ok": False, "error": "音乐库没有准备好"}
        else:
            try:
                outcome = hook(request) or {}
            except Exception as error:
                outcome = {"ok": False, "error": str(error)}
        query = str(request.get("phrase") or "")
        if not outcome.get("ok"):
            reason = str(outcome.get("error") or "没找到这首歌")
            self.log_transcript("system", "music",
                                "点歌失败：" + reason, request_id)
            hint = _music_failure_hint(reason)
            self.emit("speak", agent="none", text=hint,
                      request_id=request_id)
            return {"accepted": True, "intent": "music_request",
                    "applied": False, "reason": reason, "query": query,
                    "hint": hint}
        with self.lock:
            state = self.audio_state
        self.log_transcript(
            "system", "music",
            "正在播放：" + str(outcome.get("title") or query), request_id)
        return {"accepted": True, "intent": "music_request",
                "applied": True, "title": str(outcome.get("title") or ""),
                "source": str(outcome.get("source") or ""),
                "query": query, "state": state}

    # -- volume ---------------------------------------------------------------

    def apply_music_volume(self, request, request_id=""):
        """Send one volume command to the device's STREAM_MUSIC.

        Like the other music intents this never reaches an assistant.
        The device reports the resulting level back with a volume_state
        event, which is the only source of truth for the console display.
        """
        action = str(request.get("action") or "")
        value = request.get("value")
        if action not in ("up", "down", "mute", "unmute", "set"):
            return {"accepted": True, "intent": "music_volume",
                    "applied": False, "reason": "unknown action"}
        self.emit("set_volume", action=action, value=value,
                  request_id=request_id)
        if action == "mute":
            with self.lock:
                self.music_muted = True
        elif action in ("unmute", "up", "down", "set"):
            with self.lock:
                self.music_muted = False
        label = {"up": "调大音量", "down": "调小音量",
                 "mute": "静音", "unmute": "取消静音",
                 "set": "音量调到 %s%%" % value}[action]
        self.log_transcript("system", "music", label, request_id)
        return {"accepted": True, "intent": "music_volume",
                "applied": True, "action": action, "value": value}

    # -- Roon -----------------------------------------------------------------

    def _roon_route(self):
        """Whether a spoken playback/volume command belongs to the Roon zone.

        The sidecar owns the decision: "local" always stays here, "roon"
        always goes to Roon, and "auto" only goes to Roon while our own
        MediaPlayer is idle and the Roon zone is actually playing.
        """
        client = self.roon_client
        if client is None:
            return False
        with self.lock:
            audio_state = self.audio_state
        try:
            return bool(client.routed_to_roon(audio_state))
        except Exception:
            return False

    def apply_roon_control(self, action, request_id=""):
        """Drive the Roon zone instead of the speaker's own MediaPlayer."""
        mapping = {"pause": "pause", "resume": "resume", "next": "next",
                   "prev": "previous", "stop": "stop"}
        if action not in mapping:
            return {"accepted": True, "intent": "roon_control",
                    "action": action, "applied": False,
                    "reason": "unknown action"}
        try:
            outcome = self.roon_client.control(mapping[action]) or {}
        except Exception as error:
            outcome = {"ok": False, "error": str(error)}
        label = {"pause": "暂停播放", "resume": "继续播放",
                 "next": "下一首", "prev": "上一首",
                 "stop": "停止播放"}[action]
        if outcome.get("ok"):
            self.log_transcript("system", "roon", "Roon " + label, request_id)
        else:
            self.log_transcript("system", "roon", "Roon " + label + "失败："
                                + str(outcome.get("error") or ""), request_id)
        return {"accepted": True, "intent": "roon_control",
                "action": action, "applied": bool(outcome.get("ok")),
                "target": "roon", "reason": str(outcome.get("error") or "")}

    def apply_roon_volume(self, request, request_id=""):
        """Send a volume word to the Roon zone's own output."""
        mapping = {"up": "volume_up", "down": "volume_down",
                   "mute": "mute", "unmute": "unmute", "set": "volume_set"}
        action = str(request.get("action") or "")
        if action not in mapping:
            return {"accepted": True, "intent": "roon_volume",
                    "applied": False, "reason": "unknown action"}
        try:
            outcome = self.roon_client.control(
                mapping[action], value=request.get("value")) or {}
        except Exception as error:
            outcome = {"ok": False, "error": str(error)}
        label = {"up": "调大音量", "down": "调小音量", "mute": "静音",
                 "unmute": "取消静音",
                 "set": "音量调到 %s%%" % request.get("value")}[action]
        self.log_transcript("system", "roon", "Roon " + label, request_id)
        return {"accepted": True, "intent": "roon_volume",
                "applied": bool(outcome.get("ok")), "action": action,
                "target": "roon", "reason": str(outcome.get("error") or "")}

    # -- echo suppression -----------------------------------------------------

    def mark_spoken_done(self):
        """Close the oldest open broadcast window.

        The device reports when a broadcast finished. Until then the
        speaker is still talking and the microphone is still hearing it, so
        anything that arrives now is an echo.
        """
        with self.lock:
            for entry in self.recent_speeches:
                # Mac windows close when the Mac's process exits, never on a
                # report from the speaker.
                if entry.get("mac") or entry["done"]:
                    continue
                entry["done"] = True
                entry["ended"] = time.time()
                return True
        return False

    def is_echo(self, text, now=None):
        """True when the sentence is the speaker hearing our own output.

        The device plays our broadcast through the same speaker the
        microphone listens to, so the recogniser hands the broadcast back
        to us a moment later. Answering it repeats the broadcast forever,
        so an utterance that overlaps a recent broadcast is dropped.

        While the broadcast is still playing, anything the microphone picks
        up is our own voice, so a broad match counts as an echo. Once the
        device reported the broadcast finished, only a near-identical
        sentence counts: the user may well repeat the assistant's name
        right after hearing it, and that must not be swallowed.
        """
        now = now if now is not None else time.time()
        heard = intent.normalize(intent.strip_wake_word(text))
        if not heard:
            return False
        # Compare on the unfolded spelling too: the recogniser may hand
        # back a homophone of what we said ("小赫" heard as "小鹤").
        folded = intent.fold(heard)
        with self.lock:
            spoken = [dict(entry) for entry in self.recent_speeches]
        for entry in spoken:
            phrase = entry["text"]
            # Short lines are normally skipped: two or three characters match
            # too easily and would swallow the user's own words. A line the
            # Mac just said is different -- the microphone is literally
            # hearing it, and a two-word answer ("嗯。") coming back as a user
            # turn would start a loop, because the reply to it is spoken too.
            if len(phrase) < ECHO_MIN_SPOKEN and not entry.get("mac"):
                continue
            ended = entry.get("ended")
            playing = ended is None
            # A Mac line sizes its own window from the text length; the
            # device's broadcast keeps the fixed cap.
            cap = float(entry.get("cap") or ECHO_MAX_PLAYBACK)
            if playing:
                # Still speaking, or the device never reported the end.
                # The device normally reports the end; if that report is
                # lost, the window must not stay wide open, or the user's
                # own words would be swallowed for as long as it lasts.
                if now - entry["at"] > max(ECHO_OPEN_TIMEOUT, cap):
                    continue
                if now - entry["at"] > cap:
                    playing = False
            elif now - ended > ECHO_TAIL:
                continue

            # The recogniser may return only part of the broadcast, or the
            # broadcast may be phrased differently than it was heard. The
            # comparison runs on the folded spelling, because the echo
            # often comes back as a homophone ("小赫" heard as "小鹤").
            folded_phrase = intent.fold(phrase)
            if heard == phrase or folded == folded_phrase:
                return True
            if not playing:
                # The broadcast is over: this is the user talking again.
                # Only a sentence that is nearly the same counts.
                if difflib.SequenceMatcher(None, folded, folded_phrase).ratio() >= ECHO_SIMILARITY:
                    return True
                continue
            if folded in folded_phrase or folded_phrase in folded:
                return True
            if heard in phrase or phrase in heard:
                return True
            if difflib.SequenceMatcher(None, heard, phrase).ratio() >= ECHO_SIMILARITY:
                return True
        return False

    def wake_during_speech(self, now=None):
        """True when the speaker is still playing (or has just finished) our TTS.

        The microphone sits next to the speaker, so the wake engine hears
        our own broadcast. A reply that mentions an assistant's name -- the
        greeting "GPT在，请说你的任务。" is the worst case, because "GPT"
        is registered as one of its wake aliases -- makes the engine print
        a fresh ``vt word`` line and the watcher reports it as a new wake.
        Routing that hit switches the assistant again while it is talking,
        and the device's ``VoiceOptions`` status lines repeat the same label
        throughout the playback, so the watcher's short dedup window cannot
        catch them all.

        A wake hit inside this window is our own voice, not the user. The
        device normally reports ``playback_done``, after which the short
        ECHO_TAIL still covers the recogniser finishing the audio. If that
        report is lost, ECHO_MAX_PLAYBACK caps the window so a real wake
        word is not blocked for as long as the broadcast lasts.
        """
        now = now if now is not None else time.time()
        with self.lock:
            spoken = [dict(entry) for entry in self.recent_speeches]
        for entry in spoken:
            ended = entry.get("ended")
            if ended is None:
                cap = float(entry.get("cap") or ECHO_MAX_PLAYBACK)
                if now - entry["at"] <= cap:
                    return True
            elif now - ended <= ECHO_TAIL:
                return True
        return False

    def mac_speaking(self, now=None):
        """True while a line is being read aloud on the Mac.

        The factory wake engine on the speaker hears the Mac's speakers, so
        a plain ``wake`` (no word yet) during a Mac line is that voice, not
        the user. Only Mac windows are considered: the device's own
        broadcast is already filtered upstream by the watcher.
        """
        now = now if now is not None else time.time()
        with self.lock:
            spoken = [dict(entry) for entry in self.recent_speeches]
        for entry in spoken:
            if not entry.get("mac"):
                continue
            ended = entry.get("ended")
            if ended is None:
                cap = float(entry.get("cap") or ECHO_MAX_PLAYBACK)
                if now - entry["at"] <= cap:
                    return True
            elif now - ended <= ECHO_TAIL:
                return True
        return False

    # -- transcript -----------------------------------------------------------

    def _import_legacy_transcript(self):
        """Bring the old single-file conversation over, once.

        The first version of the history kept every assistant in one
        capped file. Split those lines into one file per assistant, then
        leave the old file behind under a ``.migrated`` name so it can
        be recovered by hand if anything looks wrong.
        """
        if not self.state_file:
            return
        from pathlib import Path
        path = Path(self.state_file).with_name("bridge-transcript.json")
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else None
        except (OSError, ValueError):
            entries = None
        if isinstance(entries, list):
            self.history.import_legacy(entries, self.thread_key)
        try:
            path.rename(path.with_name(path.name + ".migrated"))
        except OSError:
            pass

    def log_transcript(self, role, agent, message, request_id=""):
        entry = {
            "at": time.time(),
            "role": role,
            "agent": agent or "",
            "message": message,
            "request_id": request_id,
        }
        # A number that only grows, so the console can ask for everything
        # after the last line it drew instead of the whole history again.
        #
        # The number, the line in memory and the line on disk are taken
        # together under the lock: several threads write here at once,
        # and a file written out of order would show the answers back to
        # front. Appending one line to an open file is cheap enough that
        # it does not need a background writer -- and doing it here means
        # the words are on disk before they are spoken.
        with self.lock:
            self.transcript_seq += 1
            entry["seq"] = self.transcript_seq
            self.transcript.append(entry)
            self.history.append(self.thread_key(agent), entry)
        return entry

    def transcript_since(self, after=0):
        """The transcript entries after ``after``.

        The console polls this and appends what is new. The memory window
        holds the newest 300 lines; a tab that slept through more than
        that would miss the ones that fell out, so those gaps are read
        back from the files instead.
        """
        with self.lock:
            items = [item for item in self.transcript
                     if int(item.get("seq") or 0) > after]
            last = self.transcript_seq
            oldest = (int(self.transcript[0].get("seq") or last + 1)
                      if self.transcript else last + 1)
        if after < oldest - 1 and self.history.counts():
            items = [item for item in self.history.since(after)
                     if int(item.get("seq") or 0) <= last]
        return {"items": items, "last_seq": last,
                "instance_id": self.instance_id}

    def threads(self, limit=50):
        """The recent conversation of each assistant, straight from disk.

        Each assistant has its own file, so its history is measured on
        its own: a busy one cannot push a quiet one's earlier words out.
        The console gets the newest ``limit`` lines of each, plus how
        many lines there are in total, so it can offer to load more.
        """
        with self.lock:
            last = self.transcript_seq
            in_memory = list(self.transcript)
        counts = self.history.counts()
        if not counts and self.history.dir is None:
            # No state file (some tests, and a host started without one):
            # there is nowhere to keep the files. Fall back to the memory
            # window so the console still gets a conversation per assistant.
            buckets = {}
            for item in in_memory:
                key = self.thread_key(item.get("agent"))
                buckets.setdefault(key, []).append(item)
            return {"threads": {key: rows[-limit:] for key, rows in buckets.items()},
                    "counts": {key: len(rows) for key, rows in buckets.items()},
                    "speech_counts": {
                        key: sum(1 for row in rows
                                 if row.get("role") == "agent")
                        for key, rows in buckets.items()},
                    "has_more": {},
                    "last_seq": last,
                    "instance_id": self.instance_id}
        buckets = {}
        has_more = {}
        for key in counts:
            rows = self.history.recent(key, limit)
            if rows:
                buckets[key] = rows
                has_more[key] = self.history.has_more(
                    key, int(rows[0].get("seq") or 0))
        return {"threads": buckets,
                "counts": counts,
                "speech_counts": self.history.speech_counts(),
                "has_more": has_more,
                "last_seq": last,
                "instance_id": self.instance_id}

    def history_before(self, agent, seq, limit=50):
        """One page of an assistant's earlier conversation.

        The console puts this above what it already drew when the user
        asks to see older messages.
        """
        key = self.thread_key(agent)
        rows = self.history.before(key, seq, limit)
        return {"items": rows,
                "agent": key,
                "total": self.history.counts().get(key, 0),
                "has_more": self.history.has_more(
                    key, int(rows[0].get("seq") or 0)) if rows else False}

    @staticmethod
    def thread_key(agent):
        """Which conversation an entry belongs to.

        The direct path is the speaker itself, so it keeps the "direct"
        bucket. The device-level entries use "none" and belong there too.
        """
        name = str(agent or "").strip()
        if not name or name == "none":
            return "direct"
        return name

    # -- direct mode ----------------------------------------------------------

    def _assistant_settings(self, agent_id):
        """One assistant's settings, including a shared connection section.

        ``REGISTRY.config_section`` lets an assistant reuse another section
        (小家 -> homeassistant), so the HA address/token live in exactly one
        place. Both ``backend_for`` and ``direct_settings`` need this: 若琪
        in direct mode reuses an assistant's backend too.
        """
        settings = agent_settings(self.config, agent_id)
        shared = (REGISTRY.get(agent_id) or {}).get("config_section")
        if shared:
            merged = dict(self.config.get(shared) or {})
            merged.update(settings)
            settings = merged
        return settings

    def direct_settings(self, for_backend=False):
        """The direct-mode settings, together with the chosen assistant's.

        The assistant is only consulted for its command and working
        directory; the conversation it holds is not used, so the direct
        conversation stays apart from the assistant's own.

        ``for_backend=True`` also folds in a shared connection section
        (小家 -> homeassistant). The default is deliberately without it:
        ``set_direct_settings`` returns this dict to the console, and the
        HA long-lived token must never appear in an API response.
        """
        settings = dict(self.config.get("direct") or {})
        assistant = str(settings.get("assistant") or "hermes")
        settings["assistant"] = assistant
        if for_backend:
            settings["assistant_settings"] = self._assistant_settings(assistant)
        else:
            settings["assistant_settings"] = agent_settings(self.config,
                                                            assistant)
        return settings

    def direct_mode(self):
        """The direct mode: off, hybrid or full."""
        return normalise_mode((self.config.get("direct") or {}).get("mode"))

    def set_direct_mode(self, mode):
        """Change the direct mode and report the new one.

        The change is written back to the config file, so the mode
        survives a restart of the host.
        """
        value = normalise_mode(mode)
        with self.lock:
            self.config.setdefault("direct", {})["mode"] = value
        self.log_transcript("system", "none", "直连模式：" + self._mode_text(value))
        self._save_config()
        return value

    def set_direct_settings(self, settings=None):
        """Save the whole direct configuration in one go.

        The console's settings panel changes the mode, the source and the
        custom endpoint together. ``settings`` may carry any of:

        ``mode``       off / hybrid / full
        ``source``     assistant (reuse an assistant) or custom (an
                       OpenAI-compatible endpoint)
        ``assistant``  which assistant the assistant source reuses
        ``custom``     base_url / api_key / model / timeout_seconds

        The api key is only written, never echoed back: the console shows
        that one is stored, not what it is.
        """
        settings = settings or {}
        with self.lock:
            direct = self.config.setdefault("direct", {})
            if "mode" in settings and settings["mode"]:
                direct["mode"] = normalise_mode(settings["mode"])
            if "source" in settings and settings["source"]:
                source = str(settings["source"]).strip().lower()
                if source in ("assistant", "custom"):
                    direct["source"] = source
            if "assistant" in settings and settings["assistant"]:
                assistant = str(settings["assistant"]).strip()
                if assistant in REGISTRY and REGISTRY[assistant].get("enabled"):
                    direct["assistant"] = assistant
            custom = settings.get("custom")
            if isinstance(custom, dict):
                section = direct.setdefault("custom", {})
                if "base_url" in custom:
                    section["base_url"] = str(custom["base_url"] or "").strip()
                if "model" in custom:
                    section["model"] = str(custom["model"] or "").strip()
                if "timeout_seconds" in custom:
                    try:
                        section["timeout_seconds"] = int(custom["timeout_seconds"])
                    except (TypeError, ValueError):
                        pass
                # An empty key means "keep the one that is stored", so the
                # console can show a placeholder instead of the secret.
                if "api_key" in custom and str(custom["api_key"] or "").strip():
                    section["api_key"] = str(custom["api_key"]).strip()
        # The direct backend reads its settings once, when it is built, so
        # a new endpoint needs a new backend.
        self._drop_backend("direct")
        self._save_config()
        self.log_transcript("system", "none", "直连设置已更新")
        return self.direct_settings()

    def direct_status(self):
        """The direct configuration, with the api key reduced to a flag."""
        direct = self.config.get("direct") or {}
        custom = direct.get("custom") or {}
        return {
            "mode": self.direct_mode(),
            "mode_label": self._mode_text(self.direct_mode()),
            "source": str(direct.get("source") or "assistant"),
            "assistant": str(direct.get("assistant") or "hermes"),
            "custom": {
                "base_url": str(custom.get("base_url") or ""),
                "model": str(custom.get("model") or ""),
                "timeout_seconds": int(custom.get("timeout_seconds") or 60),
                # The key itself never leaves the host.
                "api_key_set": bool(str(custom.get("api_key") or "").strip()),
            },
        }

    def _save_config(self, *, strict=False):
        """Write the configuration back to its file."""
        path = self.config.get("_path")
        if not path:
            return
        try:
            import config as config_module
            data = {key: value for key, value in self.config.items()
                    if not key.startswith("_")}
            config_module.atomic_json(path, data)
        except Exception:
            if strict:
                raise
            # A config that cannot be written is not a reason to stop
            # answering; the mode still works for this session.
            pass

    @staticmethod
    def _mode_text(mode):
        if mode == FULL:
            return "全部接管"
        if mode == HYBRID:
            return "混合"
        return "关闭"

    def factory_pattern(self):
        """The rule that says which turns the factory handles on its own.

        The stored rule keeps no weather words (D047); while the
        weather override switch is OFF the factory's old weather words
        rejoin it here at runtime, so the speaker answers weather turns
        exactly the way it did before the rebuilt skill. With the switch
        ON the stored rule is returned as-is and weather.py takes the
        turns instead. The config file itself is never rewritten.
        """
        base = str((self.config.get("direct") or {}).get("factory_owns") or "")
        if self.weather.enabled:
            return base
        if not base:
            return FACTORY_WEATHER_WORDS
        return "(?:%s|%s)" % (base, FACTORY_WEATHER_WORDS)

    def factory_notice_settings(self):
        """Whether the factory's canned expiry notices are ignored.

        Only that one family of notices is dropped from the ASR stream.
        Every other factory turn still reaches the assistants, and the
        factory itself still plays whatever its cloud service dictates.
        """
        return {"enabled": bool(self.config.get("suppress_factory_notice", True))}

    def set_factory_notice(self, enabled=None):
        """Flip the expiry-notice switch and persist it to the config file."""
        if enabled is not None:
            self.config["suppress_factory_notice"] = bool(enabled)
            self._save_config()
        return self.factory_notice_settings()

    def _suppressed_notice(self, text):
        """True when the factory's canned expiry notice must be dropped.

        The clip used to be filtered only on the voice path. It also arrives
        as a typed console line or an injected event, and each of those left
        a stray line in the chat window with a stray answer following it.
        The switch remains the master control for every input channel.
        """
        return (self.factory_notice_settings()["enabled"]
                and intent.factory_notice(text))

    # -- wake words ------------------------------------------------------------

    @staticmethod
    def _valid_pinyin(value):
        """A wake word in the format the engine takes.

        Tone-marked pinyin, one syllable per digit: da4fei2yu2. The check
        is deliberately loose (it cannot tell a real syllable from a
        made-up one) but strict about shape: 1 to 4 syllables of lowercase
        letters, each ending in a tone digit 1-5. Anything else would
        reach the engine as noise and silently never wake.
        """
        return bool(re.fullmatch(r"(?:[a-z]+[1-5]){1,4}", value or ""))

    def wake_settings(self):
        """The wake-word settings, as the console shows them.

        Registry agents that are enabled are listed, plus any wake entry
        added by hand -- a custom assistant (not in REGISTRY) or a
        disabled registry agent the user re-added. A disabled registry
        agent with no wake row is still hidden, as before.
        """
        wake = self.config.get("wake") or {}
        agents = {}
        rows = wake.get("agents") or {}
        for agent_id in sorted(set(REGISTRY) | set(rows)):
            item = REGISTRY.get(agent_id)
            row = rows.get(agent_id) or {}
            if item is None:
                # Custom agent: only shown once it carries a name.
                if not row.get("name"):
                    continue
            elif not item.get("enabled") and not row:
                # Disabled registry agent with no wake row: hidden.
                continue
            name = (item or {}).get("name") or row.get("name") or agent_id
            aliases = []
            for spelling in (row.get("aliases") or []):
                value = str(spelling).strip()
                if value and value not in aliases:
                    aliases.append(value)
            # The display name is always the first vt_word entry. Once the
            # registry name equals one of the aliases (Hermes -> 小赫,
            # 小家 itself), drop the duplicate so the registered string is
            # not "小赫|小赫|...". The cloud only needs each spelling once.
            spellings = [name] + [value for value in aliases if value != name]
            agents[agent_id] = {
                "enabled": bool(row.get("enabled", True)),
                "pinyin": str(row.get("pinyin") or ""),
                # The Chinese name is what the wake engine registers as
                # vt_word, and what the recogniser is likely to write at
                # the front of a sentence. The host uses it to route.
                "name": name,
                # One pipe-separated vt_word, name first. The cloud
                # accepts a wake when its transcription contains any of
                # the aliases, so a misheard name is no longer cut off
                # as a false trigger. The device registers this string
                # as-is; the pinyin is what the acoustic match uses.
                "vt_word": "|".join(spellings),
                "aliases": aliases,
                # True when the agent is not in the built-in registry and
                # was added by hand. The console offers a remove control.
                "custom": item is None,
            }
        return {"enabled": bool(wake.get("enabled")), "agents": agents}

    def detect_local_agents(self):
        """Probe this machine for the built-in assistant backends.

        Open-source users may only have a subset of the assistants
        installed, so the wake-word settings page asks the host which
        ones are actually present before offering to add them. Detection
        is advisory: a backend not found here can still be added by hand
        if the user knows it exists.
        """
        results = {}

        # CLI tools on PATH, or the self-contained launcher the user may
        # have placed under ~/.local/bin.
        cli = {
            "hermes": "hermes",
            "codex": "codex",
            "dsh": "dsh",
            "claude": "claude",
        }
        cli_paths = {}
        for agent_id, command in cli.items():
            path = shutil.which(command)
            if not path:
                local = os.path.expanduser("~/.local/bin/" + command)
                if os.path.exists(local):
                    path = local
            if path:
                cli_paths[agent_id] = path
                results[agent_id] = {"available": True,
                                     "detail": "已安装：" + path}
            else:
                results[agent_id] = {"available": False,
                                     "detail": "未检测到 " + command + " 命令"}

        # Claude Code needs its own login, separate from the Claude Desktop
        # app. `claude auth status` is read-only and quick, so the settings
        # page can say "已安装但未登录" instead of leaving the speaker to
        # discover it on the first turn. A slow/broken probe never fails
        # the detection: the CLI itself is still installed.
        claude_path = cli_paths.get("claude")
        if claude_path:
            import subprocess
            try:
                probe = subprocess.run(
                    [claude_path, "auth", "status"],
                    capture_output=True, text=True, timeout=2.0)
                status = json.loads(probe.stdout or "{}")
                if not status.get("loggedIn"):
                    results["claude"]["detail"] += (
                        " · 未登录（终端运行 claude /login 登录）")
                else:
                    results["claude"]["detail"] += " · 已登录"
            except Exception:
                pass

        # WorkBuddy: signed in when the auth file holds a token.
        auth_path = self.auth_file_path("workbuddy")
        if os.path.exists(auth_path):
            try:
                auth = json.loads(open(auth_path, encoding="utf-8").read())
                if auth.get("token") or auth.get("access_token"):
                    results["workbuddy"] = {
                        "available": True,
                        "detail": "已登录 WorkBuddy 账号"}
                else:
                    results["workbuddy"] = {
                        "available": False, "detail": "WorkBuddy 未登录"}
            except Exception:
                results["workbuddy"] = {
                    "available": False, "detail": "WorkBuddy 凭证文件损坏"}
        else:
            results["workbuddy"] = {
                "available": False, "detail": "WorkBuddy 未登录"}

        # N.E.K.O.: the desktop runtime listens on a local websocket; a
        # TCP connect to the port is enough to know it is running.
        neko_url = (self.config.get("neko") or {}).get("url") \
            or "ws://127.0.0.1:48911"
        host, port = "127.0.0.1", 48911
        try:
            parsed = urllib.parse.urlparse(neko_url)
            if parsed.hostname:
                host = parsed.hostname
            if parsed.port:
                port = parsed.port
        except Exception:
            pass
        try:
            with socket.create_connection((host, port), timeout=0.6):
                results["neko"] = {
                    "available": True, "detail": "N.E.K.O. 运行时在线"}
        except OSError:
            results["neko"] = {
                "available": False, "detail": "未检测到 N.E.K.O. 运行时"}

        # 小家 has no local CLI: it talks to HA on the LAN. Probe the
        # configured host/port with a read-only TCP connect (same idea
        # as the neko check), and show the address in the detail so the
        # wake page can tell a wrong IP from a sleeping HA instance.
        ha = self.config.get("homeassistant") or {}
        base = str(ha.get("base_url") or "").strip()
        ha_host, ha_port = "", 0
        if base:
            try:
                parsed = urllib.parse.urlparse(
                    base if "://" in base else "http://" + base)
                ha_host = parsed.hostname or ""
                ha_port = parsed.port or (
                    443 if parsed.scheme == "https" else 8123)
            except Exception:
                pass
        where = (ha_host + ":" + str(ha_port)) if ha_host else "未填写地址"
        if not (ha.get("enabled") and base and ha.get("token")):
            results["xiaojia"] = {
                "available": False,
                "detail": "局域网 HA（" + where + "）：还没填好地址或令牌"}
        else:
            reachable = False
            try:
                with socket.create_connection((ha_host, ha_port),
                                              timeout=0.6):
                    reachable = True
            except OSError:
                pass
            results["xiaojia"] = {
                "available": reachable,
                "detail": ("局域网 HA " + where + " 在线" if reachable
                           else "局域网 HA " + where
                           + " 连不上，检查 IP 与端口")}

        # A suggested wake spelling for each known agent, so a one-click
        # add registers a useful word without the user typing pinyin.
        suggestions = {
            "dsh": "da4fei2yu2",
            "workbuddy": "ba1di4",
            "codex": "ji4pi4ti4",
            "hermes": "xiao3he4",
            "neko": "mao1niang2",
            "xiaojia": "xiao3jia1",
            "claude": "ke4lao2de3",
        }
        for agent_id, info in results.items():
            info["name"] = (REGISTRY.get(agent_id) or {}).get("name") \
                or agent_id
            info["suggested"] = suggestions.get(agent_id, "")
        return results

    def wake_spellings(self):
        """Chinese spellings of the wake words, for routing a spoken turn.

        The engine registers a pipe-separated alias table as vt_word,
        so the label it reports may be the whole table or any single
        alias. Every spelling is accepted here, plus the registry's own
        aliases for turns that arrive as text. An empty map means the
        feature is off and no turn is routed at all.
        """
        wake = self.wake_settings()
        if not wake["enabled"]:
            return {}
        spellings = {}
        for agent_id, row in wake["agents"].items():
            if not row["enabled"] or not row["pinyin"]:
                continue
            words = [row["name"]]
            words.extend(row.get("aliases") or [])
            words.extend(REGISTRY.get(agent_id, {}).get("aliases", []))
            seen, unique = set(), []
            for word in words:
                if word and word not in seen:
                    seen.add(word)
                    unique.append(word)
            spellings[agent_id] = unique
        return spellings

    def set_wake(self, enabled=None, agent=None, agent_enabled=None, pinyin=None,
                 name=None, aliases=None, add=False, remove=False,
                 aliases_map=None):
        """Change the wake-word settings and save them.

        The master switch and one assistant's row are changed separately:
        the console flips the switch the moment it is clicked, and a
        typed word only becomes active when its own save button is
        pressed. A pinyin that does not match the format is refused
        rather than stored -- a word that cannot wake is worse than no
        word.

        ``add`` registers a new wake entry: a known assistant the user
        wants to enable, or a custom assistant added by hand (which must
        carry its own ``name``). ``remove`` drops an entry entirely --
        used for the custom agents the user no longer wants.
        """
        wake = dict(self.config.get("wake") or {})
        rows = dict(wake.get("agents") or {})
        if aliases_map is not None:
            # The console edits every assistant's aliases in one card and
            # saves them together. Validate the whole batch before writing
            # anything, so a bad row cannot leave a half-updated table.
            if not isinstance(aliases_map, dict):
                raise ValueError("别名表格式不对")
            cleaned = {}
            for key, value in aliases_map.items():
                agent_id = str(key).strip()
                row = rows.get(agent_id)
                if row is None:
                    raise ValueError("未知的助手：" + agent_id)
                if isinstance(value, str):
                    words = [part.strip() for part in value.split("|")
                             if part.strip()]
                elif isinstance(value, (list, tuple)):
                    words = [str(part).strip() for part in value
                             if str(part).strip()]
                else:
                    raise ValueError("别名要用 | 分隔的文字")
                # The display name is always the first part of vt_word;
                # keeping it out of the list avoids registering it twice.
                head = str(row.get("name")
                           or REGISTRY.get(agent_id, {}).get("name")
                           or "").strip()
                seen, unique = set(), []
                for word in words:
                    if word == head or "|" in word or word in seen:
                        continue
                    seen.add(word)
                    unique.append(word)
                cleaned[agent_id] = unique
            for agent_id, words in cleaned.items():
                row = dict(rows[agent_id])
                row["aliases"] = words
                rows[agent_id] = row
            wake["agents"] = rows
        if enabled is not None:
            wake["enabled"] = bool(enabled)
        if agent is not None:
            agent = str(agent).strip()
            if not agent:
                raise ValueError("助手标识不能为空")
            item = REGISTRY.get(agent)
            if remove:
                rows.pop(agent, None)
            else:
                if item is None and not add:
                    raise ValueError("未知的助手：" + agent)
                if item is None and not name:
                    raise ValueError("添加自定义助手需要填写名称")
                row = dict(rows.get(agent) or {})
                if name is not None:
                    name = str(name).strip()
                    if not name:
                        raise ValueError("助手名称不能为空")
                    row["name"] = name
                if aliases is not None:
                    if isinstance(aliases, str):
                        aliases = [a.strip()
                                   for a in aliases.split("|") if a.strip()]
                    row["aliases"] = [str(a) for a in aliases]
                if agent_enabled is not None:
                    row["enabled"] = bool(agent_enabled)
                if pinyin is not None:
                    pinyin = str(pinyin).strip().lower()
                    # The manual-add form may register an assistant with no
                    # wake word yet (blank pinyin). An existing row must not
                    # lose its word to a blank save, so only ``add`` accepts
                    # the empty value; every non-empty word is still checked.
                    if (pinyin or not add) and not self._valid_pinyin(pinyin):
                        raise ValueError(
                            "唤醒词要写成带声调的拼音，比如 da4fei2yu2（1–4 个音节，"
                            "每个音节以 1-5 的声调数字结尾）")
                    row["pinyin"] = pinyin
                rows[agent] = row
            wake["agents"] = rows
        self.config["wake"] = wake
        self._save_config()
        # The device reads the settings with its next poll; this command
        # also wakes a sleeping device up so the new words are spoken to
        # the engine right away.
        self.emit("wake", **self.wake_settings())
        return self.wake_settings()

    def _direct_decision(self, event, text=""):
        """Who answers this turn: the model, the factory, or nobody.

        The rule that decides is the one in the configuration, and the
        device gets the same rule with the status. Both sides therefore
        agree on who answers -- which is what keeps two replies from
        happening.

        The host judges the sentence itself rather than trusting the
        field the device sends: the sentence also arrives over the logcat
        watcher, and that path carries no such field.
        """
        mode = self.direct_mode()
        factory_owns = intent.factory_owns(text, self.factory_pattern())
        if mode == FULL:
            return "answer"
        if mode == HYBRID:
            # The factory keeps its skills; everything else is the model's.
            return "silent" if factory_owns else "answer"
        # Off: the factory keeps its skills and the rest asks for an
        # assistant, as before.
        return "silent" if factory_owns else "assistant"

    # -- backends -------------------------------------------------------------

    def backend_for(self, agent_id):
        """Return the backend for an assistant, creating it on first use."""
        if agent_id in self.backends:
            return self.backends[agent_id]
        if agent_id == "direct":
            backend = DirectBackend(self.direct_settings(for_backend=True),
                                    self.store)
            self.backends[agent_id] = backend
            return backend
        item = REGISTRY.get(agent_id)
        if not item:
            return None
        settings = self._assistant_settings(agent_id)
        backend = load_backend(item["backend"], settings, self.store)
        self.backends[agent_id] = backend
        return backend

    def refresh_ha_backends(self):
        """HA address / token / agent changed from the console.

        Backends cache their settings at construction time, so an IP
        edit would otherwise keep talking to the old host until the
        next host restart. Drop the 小家 backend and the direct-mode
        wrapper (which may reuse 小家) so the next turn rebuilds both
        from the current config. No conversation state is on disk.
        """
        with self.lock:
            self.backends.pop("xiaojia", None)
            self.backends.pop("direct", None)

    # -- models ---------------------------------------------------------------

    def agent_models(self, agent_id):
        """The models an assistant can answer with, and the one it uses.

        The list comes from the assistant's own CLI, so it never offers a
        model the current account cannot reach.
        """
        if agent_id == "direct":
            # Direct mode answers with a model of its own: either the one
            # the reused assistant uses, or the custom endpoint's.
            direct = self.config.get("direct") or {}
            source = str(direct.get("source") or "assistant")
            if source == "custom":
                custom = direct.get("custom") or {}
                return str(custom.get("model") or ""), []
            assistant = str(direct.get("assistant") or "hermes")
            return self.agent_models(assistant)
        backend = self.backend_for(agent_id)
        if backend is None:
            return "", []
        try:
            names = list(backend.models())
        except Exception:
            names = []
        try:
            current = backend.current_model()
        except Exception:
            current = ""
        if not current:
            current = str(agent_settings(self.config, agent_id).get("model") or "")
        return current, names

    def agent_in_use_model(self, agent_id):
        """The model a conversation really ran on, when it is known.

        Hermes falls back to another model when the endpoint does not
        know the requested one, and the fallback shows nowhere else: the
        answer would come from a model the user did not pick. This is
        the model the last turn actually used, so the console can say so.
        """
        backend = self.backends.get(agent_id)
        # Direct mode answers with the backend of the assistant it
        # reuses, but through a backend of its own: the conversation and
        # the session are separate, and so is the model it ran on.
        if agent_id == "direct":
            direct = self.config.get("direct") or {}
            if str(direct.get("source") or "assistant") != "assistant":
                return ""
            backend = self.backends.get("direct")
            if backend is None:
                return ""
            getter = getattr(backend, "inner", None)
            if callable(getter):
                try:
                    backend = getter()
                except Exception:
                    return ""
            return str(getattr(backend, "used_model", "") or "")
        if backend is None:
            return ""
        return str(getattr(backend, "used_model", "") or "")

    def set_agent_model(self, agent_id, name):
        """Change the model an assistant answers with.

        The choice is written to the configuration file, so it survives a
        restart of the host, and it is applied to the live backend at once.
        """
        if agent_id != "direct" and agent_id not in REGISTRY:
            raise ValueError("未知的助手")
        model = str(name or "").strip()
        if agent_id == "direct":
            direct = self.config.get("direct") or {}
            source = str(direct.get("source") or "assistant")
            if source == "custom":
                with self.lock:
                    custom = self.config.setdefault("direct", {}).setdefault("custom", {})
                    custom["model"] = model
                self._save_config()
                # The direct backend reads its settings once, when it is
                # built. A new endpoint therefore needs a new backend.
                self._drop_backend("direct")
                self.log_transcript("system", "none",
                                    "直连模型：" + (model or "跟随来源"))
                return model
            # Direct mode reuses an assistant, so it answers with that
            # assistant's model. Changing it here changes it there.
            assistant = str(direct.get("assistant") or "hermes")
            self.set_agent_model(assistant, model)
            return model

        # A connected assistant (WorkBuddy and the like) keeps its model in
        # the authorisation file, not in the shared config.
        if REGISTRY.get(agent_id, {}).get("connection"):
            self.set_agent_auth(agent_id, {"model": model})
            return model

        with self.lock:
            override = self.config.setdefault("agents", {}).setdefault(agent_id, {})
            override["model"] = model
        backend = self.backend_for(agent_id)
        if backend is not None:
            try:
                backend.switch_model(model)
            except Exception:
                pass
        # The assistant's own session lives in the state file, and it
        # belongs to the model it was started with: it is dropped here so
        # the new model takes effect. The backend cannot do it on its own
        # when it does not exist yet -- after a restart it is built on the
        # first question.
        self._drop_session(agent_id)
        # Direct mode reuses this assistant but keeps a conversation of
        # its own, with its own backend and its own session. The change
        # does not reach it on its own, so it is told separately;
        # otherwise the direct thread would keep the old model.
        self._tell_direct_about(agent_id, model)
        self._save_config()
        self.log_transcript("system", agent_id,
                            "模型：" + (model or "跟随默认"))
        return model

    def _drop_session(self, name):
        """Forget a stored session, with the model and provider it ran on.

        The session and the model it runs on are recorded together, and
        the provider with them: a session belongs to the endpoint it was
        started on. A change of model or provider therefore means a new
        conversation.

        ``name`` is the key the session is stored under, without its
        ``_session`` ending: "hermes" for the assistant's own
        conversation, "direct" for the direct one.
        """
        key = str(name or "").strip()
        if not key:
            return
        changed = False
        for suffix in ("_session", "_session_model", "_session_provider"):
            full = key + suffix
            if full in self.store:
                self.store.pop(full, None)
                changed = True
        if changed:
            self._save_state()

    def _tell_direct_about(self, agent_id, model):
        """Pass a model change on to direct mode, when it reuses that assistant.

        Direct mode answers with the model of the assistant it reuses,
        but it holds a conversation of its own: its own backend, its own
        session. A change to the assistant therefore does not reach it
        on its own, and without this the direct thread would keep
        answering with the model that was replaced.
        """
        direct = self.config.get("direct") or {}
        if str(direct.get("source") or "assistant") != "assistant":
            return
        assistant = str(direct.get("assistant") or "hermes")
        if assistant != agent_id:
            return
        # The stored session runs on the model that was replaced, and a
        # resumed session keeps the model it was started with. It is
        # dropped here rather than in the backend: the backend may not
        # exist yet -- after a restart it is built on the first
        # question -- and the session outlives it in the state file.
        self._drop_session("direct")
        backend = self.backends.get("direct")
        if backend is None:
            return
        try:
            backend.switch_model(model)
        except Exception:
            pass

    def _drop_backend(self, agent_id):
        """Forget a backend so the next call rebuilds it from the config."""
        backend = self.backends.pop(agent_id, None)
        if backend is not None:
            try:
                backend.close()
            except Exception:
                pass

    # -- authorisation (connected assistants) --------------------------------

    def auth_file_path(self, agent_id):
        """The file that holds an assistant's authorisation.

        Named after the assistant id, so ``workbuddy`` becomes
        ``workbuddy-auth.json`` -- the path the repository keeps out of
        version control.
        """
        root = self.config.get("_root") or "."
        return os.path.join(str(root), agent_id + "-auth.json")

    def agent_auth(self, agent_id):
        """The stored authorisation for an assistant, or an empty dict."""
        try:
            with open(self.auth_file_path(agent_id), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def agent_auth_status(self, agent_id):
        """Whether an assistant is authorised, without revealing the token.

        A connected assistant is authorised when the browser sign-in has
        produced an access token. The model is separate: it may still be
        unset, but the account is signed in.
        """
        data = self.agent_auth(agent_id)
        token = str(data.get("access_token") or data.get("api_key") or "").strip()
        return {
            "agent": agent_id,
            "configured": bool(token),
            "base_url_set": bool(str(data.get("base_url") or "").strip()),
            "model_set": bool(str(data.get("model") or "").strip()),
            # Who is signed in, so the console can show the account name
            # instead of a bare "authorised".
            "nickname": str(data.get("nickname") or "").strip(),
            "region": str(data.get("region") or "").strip(),
        }

    # -- browser sign-in -----------------------------------------------------

    def agent_login_start(self, agent_id, region=None, base=None):
        """Start a browser sign-in for a connected assistant.

        The user is sent to a WorkBuddy login page; when they have signed in,
        the credential is fetched and stored. Returns the URL to open.
        """
        if agent_id not in REGISTRY or not REGISTRY[agent_id].get("connection"):
            raise ValueError("该助手不支持单独授权")
        oauth = self._oauth_for(agent_id)
        result = oauth.start(region=region, base=base)
        if result.get("error"):
            return result
        # Remember which assistant the login belongs to, so the poll knows
        # where to store the credential.
        self._logins[result["login_id"]] = agent_id
        return result

    def agent_login_poll(self, agent_id, login_id):
        """Check whether the browser sign-in has finished.

        On success the credential is written to the assistant's
        authorisation file and the backend is rebuilt so it uses it.
        """
        if agent_id not in REGISTRY or not REGISTRY[agent_id].get("connection"):
            raise ValueError("该助手不支持单独授权")
        oauth = self._oauth_for(agent_id)
        result = oauth.poll(login_id)
        if not result.get("done"):
            return result
        if result.get("error"):
            self._logins.pop(str(login_id or ""), None)
            return result

        account = result.get("account") or {}
        stored = self.agent_auth(agent_id)
        # A model that was already chosen survives the sign-in; a new
        # account starts from the catalogue's default.
        if not stored.get("model"):
            stored["model"] = account.get("model") or "hy3"
        stored.update({
            "access_token": account.get("access_token") or "",
            "refresh_token": account.get("refresh_token") or "",
            "domain": account.get("domain") or "",
            "region": account.get("region") or "cn",
            "auth_base": account.get("auth_base") or "",
            "uid": account.get("uid") or "",
            "nickname": account.get("nickname") or "",
            "uin": account.get("uin") or "",
        })
        if account.get("expires_at"):
            stored["expires_at"] = account["expires_at"]
        config_module.atomic_json(self.auth_file_path(agent_id), stored)
        self._logins.pop(str(login_id or ""), None)
        # The backend reads the authorisation file when it is built, so a
        # fresh credential needs a fresh backend.
        self._drop_backend(agent_id)
        self.log_transcript("system", agent_id,
                            "WorkBuddy 已登录：%s" % (stored.get("nickname") or "账号"))
        return {"done": True, "account": {
            "nickname": stored.get("nickname") or "",
            "region": stored.get("region") or "",
        }}

    def _oauth_for(self, agent_id):
        """The browser sign-in helper for an assistant."""
        oauth = self._oauth_cache.get(agent_id)
        if oauth is None:
            oauth = WorkBuddyOAuth()
            self._oauth_cache[agent_id] = oauth
        return oauth

    def set_agent_auth(self, agent_id, settings=None):
        """Save an assistant's authorisation.

        Only assistants flagged ``connection`` accept this. The token itself
        is written to the dedicated auth file (0600), never echoed back: the
        console shows only that one is stored.
        """
        if agent_id not in REGISTRY or not REGISTRY[agent_id].get("connection"):
            raise ValueError("该助手不支持单独授权")
        settings = settings or {}
        data = self.agent_auth(agent_id)
        for key in ("base_url", "model", "timeout_seconds", "nickname",
                    "region", "domain", "uid", "uin", "auth_base"):
            if key not in settings or settings[key] is None:
                continue
            value = settings[key]
            if key == "timeout_seconds":
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            else:
                value = str(value).strip()
            data[key] = value
        # An empty key means "keep the one that is stored", so the console
        # can show a placeholder instead of the secret. The token lives under
        # ``access_token``; ``api_key`` is accepted as the older spelling.
        for key in ("access_token", "api_key"):
            if key in settings and str(settings[key] or "").strip():
                data["access_token"] = str(settings[key]).strip()
                break
        config_module.atomic_json(self.auth_file_path(agent_id), data)
        # The backend reads the auth file on build, so a change needs a rebuild.
        self._drop_backend(agent_id)
        self.log_transcript("system", agent_id, "已更新授权信息")
        return self.agent_auth_status(agent_id)

    def select(self, agent_id, announce=True):
        """Switch to an assistant. Returns the id actually selected.

        ``announce`` is False when the switch came from the computer: the
        screen shows the new character, but the speaker does not greet it
        again -- the user just clicked, they did not say anything.
        """
        with self.lock:
            already = self.selected == agent_id
            self.selected = agent_id
            self.store["selected_agent"] = agent_id
        self.emit("display", agent=agent_id, state="idle")
        # Saying the name again while that assistant is already in charge
        # would repeat the greeting for no reason.
        if already:
            return agent_id
        if announce:
            self.emit("speak", agent=agent_id,
                      text=agent_name(agent_id) + "在，请说你的任务。")
        self.log_transcript("system", agent_id, "已切换到 " + agent_name(agent_id))
        self._save_state()
        return agent_id

    def exit(self, announce=True):
        """Return to the plain factory behaviour.

        ``announce`` is False when the switch came from the computer, for
        the same reason as in :meth:`select`.
        """
        with self.lock:
            was = self.selected
            self.selected = None
            self.store["selected_agent"] = None
        self.emit("display", agent="none", state="idle")
        # Leaving an assistant that was never there is a no-op, and the
        # speaker repeating "已退出助手" every time the word was caught by
        # mistake only filled the console with grey noise.
        if not was:
            return None
        if announce:
            self.emit("speak", agent="none", text="已退出助手。")
        self.log_transcript("system", "none", "已退出助手")
        self._save_state()
        return None

    # -- the main entry point -------------------------------------------------

    def handle_event(self, event):
        """Process one event from the device. Returns the HTTP response body."""
        kind = event.get("type", "asr_final")

        if kind in ("playback_done", "playback_stopped", "playback_error", "tts_error"):
            # Device acknowledgements are not user messages.
            if event.get("source") == "audio":
                # A MediaPlayer clip finished. Keep this out of the TTS
                # error counter and out of the echo-suppression window:
                # neither belongs to a spoken reply.
                with self.lock:
                    self.audio_state = ("error" if kind in ("playback_error",
                                                           "tts_error")
                                        else "idle")
                return {"accepted": True, "intent": "audio_ack"}
            if kind == "playback_done":
                with self.lock:
                    self.tts_errors = 0
                # The broadcast is over, so the microphone stops hearing it.
                self.mark_spoken_done()
            if kind == "tts_error":
                with self.lock:
                    self.tts_errors += 1
            return {"accepted": True, "intent": "ack"}

        if kind == "volume_state":
            # The device reports the level it actually set on STREAM_MUSIC.
            # It is not a user turn and must not touch the transcript.
            with self.lock:
                self.music_volume = event.get("volume")
                self.music_volume_max = event.get("max")
                self.music_muted = bool(event.get("muted"))
            return {"accepted": True, "intent": "volume_ack"}

        if kind == "tts_stuck":
            # The device's TTS engine stopped answering. Restart it; the
            # device rebinds and replays whatever was lost.
            self.log_transcript("system", "none", "语音引擎无响应，正在重启")
            threading.Thread(target=self._restart_tts, daemon=True).start()
            return {"accepted": True, "intent": "recover"}

        if kind == "wake":
            # The Mac is reading an answer in the same room; the speaker's
            # wake engine hearing it is not the user calling anyone. Dropping
            # this keeps the screen from flashing "listening" on its own.
            if self.mac_speaking():
                return {"accepted": True, "intent": "wake_echo"}
            # No assistant chosen yet means the idle ball, not the default
            # assistant's character.
            self.emit("state", state="listening",
                      agent=self.selected or "none")
            return {"accepted": True}

        if kind == "wake_word":
            # The wake engine itself says which word was matched. This is
            # the only reliable moment to pick an assistant: by the time
            # the recognised sentence arrives, the NLP layer has stripped
            # the wake word off the front ("若琪休息吧" becomes "休息吧").
            word = event.get("word", "")
            if not isinstance(word, str) or len(word) > 200:
                raise ValueError("word 必须是最多200字符的字符串")
            settings = self.wake_settings()
            spellings = self.wake_spellings() if settings["enabled"] else {}
            pinyins = {
                agent_id: row["pinyin"]
                for agent_id, row in settings["agents"].items()
                if row["enabled"] and row["pinyin"]
            }
            agent = intent.wake_word_agent(word, spellings, pinyins)
            if not agent:
                # "若琪|洛奇" and the other factory words are not ours to
                # route; the factory keeps answering exactly as before.
                return {"accepted": True, "intent": "wake_word",
                        "selected_agent": self.selected or ""}
            now = time.time()
            # A "wake" printed while the speaker is playing our own answer is
            # an echo, not the user calling someone. The ASR text path already
            # drops those; the wake-word path must do the same, or every
            # answer that mentions a name re-selects an assistant.
            if self.wake_during_speech(now):
                return {"accepted": True, "intent": "wake_echo",
                        "selected_agent": self.selected or ""}
            with self.lock:
                last_agent, when = self.last_wake_route
                age = now - when
                # The engine re-reports its own hits in bursts, often with a
                # different label each time, so a same-agent window alone
                # lets alternating agents switch the screen every second.
                # Nobody names two different assistants within 1.5s, while
                # the self-wake burst does exactly that.
                if age < WAKE_BURST_GAP or (last_agent == agent and age < 3):
                    return {"accepted": True, "intent": "wake_word",
                            "selected_agent": agent, "duplicate": True}
                self.last_wake_route = (agent, now)
            # No greeting: the speaker is already talking, and a broadcast
            # now would make the microphone hear its own voice instead.
            self.select(agent, announce=False)
            return {"accepted": True, "intent": "wake_word",
                    "selected_agent": agent}

        if kind not in ("asr_final", "text"):
            raise ValueError("不支持的事件类型")

        text = event.get("text", "")
        request_id = event.get("request_id", "")
        source = event.get("source", "rokid")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise ValueError("text 必须是非空字符串，最多16000字")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
            raise ValueError("需要最多200字符的唯一 request_id")

        with self.lock:
            if request_id in self.seen:
                return {"accepted": True, "duplicate": True}
            self.seen.add(request_id)
            if len(self.seen) > 512:
                self.seen = set(list(self.seen)[-256:])

        # A typed sentence in the console is a deliberate act, so the echo
        # guard does not apply to it. The factory-notice guard below does:
        # that clip is never a user turn, on any channel.
        typed = kind == "text" or source == "console"

        # The speaker hears its own output. Answering that echo repeats the
        # broadcast forever, so drop it before it reaches the assistant.
        if not typed and self.is_echo(text):
            return {"accepted": True, "intent": "echo", "duplicate": True}

        # The factory's canned service-expiry notice ("内容服务已到期，请续费")
        # is a broadcast artefact, not something the user said. It used to
        # be dropped only when it came from ASR; typed lines and injected
        # events still reached an assistant and showed up in the chat.
        if self._suppressed_notice(text):
            return {"accepted": True, "intent": "factory_notice",
                    "duplicate": True}

        # A custom wake word is spoken at the front of the recognised
        # sentence: "大肥鱼今天天气怎么样" both wakes the speaker and picks
        # that assistant. Take the word off before intent parsing, so the
        # assistant hears only the question. With the feature off this
        # map is empty and nothing changes.
        if not typed:
            hit, remainder = intent.wake_prefix(text, self.wake_spellings())
            if hit:
                self.select(hit)
                if not remainder.strip():
                    return {"accepted": True, "intent": "select",
                            "selected_agent": hit}
                text = remainder

        # The same sentence can arrive from the event bus and from the
        # logcat watcher within a moment. Treat repeated wording inside the
        # window as one utterance.
        normalized = intent.normalize(intent.strip_wake_word(text))
        if not normalized or normalized == "null":
            # Nothing was recognised. Answering this would make the device
            # talk back at a silence.
            return {"accepted": True, "intent": "empty", "duplicate": True}
        if not typed:
            now = time.time()
            with self.lock:
                previous, when = self.last_utterance
                if normalized and normalized == previous and now - when < DEDUP_WINDOW:
                    return {"accepted": True, "intent": "duplicate", "duplicate": True}
                self.last_utterance = (normalized, now)

        # Playback controls belong to the speaker's own MediaPlayer, not
        # to an assistant. They must be taken before parse_intent: "停止"
        # is also a stop-talking word, and with an assistant selected the
        # model would otherwise answer "好的" without touching the song.
        control = intent.music_control(text)
        if control:
            if self._roon_route():
                return self.apply_roon_control(control, request_id)
            with self.lock:
                playing = self.audio_state in ("playing", "paused")
            if not playing:
                # Nothing is on. Staying silent keeps both the model and
                # the factory from replying about a player that is idle.
                self.log_transcript("system", "music",
                                    "未在播放，忽略：" + text, request_id)
                return {"accepted": True, "intent": "music_control",
                        "action": control, "applied": False, "idle": True}
            return self.apply_music_control(control, request_id)

        # "我想听晴天" / "放一首周杰伦的歌": the bridge's own library
        # plays it. Taken before parse_intent so an assistant never
        # answers "好的" without starting the song.
        request = intent.music_request(text)
        if request:
            return self.apply_music_request(request, request_id)

        # Home turns ("把书房灯调到 50%") must be taken before the volume
        # rule, which would otherwise read the 50% as speaker loudness.
        #
        # Ownership (user decision, 2026-11-20): only 若琪 and 小家 may
        # touch the house.
        #   若琪 (no assistant selected) -> the HA bridge forwards the
        #       sentence to HA's conversation agent, with state
        #       verification, the result card and the factory guard
        #       exactly as before.
        #   小家 -> a normal turn for that assistant: its backend calls
        #       HA directly. No forwarding, no bridge result card.
        #   any other assistant -> do not execute anything: no HA call,
        #       no model turn. Answer with one fixed hint pointing at 小家.
        #       The device's ha_rule has already silenced the factory.
        # Local skills (music / volume / miniapps / weather / clock) keep
        # their priority for every assistant, unchanged.
        home_sentence = self.homeassistant.home_sentence(text)
        if home_sentence:
            if self.selected == "xiaojia":
                # A home turn for 小家 still shows the HA result card.
                # Its backend answers through HA directly, so the card is
                # built in the worker once the reply arrives (see
                # _run_job), not here in the routing path.
                return self._enqueue(home_sentence, request_id,
                                     "xiaojia", "command", home=True)
            if self.selected:
                self.log_transcript("user", self.selected, text, request_id)
                self.emit("speak", agent=self.selected,
                          text=HOME_REDIRECT_HINT, request_id=request_id)
                self.log_transcript("agent", self.selected,
                                    HOME_REDIRECT_HINT, request_id)
                return {"accepted": True, "intent": "homeassistant",
                        "status": "redirected",
                        "selected_agent": self.selected,
                        "speech": HOME_REDIRECT_HINT}
        if not self.selected:
            # 若琪: the gate, the command queue, the result card and the
            # pending clarification window all live in the HA service.
            ha_result = self.homeassistant.handle_text(text, request_id)
            if ha_result:
                return ha_result

        volume = intent.music_volume(text)
        if volume:
            if self._roon_route():
                return self.apply_roon_volume(volume, request_id)
            return self.apply_music_volume(volume, request_id)

        # Query apps answer their exact phrases before an assistant (or the
        # direct fallback) sees the sentence. Music, volume and the stops
        # above keep their priority; nothing here changes the selected agent.
        mini_result = self.miniapps.handle_text(text, request_id)
        if mini_result:
            return mini_result

        # The factory's weather skill is gone (its cloud answers with the
        # service-expiry notice), so weather turns open the native app
        # ourselves: it then fetches data from the local replacement API
        # and speaks in the factory voice, exactly as it used to.
        weather_result = self.weather.handle_text(text, request_id)
        if weather_result:
            return weather_result

        # Standby clock: 「换个表盘」and friends cycle the device face.
        clock_result = self.clock.handle_text(text, request_id)
        if clock_result:
            return clock_result

        action, argument = intent.parse_intent(text)

        if action == "select":
            if not intent.select_enabled(argument):
                self.emit("speak", agent=argument or "none",
                          text=agent_name(argument) + "还没有接入。")
                self.log_transcript("system", argument or "", agent_name(argument) + " 未接入")
                return {"accepted": True, "intent": "select",
                        "selected_agent": self.selected or ""}
            self.select(argument)
            return {"accepted": True, "intent": "select", "selected_agent": argument}

        if action == "exit":
            self.exit()
            return {"accepted": True, "intent": "exit"}

        if action == "repeat":
            speech_text = getattr(self, "last_speech", "")
            if speech_text:
                self.emit("speak", text=speech_text, request_id=request_id)
                self.log_transcript("user", self.selected or "", "重播", request_id)
                return {"accepted": True, "intent": "repeat"}
            self.emit("speak", text="我还没有回答过。", request_id=request_id)
            return {"accepted": True, "intent": "repeat", "empty": True}

        if action == "stop":
            with self.lock:
                target = self.active_request_id
                if target:
                    self.cancelled.add(target)
                    backend = self.backend_for(self.selected) if self.selected else None
                    if backend:
                        backend.cancel(target)
            self.emit("stop_speaking", request_id=request_id)
            self.log_transcript("system", self.selected or "", "已打断播报")
            return {"accepted": True, "intent": "stop", "interrupted": bool(target)}

        if action == "direct":
            # "用大模型" / "切回直连": leave the assistant and let the
            # speaker answer on its own.
            mode = self.set_direct_mode(HYBRID)
            self.exit()
            self.emit("speak", text="好，直连已打开（" + self._mode_text(mode) + "）。")
            return {"accepted": True, "intent": "direct", "mode": mode}

        # A task for the currently selected assistant.
        agent_id = self.selected
        if not agent_id:
            # No assistant chosen: the speaker may still answer, when
            # direct mode is on.
            decision = self._direct_decision(event, text)
            if decision == "silent":
                # The factory answers this one. Staying quiet avoids
                # two replies at once.
                #
                # The factory speaks through its own voice, so the words
                # it says never reach us -- but the question and the fact
                # that the factory answered it still belong in the
                # transcript. Writing only a grey "the factory answered"
                # line left the console looking like nothing had happened,
                # while in fact the turn was handled by the speaker itself.
                self.log_transcript("user", "direct", text, request_id)
                self.log_transcript("agent", "direct",
                                    "（这句话由原厂若琪回答）", request_id)
                return {"accepted": True, "intent": "factory", "selected_agent": ""}
            if decision == "answer":
                return self._queue_direct(text, request_id)
            self.emit("speak", text="先说找小赫，我才能接你的任务。")
            return {"accepted": True, "intent": "command", "selected_agent": ""}

        return self._enqueue(text, request_id, agent_id, "command")

    def _enqueue(self, text, request_id, agent_id, intent_name,
                 home=False):
        """Put one turn on the queue, unless it is a repeat.

        The queue holds at most one turn at a time. A turn that arrives
        while another one is running is **queued, not dropped**: the
        caller asked a question and deserves an answer. This matters most
        for the assistants that take a while -- DeepSeek runs commands
        and can take a minute -- because during that time every further
        question used to vanish without a trace, and the console showed
        nothing at all.

        The one exception is a repeat of a turn that is already waiting:
        the same wording twice in a row is the recogniser reporting one
        utterance twice, not two questions.
        """
        key = intent.normalize(text)
        with self.lock:
            if key in self.queued_texts:
                return {"accepted": True, "intent": intent_name,
                        "duplicate": True}
            # A voice turn can be interrupted and never reach the queue,
            # so the list is capped. The cap is generous enough that a
            # burst of questions is never mistaken for a repeat.
            self.queued_texts.append(key)
            if len(self.queued_texts) > 64:
                self.queued_texts.pop(0)
            if self.jobs.qsize() >= MAX_PENDING_TURNS:
                self.queued_texts.remove(key)
                self.emit("display", agent=agent_id, state="thinking",
                          text="问题太多了，先等前面的答完")
                self.log_transcript("system", agent_id,
                                    "队列已满，这句没有送出去", request_id)
                return {"accepted": True, "intent": intent_name,
                        "dropped": True, "reason": "queue full"}

        job = {"text": text.strip(), "request_id": request_id,
               "agent": agent_id, "key": key, "home": bool(home)}
        self.jobs.put_nowait(job)
        self.emit("state", state="queued", request_id=request_id)
        self.emit("display", agent=agent_id, state="thinking",
                  text=speech.summary_of(text, 40))
        # The question is written down as soon as it is accepted, so the
        # console shows it even while it waits its turn.
        self.log_transcript("user", agent_id, text, request_id)
        return {"accepted": True, "intent": intent_name,
                "request_id": request_id, "agent": agent_id}

    def _queue_direct(self, text, request_id):
        """Hand a question to the model, with no assistant selected.

        The turn goes through the same queue as an assistant task, so the
        speaker behaves the same way: one question at a time, the screen
        shows the progress, and the answer is spoken.
        """
        backend = self.backend_for("direct")
        ok, detail = backend.health()
        if not ok:
            self.emit("speak", agent="none", text="直连还没配置好：" + detail)
            self.log_transcript("system", "none", "直连不可用：" + detail)
            return {"accepted": True, "intent": "direct", "error": detail}

        return self._enqueue(text, request_id, "direct", "direct")

    # -- typed input from the console -----------------------------------------

    # -- proactive chat (B plan, route R3) ------------------------------------

    def neko_ear_only_settings(self):
        """The speaker-as-microphone mode for the N.E.K.O. assistant."""
        return {"enabled": bool((self.config.get("neko") or {}).get(
            "ear_only", False))}

    def neko_relay_settings(self):
        """Desktop-owned turns: no host WebSocket, Mac speaks, speaker mute."""
        defaults = dict(config_module.DEFAULTS["neko"]["relay"])
        row = ((self.config.get("neko") or {}).get("relay") or {})
        settings = dict(defaults)
        settings.update({k: v for k, v in row.items() if k in defaults})
        settings["enabled"] = bool(settings["enabled"])
        return settings

    def set_neko_relay(self, enabled=None):
        """Flip relay mode, persist, and rebuild the cached neko backend."""
        if enabled is not None:
            neko = dict(self.config.get("neko") or {})
            settings = self.neko_relay_settings()
            settings["enabled"] = bool(enabled)
            neko["relay"] = settings
            self.config["neko"] = neko
            self._save_config()
            # NekoBackend captured the relay flag when it was constructed.
            # Dropping it is what actually stops/starts the host WebSocket.
            self._drop_backend("neko")
            if not settings["enabled"]:
                self.mac_speaker.stop()
        return self.neko_relay_settings()

    def mac_voice_settings(self):
        """How the Mac reads a line while the speaker stays silent.

        Ear-only mode needs a mouth on the Mac side, because the upstream
        server delivers a turn's text and TTS audio only to the connection
        that started it -- the host's bridge connection, not the desktop
        window. ``say`` is the local synthesiser. The switch exists so the
        Mac can be muted (a meeting, a sleeping baby) without leaving the
        mode and handing the voice back to the speaker.
        """
        defaults = dict(config_module.DEFAULTS["neko"].get("mac_voice") or {
            "enabled": True, "voice": mac_speech.DEFAULT_VOICE,
            "rate": mac_speech.DEFAULT_RATE, "dodge": True,
            "dodge_with": "人家", "dodge_extra": []})
        row = ((self.config.get("neko") or {}).get("mac_voice") or {})
        settings = dict(defaults)
        settings.update({k: v for k, v in row.items() if k in defaults})
        settings["enabled"] = bool(settings["enabled"])
        settings["dodge"] = bool(settings["dodge"])
        settings["voice"] = str(settings["voice"] or mac_speech.DEFAULT_VOICE)
        settings["dodge_with"] = str(settings["dodge_with"] or "人家")
        settings["dodge_extra"] = [str(w).strip()
                                   for w in (settings.get("dodge_extra") or [])
                                   if str(w).strip()]
        try:
            settings["rate"] = max(80, min(400, int(settings["rate"])))
        except (TypeError, ValueError):
            settings["rate"] = mac_speech.DEFAULT_RATE
        return settings

    def _speak_on_mac(self, text, request_id=""):
        """Read a reply on the Mac, and open an echo window for it.

        The speaker's microphone sits in the same room as the Mac's
        speakers, so everything said here is heard by the wake engine and
        by the recogniser. ``emit("speak")`` opens a window for the device's
        own broadcast; ear-only mode has to open the same kind of window or
        the reply the Mac just read comes back as a user turn -- wake words,
        commands and all.
        """
        settings = self.mac_voice_settings()
        if not settings["enabled"] or not self.mac_speaker.available():
            return None
        spoken = text
        if settings["dodge"]:
            words = []
            for spellings in self.wake_spellings().values():
                words.extend(spellings)
            # The factory's own words are not in that table: "若琪" is the
            # speaker's name, not one of our assistants. The Mac saying it
            # wakes the factory, which answers by itself -- the host never
            # sees that turn, so there is nothing to gate.
            words.extend(settings.get("dodge_extra") or [])
            spoken = mac_speech.dodge(spoken, words, settings["dodge_with"])
        if not spoken.strip():
            return None
        self.mac_speaker.voice = settings["voice"]
        self.mac_speaker.rate = settings["rate"]
        entry = {"text": intent.normalize(spoken), "at": time.time(),
                 "done": False, "mac": True,
                 "cap": self.mac_speaker.estimate(spoken) + ECHO_TAIL}
        with self.lock:
            self.recent_speeches.append(entry)

        def finished(ok=True):
            with self.lock:
                if entry.get("ended") is None:
                    entry["done"] = True
                    entry["ended"] = time.time()

        try:
            return self.mac_speaker.speak(spoken, finished)
        except Exception as error:
            finished(False)
            self.log_transcript("system", "neko",
                                "Mac 朗读失败：" + str(error)[:120], request_id)
            return None

    def _mac_echo_window(self, text, request_id=""):
        """Gate the microphone for a line the desktop client will read aloud.

        Relay mode has no local `say` process to report when playback ends,
        so the entry simply expires on its ``cap``. D042 already switched
        ``is_echo`` / ``wake_during_speech`` to cap-based timing, and made
        ``mark_spoken_done`` skip ``mac`` entries, so no device playback
        report can close this window early.
        """
        spoken = str(text or "").strip()
        if not spoken:
            return None
        entry = {"text": intent.normalize(spoken), "at": time.time(),
                 "done": False, "mac": True,
                 "cap": self.mac_speaker.estimate(spoken) + ECHO_TAIL}
        with self.lock:
            self.recent_speeches.append(entry)
        return entry

    def _schedule_avatar_idle(self, text, request_id=""):
        """Reset the speaker's avatar to idle once the desktop has read a line.

        In relay mode the speaker never plays TTS, so the device's normal
        ``audioFinished() -> uiState = idle`` transition never happens and the
        catgirl would stay on the "speaking" animation forever while the Mac
        has gone quiet. Mirror the desktop turn with a timed ``display idle``
        after the estimated reading time so the two faces agree again.

        Guards keep the timer honest: a newer turn, a cancelled/closed engine
        or an assistant switch must not be dragged back to idle by an old
        line's timer.
        """
        delay = self.mac_speaker.estimate(text)
        timer = threading.Timer(delay, self._avatar_idle_elapsed,
                                args=(request_id,))
        timer.daemon = True
        with self.lock:
            if self.closed or not self.neko_relay_settings()["enabled"]:
                return
            self._avatar_idle_timers.add(timer)
        timer.start()

    def _avatar_idle_elapsed(self, request_id=""):
        """Fire a scheduled avatar reset, unless a newer turn owns the face."""
        timer = threading.current_thread()
        with self.lock:
            self._avatar_idle_timers.discard(timer)
            if self.closed or not self.neko_relay_settings()["enabled"]:
                return
            if self.active_request_id and self.active_request_id != request_id:
                return
            if self.selected != "neko":
                return
        self.emit("display", agent="neko", state="idle")

    def set_neko_ear_only(self, enabled=None):
        """Flip ear-only mode and persist it. Voice input stays as it was."""
        if enabled is not None:
            neko = dict(self.config.get("neko") or {})
            neko["ear_only"] = bool(enabled)
            self.config["neko"] = neko
            self._save_config()
            if not neko["ear_only"]:
                # Handing the voice back to the speaker: a Mac line still in
                # flight would talk over it.
                self.mac_speaker.stop()
        return self.neko_ear_only_settings()

    def neko_proactive_settings(self):
        """Current settings, with defaults for anything an older config lacks."""
        defaults = dict(config_module.DEFAULTS["neko"]["proactive"])
        row = ((self.config.get("neko") or {}).get("proactive") or {})
        settings = dict(defaults)
        settings.update({k: v for k, v in row.items() if k in defaults})
        settings["enabled"] = bool(settings["enabled"])
        for key, low, high in (("interval_minutes", 1, 240),
                               ("idle_seconds", 0, 3600)):
            try:
                settings[key] = max(low, min(high, int(settings[key])))
            except (TypeError, ValueError):
                settings[key] = defaults[key]
        for key in ("quiet_start", "quiet_end"):
            settings[key] = str(settings[key] or defaults[key])
            settings[key + "_min"] = hhmm_to_minutes(settings[key])
        return settings

    def set_neko_proactive(self, **changes):
        """Update and save the settings. Only known keys are accepted."""
        allowed = ("enabled", "interval_minutes", "quiet_start", "quiet_end",
                   "idle_seconds")
        settings = self.neko_proactive_settings()
        for key, value in changes.items():
            if key not in allowed:
                raise ValueError("未知的猫娘设置：" + str(key))
            if key in ("quiet_start", "quiet_end"):
                text = str(value or "").strip()
                if not re.fullmatch(r"\d{1,2}:\d{2}", text) or \
                        hhmm_to_minutes(text, fallback=-1) < 0:
                    raise ValueError("时间要写成 HH:MM，例如 23:30")
                settings[key] = text
            elif key == "enabled":
                settings[key] = bool(value)
            else:
                try:
                    number = int(value)
                except (TypeError, ValueError):
                    raise ValueError(key + " 要是数字")
                low, high = ((1, 240) if key == "interval_minutes"
                             else (0, 3600))
                if not low <= number <= high:
                    raise ValueError(key + " 超出范围")
                settings[key] = number
        neko = dict(self.config.get("neko") or {})
        neko["proactive"] = {k: settings[k] for k in allowed}
        self.config["neko"] = neko
        self._save_config()
        return self.neko_proactive_settings()

    def _quiet_now(self, settings, now=None):
        local = time.localtime(now if now is not None else time.time())
        return in_quiet_window(local.tm_hour * 60 + local.tm_min,
                               settings["quiet_start_min"],
                               settings["quiet_end_min"])

    def _proactive_gate(self, now=None, settings=None):
        """Every condition for an unsolicited turn. Pure enough to test."""
        now = now if now is not None else time.time()
        settings = settings or self.neko_proactive_settings()
        # Relay: the desktop client owns every turn, proactive ones included.
        # Ear-only: the same division of labour for the say-based fallback.
        # The saved proactive settings stay untouched and resume on exit.
        if self.neko_relay_settings()["enabled"]:
            return False
        if self.neko_ear_only_settings()["enabled"]:
            return False
        if not settings["enabled"]:
            return False
        if self.selected != "neko":
            return False
        if self.busy or not self.jobs.empty():
            return False
        # Music is playing (or paused) through the speaker's own
        # MediaPlayer. An unsolicited TTS line on top of it would be two
        # voices at once; a paused song is still on the screen and about
        # to come back.
        if self.audio_state in ("playing", "paused", "loading"):
            return False
        if self._quiet_now(settings, now):
            return False
        if now - self._proactive_last_at < settings["interval_minutes"] * 60:
            return False
        # Do not cut into a conversation that just happened.
        last_user = self.last_utterance[1] if self.last_utterance else 0.0
        if now - last_user < settings["idle_seconds"]:
            return False
        return True

    def _proactive_loop(self):
        while not self._proactive_stop.wait(5.0):
            try:
                if self._proactive_gate():
                    self._submit_proactive()
            except Exception:
                # A timer must never take the host down.
                pass

    def _submit_proactive(self, force=False):
        """Queue one unsolicited turn. force is the console's test button."""
        if not force:
            if not self._proactive_gate():
                return {"accepted": False, "reason": "gated"}
        elif self.selected != "neko":
            return {"accepted": False, "reason": "neko not selected"}
        request_id = "neko-proactive-" + uuid.uuid4().hex[:10]
        with self.lock:
            self._proactive_last_at = time.time()
        # The worker, not this timer, runs the backend: one WS, one pending
        # turn, so an unsolicited line can never clash with a question.
        self.jobs.put_nowait({"text": PROACTIVE_PROMPT,
                              "request_id": request_id,
                              "agent": "neko", "proactive": True})
        return {"accepted": True, "request_id": request_id}

    def proactive_now(self, mode="replay"):
        """Console 'say something' button.

        ``replay`` (default) repeats the catgirl's last line on the speaker
        only, touching no upstream API. ``fresh`` asks N.E.K.O. for a new
        line -- the desktop client will read that answer aloud too, which is
        upstream behaviour this project cannot suppress. Both are exposed so
        the user can pick; timed turns always use ``fresh``.
        """
        if self.selected != "neko":
            return {"accepted": False, "reason": "neko not selected"}
        if mode == "fresh":
            return self._submit_proactive(force=True)
        if mode != "replay":
            raise ValueError("mode 只能是 replay 或 fresh")
        with self.lock:
            recent = [row for row in self.transcript
                      if row.get("role") == "agent"
                      and row.get("agent") == "neko"
                      and str(row.get("message") or "").strip()]
        if not recent:
            return {"accepted": False, "reason": "no answer yet"}
        text = str(recent[-1]["message"]).strip()
        request_id = "neko-replay-" + uuid.uuid4().hex[:10]
        # The worker owns busy/state, so a replay cannot clash with a
        # question that is already in flight.
        self.jobs.put_nowait({"text": text, "request_id": request_id,
                              "agent": "neko", "replay": True})
        return {"accepted": True, "request_id": request_id, "replay": True}

    def _run_replay(self, job):
        request_id = job["request_id"]
        with self.lock:
            self.busy = True
            self.active_request_id = request_id
        try:
            if self.selected != "neko":
                return
            text = job["text"]
            self.emit("result", text=text, request_id=request_id, agent="neko")
            self.log_transcript("agent", "neko", text, request_id)
            self.deliver_speech(text, "neko", request_id)
        finally:
            with self.lock:
                self.busy = False
                self.active_request_id = ""

    def deliver_speech(self, answer, agent_id, request_id="",
                        card_id="", expires_at_ms=0):
        """The one place assistant speech leaves the host.

        Swapping in a TTS server later changes only this method: proactive
        triggers, persona, memory and the avatar stay untouched.

        ``card_id`` binds the spoken line to a card already on screen;
        the device then queues it behind any TTS session still winding
        down. Without the binding a home-feedback announcement can be
        dropped as ``tts_error error=2`` after a factory cancel.
        """
        limit = int(self.config.get("speech_chars") or 400)
        spoken = speech.clip(speech.to_speech(answer), limit,
                             "完整内容请查看电脑端。")
        self.last_speech = spoken
        self.emit("display", agent=agent_id, state="speaking",
                  text=speech.summary_of(answer, 60))
        # Ear-only mode: the speaker is a microphone and a screen. The Mac
        # reads the answer instead -- the upstream server only delivers a
        # turn's text and audio to the connection that started it, so the
        # desktop window never sees it and the host has to speak. The window
        # opened here is what keeps the speaker's microphone from hearing
        # that voice as a user turn.
        # Relay mode: the desktop client already displayed this reply in the
        # shared chat history and is reading it aloud with its own TTS. The
        # speaker must stay silent (no device TTS, no local `say`), but its
        # microphone still needs an echo window for the voice coming out of
        # the Mac's speakers.
        if agent_id == "neko" and self.neko_relay_settings()["enabled"]:
            self._mac_echo_window(spoken, request_id)
            self._schedule_avatar_idle(spoken, request_id)
            return spoken
        if agent_id == "neko" and self.neko_ear_only_settings()["enabled"]:
            self._speak_on_mac(spoken, request_id)
            return spoken
        payload = {"text": spoken, "request_id": request_id,
                   "agent": agent_id}
        if card_id:
            payload["card_id"] = card_id
            payload["expires_at_ms"] = int(expires_at_ms)
        self.emit("speak", **payload)
        return spoken

    def _run_proactive(self, job):
        request_id = job["request_id"]
        with self.lock:
            self.busy = True
            self.active_request_id = request_id
            self.active_text = ""
        try:
            # The user may have switched away between submit and execution.
            if self.selected != "neko":
                return
            backend = self.backend_for("neko")
            if backend is None:
                return
            answer = backend.run(PROACTIVE_PROMPT, request_id,
                                 memory_text=PROACTIVE_MEMORY_TEXT)
            if not answer or not str(answer).strip():
                return
            if self.selected != "neko":
                return
            self.emit("result", text=answer, request_id=request_id, agent="neko")
            self.log_transcript("agent", "neko", answer, request_id)
            self.deliver_speech(answer, "neko", request_id)
        except Exception as error:
            # A failed unsolicited turn must not make the speaker announce
            # "遇到了问题". Log it and try again on the next interval.
            self.log_transcript("system", "neko",
                                "主动搭话失败：" + str(error)[:200], request_id)
        finally:
            with self.lock:
                self.busy = False
                self.active_request_id = ""

    def chat(self, text, agent_id=""):
        """Answer a typed line, without going through the voice intents.

        A typed sentence is deliberate: it is never "找小赫" and never
        "退出助手", it is a message to whoever the user picked in the
        console. So it skips the intent layer entirely and goes straight
        to the chosen assistant.

        ``agent_id`` names the assistant. An empty string means the
        speaker itself, which is the direct path.

        The factory's canned service-expiry notice is dropped here too:
        pasted into the console it is still that clip, not a question.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("内容不能为空")
        if len(text) > 16000:
            raise ValueError("内容太长")
        if self._suppressed_notice(text):
            # No transcript line, no assistant job, no broadcast.
            return {"accepted": True, "intent": "factory_notice",
                    "duplicate": True, "dropped": True}
        request_id = "chat-" + uuid.uuid4().hex[:12]

        if agent_id and agent_id != "direct":
            if agent_id not in REGISTRY:
                raise ValueError("未知的助手")
            if not REGISTRY[agent_id].get("enabled"):
                raise ValueError(agent_name(agent_id) + " 还没有接入")

        # The console talks to an assistant without changing which one the
        # speaker is using: a line typed on the computer must not switch
        # the assistant the voice commands go to.
        target = agent_id or "direct"
        if target == "direct":
            return self._queue_direct(text, request_id)

        backend = self.backend_for(target)
        if backend is None:
            raise ValueError("助手尚未接入：" + target)
        ok, detail = backend.health()
        if not ok:
            self.log_transcript("system", target, "助手不可用：" + detail)
            raise ValueError(agent_name(target) + " 还不能回答：" + detail)

        return self._enqueue(text, request_id, target, "chat")

    # -- worker ---------------------------------------------------------------

    def _work(self):
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            try:
                self._run_job(job)
            finally:
                self.jobs.task_done()

    def _run_job(self, job):
        if job.get("replay"):
            self._run_replay(job)
            return
        if job.get("proactive"):
            self._run_proactive(job)
            return
        agent_id = job["agent"]
        request_id = job["request_id"]
        with self.lock:
            self.busy = True
            self.active_text = intent.normalize(job["text"])
            self.active_request_id = request_id

        def progress(state, detail):
            """Forward the agent's own progress to the screen.

            A backend may report the same state many times -- a streaming
            agent emits one token at a time. Only a change of state or of
            detail is worth a screen update.
            """
            key = (state, detail)
            if key == progress.last_key:
                return
            progress.last_key = key
            if state == "working":
                self.emit("display", agent=agent_id, state="working",
                          text=detail or "正在处理")
            elif state == "thinking":
                self.emit("state", state="thinking", request_id=request_id)
            elif state == "speaking":
                self.emit("display", agent=agent_id, state="speaking")
        progress.last_key = None

        try:
            backend = self.backend_for(agent_id)
            if backend is None:
                raise RuntimeError("助手尚未接入：" + str(agent_id))
            answer = backend.run(job["text"], request_id, progress)
            self.emit("result", text=answer, request_id=request_id, agent=agent_id)
            self.log_transcript("agent", agent_id, answer, request_id)
            with self.lock:
                cancelled = request_id in self.cancelled
            if cancelled:
                self.emit("state", state="idle", request_id=request_id)
                return
            card_id, expires_at_ms = "", 0
            if agent_id == "xiaojia" and job.get("home"):
                # 小家 executes HA itself, so it never reaches the bridge's
                # result card. Build one from the raw HA payload and bind
                # the spoken line to it (both feedback and safe TTS queueing).
                try:
                    payload = getattr(backend, "last_payload", None) or {}
                    card_id, expires_at_ms = self.homeassistant.present_xiaojia(
                        payload, job["text"], request_id)
                except Exception as card_error:
                    self.log_transcript("system", agent_id,
                                        "结果卡失败：" + str(card_error)[:120],
                                        request_id)
            self.deliver_speech(answer, agent_id, request_id,
                                card_id=card_id,
                                expires_at_ms=expires_at_ms)
        except Exception as error:
            detail = str(error).strip() or type(error).__name__
            self.emit("state", state="error", request_id=request_id, text=detail[:300])
            self.deliver_speech(agent_name(agent_id) + "遇到了问题，请查看电脑端状态。",
                                agent_id, request_id)
            self.log_transcript("system", agent_id, "错误：" + detail[:300], request_id)
        finally:
            with self.lock:
                self.busy = False
                self.active_text = ""
                self.active_request_id = ""
                self.cancelled.discard(request_id)
                # Remove this turn's own de-duplication key, not just any
                # key: several turns can be waiting at once, and removing
                # the wrong one would let a repeat slip through.
                key = job.get("key")
                if key and key in self.queued_texts:
                    self.queued_texts.remove(key)
            # A backend remembers its session id while it runs; write it out
            # so the next turn can continue the same conversation.
            self._save_state()

    # -- recovery -------------------------------------------------------------

    def _restart_tts(self):
        """Bring the device's TTS engine back and replay what was lost.

        The engine wedges when a request is cancelled while it is in flight;
        it then answers every request with a timeout and never recovers.
        Killing the factory's service makes the system start a fresh one, and
        the device rebinds to it and replays the text that was lost.
        """
        recovery = self.tts_recovery
        if not recovery.available():
            self.log_transcript("system", "none", "无法自动恢复语音引擎")
            return
        if not recovery.restart():
            self.log_transcript("system", "none", "语音引擎重启未成功")
            return
        self.log_transcript("system", "none", "语音引擎已重启")
        # The device keeps the text that failed and replays it once the
        # engine is back, so nothing has to be sent again here.

    # -- status ---------------------------------------------------------------

    def status(self):
        with self.lock:
            # An empty string, never null: the device reads this with
            # JSONObject.optString, which turns a JSON null into the literal
            # text "null" and would then believe an assistant is selected.
            return {
                "selected_agent": self.selected or "",
                "busy": self.busy,
                "queued": len(self.queued_texts),
                "active_request_id": self.active_request_id,
                "last_seq": self.seq,
                "server_instance_id": self.instance_id,
                # The audio channel's own state, kept separate from TTS
                # acknowledgements so a finished song cannot look like a
                # finished spoken reply.
                "audio": {"state": self.audio_state,
                          "title": self.audio_title,
                          "volume": self.music_volume,
                          "volume_max": self.music_volume_max,
                          "muted": self.music_muted},
                # The console's 「其它」 page reads the expiry-notice switch
                # from the same status poll, so a restart cannot silently
                # turn it back on.
                "factory_notice": self.factory_notice_settings(),
                # Ear-only mode is host-side only; this is for the console
                # and for diagnosing a silent speaker.
                "neko_ear_only": self.neko_ear_only_settings(),
                # Relay mode: the desktop owns the only WebSocket, so the
                # speaker is a microphone and a subtitle strip.
                "neko_relay": self.neko_relay_settings(),
                # The Mac's half of that mode: which voice reads the reply,
                # and whether wake words are dodged out of it.
                "neko_mac_voice": self.mac_voice_settings(),
                # The wake words the device should register locally, so a
                # sleeping device picks a new word up with its next poll.
                "wake": self.wake_settings(),
                # The device needs the direct mode: it decides with it
                # whether to silence the factory for a turn.
                "direct": {
                    "mode": self.direct_mode(),
                    "source": str((self.config.get("direct") or {}).get("source") or "assistant"),
                    "assistant": str((self.config.get("direct") or {}).get("assistant") or "hermes"),
                    # The rule the device needs to decide whether to
                    # silence the factory for a turn.
                    "factory_owns": self.factory_pattern(),
                    # Home-control turns silence the factory too, or the
                    # device would answer "没找到该设备" while the host
                    # is already talking to HA. Empty = HA off = no rule.
                    "ha_rule": self.homeassistant.device_rule(),
                },
                "agents": {
                    key: {"name": item["name"], "enabled": bool(item.get("enabled")),
                          "avatar": agent_avatar(key),
                          # The folder under design/avatars/ with this
                          # assistant's pictures, so the console can show
                          # the face Codex drew.
                          "design": agent_design_dir(key),
                          # The state it was last seen in, so the console
                          # can show the face that matches.
                          "state": self.agent_states.get(key, "idle"),
                          # A connected assistant (WorkBuddy and the like)
                          # needs a separate authorisation; the console uses
                          # this to show "待授权" and offer the auth panel.
                          "connection": bool(item.get("connection")),
                          "auth": self.agent_auth_status(key)
                                   if item.get("connection") else None}
                    for key, item in REGISTRY.items()
                },
            }

    def close(self):
        self.closed = True
        self._proactive_stop.set()
        # Cancel pending avatar resets so a shutting-down engine cannot queue
        # a fresh display command from a timer fired after close().
        with self.lock:
            timers = list(self._avatar_idle_timers)
            self._avatar_idle_timers.clear()
        for timer in timers:
            try:
                timer.cancel()
            except Exception:
                pass
        # A Mac line still being spoken would outlive the process that owns
        # the window it opened.
        try:
            self.mac_speaker.stop()
        except Exception:
            pass
        # Every line was flushed as it was written, so all that is left
        # is to close the append handles.
        try:
            self.history.close()
        except Exception:
            pass
        for backend in list(self.backends.values()):
            try:
                backend.close()
            except Exception:
                pass
        try:
            self.homeassistant.close()
        except Exception:
            pass
        self.jobs.put_nowait(None)
        if self.worker is not threading.current_thread():
            self.worker.join(timeout=2)

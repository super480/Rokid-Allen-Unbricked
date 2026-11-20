"""Configuration and the assistant registry.

The registry is the single place that knows which assistants exist. Adding a
new one (Codex, DSH, WorkBuddy, ...) means adding one entry here plus one
module under ``backends/``; nothing else in the host has to change.
"""
from __future__ import annotations

import copy
import json
import os
import secrets
import uuid
from pathlib import Path

# --- assistant registry -----------------------------------------------------
#
# ``enabled`` controls whether the voice command is accepted. A disabled
# assistant is still listed in the console so the user can see it exists.
#
# ``aliases`` are the spoken names. Chinese names need their common
# mis-hearings listed too: the recogniser splits "小赫" oddly sometimes.
# All ASCII aliases must be lowercase -- ``intent.normalize`` lowercases the
# input before matching.
#
# ``intent.fold`` additionally collapses known homophones, so a name that
# was never listed still matches when the recogniser picks a different
# character for the same syllable (小赫 -> 小鹤 / 小贺). The list below
# therefore only needs the names the user actually says; the folded table
# is the safety net.
#
# ``avatar`` selects the on-screen character; it must match a character
# delivered under ``design/``.
#
# ``design`` is the folder under ``design/avatars/`` that holds that
# character's pictures. It is not always the same as the assistant id: the
# DeepSeek character lives in ``deepseek/`` while the assistant is called
# ``dsh``. The web console reads this folder to show the faces.

REGISTRY = {
    "hermes": {
        "name": "小赫",
        "enabled": True,
        "backend": "hermes",
        "avatar": "hermes",
        "design": "hermes",
        "aliases": [
            "hermes", "小赫", "小赫默斯", "小赫墨斯", "赫尔墨斯", "赫墨斯", "小和",
        ],
    },
    # The GPT assistant. It drives the Codex CLI on this computer, so the
    # speaker can hand a coding task to the desktop agent.
    "codex": {
        "name": "GPT",
        "enabled": True,
        "backend": "codex",
        "avatar": "gpt",
        "design": "chatgpt",
        "aliases": ["codex", "gpt", "chatgpt", "老寇", "老扣", "老蔻", "老k"],
    },
    "dsh": {
        "name": "大肥鱼",
        "enabled": True,
        "backend": "dsh",
        "avatar": "dsh",
        "design": "deepseek",
        "aliases": ["dsh", "deepseek", "大肥鱼", "肥鱼", "大非鱼", "大肥余", "飞鱼"],
    },
    "workbuddy": {
        "name": "巴蒂",
        "enabled": True,
        # The model is served by WorkBuddy and reached with a WorkBuddy
        # authorisation that the user obtains by signing in to their
        # WorkBuddy account in a browser. The credential lives in a
        # separate file, workbuddy-auth.json. The host reads it; the
        # console writes it after a successful sign-in.
        "connection": True,
        "backend": "workbuddy",
        "avatar": "workbuddy",
        "design": "workbuddy",
        "aliases": ["workbuddy", "buddy", "巴蒂", "巴帝", "把弟", "沃克巴迪"],
    },
    # 猫娘: the companion, not a task runner. Its brain (memory, persona,
    # plugins, proactive chat) lives in the local N.E.K.O. runtime on the
    # Mac; the speaker is only a window onto it. The artwork is Codex's
    # approved v10 under design/avatars/neko, same layout as the others.
    "neko": {
        "name": "猫娘",
        "enabled": True,
        "backend": "neko",
        "avatar": "neko",
        "design": "neko",
        "aliases": ["猫娘", "neko", "猫娘酱"],
    },
    # 小家 (the sixth assistant): HA's own conversation agent, wired up as
    # a normal assistant so "喊小家" hands every turn to HA. The connection
    # (base_url/token/agent_id) is shared with the homeassistant section via
    # "config_section"; artwork under design/avatars/xiaojia is installed by
    # the design agent and is optional -- no face means the native eyes show.
    "xiaojia": {
        "name": "小家",
        "enabled": True,
        # backend id must equal the assistant id: DirectBackend.inner()
        # calls load_backend(assistant_id) when 若琪 reuses an assistant.
        "backend": "xiaojia",
        "avatar": "xiaojia",
        "design": "xiaojia",
        "config_section": "homeassistant",
        "aliases": ["小家", "小佳", "小嘉", "小加"],
    },
    # 克劳德 (seventh assistant): the Claude Code CLI installed on this
    # Mac. Artwork already lives under design/avatars/claude (installed by
    # the design agent); no assets are added by the host code.
    "claude": {
        "name": "克劳德",
        "enabled": True,
        "backend": "claude",
        "avatar": "claude",
        "design": "claude",
        "aliases": ["claude", "cloud", "克劳德", "克劳得", "柯劳德",
                    "克老德", "克劳的", "刻劳德"],
    },
}

# Which assistant answers when the user has not picked one. This is only
# a default for the registry; it is never used as a fallback for the
# "no assistant selected" state, which the device now leaves to the native
# factory Rokid eyes (the old idle ball was removed on 2026-11-12).
DEFAULT_AGENT = "hermes"

# Fallback avatar for an assistant that does not name one. The idle ball was
# removed on 2026-11-12, so the safe default is "none" — the device renders
# the native factory Rokid eyes whenever no character is defined.
DEFAULT_AVATAR = "none"


def enabled_agents():
    return [key for key, item in REGISTRY.items() if item.get("enabled")]


def agent_name(agent_id):
    if agent_id == "direct":
        return "直连"
    item = REGISTRY.get(agent_id)
    return item["name"] if item else "助手"


def agent_avatar(agent_id):
    item = REGISTRY.get(agent_id)
    return item.get("avatar", DEFAULT_AVATAR) if item else DEFAULT_AVATAR


def agent_design_dir(agent_id):
    """The folder under ``design/avatars/`` holding this assistant's art.

    It is not always the same as the assistant id: the DeepSeek character
    lives in ``deepseek/`` while the assistant is called ``dsh``. The web
    console uses this to show the faces Codex drew.
    """
    item = REGISTRY.get(agent_id)
    if not item:
        return ""
    return item.get("design") or item.get("avatar") or ""


# The eight states an assistant's art comes in. The device draws them; the
# web console shows the matching picture so both agree on what the assistant
# is doing right now.
AVATAR_STATES = (
    "idle", "listening", "thinking", "working",
    "speaking", "confirmation", "done", "error",
)


# --- config file ------------------------------------------------------------

DEFAULTS = {
    # Bind every interface: the speaker reaches the host over the LAN, while
    # the ASR watcher on this machine uses 127.0.0.1. Access is protected by
    # the bearer token, not by the bind address.
    "host": "0.0.0.0",
    "port": 8765,
    "token": "",
    "state_file": "bridge-state.json",
    "max_queue": 8,
    "speech_chars": 400,
    # The speaker's address, used to restart its TTS engine when it wedges.
    # Empty disables the automatic recovery.
    "device": "",
    "tts_recovery": False,
    # The factory speaks a canned notice when one of its cloud content
    # services has expired ("免费的内容服务已经到期了，请在若琪APP续费").
    # If the microphone hears our own speaker saying it, the ASR can
    # forward it as a user turn; the assistant then answers the notice,
    # which makes the speaker say it again -- a loop. This switch drops
    # only that one family of notices from the ASR stream. It does NOT
    # silence any other factory speech (alarms, weather, clock, ...), and
    # it cannot stop the factory from playing the clip in the first place:
    # that is the state of the Rokid account's content service.
    "suppress_factory_notice": True,
    "app_store": {"installed": {}},
    "bambu_print": {"host": "", "serial": "", "access_code": ""},
    "miniapps": {
        # Query apps read local data and send one parameterised card to the
        # speaker; no images are rendered on the Mac. Empty means "find the
        # Codex CLI on PATH, then fall back to ~/.local/bin/codex".
        "codex_executable": "",
        # How long a card stays on the Rokid screen (milliseconds).
        # User decision 2026-11-15: 30 seconds, display only (no TTS).
        "card_ttl_ms": 30000,
        # D040: full scale of the network-speed arcs, in MB/s (MiB/s).
        # 12.8 MB/s download therefore fills 64% of the green ring.
        "network_full_scale_mbps": 20.0,
        # 2026-11-16: console-defined spoken phrases, one list per app:
        # {"network_speed": ["看看宽带", "网速快不快"]}. Saved from
        # 设置 · 小应用 and effective immediately (no host restart).
        "voice_phrases": {},
    },
    "hermes": {
        "command": ["hermes", "chat", "--oneshot", "--format", "stream-json"],
        "cwd": "workspace",
        "timeout_seconds": 300,
        "keep_session": True,
        "safe_mode": False,
        # The model this assistant answers with. Empty means "whatever
        # Hermes is configured to use". A name set here is passed to the
        # CLI as --model, so it takes effect on the next turn.
        "model": "",
    },
    "dsh": {
        # The launcher under ~/.local/bin is self-contained; the backend
        # prefers it over PATH, where a managed-node copy may live.
        "command": ["dsh", "headless", "--json"],
        "cwd": "workspace",
        "timeout_seconds": 240,
        "keep_session": True,
        # Keep the upstream workspace sandbox for this assistant.
        "permission_mode": "workspace-write",
        # The model this assistant answers with, and the provider that
        # serves it. Empty means "whatever the profile is configured to
        # use". A name set here is applied through a patch file, because
        # the headless profile has no command line flag for it.
        "model": "",
        "model_provider": "",
    },
    # WorkBuddy assistant (巴蒂). The token and the endpoint are NOT kept
    # here -- they live in the dedicated, non-committed workbuddy-auth.json
    # so the secret never reaches the shared config or the APK. These are
    # only fallback defaults for when that file is absent.
    "workbuddy": {
        "timeout_seconds": 120,
        "keep_session": True,
        # The model gateway. Empty means the default one WorkBuddy serves
        # the domestic account from; the browser sign-in decides which
        # region the credential belongs to.
        "base_url": "",
        "model": "",
    },
    # The GPT assistant (Codex). The CLI is driven in non-interactive mode
    # and keeps a thread between turns, so a follow-up question keeps its
    # context.
    "codex": {
        "command": ["codex", "exec", "--json"],
        "cwd": "workspace",
        # Codex runs shell commands of its own, so it needs more headroom
        # than a plain chat turn.
        "timeout_seconds": 600,
        "keep_session": True,
        # The model this assistant answers with. Empty means "whatever
        # ~/.codex/config.toml says". A name set here is passed to the CLI
        # as --model, and it only takes effect on a new thread.
        "model": "",
        # The CLI refuses to start in a directory it does not consider a
        # safe repository unless the caller opts out. The host decides
        # that, not the speaker.
        "skip_git_repo_check": True,
    },
    # Direct mode: the speaker answers questions itself instead of only
    # relaying to a selected assistant.
    #
    #   mode     off     keep the factory behaviour: with no assistant
    #                   selected the speaker stays silent, the factory
    #                   answers on its own
    #            hybrid  the factory keeps the skills it owns (clock,
    #                   alarms, system switches); everything else
    #                   goes to the model. Playback controls, song
    #                   requests AND volume are deliberately NOT here:
    #                   the bridge owns STREAM_MUSIC and its own local /
    #                   online library, so those turns must reach the
    #                   host. See intent.music_control, music_request
    #                   and music_volume. Weather is not here either:
    #                   the factory cloud only answers it with the
    #                   service-expiry notice, so weather.py takes the
    #                   turn and opens the native card itself.
    #            full    every sentence goes to the model
    #
    #   source   assistant  reuse an existing assistant's model and tools,
    #                       with a session of its own so the assistant's
    #                       own conversation is not disturbed
    #            custom     call an OpenAI-compatible endpoint directly
    "direct": {
        "mode": "hybrid",
        "source": "assistant",
        "assistant": "hermes",
        # The turns the factory handles on its own: clock, date, alarms,
        # timers, system switches. The rule lives here so the host and
        # the device cannot disagree about who answers a turn; the
        # device gets it from the status endpoint.
        #
        # Music is entirely ours now: playback controls, song requests
        # and volume. A bare "播放" also matched "继续播放"/"停止播放",
        # and "音量/调到" belonged to the factory's own (empty) player;
        # all of them must reach the host instead. Weather left the list
        # with the skill itself: see the "weather" section below.
        "factory_owns":
            r"(几点|点啦|报时|报个时间|什么时间|现在时间|今天.*(号|日|几)|日期|星期[几一二三四五六日天]|周[几一二三四五六日天]"
            r"|闹钟|提醒我|叫我|计时|倒计时"
            r"|关机|重启|飞行模式|wifi|无线网|蓝牙|亮度|连网)",
        "custom": {
            "base_url": "",
            "api_key": "",
            "model": "",
            "timeout_seconds": 60,
        },
    },
    # The weather skill, rebuilt around the retired Rokid backend. The
    # native weather app is kept (card, animation, factory voice) and fed
    # by a local HTTPS replacement for weather-api.rokid.com; weather.py
    # opens the app over ADB when a turn asks about the weather.
    #
    #   enabled   the 覆盖原厂天气查询 switch (console · 设置 · 小应用).
    #             ON: weather.py intercepts weather turns and the factory
    #             rule carries no weather words. OFF: the turns go back
    #             to the factory -- engine.factory_pattern() rejoins the
    #             old weather words at runtime, so the speaker answers
    #             them (with its expiry notice, these days) again. One
    #             console click flips it, persists it and starts / stops
    #             the replacement API, with no host restart.
    #   port      the LAN port the replacement API listens on (TLS).
    #             scripts/weather_device_setup.py points the device's
    #             copy of weather-api.rokid.com at this host and port.
    #   city /    the console's 城市 field (D049). Once set it outranks
    #   latitude /  the location the device reports -- the user asked for
    #   longitude  that place. Empty = follow the device as before. The
    #             lat/lon fields are manual overrides (no console input).
    #   api_url / the console's data-source URLs (D049), read live by the
    #   wttr_url  API server with every query: 主源 must be Open-Meteo
    #             shaped (current_weather + daily), 备源 wttr.in shaped
    #             (?format=j1). Empty = the built-in sources. Geocoding
    #             a plain city name still goes through Open-Meteo's
    #             geocoder whatever the 主源 URL is.
    #   ttl_seconds  how long one Open-Meteo answer is reused.
    #   voice_phrases  the card's custom spoken phrases from the console
    #             (same normalisation as the mini-apps; empty = built-in
    #             keywords only). Saved via /api/weather/voice.
    "weather": {
        "enabled": False,
        "port": 8799,
        "city": "",
        "latitude": "",
        "longitude": "",
        "api_url": "",
        "wttr_url": "",
        "ttl_seconds": 300,
        "voice_phrases": [],
    },
    # The standby clock card (设置 · 小应用 · 待机时钟). The clock app
    # itself lives on the device (app/clock/, occupying the type-4 idle
    # slot); this section only stores what the console card shows.
    #
    #   face         the face last chosen from the console (grid /
    #                digital / any sdcard face). The device's
    #                own /sdcard/RokidClock/config.json stays the source
    #                of truth -- this field is the console's memory.
    #   faces        the faces the card offers. Built-in names always
    #                work; sdcard faces come from the device directory.
    #   voice_phrases  the card's custom spoken phrases (same
    #                normalisation as the mini-apps; empty = built-in
    #                keywords only). Saved via /api/clock/voice.
    "clock": {
        "face": "grid",
        "faces": ["grid", "digital", "orbit", "weekday", "minimal", "lagoon", "milk", "forest", "roman"],
        "voice_phrases": [],
    },
    # Wake words: the local wake-word feature. The factory could change
    # the wake word through its cloud, which is gone; the plan is to
    # register extra words on the device itself through the openvoice
    # addVTWord interface, one per assistant, so saying the word wakes
    # straight into that assistant.
    #
    #   enabled  the master switch. Nothing is registered while it is
    #            off, and "若琪" keeps working as before either way.
    #   agents   per-assistant words. "pinyin" is the word spelled in
    #            tone-marked pinyin, the format the engine takes
    #            (da4fei2yu2 = 大肥鱼). "enabled" turns one word on or
    #            off without losing its spelling. "aliases" are the
    #            other ways the cloud recogniser writes the name --
    #            they are registered as one pipe-separated vt_word so
    #            that a mishearing still passes the cloud's activation
    #            check ("巴蒂" heard as "巴地" no longer counts as a
    #            false trigger). Add a spelling here when the log shows
    #            a new misrecognition; the name itself always comes
    #            first in the registered string.
    "wake": {
        "enabled": False,
        "agents": {
            "dsh": {"enabled": True, "pinyin": "da4fei2yu2",
                    "aliases": ["大飞鱼", "大肥余", "飞鱼", "肥鱼"]},
            # The cloud wrote 巴蒂 as "八八" on 2026-11-15
            # ("八八控制电脑吗"). No alias matched, so the cloud judged the
            # wake fake and cut the microphone after 0.7s. "八弟" is the
            # same two-syllable sound. Both are two-character words, not
            # the bare common character "八".
            "workbuddy": {"enabled": True, "pinyin": "ba1di4",
                          "aliases": ["巴帝", "把弟", "巴地", "八地",
                                      "巴迪", "八八", "八弟",
                                      "body", "bady"]},
            # The cloud writes a spoken "GPT" as "gpt", "g p t" and even
            # "c p t" (G heard as C). Every one of those must be in
            # vt_word, or the second-stage check calls the turn
            # 'activ fake' and closes the microphone after 0.7s.
            # Trimming this table to a single alias (75049e5) was tried
            # to curb false wakes, but acoustic hits are decided by
            # vt_pinyin alone -- this table cannot affect them at all.
            "codex": {"enabled": True, "pinyin": "ji4pi4ti4",
                      "aliases": ["gpt", "计屁替", "cpt",
                                  "g p t", "c p t"]},
            "hermes": {"enabled": True, "pinyin": "xiao3he4",
                       "aliases": ["小赫", "小鹤", "小贺", "小荷", "小褐"]},
            # 小家: the display name is the first vt_word entry, so the
            # table below only needs the ASR mishearings. "小佳/小嘉/小加"
            # are the same two syllables written differently by the cloud.
            "xiaojia": {"enabled": True, "pinyin": "xiao3jia1",
                        "aliases": ["小家", "小佳", "小嘉", "小加"]},
            # A two-syllable name is easy for the cloud to mishear, so the
            # alias table follows the same rule as the other four: only
            # plausible mishearings, never common single characters.
            "neko": {"enabled": True, "pinyin": "mao1niang2",
                     "aliases": ["毛娘", "猫娘酱", "neko"]},
            # 克劳德 (ke4lao2de3): the display name is always the first
            # vt_word entry, so only the mishearings go here. "柯/科/刻"
            # for 克, "老" for 劳, "得/的" for 德, plus the English
            # "claude" (often written "cloud" by the recogniser).
            "claude": {"enabled": True, "pinyin": "ke4lao2de3",
                       "aliases": ["柯劳德", "科劳德", "刻劳德", "克劳得",
                                   "克老德", "克劳的", "claude", "cloud"]},
        },
    },
    # Where the N.E.K.O. runtime lives and which character to talk to.
    # "auto" follows GET /api/characters/current_catgirl, so renaming the
    # catgirl in the Mac client never breaks the speaker. Character names
    # are case-sensitive ("YUI", not "Yui") and are also the memory folder.
    # Override in config.local.json ("neko" or "agents"."neko").
    "neko": {
        "url": "ws://127.0.0.1:48911",
        "character": "auto",
        "timeout": 60,
        # "Ear only": the speaker is just N.E.K.O.'s microphone and screen.
        # The Mac reads the catgirl's answers out loud instead. Voice input
        # and the on-screen text are untouched. While on, the host also
        # stops feeding the speaker unsolicited proactive prompts -- the Mac
        # side starts those conversations itself.
        #
        # Why the Mac and not the desktop client: the upstream server
        # delivers a turn's text and TTS audio only to the WS connection
        # that started the turn, and that is the host's bridge connection
        # (docs/research/neko-ear-mode-assessment.md).
        "ear_only": False,
        # The Mac's mouth for the mode above. ``say`` is the local
        # synthesiser; the catgirl's own timbre would need the upstream
        # audio frames and is a later phase. ``dodge`` rewrites registered
        # wake words out of the line before it is spoken, because the
        # speaker's microphone hears the Mac's speakers and would otherwise
        # wake itself up mid-answer. ``dodge_extra`` adds words that are not
        # in our table -- the factory's own name is the important one: the
        # speaker would answer that wake itself, and the host never sees the
        # turn. Clear the list if the wording matters more than the flash.
        "mac_voice": {
            "enabled": True,
            "voice": "Tingting",
            "rate": 180,
            "dodge": True,
            "dodge_with": "人家",
            "dodge_extra": ["若琪", "洛奇"],
        },
        # Relay mode: the desktop client owns the character's single
        # /ws/{character} socket. Upstream is "newest socket wins", so a
        # second socket from the host closes the desktop window with
        # CHARACTER_SWITCHING_TERMINAL and its chat history stops syncing.
        # With relay on, the host POSTs each speaker line to the locally
        # installed rokid_bridge N.E.K.O. plugin; the plugin calls
        # ctx.push_message(...) inside the upstream process so the desktop
        # client starts the turn itself. That gives one shared chat history,
        # desktop-native TTS, and no socket contention. The host stays a
        # microphone and a subtitle strip (no device TTS, no local say).
        # Off by default -- the direct WebSocket path is the fallback.
        # See docs/research/neko-ws-conflict-assessment.md.
        "relay": {
            "enabled": False,
            "url": "http://127.0.0.1:48917",
            "token": "",
            "timeout": 60,
            "poll_interval": 0.6,
        },
        # Proactive chat (B plan, route R3). The N.E.K.O. server pushes its
        # own proactive stream only to the desktop client's WS connection,
        # so the host asks the backend to start a short turn instead.
        # "24:00" is accepted as midnight; a quiet window that crosses
        # midnight (e.g. 23:00-08:00) works too.
        "proactive": {
            "enabled": True,
            "interval_minutes": 10,
            "quiet_start": "00:00",
            "quiet_end": "11:00",
            "idle_seconds": 120,
        },
    },
    # Per-assistant overrides, e.g. {"hermes": {"cwd": "/Users/me/project"}}.
    "agents": {},
    # Home Assistant (功能三): the house's devices through HA's own
    # conversation agent. 03-home-assistant.md / research doc 2026-11-17.
    # The token is a long-lived access token from HA's profile page and
    # lives ONLY in config.local.json (gitignored, 0600) -- never in git,
    # never in an API response, never in the APK.
    "homeassistant": {
        # Master switch. Off = the feature is invisible and the factory
        # keeps every turn it used to keep.
        "enabled": False,
        # e.g. "http://192.0.2.4:8123", no trailing path.
        "base_url": "",
        "token": "",
        # Conversation API language; zh-CN support depends on the HA
        # agent actually installed -- first test decides, not this default.
        "language": "zh-CN",
        # Empty = whatever the HA instance's default conversation agent is.
        "agent_id": "",
        # Conversation requests are slow LLM calls; reads are fast.
        "request_timeout_seconds": 15,
        # Control results get a short TTS confirmation (D034 kept query
        # apps display-only; this switch covers HA only).
        "speak_results": True,
        # HA feedback cards are spoken (both 若琪 and 小家), so they only
        # need 8 s on screen. Mini-app query cards above keep their 30 s.
        "card_ttl_ms": 8000,
        # Up to 4 entities shown on the family-overview card.
        "overview_entities": [],
        # entity_id -> extra spoken names ("书房屋|书屋灯"), the ASR-variant
        # fold table for the voice gate (batch 2). Friendly names are
        # implicit aliases and need not be repeated here.
        "aliases": {},
        # Fixed scene/script shortcuts: phrase -> {domain, service,
        # entity_id, data} (batch 4).
        "shortcuts": [],
    },
    # 小家 (sixth assistant): its brain is the HA conversation agent above,
    # so all connection settings are shared through REGISTRY.config_section.
    # This section only holds assistant-level choices for the model menu.
    "xiaojia": {
        # Empty = the HA instance's default conversation agent.
        "model": "",
        # Keep conversation_id across turns so "再试一次" works.
        "keep_session": True,
    },
    # 克劳德 (seventh assistant): Claude Code CLI, driven headlessly with
    # -p --output-format json. A session id we generate ourselves keeps
    # follow-up turns in context without touching the user's own terminal
    # sessions. The launcher under ~/.local/bin is self-contained, so the
    # host finds it even when its PATH has no npm global bin directory.
    "claude": {
        "command": ["claude", "-p", "--output-format", "json"],
        "cwd": "workspace",
        "timeout_seconds": 300,
        "keep_session": True,
        # Claude Code model aliases; empty = whatever the account defaults to.
        "model": "",
        # Headless turns have no approval channel. "acceptEdits" lets it
        # read/edit files but still refuses shell commands; raise this to
        # "bypassPermissions" only if the speaker should run commands too.
        "permission_mode": "acceptEdits",
        "session_key": "claude_session",
    },
}

# Assistants that have their own settings section in the config file.
BACKEND_SECTIONS = ("hermes", "codex", "dsh", "workbuddy", "neko",
                    "xiaojia", "claude")


def atomic_json(path, data):
    """Write JSON so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        os.chmod(temp, 0o600)
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temp, path)


def fill_missing(config, defaults):
    """Fill in what a config is missing, deep by deep.

    The nested parts are copied, not shared: a setting changed at runtime
    must not reach back into DEFAULTS and change it for every other
    configuration in the process.
    """
    for key, value in defaults.items():
        if isinstance(value, dict):
            current = config.get(key)
            if not isinstance(current, dict):
                current = {}
                config[key] = current
            fill_missing(current, value)
        else:
            config.setdefault(key, copy.deepcopy(value))


def create_config(path):
    """Create a fresh config. Refuses to overwrite an existing one."""
    path = Path(path)
    if path.exists():
        raise FileExistsError("配置已存在，保留原文件")
    config = copy.deepcopy(DEFAULTS)
    config["token"] = secrets.token_urlsafe(32)
    config["workspace"] = str(path.resolve().parent / "workspace")
    Path(config["workspace"]).mkdir(parents=True, exist_ok=True)
    atomic_json(path, config)
    return config


def load_config(path):
    """Read the config, filling in anything a newer file is missing."""
    path = Path(path)
    if not path.exists():
        return create_config(path)
    config = json.loads(path.read_text(encoding="utf-8"))
    # Where the file lives, so a setting changed at runtime can be written
    # back to it.
    config["_path"] = str(path.resolve())
    for key, value in DEFAULTS.items():
        if isinstance(value, dict):
            if not isinstance(config.get(key), dict):
                config[key] = {}
            fill_missing(config[key], value)
        else:
            config.setdefault(key, copy.deepcopy(value))
    if not config.get("token"):
        config["token"] = secrets.token_urlsafe(32)
        atomic_json(path, config)
    return config


def agent_settings(config, agent_id):
    """The settings for one assistant.

    Each backend has its own section in the config file: the two CLIs take
    different commands and different options, so one section must not be
    handed to the other. A section that is missing falls back to the
    built-in defaults for that assistant, so a new one works before it has
    been configured.
    """
    section = dict(config.get(agent_id) or {})
    if not section:
        section = dict(DEFAULTS.get(agent_id) or {})
    override = (config.get("agents") or {}).get(agent_id) or {}
    section.update(override)
    # The backend needs to know where the config lives so it can write its
    # log next to the runtime files.
    section["_root"] = config.get("_root") or ""
    return section


def resolve_cwd(config, cwd_value):
    """Resolve a working directory against the config file's folder."""
    root = Path(config.get("_root") or ".").resolve()
    candidate = Path(str(cwd_value)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    return candidate

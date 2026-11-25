#!/usr/bin/env python3
"""Host HTTP service for the Rokid bridge.

Standard library only. The device posts events and polls commands; the
browser console reads the same state.

Routes
    GET  /api/status         current state and assistant list
    GET  /api/commands       commands after ?after=<seq>
    GET  /api/transcript     conversation, ?after=<seq> for the new part
    GET  /api/threads        the same conversation, split up per assistant
    GET  /api/model          models of one assistant
    GET  /api/direct         the direct configuration
    GET  /api/gemini/settings  Gemini model and local CLI availability
    GET  /api/agent-auth     authorisation status of a connected assistant
    GET  /api/agent-auth/start  start a browser sign-in, answers the login URL
    GET  /avatars/...        the assistant pictures
    POST /api/event          one device event
    POST /api/text           a typed turn (console)
    POST /api/chat           a typed line for a chosen assistant
    POST /api/agent          select an assistant
    POST /api/model          change the model of an assistant
    POST /api/direct         change the direct configuration
    POST /api/gemini/settings  save the Gemini model
    POST /api/gemini/test    check the local Antigravity login
    POST /api/agent-auth     save a connected assistant's authorisation
    POST /api/agent-auth/poll  check whether the browser sign-in finished
    POST /api/approvals      resolve a pending approval
    GET  /api/neko/proactive proactive-chat settings
    POST /api/neko/proactive change settings, or {"now": true} to speak once
    GET  /api/factory-notice  expiry-notice suppression switch
    POST /api/factory-notice  {"enabled": bool} flip the switch
    GET  /api/neko/ear-only   catgirl ear-only (speaker = microphone) switch
    GET  /api/neko/relay      catgirl relay (desktop owns the socket) switch
    POST /api/neko/relay      {"enabled": bool} flip the switch
    POST /api/neko/ear-only   {"enabled": bool} flip the switch
    GET  /api/audio/<id>     one cached clip for the device's MediaPlayer
    GET  /api/art/<id>       artwork for the device's now-playing view
    GET  /api/music/library  one page of built-in + local tracks (?q&offset&limit)
    GET  /api/music/folders  all configured music folders + index state
    GET  /api/music/folder   first folder (legacy single-folder callers)
    GET  /api/music/search   search N.E.K.O.'s music provider
    GET  /api/music/search-local  one page of matching local tracks
    GET  /api/music/browse   one level of folders on this computer
    GET  /api/music/netease  Netease Cloud Music account status
    POST /api/music/pick-folder   native macOS folder chooser
    POST /api/music/netease/login  save a Netease MUSIC_U cookie
    POST /api/music/netease/logout clear the Netease cookie
    GET  /api/music/qqmusic  QQ Music account status
    POST /api/music/qqmusic/login  save a QQ Music cookie
    POST /api/music/qqmusic/logout clear the QQ Music cookie
    GET  /api/music/qqmusic/search  search QQ Music via N.E.K.O.
    POST /api/music/play     queue a clip on the speaker
    POST /api/music/stop     stop the speaker's MediaPlayer
    POST /api/music/folders  replace the list of music folders
    POST /api/music/folder   choose one music folder (legacy)
    POST /api/music/reindex  rescan every music folder
    POST /api/music/shuffle  toggle catgirl shuffle / {"next": true}
    POST /api/music/control  {"action": pause|resume|next|prev|stop}
    POST /api/music/parse    {"text"} dry-run of the spoken song parser
    POST /api/music/volume   {"action": up|down|mute|unmute|set, "value"}
    GET  /api/miniapps        query-app metadata, built-in + custom voice
                              phrases, and current card state
    POST /api/miniapps/query  {app_id, operation, present, force_refresh}
    POST /api/miniapps/hide   close the current mini-app card
    POST /api/miniapps/voice  {app_id, phrases} save one card's custom
                              spoken phrases (effective immediately)
    GET  /api/weather         weather card: override switch + custom voice
    POST /api/weather/query   open the native weather card on the speaker
    POST /api/weather/override {"enabled": bool} flip 覆盖原厂天气查询
                              (also starts / stops the replacement API)
    POST /api/weather/voice   {"phrases": ...} save the card's custom
                              spoken phrases (effective immediately)
    POST /api/weather/settings {"city", "api_url", "wttr_url"} save the
                              card's city + source URLs (live, no restart)
    POST /api/weather/wiring  re-apply the device weather wiring (hosts/
                              DNAT/CA); the DNAT rule is RAM-only and is
                              lost on reboot, so this is the card's fix
    GET  /api/clock           standby clock card: current face + custom voice
    POST /api/clock/face      {"face": grid|digital|next|<sdcard>}
                              switch the device face (adb SET_FACE;
                              {"name": ...} accepted as an alias)
    POST /api/clock/voice     {"phrases": ...} save the card's custom
                              spoken phrases (effective immediately)
    GET  /api/roon/status    Roon Core/zone state from the local sidecar
    GET  /api/homeassistant  HA settings (token redacted) + last health check
    GET  /api/homeassistant/entities  entity listing for the alias editor (?q=)
    GET  /api/homeassistant/jobs  recent voice-command results
    POST /api/homeassistant/settings  save connection settings
    POST /api/homeassistant/test  read-only reachability / auth check
    POST /api/homeassistant/command  run one sentence through the gate + HA
    POST /api/roon/control   {"action": play|pause|resume|next|prev|stop|
                              volume_up|volume_down|volume_set|mute|unmute,
                              "value": optional}
    POST /api/roon/settings  {"target": auto|local|roon, "zone_id", "host",
                              "port"}
    POST /api/roon/power     {"enabled": bool} switch the whole Roon
                              integration on/off (persisted across restarts)
"""
from __future__ import annotations

import argparse
import collections
import hmac
import json
import os
import re
import secrets
import shutil
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from app_store import CATALOG as APP_CATALOG, preview_file
from miniapps.bambu import BambuError
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import config as config_module
import intent
import music as music_module
from audio import AudioCache
from config import AVATAR_STATES, REGISTRY, agent_name
from engine import Engine
from homeassistant import HaError
from music import MusicError, MusicProvider, shuffle_note
from netease import NeteaseError
from roon import RoonClient, SidecarPower

HERE = Path(__file__).resolve().parent

# The pictures live in the repository, not next to the host: the host sits
# in app/host/ and the artwork in design/avatars/. The path is worked out
# from the host's own location, so it does not depend on the working
# directory the host was started from.
REPO = HERE.parent.parent
AVATARS = Path(os.environ.get("ROKID_RESOURCES", REPO)) / "design" / "avatars"


def avatars_root():
    """Where the assistant pictures are."""
    return AVATARS


def available_art():
    """The characters that actually have pictures.

    Not every assistant has artwork yet: the console shows a letter
    instead of an empty circle for those, so it needs to know which ones
    have a picture to show.
    """
    root = avatars_root()
    try:
        return sorted(entry.name for entry in root.iterdir()
                      if entry.is_dir() and (entry / "avatar.png").exists())
    except OSError:
        return []


def load_config(path):
    path = Path(path)
    cfg = config_module.load_config(path)
    cfg["_root"] = str(path.resolve().parent)
    return cfg


def make_handler(engine, token, roon_power=None):
    # One cache and one music provider per host process. The cache lives in
    # runtime/, which is deliberately outside version control: clips are
    # transient downloads, not project assets. The folder can be redirected
    # through the config so the tests never touch the real one.
    runtime_root = Path(engine.config.get("audio_runtime")
                        or (HERE.parent / "runtime"))
    audio_cache = AudioCache(runtime_root / "audio")
    music_provider = MusicProvider(engine.config,
                                   local_root=runtime_root / "music",
                                   runtime_root=runtime_root)
    shuffle_recent = collections.deque(maxlen=30)
    shuffle_lock = threading.Lock()
    # Every track actually queued, in play order. "上一首" walks back one
    # entry; it is kept here because the device only ever plays one clip
    # at a time and has no playlist of its own.
    played_history = collections.deque(maxlen=32)
    history_lock = threading.Lock()

    def queue_track(item, note="", record=True):
        """Resolve, cache and queue one track on the speaker.

        Shared by the console's play button, shuffle mode and automatic
        continuation, so all three produce exactly the same command shape.
        Artwork or lyrics failing must never stop the song.
        """
        local_path = None
        transcoded = False
        transcode_error = ""
        if str(item.get("kind") or "") == "local":
            local_path = music_provider.resolve_local_path(item)
            title = str(item.get("name") or local_path.stem)
            suffix = local_path.suffix.lower()
            try:
                data, suffix = music_provider.prepare_local_clip(local_path)
            except MusicError as error:
                # A failed conversion must not cost the user the song: the
                # device can now take large clips, so stream the original.
                data, transcode_error = None, str(error)
            if data is None:
                clip = audio_cache.link(local_path, suffix, title)
            else:
                clip = audio_cache.put(data, suffix, title)
                transcoded = True
        else:
            data, suffix, title = music_provider.resolve(item)
            music_provider.validate(data, suffix)
            clip = audio_cache.put(data, suffix, title)
        # For a local file the tags inside the music file name the singer,
        # not the filename (which may start with "01." from a CD rip).
        # Read the tags once and share them with the cover/lyric lookup.
        tags = (music_provider.read_tags(local_path)
                if local_path is not None else None)
        meta = music_provider.metadata(item, local_path, tags)
        if local_path is not None:
            title = meta["title"]
        cover = ""
        try:
            art = music_provider.artwork(item, local_path, tags)
            if art:
                image, art_suffix = art
                cover = audio_cache.put(image, art_suffix, title)["url"]
        except (ValueError, OSError, MusicError):
            cover = ""
        lyrics = music_provider.lyrics(item, local_path, tags)
        engine.play_audio(clip["url"], title, artist=meta["artist"],
                          cover=cover, lyrics=lyrics, note=note)
        if record:
            with history_lock:
                played_history.append(dict(item))
        result = {"ok": True, "title": title, "bytes": clip["bytes"],
                  "state": "playing", "has_cover": bool(cover),
                  "lyric_lines": len(lyrics), "transcoded": transcoded}
        if transcode_error:
            result["transcode_error"] = transcode_error
        return result

    def queue_random():
        with shuffle_lock:
            exclude = list(shuffle_recent)
        item = music_provider.random_track(exclude)
        with shuffle_lock:
            shuffle_recent.append(item["id"])
        return queue_track(item, shuffle_note())

    def queue_previous():
        """Replay the track before the current one.

        The current clip is dropped from the tail, so pressing previous
        twice keeps walking backwards. Failure restores the tail instead
        of losing the user's place.
        """
        with history_lock:
            if len(played_history) < 2:
                return {"ok": False, "error": "没有上一首"}
            current = played_history.pop()
            item = played_history.pop()
        try:
            return queue_track(item)
        except Exception:
            with history_lock:
                played_history.append(item)
                played_history.append(current)
            raise

    def music_control_hook(action):
        return queue_previous() if action == "prev" else queue_random()

    # The engine recognises the words, but the library (and therefore
    # "next"/"prev") lives here. Voice and console buttons share this hook.
    engine.music_hook = music_control_hook

    def music_request_hook(request):
        """Play a spoken song request: local library first, Netease second.

        The index is a substring search, so candidates are scored before
        one is chosen; a zero score is not this song. Only a whole-phrase
        hit counts, which keeps "晴天" from playing "晴天雨".
        """
        if request.get("random"):
            try:
                result = queue_random()
                result["source"] = "local"
                return result
            except Exception as error:
                return {"ok": False, "error": str(error)}

        queries = [str(q) for q in (request.get("queries") or []) if q][:2]

        local = {}
        for query in queries:
            try:
                page = music_provider.search_local(query, limit=50)
            except Exception:
                continue
            for item in page.get("items") or []:
                local.setdefault(str(item.get("id") or ""), item)
        ranked = []
        for item in local.values():
            score = music_module.rank_local_track(item, request)
            if score > 0:
                ranked.append((score, len(str(item.get("name") or "")), item))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        for _, _, item in ranked[:3]:
            try:
                result = queue_track(item)
            except Exception:
                continue
            result["source"] = "local"
            return result

        online = {}
        online_errors = []
        for query in queries:
            try:
                for item in music_provider.search(query, limit=8):
                    online.setdefault(str(item.get("id") or ""), item)
            except Exception as error:
                online_errors.append(str(error))
            if len(online) >= 3:
                break
        ranked = []
        for item in online.values():
            score = music_module.rank_online_track(item, request)
            if score > 0:
                ranked.append((score, len(str(item.get("name") or "")), item))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        last_error = ""
        for _, _, item in ranked[:3]:
            try:
                result = queue_track(item)
            except Exception as error:
                last_error = str(error)
                continue
            result["source"] = "online"
            return result
        if last_error:
            return {"ok": False, "error": last_error}
        if online_errors:
            return {"ok": False, "error": online_errors[0]}
        return {"ok": False, "error": "没有找到这首歌"}

    engine.music_request_hook = music_request_hook

    # Roon lives in the Node sidecar (app/roon-bridge). Poll it and push a
    # compact "roon_now" command on changes: the speaker only draws a screen,
    # it never starts its own MediaPlayer for Roon audio.
    roon_client = RoonClient(
        base_url=engine.config.get("roon_bridge_url"),
        state_path=runtime_root / "roon-target.json")
    engine.roon_client = roon_client

    def roon_poll():
        last_key = None
        last_cover_key = None
        last_cover = ""
        last_sent = 0.0
        last_lyric_key = None
        while not engine.closed:
            try:
                if not roon_client.enabled:
                    # Switched off in the console: no sidecar requests, no
                    # Roon screen, no voice routing. Send one clear command
                    # if the speaker was still showing a Roon track.
                    if last_key is not None:
                        engine.emit("roon_now", active=False)
                        last_sent = 0.0
                    last_key = None
                    last_cover_key = None
                    last_cover = ""
                    last_lyric_key = None
                    time.sleep(1.2)
                    continue
                state = roon_client.status()
                playing = roon_client.now_playing(state)
                active = bool(
                    playing and state.get("stage") == "ready"
                    and playing["state"] in ("playing", "paused", "loading"))
                key = None
                if active:
                    key = (playing["zone_id"], playing["title"],
                           playing["artist"], playing["state"],
                           playing["image_key"])
                    if playing["image_key"] != last_cover_key:
                        last_cover = ""
                        pair = roon_client.image(playing["image_key"])
                        if pair:
                            data, content_type = pair
                            suffix = (".png" if "png" in content_type
                                      else ".jpg")
                            last_cover = audio_cache.put(
                                data, suffix, playing["title"])["url"]
                        last_cover_key = playing["image_key"]
                    now = time.time()
                    # A fresh seek position every 5 s keeps the device's
                    # local clock close without flooding the command queue.
                    if key != last_key or now - last_sent >= 5:
                        payload = {"active": True,
                                   "zone": playing["zone_name"],
                                   "title": playing["title"],
                                   "artist": playing["artist"],
                                   "album": playing["album"],
                                   "state": playing["state"],
                                   "seek_position": playing["seek_position"],
                                   "length": playing["length"],
                                   "cover": last_cover}
                        # Roon does not send lyrics. Find them once per
                        # track from our library / Netease; the device keeps
                        # the sheet between the 5-second seek refreshes.
                        # The library roots are part of the key: changing the
                        # folder in the console re-reads the *current* song
                        # instead of waiting for the next track change.
                        lyric_key = (playing["zone_id"], playing["title"],
                                     playing["artist"],
                                     tuple(music_provider.lyrics_roots()))
                        if lyric_key != last_lyric_key:
                            payload["lyrics"] = music_module.lyrics_for_track(
                                music_provider, playing["title"],
                                playing["artist"],
                                album=playing.get("album", ""))
                            last_lyric_key = lyric_key
                        engine.emit("roon_now", **payload)
                        last_sent = now
                elif last_key is not None:
                    engine.emit("roon_now", active=False)
                    last_sent = 0.0
                    last_lyric_key = None
                last_key = key
            except Exception:
                pass
            time.sleep(1.2)

    threading.Thread(target=roon_poll, name="roon-poll", daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        server_version = "RokidBridge/1.0"

        def log_message(self, fmt, *args):
            pass

        # -- helpers ----------------------------------------------------------

        def _auth_ok(self):
            header = self.headers.get("Authorization") or ""
            expected = "Bearer " + token
            if self._from_this_machine():
                return hmac.compare_digest(header, expected)
            path = urlparse(self.path).path
            device_route = ((self.command == "POST" and path == "/api/event") or
                            (self.command == "GET" and (path == "/api/commands" or
                             path.startswith(("/api/audio/", "/api/art/")))))
            credential = engine.config.get("device_token")
            return bool(device_route and credential and
                        hmac.compare_digest(header, "Bearer " + credential))

        def _from_this_machine(self):
            """Whether the request came from this computer.

            The console is opened as a plain address -- http://127.0.0.1:8765/console
            -- and a browser cannot attach an Authorization header to a page
            request. So the page needs a way to learn the token by itself.

            Handing it out is only safe for a request that came from this
            machine: the host also listens on the network, where the token
            must not be handed to whoever asks. A request that arrives over
            the loopback interface can only have been made by a program on
            this computer, which is where the token lives anyway.
            """
            try:
                address = self.client_address[0]
            except (AttributeError, IndexError):
                return False
            return address in ("127.0.0.1", "::1", "localhost")

        def _send(self, code, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {}

        # -- GET ---------------------------------------------------------------

        def do_GET(self):
            url = urlparse(self.path)
            query = parse_qs(url.query)

            # The console page and the pictures it shows are served
            # without the token. A browser cannot attach an Authorization
            # header to a page request, so requiring one here means the
            # console can never be opened: the page itself would answer
            # 401 before it could ask for the token. The page reads the
            # token from the address and sends it with every API call.
            # The data APIs stay behind the token.
            if url.path in ("/console", "/console.html", "/"):
                self._serve_console()
                return
            if url.path.startswith("/app-previews/"):
                name = url.path[len("/app-previews/"):]
                if name not in {preview_file(key) for key, _ in APP_CATALOG}:
                    self._send(404, {"error": "not found"})
                    return
                body = (Path(os.environ.get("ROKID_WEB", HERE / "web")) / "app-previews" / name).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml" if name.endswith(".svg") else "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body)
                return
            if url.path.startswith("/avatars/"):
                self._serve_avatar(url.path[len("/avatars/"):])
                return

            # The console page is opened as a plain address and a browser
            # cannot attach an Authorization header to a page request. This
            # endpoint gives the page the token it needs, but only to a
            # request that came from this computer -- the host also listens
            # on the network, and there the token must not be handed out.
            if url.path == "/console-token":
                if (not self._from_this_machine() or
                        self.headers.get("Host", "").split(":")[0] not in ("127.0.0.1", "localhost")):
                    self._send(403, {"error": "only this computer may ask"})
                    return
                self._send(200, {"token": token})
                return

            if not self._auth_ok():
                self._send(401, {"error": "unauthorized"})
                return

            if url.path == "/api/app-store":
                self._send(200, engine.app_store.status())
                return
            if url.path == "/api/bambu-print":
                self._send(200, engine.miniapps.printer.settings())
                return
            if url.path == "/api/miniapps":
                self._send(200, engine.miniapps.status())
                return
            if url.path == "/api/weather":
                # The console card (设置 · 小应用 · 天气查询).
                self._send(200, engine.weather.status())
                return
            if url.path == "/api/clock":
                # The console card (设置 · 小应用 · 待机时钟).
                self._send(200, engine.clock.status())
                return
            if url.path == "/api/roon/status":
                self._send(200, roon_client.status())
                return
            if url.path == "/api/homeassistant":
                # Settings + the cached result of the last health check.
                self._send(200, {
                    "settings": engine.homeassistant.settings(),
                    "last_test": engine.homeassistant.last_test(),
                })
                return
            if url.path == "/api/homeassistant/entities":
                # Read-only entity listing (alias editor). ?q= filters.
                # An unreachable HA is an empty list plus a message, not a
                # 500: the console renders the message inside the pane.
                try:
                    rows = engine.homeassistant.entities(
                        query.get("q", [""])[0])
                except HaError as error:
                    self._send(200, {"entities": [],
                                     "error": error.message})
                    return
                except (ValueError, TypeError) as error:
                    self._send(400, {"error": str(error)})
                    return
                self._send(200, {"entities": rows})
                return
            if url.path == "/api/homeassistant/jobs":
                self._send(200, {"jobs": engine.homeassistant.jobs()})
                return
            if url.path.startswith("/api/audio/"):
                self._serve_media(url.path[len("/api/audio/"):])
                return
            if url.path.startswith("/api/art/"):
                self._serve_media(url.path[len("/api/art/"):])
                return
            if url.path == "/api/music/library":
                try:
                    page = music_provider.library_page(
                        query.get("q", [""])[0],
                        query.get("offset", ["0"])[0],
                        query.get("limit", ["50"])[0])
                except (ValueError, TypeError):
                    self._send(400, {"error": "bad paging"})
                    return
                self._send(200, page)
                return
            if url.path in ("/api/music/folder", "/api/music/folders"):
                self._send(200, {
                    "folder": music_provider.folders()[0],
                    "folders": music_provider.folders(),
                    "lyrics_roots": music_provider.lyrics_roots(),
                    "index": music_provider.index.status(),
                    "shuffle": music_provider.shuffle_enabled()})
                return
            if url.path == "/api/music/search":
                try:
                    items = music_provider.search(query.get("q", [""])[0])
                    self._send(200, {"items": items})
                except MusicError as error:
                    self._send(200, {"items": [], "error": str(error)})
                return
            if url.path == "/api/music/qqmusic/search":
                try:
                    items = music_provider.qqmusic_search(query.get("q", [""])[0])
                    self._send(200, {"items": items})
                except MusicError as error:
                    self._send(200, {"items": [], "error": str(error)})
                return
            if url.path == "/api/music/search-local":
                try:
                    result = music_provider.search_local(
                        query.get("q", [""])[0],
                        query.get("limit", ["30"])[0])
                except (ValueError, TypeError):
                    result = {"items": [], "total": 0, "has_more": False,
                              "error": "搜索参数不合法"}
                self._send(200, result)
                return
            if url.path == "/api/music/browse":
                try:
                    result = music_provider.browse(
                        query.get("path", [""])[0] or None)
                except MusicError as error:
                    result = {"ok": False, "error": str(error)}
                self._send(200, result)
                return
            if url.path == "/api/music/netease":
                self._send(200, music_provider.netease_status())
                return
            if url.path == "/api/music/qqmusic":
                self._send(200, music_provider.qqmusic_status())
                return
            if url.path == "/api/status":
                payload = engine.status()
                # Which characters have a picture to show. The console
                # falls back to a letter for the ones that have none.
                payload["art"] = available_art()
                self._send(200, payload)
                return
            if url.path == "/api/commands":
                after = query.get("after", ["-1"])[0]
                try:
                    after_value = int(after)
                except ValueError:
                    after_value = -1
                if after_value < 0:
                    # A device that just started asks for "latest": give it the
                    # current state without replaying the whole backlog.
                    #
                    # The overlay on the speaker is built from a display
                    # command, and a device that was restarted never sees
                    # the one that built it before. So the current state is
                    # sent again -- as a fresh command, not a replay of the
                    # queue, which would repeat old speech.
                    items = [engine.resync_display()]
                    # A device that reconnects inside the card's lifetime
                    # gets the still-valid card back, but never a replay of
                    # the spoken summary. Re-emit so it carries a fresh seq.
                    card = engine.miniapps.current_command()
                    if card:
                        items.append(engine.emit("miniapp_card", **card))
                    after_value = engine.status()["last_seq"]
                else:
                    items = engine.pending(after_value)
                payload = dict(engine.status())
                payload["commands"] = items
                payload["reset_required"] = False
                self._send(200, payload)
                return
            if url.path == "/api/transcript":
                after = query.get("after", ["-1"])[0]
                try:
                    after_value = int(after)
                except ValueError:
                    after_value = -1
                if after_value < 0:
                    # No cursor: send the recent conversation, the way the
                    # first version did. The instance id comes along, so
                    # the console can tell a restart from a quiet host.
                    self._send(200, {"items": list(engine.transcript)[-80:],
                                     "last_seq": engine.transcript_seq,
                                     "instance_id": engine.instance_id})
                    return
                self._send(200, engine.transcript_since(after_value))
                return
            if url.path == "/api/threads":
                # The conversation of each assistant, already separated.
                self._send(200, engine.threads())
                return
            if url.path == "/api/history":
                # One page of an assistant's earlier conversation. The
                # console asks for this when the user wants to see older
                # messages than the first load brought back.
                key = query.get("agent", [""])[0]
                try:
                    before = int(query.get("before", ["0"])[0])
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError:
                    self._send(400, {"error": "before and limit must be numbers"})
                    return
                limit = max(1, min(limit, 200))
                self._send(200, engine.history_before(key, before, limit))
                return
            if url.path == "/api/model":
                agent = query.get("agent", [""])[0]
                current, models = engine.agent_models(agent)
                self._send(200, {"agent": agent, "current": current,
                                 "models": models,
                                 # The model the last turn really ran on.
                                 # It differs from "current" when the
                                 # endpoint fell back to another model.
                                 "in_use": engine.agent_in_use_model(agent)})
                return
            if url.path == "/api/direct":
                self._send(200, engine.direct_status())
                return
            if url.path == "/api/gemini/settings":
                self._send(200, engine.gemini_status())
                return
            if url.path == "/api/wake":
                # The wake-word settings: the master switch and one word
                # per assistant, tone-marked pinyin.
                self._send(200, engine.wake_settings())
                return
            if url.path == "/api/wake/scan":
                # Which built-in assistants are present on this machine,
                # for the wake-word settings page to offer adding them.
                self._send(200, engine.detect_local_agents())
                return
            if url.path == "/api/neko/proactive":
                self._send(200, engine.neko_proactive_settings())
                return
            if url.path == "/api/neko/ear-only":
                # Ear-only: the speaker collects voice, the Mac speaks.
                self._send(200, engine.neko_ear_only_settings())
                return
            if url.path == "/api/neko/relay":
                # Relay: the desktop client owns the only WebSocket.
                self._send(200, engine.neko_relay_settings())
                return
            if url.path == "/api/factory-notice":
                # The 「其它」 page's switch: it only mutes the factory's
                # canned service-expiry notices, not other factory speech.
                self._send(200, engine.factory_notice_settings())
                return
            if url.path == "/api/agent-auth":
                agent = query.get("agent", [""])[0]
                if not agent:
                    self._send(400, {"error": "agent required"})
                    return
                if agent not in REGISTRY:
                    self._send(400, {"error": "unknown agent"})
                    return
                self._send(200, engine.agent_auth_status(agent))
                return
            if url.path == "/api/agent-auth/start":
                # Start a browser sign-in: the answer carries the URL the
                # user opens to log in to WorkBuddy.
                agent = query.get("agent", [""])[0]
                if not agent:
                    self._send(400, {"error": "agent required"})
                    return
                if agent not in REGISTRY:
                    self._send(400, {"error": "unknown agent"})
                    return
                region = query.get("region", [""])[0] or None
                try:
                    result = engine.agent_login_start(agent, region=region)
                except ValueError as error:
                    self._send(400, {"error": str(error)})
                    return
                self._send(200, result)
                return
            self._send(404, {"error": "not found"})

        def _serve_console(self):
            """Serve the single-page console."""
            page = Path(os.environ.get("ROKID_WEB", HERE / "web")) / "console.html"
            try:
                body = page.read_bytes()
            except OSError:
                self._send(404, {"error": "console not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # The console is a single page that changes with every commit.
            # A browser that kept an old copy would run old code against the
            # current host -- a blank page with no explanation. So the page
            # is never cached.
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.end_headers()
            self.wfile.write(body)

        def _serve_avatar(self, path):
            """Serve one of the assistant's pictures.

            The pictures live under ``design/avatars/<character>/`` in the
            repository. Two shapes are served:

            * ``<character>.png``          the neutral face
            * ``<character>-<state>.png``  one of the eight states

            Only those two shapes are served, and the state names are
            fixed by the contract, so the path cannot walk out of the
            folder.
            """
            name = path.strip("/")
            if not name or "/" in name or ".." in name or not name.endswith(".png"):
                self._send(404, {"error": "not found"})
                return
            stem = name[:-4]
            if "-" in stem:
                character, _, state = stem.rpartition("-")
                if not character or state not in AVATAR_STATES:
                    self._send(404, {"error": "not found"})
                    return
                source = Path(character) / "states" / (state + ".png")
            else:
                source = Path(stem) / "avatar.png"
            target = avatars_root() / source
            try:
                body = target.read_bytes()
            except OSError:
                self._send(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_media(self, name):
            """One cached clip or artwork, streamed from disk.

            Local library tracks are symlinks into the user's music folder,
            so a 60 MB file is copied in chunks instead of being read into
            the host's memory first.
            """
            target = audio_cache.path_for(name)
            if target is None:
                self._send(404, {"error": "not found"})
                return
            try:
                size = target.stat().st_size
            except OSError:
                self._send(404, {"error": "not found"})
                return
            self.send_response(200)
            self.send_header("Content-Type", audio_cache.content_type(name))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                with target.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, 256 * 1024)
            except (OSError, BrokenPipeError):
                pass  # the device went away mid-download

        # -- POST --------------------------------------------------------------

        def do_POST(self):
            if not self._auth_ok():
                self._send(401, {"error": "unauthorized"})
                return
            url = urlparse(self.path)
            body = self._read_json()

            try:
                if url.path == "/api/event":
                    was_playing = engine.audio_state == "playing"
                    result = engine.handle_event(body)
                    if (was_playing and result.get("intent") == "audio_ack"
                            and body.get("type") == "playback_done"
                            and music_provider.shuffle_enabled()):
                        # The catgirl's shuffle mode: one finished song is
                        # followed by another random pick. Run it off the
                        # request thread, since artwork/lyrics can take a
                        # few seconds to fetch. A user stop or an error must
                        # never continue the playlist.
                        def continue_shuffle():
                            try:
                                queue_random()
                            except Exception:
                                pass
                        threading.Thread(target=continue_shuffle,
                                         daemon=True).start()
                    self._send(200, result)
                    return
                if url.path == "/api/text":
                    request_id = body.get("request_id") or ("console-" + secrets.token_hex(8))
                    event = {"type": "text", "text": body.get("text", ""),
                             "request_id": request_id, "source": "console"}
                    self._send(200, engine.handle_event(event))
                    return
                if url.path == "/api/app-store/install":
                    try:
                        state = engine.app_store.set_installed(body.get("app_id"), body.get("installed"))
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    except Exception as error:
                        self._send(503, {"error": str(error)})
                        return
                    self._send(200, state)
                    return
                if url.path in ("/api/bambu-print", "/api/bambu-print/test"):
                    printer = engine.miniapps.printer
                    try:
                        state = (printer.test_connection() if url.path.endswith("/test")
                                 else printer.save(body))
                    except BambuError as error:
                        self._send(503, {"error": str(error)})
                        return
                    self._send(200, state)
                    return
                if url.path == "/api/miniapps/query":
                    try:
                        app_id = (body.get("app_id")
                                  or engine.miniapps.default_app_id)
                        operation = (body.get("operation")
                                     or engine.miniapps.default_operation(app_id))
                        result = engine.miniapps.query(
                            app_id=app_id,
                            operation=operation,
                            present=bool(body.get("present", True)),
                            force_refresh=bool(body.get("force_refresh", False)),
                            wants_today=bool(body.get("wants_today", False)),
                            request_id=(body.get("request_id")
                                        or "miniapp-" + secrets.token_hex(8)))
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result.to_dict())
                    return
                if url.path == "/api/miniapps/hide":
                    engine.miniapps.hide()
                    self._send(200, {"ok": True})
                    return
                if url.path == "/api/miniapps/voice":
                    # 控制台 · 设置 · 小应用: one card's custom spoken
                    # phrases. Empty list clears them. Saved to the config
                    # file and active at once -- no host restart.
                    try:
                        result = engine.miniapps.set_voice_phrases(
                            app_id=body.get("app_id") or "",
                            phrases=body.get("phrases") or [])
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/weather/query":
                    # The console card's 查询天气 button: it opens the
                    # native card on the speaker (the factory voice
                    # speaks there); this Mac stays silent.
                    self._send(200, engine.weather.query_now())
                    return
                if url.path == "/api/weather/override":
                    # 覆盖原厂天气查询: ON = weather.py owns weather
                    # turns, OFF = the factory's rule regains the words
                    # (engine.factory_pattern) and answers them again.
                    self._send(200, engine.weather.set_override(
                        enabled=body.get("enabled")))
                    return
                if url.path == "/api/weather/voice":
                    # The weather card's custom spoken phrases -- the
                    # same normalisation as the mini-apps. Empty clears.
                    self._send(200, engine.weather.set_voice_phrases(
                        body.get("phrases") or []))
                    return
                if url.path == "/api/weather/settings":
                    # The weather card's city and source URLs. An empty
                    # city follows the device location again; empty URLs
                    # fall back to the built-in sources. Read live by the
                    # API server with every query -- no restart needed.
                    try:
                        result = engine.weather.set_settings(
                            city=body.get("city"),
                            api_url=body.get("api_url"),
                            wttr_url=body.get("wttr_url"))
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/weather/wiring":
                    # 修复接线 button: re-apply the device's weather
                    # wiring (hosts/DNAT/CA). The DNAT rule is RAM-only
                    # and vanishes on reboot, so this is the card-level
                    # fix for "weather card stopped working".
                    self._send(200, engine.weather.fix_wiring())
                    return
                if url.path == "/api/clock/face":
                    # Switch the standby clock's face on the device
                    # (adb SET_FACE broadcast). "next" cycles. Accepts
                    # either key: {"face": ...} from the card or the
                    # documented {"name": ...}.
                    try:
                        result = engine.clock.set_face(
                            body.get("face") or body.get("name"))
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/clock/voice":
                    # The clock card's custom spoken phrases -- the
                    # same normalisation as the mini-apps. Empty clears.
                    self._send(200, engine.clock.set_voice_phrases(
                        body.get("phrases") or []))
                    return
                if url.path == "/api/chat":
                    # A typed line for a chosen assistant. It skips the
                    # voice intents on purpose: what the user typed is a
                    # message, not a command.
                    result = engine.chat(body.get("text", ""),
                                          body.get("agent", ""))
                    self._send(200, result)
                    return
                if url.path == "/api/agent":
                    agent = body.get("agent", "")
                    # The console switches the assistant from the computer,
                    # so it does not want the speaker to greet it again.
                    announce = not body.get("quiet", False)
                    if agent == "direct":
                        # "direct" means the speaker itself: no assistant
                        # is selected, the model answers on its own.
                        engine.exit(announce=announce)
                        self._send(200, {"selected_agent": ""})
                        return
                    if agent not in REGISTRY:
                        self._send(400, {"error": "unknown agent"})
                        return
                    if not REGISTRY[agent].get("enabled"):
                        self._send(400, {"error": "assistant not enabled"})
                        return
                    self._send(200, {"selected_agent": engine.select(agent, announce=announce)})
                    return
                if url.path == "/api/model":
                    agent = body.get("agent", "")
                    if not agent:
                        self._send(400, {"error": "agent required"})
                        return
                    try:
                        model = engine.set_agent_model(agent, body.get("model", ""))
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, {"agent": agent, "model": model})
                    return
                if url.path == "/api/direct":
                    # The settings panel sends the whole configuration;
                    # a bare mode changes just the mode.
                    if body.get("mode") and len(body) == 1:
                        value = engine.set_direct_mode(body["mode"])
                        self._send(200, {"mode": value,
                                         "direct": engine.direct_status()})
                        return
                    engine.set_direct_settings(body)
                    self._send(200, {"mode": engine.direct_mode(),
                                     "direct": engine.direct_status()})
                    return
                if url.path == "/api/gemini/settings":
                    self._send(200, engine.set_gemini_settings(body))
                    return
                if url.path == "/api/gemini/test":
                    self._send(200, engine.test_gemini())
                    return
                if url.path == "/api/agent-auth":
                    # Save a connected assistant's authorisation. The token
                    # is written to its auth file, never returned.
                    agent = body.get("agent", "")
                    if not agent:
                        self._send(400, {"error": "agent required"})
                        return
                    try:
                        result = engine.set_agent_auth(agent, body)
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/agent-auth/poll":
                    # Check whether the browser sign-in has finished. The
                    # credential is stored on the host; it is never returned.
                    agent = body.get("agent", "")
                    if not agent:
                        self._send(400, {"error": "agent required"})
                        return
                    try:
                        result = engine.agent_login_poll(agent,
                                                        body.get("login_id", ""))
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/approvals":
                    self._send(200, {"ok": True})
                    return
                if url.path == "/api/factory-notice":
                    # Only the canned expiry-notice family is affected.
                    self._send(200, engine.set_factory_notice(
                        enabled=body.get("enabled")))
                    return
                if url.path == "/api/wake":
                    # Wake words. The master switch and one row per
                    # assistant change separately: {"enabled": false}
                    # turns everything off, {"agent": "dsh", "pinyin":
                    # "da4fei2yu2"} saves one word. {"agent": "...",
                    # "add": true, "name": "..."} registers a new wake
                    # entry (custom assistant or re-added built-in), and
                    # {"agent": "...", "remove": true} drops one.
                    # {"aliases_map": {agent: "a|b"}} saves every assistant's
                    # aliases at once from the console's alias card.
                    try:
                        result = engine.set_wake(
                            enabled=body.get("enabled"),
                            agent=body.get("agent"),
                            agent_enabled=body.get("agent_enabled"),
                            pinyin=body.get("pinyin"),
                            name=body.get("name"),
                            aliases=body.get("aliases"),
                            add=body.get("add", False),
                            remove=body.get("remove", False),
                            aliases_map=body.get("aliases_map"),
                        )
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/neko/ear-only":
                    # The speaker stops reading the catgirl's replies; the
                    # Mac desktop client already speaks them.
                    self._send(200, engine.set_neko_ear_only(
                        enabled=body.get("enabled")))
                    return
                if url.path == "/api/neko/relay":
                    # Hand the turn to the desktop client via the local
                    # rokid_bridge plugin. Rebuilds the cached backend, so
                    # the host stops opening /ws/{character} entirely.
                    self._send(200, engine.set_neko_relay(
                        enabled=body.get("enabled")))
                    return
                if url.path == "/api/neko/proactive":
                    # {"now": true} replays the last catgirl line on the
                    # speaker only; {"now": "fresh"} asks N.E.K.O. for a new
                    # one (the desktop client will read it aloud too).
                    # Both deliberately ignore quiet hours and the interval.
                    if body.get("now"):
                        mode = body.get("now")
                        self._send(200, engine.proactive_now(
                            "fresh" if mode == "fresh" else "replay"))
                        return
                    keys = ("enabled", "interval_minutes", "quiet_start",
                            "quiet_end", "idle_seconds")
                    try:
                        result = engine.set_neko_proactive(**{
                            k: v for k, v in body.items() if k in keys})
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, result)
                    return
                if url.path == "/api/music/pick-folder":
                    try:
                        chosen = music_provider.pick_folder()
                        self._send(200, {"ok": True, "path": chosen})
                    except (MusicError, ValueError) as error:
                        self._send(200, {"ok": False, "error": str(error)})
                    return
                if url.path in ("/api/music/netease/login",
                                "/api/music/netease/logout"):
                    try:
                        if url.path.endswith("/login"):
                            result = music_provider.netease_login(
                                body.get("cookie") or "")
                        else:
                            result = music_provider.netease_logout()
                    except (NeteaseError, ValueError, TypeError) as error:
                        result = {"ok": False, "error": str(error)}
                    self._send(200, result)
                    return
                if url.path in ("/api/music/qqmusic/login",
                                "/api/music/qqmusic/logout"):
                    try:
                        if url.path.endswith("/login"):
                            result = music_provider.qqmusic_login(
                                body.get("cookie") or "")
                        else:
                            result = music_provider.qqmusic_logout()
                    except (NeteaseError, ValueError, TypeError) as error:
                        result = {"ok": False, "error": str(error)}
                    self._send(200, result)
                    return
                if url.path == "/api/music/folders":
                    status = music_provider.index.status()
                    # The Roon lyric-library folder is saved independently of
                    # the playback folders, so a console that only knows the
                    # old payload can never clear the new setting by accident.
                    try:
                        if "folders" in body:
                            status = music_provider.set_folders(
                                body.get("folders") or [])
                        if "lyrics_roots" in body:
                            music_provider.set_lyrics_roots(
                                body.get("lyrics_roots"))
                    except (MusicError, ValueError, TypeError) as error:
                        self._send(200, {"ok": False, "error": str(error)})
                        return
                    self._send(200, {"ok": True,
                                     "folders": music_provider.folders(),
                                     "lyrics_roots":
                                         music_provider.lyrics_roots(),
                                     "index": status})
                    return
                if url.path == "/api/music/folder":
                    # The original single-folder call, kept for callers that
                    # still use it.
                    try:
                        status = music_provider.set_folder(
                            body.get("folder") or "")
                    except MusicError as error:
                        self._send(200, {"ok": False, "error": str(error)})
                        return
                    self._send(200, {"ok": True,
                                     "folder": music_provider.folders()[0],
                                     "index": status})
                    return
                if url.path == "/api/music/reindex":
                    self._send(200, {"ok": True,
                                     "index": music_provider.rebuild()})
                    return
                if url.path == "/api/music/shuffle":
                    # {"enabled": bool} toggles auto-continuation;
                    # {"next": true} just changes the song.
                    if body.get("next"):
                        try:
                            self._send(200, queue_random())
                        except (MusicError, ValueError, OSError) as error:
                            self._send(200, {"ok": False, "error": str(error)})
                        return
                    enabled = bool(body.get("enabled"))
                    music_provider.set_shuffle(enabled)
                    payload = {"ok": True, "enabled": enabled}
                    if enabled:
                        try:
                            payload["playing"] = queue_random()["title"]
                        except (MusicError, ValueError, OSError) as error:
                            music_provider.set_shuffle(False)
                            payload.update(enabled=False, error=str(error))
                    self._send(200, payload)
                    return
                if url.path == "/api/music/play":
                    item = dict(body.get("item") or {})
                    for key in ("kind", "file", "url", "name", "artist",
                                "cover", "duration", "root"):
                        if body.get(key) is not None:
                            item[key] = body[key]
                    try:
                        result = queue_track(item,
                                             str(body.get("note") or ""))
                    except (MusicError, ValueError, OSError) as error:
                        result = {"ok": False, "error": str(error)}
                    self._send(200, result)
                    return
                if url.path == "/api/music/stop":
                    engine.stop_audio()
                    self._send(200, {"ok": True, "state": "idle"})
                    return
                if url.path == "/api/music/control":
                    # Same path the spoken commands take, so the console
                    # buttons and the microphone cannot disagree.
                    action = str(body.get("action") or "")
                    self._send(200, engine.apply_music_control(action))
                    return
                if url.path == "/api/music/parse":
                    # Dry run for the console: show what the microphone
                    # would look for, without playing anything.
                    parsed = intent.music_request(str(body.get("text") or ""))
                    self._send(200, {"ok": True, "parsed": parsed})
                    return
                if url.path == "/api/music/volume":
                    # The console buttons and the spoken command share
                    # this exact entry point.
                    self._send(200, engine.apply_music_volume({
                        "action": str(body.get("action") or ""),
                        "value": body.get("value")}))
                    return
                if url.path == "/api/roon/control":
                    self._send(200, roon_client.control(
                        str(body.get("action") or ""),
                        value=body.get("value"),
                        zone_id=body.get("zone_id")))
                    return
                if url.path == "/api/homeassistant/settings":
                    # {"enabled", "base_url", "token"(keep if empty),
                    #  "clear_token", "language", "agent_id",
                    #  "speak_results", "overview_entities"}
                    try:
                        result = engine.homeassistant.update_settings(
                            enabled=body.get("enabled"),
                            base_url=body.get("base_url"),
                            token=body.get("token"),
                            clear_token=bool(body.get("clear_token")),
                            language=body.get("language"),
                            agent_id=body.get("agent_id"),
                            speak_results=body.get("speak_results"),
                            overview_entities=body.get("overview_entities"),
                            aliases=body.get("aliases"),
                            gate_extras=body.get("gate_extras"),
                            # Wake-word page: same HA instance, new LAN IP.
                            # Keep the stored long-lived token.
                            keep_token=bool(body.get("keep_token")),
                        )
                    except ValueError as error:
                        self._send(400, {"error": str(error)})
                        return
                    self._send(200, {"settings": result})
                    return
                if url.path == "/api/homeassistant/test":
                    # Read-only: GET /api/ and /api/states only. Never a
                    # service call, so this can never move a device.
                    self._send(200, engine.homeassistant.test_connection())
                    return
                if url.path == "/api/homeassistant/command":
                    # Console test drive: one sentence through the same
                    # gate + conversation agent as voice. This CAN move a
                    # device -- the operator presses the button for it.
                    sentence = str(body.get("text") or "").strip()
                    if not sentence:
                        self._send(400, {"error": "缺少 text"})
                        return
                    self._send(200, engine.homeassistant.command(
                        sentence, request_id=str(body.get("request_id")
                                                 or "")))
                    return
                if url.path == "/api/roon/settings":
                    self._send(200, roon_client.configure(
                        target=body.get("target"),
                        zone_id=body.get("zone_id"),
                        host=body.get("host"), port=body.get("port")))
                    return
                if url.path == "/api/roon/power":
                    raw = body.get("enabled")
                    if isinstance(raw, str):
                        enabled = raw.strip().lower() in (
                            "1", "true", "on", "yes")
                    else:
                        enabled = bool(raw)
                    state = roon_client.set_enabled(enabled)
                    applied = False
                    if roon_power is not None:
                        try:
                            roon_power.apply(enabled)
                            applied = True
                        except Exception as error:
                            state["error"] = str(error)[:120]
                    if enabled:
                        # The Node process needs a moment before it listens
                        # on 8766. Reporting "offline" here looks like the
                        # switch failed; the console polls again shortly.
                        state["stage"] = "starting"
                        state["error"] = ""
                    else:
                        # Clear the speaker's Roon screen immediately, even
                        # if the poll loop is between ticks.
                        engine.emit("roon_now", active=False)
                    state["ok"] = True
                    state["power_applied"] = applied
                    self._send(200, state)
                    return
            except ValueError as error:
                self._send(400, {"error": str(error)})
                return
            self._send(404, {"error": "not found"})

    return Handler


def main():
    parser = argparse.ArgumentParser(description="Rokid bridge host")
    parser.add_argument("--config", default=str(HERE / "config.local.json"))
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--mock", action="store_true",
                        help="answer with a fixed line instead of calling an agent")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port

    state_file = Path(cfg.get("state_file") or "bridge-state.json")
    if not state_file.is_absolute():
        state_file = Path(cfg["_root"]) / state_file

    engine = Engine(cfg, state_file=str(state_file))

    power = SidecarPower(HERE.parent)
    handler = make_handler(engine, cfg["token"], roon_power=power)
    server = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), handler)
    server.daemon_threads = True

    # The weather replacement API runs beside the main server: the native
    # app fetches from it over TLS (its own thread, its own port -- see
    # weather.py). The instance always exists so the console's override
    # switch can start / stop it at runtime; it only listens while the
    # switch is on.
    from weather import WeatherApiServer
    engine.weather_api = WeatherApiServer(engine.weather,
                                         cfg.get("weather") or {})
    if engine.weather.enabled:
        engine.weather_api.start()

    def shutdown(*_):
        # shutdown() blocks until serve_forever() returns, so it must not run
        # in the thread that is inside serve_forever(). Calling it straight
        # from the signal handler deadlocks the process and leaves the port
        # held.
        def stop():
            try:
                server.shutdown()
            except Exception:
                pass
            engine.close()

        threading.Thread(target=stop, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("host listening on http://%s:%s" % (cfg["host"], cfg["port"]), flush=True)
    try:
        server.serve_forever()
    finally:
        power.apply(False)
        engine.close()


if __name__ == "__main__":
    main()

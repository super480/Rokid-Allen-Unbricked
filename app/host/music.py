#!/usr/bin/env python3
"""Where the speaker's music comes from, and what to show while it plays.

Tens of thousands of tracks change the rules:

* the folder is chosen by the user in the console (picked from a real
  directory browser, not just typed);
* a background thread indexes **names and sizes only** into a JSON file,
  walking at most five levels of subfolders -- no ID3 reads, no bulk
  artwork extraction, no full library in one response;
* the console asks for one page at a time and can filter by keyword;
* artwork and lyrics are resolved **only for the track about to be played**.

Sources, deliberately separate:

* ``builtin`` -- MP3 files shipped inside the N.E.K.O. app (always local);
* ``local``   -- the user's own folders, streamed from disk via a symlink;
* ``online``  -- Netease Cloud Music through N.E.K.O.'s music service,
                 whose audio link is time-limited and whose account cookie
                 lives in N.E.K.O.'s credential store.

Plus two diagnostic clips generated in memory: a very short quiet chime
(``test``) and a silent wav (``silent``) for checking PCM really reaches the
hardware without making any noise.
"""
from __future__ import annotations

import io
import json
import math
import os
import random
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from pathlib import Path

# The tag helpers are re-exported here: the console's tests reach them
# through the music module, and keeping one import site avoids two sources
# of truth for what "a tag" means.
from media_tags import (EMPTY_METADATA, MAX_LYRIC_LINES, embedded_metadata,
                        extract_apic, extract_uslt, parse_lrc)
from netease import NeteaseAuth, NeteaseError, QQMusicAuth

APP_ROOT = Path(__file__).resolve().parent.parent
LOCAL_MUSIC = APP_ROOT / "runtime" / "music"

ALLOWED_SUFFIXES = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac"}
COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png", "folder.jpg",
               "folder.jpeg", "folder.png", "album.jpg", "album.png",
               "front.jpg")

BUILTIN_TRACKS = (
    ("Prelude.mp3", "Prelude"),
    ("Battle_1_L.mp3", "Battle 1"),
    ("Battle_Theme_1_L.mp3", "Battle Theme"),
    ("Chocobos_L.mp3", "Chocobos"),
)
NEKO_STATIC = "/static/game/games/soccer/audio/"
NETEASE_LYRIC = "https://music.163.com/api/song/lyric?id=%s&lv=1&kv=1&tv=-1"

# Shown on the speaker's screen so a shuffled track is visibly the catgirl's
# choice. No upstream text is generated: N.E.K.O. would read it aloud on the
# Mac too, which the user explicitly does not want.
SHUFFLE_NOTES = ("猫娘随手挑的", "猫娘说这首不错", "猫娘翻到的",
                 "猫娘想听这首", "猫娘替你选的")


def shuffle_note():
    return random.choice(SHUFFLE_NOTES)

# A page is small enough that rendering it is never a problem, even on a
# slow console. The index itself may hold hundreds of thousands of rows.
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_INDEX_ROWS = 300000

# Depth 0 is the chosen folder itself; 1..5 are its subfolders. A music
# collection laid out as Artist/Album/Disc/... is fully covered, while a
# stray symlink farm cannot make the scan walk forever.
SCAN_DEPTH = 5
BROWSE_MAX_DIRS = 300

# The speaker is Android 4.4. Its MediaPlayer handles 16-bit / <=48 kHz FLAC,
# but not 24-bit or 96 kHz material, and its downloader used to refuse any
# clip above 24 MB. A ROON library is mostly 24-bit lossless files of
# 30-100 MB, so almost every track hit one of those two walls. Instead of
# copying a 100 MB file over WiFi, convert just the track about to be played
# into a small AAC clip the device is guaranteed to decode.
DEVICE_MAX_BYTES = 24 * 1024 * 1024
DEVICE_MAX_RATE = 48000
DEVICE_MAX_BITS = 16
TRANSCODE_BITRATE = 256000
AFCONVERT = "/usr/bin/afconvert"
MAX_ART_BYTES = 8 * 1024 * 1024


class MusicError(Exception):
    """A source could not produce playable audio. Message is user-facing."""


def _chime(amplitude=400, seconds=0.4, frequency=660):
    """A very short, very quiet 48 kHz stereo wav. Fade in/out to avoid clicks."""
    rate = 48000
    frames = int(rate * seconds)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        pcm = bytearray()
        for i in range(frames):
            edge = min(i, frames - 1 - i, int(rate * 0.02))
            gain = edge / float(rate * 0.02)
            value = int(amplitude * gain *
                        math.sin(2 * math.pi * frequency * i / rate))
            pcm += struct.pack("<hh", value, value)
        out.writeframes(bytes(pcm))
    return buffer.getvalue()


def _silence(seconds=3.0):
    rate = 48000
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(2)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00" * (int(rate * seconds) * 4))
    return buffer.getvalue()


def _looks_like_audio(data: bytes, suffix: str) -> bool:
    if len(data) < 16:
        return False
    if suffix == ".wav":
        return data[:4] == b"RIFF" and data[8:12] == b"WAVE"
    if suffix == ".mp3":
        return data[:3] == b"ID3" or (data[0] == 0xFF and
                                      (data[1] & 0xE0) == 0xE0)
    if suffix == ".ogg":
        return data[:4] == b"OggS"
    if suffix == ".flac":
        return data[:4] == b"fLaC"
    return True  # m4a / aac: let MediaPlayer decide.


# A ripping tool often prefixes every filename with its track number:
# "01. 五月天 - 拥抱.flac". That number belongs to the album, not to the
# singer, so it must never be shown as part of the artist. Only strip a
# number followed by a separator or whitespace -- "2NE1" is a real name.
TRACK_PREFIX = re.compile(r"^\s*\d{1,3}\s*[.、_\-–—:：)）]\s*|^\s*\d{1,3}\s+")


def _strip_track_prefix(value):
    text = str(value or "").strip()
    return TRACK_PREFIX.sub("", text, count=1).strip() or text


def _display_row(item):
    """A console-facing copy of an indexed row, without CD track numbers."""
    row = dict(item)
    row["name"] = _strip_track_prefix(item.get("name") or "")
    row["artist"] = _strip_track_prefix(item.get("artist") or "")
    return row


def _guess_names(stem: str):
    """'Artist - Title' is the most common naming scheme in a music folder."""
    for separator in (" - ", " – ", " — ", "_-_"):
        if separator in stem:
            artist, _, title = stem.partition(separator)
            if artist.strip() and title.strip():
                return (_strip_track_prefix(title),
                        _strip_track_prefix(artist))
    return _strip_track_prefix(stem), "本地音乐"


# -- ranking a spoken song request -----------------------------------------
#
# The index matches a query as a plain substring of name+artist+file, so
# "晴天" also finds "晴天雨" and a singer's name matches a whole folder.
# A voice request must not play the first row: score the candidates, and
# only a whole-phrase hit counts as finding the song.
def phrase_hit(text, phrase):
    """True when phrase stands on its own inside text.

    Adjacent CJK or latin letters are part of a longer word, so
    ``phrase_hit("晴天雨", "晴天")`` is False while
    ``phrase_hit("晴天 (Live)", "晴天")`` is True.
    """
    value = str(text or "").lower()
    needle = str(phrase or "").lower()
    if not needle:
        return False
    start = 0
    while True:
        pos = value.find(needle, start)
        if pos < 0:
            return False
        before = value[pos - 1] if pos else ""
        end = pos + len(needle)
        after = value[end] if end < len(value) else ""
        if ((not before or not before.isalnum())
                and (not after or not after.isalnum())):
            return True
        start = pos + 1


def _request_queries(request):
    queries = []
    for key in ("phrase", "title"):
        value = str(request.get(key) or "").strip().lower()
        if value and value not in queries:
            queries.append(value)
    return queries


def rank_local_track(item, request):
    """Relevance of an indexed local track. Zero means 'not this song'."""
    name = str(item.get("name") or "").lower()
    artist_field = str(item.get("artist") or "").lower()
    path = str(item.get("file") or "").lower()
    best = 0
    for query in _request_queries(request):
        if name == query:
            best = max(best, 120)
        elif phrase_hit(name, query):
            best = max(best, 80)
        elif phrase_hit(artist_field, query):
            best = max(best, 40)
        elif phrase_hit(path, query):
            best = max(best, 20)
    artist = str(request.get("artist") or "").strip().lower()
    if artist and (phrase_hit(artist_field, artist)
                   or phrase_hit(path, artist)):
        best += 25
    return best


def rank_online_track(item, request):
    """Relevance of a Netease result. Zero means 'skip this one'."""
    name = str(item.get("name") or "").lower()
    artist_field = str(item.get("artist") or "").lower()
    best = 0
    for query in _request_queries(request):
        if name == query:
            best = max(best, 100)
        elif phrase_hit(name, query):
            best = max(best, 60)
        elif phrase_hit(artist_field, query):
            best = max(best, 35)
    artist = str(request.get("artist") or "").strip().lower()
    if artist and phrase_hit(artist_field, artist):
        best += 40
    return best


MAX_ROON_LYRIC_LINES = 400

# -- finding the file Roon is actually playing -------------------------------
# A Roon zone reports title/artist/album but never a file path. The Core's
# library is often mounted on this Mac as <root>/<artist>/<album>/<title>.flac;
# walking all 30k+ files of a network share takes ~80 s, so prune by artist
# and album and read tags from the few best name matches only.
LYRIC_SCAN_DEPTH = 4
LYRIC_SCAN_FILES = 3000


def _loose(text):
    """A punctuation/case-insensitive key for tag vs. file-name matching."""
    return re.sub(r"[^\w]+", "", str(text or "").lower(), flags=re.UNICODE)


def _artist_keys(artist):
    parts = re.split(r"[/、,，;；&]|feat\.?|ft\.?", str(artist or ""),
                     flags=re.IGNORECASE)
    keys = [_loose(part) for part in parts]
    return [key for key in keys if len(key) >= 2][:3]


def _child_dirs(path):
    try:
        with os.scandir(str(path)) as scan:
            return [Path(entry.path) for entry in scan if entry.is_dir()]
    except OSError:
        return []


def _audio_files(folder, depth, budget):
    """Names only: no tag reads, no recursion past ``depth``."""
    base = Path(folder)
    found = []
    try:
        walker = os.walk(str(base))
        for current, dirs, files in walker:
            relative = Path(current).relative_to(base)
            if len(relative.parts) >= depth:
                dirs[:] = []
            for name in files:
                if Path(name).suffix.lower() in ALLOWED_SUFFIXES:
                    found.append(Path(current) / name)
                    if len(found) >= budget:
                        return found
    except (OSError, ValueError):
        pass
    return found


def find_lyric_files(title, artist, album, roots, limit=4):
    """Files in a Roon library that could be this exact song.

    Nothing here raises when the volume is unmounted: Roon keeps playing and
    the screen simply shows "暂无歌词" until the share is back.
    """
    key = _loose(title)
    if len(key) < 2:
        return []
    album_key = _loose(album)
    artist_keys = _artist_keys(artist)
    hits = []
    seen = set()
    for value in roots or []:
        base = Path(str(value)).expanduser()
        if not base.is_dir():
            continue
        artist_dirs = [d for d in _child_dirs(base)
                       if any(k in _loose(d.name) or _loose(d.name) in k
                              for k in artist_keys)]
        # No artist folder: only the root's own files. A full recursive walk
        # of a big NAS share is far too slow for a per-track lyric lookup.
        scopes = artist_dirs[:4] or [base]
        for scope in scopes:
            album_dirs = ([d for d in _child_dirs(scope)
                           if album_key in _loose(d.name)]
                          if album_key and artist_dirs else [])
            directories = album_dirs[:6] or [scope]
            depth = 1 if album_dirs or not artist_dirs else LYRIC_SCAN_DEPTH
            for directory in directories:
                for path in _audio_files(directory, depth,
                                         LYRIC_SCAN_FILES):
                    stem = _loose(path.stem)
                    if key not in stem:
                        continue
                    real = str(path.resolve())
                    if real in seen:
                        continue
                    seen.add(real)
                    hits.append((len(stem) - len(key), path))
    hits.sort(key=lambda pair: pair[0])
    return [path for _, path in hits[:limit]]


def lyrics_for_track(provider, title, artist, online=True, album="",
                     roots=None):
    """Timed lyrics for a song Roon is playing, or ``[]``.

    Roon's now_playing payload has no lyrics. Look for the same track in the
    play queue's own folders, then in the Roon library mounted on this Mac
    (that file's own embedded LRC is the exact one Roon is showing), then on
    Netease. Every source is matched by the *whole* title: an artist-only hit
    would fetch a different song's words and scroll them at the wrong times.
    A miss is quiet and never fatal; the speaker shows "暂无歌词" instead.
    """
    title = str(title or "").strip()
    artist = str(artist or "").strip()
    if not title:
        return []
    request = {"title": title, "phrase": title, "artist": artist}
    queries = [q for q in (title, (artist + " " + title).strip()) if q]
    if len(queries) > 1 and queries[0] == queries[1]:
        queries = queries[:1]

    # 1) The user's own files. The index holds names only, so read tags
    # from the few best candidates, never from the whole library.
    local = {}
    for query in queries:
        try:
            page = provider.search_local(query, limit=50)
        except Exception:
            continue
        for item in page.get("items") or []:
            if str(item.get("kind") or "local") == "local":
                local.setdefault(str(item.get("id") or ""), item)
    ranked = []
    for item in local.values():
        score = rank_local_track(item, request)
        if score >= 80:  # an exact or whole-phrase title hit
            ranked.append((score, len(str(item.get("name") or "")), item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    for _, _, item in ranked[:3]:
        try:
            path = provider.resolve_local_path(item)
            lines = provider.lyrics(item, path, provider.read_tags(path))
            if lines:
                return lines[:MAX_ROON_LYRIC_LINES]
        except Exception:
            continue

    # 1b) Roon's own file. The zone tells us no path, but the Core's library
    # is usually mounted here. This is what makes an embedded LRC show up for
    # a track that lives outside the folders in the playback queue -- e.g.
    # ROON/周杰伦/... when only ROON/TANK and friends are indexed.
    if roots is None:
        getter = getattr(provider, "lyrics_roots", None)
        try:
            roots = getter() if callable(getter) else []
        except Exception:
            roots = []
    for path in find_lyric_files(title, artist, album, roots):
        try:
            item = {"kind": "local", "id": "roonlib:" + str(path),
                    "name": title, "artist": artist}
            lines = provider.lyrics(item, path, provider.read_tags(path))
            if lines:
                return lines[:MAX_ROON_LYRIC_LINES]
        except Exception:
            continue

    if not online:
        return []
    # 2) Netease, guarded the same way: an artist-only match is not enough.
    found = {}
    for query in queries:
        try:
            for item in provider.search(query, limit=8):
                found.setdefault(str(item.get("id") or ""), item)
        except Exception:
            continue
    ranked = []
    for item in found.values():
        score = rank_online_track(item, request)
        if score >= 60:
            ranked.append((score, len(str(item.get("name") or "")), item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    for _, _, item in ranked[:3]:
        try:
            lines = provider.lyrics(item)
            if lines:
                return lines[:MAX_ROON_LYRIC_LINES]
        except Exception:
            continue
    return []


# -- playback compatibility ------------------------------------------------

def flac_streaminfo(path):
    """(sample rate, channels, bits per sample) from FLAC STREAMINFO."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(64)
        if head[:4] != b"fLaC":
            return None
        size = int.from_bytes(head[5:8], "big")
        body = head[8:8 + size]
        if len(body) < 18:
            return None
        packed = int.from_bytes(body[10:18], "big")
        rate = packed >> 44
        channels = ((packed >> 41) & 7) + 1
        bits = ((packed >> 36) & 0x1F) + 1
        if not rate or not bits:
            return None
        return rate, channels, bits
    except (OSError, ValueError, IndexError):
        return None


def needs_transcode(path):
    """Whether the speaker would struggle with this file."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return False
    info = flac_streaminfo(path)
    if info and (info[0] > DEVICE_MAX_RATE or info[2] > DEVICE_MAX_BITS):
        return True
    return size > DEVICE_MAX_BYTES


def transcode_for_device(path, tmp_root):
    """Return (bytes, suffix) of a small AAC clip, or raise MusicError.

    Uses macOS's own afconvert, so there is no ffmpeg dependency. The
    explicit 44100 target also brings 96 kHz material down to a rate the
    Android 4.4 decoder understands. The temporary file is removed at once:
    the clip itself goes into the normal one-hour audio cache.

    **The source file is only ever read.** afconvert writes to a brand new
    file in ``tmp_root`` (the app's runtime cache, never the music library);
    the original is never renamed, overwritten, moved or deleted. The lossy
    AAC applies only to the playback copy, so a hi-res library stays intact.
    """
    source = Path(path)
    tmp_root = Path(tmp_root)
    tmp_root.mkdir(parents=True, exist_ok=True)
    target = tmp_root / ("transcode-" + secrets.token_hex(8) + ".m4a")
    if target.resolve() == source.resolve():
        raise MusicError("转码输出不能指向原始音乐文件")
    command = [AFCONVERT, "-f", "m4af", "-d", "aac @44100",
               "-b", str(TRANSCODE_BITRATE), str(source), str(target)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=180)
        if done.returncode != 0 or not target.is_file():
            detail = (done.stderr or b"").decode("utf-8", "replace").strip()
            raise MusicError("转码失败：" + (detail[:100] or "afconvert 未产出文件"))
        data = target.read_bytes()
        if not data or len(data) > DEVICE_MAX_BYTES:
            raise MusicError("转码结果异常，已保留原文件")
        return data, ".m4a"
    except subprocess.TimeoutExpired as error:
        raise MusicError("转码超时（这首歌太长）") from error
    except OSError as error:
        raise MusicError("转码失败：" + str(error)[:80]) from error
    finally:
        try:
            target.unlink()
        except OSError:
            pass


# -- picking a folder on this computer -------------------------------------
# The browser cannot hand a web page an absolute path (webkitdirectory only
# gives relative ones), so the console browses the host filesystem through
# a small read-only API instead. Only user data areas are enumerable; the
# token already gates the endpoint and the folder list itself accepts any
# absolute path, so this is about not dumping the whole root filesystem into
# a select list.

def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _browse_roots(extra=()):
    candidates = [Path.home(), Path("/Users"), Path("/Volumes"),
                  Path("/home"), Path("/mnt"), Path("/media")]
    candidates += [Path(str(value)) for value in extra]
    roots = []
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def browse_directory(path=None, allowed=()):
    """One directory level, for the console's folder picker."""
    roots = _browse_roots(allowed)
    if path:
        target = Path(str(path)).expanduser()
        if not target.is_absolute():
            raise MusicError("请选择绝对路径")
        target = target.resolve()
    else:
        target = Path.home().resolve()
    if not target.is_dir():
        raise MusicError("这个文件夹不存在：" + str(target))
    if roots and not any(_within(target, root) for root in roots):
        raise MusicError("只能浏览用户目录、外接磁盘和已添加的音乐文件夹")

    dirs = []
    tracks = 0
    try:
        with os.scandir(str(target)) as scan:
            for entry in scan:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append({"name": entry.name,
                                     "path": str(target / entry.name)})
                    elif (Path(entry.name).suffix.lower() in ALLOWED_SUFFIXES
                          and entry.is_file()):
                        tracks += 1
                except OSError:
                    continue
    except OSError as error:
        raise MusicError("读取文件夹失败：" + str(error)[:80]) from error
    dirs.sort(key=lambda item: item["name"].lower())
    parent = target.parent
    shortcuts = []
    for name, candidate in (("家目录", Path.home()),
                            ("音乐", Path.home() / "Music"),
                            ("桌面", Path.home() / "Desktop"),
                            ("下载", Path.home() / "Downloads"),
                            ("文稿", Path.home() / "Documents"),
                            ("外接磁盘", Path("/Volumes"))):
        if candidate.is_dir():
            shortcuts.append({"name": name, "path": str(candidate.resolve())})
    return {"ok": True, "path": str(target),
            "parent": str(parent) if any(_within(parent, r) for r in roots)
                      else None,
            "dirs": dirs[:BROWSE_MAX_DIRS],
            "truncated": len(dirs) > BROWSE_MAX_DIRS,
            "tracks": tracks, "shortcuts": shortcuts,
            "scan_depth": SCAN_DEPTH}


def pick_folder_native(timeout=90):
    """macOS Finder folder chooser. Returns an absolute path."""
    if sys.platform != "darwin":
        raise MusicError("系统选择框只在 macOS 上可用；请用上面的目录列表选")
    script = ('POSIX path of (choose folder with prompt '
              '"选择音乐文件夹")')
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise MusicError("选择超时，请用目录列表手动选") from error
    except OSError as error:
        raise MusicError("打不开系统选择框：" + str(error)[:80]) from error
    if done.returncode != 0:
        raise MusicError("已取消选择，或用目录列表手动选")
    value = (done.stdout or "").strip()
    if not value:
        raise MusicError("没有选到文件夹")
    target = Path(value).expanduser().resolve()
    if not target.is_dir():
        raise MusicError("选中的不是文件夹")
    return str(target)


class MusicSettings:
    """The folders the user picked, persisted outside version control.

    A music collection is rarely in one place, so this is a list. The old
    single ``folder`` key is still read for a seamless upgrade.
    """

    def __init__(self, runtime_root):
        self.file = Path(runtime_root) / "music-settings.json"

    def _read(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def folders(self):
        data = self._read()
        values = data.get("folders")
        if isinstance(values, list) and values:
            return [str(item) for item in values]
        if data.get("folder"):
            return [str(data["folder"])]
        return [str(LOCAL_MUSIC)]

    def folder(self):
        """First folder, kept for callers that only need one."""
        return self.folders()[0]

    def save_folders(self, folders):
        cleaned = []
        for value in folders:
            path = Path(str(value)).expanduser()
            if not path.is_dir():
                raise MusicError("这个文件夹不存在：" + str(path))
            resolved = str(path.resolve())
            if resolved not in cleaned:
                cleaned.append(resolved)
        if not cleaned:
            raise MusicError("至少添加一个音乐文件夹")
        data = self._read()
        data["folders"] = cleaned
        data["shuffle"] = bool(data.get("shuffle"))
        self._write(data)
        return cleaned

    def save(self, folder):
        return self.save_folders([folder])

    def lyrics_roots(self):
        """Where to find the files Roon plays, for their embedded lyrics.

        Not the playback folders: a Roon library is often much bigger than
        what the user wants in the speaker's own song queue. When nothing is
        configured, the parent of each playback folder is a good guess
        (``.../ROON/TANK`` -> ``.../ROON``).
        """
        data = self._read()
        saved = data.get("lyrics_roots")
        if isinstance(saved, list):
            return [str(value) for value in saved if value]
        roots = []
        for folder in self.folders():
            try:
                parent = Path(str(folder)).expanduser().resolve().parent
                # The built-in empty play folder lives inside the project;
                # its parent is our own runtime directory, not a collection.
                if parent == APP_ROOT or APP_ROOT in parent.parents:
                    continue
                if parent.is_dir() and str(parent) not in roots:
                    roots.append(str(parent))
            except (OSError, ValueError):
                continue
        return roots

    def save_lyrics_roots(self, roots):
        data = self._read()
        if roots is None:                 # restore the automatic guess
            data.pop("lyrics_roots", None)
        else:
            cleaned = []
            for value in roots:
                path = Path(str(value)).expanduser()
                if not path.is_dir():
                    raise MusicError("这个文件夹不存在：" + str(path))
                resolved = str(path.resolve())
                if resolved not in cleaned:
                    cleaned.append(resolved)
            data["lyrics_roots"] = cleaned
        if "folders" not in data:
            data["folders"] = self.folders()
        self._write(data)
        return self.lyrics_roots()

    def shuffle(self):
        return bool(self._read().get("shuffle"))

    def set_shuffle(self, enabled):
        data = self._read()
        data["shuffle"] = bool(enabled)
        if "folders" not in data:
            data["folders"] = self.folders()
        self._write(data)
        return bool(enabled)

    def _write(self, data):
        self.file.parent.mkdir(parents=True, exist_ok=True)
        self.file.write_text(json.dumps(data, ensure_ascii=False),
                             encoding="utf-8")


class LocalIndex:
    """A name/size-only index of every folder the user picked.

    Scanning runs on a daemon thread so changing folders never blocks an
    HTTP request. Each row remembers which root it came from. The result is
    cached on disk; a restart reads it instead of walking the folders again.
    """

    def __init__(self, settings: MusicSettings, runtime_root):
        self.settings = settings
        self.file = Path(runtime_root) / "music-index.json"
        self.lock = threading.RLock()
        self.entries = []
        self.folders = []
        self.state = "empty"       # empty | scanning | ready | error
        self.error = ""
        self.built_at = 0.0
        self._thread = None
        self._load()

    def _load(self):
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            entries = data.get("entries") or []
            if isinstance(entries, list):
                self.entries = entries
                roots = data.get("folders") or []
                if not roots and data.get("folder"):
                    roots = [data["folder"]]
                self.folders = [str(item) for item in roots]
                self.built_at = float(data.get("built_at") or 0)
                self.state = "ready" if self.folders else "empty"
        except (OSError, ValueError, TypeError):
            pass

    def _persist(self, folders, entries):
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            self.file.write_text(json.dumps(
                {"folders": [str(item) for item in folders],
                 "built_at": time.time(),
                 "entries": entries}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass  # an index that cannot be saved is still usable in memory

    def set_folders(self, folders):
        saved = self.settings.save_folders(folders)
        self.rebuild(saved)
        return self.status()

    def set_folder(self, folder):
        return self.set_folders([folder])

    def rebuild(self, folders=None):
        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status()   # one scan at a time
            roots = [Path(str(item)) for item in
                     (folders if folders is not None else
                      self.settings.folders())]
            self.state = "scanning"
            self.error = ""
            self._thread = threading.Thread(
                target=self._scan, args=(roots,), daemon=True,
                name="music-index")
            self._thread.start()
        return self.status()

    def _scan(self, roots):
        entries = []
        try:
            for root in roots:
                # A folder that went away must not break the others.
                if not root.is_dir():
                    continue
                for dirpath, dirnames, filenames in os.walk(str(root)):
                    relative_dir = os.path.relpath(dirpath, str(root))
                    depth = (0 if relative_dir == os.curdir else
                             len(Path(relative_dir).parts))
                    # Files at depth 5 are still indexed; their child
                    # folders are not. That is exactly five levels of
                    # subfolders below the chosen folder.
                    dirnames[:] = ([d for d in dirnames
                                    if not d.startswith(".")]
                                   if depth < SCAN_DEPTH else [])
                    for filename in filenames:
                        suffix = Path(filename).suffix.lower()
                        if suffix not in ALLOWED_SUFFIXES:
                            continue
                        full = Path(dirpath) / filename
                        try:
                            rel = str(full.relative_to(root))
                            size = full.stat().st_size
                        except (OSError, ValueError):
                            continue
                        title, artist = _guess_names(full.stem)
                        entries.append({
                            "kind": "local",
                            "id": "local:" + str(root) + "/" + rel,
                            "name": title, "artist": artist,
                            "file": rel, "root": str(root),
                            "size": size, "suffix": suffix})
                        if len(entries) >= MAX_INDEX_ROWS:
                            break
                    if len(entries) >= MAX_INDEX_ROWS:
                        break
                if len(entries) >= MAX_INDEX_ROWS:
                    break
            entries.sort(key=lambda item: (item["name"].lower(),
                                           item["file"].lower()))
            # Persist BEFORE announcing "ready": a caller that sees ready
            # must be able to restart and read the same index from disk.
            self._persist(roots, entries)
            with self.lock:
                self.entries = entries
                self.folders = [str(item) for item in roots]
                self.built_at = time.time()
                self.state = "ready"
        except Exception as error:
            with self.lock:
                self.state = "error"
                self.error = str(error)[:200]

    def status(self):
        with self.lock:
            return {"state": self.state, "count": len(self.entries),
                    "folders": self.folders or self.settings.folders(),
                    "built_at": self.built_at, "error": self.error,
                    "scan_depth": SCAN_DEPTH}

    def query(self, q="", offset=0, limit=PAGE_SIZE):
        q = str(q or "").strip().lower()
        offset = max(0, int(offset or 0))
        limit = min(max(1, int(limit or PAGE_SIZE)), MAX_PAGE_SIZE)
        with self.lock:
            rows = self.entries
        if q:
            rows = [item for item in rows
                    if q in (item["name"] + " " + item["artist"] + " "
                             + item["file"]).lower()]
        page = rows[offset:offset + limit]
        # An index built before track numbers were stripped still holds
        # "01. 五月天". Never rewrite the stored rows (the search matcher
        # needs the original path), but show clean names in the console.
        return [_display_row(item) for item in page], len(rows), \
            offset + len(page) < len(rows)

    def random(self, exclude=()):
        """Pick a track from the whole collection, avoiding recent ones."""
        with self.lock:
            rows = list(self.entries)
        blocked = set(exclude or ())
        pool = [item for item in rows if item.get("id") not in blocked]
        if not pool:
            pool = rows
        return random.choice(pool) if pool else None

    def path_for(self, item):
        """Resolve an indexed track, never escaping a chosen folder."""
        allowed = [Path(str(value)).resolve()
                   for value in self.settings.folders()]
        requested = str(item.get("root") or "")
        root = (Path(requested).resolve() if requested else
                (allowed[0] if allowed else Path(LOCAL_MUSIC).resolve()))
        if root not in allowed:
            raise MusicError("文件不在音乐文件夹里")
        relative = str(item.get("file") or "")
        parts = Path(relative).parts
        if not relative or ".." in parts or Path(relative).is_absolute():
            raise MusicError("文件名不合法")
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            raise MusicError("文件不在音乐文件夹里")
        if not target.is_file():
            raise MusicError("本地文件不存在")
        return target


class MusicProvider:
    def __init__(self, config, timeout=10, local_root=None, runtime_root=None):
        neko = config.get("neko") or {}
        raw = str(neko.get("url") or "ws://127.0.0.1:48911")
        self.base = raw.replace("wss://", "https://").replace(
            "ws://", "http://").rstrip("/")
        self.timeout = timeout
        # Injectable so the tests never touch the real music folder.
        self.runtime_root = Path(runtime_root) if runtime_root else (
            Path(local_root).parent if local_root else APP_ROOT / "runtime")
        self.local_root = Path(local_root) if local_root else LOCAL_MUSIC
        self.settings = MusicSettings(self.runtime_root)
        self.index = LocalIndex(self.settings, self.runtime_root)
        self.netease = NeteaseAuth(self.base, timeout=4)
        self.qqmusic = QQMusicAuth(self.base, timeout=4)

    # -- the folder picker and the online accounts -------------------------

    def browse(self, path=None):
        return browse_directory(path, self.settings.folders())

    def pick_folder(self):
        return pick_folder_native()

    def netease_status(self):
        return self.netease.status()

    def netease_login(self, cookie):
        return self.netease.login(cookie)

    def netease_logout(self):
        return self.netease.logout()

    def qqmusic_status(self):
        return self.qqmusic.status()

    def qqmusic_login(self, cookie):
        return self.qqmusic.login(cookie)

    def qqmusic_logout(self):
        return self.qqmusic.logout()

    def qqmusic_search(self, query, limit=8):
        """Ask N.E.K.O. for QQ Music songs. Mirrors :meth:`search`."""
        query = str(query or "").strip()
        if not query:
            raise MusicError("请先输入歌名")
        url = "/api/music/qqmusic/search?" + urllib.parse.urlencode(
            {"query": query, "limit": limit})
        try:
            raw = self._fetch(url)
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception as error:
            raise MusicError("QQ 音乐搜索失败：" + str(error)[:120]) from error
        rows = payload.get("data") or []
        if not payload.get("success") or not rows:
            raise MusicError("QQ 音乐没有找到这首歌")
        return [{"kind": "online", "id": "online:" + str(row.get("url") or ""),
                 "name": str(row.get("name") or "未知歌曲"),
                 "artist": str(row.get("artist") or ""),
                 "duration": row.get("duration"),
                 "cover": str(row.get("cover") or ""),
                 "url": str(row.get("url") or "")}
                for row in rows[:limit]]

    def prepare_local_clip(self, path):
        """(bytes|None, suffix): a converted clip, or None to stream as-is."""
        if not needs_transcode(path):
            return None, Path(path).suffix.lower()
        data, suffix = transcode_for_device(path, self.runtime_root / "tmp")
        return data, suffix

    # -- discovery ---------------------------------------------------------

    def search_local(self, query, limit=30):
        """One bounded page of matching local tracks; never the whole library."""
        q = str(query or "").strip()
        if not q:
            return {"items": [], "total": 0, "has_more": False,
                    "error": "请输入歌名或歌手"}
        limit = min(max(1, int(limit or 30)), 50)
        rows, total, _ = self.index.query(q, 0, limit)
        return {"items": rows, "total": total,
                "has_more": total > len(rows)}

    def _builtin(self, q=""):
        items = [{"kind": "builtin", "id": "builtin:" + filename,
                  "name": title + "（内置试听）", "artist": "N.E.K.O.",
                  "file": filename}
                 for filename, title in BUILTIN_TRACKS]
        if q:
            items = [x for x in items if q in (x["name"] + x["artist"]).lower()]
        return items

    def library(self):
        """Backwards-compatible flat view: built-ins plus the first page."""
        page, _, _ = self.index.query("", 0, PAGE_SIZE)
        return self._builtin() + page

    def library_page(self, q="", offset=0, limit=PAGE_SIZE):
        """What the console actually uses: one bounded page at a time."""
        page, total, has_more = self.index.query(q, offset, limit)
        builtin = self._builtin(q) if int(offset or 0) == 0 else []
        return {"items": builtin + page, "total": total + len(builtin),
                "has_more": has_more, "offset": int(offset or 0) + len(page),
                "limit": int(limit), "index": self.index.status()}

    def set_folder(self, folder):
        return self.index.set_folder(folder)

    def set_folders(self, folders):
        return self.index.set_folders(folders)

    def folders(self):
        return self.settings.folders()

    def lyrics_roots(self):
        return self.settings.lyrics_roots()

    def set_lyrics_roots(self, roots):
        return self.settings.save_lyrics_roots(roots)

    def rebuild(self):
        return self.index.rebuild()

    def shuffle_enabled(self):
        return self.settings.shuffle()

    def set_shuffle(self, enabled):
        return self.settings.set_shuffle(enabled)

    def random_track(self, exclude=()):
        item = self.index.random(exclude)
        if item is None:
            raise MusicError("本地曲库是空的，先添加音乐文件夹并重建索引")
        return item

    def search(self, query, limit=8):
        """Ask N.E.K.O. for songs. Returns descriptors, not audio."""
        query = str(query or "").strip()
        if not query:
            raise MusicError("请先输入歌名")
        url = "/api/music/search?" + urllib.parse.urlencode(
            {"query": query, "limit": limit})
        try:
            raw = self._fetch(url)
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception as error:
            raise MusicError("搜索失败：" + str(error)[:120]) from error
        rows = payload.get("data") or []
        if not payload.get("success") or not rows:
            raise MusicError("没有找到这首歌")
        return [{"kind": "online", "id": "online:" + str(row.get("url") or ""),
                 "name": str(row.get("name") or "未知歌曲"),
                 "artist": str(row.get("artist") or ""),
                 "duration": row.get("duration"),
                 "cover": str(row.get("cover") or ""),
                 "url": str(row.get("url") or "")}
                for row in rows[:limit]]

    # -- playback ----------------------------------------------------------

    def resolve(self, item) -> tuple:
        """Return (bytes, suffix, title) for downloaded / generated clips."""
        kind = str(item.get("kind") or "")
        if kind == "test":
            return _chime(), ".wav", "试音提示"
        if kind == "silent":
            return _silence(), ".wav", "静音自检"
        if kind == "builtin":
            filename = self._safe_name(item.get("file"))
            if filename not in {f for f, _ in BUILTIN_TRACKS}:
                raise MusicError("内置曲库里没有这首")
            data = self._fetch(NEKO_STATIC + urllib.parse.quote(filename))
            return data, ".mp3", dict(BUILTIN_TRACKS)[filename]
        if kind == "local":
            path = self.index.path_for(item)
            try:
                data = path.read_bytes()
            except OSError as error:
                raise MusicError("读取失败：" + str(error)[:80]) from error
            return data, path.suffix.lower(), path.stem
        if kind == "online":
            url = str(item.get("url") or "")
            if not url.startswith("/api/music/"):
                raise MusicError("这首歌的音频地址无效")
            try:
                data = self._fetch(url, timeout=25)
            except urllib.error.HTTPError as error:
                raise MusicError(
                    "这首歌暂时取不到音频（上游 %s）" % error.code) from error
            except Exception as error:
                raise MusicError("取音频失败：" + str(error)[:100]) from error
            return data, ".mp3", str(item.get("name") or "在线歌曲")
        raise MusicError("未知的音频来源")

    def resolve_local_path(self, item):
        """The local path for streaming, without reading the file."""
        return self.index.path_for(item)

    def read_tags(self, local_path):
        """Read one local file's tags once. Never fatal: a broken tag must
        not stop the song. Callers may pass the result to metadata(),
        artwork() and lyrics() so the same 4 MB is not read three times.
        """
        try:
            return embedded_metadata(local_path)
        except Exception:
            return dict(EMPTY_METADATA)

    def metadata(self, item, local_path=None, tags=None):
        """What the now-playing screen calls this track.

        The index holds only filenames (a library this large must never be
        opened in bulk), so for a local file we read its own TITLE / ARTIST
        tag at play time. That also removes the "01." some rippers put in
        front of the singer in the filename. A file with no tags falls back
        to the guessed names, with the track number stripped anyway.
        """
        title = _strip_track_prefix(
            item.get("name") or item.get("file") or "音乐")
        artist = _strip_track_prefix(item.get("artist") or "")
        if local_path is not None:
            if tags is None:
                tags = self.read_tags(local_path)
            # Use the tag verbatim: unlike a filename, "01. 序" may be the
            # song's real title and must not be rewritten.
            if tags.get("title"):
                title = tags["title"]
            if tags.get("artist"):
                artist = tags["artist"]
        return {"title": title, "artist": artist}

    def artwork(self, item, local_path=None, tags=None):
        """Return (image bytes, suffix) or None. Best effort, never fatal.

        For a local track the picture embedded in the file itself wins; a
        sibling cover.jpg is only a fallback for files with no embedded art.
        """
        kind = str(item.get("kind") or "")
        try:
            if kind == "online":
                cover = str(item.get("cover") or "")
                if cover.startswith("https://"):
                    data = self._fetch_url(cover, timeout=8)
                    if data and len(data) <= MAX_ART_BYTES:
                        return data, ".jpg"
            if kind == "local" and local_path is not None:
                if tags is None:
                    tags = self.read_tags(local_path)
                art = tags.get("art")
                if art and len(art[0]) <= MAX_ART_BYTES:
                    return art
                for name in COVER_NAMES:
                    sibling = local_path.parent / name
                    if (sibling.is_file() and
                            sibling.stat().st_size <= MAX_ART_BYTES):
                        return sibling.read_bytes(), sibling.suffix.lower()
        except Exception:
            pass
        return None

    def lyrics(self, item, local_path=None, tags=None):
        """Return [{t: ms, text: str}] or []. Best effort, never fatal.

        Local order: the file's own lyric tag -> a sibling .lrc. Only text
        with LRC timestamps can be synced to MediaPlayer's position, so an
        untagged plain-text lyric falls through instead of freezing on line 1.
        """
        kind = str(item.get("kind") or "")
        try:
            if kind == "local" and local_path is not None:
                if tags is None:
                    tags = self.read_tags(local_path)
                embedded = tags.get("lyrics")
                if embedded:
                    lines = parse_lrc(embedded)
                    if lines:
                        return lines
                lrc = local_path.with_suffix(".lrc")
                if lrc.is_file():
                    lines = parse_lrc(lrc.read_text(encoding="utf-8",
                                                    errors="replace"))
                    if lines:
                        return lines
            if kind == "online":
                match = re.search(r"/netease/(\d+)", str(item.get("url") or ""))
                if match:
                    raw = self._fetch_url(NETEASE_LYRIC % match.group(1),
                                          timeout=8)
                    payload = json.loads(raw.decode("utf-8", "replace"))
                    text = (payload.get("lrc") or {}).get("lyric") or ""
                    return parse_lrc(text)
        except Exception:
            pass
        return []

    # -- internals ---------------------------------------------------------

    def validate(self, data: bytes, suffix: str):
        if not _looks_like_audio(data, suffix):
            raise MusicError("拿到的不是音频文件（上游可能返回了错误页）")

    @staticmethod
    def _safe_name(name):
        name = str(name or "")
        if "/" in name or "\\" in name or ".." in name or not name:
            raise MusicError("文件名不合法")
        return name

    def _fetch(self, path, timeout=None):
        request = urllib.request.Request(
            self.base + path,
            headers={"User-Agent": "RokidBridge/1.0",
                     "Accept": "audio/*,application/json,*/*"})
        with urllib.request.urlopen(request,
                                    timeout=timeout or self.timeout) as response:
            data = response.read(24 * 1024 * 1024 + 1)
        if len(data) > 24 * 1024 * 1024:
            raise MusicError("音频文件太大")
        return data

    def _fetch_url(self, url, timeout=8):
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0",
                          "Referer": "https://music.163.com/",
                          "Accept": "*/*"})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(8 * 1024 * 1024 + 1)

#!/usr/bin/env python3
"""Read the cover, words and names stored *inside* a music file, no dependencies.

Only the file that is about to be played is parsed. A library with tens of
thousands of tracks must never be opened in bulk: the index keeps names and
sizes only, and these helpers run once per playback.

The user wants the file's own metadata to win, so the provider asks
``embedded_metadata()`` first and only falls back to a sibling cover image or
``.lrc`` file when the file itself carries nothing. The TITLE / ARTIST tags
also win over the guess made from a filename such as ``01. 五月天 - 拥抱``,
whose leading track number would otherwise show up in front of the singer.
Formats understood:

* mp3 / tagged wav -- ID3v2 ``APIC``/``PIC`` and ``USLT``/``ULT``
* flac             -- ``PICTURE`` block and Vorbis-comment
                      ``METADATA_BLOCK_PICTURE`` / ``LYRICS``
* ogg / opus       -- the Xiph comment packet on the first pages
* m4a / aac / mp4  -- ``covr`` and ``©lyr`` atoms in ``moov/udta/meta/ilst``
                      (``moov`` usually sits at the end, so the tail is read)

Everything here is defensive: a malformed tag returns nothing rather than
raising, because a broken tag must not stop a song from playing.
"""
from __future__ import annotations

import base64
import os
import re
import struct

LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")

# A lyric sheet larger than this is almost certainly a malformed file.
MAX_LYRIC_LINES = 400


def syncsafe(raw: bytes) -> int:
    """ID3v2 synchsafe integer (7 bits per byte)."""
    if len(raw) != 4:
        return 0
    return (raw[0] << 21) | (raw[1] << 14) | (raw[2] << 7) | raw[3]


def id3_frames(data: bytes):
    """Yield (frame id, body) for the frames in an ID3v2 tag."""
    if len(data) < 10 or data[:3] != b"ID3":
        return []
    version = data[3]
    # ID3v2.2 uses three-letter ids and a three-byte size; v2.3/v2.4 use
    # four of each. Reading the short form with the long layout would mix
    # the text encoding byte into the frame id and lose every tag.
    legacy = version == 2
    id_len = 3 if legacy else 4
    size_len = 3 if legacy else 4
    # v2.3/v2.4 add two flag bytes after the size; v2.2 has none.
    header = id_len + size_len + (0 if legacy else 2)
    tag_size = syncsafe(data[6:10])
    end = min(10 + tag_size, len(data))
    pos = 10
    # An extended header sits before the frames when bit 6 of the flags is
    # set. Its own size is syncsafe (v2.4) or a plain big-endian int (v2.3).
    if data[5] & 0x40 and pos + 4 <= end:
        raw = data[pos:pos + 4]
        skip = syncsafe(raw) if version == 4 else struct.unpack(">I", raw)[0]
        pos += max(4, skip)
    frames = []
    while pos + header <= end:
        fid = data[pos:pos + id_len]
        if not fid or not all(48 <= c <= 57 or 65 <= c <= 90 for c in fid):
            break
        raw_size = data[pos + id_len:pos + id_len + size_len]
        if legacy:
            size = int.from_bytes(raw_size, "big")
        elif version == 4:
            size = syncsafe(raw_size)
        else:
            size = struct.unpack(">I", raw_size)[0]
        if size <= 0 or pos + header + size > end:
            break
        frames.append((fid.decode("latin1"),
                       data[pos + header:pos + header + size]))
        pos += header + size
    return frames


def decode_text(raw: bytes, encoding: int) -> str:
    try:
        if encoding == 0:
            # Plenty of Chinese tags declare ISO-8859-1 but really hold UTF-8
            # bytes. Prefer UTF-8 when it decodes cleanly; genuinely
            # single-byte text cannot, so it still falls back to latin1.
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return raw.decode("latin1", "replace")
        if encoding == 1:
            return raw.decode("utf-16", "replace")
        if encoding == 2:
            return raw.decode("utf-16-be", "replace")
        return raw.decode("utf-8", "replace")
    except (UnicodeError, LookupError):
        return raw.decode("utf-8", "replace")


def _image_suffix(image: bytes):
    if image[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if image[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if image[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if image[:4] == b"RIFF" and image[8:12] == b"WEBP":
        return ".webp"
    return None


def extract_apic(data: bytes):
    """Return (image bytes, suffix) from an ID3v2 APIC/PIC frame, or None."""
    for fid, body in id3_frames(data):
        if not (fid.startswith("APIC") or fid == "PIC"):
            continue
        try:
            if fid == "PIC":  # ID3v2.2: image format(3), type, desc, data
                i = 3 + 1
                encoding = 0
            else:
                encoding = body[0]
                i = 1
                nul = body.find(b"\x00", i)  # MIME type
                if nul < 0:
                    continue
                i = nul + 1 + 1  # skip the picture type byte
            if encoding in (1, 2):  # UTF-16 description, two-byte terminator
                j = i
                while j + 1 < len(body) and body[j:j + 2] != b"\x00\x00":
                    j += 2
                i = j + 2
            else:
                nul = body.find(b"\x00", i)
                i = (nul + 1) if nul >= 0 else i
            image = body[i:]
            suffix = _image_suffix(image)
            if suffix:
                return image, suffix
        except (IndexError, ValueError):
            continue
    # Fallback: some writers produce a non-standard APIC. Look for a JPEG or
    # PNG header inside the tag area and let BitmapFactory ignore the tail.
    tag_size = syncsafe(data[6:10]) if len(data) >= 10 else 0
    scan_end = min(10 + tag_size, len(data))
    for magic, suffix in ((b"\xff\xd8\xff", ".jpg"),
                          (b"\x89PNG\r\n\x1a\n", ".png")):
        at = data.find(magic, 10, scan_end)
        if at >= 0:
            return data[at:scan_end], suffix
    return None


def extract_uslt(data: bytes):
    """Return the text of an embedded USLT/ULT lyric frame, or None."""
    for fid, body in id3_frames(data):
        if not (fid.startswith("USLT") or fid == "ULT"):
            continue
        try:
            encoding = body[0] if fid != "ULT" else 0
            i = 4 if fid != "ULT" else 6  # encoding + language (3)
            if encoding in (1, 2):
                j = i
                while j + 1 < len(body) and body[j:j + 2] != b"\x00\x00":
                    j += 2
                i = j + 2
            else:
                nul = body.find(b"\x00", i)
                i = (nul + 1) if nul >= 0 else i
            text = decode_text(body[i:], encoding).strip()
            if text:
                return text
        except (IndexError, ValueError):
            continue
    return None


def parse_lrc(text):
    """Turn LRC text into a sorted [{t: milliseconds, text: ...}] list."""
    if not text:
        return []
    lines = []
    for raw in str(text).splitlines():
        stamps = list(LRC_TIME.finditer(raw))
        if not stamps:
            continue
        body = raw[stamps[-1].end():].strip()
        if not body:
            continue
        for stamp in stamps:
            minutes = int(stamp.group(1))
            seconds = int(stamp.group(2))
            fraction = stamp.group(3) or "0"
            # Two digits are centiseconds, three are milliseconds.
            millis = int((fraction + "000")[:3]) if len(fraction) <= 3 \
                else int(fraction[:3])
            lines.append({"t": minutes * 60000 + seconds * 1000 + millis,
                          "text": body})
    lines.sort(key=lambda item: item["t"])
    return lines[:MAX_LYRIC_LINES]


# -- metadata stored inside one local file ----------------------------------

EMBEDDED_SCAN_BYTES = 2 * 1024 * 1024
_MP4_SUFFIXES = {".m4a", ".aac", ".mp4"}
_MP4_CONTAINERS = (b"moov", b"udta", b"meta", b"ilst", b"trak", b"mdia")
EMPTY_METADATA = {"art": None, "lyrics": None,
                  "title": None, "artist": None}


def _text(value):
    """One usable tag value: no NUL padding, no surrounding whitespace.

    Chinese ID3v1 tags are often GBK while Vorbis comments are UTF-8;
    either way a malformed byte sequence must yield None, not a crash.
    """
    if isinstance(value, bytes):
        for encoding in ("utf-8", "gb18030", "latin1"):
            try:
                value = value.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        else:
            return None
    text = str(value or "").replace("\x00", "").strip()
    return text or None


def _names(values):
    """Title / performer from an UPPERCASE key -> bytes mapping.

    ARTIST is the singer on this recording; ALBUMARTIST is only a fallback
    for compilations that leave ARTIST empty.
    """
    def pick(*keys):
        for key in keys:
            found = _text(values.get(key))
            if found:
                return found
        return None
    return {"title": pick("TITLE"),
            "artist": pick("ARTIST", "ALBUMARTIST", "ALBUM ARTIST")}


ID3_NAME_FRAMES = {"TIT2": "title", "TPE1": "artist",
                   "TPE2": "album_artist",
                   "TT2": "title", "TP1": "artist", "TP2": "album_artist"}


def id3_names(data):
    """Title and performer from ID3v2 text frames (v2.2 / v2.3 / v2.4)."""
    found = {"title": None, "artist": None, "album_artist": None}
    for fid, body in id3_frames(data):
        key = next((ID3_NAME_FRAMES[name] for name in ID3_NAME_FRAMES
                    if fid.startswith(name)), None)
        if key is None or not body or found[key] is not None:
            continue
        # Every version starts the frame body with its text encoding byte.
        value = _text(decode_text(body[1:], body[0]))
        if value:
            found[key] = value
    return {"title": found["title"],
            "artist": found["artist"] or found["album_artist"]}


def id3v1_names(blob):
    """Fallback for old mp3s: the 128-byte TAG block at the very end."""
    tag = blob[-128:] if blob else b""
    if len(tag) != 128 or tag[:3] != b"TAG":
        return {"title": None, "artist": None}
    return {"title": _text(tag[3:33]), "artist": _text(tag[33:63])}


def _be32(raw, pos):
    return int.from_bytes(raw[pos:pos + 4], "big") if pos + 4 <= len(raw) else 0


def _le32(raw, pos):
    return int.from_bytes(raw[pos:pos + 4], "little") if pos + 4 <= len(raw) else 0


def flac_picture(raw):
    """One FLAC PICTURE block -> (image bytes, suffix), or None."""
    try:
        pos = 4                                  # picture type
        mime_len = _be32(raw, pos)
        pos += 4 + mime_len
        desc_len = _be32(raw, pos)
        pos += 4 + desc_len + 16                 # description + w/h/depth/colours
        data_len = _be32(raw, pos)
        pos += 4
        if data_len <= 0 or pos + data_len > len(raw):
            return None
        image = raw[pos:pos + data_len]
        suffix = _image_suffix(image)
        return (image, suffix) if suffix else None
    except (TypeError, ValueError, IndexError):
        return None


def vorbis_comments(raw):
    """Xiph-style comment block -> {UPPERCASE_KEY: bytes value}."""
    found = {}
    try:
        vendor_len = _le32(raw, 0)
        pos = 4 + vendor_len
        count = _le32(raw, pos)
        pos += 4
        for _ in range(min(count, 200)):
            length = _le32(raw, pos)
            pos += 4
            if length < 0 or pos + length > len(raw):
                break
            entry = raw[pos:pos + length]
            pos += length
            key, separator, value = entry.partition(b"=")
            if separator:
                found[key.decode("latin1", "replace").upper()] = value
    except (TypeError, ValueError, IndexError):
        pass
    return found


def _from_comments(values):
    art = None
    lyrics = None
    blob = values.get("METADATA_BLOCK_PICTURE")
    if blob:
        try:
            art = flac_picture(base64.b64decode(blob))
        except (ValueError, TypeError):
            art = None
    for key in ("LYRICS", "UNSYNCEDLYRICS", "UNSYNCED LYRICS"):
        raw = values.get(key)
        if raw:
            lyrics = raw.decode("utf-8", "replace").strip() or None
            if lyrics:
                break
    result = {"art": art, "lyrics": lyrics}
    result.update(_names(values))
    return result


def flac_metadata(head):
    """Walk the FLAC metadata blocks at the head of the file."""
    result = dict(EMPTY_METADATA)
    if not head.startswith(b"fLaC"):
        return result
    pos = 4
    for _ in range(64):
        if pos + 4 > len(head):
            break
        header = head[pos]
        kind = header & 0x7F
        last = bool(header & 0x80)
        length = int.from_bytes(head[pos + 1:pos + 4], "big")
        body = head[pos + 4:pos + 4 + length]
        pos += 4 + length
        if len(body) < length:
            break
        if kind == 6 and result["art"] is None:
            result["art"] = flac_picture(body)
        elif kind == 4:
            found = _from_comments(vorbis_comments(body))
            for key, value in found.items():
                if result[key] is None:
                    result[key] = value
        if last:
            break
    return result


def ogg_metadata(head):
    """The comment packet lives on the first Ogg pages; never parse audio."""
    if head[:4] != b"OggS":
        return dict(EMPTY_METADATA)
    packets = b""
    pos = 0
    for _ in range(8):
        if pos + 27 > len(head) or head[pos:pos + 4] != b"OggS":
            break
        count = head[pos + 26]
        table = head[pos + 27:pos + 27 + count]
        if len(table) < count:
            break
        length = sum(table)
        payload = head[pos + 27 + count:pos + 27 + count + length]
        packets += payload
        pos += 27 + count + length
        if b"\x03vorbis" in packets or b"OpusTags" in packets:
            break
    for marker, skip in ((b"\x03vorbis", 7), (b"OpusTags", 8)):
        at = packets.find(marker)
        if at >= 0:
            return _from_comments(vorbis_comments(packets[at + skip:]))
    return dict(EMPTY_METADATA)


def mp4_boxes(data, start=0, end=None):
    """Yield (type, payload_start, payload_end) for MP4 atoms."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size = int.from_bytes(data[pos:pos + 4], "big")
        kind = data[pos + 4:pos + 8]
        header = 8
        if size == 1 and pos + 16 <= end:
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        yield kind, pos + header, pos + size
        pos += size


def _mp4_find(data, start, end, wanted, depth=0):
    if depth > 5:
        return
    for kind, begin, finish in mp4_boxes(data, start, end):
        if kind in wanted:
            yield kind, begin, finish
        if kind in _MP4_CONTAINERS:
            skip = 4 if kind == b"meta" else 0   # meta starts with version/flags
            yield from _mp4_find(data, begin + skip, finish, wanted, depth + 1)


def _mp4_payload(data, begin, finish):
    for kind, start, end in mp4_boxes(data, begin, finish):
        if kind == b"data" and end - start > 8:
            return data[start + 8:end]           # version/flags + locale
    return None


def mp4_metadata(head, tail):
    """covr / ©lyr / ©nam / ©ART atoms. moov is usually in the tail."""
    result = dict(EMPTY_METADATA)
    regions = [(head, 0, len(head))]
    at = tail.find(b"moov")
    if at >= 4:
        regions.append((tail, at - 4, len(tail)))
    wanted = (b"covr", b"\xa9lyr", b"\xa9nam", b"\xa9ART", b"aART")
    for data, start, end in regions:
        for kind, begin, finish in _mp4_find(data, start, end, wanted):
            payload = _mp4_payload(data, begin, finish)
            if not payload:
                continue
            if kind == b"covr" and result["art"] is None:
                suffix = _image_suffix(payload)
                if suffix:
                    result["art"] = (payload, suffix)
            elif kind == b"\xa9lyr" and result["lyrics"] is None:
                result["lyrics"] = (
                    payload.decode("utf-8", "replace").strip() or None)
            elif kind == b"\xa9nam" and result["title"] is None:
                result["title"] = _text(payload)
            elif kind == b"\xa9ART" and result["artist"] is None:
                result["artist"] = _text(payload)
            elif kind == b"aART" and result["artist"] is None:
                # Album artist is only a fallback; a later ©ART still wins,
                # so remember it separately instead of blocking that frame.
                result.setdefault("album_artist", _text(payload))
    if not result["artist"]:
        result["artist"] = result.pop("album_artist", None)
    result.pop("album_artist", None)
    return result


def embedded_metadata(path):
    """Cover, lyrics and names stored inside one local audio file.

    Returns ``{"art": (bytes, suffix) | None, "lyrics": str | None,
    "title": str | None, "artist": str | None}``. Never raises: a broken tag
    must not stop a song from playing.
    """
    try:
        total = os.path.getsize(path)
        with open(path, "rb") as handle:
            head = handle.read(EMBEDDED_SCAN_BYTES)
            tail = b""
            if total > EMBEDDED_SCAN_BYTES:
                handle.seek(-EMBEDDED_SCAN_BYTES, os.SEEK_END)
                tail = handle.read(EMBEDDED_SCAN_BYTES)
    except (OSError, ValueError):
        return dict(EMPTY_METADATA)
    suffix = os.path.splitext(str(path))[1].lower()
    # ID3v1 lives in the last 128 bytes, which are in head for small files
    # and in tail once the file is bigger than the scan window.
    end = tail or head
    try:
        if head.startswith(b"fLaC"):
            return flac_metadata(head)
        if head[:4] == b"OggS":
            return ogg_metadata(head)
        if head[4:8] == b"ftyp" or suffix in _MP4_SUFFIXES:
            return mp4_metadata(head, tail)
        result = dict(EMPTY_METADATA)
        if head[:3] == b"ID3":
            # Magic only: scanning a raw PCM or FLAC stream for a JPEG
            # header would otherwise invent a cover out of audio samples.
            result["art"] = extract_apic(head)
            result["lyrics"] = extract_uslt(head)
            names = id3_names(head)
        else:
            names = {"title": None, "artist": None}
        v1 = id3v1_names(end)
        result["title"] = names["title"] or v1["title"]
        result["artist"] = names["artist"] or v1["artist"]
        return result
    except Exception:
        pass
    return dict(EMPTY_METADATA)

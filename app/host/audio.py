#!/usr/bin/env python3
"""A tiny media cache between the host and the speaker.

The device already polls the authenticated host for commands. An ``audio``
command carries a *relative* path; the device downloads the bytes with the
same token it uses for every other request, writes them into its own cache
directory, and hands the local file to MediaPlayer. Artwork travels the same
way through ``/api/art/``.

Streaming a LAN URL straight into Android 4.4's MediaPlayer is unreliable
(no auth header, no seek, opaque decoder errors), and an unauthenticated
static file server would expose the host to the whole network. Downloading
through the existing channel solves both.

Local library tracks are exposed as *symlinks*, not copies: a 60 MB lossless
file must never be read into the host's memory just to be handed to the
device. The server streams the target file in chunks instead.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from pathlib import Path

CONTENT_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Only names this module itself minted are ever served: no directory
# separators, no dot-dot, no arbitrary extension.
NAME_RE = re.compile(
    r"^[0-9a-f]{16}\.(mp3|wav|ogg|m4a|aac|flac|jpe?g|png|gif|webp)$")

# Downloaded clips (online songs, generated test tones, artwork) are bounded.
# Local library files are streamed from disk instead and are not subject to
# this limit.
MAX_BYTES = 24 * 1024 * 1024

# Cached entries are transient. Keep at most an hour's worth so a long-lived
# host does not fill the disk with songs nobody plays any more.
MAX_AGE = 3600.0


class AudioCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._sweep()

    def url_for(self, name):
        """Art uses its own route; everything else is an audio clip."""
        prefix = ("/api/art/" if Path(str(name)).suffix.lower()
                  in IMAGE_SUFFIXES else "/api/audio/")
        return prefix + str(name)

    def _descriptor(self, name, title, size):
        return {"name": name, "url": self.url_for(name),
                "title": title, "bytes": size}

    def put(self, data: bytes, suffix: str = ".mp3", title: str = "") -> dict:
        """Store one downloaded clip or image and return its descriptors."""
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError("empty media")
        if len(data) > MAX_BYTES:
            raise ValueError("media too large")
        suffix = str(suffix).lower()
        if suffix not in CONTENT_TYPES:
            raise ValueError("unsupported media format")
        name = secrets.token_hex(8) + suffix
        (self.root / name).write_bytes(bytes(data))
        with self.lock:
            self._sweep()
        return self._descriptor(name, title, len(data))

    def link(self, source: Path, suffix: str = ".mp3",
             title: str = "") -> dict:
        """Expose an existing local file without copying it.

        The caller has already checked that ``source`` came from the index of
        the configured music folder, so the symlink cannot point anywhere the
        user did not opt into.
        """
        source = Path(source)
        if not source.is_file():
            raise ValueError("file not found")
        suffix = str(suffix).lower()
        if suffix not in CONTENT_TYPES:
            raise ValueError("unsupported media format")
        name = secrets.token_hex(8) + suffix
        (self.root / name).symlink_to(source.resolve())
        with self.lock:
            self._sweep()
        return self._descriptor(name, title, source.stat().st_size)

    def path_for(self, name):
        """Resolve a minted name, or None. Never escapes the cache dir."""
        if not NAME_RE.match(str(name or "")):
            return None
        target = self.root / str(name)
        return target if target.is_file() else None

    def content_type(self, name):
        return CONTENT_TYPES.get(Path(str(name)).suffix.lower(),
                                "application/octet-stream")

    def _sweep(self):
        now = time.time()
        for path in self.root.glob("*.*"):
            try:
                # lstat, not stat: a symlink's target is an old music file
                # and must not make the link look expired on arrival.
                if now - path.lstat().st_mtime > MAX_AGE:
                    path.unlink()
            except OSError:
                pass

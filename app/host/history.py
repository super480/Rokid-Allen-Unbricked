"""The conversation of each assistant, kept on disk.

One file per assistant, one JSON object per line. Lines are only ever
appended, so a new line can never push an older one out. The 300-line
window the engine keeps in memory is for the live console; the file is
the record, and it has no limit.

A crash halfway through a write is not a disaster: it can leave at most
one broken line at the end of one file, and a broken line is skipped
when the file is read back. Every earlier line is still there.
"""

import collections
import json
import os
import re
import threading
from pathlib import Path

# A file name is made from the assistant's id. Anything that is not a
# plain letter, digit, dash or underscore becomes an underscore, so an
# id can never turn into a path that escapes this directory.
SAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")

# How many lines the engine keeps in memory for the live console.
MEMORY_WINDOW = 300


def safe_name(key):
    name = SAFE_NAME.sub("_", str(key or "").strip()).strip("_")
    return (name or "direct")[:80]


def _read_lines(path):
    """The valid objects in a jsonl file, oldest first.

    A line that cannot be read is skipped rather than raising: the only
    way to end up with one is a crash during the last write, and losing
    that one line is much better than losing the whole conversation.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    yield item
    except OSError:
        return


class HistoryStore:
    """Append-only per-assistant conversation files.

    ``root`` is the directory the state file lives in; the history goes
    into a ``history`` folder next to it.
    """

    def __init__(self, state_file):
        self.dir = Path(state_file).with_name("history") if state_file else None
        self._lock = threading.Lock()
        self._counts = {}
        self._speech_counts = {}
        self._max_seq = 0
        # One open append handle per assistant. Keeping it open makes a
        # write a single line plus a flush: cheap enough to do straight
        # away, so a line is on disk before the answer is spoken.
        self._handles = {}
        if self.dir is None:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        # One pass at start-up gives us the counts and the last number
        # used. The files are the only source of truth, so even a host
        # that was killed mid-write still resumes with the right number.
        for path in self.dir.glob("*.jsonl"):
            key = path.stem
            count = 0
            spoken = 0
            for item in _read_lines(path):
                count += 1
                if item.get("role") == "agent":
                    spoken += 1
                seq = item.get("seq")
                if isinstance(seq, int) and seq > self._max_seq:
                    self._max_seq = seq
            if count:
                self._counts[key] = count
                self._speech_counts[key] = spoken

    @property
    def max_seq(self):
        return self._max_seq

    def counts(self):
        with self._lock:
            return dict(self._counts)

    def speech_counts(self):
        """How many assistant replies each conversation holds.

        System notices ("已切换到 X") and the user's own lines stay on disk
        and can still be paged back, but they are not things the assistant
        said. The console's per-assistant counter shows only these.
        """
        with self._lock:
            return dict(self._speech_counts)

    def _path(self, key):
        if self.dir is None:
            return None
        return self.dir / (safe_name(key) + ".jsonl")

    def append(self, key, entry):
        """Add one line. This is the only way the file is ever written."""
        path = self._path(key)
        if path is None:
            return
        name = safe_name(key)
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        with self._lock:
            try:
                handle = self._handles.get(name)
                if handle is None:
                    created = not path.exists()
                    handle = path.open("a", encoding="utf-8")
                    if created:
                        # The file holds the user's own words: nobody else
                        # on this machine should be able to read them.
                        os.chmod(path, 0o600)
                    self._handles[name] = handle
                handle.write(line)
                handle.flush()
                self._counts[name] = self._counts.get(name, 0) + 1
                if entry.get("role") == "agent":
                    self._speech_counts[name] = (
                        self._speech_counts.get(name, 0) + 1)
                seq = entry.get("seq")
                if isinstance(seq, int) and seq > self._max_seq:
                    self._max_seq = seq
            except OSError:
                # The conversation in memory is still intact. A full disk
                # must not stop the assistant from answering.
                pass

    def close(self):
        with self._lock:
            for handle in self._handles.values():
                try:
                    handle.close()
                except OSError:
                    pass
            self._handles.clear()

    def since(self, seq):
        """Every line after ``seq``, across all assistants, by number.

        The console normally asks the 300-line memory window. A tab that
        slept through more than 300 lines would otherwise miss the ones
        that fell out of it, so those gaps come from the files instead.
        """
        found = []
        for path in self.dir.glob("*.jsonl") if self.dir else []:
            for item in _read_lines(path):
                number = item.get("seq")
                if isinstance(number, int) and number > seq:
                    found.append(item)
        found.sort(key=lambda item: (item.get("seq") or 0))
        return found

    def recent(self, key, limit=50):
        """The last ``limit`` lines of one assistant, oldest first."""
        path = self._path(key)
        if path is None or not path.exists():
            return []
        tail = collections.deque(maxlen=limit)
        for item in _read_lines(path):
            tail.append(item)
        return list(tail)

    def before(self, key, seq, limit=50):
        """The ``limit`` lines of one assistant just before ``seq``.

        Used by the console's "earlier messages" button. The result is
        oldest first, so the page can put it above what it already drew.
        """
        path = self._path(key)
        if path is None or not path.exists():
            return []
        tail = collections.deque(maxlen=limit)
        for item in _read_lines(path):
            number = item.get("seq")
            if isinstance(number, int) and number >= seq:
                break
            tail.append(item)
        return list(tail)

    def has_more(self, key, seq):
        """Is there anything in this assistant's file before ``seq``?"""
        path = self._path(key)
        if path is None or not path.exists():
            return False
        for item in _read_lines(path):
            number = item.get("seq")
            if isinstance(number, int) and number < seq:
                return True
        return False

    def window(self, limit=MEMORY_WINDOW):
        """The newest ``limit`` lines across all assistants, by number.

        This is what the engine keeps in memory after a restart: the
        console asks for "everything after number N", and N can be any
        number it last saw, so the window has to be in the same order as
        the numbers.
        """
        merged = []
        for path in self.dir.glob("*.jsonl") if self.dir else []:
            tail = collections.deque(maxlen=limit)
            for item in _read_lines(path):
                tail.append(item)
            merged.extend(tail)
        merged.sort(key=lambda item: (item.get("seq") or 0))
        return merged[-limit:]

    def import_legacy(self, entries, thread_key):
        """Bring the old single-file conversation over, once.

        The old format kept every assistant in one capped file. Split it
        by assistant so no assistant can push another one out again.
        """
        if not entries:
            return 0
        existing = set(self._counts)
        if existing:
            # Already imported (or already in use): never duplicate.
            return 0
        written = 0
        for item in entries:
            if not isinstance(item, dict):
                continue
            try:
                seq = int(item.get("seq") or 0)
            except (TypeError, ValueError):
                continue
            if seq <= 0:
                continue
            entry = dict(item)
            entry["seq"] = seq
            self.append(thread_key(entry.get("agent")), entry)
            written += 1
        return written

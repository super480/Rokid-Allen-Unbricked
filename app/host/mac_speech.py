"""The Mac's own voice, for ear-only mode.

Ear-only mode has two halves and this module is the second one: the speaker
stops reading the catgirl's answers, so *something* on the Mac has to read
them. N.E.K.O.'s desktop client cannot do it for us -- the upstream server
delivers a turn's text and TTS audio only to the WebSocket connection that
started that turn, and the host's bridge connection is the one that starts
them (see docs/research/neko-ear-mode-assessment.md). So the host speaks.

``say`` is the macOS speech synthesiser: it is already installed, needs no
network, starts in a few milliseconds, and can be killed mid-sentence. The
catgirl's own timbre would need the upstream audio frames instead; that is a
later phase and would plug in here without touching the engine.

Why this module also carries :func:`dodge`: the speaker's microphone is a
few centimetres from the Mac's, so everything said here is heard by the wake
engine as well. A reply that contains a wake word ("猫娘在呢") wakes the
speaker from the far end of the room. The echo window in the engine stops
that from *doing* anything, but dodging the word avoids the flash entirely.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time

#: macOS's built-in Mandarin voice. Present on every stock install; the
#: novelty voices (Eddy, Flo, ...) are not always downloaded.
DEFAULT_VOICE = "Tingting"

#: Words per minute. ``say``'s default is 175, which is a little slow for a
#: companion that is supposed to feel alive.
DEFAULT_RATE = 180

#: Rough speaking speed, used only to size an echo window before the
#: subprocess reports that it finished. Deliberately pessimistic.
CHARS_PER_SECOND = 4.0


def dodge(text, words, replacement="人家"):
    """Rewrite wake words out of a line before the Mac says it.

    The speaker's wake engine matches on pinyin, so a line that contains one
    of the registered words wakes it up even when the user said nothing.
    Longest words go first, otherwise ``猫娘`` would be clipped by ``猫``
    (longest-first also keeps a shorter alias from mangling a longer one).
    Latin spellings are matched case-insensitively.
    """
    result = str(text or "")
    if not result:
        return ""
    for word in sorted({str(w).strip() for w in (words or []) if str(w).strip()},
                       key=len, reverse=True):
        if word.isascii():
            result = re.sub(re.escape(word), replacement, result, flags=re.IGNORECASE)
        else:
            result = result.replace(word, replacement)
    return result


class MacSpeech:
    """One ``say`` at a time, with a handle to stop it.

    ``runner`` is the seam for tests: it returns an object with ``stdin``,
    ``wait()`` and ``terminate()`` (a ``subprocess.Popen`` satisfies it), so
    no test ever needs to make a sound.
    """

    def __init__(self, voice=DEFAULT_VOICE, rate=DEFAULT_RATE, runner=None,
                 clock=time.time):
        self.voice = voice or DEFAULT_VOICE
        try:
            self.rate = max(80, min(400, int(rate or DEFAULT_RATE)))
        except (TypeError, ValueError):
            self.rate = DEFAULT_RATE
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._process = None
        self._thread = None
        self.started_at = 0.0
        self.last_text = ""

    # -- capability -----------------------------------------------------------

    def available(self):
        """True when a voice can actually be produced right now."""
        if self._runner is not None:
            return True
        return shutil.which("say") is not None

    def estimate(self, text):
        """Seconds, for sizing an echo window before the process reports."""
        chars = len(str(text or ""))
        pace = CHARS_PER_SECOND * (self.rate / float(DEFAULT_RATE))
        return max(1.5, chars / max(1.0, pace) + 1.0)

    @property
    def busy(self):
        with self._lock:
            return self._process is not None

    # -- speaking -------------------------------------------------------------

    def speak(self, text, on_done=None):
        """Say ``text`` in the background. Returns a description, or None.

        A new line always replaces the previous one: two overlapping replies
        would be unintelligible, and the newer answer is the true state.
        ``on_done(ok)`` runs when the process exits (also when stopped), so
        the caller can close whatever window it opened.
        """
        text = str(text or "").strip()
        if not text:
            return None
        self.stop()
        payload = {"engine": "say", "voice": self.voice, "rate": self.rate,
                   "chars": len(text)}
        with self._lock:
            self.started_at = self._clock()
            self.last_text = text
            self._thread = threading.Thread(target=self._run, args=(text, on_done),
                                            daemon=True)
            thread = self._thread
        thread.start()
        return payload

    def _spawn(self):
        if self._runner is not None:
            return self._runner()
        if shutil.which("say") is None:
            raise RuntimeError("这台 Mac 上没有 say 命令")
        argv = ["say", "-v", self.voice, "-r", str(self.rate)]
        return subprocess.Popen(argv, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)

    def _run(self, text, on_done):
        process = None
        ok = False
        try:
            process = self._spawn()
            with self._lock:
                self._process = process
            stream = getattr(process, "stdin", None)
            if stream is not None:
                # Text on stdin: no argv length limit and no option parsing
                # on a line that happens to start with a dash.
                data = text.encode("utf-8")
                try:
                    stream.write(data)
                except TypeError:
                    stream.write(text)
                try:
                    stream.close()
                except Exception:
                    pass
            process.wait()
            ok = True
        except Exception:
            ok = False
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
            if on_done is not None:
                try:
                    on_done(ok)
                except Exception:
                    pass

    def stop(self):
        """Cut the current line off. True when something was playing."""
        with self._lock:
            process = self._process
            self._process = None
        if process is None:
            return False
        for action in ("terminate", "kill"):
            try:
                getattr(process, action)()
                return True
            except Exception:
                continue
        return False

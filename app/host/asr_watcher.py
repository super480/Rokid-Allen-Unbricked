#!/usr/bin/env python3
"""Read the final ASR lines and the matched wake word; no firmware change.

The Rokid event bus does not always carry the user's own words -- it often
delivers the factory's NLP result instead. The reliable source is the final
recognition line in the device log, which this watcher tails over ADB and
forwards to the host. The same log also says which wake word was matched,
which is what selects an assistant: the NLP layer strips the word off, so
the recognised sentence itself cannot tell us who was called.

Nothing is recorded from the microphone; the factory does the recording.
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

FINAL = re.compile(
    r"^\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}.*?SirenJava.*?toStringResult: \+ type 4 action null nlp null"
    r"\s+location .*? extype \d+ activation \d+ asr (.*?) energy [-\d.]+\s*$"
)

# The wake engine prints the word it matched before the recogniser starts:
#   I/BlackSiren: vt word with 若琪|洛奇 [3200, 9280]11905.688477
#   D/speech.speech: VoiceOptions: ... voice_trigger(若琪|洛奇), ...
# The NLP result that follows has that word removed ("休息吧" for
# "若琪休息吧"), so the ASR text alone cannot tell which assistant the
# speaker called. The label in these lines is the vt_word we registered.
LOG_SPEC = "SirenJava:I BlackSiren:I speech.speech:D"
VT_WORD = re.compile(r"\bvt word with\s+(.+?)\s*(?:\[|$)")
VOICE_TRIGGER = re.compile(r"voice_trigger\(([^)]*)\)")

# How often to check that the device-side bridge service is still alive.
# A killed service does not necessarily break the logcat stream this watcher
# tails, so reconnect-only recovery is not enough: a silent bridge death
# otherwise leaves the speaker with no avatar, no TTS and no command polling
# until something else happens to restart logcat.
BRIDGE_HEALTH_INTERVAL = 300.0

def device_serial(value):
    """Normalise --device to one adb serial, never appending a port twice.

    ``manage.py`` passes the value through from its own --device flag, and
    that flag is documented in two forms: a bare IP (the manage default)
    and a full serial (used by scripts/doctor.py and config.local.json's
    "device"). adb -s needs the full serial in both cases, so a bare IP
    gets :5555 while an existing host:port is left alone. Appending :5555
    unconditionally produced 192.0.2.31:5555:5555, which adb rejects
    with "unknown host service '5555:features'" -- the watcher then loops
    forever and no wake word ever reaches the host.
    """
    value = str(value or "").strip()
    if not value:
        raise ValueError("请先输入若琪 IP")
    if ":" in value:
        return value
    return value + ":5555"


def is_playback_status(line):
    """A VoiceOptions line printed while our own TTS is playing.

    ``speech.speech`` reprints VoiceOptions (including the previous
    ``voice_trigger`` label) on every playback state change, not only when
    the wake engine matched a word. The ``"voice":{"state":"PLAYING"}``
    marker says the speaker is talking to itself; forwarding that line as a
    fresh wake re-selects an assistant on every state update. A real
    ``vt word with`` hit from BlackSiren is unaffected -- that line has no
    VoiceOptions prefix.
    """
    return ("VoiceOptions" in line and "vt word with" not in line
            and re.search(r'"state"\s*:\s*"PLAYING"', line) is not None)


def parse_wake_word(line):
    """Return the wake word the engine just matched, or None.

    Both lines carry the same label, and the factory word is stored with
    its aliases separated by a pipe ("若琪|洛奇"). Keep the label whole:
    the host splits it and decides whether it belongs to an assistant.
    """
    match = VT_WORD.search(line) or VOICE_TRIGGER.search(line)
    if not match:
        return None
    return match.group(1).strip() or None


def parse_final(line):
    """Return the recognised sentence, or None when the line is not final."""
    match = FINAL.match(line)
    if not match:
        return None
    text = match.group(1).strip()
    if text in ("", "null"):
        return None
    text = re.sub(r"^(?:若琪|若弃|洛奇|罗琪)[，,。\s]*", "", text).strip()
    return text or None


def stamp(line):
    """Turn the logcat timestamp into a datetime, or None when unparsable."""
    try:
        today = datetime.datetime.now().year
        return datetime.datetime.strptime(str(today) + "-" + line[:18], "%Y-%m-%d %H:%M:%S.%f")
    except ValueError:
        return None


def ensure_bridge(base, running=lambda: True):
    """Restart the device bridge service when it is not running.

    Two callers use this: the logcat reconnect loop and a periodic health
    check. The health check is needed because `am force-stop` silences the
    service without closing the logcat stream. `am startservice` is a no-op
    when the service is already up, so a false alarm is harmless.
    """
    if not running():
        return
    # Ask Android first. A process-name match is enough: the service and
    # its host process share the package name and `ps <pkg>` is the only
    # process query this old device shell supports.
    try:
        found = subprocess.run(
            base + ["ps cn.rokid.bridge"],
            capture_output=True, text=True, timeout=10)
        lines = (found.stdout or "").splitlines()
        if any("cn.rokid.bridge" in line and "USER" not in line
               for line in lines[1:]):
            return
    except Exception:
        # An unreachable device cannot be healed from here; the reconnect
        # loop will try again on the next pass.
        return
    try:
        result = subprocess.run(
            base + ["am startservice -n cn.rokid.bridge/.BridgeService"],
            capture_output=True, text=True, timeout=10)
        stderr = (result.stderr or "").strip()
        if stderr and ("Error" in stderr or "error" in stderr):
            print("bridge start reported: " + stderr, flush=True)
        else:
            print("bridge service start requested (self-heal)", flush=True)
    except Exception as error:
        print("bridge start skipped: " + type(error).__name__, flush=True)


def main():
    parser = argparse.ArgumentParser(description="Forward final ASR lines to the host")
    parser.add_argument("--device", required=True)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[2]
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    adb = os.environ.get("ROKID_ADB", "adb")

    # Only one watcher may run. Several of them report the very same log line
    # and the host would then show one question dozens of times.
    # flock is released by the kernel when the process dies, so a crashed
    # watcher never leaves a stale lock behind.
    lock_dir = Path(args.config).resolve().parent / "runtime"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "asr-watcher.lock"
    lock_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("ASR watcher already running; this instance exits.", flush=True)
        return
    os.ftruncate(lock_fd, 0)
    os.write(lock_fd, str(os.getpid()).encode())

    base = [adb, "-s", device_serial(args.device), "shell"]
    process = None
    running = True
    health_stop = threading.Event()

    def stop(*_):
        nonlocal running
        running = False
        health_stop.set()
        if process and process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    def bridge_health():
        """Periodic belt-and-braces recovery for a silently dead service."""
        while not health_stop.wait(BRIDGE_HEALTH_INTERVAL):
            ensure_bridge(base, lambda: running)

    threading.Thread(target=bridge_health, name="bridge-health",
                     daemon=True).start()

    session = uuid.uuid4().hex
    counter = 0
    # One wake prints two lines (BlackSiren and openvoice). Report it once.
    last_wake_word, last_wake_at = "", 0.0

    def deliver(payload, label):
        for attempt in range(3):
            try:
                request = Request(
                    "http://127.0.0.1:" + str(cfg.get("port", 8765)) + "/api/event",
                    data=payload,
                    headers={"Authorization": "Bearer " + cfg["token"],
                             "Content-Type": "application/json"})
                with urlopen(request, timeout=6) as response:
                    result = json.load(response)
                # Keep the recognised text (or the matched wake label) in
                # the log. Without it a new cloud misrecognition is
                # invisible: the transcript only shows what reached an
                # agent, and a fake activation cuts the turn before that.
                try:
                    sent = json.loads(payload.decode("utf-8"))
                except Exception:
                    sent = {}
                detail = str(sent.get("text") or sent.get("word") or "")[:80]
                print(label + " delivered; intent=" +
                      result.get("intent", "unknown") +
                      (" text=" + repr(detail) if detail else ""), flush=True)
                return True
            except Exception as error:
                if attempt == 2:
                    print(label + " delivery failed: " + type(error).__name__,
                          flush=True)
                time.sleep(.5 * (attempt + 1))
        return False

    while running:
        # Self-heal: whenever logcat breaks (device reboot or crash) the
        # reconnect loop lands here, so bring the bridge service back before
        # re-attaching. `am startservice` is a no-op if it is already up.
        ensure_bridge(base)
        try:
            # Snapshot before attaching. Never replay old buffered speech or
            # a wake word from before the watcher started -- the same spec
            # is used for the live tail, so the cutoff covers every tag.
            old = subprocess.check_output(
                base + ["logcat -d -v threadtime -s " + LOG_SPEC],
                text=True, timeout=10).splitlines()
            seen = {hashlib.sha256(x.encode()).hexdigest() for x in old}
            stamps = [t for x in old if (t := stamp(x)) is not None]
            cutoff = max(stamps) if stamps else datetime.datetime.now()
            process = subprocess.Popen(
                base + ["logcat -v threadtime -s " + LOG_SPEC],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace")
            print("ASR watcher ready (historical buffer excluded)", flush=True)
            for raw in process.stdout:
                if not running:
                    break
                line = raw.rstrip("\r\n")
                key = hashlib.sha256(line.encode()).hexdigest()
                moment = stamp(line)
                if key in seen or moment is None or moment < cutoff:
                    continue
                if len(seen) > 8192:
                    seen.clear()
                seen.add(key)
                text = parse_final(line)
                # A status line from our own playback repeats the last
                # trigger label; it is not a new wake. Skip it before the
                # parser turns it into one.
                if is_playback_status(line):
                    continue
                word = None if text else parse_wake_word(line)
                if not text and not word:
                    continue
                if word:
                    now = time.time()
                    if word == last_wake_word and now - last_wake_at < 4:
                        continue
                    last_wake_word, last_wake_at = word, now
                counter += 1
                payload = {
                    "type": "asr_final" if text else "wake_word",
                    "text": text or "",
                    "source": "siren-final-log" if text else "wake-log",
                    "request_id": ("asr-" if text else "wake-") + session
                                  + "-" + str(counter),
                }
                if word:
                    payload["word"] = word
                deliver(json.dumps(payload, ensure_ascii=False).encode(),
                        "Final ASR" if text else "Wake word")
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        except Exception as error:
            print("ASR watcher reconnecting: " + type(error).__name__, flush=True)
        if running:
            time.sleep(2)


if __name__ == "__main__":
    main()

"""Read-only Mac metrics: CPU load, memory used and network throughput.

Nothing here writes to the machine. The primary sampler uses ``psutil``
(installed on this Mac); a pure standard-library fallback parses
``ps`` / ``sysctl`` / ``vm_stat`` / ``netstat -ib`` so the two new query
apps still work if psutil is missing.

Throughput is a *delta between two consecutive counter samples*, taken by a
one-second daemon sampler. A query therefore returns immediately with a
number that describes the last second, instead of blocking for a full
second or reporting an average since the previous query. CPU percentage
behaves the same way: the first ``psutil.cpu_percent(interval=None)`` has
no baseline and is deliberately discarded.

The sampler is injectable, so tests drive the exact same arithmetic with a
fake clock and fake counters -- no real machine reads, no sleeps.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

MB = 1024.0 * 1024.0
KB = 1024.0
GB = 1024.0 * 1024.0 * 1024.0
DEFAULT_FULL_SCALE_MBPS = 20.0
DEFAULT_SAMPLE_SECONDS = 1.0
# Auto-ranging steps in KB/s. A fixed 20 MB/s scale draws a 0.7% arc for a
# typical 140 KB/s home link (invisible), while the design's 12.8 MB/s
# example still lands on its exact "20 MB/s" step. Steps start at 128 KB/s
# so quiet links keep a visible arc; the configured full scale is the cap
# and the footer always names the step actually used.
SCALE_STEPS_KBPS = (128, 256, 512, 1024, 2048, 5120,
                    10240, 20480, 51200, 102400)

_PHYSICAL = re.compile(r"^en\d+$")
_VIRTUAL_PREFIXES = ("lo", "utun", "awdl", "gif", "stf", "anpi",
                     "bridge", "llw", "p2p", "utap", "ipsec")


class MetricsError(Exception):
    """A read that cannot produce trustworthy numbers, with a stable code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Sample:
    """One instantaneous snapshot of the machine's counters."""
    at: float                        # time.monotonic(), not wall clock
    cpu_percent: float | None = None
    mem_used_bytes: int | None = None
    mem_total_bytes: int | None = None
    io: dict[str, tuple[int, int]] = field(default_factory=dict)  # rx, tx


@dataclass(frozen=True)
class Reading:
    """A derived, displayable view (throughput needs two samples)."""
    at_wall: float
    cpu_percent: float | None
    mem_used_bytes: int | None
    mem_total_bytes: int | None
    down_bps: float
    up_bps: float
    interface: str

    @property
    def partial(self) -> bool:
        return (self.cpu_percent is None or self.mem_used_bytes is None
                or self.mem_total_bytes is None)


class PsutilSampler:
    """Primary path: the psutil module already installed on this Mac."""

    def __init__(self, module=None):
        if module is None:
            import psutil as module  # noqa: F811
        self.psutil = module
        self._cpu_ready = False

    def read(self) -> Sample:
        p = self.psutil
        # The first non-blocking call has no baseline and returns a bogus 0.
        cpu = float(p.cpu_percent(interval=None))
        cpu_value = clamp(cpu, 0.0, 100.0) if self._cpu_ready else None
        self._cpu_ready = True
        vm = p.virtual_memory()
        io: dict[str, tuple[int, int]] = {}
        for name, counters in (p.net_io_counters(pernic=True) or {}).items():
            io[name] = (int(counters.bytes_recv), int(counters.bytes_sent))
        return Sample(at=time.monotonic(), cpu_percent=cpu_value,
                      mem_used_bytes=int(vm.used), mem_total_bytes=int(vm.total),
                      io=io)


class CommandSampler:
    """Fallback: only Python's standard library plus macOS CLI tools.

    ``top``/``ps`` are blocked in the agent sandbox, so CPU comes from the
    second (one-second) sample of ``iostat -c 2 -w 1``. ``netstat -ibn``
    is used instead of ``-ib``: on this Mac the latter spends ~5 seconds
    resolving names, while ``-n`` returns the same counters in 10ms.
    """

    @staticmethod
    def _run(argv: list[str], timeout: float = 5.0) -> str:
        try:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=timeout)
            return out.stdout or ""
        except (OSError, subprocess.SubprocessError):
            return ""

    def read(self) -> Sample:
        cpu = self._cpu()
        used, total = self._memory()
        return Sample(at=time.monotonic(), cpu_percent=cpu,
                      mem_used_bytes=used, mem_total_bytes=total,
                      io=self._net())

    def _cpu(self) -> float | None:
        # Two samples one second apart; the LAST line is the live window.
        text = self._run(["iostat", "-c", "2", "-w", "1"], timeout=8.0)
        for line in reversed(text.splitlines()):
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                us, sy = float(parts[3]), float(parts[4])
            except ValueError:
                continue
            return clamp(us + sy, 0.0, 100.0)
        return None

    def _memory(self) -> tuple[int | None, int | None]:
        total_text = self._run(["sysctl", "-n", "hw.memsize"]).strip()
        try:
            total = int(total_text)
        except ValueError:
            total = None
        text = self._run(["vm_stat"])
        page_size = 4096
        match = re.search(r"page size of (\d+) bytes", text)
        if match:
            page_size = int(match.group(1))
        keys = ("Pages active", "Pages speculative", "Pages wired down",
                "Pages occupied by compressor")
        pages = {}
        for line in text.splitlines():
            for key in keys:
                if line.startswith(key + ":"):
                    number = re.search(r"(\d+)", line.split(":", 1)[1])
                    if number:
                        pages[key] = int(number.group(1))
        if not pages or total is None:
            return None, total
        return min(total, int(sum(pages.values()) * page_size)), total

    def _net(self) -> dict[str, tuple[int, int]]:
        io: dict[str, tuple[int, int]] = {}
        for line in self._run(["netstat", "-ibn"]).splitlines():
            parts = line.split()
            if not parts:
                continue
            link = next((i for i, part in enumerate(parts)
                         if part.startswith("<Link#")), None)
            if link is None:
                continue
            rest = parts[link + 1:]
            if rest and ":" in rest[0]:        # MAC address column
                rest = rest[1:]
            if len(rest) < 6:
                continue
            try:
                rx, tx = int(rest[2]), int(rest[5])
            except ValueError:
                continue
            io[parts[0].rstrip("*")] = (rx, tx)
        return io


def default_sampler():
    try:
        return PsutilSampler()
    except ImportError:
        return CommandSampler()


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def pick_interfaces(io: dict[str, tuple[int, int]]) -> list[str]:
    """Physical ethernet/Wi-Fi interfaces carrying the real traffic.

    VPN tunnels (utun*) and loopback are excluded on purpose: on this Mac the
    default route is a tunnel, but en0 still sees the encapsulated bytes.
    """
    names = [name for name in io if _PHYSICAL.match(name)]
    active = [name for name in names if sum(io[name]) > 0]
    if active:
        return active
    if names:
        return names
    others = [name for name in io
              if sum(io[name]) > 0
              and not name.startswith(_VIRTUAL_PREFIXES)]
    return others[:1]


def format_rate(bps: float) -> str:
    """Human-facing throughput. Design稿 uses MB/s = MiB/s."""
    if bps >= MB:
        return "%.1f MB/s" % (bps / MB)
    if bps >= KB:
        return "%.0f KB/s" % (bps / KB)
    return "%.0f B/s" % max(0.0, bps)


def format_memory(used_bytes: int | None, total_bytes: int | None) -> str:
    if used_bytes is None or total_bytes is None or total_bytes <= 0:
        return "—"
    return "%.1f / %.0f GB" % (used_bytes / GB, total_bytes / GB)


class MacSystemProvider:
    """One-second sampler + the read() contract MiniAppService expects."""

    def __init__(self, sampler=None, config: dict | None = None,
                 autostart: bool = True, sleep=time.sleep,
                 interval: float = DEFAULT_SAMPLE_SECONDS):
        settings = config or {}
        self.sampler = sampler if sampler is not None else default_sampler()
        self.interval = float(interval)
        self._sleep = sleep
        self.full_scale_mbps = clamp(
            float(settings.get("network_full_scale_mbps")
                  or DEFAULT_FULL_SCALE_MBPS), 1.0, 1000.0)
        self._lock = threading.RLock()
        self._prev: Sample | None = None
        self._reading: Reading | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if autostart:
            self.start()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._loop,
                                            name="mac-metrics", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.interval)

    def poll(self) -> None:
        """Take one sample; derive a reading when a previous one exists."""
        sample = self.sampler.read()
        with self._lock:
            prev, self._prev = self._prev, sample
            if prev is None:
                return
            elapsed = sample.at - prev.at
            # Two polls only milliseconds apart produce a meaningless
            # delta/elapsed spike (or a bogus zero). This also protects
            # against a cold-start poll interleaving with the daemon's.
            if elapsed < self.interval * 0.5:
                return
            down, up, names = self._delta(prev.io, sample.io, elapsed)
            self._reading = Reading(
                at_wall=time.time(), cpu_percent=sample.cpu_percent,
                mem_used_bytes=sample.mem_used_bytes,
                mem_total_bytes=sample.mem_total_bytes,
                down_bps=down, up_bps=up, interface=",".join(names))

    @staticmethod
    def _delta(before: dict[str, tuple[int, int]],
               after: dict[str, tuple[int, int]], elapsed: float
               ) -> tuple[float, float, list[str]]:
        names = pick_interfaces(after)
        down = up = 0.0
        used: list[str] = []
        for name in names:
            old = before.get(name)
            new = after.get(name)
            if old is None or new is None:
                continue
            drx, dtx = new[0] - old[0], new[1] - old[1]
            # A negative delta means the interface was reset / counter
            # wrapped. Never show a negative speed; skip this sample.
            if drx < 0 or dtx < 0:
                continue
            down += drx / elapsed
            up += dtx / elapsed
            used.append(name)
        return down, up, used

    def _auto_scale(self, peak_bps: float) -> float:
        """Smallest readable KB/s step that fits the current peak rate."""
        cap = self.full_scale_mbps * 1024.0
        peak = max(0.0, peak_bps) / 1024.0
        for step in SCALE_STEPS_KBPS:
            if step > cap:
                break
            if peak <= step * 0.8:      # leave 20% headroom before the cap
                return float(step)
        return cap

    def read(self, operation: str = "") -> dict[str, Any]:
        """Return an already-normalised payload for the card builder."""
        if operation not in ("resources", "speed"):
            raise MetricsError("field_error", "unknown system operation")
        with self._lock:
            reading = self._reading
        if reading is None:
            if self._thread is None:
                # No daemon (poll-driven use / unit tests): take two samples
                # with the configured interval so the speed is real.
                self.poll()
                self._sleep(self.interval)
                self.poll()
            else:
                # The daemon owns the clock. Never poll here: an extra poll
                # a few milliseconds after the daemon's would overwrite the
                # good one-second reading with a near-zero delta.
                for _ in range(4):
                    self._sleep(min(self.interval, 1.0))
                    with self._lock:
                        reading = self._reading
                    if reading is not None:
                        break
            with self._lock:
                reading = self._reading
        if reading is None:
            raise MetricsError("unavailable", "本机系统采样尚未就绪")
        cpu = reading.cpu_percent
        mem_used = reading.mem_used_bytes
        mem_total = reading.mem_total_bytes
        mem_percent = (clamp(mem_used / mem_total * 100.0, 0.0, 100.0)
                       if mem_used is not None and mem_total else None)
        scale_kbps = self._auto_scale(max(reading.down_bps, reading.up_bps))
        full = scale_kbps * 1024.0
        return {
            "cpu_percent": cpu,
            "mem_percent": mem_percent,
            "mem_used_bytes": mem_used,
            "mem_total_bytes": mem_total,
            "down_bps": max(0.0, reading.down_bps),
            "up_bps": max(0.0, reading.up_bps),
            "down_percent": clamp(reading.down_bps / full * 100.0, 0.0, 100.0),
            "up_percent": clamp(reading.up_bps / full * 100.0, 0.0, 100.0),
            "full_scale_mbps": scale_kbps / 1024.0,
            "full_scale_kbps": scale_kbps,
            "full_scale_cap_mbps": self.full_scale_mbps,
            "interface": reading.interface,
            "sampled_at": reading.at_wall,
            "partial": reading.partial,
            "has_network": bool(reading.interface),
            "source": "mac_local_metrics",
        }

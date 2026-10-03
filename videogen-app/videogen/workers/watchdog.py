"""Watchdog: hard timeout + stall + livelock detection (ARCHITECTURE.md §7).

Two separate notions:
  * RUNNING          — the process is alive;
  * MAKING PROGRESS  — a progress signal changed recently.

Signals: FFmpeg progress marker (frames / output time), output file size,
filesystem activity (mtime), CPU time. CPU alone keeps a process "alive" for
at most ``livelock_factor × stall`` — a process spinning at 100 % CPU
without producing frames is still declared hung.

The class is pure bookkeeping driven by an injectable clock, so it is
unit-testable without real processes; :mod:`videogen.ffmpeg_ctl.runner`
feeds it real observations.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

CPU_EPSILON_S = 0.5


@dataclass(frozen=True)
class Verdict:
    kind: str          # "hard_timeout" | "stall" | "livelock"
    detail: str


class Watchdog:
    def __init__(self, *, hard_s: float, stall_s: float | None, livelock_factor: float = 2.0,
                 cpu_epsilon_s: float = CPU_EPSILON_S, clock: Callable[[], float] = time.monotonic) -> None:
        if hard_s <= 0:
            raise ValueError("hard timeout must be positive")
        self.hard_s = hard_s
        self.stall_s = stall_s
        self.livelock_s = stall_s * livelock_factor if stall_s else None
        self.cpu_epsilon_s = cpu_epsilon_s
        self.clock = clock
        self.started = clock()
        self.last_real = self.started      # progress / size / fs change
        self.last_cpu = self.started       # CPU time advanced
        self._marker: Any = None
        self._size: int | None = None
        self._mtime: float | None = None
        self._cpu: float | None = None
        self.observations = 0

    def observe(self, *, marker: Any = None, out_size: int | None = None, fs_mtime: float | None = None,
                cpu_time: float | None = None) -> None:
        now = self.clock()
        self.observations += 1
        if marker is not None and marker != self._marker:
            self._marker = marker
            self.last_real = now
        if out_size is not None and (self._size is None or out_size > self._size):
            if self._size is not None:
                self.last_real = now
            self._size = out_size
        if fs_mtime is not None and (self._mtime is None or fs_mtime > self._mtime):
            if self._mtime is not None:
                self.last_real = now
            self._mtime = fs_mtime
        if cpu_time is not None:
            if self._cpu is None or cpu_time - self._cpu >= self.cpu_epsilon_s:
                if self._cpu is not None:
                    self.last_cpu = now
                self._cpu = cpu_time

    def touch(self) -> None:
        """Explicit progress (e.g. a worker answered a request)."""
        self.last_real = self.clock()

    def verdict(self) -> Verdict | None:
        now = self.clock()
        elapsed = now - self.started
        if elapsed > self.hard_s:
            return Verdict("hard_timeout", f"exceeded hard timeout {self.hard_s:.0f}s")
        if self.stall_s is None:
            return None
        idle_real = now - self.last_real
        idle_any = now - max(self.last_real, self.last_cpu)
        if idle_any > self.stall_s:
            return Verdict("stall", f"no progress and no CPU activity for {idle_any:.0f}s")
        if self.livelock_s is not None and idle_real > self.livelock_s:
            return Verdict("livelock", f"CPU busy but no progress for {idle_real:.0f}s")
        return None

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "elapsed_s": round(now - self.started, 2),
            "idle_progress_s": round(now - self.last_real, 2),
            "idle_cpu_s": round(now - self.last_cpu, 2),
            "hard_s": self.hard_s, "stall_s": self.stall_s, "livelock_s": self.livelock_s,
            "last_marker": repr(self._marker), "last_size": self._size, "cpu_time": self._cpu,
        }

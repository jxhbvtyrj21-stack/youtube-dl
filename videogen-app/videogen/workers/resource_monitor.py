"""RAM / CPU / disk monitoring (ARCHITECTURE.md §9.1, §9.4, §37).

A background thread samples system resources at a fixed interval; the queue
asks :meth:`ResourceMonitor.probe` before starting work. Sampling is cheap
and never blocks the caller (probe returns the latest sample).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from videogen.config.settings import ResourceLimits
from videogen.core.queue_manager import ResourceStatus
from videogen.utils.system import free_disk_bytes

log = logging.getLogger(__name__)
MB = 1024 * 1024


@dataclass(frozen=True)
class ResourceSample:
    ts: float
    ram_available_mb: float
    ram_percent: float
    tree_rss_mb: float
    cpu_percent: float
    disk_free_mb: dict[str, float] = field(default_factory=dict)


def sample(paths: list[Path], pid: int | None = None) -> ResourceSample:
    vm = psutil.virtual_memory()
    tree = 0
    try:
        root = psutil.Process(pid or os.getpid())
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        procs = []
    for p in procs:
        try:
            tree += p.memory_info().rss
        except psutil.Error:
            continue
    disks: dict[str, float] = {}
    for p in paths:
        try:
            disks[str(p)] = free_disk_bytes(p) / MB
        except OSError:
            disks[str(p)] = -1.0
    return ResourceSample(time.time(), vm.available / MB, vm.percent, tree / MB,
                          psutil.cpu_percent(interval=None), disks)


def evaluate(s: ResourceSample, limits: ResourceLimits) -> ResourceStatus:
    if s.ram_available_mb < limits.ram_available_min_mb:
        return ResourceStatus(False, "ram",
                              f"Мало вільної оперативної пам'яті: {s.ram_available_mb:.0f} МБ "
                              f"(потрібно щонайменше {limits.ram_available_min_mb} МБ).")
    if s.tree_rss_mb > limits.ram_tree_max_mb:
        return ResourceStatus(False, "ram",
                              f"Програма використовує забагато пам'яті: {s.tree_rss_mb:.0f} МБ "
                              f"(ліміт {limits.ram_tree_max_mb} МБ).")
    for path, free in s.disk_free_mb.items():
        if 0 <= free < limits.disk_reserve_mb:
            return ResourceStatus(False, "disk",
                                  f"Недостатньо вільного місця на диску ({path}): {free:.0f} МБ.")
    return ResourceStatus(True)


class ResourceMonitor:
    def __init__(self, limits: ResourceLimits, paths: list[Path], *, pid: int | None = None) -> None:
        self.limits = limits
        self.paths = list(paths)
        self.pid = pid
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest: ResourceSample | None = None
        self._thread: threading.Thread | None = None
        self.history: list[ResourceSample] = []
        self.history_max = 1800

    def start(self) -> None:
        psutil.cpu_percent(interval=None)   # prime the CPU counter
        self._take()
        self._thread = threading.Thread(target=self._run, name="resource-monitor", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _take(self) -> ResourceSample:
        s = sample(self.paths, self.pid)
        with self._lock:
            self._latest = s
            self.history.append(s)
            if len(self.history) > self.history_max:
                del self.history[: len(self.history) - self.history_max]
        return s

    def _run(self) -> None:
        while not self._stop.wait(self.limits.monitor_interval_s):
            try:
                self._take()
            except Exception:  # noqa: BLE001 - monitoring must never die silently
                log.exception("resource sampling failed")

    def latest(self) -> ResourceSample:
        with self._lock:
            s = self._latest
        return s if s is not None else self._take()

    def probe(self) -> ResourceStatus:
        """For QueueManager: fresh sample (cheap) evaluated against limits."""
        return evaluate(self._take(), self.limits)


# ---------------------------------------------------------------- disk pre-flight (§9.4)

@dataclass(frozen=True)
class DiskEstimate:
    workspace_bytes: int
    output_bytes: int


def estimate_job_disk(*, n_images: int, width: int, height: int, overscan: float, duration_s: float,
                      video_bitrate_bps: int = 8_000_000, audio_bitrate_bps: int = 192_000,
                      sample_rate: int = 48000, channels: int = 2, archive_bytes: int = 0) -> DiskEstimate:
    norm = int(n_images * width * height * overscan * overscan * 0.4)
    wav = int(duration_s * sample_rate * channels * 2)
    segments = int(duration_s * video_bitrate_bps / 8)
    final = int(duration_s * (video_bitrate_bps + audio_bitrate_bps) / 8)
    # temp output lives in the workspace; final copy lands in output (x2 safety)
    return DiskEstimate(workspace_bytes=norm + wav + segments + final,
                        output_bytes=final * 2 + archive_bytes)


def check_disk(est: DiskEstimate, workspace: Path, output: Path, reserve_mb: int) -> None:
    """Raise DiskSpaceError with a clear message if the job cannot fit."""
    from videogen.core.errors import DiskSpaceError
    from videogen.utils.system import same_volume

    reserve = reserve_mb * MB
    needs: list[tuple[Path, int]]
    if same_volume(workspace, output):
        needs = [(workspace, est.workspace_bytes + est.output_bytes)]
    else:
        needs = [(workspace, est.workspace_bytes), (output, est.output_bytes)]
    for path, need in needs:
        free = free_disk_bytes(path)
        if free < need + reserve:
            raise DiskSpaceError(
                f"Недостатньо вільного місця на диску. Потрібно приблизно "
                f"{(need + reserve) / MB / 1024:.1f} ГБ, вільно {free / MB / 1024:.1f} ГБ ({path}).",
                code="DISK_SPACE")

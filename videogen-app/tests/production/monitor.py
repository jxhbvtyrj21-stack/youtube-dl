"""Resource sampling and process inspection for production stress tests."""

from __future__ import annotations

import os
import statistics
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

MB = 1024 * 1024
MEDIA_NAMES = ("ffmpeg", "ffprobe")


def _name(p: psutil.Process) -> str:
    try:
        return p.name().lower().removesuffix(".exe")
    except psutil.Error:
        return ""


def _cmd(p: psutil.Process) -> str:
    try:
        return " ".join(p.cmdline())
    except psutil.Error:
        return ""


def media_processes() -> list[psutil.Process]:
    """Every ffmpeg/ffprobe on the machine (Windows process list equivalent)."""
    return [p for p in psutil.process_iter() if _name(p) in MEDIA_NAMES]


def videogen_processes() -> list[psutil.Process]:
    """Python/EXE processes belonging to VideoGen (engine, workers, GUI harness)."""
    out = []
    for p in psutil.process_iter():
        n = _name(p)
        if n.startswith("videogen"):
            out.append(p)
        elif n.startswith("python"):
            c = _cmd(p)
            if "multiprocessing-fork" in c or "spawn_main" in c or "gui_harness" in c:
                out.append(p)
    return out


def orphans() -> list[str]:
    """ffmpeg / VideoGen helper processes whose parent no longer exists."""
    out = []
    for p in media_processes() + videogen_processes():
        try:
            if "resource_tracker" in _cmd(p):
                continue
            ppid = p.ppid()
            if ppid and not psutil.pid_exists(ppid):
                out.append(f"{p.pid} {_name(p)} parent {ppid} gone: {_cmd(p)[:100]}")
        except psutil.Error:
            continue
    return out


def tree_rss_mb(pid: int) -> float:
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return 0.0
    total = 0
    for p in procs:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            continue
    return total / MB


def rss_mb(pid: int) -> float:
    try:
        return psutil.Process(pid).memory_info().rss / MB
    except psutil.Error:
        return 0.0


def count_files(path: Path) -> int:
    n = 0
    for _d, _s, files in os.walk(path):
        n += len(files)
    return n


@dataclass
class Sampler:
    """Samples every `interval` seconds until stopped."""

    engine_pid: Callable[[], int | None]
    disk_path: Path
    temp_path: Path
    interval: float = 2.0
    samples: list[dict] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def start(self) -> "Sampler":
        psutil.cpu_percent(interval=None)
        self.t0 = time.monotonic()
        self.sample("start")
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.sample()

    def sample(self, mark: str = "", **extra) -> dict:
        pid = self.engine_pid()
        vm = psutil.virtual_memory()
        media = media_processes()
        try:
            children = len(psutil.Process(pid).children(recursive=True)) if pid else 0
        except psutil.Error:
            children = 0
        row = {
            "t": round(time.monotonic() - self.t0, 1), "mark": mark,
            "engine_rss_mb": round(rss_mb(pid), 1) if pid else 0.0,
            "engine_tree_rss_mb": round(tree_rss_mb(pid), 1) if pid else 0.0,
            "test_rss_mb": round(rss_mb(os.getpid()), 1),
            "sys_avail_mb": round(vm.available / MB),
            "cpu_pct": psutil.cpu_percent(interval=None),
            "engine_children": children,
            "ffmpeg_procs": len(media),
            "disk_free_mb": round(psutil.disk_usage(str(self.disk_path)).free / MB),
            "temp_files": count_files(self.temp_path) if self.temp_path.exists() else 0,
            **extra,
        }
        self.samples.append(row)
        return row

    def stop(self) -> list[dict]:
        self._stop.set()
        if self._thread:
            self._thread.join(10)
        self.sample("end")
        return self.samples


def slope(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 3:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0.0


def summarize(samples: list[dict], key: str = "engine_rss_mb") -> dict:
    vals = [s[key] for s in samples if s.get(key)]
    if not vals:
        return {}
    half = len(vals) // 2
    return {
        f"{key}_start": vals[0], f"{key}_end": vals[-1], f"{key}_max": max(vals),
        f"{key}_median_1st_half": statistics.median(vals[:half] or vals),
        f"{key}_median_2nd_half": statistics.median(vals[half:]),
        f"{key}_slope_per_min": round(slope([s["t"] for s in samples if s.get(key)], vals) * 60, 3),
        "cpu_pct_avg": round(statistics.mean(s["cpu_pct"] for s in samples), 1),
        "ffmpeg_procs_max": max(s["ffmpeg_procs"] for s in samples),
        "engine_children_max": max(s["engine_children"] for s in samples),
        "temp_files_max": max(s["temp_files"] for s in samples),
        "temp_files_end": samples[-1]["temp_files"],
        "disk_free_mb_min": min(s["disk_free_mb"] for s in samples),
        "sys_avail_mb_min": min(s["sys_avail_mb"] for s in samples),
        "samples": len(samples),
    }

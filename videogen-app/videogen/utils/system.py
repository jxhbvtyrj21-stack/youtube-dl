"""System information: app-data location, free disk, memory."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import psutil

from videogen import APP_NAME


def app_data_dir() -> Path:
    """``%LOCALAPPDATA%\\VideoGen`` on Windows, XDG-ish elsewhere.

    ``VIDEOGEN_APPDATA`` overrides (used by tests and portable installs).
    """
    override = os.environ.get("VIDEOGEN_APPDATA")
    if override:
        return Path(override)
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_NAME


def free_disk_bytes(path: Path) -> int:
    """Free bytes on the volume holding ``path`` (walks up to an existing dir)."""
    p = Path(path).resolve()
    for _ in range(len(p.parts) + 1):
        if p.exists():
            return shutil.disk_usage(p).free
        p = p.parent
    return shutil.disk_usage(Path.cwd()).free


def same_volume(a: Path, b: Path) -> bool:
    def _dev(p: Path) -> int:
        p = Path(p).resolve()
        for _ in range(len(p.parts) + 1):
            if p.exists():
                return os.stat(p).st_dev
            p = p.parent
        return -1
    return _dev(a) == _dev(b)


@dataclass(frozen=True)
class MemorySnapshot:
    available_mb: float
    total_mb: float
    percent_used: float
    process_rss_mb: float
    tree_rss_mb: float


def memory_snapshot(pid: int | None = None) -> MemorySnapshot:
    vm = psutil.virtual_memory()
    proc = psutil.Process(pid or os.getpid())
    rss = proc.memory_info().rss
    tree = rss
    try:
        for child in proc.children(recursive=True):
            try:
                tree += child.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass  # invariant-ok: process tree changed while iterating; rss is enough
    mb = 1024 * 1024
    return MemorySnapshot(vm.available / mb, vm.total / mb, vm.percent, rss / mb, tree / mb)

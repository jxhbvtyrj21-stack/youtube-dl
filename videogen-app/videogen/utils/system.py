"""System information: app-data location, free disk, memory."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


def redirect_missing_std_streams(log_dir: Path) -> None:
    """Windowed (console-less) EXE: sys.stdout/stderr are None, so any
    traceback printed by Python or a library is lost — or crashes code that
    writes to them. Send them to a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = open(log_dir / f"console-{os.getpid()}.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def write_crash_report(appdata: Path, where: str, exc: BaseException) -> Path | None:
    """Last-resort crash file when the logging system itself is not up."""
    import traceback
    try:
        appdata.mkdir(parents=True, exist_ok=True)
        p = appdata / "crash.log"
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"\n=== {where} pid={os.getpid()} ===\n")
            fh.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        return p
    except OSError:
        return None


class InstanceLock:
    """Exclusive lock on the app-data folder: only one Engine may own the
    state database. Released automatically by the OS if the process dies
    (so a crash never leaves a stale lock)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        try:   # informational only (pid of the owner)
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()).encode())
            fh.flush()
        except OSError:
            pass  # invariant-ok: the lock itself is what matters
        self._fh = fh
        return True

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass  # invariant-ok: closing the file releases the lock anyway
        finally:
            fh.close()

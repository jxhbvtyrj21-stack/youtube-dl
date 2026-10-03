"""Diagnostic snapshots and bounded diagnostics storage (ARCHITECTURE.md §13.2-3)."""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import time
from pathlib import Path
from typing import Any

import psutil

from videogen import __version__
from videogen.storage.atomic import atomic_write_text

log = logging.getLogger(__name__)


def process_tree_info(pid: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        root = psutil.Process(pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        return out
    for p in procs:
        try:
            with p.oneshot():
                ct = p.cpu_times()
                out.append({
                    "pid": p.pid, "name": p.name(), "status": p.status(),
                    "cpu_user_s": ct.user, "cpu_system_s": ct.system,
                    "rss_mb": round(p.memory_info().rss / 1048576, 1),
                })
        except psutil.Error:
            out.append({"pid": p.pid, "status": "gone"})
    return out


def system_info(paths: list[Path] | None = None) -> dict[str, Any]:
    vm = psutil.virtual_memory()
    disks = {}
    for p in paths or []:
        try:
            disks[str(p)] = round(shutil.disk_usage(p).free / 1048576)
        except OSError as exc:
            disks[str(p)] = repr(exc)
    return {
        "app_version": __version__,
        "os": platform.platform(),
        "python": platform.python_version(),
        "ram_available_mb": round(vm.available / 1048576),
        "ram_percent": vm.percent,
        "cpu_percent": psutil.cpu_percent(interval=None),
        "disk_free_mb": disks,
    }


def write_snapshot(diag_dir: Path, *, reason: str, pid: int | None = None,
                   extra: dict[str, Any] | None = None, paths: list[Path] | None = None) -> Path | None:
    """Write ``snapshot-<ts>.json``. Never raises (diagnostics must not fail a job)."""
    try:
        data: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "reason": reason,
            "system": system_info(paths),
        }
        if pid is not None:
            data["process_tree"] = process_tree_info(pid)
        if extra:
            data.update(extra)
        path = Path(diag_dir) / f"snapshot-{int(time.time() * 1000)}.json"
        atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, default=str))
        return path
    except Exception:  # noqa: BLE001
        log.exception("could not write diagnostic snapshot")
        return None


def _dir_size(path: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                continue
    return total


def prune_diagnostics(root: Path, max_jobs: int, max_mb: int) -> list[Path]:
    """Delete oldest per-job diagnostic dirs beyond the count/size limits."""
    removed: list[Path] = []
    try:
        dirs = sorted((p for p in Path(root).iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
    except OSError:
        return removed
    sizes = {d: _dir_size(d) for d in dirs}
    total = sum(sizes.values())
    limit = max_mb * 1048576
    for d in list(dirs):
        if len(dirs) - len(removed) <= max_jobs and total <= limit:
            break
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            removed.append(d)
            total -= sizes[d]
    return removed

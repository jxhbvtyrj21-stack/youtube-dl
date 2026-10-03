"""Child process creation flags and process-tree termination
(ARCHITECTURE.md §8.4).

PHASE 3 provides the portable psutil-based tree kill. PHASE 4 adds Windows
Job Objects (kill-on-close) on top; the public functions stay the same.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from typing import Any

import psutil

log = logging.getLogger(__name__)

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def popen_kwargs() -> dict[str, Any]:
    """Flags so a child never opens a console window and can be killed as a
    group; ``shell`` is never used."""
    if os.name == "nt":
        return {"creationflags": CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _tree(pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return []
    try:
        children = root.children(recursive=True)
    except psutil.Error:
        children = []
    return [root, *children]


def kill_tree(pid: int, *, wait_s: float = 5.0, graceful_s: float = 0.0) -> list[int]:
    """Terminate ``pid`` and all its descendants. Returns PIDs still alive.

    Order: optional graceful terminate (``graceful_s`` > 0) -> kill. Children
    are snapshotted *before* the parent dies (afterwards they are reparented
    and no longer discoverable from the parent).
    """
    procs = _tree(pid)
    if not procs:
        return []
    if graceful_s > 0:
        for p in procs:
            try:
                p.terminate()
            except psutil.Error:
                continue
        _gone, alive = psutil.wait_procs(procs, timeout=graceful_s)
        procs = alive
    if os.name != "nt":
        # Only kill the process *group* if pid leads its own group (started
        # with start_new_session). Otherwise the group is ours and killpg
        # would kill the caller itself.
        try:
            if os.getpgid(pid) == pid and os.getpgid(0) != pid:
                os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass  # invariant-ok: group already gone; individual kills below
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            continue
    _gone, alive = psutil.wait_procs(procs, timeout=wait_s)
    survivors = [p.pid for p in alive if _is_alive(p)]
    if survivors:
        log.critical("processes survived kill: %s", survivors)
    return survivors


def _is_alive(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def descendants_alive(pids: list[int]) -> list[int]:
    out = []
    for pid in pids:
        try:
            if _is_alive(psutil.Process(pid)):
                out.append(pid)
        except psutil.Error:
            continue
    return out


def reap(proc: subprocess.Popen[bytes], timeout: float = 5.0) -> int | None:
    """Collect the exit status so no zombie remains (POSIX)."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None

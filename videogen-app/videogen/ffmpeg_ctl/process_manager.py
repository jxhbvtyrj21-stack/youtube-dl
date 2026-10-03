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


# ====================================================================== Job Objects

class JobObject:
    """Windows Job Object with KILL_ON_JOB_CLOSE (ARCHITECTURE.md §8.4).

    Every child assigned to it — and every process *those* children start —
    is terminated by the OS when the job handle closes, including when our
    own process is killed from Task Manager. On other platforms this is a
    no-op; process groups + :func:`kill_tree` provide the equivalent there.
    """

    def __init__(self, name: str = "") -> None:
        self.name = name
        self._handle: Any = None
        self.supported = os.name == "nt"
        if self.supported:
            try:
                self._handle = _win_create_kill_on_close_job()
            except OSError:
                log.exception("could not create Job Object; falling back to psutil tree kill")
                self.supported = False

    def assign(self, pid: int) -> bool:
        if not self.supported or self._handle is None:
            return False
        try:
            _win_assign(self._handle, pid)
            return True
        except OSError:
            # e.g. process already exited, or nested-job restrictions
            log.warning("could not assign pid %s to Job Object %s", pid, self.name)
            return False

    def terminate(self, exit_code: int = 1) -> bool:
        if not self.supported or self._handle is None:
            return False
        try:
            return bool(_kernel32().TerminateJobObject(self._handle, exit_code))
        except OSError:
            return False

    def close(self) -> None:
        if self._handle is not None:
            try:
                _kernel32().CloseHandle(self._handle)
            except OSError:
                log.warning("CloseHandle(Job Object) failed")
            self._handle = None

    def __enter__(self) -> "JobObject":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _kernel32() -> Any:
    import ctypes
    return ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]


def _win_structs() -> Any:
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):  # noqa: N801
        _fields_ = [(n, ctypes.c_ulonglong) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return JOBOBJECT_EXTENDED_LIMIT_INFORMATION


JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


def _win_create_kill_on_close_job() -> Any:
    import ctypes
    k32 = _kernel32()
    k32.CreateJobObjectW.restype = ctypes.c_void_p
    handle = k32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    info = _win_structs()()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = k32.SetInformationJobObject(ctypes.c_void_p(handle), JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
                                     ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        k32.CloseHandle(ctypes.c_void_p(handle))
        raise ctypes.WinError(err)  # type: ignore[attr-defined]
    return ctypes.c_void_p(handle)


def _win_assign(job_handle: Any, pid: int) -> None:
    import ctypes
    k32 = _kernel32()
    k32.OpenProcess.restype = ctypes.c_void_p
    ph = k32.OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, False, pid)
    if not ph:
        raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    try:
        if not k32.AssignProcessToJobObject(job_handle, ctypes.c_void_p(ph)):
            raise ctypes.WinError(ctypes.get_last_error())  # type: ignore[attr-defined]
    finally:
        k32.CloseHandle(ctypes.c_void_p(ph))


# ====================================================================== registry

class ProcessRegistry:
    """All live child processes started by this Engine.

    Used to (a) kill everything on shutdown via any path, (b) assert in tests
    that no process is left behind after SUCCESS/FAILED/TIMEOUT/CANCEL.
    """

    def __init__(self) -> None:
        import threading
        self._lock = threading.Lock()
        self._procs: dict[int, str] = {}

    def add(self, pid: int, label: str) -> None:
        with self._lock:
            self._procs[pid] = label

    def remove(self, pid: int) -> None:
        with self._lock:
            self._procs.pop(pid, None)

    def snapshot(self) -> dict[int, str]:
        with self._lock:
            return dict(self._procs)

    def alive(self) -> list[int]:
        return descendants_alive(list(self.snapshot()))

    def kill_all(self, wait_s: float = 5.0) -> list[int]:
        survivors: list[int] = []
        for pid in list(self.snapshot()):
            survivors += kill_tree(pid, wait_s=wait_s)
            self.remove(pid)
        return survivors


REGISTRY = ProcessRegistry()

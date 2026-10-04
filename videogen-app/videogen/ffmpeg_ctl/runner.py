"""Bounded execution of external tools (ffmpeg / ffprobe).

Guarantees for every call:
  * ``argv`` list, never a shell;
  * stdout and stderr are drained by dedicated reader threads into bounded
    buffers — a chatty process can never deadlock on a full pipe;
  * a hard timeout; on expiry or cancellation the whole process tree is
    killed and the kill is verified;
  * the exit status is always reaped.

PHASE 4 extends this with live ``-progress`` parsing and stall detection.
"""

from __future__ import annotations

import collections
import logging
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import psutil

from videogen.core.cancellation import CancellationToken
from videogen.core.models import ProcessResult
from videogen.ffmpeg_ctl.process_manager import descendants_alive, kill_tree, popen_kwargs, reap
from videogen.workers.watchdog import Verdict

if TYPE_CHECKING:
    from videogen.config.settings import TimeoutPolicy
    from videogen.ffmpeg_ctl.process_manager import JobObject
    from videogen.ffmpeg_ctl.progress import FFmpegProgress

log = logging.getLogger(__name__)

STDERR_TAIL_LINES = 400
STDOUT_MAX_BYTES = 32 * 1024 * 1024
POLL_S = 0.1


@dataclass(frozen=True)
class RunOutput:
    result: ProcessResult
    stdout: bytes
    timed_out: bool
    cancelled: bool
    stdout_truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.result.returncode == 0 and not self.timed_out and not self.cancelled

    @property
    def stderr(self) -> str:
        return self.result.stderr_tail


class _Reader(threading.Thread):
    def __init__(self, stream: IO[bytes], *, keep_lines: int | None, max_bytes: int, name: str) -> None:
        super().__init__(name=name, daemon=True)
        self.stream = stream
        self.keep_lines = keep_lines
        self.max_bytes = max_bytes
        self.lines: collections.deque[bytes] = collections.deque(maxlen=keep_lines or 1)
        self.chunks: list[bytes] = []
        self.size = 0
        self.truncated = False
        self.last_activity = time.monotonic()

    def run(self) -> None:
        try:
            if self.keep_lines:
                for line in iter(self.stream.readline, b""):
                    self.lines.append(line[:4000])
                    self.last_activity = time.monotonic()
            else:
                for chunk in iter(lambda: self.stream.read(65536), b""):
                    self.last_activity = time.monotonic()
                    if self.size + len(chunk) <= self.max_bytes:
                        self.chunks.append(chunk)
                        self.size += len(chunk)
                    else:
                        self.truncated = True   # keep draining, stop storing
        except (OSError, ValueError):
            pass  # invariant-ok: pipe closed because the process was killed
        finally:
            try:
                self.stream.close()
            except OSError:
                pass  # invariant-ok: already closed

    def text(self) -> str:
        return b"".join(self.lines).decode("utf-8", "replace")

    def data(self) -> bytes:
        return b"".join(self.chunks)


def run_tool(argv: list[str], *, timeout_s: float, cwd: Path | None = None,
             token: CancellationToken | None = None, kill_wait_s: float = 5.0) -> RunOutput:
    """Run ``argv`` to completion, timeout or cancellation. Never hangs.

    Raises ``FileNotFoundError``/``PermissionError`` only if the executable
    cannot be started at all.
    """
    # a plain str would pass the element check and, on Windows, be parsed
    # as a command line — exactly what argument lists exist to prevent
    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of str")
    started = time.time()
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=str(cwd) if cwd else None,
                            shell=False, **popen_kwargs())
    from videogen.ffmpeg_ctl.process_manager import REGISTRY
    REGISTRY.add(proc.pid, argv[0])
    out_reader = _Reader(proc.stdout, keep_lines=None, max_bytes=STDOUT_MAX_BYTES, name="tool-stdout")  # type: ignore[arg-type]
    err_reader = _Reader(proc.stderr, keep_lines=STDERR_TAIL_LINES, max_bytes=0, name="tool-stderr")  # type: ignore[arg-type]
    out_reader.start()
    err_reader.start()

    killed = threading.Event()
    reason: list[str] = []

    def _kill(why: str) -> None:
        if killed.is_set():
            return
        killed.set()
        reason.append(why)
        kill_tree(proc.pid, wait_s=kill_wait_s)

    handle = token.register(lambda: _kill("cancelled")) if token is not None else None
    deadline = time.monotonic() + timeout_s
    timed_out = False
    try:
        while proc.poll() is None and not killed.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                log.warning("tool timed out after %.1fs: %s", timeout_s, argv[0],
                            extra={"event": "tool_timeout"})
                _kill("timeout")
                break
            try:
                proc.wait(timeout=min(POLL_S * 5, remaining))
            except subprocess.TimeoutExpired:
                continue
    finally:
        if handle is not None and token is not None:
            token.unregister(handle)
        if proc.poll() is None:
            _kill(reason[0] if reason else "cleanup")
        rc = reap(proc, timeout=kill_wait_s)
        out_reader.join(timeout=kill_wait_s)
        err_reader.join(timeout=kill_wait_s)
        REGISTRY.remove(proc.pid)

    cancelled = bool(reason) and reason[0] == "cancelled"
    result = ProcessResult(
        pid=proc.pid, argv=tuple(argv), started_at=started, ended_at=time.time(),
        returncode=rc, stderr_tail=err_reader.text(), killed_by_watchdog=timed_out,
        kill_reason=reason[0] if reason else "")
    return RunOutput(result, out_reader.data(), timed_out, cancelled, out_reader.truncated)


# ============================================================== supervised FFmpeg

@dataclass(frozen=True)
class FFmpegRun:
    result: ProcessResult
    progress: "FFmpegProgress"
    verdict: "Verdict | None"
    cancelled: bool
    survivors: tuple[int, ...] = ()

    @property
    def ok(self) -> bool:
        return self.result.returncode == 0 and self.verdict is None and not self.cancelled


ProgressCallback = Callable[["FFmpegProgress"], None]
SnapshotCallback = Callable[[str, dict[str, Any]], None]


def run_ffmpeg(argv: list[str], *, hard_s: float, stall_s: float | None, cwd: Path | None = None,
               output_path: Path | None = None, token: CancellationToken | None = None,
               on_progress: ProgressCallback | None = None, on_snapshot: SnapshotCallback | None = None,
               policy: "TimeoutPolicy | None" = None, job: "JobObject | None" = None,
               label: str = "ffmpeg") -> FFmpegRun:
    """Run FFmpeg under a watchdog (ARCHITECTURE.md §7, §8.2).

    ``argv`` should contain ``-progress pipe:1 -nostats``; stdout is parsed
    as progress. On stall / livelock / hard timeout / cancellation the
    escalation is: ``q`` on stdin -> terminate -> kill tree (Job Object on
    Windows) -> verification that every process of the tree is gone. The
    partially written ``output_path`` is deleted in that case.
    """
    from videogen.config.settings import TimeoutPolicy
    from videogen.ffmpeg_ctl.process_manager import REGISTRY
    from videogen.ffmpeg_ctl.progress import FFmpegProgress, ProgressParser
    from videogen.workers.watchdog import Watchdog

    if not isinstance(argv, (list, tuple)) or not argv or not all(isinstance(a, str) for a in argv):
        raise TypeError("argv must be a non-empty list of str")
    tp = policy or TimeoutPolicy()
    started = time.time()
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(cwd) if cwd else None, shell=False, **popen_kwargs())
    REGISTRY.add(proc.pid, label)
    if job is not None:
        job.assign(proc.pid)

    parser = ProgressParser()
    plock = threading.Lock()
    latest: list[FFmpegProgress] = [FFmpegProgress()]

    def _read_progress() -> None:
        try:
            for line in iter(proc.stdout.readline, b""):  # type: ignore[union-attr]
                snap = parser.feed_line(line)
                if snap is None:
                    continue
                with plock:
                    latest[0] = snap
                if on_progress is not None:
                    try:
                        on_progress(snap)
                    except Exception:  # noqa: BLE001 - a UI callback must not break the run
                        log.exception("progress callback failed")
        except (OSError, ValueError):
            pass  # invariant-ok: pipe closed after kill
        finally:
            try:
                proc.stdout.close()  # type: ignore[union-attr]
            except OSError:
                pass  # invariant-ok: already closed

    out_thread = threading.Thread(target=_read_progress, name="ffmpeg-progress", daemon=True)
    err_reader = _Reader(proc.stderr, keep_lines=STDERR_TAIL_LINES, max_bytes=0, name="ffmpeg-stderr")  # type: ignore[arg-type]
    out_thread.start()
    err_reader.start()

    cancel_flag = threading.Event()
    handle = token.register(cancel_flag.set) if token is not None else None
    wd = Watchdog(hard_s=hard_s, stall_s=stall_s)
    try:
        ps_proc = psutil.Process(proc.pid)
    except psutil.Error:
        ps_proc = None

    verdict: Verdict | None = None
    survivors: tuple[int, ...] = ()
    poll = max(0.05, tp.watchdog_poll_s)
    max_loops = int((hard_s + 1) / poll) + 10
    try:
        for _ in range(max_loops):
            if proc.poll() is not None:
                break
            if cancel_flag.is_set():
                break
            with plock:
                marker = latest[0].marker
            wd.observe(marker=marker, out_size=_size(output_path), cpu_time=_cpu(ps_proc))
            verdict = wd.verdict()
            if verdict is not None:
                break
            try:
                proc.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                continue
        else:
            verdict = Verdict("hard_timeout", "watchdog loop bound reached")

        if proc.poll() is None and (verdict is not None or cancel_flag.is_set()):
            reason = verdict.kind if verdict else "cancelled"
            log.warning("%s pid=%s: %s (%s); escalating", label, proc.pid, reason,
                        verdict.detail if verdict else "user request", extra={"event": "watchdog"})
            if on_snapshot is not None:
                try:
                    on_snapshot(reason, {"argv": list(argv), "pid": proc.pid, "watchdog": wd.snapshot(),
                                         "progress": repr(latest[0]), "stderr_tail": err_reader.text()[-4000:]})
                except Exception:  # noqa: BLE001
                    log.exception("snapshot callback failed")
            survivors = tuple(_escalate(proc, tp, job))
    finally:
        if handle is not None and token is not None:
            token.unregister(handle)
        if proc.poll() is None:
            survivors = tuple(_escalate(proc, tp, job))
        rc = reap(proc, timeout=tp.kill_wait_s)
        out_thread.join(timeout=tp.kill_wait_s)
        err_reader.join(timeout=tp.kill_wait_s)
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass  # invariant-ok: stdin already broken after kill
        REGISTRY.remove(proc.pid)

    killed = verdict is not None or cancel_flag.is_set()
    if killed and output_path is not None:
        try:
            output_path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete partial output %s", output_path)
    result = ProcessResult(
        pid=proc.pid, argv=tuple(argv), started_at=started, ended_at=time.time(), returncode=rc,
        stderr_tail=err_reader.text(), killed_by_watchdog=verdict is not None,
        kill_reason=(verdict.kind if verdict else ("cancelled" if cancel_flag.is_set() else "")),
        survivors=survivors)
    with plock:
        last = latest[0]
    return FFmpegRun(result, last, verdict, cancel_flag.is_set() and verdict is None, survivors)


def _size(p: Path | None) -> int | None:
    if p is None:
        return None
    try:
        return p.stat().st_size
    except OSError:
        return None


def _cpu(p: "psutil.Process | None") -> float | None:
    if p is None:
        return None
    try:
        t = p.cpu_times()
        return t.user + t.system
    except psutil.Error:
        return None


def _escalate(proc: "subprocess.Popen[bytes]", tp: "TimeoutPolicy", job: "JobObject | None") -> list[int]:
    """graceful 'q' -> terminate -> kill tree. Returns surviving PIDs."""
    try:
        tree = [proc.pid] + [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
    except psutil.Error:
        tree = [proc.pid]
    # 1. graceful: FFmpeg finishes the current packet and closes the file
    try:
        if proc.stdin:
            proc.stdin.write(b"q\n")
            proc.stdin.flush()
    except (OSError, ValueError):
        pass  # invariant-ok: stdin closed — go straight to terminate
    try:
        proc.wait(timeout=tp.graceful_wait_s)
    except subprocess.TimeoutExpired:
        # 2. terminate
        try:
            proc.terminate()
        except OSError:
            pass  # invariant-ok: already exited
        try:
            proc.wait(timeout=tp.terminate_wait_s)
        except subprocess.TimeoutExpired:
            pass  # invariant-ok: escalate to tree kill below
    # 3. kill the whole tree (children may outlive a terminated parent)
    if job is not None:
        job.terminate()
    survivors: list[int] = []
    for pid in tree:
        survivors += kill_tree(pid, wait_s=tp.kill_wait_s)
    # 4. verify
    survivors = descendants_alive(sorted(set(survivors + tree)))
    if survivors:
        log.critical("processes still alive after kill: %s", survivors, extra={"event": "orphan"})
    return survivors


def raise_for(run: FFmpegRun, what: str) -> None:
    """Translate a failed FFmpegRun into the error taxonomy (§11)."""
    from videogen.core.errors import FFmpegCrashError, JobCancelledError, OperationTimeoutError

    if run.cancelled:
        raise JobCancelledError()
    if run.verdict is not None:
        kind = {"hard_timeout": "перевищено максимальний час", "stall": "процес завис",
                "livelock": "процес завис (навантаження без результату)"}.get(run.verdict.kind, run.verdict.kind)
        raise OperationTimeoutError(f"{what}: {kind}.", code=run.verdict.kind.upper(),
                                    detail=run.result.stderr_tail[-2000:])
    if run.result.returncode != 0:
        if _disk_full(run.result.stderr_tail):
            # not a crash: retrying cannot help and the batch must pause
            from videogen.core.errors import DiskSpaceError
            raise DiskSpaceError(f"{what}: недостатньо вільного місця на диску.", code="DISK_SPACE",
                                 detail=run.result.stderr_tail[-2000:])
        raise FFmpegCrashError(f"{what}: FFmpeg завершився з помилкою (код {run.result.returncode}).",
                               detail=run.result.stderr_tail[-2000:])


_DISK_FULL_MARKERS = ("no space left on device", "not enough space on the disk", "disk full",
                      "there is not enough space")


def _disk_full(stderr: str) -> bool:
    low = stderr.lower()
    return any(m in low for m in _DISK_FULL_MARKERS)

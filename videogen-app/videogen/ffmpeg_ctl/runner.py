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
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from videogen.core.cancellation import CancellationToken
from videogen.core.models import ProcessResult
from videogen.ffmpeg_ctl.process_manager import kill_tree, popen_kwargs, reap

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

    cancelled = bool(reason) and reason[0] == "cancelled"
    result = ProcessResult(
        pid=proc.pid, argv=tuple(argv), started_at=started, ended_at=time.time(),
        returncode=rc, stderr_tail=err_reader.text(), killed_by_watchdog=timed_out,
        kill_reason=reason[0] if reason else "")
    return RunOutput(result, out_reader.data(), timed_out, cancelled, out_reader.truncated)

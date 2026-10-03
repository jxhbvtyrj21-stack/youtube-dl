"""Structured, rotating, multi-process logging (ARCHITECTURE.md §13).

Every process logs through a ``QueueHandler`` into one
``multiprocessing.Queue``. A single ``QueueListener`` in the Engine owns all
files, so rotation works on Windows (no two processes hold the same log).

Files:
  * ``application.log`` — INFO+ (DEBUG if configured), rotated;
  * ``errors.log``      — WARNING+, rotated;
  * ``diagnostics/<job_id>/job.log`` — JSON Lines for one job, enabled per job.

Records may carry ``extra={"job_id", "stage", "event", "duration_ms",
"error", "detail"}``; missing fields are rendered as ``-``.
"""

from __future__ import annotations

import copy
import json
import logging
import logging.handlers
import multiprocessing
import queue as queue_mod
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from videogen.config.settings import LoggingSettings

STRUCT_FIELDS = ("job_id", "stage", "event", "duration_ms", "error")
LINE_FORMAT = ("%(asctime)s %(levelname)-8s %(processName)s job=%(job_id)s stage=%(stage)s "
               "event=%(event)s | %(message)s")


CONTROL_ATTR = "_vg_control"


def _is_control(record: logging.LogRecord) -> bool:
    return getattr(record, CONTROL_ATTR, None) is not None


class _Defaults(logging.Filter):
    """Fills missing structured fields; drops internal control records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if _is_control(record):
            return False
        for f in STRUCT_FIELDS:
            if not hasattr(record, f):
                setattr(record, f, "-")
        return True


class IsoFormatter(logging.Formatter):
    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds")


def _json_record(record: logging.LogRecord) -> str:
    data: dict[str, Any] = {
        "ts": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
        "level": record.levelname,
        "logger": record.name,
        "process": record.processName,
        "message": record.getMessage(),
    }
    for f in (*STRUCT_FIELDS, "detail"):
        v = getattr(record, f, None)
        if v not in (None, "-"):
            data[f] = v
    if record.exc_text:
        data["traceback"] = record.exc_text
    return json.dumps(data, ensure_ascii=False, default=str)


class JobLogRouter(logging.Handler):
    """Routes records with ``job_id`` to that job's JSONL file, if registered.

    Open/close requests travel through the same log queue as ordinary
    records (see :meth:`LogSystem.open_job_log`), so a job's last records are
    always written before its file is closed — no ordering race with the
    asynchronous listener.
    """

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self._files: dict[str, Any] = {}
        self._flock = threading.Lock()

    def open_job(self, job_id: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a", encoding="utf-8")
        with self._flock:
            old = self._files.pop(job_id, None)
            self._files[job_id] = fh
        if old is not None:
            old.close()

    def close_job(self, job_id: str) -> None:
        with self._flock:
            fh = self._files.pop(job_id, None)
        if fh is not None:
            fh.close()

    def emit(self, record: logging.LogRecord) -> None:
        control = getattr(record, CONTROL_ATTR, None)
        if control is not None:
            action, job_id, path = control
            try:
                if action == "open":
                    self.open_job(job_id, Path(path))
                else:
                    self.close_job(job_id)
            except Exception:  # noqa: BLE001
                self.handleError(record)
            return
        job_id = getattr(record, "job_id", "-")
        if job_id in (None, "-"):
            return
        with self._flock:
            fh = self._files.get(job_id)
            if fh is None:
                return
            try:
                fh.write(_json_record(record) + "\n")
                fh.flush()
            except Exception:  # noqa: BLE001
                self.handleError(record)

    def close(self) -> None:
        with self._flock:
            files = list(self._files.values())
            self._files.clear()
        for fh in files:
            fh.close()
        super().close()


class StructuredQueueHandler(logging.handlers.QueueHandler):
    """Like QueueHandler, but keeps the traceback in ``exc_text`` instead of
    merging it into the message, so the JSON job log gets a separate
    ``traceback`` field while text logs still print it."""

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        msg = record.getMessage()
        exc_text = record.exc_text
        if record.exc_info and not exc_text:
            exc_text = logging.Formatter().formatException(record.exc_info)
        rec = copy.copy(record)
        rec.msg = msg
        rec.message = msg
        rec.args = None
        rec.exc_info = None
        rec.exc_text = exc_text
        rec.stack_info = None
        return rec


class CallbackHandler(logging.Handler):
    """Forwards INFO+ records to a callback (Engine -> GUI journal)."""

    def __init__(self, callback: Callable[[logging.LogRecord], None], level: int = logging.INFO) -> None:
        super().__init__(level)
        self.callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.callback(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class LogSystem:
    """Owns the listener and file handlers. Create once per Engine process."""

    def __init__(self, log_dir: Path, settings: LoggingSettings | None = None,
                 *, mp_context: Any = None) -> None:
        self.settings = settings or LoggingSettings()
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        ctx = mp_context or multiprocessing.get_context("spawn")
        self.queue: Any = ctx.Queue(-1)

        fmt = IsoFormatter(LINE_FORMAT)
        defaults = _Defaults()
        level = getattr(logging, self.settings.level, logging.INFO)

        self.app_handler = logging.handlers.RotatingFileHandler(
            self.log_dir / "application.log", maxBytes=self.settings.max_bytes,
            backupCount=self.settings.backup_count, encoding="utf-8", delay=True)
        self.app_handler.setLevel(level)
        self.err_handler = logging.handlers.RotatingFileHandler(
            self.log_dir / "errors.log", maxBytes=self.settings.max_bytes,
            backupCount=self.settings.backup_count, encoding="utf-8", delay=True)
        self.err_handler.setLevel(logging.WARNING)
        for h in (self.app_handler, self.err_handler):
            h.setFormatter(fmt)
            h.addFilter(defaults)
        self.job_router = JobLogRouter()
        self._extra_handlers: list[logging.Handler] = []

        self.listener = logging.handlers.QueueListener(
            self.queue, self.app_handler, self.err_handler, self.job_router, _Fanout(self),
            respect_handler_level=True)
        self._root_handler = StructuredQueueHandler(self.queue)
        self._installed = False

    def install(self) -> None:
        """Route this process's root logger into the queue and start listening."""
        root = logging.getLogger()
        root.addHandler(self._root_handler)
        root.setLevel(logging.DEBUG)
        self.listener.start()
        self._installed = True

    def open_job_log(self, job_id: str, path: Path) -> None:
        self._control(("open", job_id, str(path)))

    def close_job_log(self, job_id: str) -> None:
        self._control(("close", job_id, ""))

    def _control(self, payload: tuple[str, str, str]) -> None:
        rec = logging.LogRecord("videogen.logcontrol", logging.CRITICAL, __file__, 0, "", None, None)
        setattr(rec, CONTROL_ATTR, payload)
        self.queue.put_nowait(rec)

    def add_handler(self, handler: logging.Handler) -> None:
        handler.addFilter(_Defaults())
        self._extra_handlers.append(handler)

    def shutdown(self, timeout: float = 5.0) -> None:
        if not self._installed:
            return
        logging.getLogger().removeHandler(self._root_handler)
        # QueueListener.stop() enqueues a sentinel and joins; bound it.
        t = threading.Thread(target=self.listener.stop, daemon=True)
        t.start()
        t.join(timeout)
        for h in (self.app_handler, self.err_handler, self.job_router, *self._extra_handlers):
            try:
                h.close()
            except Exception:  # noqa: BLE001
                pass  # invariant-ok: closing handlers at shutdown is best effort
        try:
            self.queue.close()
            self.queue.join_thread()
        except (OSError, ValueError, AttributeError):
            pass  # invariant-ok: queue already closed
        self._installed = False


class _Fanout(logging.Handler):
    """Lets handlers be added after the listener started (GUI forwarding)."""

    def __init__(self, system: LogSystem) -> None:
        super().__init__(logging.DEBUG)
        self.system = system
        self.addFilter(_Defaults())

    def emit(self, record: logging.LogRecord) -> None:
        if _is_control(record):
            return
        for h in list(self.system._extra_handlers):
            if record.levelno >= h.level:
                h.handle(record)


def configure_child_logging(log_queue: Any, level: int = logging.DEBUG) -> None:
    """Call first thing in a child process (ImageWorker, archiver …)."""
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.addHandler(StructuredQueueHandler(log_queue))
    root.setLevel(level)


def drain(q: "queue_mod.Queue[Any]", limit: int) -> list[Any]:
    """Non-blocking read of up to ``limit`` items (used by tests/GUI)."""
    items = []
    for _ in range(limit):
        try:
            items.append(q.get_nowait())
        except queue_mod.Empty:
            break
    return items

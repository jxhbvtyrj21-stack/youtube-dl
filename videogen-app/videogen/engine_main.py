"""Engine process entry point (ARCHITECTURE.md §3, §3.1).

The GUI starts this in a separate process (``multiprocessing`` spawn) with
two queues. The command loop never blocks without a timeout; a heartbeat is
sent every second; if the GUI process disappears the engine shuts down
(and, on Windows, the GUI's Job Object kills it anyway).
"""

from __future__ import annotations

import logging
import os
import queue as queue_mod
import threading
import time
from pathlib import Path
from typing import Any

from videogen.config.settings import settings_from_dict
from videogen.core import events as ev
from videogen.core.models import BatchState

log = logging.getLogger("videogen.engine")

HEARTBEAT_S = 1.0
COMMAND_POLL_S = 0.25
MAX_LIFETIME_S = 30 * 24 * 3600     # absolute bound for the command loop
NON_DROPPABLE_RETRIES = 10


class EventSink:
    """Bounded, back-pressure-aware delivery to the GUI (§3.1)."""

    def __init__(self, q: Any) -> None:
        self.q = q
        self.dropped = 0

    def __call__(self, e: ev.Event) -> None:
        droppable = isinstance(e, ev.DROPPABLE_EVENTS)
        for _ in range(1 if droppable else NON_DROPPABLE_RETRIES):
            try:
                self.q.put(e, timeout=0.5)
                return
            except queue_mod.Full:
                continue
        self.dropped += 1


def engine_main(commands_q: Any, events_q: Any, appdata: str, settings_data: dict[str, Any],
                parent_pid: int) -> None:
    from videogen.core.engine import Engine
    from videogen.utils.system import redirect_missing_std_streams, write_crash_report

    redirect_missing_std_streams(Path(appdata) / "logs")
    settings, warnings = settings_from_dict(settings_data)
    sink = EventSink(events_q)
    try:
        engine = Engine(Path(appdata), settings, sink)
    except Exception as exc:  # noqa: BLE001 - report why the engine could not start
        write_crash_report(Path(appdata), "engine start", exc)
        from videogen.core.errors import VideoGenError
        msg = exc.user_message if isinstance(exc, VideoGenError) else "Не вдалося запустити обробник відео."
        sink(ev.EngineError(time.time(), msg, repr(exc)))
        try:
            events_q.cancel_join_thread()
        except (AttributeError, OSError, ValueError):
            log.debug("events queue already closed")
        return
    log.info("engine started pid=%s parent=%s", os.getpid(), parent_pid)
    for w in warnings:
        log.warning("settings: %s", w)
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(HEARTBEAT_S):
            sink(ev.Heartbeat(time.time(), engine.batch_state))
            if not _alive(parent_pid):
                log.error("GUI process %s is gone; engine shutting down", parent_pid)
                stop.set()

    hb = threading.Thread(target=heartbeat, name="heartbeat", daemon=True)
    try:
        engine.startup()
        hb.start()
        deadline = time.monotonic() + MAX_LIFETIME_S
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                cmd = commands_q.get(timeout=COMMAND_POLL_S)
            except queue_mod.Empty:
                continue
            except (EOFError, OSError):
                break
            if isinstance(cmd, ev.Shutdown) or cmd is None:
                break
            try:
                engine.handle(cmd)
            except Exception as exc:  # noqa: BLE001 - one bad command must not kill the engine
                log.exception("command %r failed", cmd)
                sink(ev.EngineError(time.time(), "Внутрішня помилка під час виконання команди.", repr(exc)))
    finally:
        stop.set()
        engine.shutdown()
        # If the GUI is gone nobody drains events_q; without this the queue's
        # feeder thread would block interpreter exit forever on a full pipe.
        try:
            events_q.cancel_join_thread()
        except (AttributeError, OSError, ValueError):
            log.debug("events queue already closed")
        if engine.batch_state is not BatchState.IDLE:
            log.error("engine exited with a running batch")


def _alive(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except Exception:  # noqa: BLE001
        return os.getppid() == pid

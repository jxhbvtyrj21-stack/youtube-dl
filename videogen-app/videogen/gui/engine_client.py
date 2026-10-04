"""GUI side of the GUI <-> Engine channel (ARCHITECTURE.md §3.1).

Every method is non-blocking or strictly bounded: the Qt main thread never
waits on the Engine. If the Engine dies, the client reports it; restarting
spawns a fresh process (the OS-level Job Object guarantees the old one and
all its children are gone).
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import queue as queue_mod
import time
from pathlib import Path
from typing import Any

from videogen.config.settings import Settings
from videogen.core import events as ev
from videogen.ffmpeg_ctl.process_manager import JobObject, kill_tree

log = logging.getLogger(__name__)

EVENTS_QUEUE_MAX = 1000
COMMANDS_QUEUE_MAX = 100


class EngineClient:
    def __init__(self, appdata: Path, settings: Settings) -> None:
        self.appdata = Path(appdata)
        self.settings = settings
        self._ctx = multiprocessing.get_context("spawn")
        self._proc: Any = None
        self._commands: Any = None
        self._events: Any = None
        # VIDEOGEN_TEST_NO_JOB_OBJECT=1 (tests only): exercise the engine's own
        # "GUI is gone" shutdown path without the OS-level safety net.
        self._job = JobObject("engine" if os.environ.get("VIDEOGEN_TEST_NO_JOB_OBJECT") != "1" else "disabled")
        if os.environ.get("VIDEOGEN_TEST_NO_JOB_OBJECT") == "1":
            self._job.close()
            self._job.supported = False
        self.started_at = 0.0
        self.restarts = 0

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        from videogen.engine_main import engine_main
        self._commands = self._ctx.Queue(COMMANDS_QUEUE_MAX)
        self._events = self._ctx.Queue(EVENTS_QUEUE_MAX)
        self._proc = self._ctx.Process(
            target=engine_main, name="VideoGenEngine", daemon=False,
            args=(self._commands, self._events, str(self.appdata), self.settings.to_dict(), os.getpid()))
        self._proc.start()
        self._job.assign(self._proc.pid)
        self.started_at = time.monotonic()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.is_alive()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def restart(self) -> None:
        self.kill()
        self.restarts += 1
        self.start()

    def kill(self) -> None:
        if self._proc is None:
            return
        if self._proc.is_alive():
            kill_tree(self._proc.pid, wait_s=5)
        self._proc.join(timeout=5)
        self._close_queues()
        self._proc = None

    def request_shutdown(self) -> None:
        """Ask the engine to stop the batch and exit (non-blocking)."""
        self.send(ev.Shutdown())

    def finish_shutdown(self, deadline_s: float) -> bool:
        """Bounded wait for exit; kills the tree after the deadline."""
        if self._proc is None:
            return True
        self._proc.join(timeout=max(0.0, deadline_s))
        clean = not self._proc.is_alive()
        if not clean:
            log.error("engine did not exit in %.0fs; killing", deadline_s)
        self.kill()
        self._job.close()
        return clean

    def _close_queues(self) -> None:
        for q in (self._commands, self._events):
            if q is None:
                continue
            try:
                q.cancel_join_thread()
                q.close()
            except (OSError, ValueError, AttributeError):
                continue
        self._commands = self._events = None

    # ------------------------------------------------------------ messaging

    def send(self, cmd: ev.Command) -> bool:
        if self._commands is None:
            return False
        try:
            self._commands.put_nowait(cmd)
            return True
        except (queue_mod.Full, OSError, ValueError):
            log.warning("command dropped: %r", cmd)
            return False

    def poll(self, limit: int = 200) -> list[ev.Event]:
        out: list[ev.Event] = []
        if self._events is None:
            return out
        for _ in range(limit):
            try:
                out.append(self._events.get_nowait())
            except queue_mod.Empty:
                break
            except (OSError, ValueError, EOFError):
                break
        return out

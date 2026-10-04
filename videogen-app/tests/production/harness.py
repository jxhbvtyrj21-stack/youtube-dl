"""Real Engine process driven like the GUI does (EngineClient), plus helpers."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from videogen.config.settings import Settings
from videogen.core import events as ev
from videogen.core.models import BatchState
from videogen.gui.engine_client import EngineClient


class EngineHarness:
    def __init__(self, appdata: Path, settings: Settings) -> None:
        self.client = EngineClient(appdata, settings)
        self.events: list[ev.Event] = []
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self.on_event = None

    def start(self, ready_timeout: float = 120) -> "EngineHarness":
        self.client.start()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        assert self.wait_for(lambda: self.of(ev.EngineReady), ready_timeout), "engine did not start"
        return self

    def _read(self) -> None:
        while not self._stop.wait(0.05):
            for e in self.client.poll(500):
                with self.lock:
                    self.events.append(e)
                if self.on_event:
                    self.on_event(e)

    def of(self, cls):
        with self.lock:
            return [e for e in self.events if isinstance(e, cls)]

    def wait_for(self, pred, timeout: float, interval: float = 0.1) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return True
            time.sleep(interval)
        return bool(pred())

    @property
    def pid(self) -> int | None:
        return self.client.pid

    def run_batch(self, inp: Path, out: Path, ws: Path, *, orientation: str = "16:9",
                  timeout: float = 7200, mode: str = "A") -> list[ev.JobFinished]:
        n0 = len(self.of(ev.JobFinished))
        mark = len(self.events)
        self.client.send(ev.StartBatch(mode, orientation, str(inp), str(out), str(ws)))

        def done() -> bool:
            with self.lock:
                new = self.events[mark:]
            started = any(isinstance(e, ev.BatchStateChanged) and e.state is BatchState.RUNNING for e in new)
            idle = any(isinstance(e, ev.BatchStateChanged) and e.state is BatchState.IDLE for e in new)
            err = any(isinstance(e, ev.EngineError) for e in new)
            return (started and idle) or (err and not started)
        assert self.wait_for(done, timeout), "batch did not finish in time"
        return self.of(ev.JobFinished)[n0:]

    def stop(self) -> None:
        self.client.request_shutdown()
        self.client.finish_shutdown(30)
        self._stop.set()
        if self._reader:
            self._reader.join(5)

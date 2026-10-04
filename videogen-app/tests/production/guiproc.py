"""Run gui_harness.py as a separate OS process and follow its status lines."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

HARNESS = Path(__file__).with_name("gui_harness.py")


class GuiProcess:
    def __init__(self, appdata: Path, inp: Path, out: Path, ws: Path, mode: str, *, settings_json: str,
                 extra_env: dict[str, str] | None = None) -> None:
        env = dict(os.environ, VIDEOGEN_TEST_SETTINGS=settings_json, QT_QPA_PLATFORM="offscreen",
                   PYTHONIOENCODING="utf-8", **(extra_env or {}))
        self.proc = subprocess.Popen([sys.executable, str(HARNESS), str(appdata), str(inp), str(out), str(ws), mode],
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        self.status: list[dict] = []
        self.other: list[str] = []
        self._t = threading.Thread(target=self._read, daemon=True)
        self._t.start()

    def _read(self) -> None:
        for raw in iter(self.proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            try:
                self.status.append(json.loads(line))
            except ValueError:
                self.other.append(line)

    @property
    def last(self) -> dict:
        return self.status[-1] if self.status else {}

    def wait(self, pred, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.status and pred(self.last):
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.1)
        return False

    def kill(self) -> None:
        self.proc.kill()              # TerminateProcess on Windows: no cleanup code runs
        self.proc.wait(30)

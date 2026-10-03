"""GUI responsiveness while the engine processes a large job (ТЗ §41, §43)."""

from __future__ import annotations

import json
import os
import statistics
import time

import pytest
from PySide6.QtCore import QTimer

from videogen.config.settings import ImageSettings
from videogen.core.models import BatchState
from videogen.gui.main_window import MainWindow
from tests.fixtures import factory as F
from tests.pipeline_support import small_settings
from tests.stress.test_pipeline_stress import REPORTS

N = int(os.environ.get("VIDEOGEN_GUI_STRESS_IMAGES", "300"))


@pytest.mark.stress
@pytest.mark.timeout(3600)
def test_gui_latency_during_large_job(qtbot, tmp_path):
    d = tmp_path / "input" / "gui_stress"
    d.mkdir(parents=True)
    for i in range(N):
        F.jpg(d / f"{i:05d}.jpg", size=(640, 360))
    F.tone(d / "a.mp3", N * 0.4)
    w = MainWindow(tmp_path / "appdata", small_settings(images=ImageSettings(min_seconds_per_image=0.3)))
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: w.model.engine_ready, timeout=60000)
    w.in_pick.set_path(str(tmp_path / "input"))
    w.out_pick.set_path(str(tmp_path / "output"))
    w.ws_pick.set_path(str(tmp_path / "ws"))
    w.btn_start.click()
    lat: list[float] = []
    deadline = time.monotonic() + 3000
    started = False
    while time.monotonic() < deadline:
        t0 = time.monotonic()
        QTimer.singleShot(0, lambda t0=t0: lat.append(time.monotonic() - t0))
        qtbot.wait(50)
        started = started or w.model.batch_state is not BatchState.IDLE
        if started and w.model.batch_state is BatchState.IDLE and w.model.succeeded == 1:
            break
    assert w.model.succeeded == 1
    lat.sort()
    p50, p99, mx = statistics.median(lat), lat[int(len(lat) * 0.99)], lat[-1]
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "gui_latency.json").write_text(json.dumps(
        {"images": N, "samples": len(lat), "p50_ms": p50 * 1000, "p99_ms": p99 * 1000, "max_ms": mx * 1000,
         "median_tick_ms": statistics.median(w.tick_durations) * 1000}))
    assert p99 < 0.1 and mx < 0.5, (p50, p99, mx)
    w.close()
    qtbot.waitUntil(lambda: w._may_close, timeout=40000)

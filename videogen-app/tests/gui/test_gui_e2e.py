"""GUI + real Engine process + real FFmpeg (offscreen)."""

from __future__ import annotations

import os
import shutil
import time

import psutil
import pytest
from PySide6.QtCore import QTimer

from videogen.config.settings import TimeoutPolicy
from videogen.core.models import BatchState
from videogen.gui.main_window import MainWindow
from tests.pipeline_support import make_job_folder, small_settings

pytestmark = pytest.mark.timeout(300)


def _window(qtbot, tmp_path, settings):
    w = MainWindow(tmp_path / "appdata", settings)
    qtbot.addWidget(w)
    w.show()
    qtbot.waitUntil(lambda: w.model.engine_ready, timeout=60000)
    w.in_pick.set_path(str(tmp_path / "input"))
    w.out_pick.set_path(str(tmp_path / "output"))
    w.ws_pick.set_path(str(tmp_path / "ws"))
    return w


def _close(qtbot, w):
    pid = w.client.pid
    w.close()
    qtbot.waitUntil(lambda: w._may_close, timeout=40000)
    assert pid is None or not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def _latency_probe(qtbot, seconds: float) -> list[float]:
    """Measure how late zero-delay timers fire on the GUI thread."""
    lat: list[float] = []
    end = time.monotonic() + seconds

    def shoot():
        t0 = time.monotonic()
        QTimer.singleShot(0, lambda: lat.append(time.monotonic() - t0))
    for _ in range(int(seconds / 0.05)):
        if time.monotonic() > end:
            break
        shoot()
        qtbot.wait(50)
    return lat


def test_start_to_finished_video(qtbot, tmp_path):
    (tmp_path / "input").mkdir()
    make_job_folder(tmp_path / "input", "Відео з GUI", n_images=3, audio_s=2.5)
    w = _window(qtbot, tmp_path, small_settings())
    w.btn_start.click()
    qtbot.waitUntil(lambda: w.model.batch_state is BatchState.RUNNING or w.model.succeeded == 1, timeout=30000)
    qtbot.waitUntil(lambda: w.model.succeeded == 1 and w.model.batch_state is BatchState.IDLE, timeout=180000)
    assert (tmp_path / "output" / "Відео з GUI.mp4").exists()
    assert w.c_ok.value.text() == "1" and w.overall.value() == 1000
    assert "успішно" in w.log_view.toPlainText()
    assert w.btn_start.isEnabled()
    _close(qtbot, w)


@pytest.mark.skipif(os.name == "nt", reason="POSIX wrapper")
def test_gui_responsive_while_ffmpeg_hangs_and_stop_works(qtbot, tmp_path, monkeypatch):
    from tests.pipeline_support import ffmpeg_wrapper
    wrap = tmp_path / "wrap"
    wrap.mkdir()
    ff, _ = ffmpeg_wrapper(wrap, "hang")
    os.symlink(shutil.which("ffprobe"), wrap / "ffprobe")
    monkeypatch.setenv("VIDEOGEN_FFMPEG_DIR", str(wrap))
    (tmp_path / "input").mkdir()
    make_job_folder(tmp_path / "input", "hang", n_images=2, audio_s=2)
    s = small_settings(timeouts=TimeoutPolicy(stall_min_s=600, segment_base_s=600))   # only STOP can end it
    w = _window(qtbot, tmp_path, s)
    w.btn_start.click()
    qtbot.waitUntil(lambda: "Рендеринг" in w.stage_label.text(), timeout=60000)
    qtbot.wait(1000)
    engine = psutil.Process(w.client.pid)
    assert any("wrap.py" in " ".join(c.cmdline()) for c in engine.children(recursive=True))
    lat = _latency_probe(qtbot, 4.0)
    assert lat and max(lat) < 0.2, max(lat)             # GUI stays responsive
    assert w.btn_stop.isEnabled() and w.btn_open_log.isEnabled()
    t0 = time.monotonic()
    w.btn_stop.click()
    qtbot.waitUntil(lambda: w.model.batch_state is BatchState.IDLE, timeout=30000)
    assert time.monotonic() - t0 < 20
    assert not any("wrap.py" in " ".join(c.cmdline()) for c in engine.children(recursive=True))
    assert w.model.cancelled == 1
    _close(qtbot, w)


def test_engine_crash_then_restart_offers_recovery(qtbot, tmp_path):
    (tmp_path / "input").mkdir()
    make_job_folder(tmp_path / "input", "long", n_images=6, audio_s=20)
    import dataclasses
    s = small_settings(video=dataclasses.replace(small_settings().video, preset="slow"))
    w = _window(qtbot, tmp_path, s)
    w.btn_start.click()
    qtbot.waitUntil(lambda: "Рендеринг" in w.stage_label.text(), timeout=90000)
    engine = psutil.Process(w.client.pid)
    tree = [engine.pid] + [c.pid for c in engine.children(recursive=True)]
    for p in reversed(tree):
        try:
            psutil.Process(p).kill()
        except psutil.Error:
            pass
    qtbot.waitUntil(lambda: not w.engine_banner.isHidden(), timeout=10000)
    assert not w.btn_start.isEnabled()
    w.btn_restart_engine.click()
    qtbot.waitUntil(lambda: w._recovery is not None, timeout=60000)
    w._recovery.accept()                          # default: Resume
    qtbot.waitUntil(lambda: w.model.succeeded == 1, timeout=240000)
    assert (tmp_path / "output" / "long.mp4").exists()
    _close(qtbot, w)

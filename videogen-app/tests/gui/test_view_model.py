from __future__ import annotations

import time

import pytest

from videogen.core import events as ev
from videogen.core.models import BatchState, ErrorInfo, JobStatus, Stage
from videogen.gui.progress import MAX_LOG_LINES, ViewModel, buttons_for

T = time.time()


def test_button_matrix():
    b = buttons_for(BatchState.IDLE, True, True)
    assert (b.start, b.pause, b.resume, b.stop, b.cancel_current, b.inputs) == (True, False, False, False, False, True)
    b = buttons_for(BatchState.RUNNING, True, True)
    assert (b.start, b.pause, b.resume, b.stop, b.cancel_current, b.inputs) == (False, True, False, True, True, False)
    b = buttons_for(BatchState.PAUSED, True, True)
    assert (b.pause, b.resume, b.stop) == (False, True, True)
    assert not buttons_for(BatchState.IDLE, False, True).start          # engine not ready yet
    assert not buttons_for(BatchState.IDLE, True, False).start          # engine dead
    for st in BatchState:
        bb = buttons_for(st, True, True)
        assert not (bb.start and bb.stop)                                 # never both


def test_reducer_flow():
    m = ViewModel()
    m.apply(ev.EngineReady(T, "0.5", "6.1"))
    m.apply(ev.JobQueued(T, "j1", "Відео 1"))
    m.apply(ev.JobQueued(T, "j2", "Відео 2"))
    m.apply(ev.BatchStateChanged(T, BatchState.RUNNING))
    m.apply(ev.JobStageChanged(T, "j1", Stage.RENDERING, 1))
    m.apply(ev.JobProgress(T, "j1", Stage.RENDERING, 50.0, frame=120, total_frames=240, fps=30, speed=1.2))
    assert m.overall_percent == pytest.approx(25.0)
    assert "кадр 120/240" in m.stage_text and "Відео 1" in m.stage_text
    m.apply(ev.JobFinished(T, "j1", JobStatus.SUCCESS, "/out/a.mp4"))
    m.apply(ev.JobFinished(T, "j2", JobStatus.FAILED, None, ErrorInfo("INPUT", "X", "Аудіофайл пошкоджений.")))
    m.apply(ev.BatchCounters(T, 2, 1, 0, 1, 0, 3, 42.0))
    m.apply(ev.BatchStateChanged(T, BatchState.IDLE))
    assert m.overall_percent == 100.0
    assert (m.succeeded, m.failed, m.skipped_files) == (1, 1, 3)
    assert any("Аудіофайл пошкоджений" in line for line in m.log)
    assert m.eta_text() == "—"                              # not running any more
    m.apply(ev.BatchStateChanged(T, BatchState.RUNNING))
    assert m.eta_text() == "0:00:42"


def test_log_is_bounded():
    m = ViewModel()
    for i in range(MAX_LOG_LINES * 3):
        m.apply(ev.LogLine(T, "INFO", f"line {i}"))
    assert len(m.log) == MAX_LOG_LINES and m.log_seq == MAX_LOG_LINES * 3


def test_engine_errors_and_interrupted():
    m = ViewModel()
    m.apply(ev.EngineError(T, "FFmpeg не знайдено."))
    m.apply(ev.InterruptedJobsFound(T, ("a", "b"), ("Відео A", "Відео B")))
    assert m.errors == ["FFmpeg не знайдено."]
    assert m.interrupted == [("a", "Відео A"), ("b", "Відео B")]


def test_progress_never_goes_backwards_within_stage():
    m = ViewModel()
    m.apply(ev.JobProgress(T, "j", Stage.RENDERING, 60.0))
    m.apply(ev.JobProgress(T, "j", Stage.RENDERING, 55.0))   # late, out-of-order event
    assert m.jobs["j"].percent == 60.0

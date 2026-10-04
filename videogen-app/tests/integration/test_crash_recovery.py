"""Acceptance: kill the application mid-render, restart, find the interrupted
job, resume it from the first unfinished segment, and never touch outputs
that were already finished."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from videogen.core import events as ev
from videogen.core.engine import Engine
from videogen.core.models import JobStatus, RecoveryAction
from videogen.utils.hashing import sha256_file
from tests.helpers import wait_until
from tests.pipeline_support import Events, make_job_folder, small_settings

DRIVER = Path(__file__).resolve().parents[1] / "engine_driver.py"


def _db_rows(db: Path):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            return con.execute("SELECT name, status, stage, resume_from_segment FROM jobs").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX session kill")
@pytest.mark.timeout(400)
def test_kill_during_render_then_resume(tmp_path, monkeypatch):
    d = {k: tmp_path / k for k in ("input", "output", "ws", "appdata")}
    d["input"].mkdir()
    make_job_folder(d["input"], "1_first", n_images=2, audio_s=2)
    make_job_folder(d["input"], "2_long", n_images=8, audio_s=24)
    s = small_settings(video=dataclasses.replace(small_settings().video, preset="slow"))
    sfile = tmp_path / "settings.json"
    sfile.write_text(json.dumps(s.to_dict()))

    proc = subprocess.Popen([sys.executable, str(DRIVER), str(d["appdata"]), str(d["input"]), str(d["output"]),
                             str(d["ws"]), str(sfile)], start_new_session=True)
    db = d["appdata"] / "state.db"

    def mid_render():
        rows = {r[0]: r for r in _db_rows(db)}
        return ("2_long" in rows and rows["2_long"][2] == "RENDERING" and rows["2_long"][3] >= 2
                and rows.get("1_first", (0, ""))[1] == "SUCCESS")
    assert wait_until(mid_render, 300, 0.05), _db_rows(db)

    tree = [proc.pid] + [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
    os.killpg(proc.pid, 9)                       # like a power cut / Task Manager kill
    proc.wait(10)
    assert wait_until(lambda: not any(psutil.pid_exists(p) and psutil.Process(p).status() != "zombie"
                                      for p in tree), 10)

    first_out = d["output"] / "1_first.mp4"
    first_sha = sha256_file(first_out)[0]
    resume_point = {r[0]: r[3] for r in _db_rows(db)}["2_long"]

    # ---- restart
    from videogen.core import pipeline as pl
    seg_calls = []
    real = pl.run_ffmpeg

    def spy(argv, **kw):
        if kw.get("label", "").startswith("ffmpeg-seg"):
            seg_calls.append(kw["label"])
        return real(argv, **kw)
    monkeypatch.setattr(pl, "run_ffmpeg", spy)

    events = Events()
    eng = Engine(d["appdata"], s, events)
    try:
        interrupted = eng.startup()
        assert [j.name for j in interrupted] == ["2_long"]
        found = events.of(ev.InterruptedJobsFound)
        assert found and found[0].names == ("2_long",)
        assert not list(d["output"].glob(".*.part"))
        eng.handle(ev.RecoveryDecision(interrupted[0].job_id, RecoveryAction.RESUME))
        assert eng.wait_idle(300)
        long_job = eng.state.get_job(interrupted[0].job_id)
        assert long_job.status is JobStatus.SUCCESS, long_job.error
        assert len(seg_calls) == 8 - resume_point, (seg_calls, resume_point)   # finished segments reused
        assert sha256_file(first_out)[0] == first_sha                           # finished output untouched
        assert sorted(p.name for p in d["output"].glob("*.mp4")) == ["1_first.mp4", "2_long.mp4"]
    finally:
        eng.shutdown()


def test_ignore_and_retry_choices(tmp_path):
    from tests.helpers import make_config
    from videogen.config.settings import Settings
    appdata = tmp_path / "appdata"
    eng = Engine(appdata, Settings(), lambda e: None, install_logging=False)
    try:
        ws = tmp_path / "ws" / "vg-b1" / "j0001"
        (tmp_path / "ws" / "vg-b1").mkdir(parents=True)
        (tmp_path / "ws" / "vg-b1" / ".videogen-workspace").write_text("b1")
        ws.mkdir()
        (ws / "junk.tmp.mp4").write_bytes(b"x")
        cfg = make_config("x")
        eng.state.add_job(cfg, 1, str(ws))
        eng.state.transition(cfg.job_id, JobStatus.RUNNING)
        eng.state.mark_running_as_interrupted()
        eng.recover(cfg.job_id, RecoveryAction.IGNORE)
        assert eng.state.get_job(cfg.job_id).status is JobStatus.CANCELLED
        assert not ws.exists()
    finally:
        eng.shutdown()


def test_second_engine_on_same_data_is_refused(tmp_path):
    from videogen.config.settings import Settings
    from videogen.core.errors import AlreadyRunningError
    a = Engine(tmp_path / "appdata", Settings(), lambda e: None, install_logging=False)
    try:
        with pytest.raises(AlreadyRunningError):
            Engine(tmp_path / "appdata", Settings(), lambda e: None, install_logging=False)
    finally:
        a.shutdown()
    b = Engine(tmp_path / "appdata", Settings(), lambda e: None, install_logging=False)   # lock released
    b.shutdown()


def _engine_proc(cq, eq, appdata):
    from videogen.config.settings import Settings
    from videogen.engine_main import engine_main
    engine_main(cq, eq, appdata, Settings().to_dict(), parent_pid=-1)


def test_engine_exits_even_if_nobody_reads_events(tmp_path):
    """GUI gone + full event pipe must not hang engine shutdown."""
    import multiprocessing
    from videogen.core import events as ev
    ctx = multiprocessing.get_context("spawn")
    cq, eq = ctx.Queue(), ctx.Queue(1000)
    p = ctx.Process(target=_engine_proc, args=(cq, eq, str(tmp_path / "appdata")))
    p.start()
    bad = ev.StartBatch("A", "16:9", str(tmp_path / ("x" * 200)), str(tmp_path / "o"), "")
    for _ in range(400):                     # each produces EngineError + LogLine (~1 KB): > pipe buffer
        cq.put(bad)
    time.sleep(3)                            # heartbeats pile up, nobody reads eq
    cq.put(ev.Shutdown())
    p.join(30)
    alive = p.is_alive()
    if alive:
        p.kill()
    assert not alive, "engine hung on exit with an unread event queue"


def _engine_proc_with_parent(cq, eq, appdata, settings, parent_pid):
    from videogen.config.settings import settings_from_dict
    from videogen.engine_main import engine_main
    engine_main(cq, eq, appdata, settings_from_dict(settings)[0].to_dict(), parent_pid=parent_pid)


def test_engine_does_not_wait_on_full_channel_once_gui_is_gone(tmp_path):
    """Regression (production test 8): with the GUI gone and the event channel
    full, every non-droppable event still waited 5 s for room that could never
    come: cancellation reached FFmpeg 5 s late and shutdown took 5 s per event
    (per queued job) instead of being immediate."""
    import multiprocessing
    import queue as queue_mod
    from videogen.core.state_manager import StateManager
    from tests.pipeline_support import start_cmd
    ctx = multiprocessing.get_context("spawn")
    gui = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    cq, eq = ctx.Queue(), ctx.Queue(1000)
    inp = tmp_path / "in"
    for i in range(20):
        make_job_folder(inp, f"j{i:02d}", n_images=3, audio_s=6.0)
    p = ctx.Process(target=_engine_proc_with_parent,
                    args=(cq, eq, str(tmp_path / "appdata"), small_settings().to_dict(), gui.pid))
    p.start()
    try:
        seen: list = []

        def drain_until(pred, timeout):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                try:
                    e = eq.get(timeout=0.2)
                except queue_mod.Empty:
                    continue
                seen.append(e)
                if pred(e):
                    return True
            return False

        assert drain_until(lambda e: isinstance(e, ev.EngineReady), 60)
        cq.put(start_cmd(inp, tmp_path / "out", tmp_path / "ws"))
        assert drain_until(lambda e: isinstance(e, ev.JobStageChanged), 60)
        for _ in range(2000):                    # stop reading and fill the channel up
            try:
                eq.put_nowait("x" * 1024)   # > pipe buffer on every OS
            except queue_mod.Full:
                break
        gui.kill()
        gui.wait(10)
        t0 = time.monotonic()
        p.join(60)
        exit_s = time.monotonic() - t0
        alive = p.is_alive()
    finally:
        if p.is_alive():
            p.kill()
            p.join(10)
        if gui.poll() is None:
            gui.kill()
        # Nobody will read the channel again: without this the fillers still
        # buffered in this process would block interpreter exit (Windows pipes
        # are small) - the very hang the engine avoids the same way.
        for q in (eq, cq):
            q.cancel_join_thread()
    assert not alive
    assert exit_s < 12, f"engine took {exit_s:.1f} s to exit after the GUI vanished"
    s = StateManager(tmp_path / "appdata" / "state.db")
    try:
        statuses = {j.status for j in s.list_jobs()}
    finally:
        s.close()
    assert statuses <= {JobStatus.INTERRUPTED, JobStatus.SUCCESS} and JobStatus.INTERRUPTED in statuses


def test_gui_loss_leaves_job_resumable_with_workspace(tmp_path):
    """Regression (production tests 6/9): graceful engine shutdown because the
    GUI vanished must keep the job resumable — and Resume must reuse work."""
    import dataclasses
    from tests.pipeline_support import make_job_folder, small_settings, start_cmd
    from tests.helpers import wait_until
    from videogen.core import pipeline as pl
    d = {k: tmp_path / k for k in ("input", "output", "ws", "appdata")}
    make_job_folder(d["input"], "довгий", n_images=8, audio_s=16)
    s = small_settings(video=dataclasses.replace(small_settings().video, preset="medium"))
    eng = Engine(d["appdata"], s, lambda e: None)
    eng.startup()
    eng.start_batch(ev.StartBatch("A", "16:9", str(d["input"]), str(d["output"]), str(d["ws"])))
    assert wait_until(lambda: (eng.state.list_jobs() or [None])[0] is not None
                      and eng.state.list_jobs()[0].resume_from_segment >= 2, 120)
    eng.shutdown(interrupt=True)
    eng2 = Engine(d["appdata"], s, lambda e: None)
    calls = []
    real = pl.run_ffmpeg
    pl.run_ffmpeg = lambda argv, **kw: (calls.append(kw.get("label")), real(argv, **kw))[1]
    try:
        interrupted = eng2.startup()
        assert [j.name for j in interrupted] == ["довгий"]
        point = interrupted[0].resume_from_segment
        assert point >= 2
        assert list(d["ws"].rglob("s0000*.mp4"))           # verified segments were kept
        eng2.recover(interrupted[0].job_id, RecoveryAction.RESUME)
        assert eng2.wait_idle(300)
        assert eng2.state.get_job(interrupted[0].job_id).status is JobStatus.SUCCESS
        assert len([c for c in calls if c and c.startswith("ffmpeg-seg")]) == 8 - point
    finally:
        pl.run_ffmpeg = real
        eng2.shutdown()

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

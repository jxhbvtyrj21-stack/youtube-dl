"""Gap F: the process dies after the final video was published (atomic rename
into the output folder) but before the archive and before SUCCESS is written.
The Engine runs in a separate process (tests/engine_driver.py) that stops at
exactly that point and is killed there (TerminateProcess / SIGKILL)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from videogen.core.engine import Engine
from videogen.core.models import JobStatus, RecoveryAction
from videogen.media.media_validator import probe_media
from videogen.config.settings import TimeoutPolicy
from videogen.utils.hashing import sha256_file
from tests.fixtures import factory as F
from tests.helpers import wait_until
from tests.pipeline_support import Events, make_job_folder, small_settings

DRIVER = Path(__file__).resolve().parents[1] / "engine_driver.py"


def _row(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
    try:
        return con.execute("SELECT status, stage, output_file, workspace_dir FROM jobs").fetchone()
    finally:
        con.close()


def _crash_after_publish(tmp_path: Path):
    d = {k: tmp_path / k for k in ("input", "output", "ws", "appdata")}
    d["input"].mkdir()
    make_job_folder(d["input"], "фінал", n_images=3, audio_s=4.0)
    sfile = tmp_path / "settings.json"
    sfile.write_text(json.dumps(small_settings().to_dict()))
    marker = tmp_path / "published.txt"
    env = dict(os.environ, VIDEOGEN_DRIVER_HANG_AFTER_PUBLISH=str(marker))
    proc = subprocess.Popen([sys.executable, str(DRIVER), str(d["appdata"]), str(d["input"]), str(d["output"]),
                             str(d["ws"]), str(sfile)], env=env)
    try:
        assert wait_until(marker.exists, 300, 0.05), "the job never reached the published state"
        tree = [proc] + psutil.Process(proc.pid).children(recursive=True)
        for p in tree:                       # power cut / Task Manager: no cleanup code runs
            try:
                p.kill()
            except psutil.Error:
                pass
        proc.wait(30)
    finally:
        if proc.poll() is None:
            proc.kill()
    published = Path(marker.read_text(encoding="utf-8"))
    return d, published


def _ffmpeg_spy(monkeypatch):
    from videogen.core import pipeline as pl
    labels = []
    real = pl.run_ffmpeg

    def spy(argv, **kw):
        labels.append(kw.get("label") or "")
        return real(argv, **kw)
    monkeypatch.setattr(pl, "run_ffmpeg", spy)
    return labels


def _frames(p: Path) -> int:
    return probe_media(F.FFPROBE, p, TimeoutPolicy()).video_frames


@pytest.mark.timeout(600)
def test_crash_after_publish_is_interrupted_and_resume_reuses_the_video(tmp_path, monkeypatch):
    d, published = _crash_after_publish(tmp_path)
    db = d["appdata"] / "state.db"
    status, stage, output_file, wsd = _row(db)
    sha = sha256_file(published)[0]
    manifest = json.loads((Path(wsd) / "manifest.json").read_text(encoding="utf-8"))
    # at the moment of the crash: never SUCCESS, but already linked to the published video
    assert status == "RUNNING" and stage in ("FINALIZING", "ARCHIVING"), (status, stage)
    assert output_file == str(published) and published.exists()
    assert manifest["output_file"] == str(published) and manifest["output_sha256"] == sha

    labels = _ffmpeg_spy(monkeypatch)
    eng = Engine(d["appdata"], small_settings(), Events())
    try:
        interrupted = eng.startup()
        assert [j.name for j in interrupted] == ["фінал"]
        assert interrupted[0].status is JobStatus.INTERRUPTED
        eng.recover(interrupted[0].job_id, RecoveryAction.RESUME)
        assert eng.wait_idle(300)
        job = eng.state.get_job(interrupted[0].job_id)
    finally:
        eng.shutdown()
    assert job.status is JobStatus.SUCCESS
    assert job.output_file == str(published)                     # the same file: no duplicate
    assert sha256_file(published)[0] == sha                      # untouched
    assert sorted(p.name for p in d["output"].glob("*.mp4")) == ["фінал.mp4"]
    assert not [x for x in labels if x.startswith("ffmpeg-seg") or x == "ffmpeg-mux"], labels
    assert list((d["output"] / "_archive").glob("фінал*.zip"))
    assert not list(d["output"].rglob(".*.part"))
    assert not [p for p in d["ws"].rglob("*") if p.is_file() and p.name != ".videogen-workspace"]


@pytest.mark.timeout(600)
def test_crash_after_publish_then_ignore_keeps_the_verified_video_linked(tmp_path):
    d, published = _crash_after_publish(tmp_path)
    sha = sha256_file(published)[0]
    eng = Engine(d["appdata"], small_settings(), Events())
    try:
        [job] = eng.startup()
        eng.recover(job.job_id, RecoveryAction.IGNORE)
        job = eng.state.get_job(job.job_id)
    finally:
        eng.shutdown()
    # documented semantics: CANCELLED (the job was not completed: no archive),
    # the already verified video is kept and stays linked to the job
    assert job.status is JobStatus.CANCELLED
    assert job.output_file == str(published)
    assert published.exists() and sha256_file(published)[0] == sha and _frames(published) > 0
    assert not list((d["output"] / "_archive").glob("*")) if (d["output"] / "_archive").exists() else True
    assert not list(d["output"].rglob(".*.part"))
    assert not [p for p in d["ws"].rglob("*") if p.is_file() and p.name != ".videogen-workspace"]


@pytest.mark.timeout(600)
def test_resume_does_not_reuse_a_published_file_that_was_changed(tmp_path, monkeypatch):
    """_existing_final recognises our file only by its SHA-256: a file that
    was altered after the crash must not be adopted as the job's result."""
    d, published = _crash_after_publish(tmp_path)
    with open(published, "r+b") as fh:                     # damage the published file
        fh.seek(published.stat().st_size // 2)
        fh.write(b"\0" * 4096)
    labels = _ffmpeg_spy(monkeypatch)
    eng = Engine(d["appdata"], small_settings(), Events())
    try:
        [job] = eng.startup()
        eng.recover(job.job_id, RecoveryAction.RESUME)
        assert eng.wait_idle(300)
        job = eng.state.get_job(job.job_id)
    finally:
        eng.shutdown()
    assert job.status is JobStatus.SUCCESS
    assert job.output_file != str(published)                     # the altered file is not claimed
    assert "ffmpeg-mux" in labels                               # the result was rebuilt and re-verified
    assert _frames(Path(job.output_file)) > 0

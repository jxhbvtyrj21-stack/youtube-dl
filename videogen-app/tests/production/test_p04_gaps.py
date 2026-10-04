"""Gap-closing tests (GAP_CLOSING_REPORT.md): scenarios the first production
series did not cover. Real FFmpeg, real files, real volumes; the Engine runs
in this process so a failure can be injected at an exact point of the
pipeline without test hooks in the product."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil
import pytest

from videogen.config.settings import ResourceLimits
from videogen.core import events as ev
from videogen.core import pipeline as pl
from videogen.core.engine import Engine
from videogen.core.models import BatchState, JobStatus
from videogen.ffmpeg_ctl.process_manager import REGISTRY
from videogen.utils.hashing import sha256_file
from tests.production import monitor
from tests.production.common import assert_no_media_processes, expected_frames, frames, prod_settings, ws_files
from tests.production.media_sets import normal_set
from tests.pipeline_support import Events

SMALL = os.environ.get("VIDEOGEN_SMALL_DISK")


def _fill(volume: Path, leave_bytes: int) -> Path:
    """Write a ballast file so that only ``leave_bytes`` stay free."""
    ballast = volume / "ballast.bin"
    free = psutil.disk_usage(str(volume)).free
    left = free - leave_bytes
    chunk = b"\0" * (4 * 1024 * 1024)
    with open(ballast, "wb") as fh:
        while left > 0:
            n = min(len(chunk), left)
            try:
                fh.write(chunk[:n])
                fh.flush()
            except OSError:
                break
            left -= n
    return ballast


def _own_children() -> list[str]:
    """Live child processes of this (test + in-process Engine) process."""
    out = []
    for c in psutil.Process().children(recursive=True):
        try:
            if "resource_tracker" not in monitor._cmd(c) and c.status() != psutil.STATUS_ZOMBIE:
                out.append(f"{c.pid} {monitor._cmd(c)[:80]}")
        except psutil.Error:
            continue
    return out


def _wait(pred, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.2)
    return bool(pred())


# ---------------------------------------------------------------- G

@pytest.mark.timeout(1800)
@pytest.mark.skipif(not SMALL, reason="needs VIDEOGEN_SMALL_DISK (a small real volume)")
def test_g_disk_full_during_archiving(work, record, baseline, monkeypatch):
    record.update(number="G", title="DISK FULL DURING ARCHIVING (відео вже опубліковане)", input=(
        f"2 jobs; output і архів на малому томі {SMALL}; коли job 1 доходить до ARCHIVING, том "
        "заповнюється баластом (лишається 256 КБ) — справжній ENOSPC у процесі архівування"),
        expected="job 1: FAILED DISK_SPACE (не SUCCESS, без повторів), повідомлення каже, що відео створено, "
                 "а архів — ні; опубліковане відео не змінене і зв'язане з job у БД; часткового архіву немає; "
                 "workspace прибрано; пакет PAUSED, job 2 не стартує на повний диск; після звільнення місця і "
                 "Resume job 2 — SUCCESS з архівом; жодного процесу архіватора")
    vol = Path(SMALL)
    base = vol / f"g-{int(time.time())}"
    out = base / "out"
    inp, ws = work / "in", work / "ws"
    normal_set(inp / "1 архів не влізе", 6, seconds_per_image=2.0)
    normal_set(inp / "2 наступний", 3, seconds_per_image=2.0)
    real_archive = pl._run_archive_process
    ballast: list[Path] = []

    def archive_on_full_disk(dest, entries, max_bytes, s, ctx):
        if not ballast:                                    # only the first job's archive
            ballast.append(_fill(vol, 256 * 1024))
        return real_archive(dest, entries, max_bytes, s, ctx)
    monkeypatch.setattr(pl, "_run_archive_process", archive_on_full_disk)

    events = Events()
    s = prod_settings(resources=ResourceLimits(disk_reserve_mb=64, ram_available_min_mb=256))
    eng = Engine(work / "appdata", s, events)
    eng.startup()
    try:
        eng.start_batch(ev.StartBatch("A", "16:9", str(inp), str(out), str(ws)))
        assert _wait(lambda: events.of(ev.JobFinished), 900), "job 1 never finished"
        _wait(lambda: eng.batch_state is BatchState.PAUSED, 30)
        time.sleep(3)                                      # a paused batch must not start job 2
        batch_after_failure = eng.batch_state.value
        jobs = {j.name: j for j in eng.state.list_jobs()}
        a, b = jobs["1 архів не влізе"], jobs["2 наступний"]
        videos = sorted(p.name for p in out.glob("*.mp4"))
        archives = sorted(p.name for p in (out / "_archive").glob("*")) if (out / "_archive").exists() else []
        diag = json.loads((work / "appdata" / "diagnostics" / a.job_id / "manifest.json").read_text(encoding="utf-8"))
        video_a = out / "1 архів не влізе.mp4"
        sha_ok = video_a.exists() and sha256_file(video_a)[0] == diag.get("output_sha256")
        frames_ok = video_a.exists() and frames(video_a) == expected_frames(12.0)
        pending = eng.state.pending_cleanups()
        archivers = _own_children()
        state_a = (a.status.value, a.error.error_class if a.error else "", a.error.code if a.error else "",
                   a.attempts, a.output_file)
        b_before = b.status.value
        for p in ballast:
            p.unlink()
        if eng.batch_state is BatchState.PAUSED:
            eng.handle(ev.Resume())
        idle = eng.wait_idle(900)
        b = eng.state.get_job(b.job_id)
        b_archive = list((out / "_archive").glob("2 наступний*.zip"))
    finally:
        for p in ballast:
            if p.exists():
                p.unlink()
        eng.shutdown()
    record["resource_usage"] = {
        "batch_after_failure": batch_after_failure, "batch_idle_at_end": idle, "job1": state_a, "job1_message": a.error.message[:160] if a.error else "", "videos": videos,
        "archive_dir_after_failure": archives, "video1_sha_matches_manifest": sha_ok,
        "video1_frames_ok": frames_ok, "job2_status_while_paused": b_before, "job2_final": b.status.value,
        "job2_archive": [p.name for p in b_archive], "workspace_files": len(ws_files(ws)),
        "pending_cleanups": len(pending), "child_processes_after_failure": archivers, "registry": REGISTRY.snapshot(),
    }
    record["actual"] = "; ".join(f"{k}: {v}" for k, v in record["resource_usage"].items())
    try:
        assert a.status is JobStatus.FAILED and a.error is not None
        assert (a.error.error_class, a.error.code) == ("RESOURCE", "DISK_SPACE"), a.error
        assert batch_after_failure == "PAUSED"                     # a disk problem pauses the batch
        assert a.attempts == 1                                     # no pointless retry on a full disk
        assert "1 архів не влізе.mp4" in a.error.message           # the user is told the video exists
        assert a.output_file == str(video_a)                       # state is linked to the artefact
        assert sha_ok and frames_ok                                # published video untouched and valid
        assert not [n for n in archives if n.endswith((".zip", ".part"))]
        assert b_before == "QUEUED"                                # batch paused, job 2 not burnt
        assert b.status is JobStatus.SUCCESS and b_archive
        assert ws_files(ws) == [] and pending == []
        assert not archivers and not REGISTRY.snapshot()
        assert_no_media_processes(baseline)
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)

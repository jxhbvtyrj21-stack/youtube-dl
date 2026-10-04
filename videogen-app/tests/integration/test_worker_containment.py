"""Gap H: the Engine process dies while one of its worker processes is alive
(ImageWorker during NORMALIZING, Archiver during ARCHIVING). The Engine runs
in its own process exactly as under the GUI (EngineClient); it is killed with
SIGKILL / TerminateProcess. The evidence is the real process table, not
``os.getppid()``."""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil
import pytest
from PIL import Image

from videogen.core import events as ev
from videogen.core.models import JobStatus, RecoveryAction, Stage
from tests.pipeline_support import make_job_folder, small_settings
from tests.production.harness import EngineHarness

CONTAIN_S = 5.0          # a worker must be gone this soon after its Engine died


def _workers(engine_pid: int) -> list[psutil.Process]:
    out = []
    try:
        kids = psutil.Process(engine_pid).children(recursive=True)
    except psutil.Error:
        return out
    for c in kids:
        try:
            name = c.name().lower()
            cmd = " ".join(c.cmdline())
        except psutil.Error:
            continue
        if "ffmpeg" in name or "ffprobe" in name or "resource_tracker" in cmd:
            continue
        out.append(c)
    return out


def _alive(p: psutil.Process) -> bool:
    try:
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _kill_engine_and_watch(h: EngineHarness, workers: list[psutil.Process], watch: Path | None = None):
    sizes = []
    engine = psutil.Process(h.pid)
    engine.kill()
    t0 = time.monotonic()
    gone_after: dict[int, float] = {}
    for _ in range(int(30 / 0.1)):
        for w in workers:
            if w.pid not in gone_after and not _alive(w):
                gone_after[w.pid] = round(time.monotonic() - t0, 2)
        if watch is not None:
            sizes.append(sum(p.stat().st_size for p in watch.glob(".*.part")) if watch.exists() else 0)
        if len(gone_after) == len(workers):
            break
        time.sleep(0.1)
    for w in workers:                       # never leave a stray process behind the test
        if _alive(w):
            w.kill()
    return gone_after, sizes


def _part_bytes(archive_dir: Path) -> int:
    return sum(p.stat().st_size for p in archive_dir.glob(".*.part")) if archive_dir.exists() else 0


def _restart_and_ignore(appdata: Path) -> list[str]:
    h = EngineHarness(appdata, small_settings()).start()
    try:
        found = h.of(ev.InterruptedJobsFound)
        ids = list(found[0].job_ids) if found else []
        for jid in ids:
            h.client.send(ev.RecoveryDecision(jid, RecoveryAction.IGNORE))
        time.sleep(3)
    finally:
        h.stop()
    return ids


def _ws_files(ws: Path) -> list[str]:
    return [str(p) for p in ws.rglob("*") if p.is_file() and p.name != ".videogen-workspace"] if ws.exists() else []


@pytest.mark.timeout(600)
def test_engine_dies_while_image_worker_is_alive(tmp_path):
    inp, out, ws, appdata = (tmp_path / k for k in ("in", "out", "ws", "appdata"))
    d = inp / "багато зображень"
    d.mkdir(parents=True)
    for i in range(80):
        Image.effect_noise((2400, 1600), 60).convert("RGB").save(d / f"{i:03d}.jpg", quality=95)
    from tests.fixtures import factory as F
    F.tone(d / "a.mp3", 40.0)
    h = EngineHarness(appdata, small_settings()).start()
    try:
        h.client.send(ev.StartBatch("A", "16:9", str(inp), str(out), str(ws)))
        assert h.wait_for(lambda: any(e.stage is Stage.NORMALIZING for e in h.of(ev.JobStageChanged)), 120)
        assert h.wait_for(lambda: _workers(h.pid), 30), "ImageWorker never appeared"
        time.sleep(1.0)
        workers = _workers(h.pid)
        gone_after, _ = _kill_engine_and_watch(h, workers)
    finally:
        h.stop()
    stray = [w.pid for w in workers if w.pid not in gone_after]
    print(f"\nH/ImageWorker: workers={len(workers)} gone_after_s={gone_after}")
    assert not stray, f"ImageWorker outlived its Engine by > 30 s: {stray}"
    assert max(gone_after.values()) <= CONTAIN_S, gone_after
    ids = _restart_and_ignore(appdata)
    assert len(ids) == 1                                    # the job is INTERRUPTED, never SUCCESS
    assert _ws_files(ws) == []
    assert not list(out.glob("*.mp4")) and not list(out.glob(".*.part"))


@pytest.mark.timeout(900)
def test_engine_dies_while_archiver_is_alive(tmp_path):
    inp, out, ws, appdata = (tmp_path / k for k in ("in", "out", "ws", "appdata"))
    d = make_job_folder(inp, "великий архів", n_images=0, audio_s=24.0)
    for i in range(24):                     # incompressible BMP: archived with DEFLATE -> seconds of work
        Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3)).save(d / f"{i:03d}.bmp")
    h = EngineHarness(appdata, small_settings()).start()
    archive_dir = out / "_archive"
    try:
        h.client.send(ev.StartBatch("A", "16:9", str(inp), str(out), str(ws)))
        assert h.wait_for(lambda: any(e.stage is Stage.ARCHIVING for e in h.of(ev.JobStageChanged)), 600)
        assert h.wait_for(lambda: _workers(h.pid) and list(archive_dir.glob(".*.part")), 30), \
            "Archiver never started writing"
        time.sleep(0.5)
        workers = _workers(h.pid)
        gone_after, sizes = _kill_engine_and_watch(h, workers, archive_dir)
        # once the Archiver is gone nothing may write the archive any more
        part_at_exit = _part_bytes(archive_dir)
        time.sleep(1.0)
        part_1s_later = _part_bytes(archive_dir)
    finally:
        h.stop()
    grew_after_death = part_1s_later != part_at_exit
    stray = [w.pid for w in workers if w.pid not in gone_after]
    zips_after_death = sorted(p.name for p in archive_dir.glob("*.zip"))
    print(f"\nH/Archiver: workers={len(workers)} gone_after_s={gone_after} part_bytes_first={sizes[:1]} "
          f"part_bytes_last={sizes[-1:]} part_at_exit={part_at_exit} part_1s_later={part_1s_later} "
          f"zips={zips_after_death}")
    assert not stray, f"Archiver outlived its Engine by > 30 s: {stray}"
    assert max(gone_after.values()) <= CONTAIN_S, (gone_after, sizes[:3], sizes[-3:])
    assert not grew_after_death, "the archive kept being written after the Engine died"
    assert zips_after_death == [], "an archive was completed for a job whose Engine had died"
    ids = _restart_and_ignore(appdata)
    assert len(ids) == 1                                    # INTERRUPTED, never SUCCESS
    assert not list(archive_dir.glob(".*.part")), "stale partial archive left after restart"
    assert _ws_files(ws) == []
    # the verified video had already been published: it stays, linked to the cancelled job
    from videogen.core.state_manager import StateManager
    s = StateManager(appdata / "state.db")
    try:
        [job] = s.list_jobs()
    finally:
        s.close()
    assert job.status is JobStatus.CANCELLED
    assert job.output_file and Path(job.output_file).exists()

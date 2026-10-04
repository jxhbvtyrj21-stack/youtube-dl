"""The state DB must survive a hard kill at any moment (power loss / Task
Manager kill) and recovery must turn in-flight jobs into INTERRUPTED."""

from __future__ import annotations

import multiprocessing
import psutil
import os
import threading
import time

import pytest

from videogen.config.settings import RetryPolicy
from videogen.core.job_manager import JobManager
from videogen.core.models import JobStatus
from videogen.core.queue_manager import QueueManager
from videogen.core.state_manager import StateManager
from tests.helpers import FakeExecutor, add_jobs
from tests.pipeline_support import small_settings


N_JOBS = 3000


def _hammer(db_path: str, ready) -> None:
    s = StateManager(db_path)
    ids = add_jobs(s, N_JOBS, batch_id="crash")
    ready.set()
    for jid in ids:   # runs until killed
        s.transition(jid, JobStatus.RUNNING, increment_attempts=True)
        s.transition(jid, JobStatus.SUCCESS, output_file=jid)


@pytest.mark.parametrize("delay", [0.05, 0.15, 0.3])
def test_hard_kill_during_writes_keeps_db_consistent(tmp_path, delay):
    db = tmp_path / "state.db"
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    p = ctx.Process(target=_hammer, args=(str(db), ready))
    p.start()
    assert ready.wait(30)
    time.sleep(delay)
    p.kill()                      # SIGKILL / TerminateProcess: no cleanup at all
    p.join(10)

    s = StateManager(db)
    assert not s.open_report.recovered_from_corruption
    before = s.counters("crash")
    interrupted = s.mark_running_as_interrupted()
    after = s.counters("crash")
    assert after.running == 0 and after.queued == 0
    # everything that was not finished (running or never started) is resumable
    assert after.interrupted == len(interrupted) == N_JOBS - after.succeeded
    assert after.total == before.total == N_JOBS
    assert 0 < after.succeeded < N_JOBS, "kill must land in the middle of the writes"
    for jid in interrupted:
        assert s.get_job(jid).status is JobStatus.INTERRUPTED
    s.close()


def test_large_batch_no_thread_leak(tmp_path):
    s = StateManager(tmp_path / "state.db")
    ids = add_jobs(s, 1000)
    baseline = threading.active_count()
    qm = QueueManager(s, JobManager(s, FakeExecutor(), RetryPolicy(transient_backoff_s=(0.0,))),
                      lambda e: None)
    qm.start(ids, batch_id="b1")
    assert qm.wait(120)
    time.sleep(0.2)
    assert s.counters("b1").succeeded == 1000
    assert threading.active_count() <= baseline + 1
    s.close()


@pytest.mark.skipif(os.name == "nt", reason="uses POSIX fd listing")
def test_no_file_descriptor_leak_over_many_state_operations(tmp_path):
    s = StateManager(tmp_path / "state.db")
    fds_before = len(os.listdir("/proc/self/fd"))
    ids = add_jobs(s, 300)
    for jid in ids:
        s.transition(jid, JobStatus.RUNNING)
        s.transition(jid, JobStatus.FAILED)
        s.get_job(jid)
    fds_after = len(os.listdir("/proc/self/fd"))
    assert fds_after - fds_before <= 2
    s.close()


def _open_handles() -> int:
    p = psutil.Process()
    return p.num_handles() if hasattr(p, "num_handles") else p.num_fds()


def test_archive_process_does_not_leak_handles(tmp_path):
    """Regression (production test #2): each archive left a Process object
    and its pipes behind (healthy child killed + exit status stolen)."""
    from multiprocessing import process as mp_process

    from videogen.config.settings import Settings
    from videogen.core.cancellation import CancellationToken
    from videogen.core.pipeline import _run_archive_process
    from videogen.media.archiver import ArchiveEntry

    class Ctx:
        token = CancellationToken()
    f = tmp_path / "a.txt"
    f.write_text("x")
    _run_archive_process(tmp_path / "w.zip", [ArchiveEntry(f, "a.txt")], 10**9, Settings(), Ctx())
    before = _open_handles()
    for i in range(15):
        res = _run_archive_process(tmp_path / f"{i}.zip", [ArchiveEntry(f, "a.txt")], 10**9, Settings(), Ctx())
        assert res["ok"]
    assert _open_handles() - before <= 2
    assert not [c for c in mp_process._children if c.name == "Archiver"]


def test_engine_sequential_jobs_do_not_leak_handles(tmp_path):
    from videogen.core.engine import Engine
    from tests.pipeline_support import make_job_folder, start_cmd
    eng = Engine(tmp_path / "appdata", small_settings(), lambda e: None)
    eng.startup()
    counts = []
    try:
        for i in range(6):
            inp = tmp_path / f"in{i}"
            make_job_folder(inp, f"j{i}", n_images=2, audio_s=1.5)
            eng.start_batch(start_cmd(inp, tmp_path / "out", tmp_path / "ws"))
            assert eng.wait_idle(120)
            counts.append(_open_handles())
    finally:
        eng.shutdown()
    assert counts[-1] - counts[1] <= 2, counts

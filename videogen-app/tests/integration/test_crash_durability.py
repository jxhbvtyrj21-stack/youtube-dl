"""The state DB must survive a hard kill at any moment (power loss / Task
Manager kill) and recovery must turn in-flight jobs into INTERRUPTED."""

from __future__ import annotations

import multiprocessing
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
    assert after.running == 0
    assert after.interrupted == len(interrupted) <= 1
    assert after.total == before.total == N_JOBS
    assert after.succeeded + after.queued + after.interrupted == N_JOBS
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

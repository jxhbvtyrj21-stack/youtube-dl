from __future__ import annotations

import threading

import pytest

from videogen.config.settings import ResourceLimits, RetryPolicy
from videogen.core import events as ev
from videogen.core.errors import DiskSpaceError, FFmpegCrashError, InputError, OperationTimeoutError
from videogen.core.job_manager import JobManager
from videogen.core.models import BatchState, JobStatus
from videogen.core.queue_manager import QueueManager, ResourceStatus
from videogen.core.state_manager import StateManager
from tests.helpers import FakeExecutor, add_jobs, wait_until

NO_BACKOFF = RetryPolicy(transient_backoff_s=(0.0,))


class Recorder:
    def __init__(self):
        self.events: list[ev.Event] = []
        self.lock = threading.Lock()

    def __call__(self, e: ev.Event) -> None:
        with self.lock:
            self.events.append(e)

    def states(self):
        with self.lock:
            return [e.state for e in self.events if isinstance(e, ev.BatchStateChanged)]

    def finished(self):
        with self.lock:
            return [e for e in self.events if isinstance(e, ev.JobFinished)]


@pytest.fixture()
def state(tmp_path):
    s = StateManager(tmp_path / "state.db")
    yield s
    s.close()


def make_qm(state, ex, **kw):
    rec = Recorder()
    qm = QueueManager(state, JobManager(state, ex, NO_BACKOFF), rec, **kw)
    return qm, rec


def statuses(state, ids):
    return [state.get_job(j).status for j in ids]


def test_failures_do_not_stop_the_batch(state):
    """Requirement 19: 100 jobs, 3 failing in different ways -> 97 + 3."""
    ids = add_jobs(state, 100)
    ex = FakeExecutor(script={
        "job016": [InputError("corrupted image")],
        "job037": [OperationTimeoutError("stall"), OperationTimeoutError("stall")],
        "job063": [InputError("invalid audio")],
    })
    qm, rec = make_qm(state, ex)
    qm.start(ids, batch_id="b1")
    assert qm.wait(30)
    c = state.counters("b1")
    assert (c.succeeded, c.failed) == (97, 3)
    assert len(rec.finished()) == 100
    assert rec.states() == [BatchState.RUNNING, BatchState.COMPLETED, BatchState.IDLE]
    assert qm.batch_state is BatchState.IDLE
    counters = [e for e in rec.events if isinstance(e, ev.BatchCounters)][-1]
    assert (counters.succeeded, counters.failed, counters.total) == (97, 3, 100)


def test_stop_cancels_current_and_remaining(state):
    ids = add_jobs(state, 5)
    ex = FakeExecutor(script={"job001": ["block"]})
    qm, rec = make_qm(state, ex)
    qm.start(ids)
    assert wait_until(lambda: qm.active_jobs == [ids[1]], 5)
    qm.stop()
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.SUCCESS] + [JobStatus.CANCELLED] * 4
    assert rec.states()[-3:] == [BatchState.STOPPING, BatchState.STOPPED, BatchState.IDLE]
    assert ("job001", JobStatus.CANCELLED) in ex.finalized      # cleanup ran for the cancelled job


def test_cancel_current_job_continues_batch(state):
    ids = add_jobs(state, 3)
    ex = FakeExecutor(script={"job000": ["block"]})
    qm, _ = make_qm(state, ex)
    qm.start(ids)
    assert wait_until(lambda: qm.active_jobs == [ids[0]], 5)
    assert qm.cancel_current() == [ids[0]]
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.CANCELLED, JobStatus.SUCCESS, JobStatus.SUCCESS]


def test_pause_waits_for_atomic_operation_then_resume(state):
    ids = add_jobs(state, 3)
    ex = FakeExecutor(script={"job000": ["slow:0.3"]})
    qm, rec = make_qm(state, ex)
    qm.start(ids)
    assert wait_until(lambda: qm.active_jobs == [ids[0]], 5)
    qm.pause()
    assert qm.batch_state in (BatchState.PAUSING, BatchState.PAUSED)
    assert wait_until(lambda: qm.batch_state is BatchState.PAUSED, 5)
    done_while_paused = [j for j in ids if state.get_job(j).status is JobStatus.SUCCESS]
    threading.Event().wait(0.3)
    assert [j for j in ids if state.get_job(j).status is JobStatus.SUCCESS] == done_while_paused
    assert state.get_job(ids[2]).status is JobStatus.QUEUED
    qm.resume()
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.SUCCESS] * 3
    assert BatchState.PAUSED in rec.states()


def test_stop_while_paused(state):
    ids = add_jobs(state, 3)
    qm, rec = make_qm(state, FakeExecutor(script={"job000": ["slow:0.2"]}))
    qm.start(ids)
    qm.pause()
    assert wait_until(lambda: qm.batch_state is BatchState.PAUSED, 5)
    qm.stop()
    assert qm.wait(5)
    assert rec.states()[-2:] == [BatchState.STOPPED, BatchState.IDLE]


def test_resource_wait_recovers(state):
    ids = add_jobs(state, 2)
    probes = iter([ResourceStatus(False, "ram", "мало RAM")] * 3)
    qm, rec = make_qm(state, FakeExecutor(),
                      resource_probe=lambda: next(probes, ResourceStatus(True)), resource_poll_s=0.01)
    qm.start(ids)
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.SUCCESS] * 2
    assert BatchState.RESOURCE_WAIT in rec.states()
    assert any(isinstance(e, ev.ResourceWarning) for e in rec.events)


def test_resource_wait_is_bounded(state):
    ids = add_jobs(state, 3)
    qm, rec = make_qm(state, FakeExecutor(),
                      limits=ResourceLimits(resource_wait_max_s=0.2),
                      resource_probe=lambda: ResourceStatus(False, "ram", "мало RAM"),
                      resource_poll_s=0.01)
    qm.start(ids)
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.CANCELLED] * 3
    assert state.get_job(ids[0]).error.code == "RESOURCE_TIMEOUT"
    assert rec.states()[-2:] == [BatchState.STOPPED, BatchState.IDLE]


def test_disk_full_pauses_batch(state):
    ids = add_jobs(state, 3)
    ex = FakeExecutor(script={"job000": [DiskSpaceError("Недостатньо вільного місця на диску.",
                                                         code="DISK_SPACE")]})
    qm, rec = make_qm(state, ex)
    qm.start(ids)
    assert wait_until(lambda: qm.batch_state is BatchState.PAUSED, 5)
    assert state.get_job(ids[0]).status is JobStatus.FAILED
    assert state.get_job(ids[1]).status is JobStatus.QUEUED       # did not burn the rest
    qm.resume()
    assert qm.wait(5)


def test_parallel_slots(state):
    ids = add_jobs(state, 8)
    ex = FakeExecutor(default="slow:0.1")
    qm, _ = make_qm(state, ex, max_parallel=2)
    qm.start(ids)
    assert wait_until(lambda: len(qm.active_jobs) == 2, 5)
    assert qm.wait(10)
    assert statuses(state, ids) == [JobStatus.SUCCESS] * 8


def test_crash_retry_inside_batch(state):
    ids = add_jobs(state, 2)
    ex = FakeExecutor(script={"job000": [FFmpegCrashError("x"), "ok"]})
    qm, _ = make_qm(state, ex)
    qm.start(ids)
    assert qm.wait(5)
    assert state.get_job(ids[0]).attempts == 2
    assert statuses(state, ids) == [JobStatus.SUCCESS] * 2


def test_cannot_start_twice(state):
    ids = add_jobs(state, 1)
    qm, _ = make_qm(state, FakeExecutor(default="slow:0.2"))
    qm.start(ids)
    with pytest.raises(RuntimeError):
        qm.start(ids)
    assert qm.wait(5)


def test_executor_bug_does_not_kill_batch(state):
    ids = add_jobs(state, 3)
    ex = FakeExecutor(script={"job001": [KeyError("bug")]})
    qm, _ = make_qm(state, ex)
    qm.start(ids)
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.SUCCESS]
    assert state.get_job(ids[1]).error.error_class == "INTERNAL"


def test_pause_during_resource_wait_does_not_start_job(state):
    ids = add_jobs(state, 2)
    gate = threading.Event()
    qm, rec = make_qm(state, FakeExecutor(),
                      resource_probe=lambda: ResourceStatus(gate.is_set(), "ram", "мало RAM"),
                      resource_poll_s=0.01)
    qm.start(ids)
    assert wait_until(lambda: qm.batch_state is BatchState.RESOURCE_WAIT, 5)
    qm.pause()
    gate.set()
    assert wait_until(lambda: qm.batch_state is BatchState.PAUSED, 5)
    threading.Event().wait(0.2)
    assert statuses(state, ids) == [JobStatus.QUEUED, JobStatus.QUEUED]
    qm.stop()
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.CANCELLED, JobStatus.CANCELLED]


def test_interrupt_keeps_jobs_resumable(state):
    """Regression (production tests 6/9): when the application goes away
    (GUI lost) jobs must become INTERRUPTED, not CANCELLED."""
    ids = add_jobs(state, 3)
    ex = FakeExecutor(script={"job000": ["block"]})
    qm, rec = make_qm(state, ex)
    qm.start(ids)
    assert wait_until(lambda: qm.active_jobs == [ids[0]], 5)
    qm.stop("GUI завершився аварійно", interrupt=True)
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.INTERRUPTED] * 3
    assert ex.finalized == [("job000", JobStatus.INTERRUPTED)]
    assert "продовжити" in state.get_job(ids[0]).error.message


def test_user_stop_still_cancels(state):
    ids = add_jobs(state, 2)
    qm, _ = make_qm(state, FakeExecutor(script={"job000": ["block"]}))
    qm.start(ids)
    assert wait_until(lambda: qm.active_jobs == [ids[0]], 5)
    qm.stop()
    assert qm.wait(5)
    assert statuses(state, ids) == [JobStatus.CANCELLED] * 2

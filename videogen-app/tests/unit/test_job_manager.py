from __future__ import annotations

import threading
import time

import pytest

from videogen.config.settings import RetryPolicy
from videogen.core.cancellation import CancellationToken, PauseGate
from videogen.core.errors import (
    ArchiveError, FFmpegCrashError, InputError, OperationTimeoutError, TransientError, VerificationError,
)
from videogen.core.job_manager import HARD_ATTEMPT_CAP, JobManager
from videogen.core.models import JobStatus
from videogen.core.state_manager import StateManager
from tests.helpers import FakeExecutor, add_jobs, wait_until

NO_BACKOFF = RetryPolicy(transient_backoff_s=(0.0,))


@pytest.fixture()
def state(tmp_path):
    s = StateManager(tmp_path / "state.db")
    yield s
    s.close()


def run(state, executor, policy=NO_BACKOFF, token=None):
    [jid] = add_jobs(state, 1)
    jm = JobManager(state, executor, policy)
    return jm.run(jid, token or CancellationToken(), PauseGate())


def test_success(state):
    ex = FakeExecutor()
    st = run(state, ex)
    assert st.status is JobStatus.SUCCESS and st.attempts == 1
    assert ex.finalized == [("job000", JobStatus.SUCCESS)]


def test_partial_is_reported_as_partial_not_success(state):
    ex = FakeExecutor(script={"job000": ["partial"]})
    st = run(state, ex)
    assert st.status is JobStatus.PARTIAL and st.skipped_images == 2
    assert "[PARTIAL]" in st.output_file


@pytest.mark.parametrize("exc,expected_attempts", [
    (InputError("bad audio", code="INVALID_AUDIO"), 1),          # corrupted input: NO retry
    (TransientError("locked"), 3),                                # transient: 2 retries
    (FFmpegCrashError("crash"), 2),                               # ffmpeg crash: 1 retry
    (OperationTimeoutError("stall"), 2),                          # timeout: 1 retry
    (VerificationError("bad output"), 2),                         # verification: 1 retry
    (ArchiveError("zip"), 1),
    (ZeroDivisionError("bug"), 1),                                # internal: no retry
])
def test_retry_budget_per_error_class(state, exc, expected_attempts):
    ex = FakeExecutor(default=exc)
    st = run(state, ex)
    assert st.status is JobStatus.FAILED
    assert st.attempts == expected_attempts
    assert len(ex.calls) == expected_attempts
    assert len(ex.retries) == expected_attempts - 1
    assert ex.finalized == [("job000", JobStatus.FAILED)]       # cleanup ran exactly once


def test_timeout_retry_gets_clean_workspace_and_then_succeeds(state):
    ex = FakeExecutor(script={"job000": [OperationTimeoutError("stall"), "ok"]})
    st = run(state, ex)
    assert st.status is JobStatus.SUCCESS and st.attempts == 2 and st.error is None
    assert ex.retries == [("job000", "TIMEOUT")]


def test_mixed_error_classes_each_have_own_budget_and_total_is_bounded(state):
    seq = [TransientError("a"), TransientError("b"), FFmpegCrashError("c"), OperationTimeoutError("d"),
           VerificationError("e"), TransientError("f")]
    ex = FakeExecutor(script={"job000": seq})
    st = run(state, ex)
    assert st.status is JobStatus.FAILED
    assert st.attempts == 6
    assert st.error.error_class == "TRANSIENT"


def test_finalize_runs_even_if_executor_raises_base_exception(state):
    class Boom(BaseException):
        pass
    ex = FakeExecutor(default=Boom("hard"))
    [jid] = add_jobs(state, 1)
    jm = JobManager(state, ex, NO_BACKOFF)
    with pytest.raises(Boom):
        jm.run(jid, CancellationToken(), PauseGate())
    assert state.get_job(jid).status is JobStatus.FAILED      # never left RUNNING
    assert ex.finalized and ex.finalized[0][1] is JobStatus.FAILED


def test_cancel_during_execute(state):
    ex = FakeExecutor(default="block")
    [jid] = add_jobs(state, 1)
    token = CancellationToken()
    jm = JobManager(state, ex, NO_BACKOFF)
    result = {}
    t = threading.Thread(target=lambda: result.setdefault("st", jm.run(jid, token, PauseGate())))
    t.start()
    assert ex.started.wait(5)
    token.cancel("stop")
    t.join(5)
    assert not t.is_alive()
    assert result["st"].status is JobStatus.CANCELLED
    assert ex.finalized == [("job000", JobStatus.CANCELLED)]


def test_cancel_during_backoff_is_immediate(state):
    ex = FakeExecutor(default=TransientError("locked"))
    policy = RetryPolicy(transient_backoff_s=(30.0,))
    [jid] = add_jobs(state, 1)
    token = CancellationToken()
    jm = JobManager(state, ex, policy)
    t0 = time.monotonic()
    threading.Timer(0.3, token.cancel).start()
    st = jm.run(jid, token, PauseGate())
    assert time.monotonic() - t0 < 5
    assert st.status is JobStatus.CANCELLED


def test_error_raised_after_cancel_is_reported_as_cancelled(state):
    token = CancellationToken()

    def act(ctx):
        token.cancel()
        raise FFmpegCrashError("killed because of cancel")

    st = run(state, FakeExecutor(default=act), token=token)
    assert st.status is JobStatus.CANCELLED


def test_hard_attempt_cap_across_restarts(state):
    [jid] = add_jobs(state, 1)
    # simulate many prior interrupted attempts
    for _ in range(HARD_ATTEMPT_CAP):
        state.transition(jid, JobStatus.RUNNING, increment_attempts=True)
        state.transition(jid, JobStatus.INTERRUPTED)
        state.transition(jid, JobStatus.QUEUED)
    ex = FakeExecutor()
    st = JobManager(state, ex, NO_BACKOFF).run(jid, CancellationToken(), PauseGate())
    assert st.status is JobStatus.FAILED and st.error.code == "TOO_MANY_ATTEMPTS"
    assert ex.calls == []


def test_running_a_finished_job_is_rejected(state):
    [jid] = add_jobs(state, 1)
    jm = JobManager(state, FakeExecutor(), NO_BACKOFF)
    jm.run(jid, CancellationToken(), PauseGate())
    with pytest.raises(ValueError):
        jm.run(jid, CancellationToken(), PauseGate())


def test_child_token_follows_parent_and_detaches():
    parent = CancellationToken()
    child = CancellationToken(parent)
    child.detach()
    parent.cancel()
    assert not child.cancelled
    child2 = CancellationToken(parent)       # parent already cancelled -> immediate
    assert child2.cancelled


def test_cancel_callbacks_are_invoked_once_and_errors_isolated():
    token = CancellationToken()
    hits = []
    token.register(lambda: (_ for _ in ()).throw(RuntimeError("bad cb")))
    token.register(lambda: hits.append(1))
    token.cancel()
    token.cancel()
    assert hits == [1]


def test_pause_gate_blocks_until_release():
    gate = PauseGate()
    token = CancellationToken()
    gate.request_pause()
    passed = threading.Event()
    t = threading.Thread(target=lambda: (gate.checkpoint(token), passed.set()))
    t.start()
    assert wait_until(lambda: gate.someone_paused, 2)
    assert not passed.is_set()
    gate.release()
    assert passed.wait(2)

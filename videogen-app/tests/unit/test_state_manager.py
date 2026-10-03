from __future__ import annotations

import threading

import pytest

from videogen.core.models import ErrorInfo, IllegalTransition, JobStatus, Stage
from videogen.core.state_manager import StateManager
from tests.helpers import add_jobs, make_config


@pytest.fixture()
def state(tmp_path):
    s = StateManager(tmp_path / "state.db")
    yield s
    s.close()


def test_add_and_get(state):
    cfg = make_config("Відео 1")
    st = state.add_job(cfg, 0, "/ws/j0000")
    assert st.status is JobStatus.QUEUED and st.stage is Stage.NONE and st.attempts == 0
    assert st.config == cfg
    assert state.workspace_dir(cfg.job_id) == "/ws/j0000"


def test_full_happy_path(state):
    [jid] = add_jobs(state, 1)
    st = state.transition(jid, JobStatus.RUNNING, increment_attempts=True)
    assert st.attempts == 1 and st.started_at
    state.set_stage(jid, Stage.RENDERING)
    assert state.get_job(jid).stage is Stage.RENDERING
    st = state.transition(jid, JobStatus.SUCCESS, output_file="/out/a.mp4")
    assert st.status is JobStatus.SUCCESS and st.ended_at and st.output_file == "/out/a.mp4"
    assert st.stage is Stage.NONE


def test_illegal_transition_rejected_and_state_unchanged(state):
    [jid] = add_jobs(state, 1)
    with pytest.raises(IllegalTransition):
        state.transition(jid, JobStatus.SUCCESS)
    assert state.get_job(jid).status is JobStatus.QUEUED
    state.transition(jid, JobStatus.RUNNING)
    state.transition(jid, JobStatus.SUCCESS, output_file="x")
    with pytest.raises(IllegalTransition):
        state.transition(jid, JobStatus.QUEUED)   # SUCCESS is final


def test_set_stage_requires_running(state):
    [jid] = add_jobs(state, 1)
    with pytest.raises(ValueError):
        state.set_stage(jid, Stage.RENDERING)


def test_error_persisted(state):
    [jid] = add_jobs(state, 1)
    state.transition(jid, JobStatus.RUNNING)
    err = ErrorInfo("INPUT", "INVALID_AUDIO", "Аудіофайл пошкоджений.", "trace")
    st = state.transition(jid, JobStatus.FAILED, error=err)
    assert st.error == err


def test_running_jobs_become_interrupted_after_restart(tmp_path):
    s = StateManager(tmp_path / "state.db")
    ids = add_jobs(s, 4)
    s.transition(ids[0], JobStatus.RUNNING)
    s.set_stage(ids[0], Stage.RENDERING)
    s.transition(ids[1], JobStatus.RUNNING)
    s.transition(ids[1], JobStatus.SUCCESS, output_file="/out/1.mp4")
    s.transition(ids[2], JobStatus.RUNNING)
    s.transition(ids[2], JobStatus.RETRY_PENDING)
    # simulate crash: connection dropped without any further writes
    s._conn.close()

    s2 = StateManager(tmp_path / "state.db")
    interrupted = s2.mark_running_as_interrupted()
    assert sorted(interrupted) == sorted([ids[0], ids[2]])
    assert s2.get_job(ids[0]).status is JobStatus.INTERRUPTED
    assert s2.get_job(ids[1]).status is JobStatus.SUCCESS      # finished output untouched
    assert s2.get_job(ids[1]).output_file == "/out/1.mp4"
    assert s2.get_job(ids[3]).status is JobStatus.QUEUED
    # recovery choices
    s2.transition(ids[0], JobStatus.QUEUED)                     # Resume/Retry
    s2.transition(ids[2], JobStatus.CANCELLED)                  # Ignore
    assert s2.mark_running_as_interrupted() == []
    s2.close()


def test_corrupt_database_is_quarantined_and_recreated(tmp_path):
    db = tmp_path / "state.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    s = StateManager(db)
    assert s.open_report.recovered_from_corruption
    assert (tmp_path / s.open_report.corrupt_copy.split("/")[-1]).exists()
    add_jobs(s, 1)
    assert s.counters().total == 1
    s.close()


def test_counters_and_outputs(state):
    ids = add_jobs(state, 5)
    for jid, final in zip(ids, [JobStatus.SUCCESS, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED]):
        state.transition(jid, JobStatus.RUNNING)
        state.transition(jid, final, output_file=f"/out/{jid}.mp4" if final in
                         (JobStatus.SUCCESS, JobStatus.PARTIAL) else None,
                         skipped_images=3 if final is JobStatus.PARTIAL else None)
    c = state.counters("b1")
    assert (c.total, c.succeeded, c.partial, c.failed, c.cancelled, c.queued) == (5, 1, 1, 1, 1, 1)
    assert c.skipped_images == 3
    assert state.successful_outputs() == {f"/out/{ids[0]}.mp4", f"/out/{ids[1]}.mp4"}
    assert state.counters("other").total == 0


def test_manual_retry_resets_resume_point(state):
    [jid] = add_jobs(state, 1)
    state.transition(jid, JobStatus.RUNNING)
    state.set_resume_point(jid, 17)
    state.transition(jid, JobStatus.FAILED, error=ErrorInfo("TIMEOUT", "TIMEOUT", "x"))
    st = state.reset_for_retry(jid)
    assert st.status is JobStatus.QUEUED and st.resume_from_segment == 0 and st.error is None


def test_pending_cleanup(state):
    state.add_pending_cleanup("/ws/a")
    state.add_pending_cleanup("/ws/a")
    state.add_pending_cleanup("/ws/b")
    assert state.pending_cleanups() == [("/ws/a", 1), ("/ws/b", 0)]
    state.remove_pending_cleanup("/ws/a")
    assert state.pending_cleanups() == [("/ws/b", 0)]


def test_concurrent_transitions_are_serialised(state):
    ids = add_jobs(state, 40)
    errors: list[BaseException] = []

    def worker(chunk):
        try:
            for jid in chunk:
                state.transition(jid, JobStatus.RUNNING, increment_attempts=True)
                state.set_stage(jid, Stage.RENDERING)
                state.transition(jid, JobStatus.SUCCESS, output_file=jid)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(ids[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert not errors
    assert state.counters().succeeded == 40

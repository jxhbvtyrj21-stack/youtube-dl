"""State database failure modes (ТЗ production test #10)."""

from __future__ import annotations

import multiprocessing
import sqlite3
import time

import pytest

from videogen.core.models import JobStatus
from videogen.core.state_manager import StateLockedError, StateManager
from tests.helpers import add_jobs


def _hold_exclusive(db: str, ready, release) -> None:
    con = sqlite3.connect(db, isolation_level=None)
    con.execute("BEGIN EXCLUSIVE")
    ready.set()
    release.wait(60)
    con.execute("ROLLBACK")
    con.close()


def test_locked_database_is_not_mistaken_for_corruption(tmp_path):
    db = tmp_path / "state.db"
    s = StateManager(db)
    add_jobs(s, 3)
    s.close()
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_exclusive, args=(str(db), ready, release))
    p.start()
    try:
        assert ready.wait(30)
        t0 = time.monotonic()
        with pytest.raises(StateLockedError) as ei:
            StateManager(db, lock_wait_s=2.0)
        assert time.monotonic() - t0 < 15                     # bounded, no hang
        assert "заблокована" in ei.value.user_message
    finally:
        release.set()
        p.join(30)
    # the valid database was NOT quarantined and still holds the jobs
    assert not list(tmp_path.glob("state.db.corrupt-*"))
    s2 = StateManager(db)
    assert s2.counters().total == 3
    s2.close()


def test_invalid_json_row_does_not_break_startup(tmp_path):
    db = tmp_path / "state.db"
    s = StateManager(db)
    ids = add_jobs(s, 3)
    s.transition(ids[1], JobStatus.RUNNING)
    s._conn.execute("UPDATE jobs SET config_json='{broken' WHERE job_id=?", (ids[1],))
    s._conn.execute("UPDATE jobs SET error_json='not json', status='FAILED' WHERE job_id=?", (ids[2],))
    s.close()
    s = StateManager(db)
    s.mark_running_as_interrupted()
    jobs = s.list_jobs()
    assert {j.job_id for j in jobs} == {ids[0], ids[2]}          # unreadable row skipped, not fatal
    assert s.get_job(ids[2]).error is not None                    # bad error_json tolerated
    assert s.corrupt_rows == [ids[1]]
    assert s.counters().total == 3
    s.close()


def test_missing_state_file_is_recreated(tmp_path):
    s = StateManager(tmp_path / "sub" / "state.db")
    assert s.counters().total == 0 and not s.open_report.recovered_from_corruption
    s.close()


@pytest.mark.parametrize("payload", [b"", b"SQLite format 3\x00" + b"\xff" * 5000, b"\x00" * 4096])
def test_corrupted_database_variants_recover(tmp_path, payload):
    db = tmp_path / "state.db"
    db.write_bytes(payload)
    t0 = time.monotonic()
    s = StateManager(db)
    assert time.monotonic() - t0 < 15
    add_jobs(s, 1)
    assert s.counters().total == 1
    s.close()

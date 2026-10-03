"""Persistent job state in SQLite (ARCHITECTURE.md §5, §14).

* WAL + ``synchronous=FULL``: a committed transition survives power loss.
* Every status change goes through :meth:`StateManager.transition`, which
  validates it against ``ALLOWED_TRANSITIONS`` inside the same transaction.
* Write-ahead discipline: callers record ``RUNNING/<stage>`` *before* they
  start the corresponding work.
* Thread-safe: one connection guarded by a lock (the Engine is the only
  writer process).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from videogen.core.models import (
    ErrorInfo, JobConfig, JobState, JobStatus, Stage, check_transition,
)
from videogen.storage.manifest import utc_now

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    input_dir TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    workspace_dir TEXT NOT NULL,
    engine_pid INTEGER
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    interrupted_at TEXT,
    error_json TEXT,
    output_file TEXT,
    skipped_images INTEGER NOT NULL DEFAULT 0,
    resume_from_segment INTEGER NOT NULL DEFAULT 0,
    workspace_dir TEXT NOT NULL,
    config_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS jobs_batch ON jobs(batch_id, seq);
CREATE TABLE IF NOT EXISTS pending_cleanup (
    path TEXT PRIMARY KEY,
    added_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
"""


@dataclass(frozen=True)
class OpenReport:
    recovered_from_corruption: bool = False
    corrupt_copy: str = ""


@dataclass(frozen=True)
class Counters:
    total: int = 0
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    cancelled: int = 0
    interrupted: int = 0
    skipped_images: int = 0


class StateManager:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self.open_report = OpenReport()
        self._conn = self._open()

    # ------------------------------------------------------------ opening

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _open(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            if not ok:
                raise sqlite3.DatabaseError("integrity_check failed")
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
                         (str(SCHEMA_VERSION),))
            return conn
        except sqlite3.DatabaseError as exc:
            if conn is not None:
                conn.close()
            corrupt = self.db_path.with_name(f"{self.db_path.name}.corrupt-{int(time.time())}")
            log.critical("state database %s is corrupt (%r); moved to %s", self.db_path, exc, corrupt)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self.db_path) + suffix)
                if src.exists():
                    src.replace(Path(str(corrupt) + suffix))
            self.open_report = OpenReport(True, str(corrupt))
            conn = self._connect()
            conn.executescript(_SCHEMA)
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema', ?)",
                         (str(SCHEMA_VERSION),))
            return conn

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass  # invariant-ok: closing an already broken connection

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------ batches

    def add_batch(self, batch_id: str, input_dir: str, output_dir: str, workspace_dir: str,
                  engine_pid: int | None = None) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO batches VALUES (?,?,?,?,?,?)",
                      (batch_id, utc_now(), input_dir, output_dir, workspace_dir, engine_pid))

    # ------------------------------------------------------------ jobs

    def add_job(self, config: JobConfig, seq: int, workspace_dir: str) -> JobState:
        now = utc_now()
        with self._tx() as c:
            c.execute(
                "INSERT INTO jobs(job_id, batch_id, seq, name, status, stage, attempts, created_at,"
                " updated_at, workspace_dir, config_json) VALUES (?,?,?,?,?,?,0,?,?,?,?)",
                (config.job_id, config.batch_id, seq, config.name, JobStatus.QUEUED.value,
                 Stage.NONE.value, now, now, workspace_dir, json.dumps(config.to_dict(), ensure_ascii=False)))
        return self.get_job(config.job_id)

    def get_job(self, job_id: str) -> JobState:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _row_to_state(row)

    def workspace_dir(self, job_id: str) -> str:
        with self._lock:
            row = self._conn.execute("SELECT workspace_dir FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return row[0]

    def list_jobs(self, *, batch_id: str | None = None,
                  statuses: Iterable[JobStatus] | None = None) -> list[JobState]:
        sql = "SELECT * FROM jobs"
        where: list[str] = []
        args: list[object] = []
        if batch_id is not None:
            where.append("batch_id=?")
            args.append(batch_id)
        if statuses is not None:
            st = [s.value for s in statuses]
            if not st:
                return []
            where.append(f"status IN ({','.join('?' * len(st))})")
            args.extend(st)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at, seq"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [_row_to_state(r) for r in rows]

    def transition(self, job_id: str, new: JobStatus, *, stage: Stage | None = None,
                   error: ErrorInfo | None = None, output_file: str | None = None,
                   skipped_images: int | None = None, increment_attempts: bool = False,
                   clear_error: bool = False) -> JobState:
        now = utc_now()
        with self._tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            check_transition(JobStatus(row[0]), new)
            sets = ["status=?", "updated_at=?"]
            args: list[object] = [new.value, now]
            if stage is not None:
                sets.append("stage=?")
                args.append(stage.value)
            elif new is not JobStatus.RUNNING:
                sets.append("stage=?")
                args.append(Stage.NONE.value)
            if new is JobStatus.RUNNING:
                sets.append("started_at=COALESCE(started_at, ?)")
                args.append(now)
                sets.append("ended_at=NULL")
            if new in (JobStatus.SUCCESS, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED):
                sets.append("ended_at=?")
                args.append(now)
            if new is JobStatus.INTERRUPTED:
                sets.append("interrupted_at=?")
                args.append(now)
            if error is not None:
                sets.append("error_json=?")
                args.append(json.dumps(error.to_dict(), ensure_ascii=False))
            elif clear_error:
                sets.append("error_json=NULL")
            if output_file is not None:
                sets.append("output_file=?")
                args.append(output_file)
            if skipped_images is not None:
                sets.append("skipped_images=?")
                args.append(skipped_images)
            if increment_attempts:
                sets.append("attempts=attempts+1")
            args.append(job_id)
            c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", args)
        return self.get_job(job_id)

    def set_stage(self, job_id: str, stage: Stage) -> None:
        with self._tx() as c:
            row = c.execute("SELECT status FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row[0] != JobStatus.RUNNING.value:
                raise ValueError(f"set_stage on non-running job {job_id} ({row[0]})")
            c.execute("UPDATE jobs SET stage=?, updated_at=? WHERE job_id=?",
                      (stage.value, utc_now(), job_id))

    def set_resume_point(self, job_id: str, segment_index: int) -> None:
        with self._tx() as c:
            c.execute("UPDATE jobs SET resume_from_segment=?, updated_at=? WHERE job_id=?",
                      (segment_index, utc_now(), job_id))

    def reset_for_retry(self, job_id: str) -> JobState:
        """Manual 'Retry': start the job from scratch (resume point cleared)."""
        st = self.transition(job_id, JobStatus.QUEUED, clear_error=True)
        self.set_resume_point(job_id, 0)
        return self.get_job(st.job_id)

    def mark_running_as_interrupted(self) -> list[str]:
        """Crash recovery step 1: anything that was mid-flight is INTERRUPTED."""
        now = utc_now()
        with self._tx() as c:
            rows = c.execute("SELECT job_id FROM jobs WHERE status IN (?, ?)",
                             (JobStatus.RUNNING.value, JobStatus.RETRY_PENDING.value)).fetchall()
            ids = [r[0] for r in rows]
            if ids:
                c.execute(
                    f"UPDATE jobs SET status=?, interrupted_at=?, updated_at=? "
                    f"WHERE job_id IN ({','.join('?' * len(ids))})",
                    [JobStatus.INTERRUPTED.value, now, now, *ids])
        if ids:
            log.warning("marked %d job(s) INTERRUPTED after unclean shutdown: %s", len(ids), ids)
        return ids

    def counters(self, batch_id: str | None = None) -> Counters:
        sql = "SELECT status, COUNT(*), COALESCE(SUM(skipped_images),0) FROM jobs"
        args: tuple[object, ...] = ()
        if batch_id is not None:
            sql += " WHERE batch_id=?"
            args = (batch_id,)
        sql += " GROUP BY status"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        by = {r[0]: r[1] for r in rows}
        skipped = sum(r[2] for r in rows)
        return Counters(
            total=sum(by.values()),
            queued=by.get("QUEUED", 0) + by.get("RETRY_PENDING", 0),
            running=by.get("RUNNING", 0),
            succeeded=by.get("SUCCESS", 0),
            partial=by.get("PARTIAL", 0),
            failed=by.get("FAILED", 0),
            cancelled=by.get("CANCELLED", 0),
            interrupted=by.get("INTERRUPTED", 0),
            skipped_images=skipped,
        )

    def batch_dirs(self) -> list[tuple[str, str, str]]:
        """(batch_id, output_dir, workspace_dir) for every batch."""
        with self._lock:
            return [(r[0], r[1], r[2]) for r in self._conn.execute(
                "SELECT batch_id, output_dir, workspace_dir FROM batches ORDER BY created_at")]

    def successful_outputs(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT output_file FROM jobs WHERE status IN (?, ?) AND output_file IS NOT NULL",
                (JobStatus.SUCCESS.value, JobStatus.PARTIAL.value)).fetchall()
        return {r[0] for r in rows}

    # ------------------------------------------------------------ cleanup bookkeeping

    def add_pending_cleanup(self, path: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO pending_cleanup(path, added_at, attempts) VALUES (?,?,0) "
                      "ON CONFLICT(path) DO UPDATE SET attempts=attempts+1", (path, utc_now()))

    def pending_cleanups(self) -> list[tuple[str, int]]:
        with self._lock:
            return [(r[0], r[1]) for r in
                    self._conn.execute("SELECT path, attempts FROM pending_cleanup ORDER BY added_at")]

    def remove_pending_cleanup(self, path: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM pending_cleanup WHERE path=?", (path,))


def _row_to_state(row: sqlite3.Row) -> JobState:
    err = json.loads(row["error_json"]) if row["error_json"] else None
    return JobState(
        job_id=row["job_id"],
        batch_id=row["batch_id"],
        name=row["name"],
        status=JobStatus(row["status"]),
        stage=Stage(row["stage"]),
        attempts=row["attempts"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        error=ErrorInfo(**err) if err else None,
        output_file=row["output_file"],
        skipped_images=row["skipped_images"],
        resume_from_segment=row["resume_from_segment"],
        config=JobConfig.from_dict(json.loads(row["config_json"])),
    )

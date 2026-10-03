"""Batch queue: ordering, concurrency slots, PAUSE/RESUME/STOP/CANCEL,
resource gating (ARCHITECTURE.md §5.2, §9, §16).

Runs inside the Engine process in its own threads. Control methods
(:meth:`pause`, :meth:`stop`, …) are non-blocking and safe to call from the
Engine command loop at any time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from videogen.config.settings import ResourceLimits
from videogen.core import events as ev
from videogen.core.cancellation import CancellationToken, PauseGate
from videogen.core.errors import ErrorClass, JobCancelledError
from videogen.core.job_manager import JobManager
from videogen.core.models import BatchState, ErrorInfo, JobStatus, check_batch_transition
from videogen.core.state_manager import StateManager

log = logging.getLogger(__name__)

MAX_JOBS_PER_BATCH = 1_000_000


@dataclass(frozen=True)
class ResourceStatus:
    ok: bool
    kind: str = ""
    message: str = ""


ResourceProbe = Callable[[], ResourceStatus]


def _always_ok() -> ResourceStatus:
    return ResourceStatus(True)


class QueueManager:
    def __init__(self, state: StateManager, jobs: JobManager, emit: Callable[[ev.Event], None],
                 *, max_parallel: int = 1, limits: ResourceLimits | None = None,
                 resource_probe: ResourceProbe = _always_ok,
                 resource_poll_s: float | None = None) -> None:
        self.state = state
        self.jobs = jobs
        self.emit = emit
        self.max_parallel = max(1, max_parallel)
        self.limits = limits or ResourceLimits()
        self.resource_probe = resource_probe
        self.resource_poll_s = resource_poll_s if resource_poll_s is not None else self.limits.monitor_interval_s

        self._lock = threading.RLock()
        self._batch_state = BatchState.IDLE
        self._batch_token = CancellationToken()
        self._pause = PauseGate()
        self._pause.on_paused = self._on_paused
        self._pending: deque[str] = deque()
        self._active: dict[str, CancellationToken] = {}
        self._threads: list[threading.Thread] = []
        self._done = threading.Event()
        self._done.set()
        self._stop_reason = ""
        self._batch_id: str | None = None

    # ------------------------------------------------------------ state

    @property
    def batch_state(self) -> BatchState:
        with self._lock:
            return self._batch_state

    def _set_state(self, new: BatchState, reason: str = "") -> None:
        with self._lock:
            if new is self._batch_state:
                return
            check_batch_transition(self._batch_state, new)
            self._batch_state = new
        log.info("batch state -> %s %s", new.value, reason, extra={"event": "batch_state"})
        self.emit(ev.BatchStateChanged(time.time(), new, reason))

    def _on_paused(self) -> None:
        with self._lock:
            if self._batch_state is BatchState.PAUSING:
                self._set_state(BatchState.PAUSED)

    # ------------------------------------------------------------ control

    def start(self, job_ids: Sequence[str], batch_id: str | None = None) -> None:
        with self._lock:
            if self._batch_state not in (BatchState.IDLE,):
                raise RuntimeError(f"cannot start batch in state {self._batch_state.value}")
            self._batch_token = CancellationToken()
            self._pause = PauseGate()
            self._pause.on_paused = self._on_paused
            self._pending = deque(job_ids)
            self._stop_reason = ""
            self._batch_id = batch_id
            self._done.clear()
            self._set_state(BatchState.RUNNING)
            n = min(self.max_parallel, max(1, len(job_ids)))
            self._threads = [threading.Thread(target=self._slot, name=f"job-slot-{i}", daemon=True)
                             for i in range(n)]
            supervisor = threading.Thread(target=self._supervise, name="batch-supervisor", daemon=True)
        for t in self._threads:
            t.start()
        supervisor.start()

    def pause(self) -> None:
        with self._lock:
            if self._batch_state in (BatchState.RUNNING, BatchState.RESOURCE_WAIT):
                self._set_state(BatchState.PAUSING)
                self._pause.request_pause()
                if not self._active:
                    # nothing in flight: paused immediately
                    self._set_state(BatchState.PAUSED)

    def resume(self) -> None:
        with self._lock:
            if self._batch_state in (BatchState.PAUSED, BatchState.PAUSING):
                self._pause.release()
                if self._batch_state is BatchState.PAUSING:
                    self._batch_state = BatchState.PAUSED  # collapse PAUSING->PAUSED->RUNNING
                self._set_state(BatchState.RUNNING)

    def stop(self, reason: str = "Зупинено користувачем.") -> None:
        with self._lock:
            if self._batch_state in (BatchState.IDLE, BatchState.STOPPING, BatchState.STOPPED,
                                     BatchState.COMPLETED):
                return
            self._stop_reason = reason
            self._set_state(BatchState.STOPPING, reason)
            self._pause.release()
        # cancels all job tokens (children of the batch token) -> kills subprocesses
        self._batch_token.cancel(reason)

    def cancel_current(self) -> list[str]:
        with self._lock:
            active = dict(self._active)
        for job_id, token in active.items():
            log.info("cancel current job requested", extra={"job_id": job_id, "event": "cancel"})
            token.cancel("Поточне завдання скасовано користувачем.")
        return list(active)

    def wait(self, timeout: float) -> bool:
        return self._done.wait(timeout)

    @property
    def active_jobs(self) -> list[str]:
        with self._lock:
            return list(self._active)

    # ------------------------------------------------------------ workers

    def _next_job(self) -> str | None:
        with self._lock:
            if self._batch_token.cancelled or not self._pending:
                return None
            return self._pending.popleft()

    def _wait_resources(self) -> bool:
        """Return True when resources are fine; False if the wait timed out
        or the batch was stopped."""
        status = self.resource_probe()
        if status.ok:
            return True
        with self._lock:
            prev = self._batch_state
            if prev is BatchState.RUNNING:
                self._set_state(BatchState.RESOURCE_WAIT, status.message)
        self.emit(ev.ResourceWarning(time.time(), status.kind, status.message))
        log.warning("resources low: %s", status.message, extra={"event": "resource_wait"})
        deadline = time.monotonic() + self.limits.resource_wait_max_s
        ok = False
        while time.monotonic() < deadline and not self._batch_token.wait(self.resource_poll_s):
            status = self.resource_probe()
            if status.ok:
                ok = True
                break
        with self._lock:
            if not ok and not self._stop_reason:
                self._stop_reason = status.message or "Недостатньо системних ресурсів."
            if self._batch_state is BatchState.RESOURCE_WAIT:
                self._set_state(BatchState.RUNNING if ok else BatchState.STOPPING,
                                "" if ok else status.message)
        return ok

    def enqueue(self, job_ids: Sequence[str]) -> bool:
        """Append jobs to a running batch. Returns False if the batch is not
        running (caller should start a new batch instead)."""
        with self._lock:
            if self._batch_state in (BatchState.IDLE, BatchState.STOPPING, BatchState.STOPPED,
                                     BatchState.COMPLETED) or self._batch_token.cancelled:
                return False
            self._pending.extend(job_ids)
            return True

    def _slot(self) -> None:
        # bounded: each iteration consumes one job; _next_job() returns None when empty
        for _ in range(MAX_JOBS_PER_BATCH):
            try:
                self._pause.checkpoint(self._batch_token)
            except JobCancelledError:
                return
            job_id = self._next_job()
            if job_id is None:
                return
            if not self._wait_resources():
                self._fail_for_resources(job_id)
                self._batch_token.cancel("Недостатньо системних ресурсів.")
                return
            try:
                # PAUSE/STOP may have been requested while waiting for resources
                self._pause.checkpoint(self._batch_token)
            except JobCancelledError:
                with self._lock:
                    self._pending.appendleft(job_id)   # supervisor marks it CANCELLED
                return
            token = CancellationToken(parent=self._batch_token)
            with self._lock:
                self._active[job_id] = token
            try:
                final = self.jobs.run(job_id, token, self._pause)
                self.emit(ev.JobFinished(time.time(), job_id, final.status, final.output_file,
                                         final.error, final.skipped_images))
                if final.status is JobStatus.FAILED and final.error is not None \
                        and final.error.code == "DISK_SPACE":
                    log.error("disk space exhausted; pausing batch", extra={"job_id": job_id})
                    self.pause()
            except Exception:  # noqa: BLE001 - one job must never kill the batch
                log.exception("unexpected error running job", extra={"job_id": job_id})
            finally:
                token.detach()
                with self._lock:
                    self._active.pop(job_id, None)
                self._emit_counters()

    def _fail_for_resources(self, job_id: str) -> None:
        try:
            self.state.transition(job_id, JobStatus.CANCELLED, error=ErrorInfo(
                ErrorClass.RESOURCE.value, "RESOURCE_TIMEOUT",
                "Недостатньо системних ресурсів (пам'ять або диск) — пакет зупинено."))
        except Exception:  # noqa: BLE001
            log.exception("could not mark job", extra={"job_id": job_id})

    def _supervise(self) -> None:
        for t in self._threads:
            t.join()  # invariant-ok: slots terminate (finite queue, cancel-aware waits)
        # anything never started is cancelled when the batch was stopped
        with self._lock:
            leftover = list(self._pending)
            self._pending.clear()
            stopped = self._batch_token.cancelled
        for job_id in leftover:
            try:
                self.state.transition(job_id, JobStatus.CANCELLED, error=JobCancelledError(
                    self._stop_reason or "Пакет зупинено.").to_info())
            except Exception:  # noqa: BLE001
                log.exception("could not cancel leftover job", extra={"job_id": job_id})
        self._emit_counters()
        with self._lock:
            if stopped:
                if self._batch_state is not BatchState.STOPPING:
                    self._batch_state = BatchState.STOPPING
                self._set_state(BatchState.STOPPED, self._stop_reason)
            else:
                if self._batch_state in (BatchState.PAUSING, BatchState.PAUSED, BatchState.RESOURCE_WAIT):
                    self._batch_state = BatchState.RUNNING
                self._set_state(BatchState.COMPLETED)
            self._set_state(BatchState.IDLE)
        self._done.set()

    def _emit_counters(self) -> None:
        try:
            c = self.state.counters(self._batch_id)
            self.emit(ev.BatchCounters(time.time(), c.total, c.succeeded, c.partial, c.failed,
                                       c.cancelled, c.skipped_images, None))
        except Exception:  # noqa: BLE001
            log.exception("could not emit counters")

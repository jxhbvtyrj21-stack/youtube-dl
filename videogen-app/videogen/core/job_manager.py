"""Life cycle of a single job: attempts, retry policy, finalisation
(ARCHITECTURE.md §5.1, §11).

The job's real work is done by a :class:`JobExecutor` (the media pipeline,
PHASE 5). This module owns everything around it that must be uniform and
bounded: state transitions (write-ahead), error classification, the finite
retry budget per error class, back-off, and the ``finally`` cleanup.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from videogen.config.settings import RetryPolicy
from videogen.core.cancellation import CancellationToken, PauseGate
from videogen.core.errors import ErrorClass, JobCancelledError, classify, retry_budget
from videogen.core.models import ErrorInfo, JobState, JobStatus, Stage
from videogen.core.state_manager import StateManager

log = logging.getLogger(__name__)

#: Absolute ceiling on attempts across restarts, regardless of error class.
#: Guards against a job that crashes the whole Engine being resumed forever.
HARD_ATTEMPT_CAP = 8


@dataclass(frozen=True)
class JobOutcome:
    status: JobStatus                  # SUCCESS or PARTIAL
    output_file: str
    skipped_images: int = 0
    warnings: tuple[str, ...] = ()


@dataclass
class JobContext:
    job: JobState
    attempt: int
    token: CancellationToken
    pause: PauseGate
    state: StateManager
    on_stage: Callable[[str, Stage, int], None] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def job_id(self) -> str:
        return self.job.job_id

    def stage(self, stage: Stage) -> None:
        """Record the stage *before* doing its work (write-ahead)."""
        self.token.raise_if_cancelled()
        self.state.set_stage(self.job_id, stage)
        log.info("stage %s", stage.value, extra={"job_id": self.job_id, "stage": stage.value,
                                                  "event": "stage_start"})
        if self.on_stage is not None:
            self.on_stage(self.job_id, stage, self.attempt)

    def checkpoint(self) -> None:
        """Call between atomic operations: honours cancel and pause."""
        self.pause.checkpoint(self.token)


class JobExecutor(Protocol):
    def execute(self, ctx: JobContext) -> JobOutcome:
        """Do the work. Raise a VideoGenError subclass on failure."""

    def prepare_retry(self, ctx: JobContext, error: ErrorInfo) -> None:
        """Make the workspace clean for the next attempt. Must not raise."""

    def finalize(self, ctx: JobContext, final: JobState) -> None:
        """Save diagnostics and clean the workspace. Must not raise."""


class JobManager:
    def __init__(self, state: StateManager, executor: JobExecutor, retry: RetryPolicy,
                 *, on_stage: Callable[[str, Stage, int], None] | None = None,
                 sleep: Callable[[CancellationToken, float], bool] | None = None) -> None:
        self.state = state
        self.executor = executor
        self.retry = retry
        self.on_stage = on_stage
        self._sleep = sleep or (lambda token, s: token.wait(s))

    def max_attempts(self) -> int:
        r = self.retry
        return 1 + r.transient + r.ffmpeg_crash + r.timeout + r.verification

    def run(self, job_id: str, token: CancellationToken, pause: PauseGate) -> JobState:
        """Run a QUEUED (or RETRY_PENDING) job to a terminal state. Never raises
        for job-level failures; returns the final JobState."""
        job = self.state.get_job(job_id)
        if job.status not in (JobStatus.QUEUED, JobStatus.RETRY_PENDING):
            raise ValueError(f"job {job_id} is {job.status.value}, cannot run")

        used: Counter[ErrorClass] = Counter()
        ctx: JobContext | None = None
        final: JobState | None = None
        try:
            for _ in range(self.max_attempts()):
                if token.cancelled:
                    final = self.state.transition(job_id, JobStatus.CANCELLED, error=JobCancelledError().to_info())
                    break
                if job.attempts >= HARD_ATTEMPT_CAP:
                    err = ErrorInfo(ErrorClass.INTERNAL.value, "TOO_MANY_ATTEMPTS",
                                    f"Завдання перевищило максимальну кількість спроб ({HARD_ATTEMPT_CAP}).")
                    self.state.transition(job_id, JobStatus.RUNNING, stage=Stage.NONE)
                    final = self.state.transition(job_id, JobStatus.FAILED, error=err)
                    break

                job = self.state.transition(job_id, JobStatus.RUNNING, stage=Stage.NONE,
                                            increment_attempts=True)
                ctx = JobContext(job=job, attempt=job.attempts, token=token, pause=pause,
                                 state=self.state, on_stage=self.on_stage)
                t0 = time.monotonic()
                log.info("attempt %d started", job.attempts,
                         extra={"job_id": job_id, "event": "attempt_start"})
                try:
                    outcome = self.executor.execute(ctx)
                except JobCancelledError as exc:
                    final = self.state.transition(job_id, JobStatus.CANCELLED, error=exc.to_info())
                    break
                except Exception as exc:  # noqa: BLE001 - every failure is classified
                    info = classify(exc)
                    if token.cancelled:
                        final = self.state.transition(job_id, JobStatus.CANCELLED,
                                                      error=JobCancelledError().to_info())
                        break
                    cls = ErrorClass(info.error_class)
                    used[cls] += 1
                    level = logging.ERROR if cls is ErrorClass.INTERNAL else logging.WARNING
                    log.log(level, "attempt %d failed: %s [%s/%s]", job.attempts, info.message,
                            info.error_class, info.code,
                            extra={"job_id": job_id, "event": "attempt_failed",
                                   "duration_ms": int((time.monotonic() - t0) * 1000),
                                   "error": info.code, "detail": info.detail})
                    if used[cls] <= retry_budget(cls, self.retry):
                        job = self.state.transition(job_id, JobStatus.RETRY_PENDING, error=info)
                        self.executor.prepare_retry(ctx, info)
                        delay = self._backoff(cls, used[cls])
                        if delay > 0 and self._sleep(token, delay):
                            final = self.state.transition(job_id, JobStatus.CANCELLED,
                                                          error=JobCancelledError().to_info())
                            break
                        continue
                    final = self.state.transition(job_id, JobStatus.FAILED, error=info)
                    break
                else:
                    if outcome.status not in (JobStatus.SUCCESS, JobStatus.PARTIAL):
                        raise ValueError(f"executor returned non-final status {outcome.status}")
                    final = self.state.transition(
                        job_id, outcome.status, output_file=outcome.output_file,
                        skipped_images=outcome.skipped_images, clear_error=True)
                    log.info("job finished: %s -> %s", outcome.status.value, outcome.output_file,
                             extra={"job_id": job_id, "event": "job_done",
                                    "duration_ms": int((time.monotonic() - t0) * 1000)})
                    break
            if final is None:
                # Loop exhausted: only possible if budgets were changed mid-run.
                final = self.state.transition(
                    job_id, JobStatus.FAILED,
                    error=ErrorInfo(ErrorClass.INTERNAL.value, "RETRY_EXHAUSTED",
                                    "Вичерпано кількість спроб."))
            return final
        except BaseException:
            # Unexpected programming error inside the manager itself: make sure
            # the job does not stay RUNNING, then propagate.
            log.exception("job manager failure", extra={"job_id": job_id})
            try:
                cur = self.state.get_job(job_id)
                if cur.status in (JobStatus.RUNNING, JobStatus.RETRY_PENDING):
                    final = self.state.transition(job_id, JobStatus.FAILED, error=ErrorInfo(
                        ErrorClass.INTERNAL.value, "INTERNAL",
                        "Внутрішня помилка програми. Подробиці записано в журнал."))
            except Exception:  # noqa: BLE001
                log.exception("could not mark job failed", extra={"job_id": job_id})
            raise
        finally:
            if ctx is not None:
                try:
                    self.executor.finalize(ctx, final or self.state.get_job(job_id))
                except Exception:  # noqa: BLE001 - finalize must never mask the result
                    log.exception("finalize failed", extra={"job_id": job_id})

    def _backoff(self, cls: ErrorClass, n: int) -> float:
        if cls is ErrorClass.TRANSIENT:
            delays = self.retry.transient_backoff_s
            return delays[min(n - 1, len(delays) - 1)] if delays else 0.0
        return 0.0

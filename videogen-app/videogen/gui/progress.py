"""GUI view-model: folds Engine events into display state (no Qt here).

Keeping this logic Qt-free makes it fully unit-testable and keeps the
main-thread work per event tiny (dict/deque updates only).
"""

from __future__ import annotations

import collections
import time
from dataclasses import dataclass, field

from videogen.core import events as ev
from videogen.core.models import BatchState, JobStatus, Stage

MAX_LOG_LINES = 2000

STAGE_NAMES = {
    Stage.NONE: "Підготовка", Stage.VALIDATING: "Перевірка вхідних даних",
    Stage.GENERATING: "Генерація озвучки та зображень", Stage.AUDIO: "Обробка аудіо",
    Stage.NORMALIZING: "Нормалізація зображень", Stage.TIMELINE: "Розрахунок таймлайну",
    Stage.RENDERING: "Рендеринг", Stage.MUXING: "Збирання відео", Stage.VERIFYING: "Перевірка відео",
    Stage.FINALIZING: "Збереження результату", Stage.ARCHIVING: "Архівування",
    Stage.CLEANUP: "Очищення",
}
BATCH_NAMES = {
    BatchState.IDLE: "Очікування", BatchState.RUNNING: "Виконується", BatchState.PAUSING: "Призупинення…",
    BatchState.PAUSED: "Призупинено", BatchState.RESOURCE_WAIT: "Очікування ресурсів",
    BatchState.STOPPING: "Зупинка…", BatchState.STOPPED: "Зупинено", BatchState.COMPLETED: "Завершено",
}
STATUS_NAMES = {
    JobStatus.SUCCESS: "успішно", JobStatus.PARTIAL: "неповне (PARTIAL)", JobStatus.FAILED: "помилка",
    JobStatus.CANCELLED: "скасовано", JobStatus.INTERRUPTED: "перервано",
}


@dataclass
class ButtonState:
    start: bool = True
    pause: bool = False
    resume: bool = False
    stop: bool = False
    cancel_current: bool = False
    inputs: bool = True      # mode / format / folder pickers


def buttons_for(state: BatchState, engine_ready: bool, engine_alive: bool) -> ButtonState:
    if not engine_alive or not engine_ready:
        return ButtonState(start=False, inputs=True)
    if state is BatchState.IDLE:
        return ButtonState(start=True, inputs=True)
    if state in (BatchState.RUNNING, BatchState.RESOURCE_WAIT):
        return ButtonState(start=False, pause=True, stop=True, cancel_current=True, inputs=False)
    if state is BatchState.PAUSING:
        return ButtonState(start=False, resume=True, stop=True, cancel_current=True, inputs=False)
    if state is BatchState.PAUSED:
        return ButtonState(start=False, resume=True, stop=True, cancel_current=False, inputs=False)
    # STOPPING / STOPPED / COMPLETED are transient on the way to IDLE
    return ButtonState(start=False, inputs=False)


@dataclass
class JobView:
    job_id: str
    name: str
    stage: Stage = Stage.NONE
    percent: float = 0.0
    status: JobStatus | None = None
    frame: int = 0
    total_frames: int = 0
    fps: float = 0.0
    speed: float = 0.0


@dataclass
class ViewModel:
    engine_ready: bool = False
    engine_alive: bool = True
    engine_version: str = ""
    ffmpeg_version: str = ""
    batch_state: BatchState = BatchState.IDLE
    batch_reason: str = ""
    jobs: dict[str, JobView] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    current: str | None = None
    succeeded: int = 0
    partial: int = 0
    failed: int = 0
    cancelled: int = 0
    skipped_files: int = 0
    eta_s: float | None = None
    last_heartbeat: float = field(default_factory=time.monotonic)
    log: collections.deque[str] = field(default_factory=lambda: collections.deque(maxlen=MAX_LOG_LINES))
    errors: list[str] = field(default_factory=list)          # user-facing popups (drained by the view)
    interrupted: list[tuple[str, str]] = field(default_factory=list)
    dirty: bool = True
    log_seq: int = 0          # total lines ever logged (the view appends only new ones)

    # ------------------------------------------------------------ reducer

    def apply(self, e: ev.Event) -> None:
        self.dirty = True
        if isinstance(e, ev.Heartbeat):
            self.last_heartbeat = time.monotonic()
            self.engine_alive = True
            self.batch_state = e.batch_state
            return
        self.last_heartbeat = time.monotonic()
        if isinstance(e, ev.EngineReady):
            self.engine_ready, self.engine_version, self.ffmpeg_version = True, e.version, e.ffmpeg_version
            self._log("INFO", f"Програма готова (версія {e.version}, FFmpeg {e.ffmpeg_version or 'не знайдено'}).")
        elif isinstance(e, ev.BatchStateChanged):
            self.batch_state, self.batch_reason = e.state, e.reason
            self._log("INFO", f"Стан: {BATCH_NAMES.get(e.state, e.state.value)}"
                              + (f" — {e.reason}" if e.reason else ""))
        elif isinstance(e, ev.JobQueued):
            if e.job_id not in self.jobs:
                self.order.append(e.job_id)
            self.jobs[e.job_id] = JobView(e.job_id, e.name)
        elif isinstance(e, ev.JobStageChanged):
            jv = self._job(e.job_id)
            jv.stage = e.stage
            self.current = e.job_id
        elif isinstance(e, ev.JobProgress):
            jv = self._job(e.job_id)
            jv.stage, jv.percent = e.stage, max(jv.percent if jv.stage == e.stage else 0.0, e.percent)
            jv.frame, jv.total_frames, jv.fps, jv.speed = e.frame, e.total_frames, e.fps, e.speed
            self.current = e.job_id
        elif isinstance(e, ev.JobFinished):
            jv = self._job(e.job_id)
            jv.status = e.status
            if e.status in (JobStatus.SUCCESS, JobStatus.PARTIAL):
                jv.percent = 100.0
            text = f"«{jv.name}»: {STATUS_NAMES.get(e.status, e.status.value)}"
            if e.error is not None and e.status is not JobStatus.CANCELLED:
                text += f" — {e.error.message}"
            elif e.output_file:
                text += f" → {e.output_file}"
            self._log("ERROR" if e.status is JobStatus.FAILED else
                      "WARNING" if e.status is JobStatus.PARTIAL else "INFO", text)
            if self.current == e.job_id:
                self.current = None
        elif isinstance(e, ev.BatchCounters):
            self.succeeded, self.partial, self.failed = e.succeeded, e.partial, e.failed
            self.cancelled, self.skipped_files, self.eta_s = e.cancelled, e.skipped_files, e.eta_s
        elif isinstance(e, ev.ImageSkipped):
            self._log("WARNING", f"Зображення №{e.index} ({e.file_name}): {e.reason}")
        elif isinstance(e, ev.ResourceWarning):
            self._log("WARNING", e.message)
        elif isinstance(e, ev.InterruptedJobsFound):
            self.interrupted = list(zip(e.job_ids, e.names))
        elif isinstance(e, ev.LogLine):
            self._log(e.level, e.message)
        elif isinstance(e, ev.EngineError):
            self.errors.append(e.message)
            self._log("ERROR", e.message)

    def _job(self, job_id: str) -> JobView:
        jv = self.jobs.get(job_id)
        if jv is None:
            jv = JobView(job_id, job_id)
            self.jobs[job_id] = jv
            self.order.append(job_id)
        return jv

    def _log(self, level: str, text: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')}  {level:<7} {text}")
        self.log_seq += 1

    def reset_batch(self) -> None:
        self.jobs.clear()
        self.order.clear()
        self.current = None
        self.succeeded = self.partial = self.failed = self.cancelled = self.skipped_files = 0
        self.eta_s = None
        self.dirty = True

    # ------------------------------------------------------------ derived

    @property
    def current_job(self) -> JobView | None:
        return self.jobs.get(self.current) if self.current else None

    @property
    def overall_percent(self) -> float:
        runnable = [j for j in self.jobs.values()]
        if not runnable:
            return 0.0
        done = sum(1 for j in runnable if j.status is not None)
        active = sum(j.percent for j in runnable if j.status is None) / 100.0
        return min(100.0, 100.0 * (done + active) / len(runnable))

    @property
    def stage_text(self) -> str:
        j = self.current_job
        if j is None:
            return BATCH_NAMES.get(self.batch_state, "")
        detail = ""
        if j.stage is Stage.RENDERING and j.total_frames:
            detail = f" — кадр {j.frame}/{j.total_frames}, {j.fps:.0f} fps, ×{j.speed:.2f}"
        return f"{j.name}: {STAGE_NAMES.get(j.stage, j.stage.value)}{detail}"

    def eta_text(self) -> str:
        if self.batch_state not in (BatchState.RUNNING, BatchState.PAUSING, BatchState.RESOURCE_WAIT):
            return "—"
        if self.eta_s is None:
            return "оцінюється…"
        s = int(self.eta_s)
        return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    def heartbeat_age(self) -> float:
        return time.monotonic() - self.last_heartbeat

    def buttons(self) -> ButtonState:
        return buttons_for(self.batch_state, self.engine_ready, self.engine_alive)

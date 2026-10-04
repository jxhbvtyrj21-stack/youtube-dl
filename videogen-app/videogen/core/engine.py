"""Engine: owns state, queue, pipeline, logging and recovery
(ARCHITECTURE.md §3, §14). Runs in its own process (see engine_main.py);
also usable in-process for tests and the headless self-test.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from videogen import __version__
from videogen.applog.diagnostics import prune_diagnostics
from videogen.applog.logger import CallbackHandler, LogSystem
from videogen.config.settings import Settings
from videogen.core import events as ev
from videogen.core.discovery import DiscoveredJob, discover
from videogen.core.errors import AlreadyRunningError, FFmpegUnavailableError, InputError, VideoGenError
from videogen.core.job_manager import JobManager
from videogen.core.models import (
    BatchState, JobConfig, JobState, JobStatus, Mode, Orientation, RecoveryAction,
)
from videogen.core.pipeline import MediaPipeline, PipelineEnv
from videogen.core.queue_manager import QueueManager, ResourceStatus
from videogen.core.state_manager import StateManager
from videogen.ffmpeg_ctl.locator import FFmpegTools, locate
from videogen.ffmpeg_ctl.process_manager import REGISTRY
from videogen.providers.base import ImageGenProvider, TTSProvider
from videogen.storage.cleanup import remove_stale_parts, remove_tree
from videogen.storage.workspace import WorkspaceRoot, new_batch_id
from videogen.utils.system import InstanceLock
from videogen.workers.resource_monitor import ResourceMonitor

log = logging.getLogger(__name__)
ETA_MIN_ELAPSED_S = 10.0


class EtaTracker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset(0)

    def reset(self, total: int) -> None:
        with self._lock:
            self.total = total
            self.started = time.monotonic()
            self.finished = 0
            self.active: dict[str, float] = {}

    def progress(self, job_id: str, percent: float) -> None:
        with self._lock:
            self.active[job_id] = percent

    def done(self, job_id: str) -> None:
        with self._lock:
            self.active.pop(job_id, None)
            self.finished += 1

    def eta(self) -> float | None:
        with self._lock:
            if self.total <= 0:
                return None
            frac = (self.finished + sum(self.active.values()) / 100.0) / self.total
            elapsed = time.monotonic() - self.started
        if elapsed < ETA_MIN_ELAPSED_S or frac <= 0.01:
            return None
        return max(0.0, elapsed * (1 - frac) / frac)


class Engine:
    def __init__(self, appdata: Path, settings: Settings, emit: Callable[[ev.Event], None], *,
                 tools: FFmpegTools | None = None, tts: TTSProvider | None = None,
                 image_provider: ImageGenProvider | None = None, install_logging: bool = True) -> None:
        self.appdata = Path(appdata)
        self.appdata.mkdir(parents=True, exist_ok=True)
        self._lock = InstanceLock(self.appdata / "engine.lock")
        if not self._lock.acquire():
            raise AlreadyRunningError(
                "Програма вже запущена (інший екземпляр використовує ті самі дані). "
                "Закрийте його та спробуйте знову.")
        self.settings = settings
        self._raw_emit = emit
        self.eta = EtaTracker()
        self.log_system: LogSystem | None = None
        if install_logging:
            self.log_system = LogSystem(self.appdata / "logs", settings.logging)
            self.log_system.install()
            self.log_system.add_handler(CallbackHandler(self._forward_log, logging.INFO))
        self.state = StateManager(self.appdata / "state.db")
        self._tools = tools
        self._tts, self._images = tts, image_provider
        self._monitor: ResourceMonitor | None = None
        self.pipeline: MediaPipeline | None = None
        self.queue: QueueManager | None = None
        self.current_batch: str | None = None

    # ------------------------------------------------------------ events

    def emit(self, e: ev.Event) -> None:
        if isinstance(e, ev.JobProgress):
            self.eta.progress(e.job_id, e.percent)
        elif isinstance(e, ev.JobFinished):
            self.eta.done(e.job_id)
        elif isinstance(e, ev.BatchCounters):
            e = ev.BatchCounters(e.ts, e.total, e.succeeded, e.partial, e.failed, e.cancelled,
                                 e.skipped_files, self.eta.eta())
        try:
            self._raw_emit(e)
        except Exception:  # noqa: BLE001 - the GUI channel must never break the engine
            pass  # invariant-ok: delivery failure of a UI event; state DB stays the source of truth

    def _forward_log(self, record: logging.LogRecord) -> None:
        if not record.name.startswith("videogen"):
            return
        job = getattr(record, "job_id", "-")
        self.emit(ev.LogLine(time.time(), record.levelname, record.getMessage().split("\n", 1)[0][:500],
                             "" if job in (None, "-") else str(job)))

    # ------------------------------------------------------------ tools

    def tools(self) -> FFmpegTools:
        if self._tools is None:
            self._tools = locate(os.environ.get("VIDEOGEN_FFMPEG_DIR"))
        return self._tools

    def _ensure_runtime(self) -> None:
        if self.pipeline is not None:
            return
        env = PipelineEnv(self.settings, self.tools(), self.state, self.appdata, self.emit, self.log_system,
                          self._tts, self._images)
        self.pipeline = MediaPipeline(env)
        jm = JobManager(self.state, self.pipeline, self.settings.retry, on_stage=self._on_stage)
        self.queue = QueueManager(self.state, jm, self.emit,
                                  max_parallel=self.settings.concurrency.max_parallel_jobs,
                                  limits=self.settings.resources, resource_probe=self._probe)

    def _probe(self) -> ResourceStatus:
        return self._monitor.probe() if self._monitor is not None else ResourceStatus(True)

    def _on_stage(self, job_id: str, stage, attempt: int) -> None:  # type: ignore[no-untyped-def]
        self.emit(ev.JobStageChanged(time.time(), job_id, stage, attempt))

    # ------------------------------------------------------------ startup / recovery

    def startup(self) -> list[JobState]:
        """Crash recovery (§14). Returns the INTERRUPTED jobs."""
        if self.state.open_report.recovered_from_corruption:
            self.emit(ev.EngineError(time.time(), "Базу стану було пошкоджено; її відновлено з нуля.",
                                     self.state.open_report.corrupt_copy))
        self.state.mark_running_as_interrupted()
        known = self.state.successful_outputs()
        for _batch, out_dir, _ws in self.state.batch_dirs():
            removed = remove_stale_parts(Path(out_dir), {k for k in known})
            for r in removed:
                log.warning("removed incomplete output %s", r)
        # workspaces still needed for Resume are never cleaned, whatever was recorded
        resumable = {self.state.workspace_dir(j.job_id)
                     for j in self.state.list_jobs(statuses=[JobStatus.INTERRUPTED])}
        for path, attempts in self.state.pending_cleanups():
            if path in resumable:
                self.state.remove_pending_cleanup(path)
                continue
            res = remove_tree(Path(path), deadline_s=30)
            if res.ok or not Path(path).exists():
                self.state.remove_pending_cleanup(path)
            elif attempts >= 5:
                log.error("giving up cleaning %s", path)
                self.state.remove_pending_cleanup(path)
            else:
                self.state.add_pending_cleanup(path)
        prune_diagnostics(self.appdata / "diagnostics", self.settings.cleanup.diagnostics_max_jobs,
                          self.settings.cleanup.diagnostics_max_mb)
        interrupted = self.state.list_jobs(statuses=[JobStatus.INTERRUPTED])
        for j in interrupted:
            for p in Path(self.state.workspace_dir(j.job_id)).glob("*/*.tmp*"):
                p.unlink(missing_ok=True)
        if interrupted:
            self.emit(ev.InterruptedJobsFound(time.time(), tuple(j.job_id for j in interrupted),
                                              tuple(j.name for j in interrupted)))
        try:
            version = self.tools().version
        except FFmpegUnavailableError as exc:
            version = ""
            self.emit(ev.EngineError(time.time(), exc.user_message, exc.detail))
        self.emit(ev.EngineReady(time.time(), __version__, version))
        return interrupted

    def recover(self, job_id: str, action: RecoveryAction) -> None:
        job = self.state.get_job(job_id)
        if job.status is not JobStatus.INTERRUPTED:
            return
        ws = Path(self.state.workspace_dir(job_id))
        if action is RecoveryAction.IGNORE:
            self.state.transition(job_id, JobStatus.CANCELLED)     # also schedules the cleanup
            if remove_tree(ws, deadline_s=60).ok:
                self.state.remove_pending_cleanup(str(ws))
            return
        self.state.transition(job_id, JobStatus.QUEUED, clear_error=True)
        if action is RecoveryAction.RETRY:
            self.state.set_resume_point(job_id, 0)
            remove_tree(ws, deadline_s=60)
        self._run_jobs([job_id], job.batch_id)

    # ------------------------------------------------------------ batches

    def start_batch(self, cmd: ev.StartBatch) -> str | None:
        try:
            return self._start_batch(cmd)
        except VideoGenError as exc:
            log.error("cannot start batch: %s", exc.user_message)
            self.emit(ev.EngineError(time.time(), exc.user_message, exc.detail))
            return None

    def _start_batch(self, cmd: ev.StartBatch) -> str:
        if self.queue is not None and self.queue.batch_state is not BatchState.IDLE:
            raise InputError("Обробка вже виконується.", code="BUSY")
        mode, orient = Mode(cmd.mode), Orientation(cmd.orientation)
        inp, out = Path(cmd.input_dir), Path(cmd.output_dir)
        if not inp.is_dir():
            raise InputError(f"Вхідну папку не знайдено: {inp}", code="INPUT_MISSING")
        if not cmd.output_dir:
            raise InputError("Не вибрано папку для результатів.", code="OUTPUT_MISSING")
        root = WorkspaceRoot(Path(cmd.workspace_dir or (self.appdata / "workspace")))
        root.ensure()
        self._ensure_runtime()
        found = discover(inp, mode)
        if not found:
            raise InputError("У вхідній папці не знайдено матеріалів для відео.", code="NOTHING_FOUND")
        batch_id = new_batch_id()
        bws = root.batch(batch_id)
        self.state.add_batch(batch_id, str(inp), str(out), str(root.path), os.getpid())
        v = self.settings.video
        w, h = (v.horizontal_width, v.horizontal_height) if orient is Orientation.HORIZONTAL else \
            (v.vertical_width, v.vertical_height)
        ids: list[str] = []
        for seq, d in enumerate(found, start=1):
            cfg = self._job_config(batch_id, seq, d, mode, orient, out, w, h)
            self.state.add_job(cfg, seq, str(bws.job(seq).root))
            self.emit(ev.JobQueued(time.time(), cfg.job_id, cfg.name))
            for note in d.notes:
                log.info("%s: %s", d.name, note, extra={"job_id": cfg.job_id})
            if d.problems:
                err = InputError(" ".join(d.problems), code="INPUT_PROBLEM").to_info()
                self.state.transition(cfg.job_id, JobStatus.RUNNING)
                self.state.transition(cfg.job_id, JobStatus.FAILED, error=err)
                log.error("%s: %s", d.name, err.message, extra={"job_id": cfg.job_id})
                self.emit(ev.JobFinished(time.time(), cfg.job_id, JobStatus.FAILED, None, err))
            else:
                ids.append(cfg.job_id)
        self.current_batch = batch_id
        log.info("batch %s: %d job(s) found, %d runnable", batch_id, len(found), len(ids))
        self.eta.reset(len(ids))
        self._start_monitor([root.path, out])
        if ids:
            assert self.queue is not None
            self.queue.start(ids, batch_id=batch_id)
        return batch_id

    def _run_jobs(self, ids: list[str], batch_id: str) -> None:
        self._ensure_runtime()
        assert self.queue is not None
        if not self.queue.enqueue(ids):
            self.eta.reset(len(ids))
            self.queue.start(ids, batch_id=batch_id)

    def _start_monitor(self, paths: list[Path]) -> None:
        if self._monitor is not None:
            self._monitor.stop()
        self._monitor = ResourceMonitor(self.settings.resources, paths)
        self._monitor.start()

    def _job_config(self, batch_id: str, seq: int, d: DiscoveredJob, mode: Mode, orient: Orientation,
                    out: Path, w: int, h: int) -> JobConfig:
        return JobConfig(
            job_id=f"{batch_id}-{seq:04d}", batch_id=batch_id, name=d.name, mode=mode, orientation=orient,
            input_dir=str(d.folder), output_dir=str(out), width=w, height=h,
            fps=self.settings.video.fps, image_files=tuple(str(p) for p in d.images),
            audio_file=str(d.audio) if d.audio else None,
            script_file=str(d.script) if d.script else None,
            prompts_file=str(d.prompts) if d.prompts else None)

    # ------------------------------------------------------------ commands

    def handle(self, cmd: ev.Command) -> None:
        q = self.queue
        if isinstance(cmd, ev.StartBatch):
            self.start_batch(cmd)
        elif isinstance(cmd, ev.Pause) and q:
            q.pause()
        elif isinstance(cmd, ev.Resume) and q:
            q.resume()
        elif isinstance(cmd, ev.Stop) and q:
            q.stop()
        elif isinstance(cmd, ev.CancelCurrentJob) and q:
            q.cancel_current()
        elif isinstance(cmd, ev.RecoveryDecision):
            self.recover(cmd.job_id, cmd.action)

    @property
    def batch_state(self) -> BatchState:
        return self.queue.batch_state if self.queue else BatchState.IDLE

    def wait_idle(self, timeout: float) -> bool:
        return self.queue.wait(timeout) if self.queue else True

    def shutdown(self, timeout: float = 20.0, *, interrupt: bool = False,
                 final_note: Callable[[], str] | None = None) -> None:
        """``interrupt=True`` when the GUI vanished: unfinished jobs stay
        resumable (INTERRUPTED) instead of being cancelled. ``final_note`` is
        logged after everything stopped, while the log is still open."""
        if self.queue is not None and self.queue.batch_state is not BatchState.IDLE:
            self.queue.stop("GUI завершився аварійно" if interrupt else "Програму закрито.", interrupt=interrupt)
            self.queue.wait(timeout)
        survivors = REGISTRY.kill_all()
        if survivors:
            log.critical("processes survived shutdown: %s", survivors)
        if self._monitor is not None:
            self._monitor.stop()
        self.state.close()
        if final_note is not None:
            log.info("%s", final_note())
        if self.log_system is not None:
            self.log_system.shutdown()
        self._lock.release()

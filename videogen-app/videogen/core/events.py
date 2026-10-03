"""Typed messages between GUI and Engine (ARCHITECTURE.md §3.1).

All messages are small frozen dataclasses, safe to pickle through
``multiprocessing.Queue``. No free-form dicts cross the process boundary.
"""

from __future__ import annotations

from dataclasses import dataclass

from videogen.core.models import BatchState, ErrorInfo, JobStatus, RecoveryAction, Stage

# ---------------------------------------------------------------- commands


@dataclass(frozen=True)
class Command:
    pass


@dataclass(frozen=True)
class StartBatch(Command):
    mode: str
    orientation: str
    input_dir: str
    output_dir: str
    workspace_dir: str


@dataclass(frozen=True)
class Pause(Command):
    pass


@dataclass(frozen=True)
class Resume(Command):
    pass


@dataclass(frozen=True)
class Stop(Command):
    pass


@dataclass(frozen=True)
class CancelCurrentJob(Command):
    pass


@dataclass(frozen=True)
class RecoveryDecision(Command):
    job_id: str
    action: RecoveryAction


@dataclass(frozen=True)
class Shutdown(Command):
    pass


@dataclass(frozen=True)
class Ping(Command):
    token: int = 0


# ------------------------------------------------------------------ events


@dataclass(frozen=True)
class Event:
    ts: float


@dataclass(frozen=True)
class EngineReady(Event):
    version: str
    ffmpeg_version: str


@dataclass(frozen=True)
class Heartbeat(Event):
    batch_state: BatchState


@dataclass(frozen=True)
class BatchStateChanged(Event):
    state: BatchState
    reason: str = ""


@dataclass(frozen=True)
class BatchCounters(Event):
    total: int
    succeeded: int
    partial: int
    failed: int
    cancelled: int
    skipped_files: int
    eta_s: float | None


@dataclass(frozen=True)
class JobQueued(Event):
    job_id: str
    name: str


@dataclass(frozen=True)
class JobStageChanged(Event):
    job_id: str
    stage: Stage
    attempt: int


@dataclass(frozen=True)
class JobProgress(Event):
    """Throttled (<= 5/s per job). Droppable under back-pressure."""

    job_id: str
    stage: Stage
    percent: float
    frame: int = 0
    total_frames: int = 0
    fps: float = 0.0
    speed: float = 0.0
    out_time_s: float = 0.0


@dataclass(frozen=True)
class JobFinished(Event):
    job_id: str
    status: JobStatus
    output_file: str | None = None
    error: ErrorInfo | None = None
    skipped_images: int = 0


@dataclass(frozen=True)
class ImageSkipped(Event):
    job_id: str
    index: int          # 1-based for users
    file_name: str
    reason: str


@dataclass(frozen=True)
class ResourceWarning(Event):
    kind: str           # "ram" | "disk" | "cpu"
    message: str


@dataclass(frozen=True)
class InterruptedJobsFound(Event):
    job_ids: tuple[str, ...]
    names: tuple[str, ...]


@dataclass(frozen=True)
class LogLine(Event):
    """INFO+ log line for the GUI journal. Droppable under back-pressure."""

    level: str
    message: str
    job_id: str = ""


@dataclass(frozen=True)
class EngineError(Event):
    message: str
    detail: str = ""


#: Events that may be dropped when the GUI cannot keep up; every other event
#: type must be delivered (ARCHITECTURE.md §3.1).
DROPPABLE_EVENTS: tuple[type[Event], ...] = (JobProgress, LogLine, Heartbeat)


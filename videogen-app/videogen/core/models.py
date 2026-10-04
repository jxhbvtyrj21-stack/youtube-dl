"""Typed domain entities and state machines (ARCHITECTURE.md §5)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Mode(str, Enum):
    AUDIO_IMAGES = "A"        # Audio + Images -> Video
    SCRIPT_PROMPTS = "B"      # Script + Prompts -> Voice + Images -> Video


class Orientation(str, Enum):
    HORIZONTAL = "16:9"
    VERTICAL = "9:16"


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_PENDING = "RETRY_PENDING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"


class Stage(str, Enum):
    NONE = "NONE"
    VALIDATING = "VALIDATING"
    GENERATING = "GENERATING"      # MODE B only
    NORMALIZING = "NORMALIZING"
    AUDIO = "AUDIO"
    TIMELINE = "TIMELINE"
    RENDERING = "RENDERING"
    MUXING = "MUXING"
    VERIFYING = "VERIFYING"
    FINALIZING = "FINALIZING"
    ARCHIVING = "ARCHIVING"
    CLEANUP = "CLEANUP"


class BatchState(str, Enum):
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    RESOURCE_WAIT = "RESOURCE_WAIT"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    COMPLETED = "COMPLETED"


class ImageStatus(str, Enum):
    PENDING = "PENDING"
    VALID = "VALID"
    NORMALIZED = "NORMALIZED"
    INVALID = "INVALID"


class RecoveryAction(str, Enum):
    RESUME = "RESUME"
    RETRY = "RETRY"
    IGNORE = "IGNORE"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.SUCCESS, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED,
})

#: Single source of truth for allowed job transitions (ARCHITECTURE.md §5.1).
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED, JobStatus.INTERRUPTED}),
    JobStatus.RUNNING: frozenset({
        JobStatus.SUCCESS, JobStatus.PARTIAL, JobStatus.FAILED, JobStatus.CANCELLED,
        JobStatus.INTERRUPTED, JobStatus.RETRY_PENDING,
    }),
    JobStatus.RETRY_PENDING: frozenset({
        JobStatus.RUNNING, JobStatus.QUEUED, JobStatus.CANCELLED, JobStatus.INTERRUPTED,
    }),
    JobStatus.INTERRUPTED: frozenset({JobStatus.QUEUED, JobStatus.CANCELLED}),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
    JobStatus.PARTIAL: frozenset({JobStatus.QUEUED}),
    JobStatus.CANCELLED: frozenset({JobStatus.QUEUED}),
    JobStatus.SUCCESS: frozenset(),
}

#: Allowed batch transitions (ARCHITECTURE.md §5.2).
ALLOWED_BATCH_TRANSITIONS: dict[BatchState, frozenset[BatchState]] = {
    BatchState.IDLE: frozenset({BatchState.RUNNING}),
    BatchState.RUNNING: frozenset({
        BatchState.PAUSING, BatchState.RESOURCE_WAIT, BatchState.STOPPING, BatchState.COMPLETED,
    }),
    BatchState.PAUSING: frozenset({BatchState.PAUSED, BatchState.STOPPING}),
    BatchState.PAUSED: frozenset({BatchState.RUNNING, BatchState.STOPPING}),
    BatchState.RESOURCE_WAIT: frozenset({
        BatchState.RUNNING, BatchState.STOPPING, BatchState.PAUSING,
    }),
    BatchState.STOPPING: frozenset({BatchState.STOPPED}),
    BatchState.STOPPED: frozenset({BatchState.IDLE}),
    BatchState.COMPLETED: frozenset({BatchState.IDLE}),
}


class IllegalTransition(RuntimeError):
    """A programming error: an attempt to move a state machine along an edge
    that does not exist."""


def check_transition(current: JobStatus, new: JobStatus) -> None:
    if new not in ALLOWED_TRANSITIONS[current]:
        raise IllegalTransition(f"job transition {current.value} -> {new.value} is not allowed")


def check_batch_transition(current: BatchState, new: BatchState) -> None:
    if new not in ALLOWED_BATCH_TRANSITIONS[current]:
        raise IllegalTransition(f"batch transition {current.value} -> {new.value} is not allowed")


# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ErrorInfo:
    error_class: str
    code: str
    message: str            # user facing (Ukrainian)
    detail: str = ""        # technical, for logs/manifest only

    def to_dict(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class JobConfig:
    """Immutable description of what a job must produce."""

    job_id: str
    batch_id: str
    name: str
    mode: Mode
    orientation: Orientation
    input_dir: str
    output_dir: str
    width: int
    height: int
    fps: int
    image_files: tuple[str, ...] = ()
    audio_file: str | None = None
    script_file: str | None = None
    prompts_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["mode"] = self.mode.value
        d["orientation"] = self.orientation.value
        d["image_files"] = list(self.image_files)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobConfig":
        data = dict(d)
        data["mode"] = Mode(data["mode"])
        data["orientation"] = Orientation(data["orientation"])
        data["image_files"] = tuple(data.get("image_files") or ())
        return cls(**data)


@dataclass(frozen=True)
class JobState:
    """Snapshot of a job row in the state database."""

    job_id: str
    batch_id: str
    name: str
    status: JobStatus
    stage: Stage
    attempts: int
    created_at: str
    updated_at: str
    started_at: str | None
    ended_at: str | None
    error: ErrorInfo | None
    output_file: str | None
    skipped_images: int
    resume_from_segment: int
    config: JobConfig


@dataclass
class ImageItem:
    """Per-image bookkeeping. Holds metadata only — never pixels."""

    index: int
    source_path: str
    status: ImageStatus = ImageStatus.PENDING
    size_bytes: int = 0
    sha256: str = ""
    hash_mode: str = "full"
    detected_format: str = ""
    width: int = 0
    height: int = 0
    mode: str = ""
    has_alpha: bool = False
    decoder: str = ""
    decoders_tried: list[str] = field(default_factory=list)
    #: decoded by a fallback decoder after the primary one reported damaged
    #: data: usable, but may contain artefacts -> job is PARTIAL (degraded)
    recovered: bool = False
    normalized_path: str = ""
    reason_code: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ImageItem":
        data = dict(d)
        data["status"] = ImageStatus(data.get("status", ImageStatus.PENDING.value))
        return cls(**data)


@dataclass(frozen=True)
class MediaInfo:
    path: str
    container: str
    duration_s: float
    has_video: bool
    has_audio: bool
    video_codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_frames: int = 0
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    size_bytes: int = 0


@dataclass(frozen=True)
class ProcessResult:
    pid: int
    argv: tuple[str, ...]
    started_at: float
    ended_at: float
    returncode: int | None
    stderr_tail: str
    killed_by_watchdog: bool = False
    kill_reason: str = ""
    survivors: tuple[int, ...] = ()

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at


@dataclass(frozen=True)
class RenderResult:
    status: JobStatus                   # SUCCESS or PARTIAL
    output_file: str
    output_sha256: str
    duration_s: float
    skipped_images: int = 0
    warnings: tuple[str, ...] = ()

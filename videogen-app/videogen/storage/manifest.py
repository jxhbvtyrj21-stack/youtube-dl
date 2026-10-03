"""Per-job ``manifest.json`` (ARCHITECTURE.md §20).

The manifest mirrors the job's state on disk next to its workspace, so a job
can be diagnosed — and the state DB rebuilt — even if ``state.db`` is lost.
It is always written atomically.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from videogen import __version__
from videogen.core.models import ErrorInfo, ImageItem, JobConfig, JobStatus, Stage
from videogen.storage.atomic import atomic_write_text

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class SegmentRecord:
    index: int
    frames: int
    status: str = "PENDING"      # PENDING | DONE
    size: int = 0


@dataclass
class JobManifest:
    config: JobConfig
    created_at: str = field(default_factory=utc_now)
    software_version: str = __version__
    ffmpeg_version: str = ""
    status: JobStatus = JobStatus.QUEUED
    stage: Stage = Stage.NONE
    attempts: int = 0
    start_time: str | None = None
    end_time: str | None = None
    images: list[ImageItem] = field(default_factory=list)
    audio: dict[str, Any] = field(default_factory=dict)
    audio_sha256: str = ""
    timeline: dict[str, Any] = field(default_factory=dict)
    segments: list[SegmentRecord] = field(default_factory=list)
    expected_duration: float | None = None
    actual_duration: float | None = None
    error: ErrorInfo | None = None
    output_file: str | None = None
    output_sha256: str = ""
    archive_file: str | None = None
    warnings: list[str] = field(default_factory=list)
    settings_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def skipped_images(self) -> list[ImageItem]:
        from videogen.core.models import ImageStatus
        return [i for i in self.images if i.status == ImageStatus.INVALID]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "job_id": self.config.job_id,
            "batch_id": self.config.batch_id,
            "job_name": self.config.name,
            "created_at": self.created_at,
            "software_version": self.software_version,
            "ffmpeg_version": self.ffmpeg_version,
            "mode": self.config.mode.value,
            "orientation": self.config.orientation.value,
            "resolution": f"{self.config.width}x{self.config.height}",
            "fps": self.config.fps,
            "config": self.config.to_dict(),
            "status": self.status.value,
            "stage": self.stage.value,
            "attempts": self.attempts,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "input_files": [i.to_dict() for i in self.images],
            "audio_file": self.config.audio_file,
            "audio_sha256": self.audio_sha256,
            "audio": self.audio,
            "timeline": self.timeline,
            "segments": [dataclasses.asdict(s) for s in self.segments],
            "expected_duration": self.expected_duration,
            "actual_duration": self.actual_duration,
            "error": self.error.to_dict() if self.error else None,
            "output_file": self.output_file,
            "output_sha256": self.output_sha256,
            "archive_file": self.archive_file,
            "skipped_count": len(self.skipped_images),
            "warnings": self.warnings,
            "settings_snapshot": self.settings_snapshot,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "JobManifest":
        if d.get("schema") != SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema {d.get('schema')!r}")
        err = d.get("error")
        return cls(
            config=JobConfig.from_dict(d["config"]),
            created_at=d.get("created_at") or utc_now(),
            software_version=d.get("software_version", ""),
            ffmpeg_version=d.get("ffmpeg_version", ""),
            status=JobStatus(d.get("status", "QUEUED")),
            stage=Stage(d.get("stage", "NONE")),
            attempts=int(d.get("attempts", 0)),
            start_time=d.get("start_time"),
            end_time=d.get("end_time"),
            images=[ImageItem.from_dict(x) for x in d.get("input_files", [])],
            audio=dict(d.get("audio") or {}),
            audio_sha256=d.get("audio_sha256", ""),
            timeline=dict(d.get("timeline") or {}),
            segments=[SegmentRecord(**s) for s in d.get("segments", [])],
            expected_duration=d.get("expected_duration"),
            actual_duration=d.get("actual_duration"),
            error=ErrorInfo(**err) if err else None,
            output_file=d.get("output_file"),
            output_sha256=d.get("output_sha256", ""),
            archive_file=d.get("archive_file"),
            warnings=list(d.get("warnings", [])),
            settings_snapshot=dict(d.get("settings_snapshot") or {}),
        )


def write_manifest(path: Path, manifest: JobManifest) -> None:
    atomic_write_text(path, json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2))


def read_manifest(path: Path) -> JobManifest | None:
    """Return the manifest or None if missing/corrupt (logged, never raises)."""
    try:
        return JobManifest.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("manifest %s unreadable: %r", path, exc)
        return None

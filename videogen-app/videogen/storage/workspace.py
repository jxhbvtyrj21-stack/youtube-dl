"""Per-batch / per-job working directories (ARCHITECTURE.md §4.2).

Only short ASCII names are used below the workspace root, so FFmpeg
arguments and concat lists never contain Unicode, quotes or shell
metacharacters regardless of the user's file names.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from videogen.core.errors import InputError
from videogen.utils.paths import is_within

MARKER_NAME = ".videogen-workspace"
BATCH_PREFIX = "vg-"
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]


def job_dir_name(seq: int) -> str:
    return f"j{seq:04d}"


def _check_id(value: str) -> str:
    if not _ID_RE.match(value):
        raise ValueError(f"unsafe id {value!r}")
    return value


@dataclass(frozen=True)
class JobWorkspace:
    root: Path

    @property
    def norm_dir(self) -> Path:
        return self.root / "norm"

    @property
    def audio_dir(self) -> Path:
        return self.root / "audio"

    @property
    def seg_dir(self) -> Path:
        return self.root / "seg"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def gen_dir(self) -> Path:
        """MODE B: provider outputs (voice, generated images)."""
        return self.root / "gen"

    @property
    def manifest_path(self) -> Path:
        return self.root / "manifest.json"

    def create(self) -> "JobWorkspace":
        for d in (self.root, self.norm_dir, self.audio_dir, self.seg_dir, self.out_dir, self.gen_dir):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def normalized_image(self, index: int) -> Path:
        return self.norm_dir / f"i{index:05d}.jpg"

    def segment(self, index: int) -> Path:
        return self.seg_dir / f"s{index:05d}.mp4"

    def temp_output(self) -> Path:
        return self.out_dir / "render.tmp.mp4"


@dataclass(frozen=True)
class BatchWorkspace:
    root: Path
    batch_id: str

    def job(self, seq: int) -> JobWorkspace:
        return JobWorkspace(self.root / job_dir_name(seq))


class WorkspaceRoot:
    """The user-selected temporary workspace directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).resolve()

    def ensure(self) -> None:
        try:
            self.path.mkdir(parents=True, exist_ok=True)
            probe = self.path / f".probe-{uuid.uuid4().hex[:8]}"
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            raise InputError(
                f"Тимчасова робоча папка недоступна для запису: {self.path}",
                code="WORKSPACE_NOT_WRITABLE", detail=repr(exc)) from exc

    def batch(self, batch_id: str) -> BatchWorkspace:
        root = self.path / f"{BATCH_PREFIX}{_check_id(batch_id)}"
        root.mkdir(parents=True, exist_ok=True)
        marker = root / MARKER_NAME
        if not marker.exists():
            marker.write_text(batch_id, encoding="ascii")
        return BatchWorkspace(root, batch_id)

    def existing_batches(self) -> list[Path]:
        if not self.path.is_dir():
            return []
        out = []
        with os.scandir(self.path) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False) and entry.name.startswith(BATCH_PREFIX) \
                        and (Path(entry.path) / MARKER_NAME).is_file():
                    out.append(Path(entry.path))
        return sorted(out)


def is_managed_path(path: Path) -> bool:
    """True if ``path`` lies inside a batch directory carrying our marker.

    Cleanup refuses to delete anything for which this is False — a guard
    against deleting user data because of a wrong path.
    """
    p = Path(path)
    try:
        p = p.resolve()
    except OSError:
        return False
    for parent in (p, *p.parents):
        if parent.name.startswith(BATCH_PREFIX) and (parent / MARKER_NAME).is_file():
            return is_within(p, parent)
    return False

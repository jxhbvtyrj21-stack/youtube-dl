"""Find and verify ffmpeg / ffprobe.

Search order: explicit directory (settings / ``VIDEOGEN_FFMPEG_DIR``) ->
``ffmpeg/`` next to the frozen EXE -> ``PATH``. Each candidate is verified
by running ``-version`` with a timeout; a binary that hangs or fails is not
accepted.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from videogen.core.errors import FFmpegUnavailableError
from videogen.ffmpeg_ctl.runner import run_tool

_EXE = ".exe" if os.name == "nt" else ""
VERSION_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str
    version: str


def _candidates(explicit_dir: str | None) -> list[Path]:
    dirs: list[Path] = []
    for d in (explicit_dir, os.environ.get("VIDEOGEN_FFMPEG_DIR")):
        if d:
            dirs.append(Path(d))
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        dirs += [base / "ffmpeg", base]
    return dirs


def _version(exe: str) -> str:
    out = run_tool([exe, "-hide_banner", "-version"], timeout_s=VERSION_TIMEOUT_S)
    if not out.ok:
        raise FFmpegUnavailableError(f"FFmpeg не запускається: {exe}", detail=out.stderr[-2000:])
    first = out.stdout.decode("utf-8", "replace").splitlines()[:1]
    m = re.search(r"version\s+(\S+)", first[0]) if first else None
    return m.group(1) if m else "unknown"


def locate(explicit_dir: str | None = None) -> FFmpegTools:
    pairs: list[tuple[str, str]] = []
    for d in _candidates(explicit_dir):
        f, p = d / f"ffmpeg{_EXE}", d / f"ffprobe{_EXE}"
        if f.is_file() and p.is_file():
            pairs.append((str(f), str(p)))
    wf, wp = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if wf and wp:
        pairs.append((wf, wp))
    errors: list[str] = []
    for ffmpeg, ffprobe in pairs:
        try:
            version = _version(ffmpeg)
            _version(ffprobe)
            return FFmpegTools(ffmpeg, ffprobe, version)
        except (FFmpegUnavailableError, OSError) as exc:
            errors.append(f"{ffmpeg}: {exc}")
    raise FFmpegUnavailableError(
        "FFmpeg не знайдено. Перевстановіть програму або вкажіть папку з ffmpeg у налаштуваннях.",
        detail="; ".join(errors) or "no candidates")

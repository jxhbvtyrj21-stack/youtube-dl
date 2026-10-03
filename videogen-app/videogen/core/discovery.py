"""Find jobs in the input folder (ARCHITECTURE.md §4.1, OPEN_QUESTIONS Q1).

One folder = one video. Sub-folders of the input folder are separate jobs;
if there are none, the input folder itself is one job. Files are grouped by
their *content* (image signature / audio container), not just extension.

Discovery only lists directories and reads a few header bytes — it never
decodes media, so it is fast even for thousands of files.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from videogen.core.models import Mode
from videogen.media.audio_processor import AUDIO_EXTENSIONS
from videogen.media.image_validator import IMAGE_EXTENSIONS, sniff_format
from videogen.utils.paths import natural_sort_key

IGNORED_NAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
SCRIPT_NAMES = ("script.txt",)
PROMPT_NAMES = ("prompts.txt",)
MAX_FILES_PER_JOB = 20_000

_AUDIO_SIGNATURES = (b"ID3", b"RIFF", b"fLaC", b"OggS", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xf1",
                     b"\xff\xf9", b"FORM", b"0&\xb2u")


@dataclass
class DiscoveredJob:
    name: str
    folder: Path
    mode: Mode
    images: list[Path] = field(default_factory=list)
    audio: Path | None = None
    script: Path | None = None
    prompts: Path | None = None
    problems: list[str] = field(default_factory=list)       # user-facing (Ukrainian)
    notes: list[str] = field(default_factory=list)
    ignored: list[Path] = field(default_factory=list)
    file_count: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems


def _head(p: Path, n: int = 16) -> bytes:
    try:
        with open(p, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def _is_audio(p: Path, head: bytes) -> bool:
    if p.suffix.lower() not in AUDIO_EXTENSIONS:
        return False
    if head[4:8] == b"ftyp":                       # m4a/aac in MP4 container
        return True
    return head.startswith(_AUDIO_SIGNATURES) or (len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)


def _files(folder: Path) -> list[Path]:
    out: list[Path] = []
    with os.scandir(folder) as it:
        for e in it:
            name = e.name
            if name.startswith((".", "~$")) or name.lower() in IGNORED_NAMES:
                continue
            if e.is_file(follow_symlinks=True):
                out.append(Path(e.path))
            if len(out) > MAX_FILES_PER_JOB:
                break
    return sorted(out, key=lambda p: natural_sort_key(p.name))


def classify_folder(folder: Path, mode: Mode) -> DiscoveredJob:
    job = DiscoveredJob(name=folder.name, folder=folder, mode=mode)
    try:
        files = _files(folder)
    except OSError as exc:
        job.problems.append(f"Не вдалося прочитати папку: {exc.strerror or exc}")
        return job
    job.file_count = len(files)
    if len(files) > MAX_FILES_PER_JOB:
        job.problems.append(f"У папці понад {MAX_FILES_PER_JOB} файлів.")
        return job

    audios: list[Path] = []
    for f in files:
        low = f.name.lower()
        if mode is Mode.SCRIPT_PROMPTS and low in SCRIPT_NAMES:
            job.script = f
            continue
        if mode is Mode.SCRIPT_PROMPTS and low in PROMPT_NAMES:
            job.prompts = f
            continue
        head = _head(f)
        fmt = sniff_format(head)
        if fmt is not None or f.suffix.lower() in IMAGE_EXTENSIONS:
            # images with a broken/unknown signature are still listed: the
            # validator reports them as problematic instead of hiding them
            if fmt in (None, "JPEG", "PNG", "WEBP", "BMP", "TIFF", "HEIF", "GIF"):
                job.images.append(f)
                continue
        if _is_audio(f, head) or f.suffix.lower() in AUDIO_EXTENSIONS:
            audios.append(f)
            continue
        job.ignored.append(f)

    if job.ignored:
        job.notes.append(f"Пропущено файлів іншого типу: {len(job.ignored)}.")
    if mode is Mode.AUDIO_IMAGES:
        if not job.images:
            job.problems.append("У папці немає зображень.")
        if not audios:
            job.problems.append("У папці немає аудіофайлу.")
        elif len(audios) > 1:
            names = ", ".join(a.name for a in audios[:5])
            job.problems.append(f"У папці кілька аудіофайлів ({names}); залиште один.")
        else:
            job.audio = audios[0]
    else:
        if job.script is None:
            job.problems.append("У папці немає файлу script.txt.")
        if job.prompts is None:
            job.problems.append("У папці немає файлу prompts.txt.")
    return job


def discover(input_dir: Path, mode: Mode) -> list[DiscoveredJob]:
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        return []
    subdirs: list[Path] = []
    with os.scandir(input_dir) as it:
        for e in it:
            if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "_")):
                subdirs.append(Path(e.path))
    if not subdirs:
        job = classify_folder(input_dir, mode)
        return [job] if job.file_count > 0 or job.problems and "прочитати" in job.problems[0] else []
    jobs = [classify_folder(d, mode) for d in sorted(subdirs, key=lambda p: natural_sort_key(p.name))]
    # sub-folders without any files (e.g. an empty "archive" folder) are not jobs
    return [j for j in jobs if j.file_count > 0]

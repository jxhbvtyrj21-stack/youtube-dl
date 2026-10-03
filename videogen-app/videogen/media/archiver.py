"""Streaming job archive (ARCHITECTURE.md §12).

* Files are streamed into the ZIP one by one (``ZipFile.write`` copies in
  blocks) — the archive is never built in RAM.
* Already-compressed media are STORED (no CPU wasted, no size gain).
* Written to ``.part``; verified (CRC of every entry, entry count, sizes);
  then atomically renamed.
* Temporary render files are never archived — only what the caller lists.
"""

from __future__ import annotations

import logging
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import ArchiveError, JobCancelledError
from videogen.storage.atomic import replace_with_retry

log = logging.getLogger(__name__)

STORED_EXT = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp3", ".m4a", ".aac", ".ogg", ".opus",
                        ".mp4", ".mov", ".zip", ".flac"})


@dataclass(frozen=True)
class ArchiveEntry:
    source: Path
    arcname: str


@dataclass
class ArchiveResult:
    path: str | None
    entries: int = 0
    bytes_in: int = 0
    skipped_reason: str = ""
    warnings: list[str] = field(default_factory=list)


def estimate_size(entries: list[ArchiveEntry]) -> int:
    total = 0
    for e in entries:
        try:
            total += e.source.stat().st_size + 200 + len(e.arcname.encode())
        except OSError:
            continue
    return total


def create_archive(dest: Path, entries: list[ArchiveEntry], *, max_size_bytes: int,
                   token: CancellationToken | None = None) -> ArchiveResult:
    if not entries:
        raise ArchiveError("Немає файлів для архівування.", code="ARCHIVE_EMPTY")
    est = estimate_size(entries)
    if est > max_size_bytes:
        msg = (f"Архів не створено: очікуваний розмір {est / 1048576:.0f} МБ перевищує "
               f"ліміт {max_size_bytes / 1048576:.0f} МБ.")
        log.warning(msg)
        return ArchiveResult(None, skipped_reason=msg)
    names = [e.arcname for e in entries]
    if len(set(names)) != len(names):
        raise ArchiveError("Дублікати імен в архіві.", code="ARCHIVE_DUPLICATE")

    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name("." + dest.name + ".part")
    result = ArchiveResult(None)
    try:
        with zipfile.ZipFile(part, "w", allowZip64=True) as zf:
            for e in entries:
                if token is not None and token.cancelled:
                    raise JobCancelledError()
                if not e.source.is_file():
                    raise ArchiveError(f"Файл для архіву зник: {e.source.name}", code="ARCHIVE_SOURCE_MISSING")
                compress = zipfile.ZIP_STORED if e.source.suffix.lower() in STORED_EXT else zipfile.ZIP_DEFLATED
                zf.write(e.source, e.arcname, compress_type=compress)
                result.entries += 1
                result.bytes_in += e.source.stat().st_size
        _verify(part, entries, result.bytes_in, token)
        replace_with_retry(part, dest)
        result.path = str(dest)
        return result
    except (ArchiveError, JobCancelledError):
        raise
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ArchiveError("Не вдалося створити архів.", detail=repr(exc)) from exc
    finally:
        if part.exists():
            try:
                os.unlink(part)
            except OSError:
                log.warning("could not remove partial archive %s", part)


def _verify(path: Path, entries: list[ArchiveEntry], bytes_in: int, token: CancellationToken | None) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise ArchiveError("Архів порожній.", code="ARCHIVE_EMPTY")
    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        if len(infos) != len(entries):
            raise ArchiveError("Кількість файлів в архіві не збігається.", code="ARCHIVE_VERIFY")
        if sum(i.file_size for i in infos) != bytes_in:
            raise ArchiveError("Розмір вмісту архіву не збігається.", code="ARCHIVE_VERIFY")
        for info in infos:      # streaming CRC check, one entry at a time
            if token is not None and token.cancelled:
                raise JobCancelledError()
            with zf.open(info) as fh:
                for _ in range(info.file_size // (1 << 20) + 2):
                    if not fh.read(1 << 20):
                        break

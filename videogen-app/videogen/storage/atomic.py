"""Crash-safe file writes and renames (ARCHITECTURE.md §10, §20).

``atomic_write_*``: write to a temp file in the same directory, flush,
fsync, then ``os.replace``. A crash at any point leaves either the old
complete file or the new complete file — never a truncated one.

On Windows ``os.replace`` can fail with ``PermissionError`` while an
antivirus scanner or Explorer briefly holds the target. That is retried a
bounded number of times.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

REPLACE_RETRY_DELAYS_S: tuple[float, ...] = (0.2, 0.5, 1.0, 2.0, 2.0)


def fsync_dir(directory: Path) -> None:
    """Persist a rename on POSIX. No-op (not supported) on Windows."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass  # invariant-ok: some filesystems do not support directory fsync
    finally:
        os.close(fd)


def replace_with_retry(src: Path, dst: Path,
                       delays: tuple[float, ...] = REPLACE_RETRY_DELAYS_S) -> None:
    """``os.replace`` with bounded retries on transient Windows locks."""
    last_exc: OSError | None = None
    for attempt in range(len(delays) + 1):
        try:
            os.replace(src, dst)
            fsync_dir(Path(dst).parent)
            return
        except PermissionError as exc:
            last_exc = exc
            if attempt < len(delays):
                time.sleep(delays[attempt])
    assert last_exc is not None
    raise last_exc


def atomic_write_bytes(path: Path, data: bytes) -> None:
    from videogen.utils.paths import long_path
    path = Path(long_path(path))   # \\?\ prefix on Windows for paths beyond MAX_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # Short fixed prefix: a temp name derived from the target could exceed the
    # file-name length limit even when the target itself fits.
    fd, tmp_name = tempfile.mkstemp(prefix=".vg-", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass  # invariant-ok: best effort removal of our own temp file
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))

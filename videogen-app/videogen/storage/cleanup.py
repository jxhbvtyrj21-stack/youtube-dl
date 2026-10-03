"""Bounded, never-raising cleanup (ARCHITECTURE.md §15).

* refuses to touch anything outside a marked VideoGen workspace;
* retries files locked by antivirus/Explorer a bounded number of times;
* stops at a deadline; a filesystem call that blocks forever (dead network
  share) cannot hang the caller because the walk runs in a daemon thread
  that is abandoned after the deadline.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from videogen.storage.workspace import is_managed_path
from videogen.utils.paths import long_path

log = logging.getLogger(__name__)

RETRY_DELAYS_S: tuple[float, ...] = (0.2, 0.5, 1.0)


@dataclass
class CleanupResult:
    path: str
    removed_files: int = 0
    removed_dirs: int = 0
    failed: list[str] = field(default_factory=list)
    timed_out: bool = False
    refused: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.timed_out and not self.refused


def _remove_with_retry(fn, p: str, delays: tuple[float, ...]) -> bool:
    for attempt in range(len(delays) + 1):
        try:
            fn(p)
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            try:
                os.chmod(p, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(p) else 0))
            except OSError:
                pass  # invariant-ok: chmod is a best-effort unlock before retry
            if attempt < len(delays):
                time.sleep(delays[attempt])
        except OSError:
            if attempt < len(delays):
                time.sleep(delays[attempt])
    return False


def _walk_remove(root: Path, deadline: float, result: CleanupResult,
                 delays: tuple[float, ...], cancel: threading.Event) -> None:
    top = long_path(root)
    for dirpath, dirnames, filenames in os.walk(top, topdown=False):
        for name in filenames:
            if cancel.is_set() or time.monotonic() > deadline:
                result.timed_out = True
                return
            p = os.path.join(dirpath, name)
            if _remove_with_retry(os.unlink, p, delays):
                result.removed_files += 1
            else:
                result.failed.append(p)
        for name in dirnames:
            p = os.path.join(dirpath, name)
            if os.path.islink(p):
                if _remove_with_retry(os.unlink, p, delays):
                    result.removed_files += 1
                else:
                    result.failed.append(p)
            elif _remove_with_retry(os.rmdir, p, delays):
                result.removed_dirs += 1
            elif os.path.exists(p):
                result.failed.append(p)
    if _remove_with_retry(os.rmdir, top, delays):
        result.removed_dirs += 1
    elif os.path.exists(top):
        result.failed.append(top)


def remove_tree(path: Path, *, deadline_s: float, retry_delays: tuple[float, ...] = RETRY_DELAYS_S,
                require_managed: bool = True) -> CleanupResult:
    """Delete ``path`` recursively within ``deadline_s`` seconds. Never raises."""
    result = CleanupResult(str(path))
    try:
        p = Path(path)
        if not p.exists():
            return result
        if require_managed and not is_managed_path(p):
            result.refused = True
            log.error("cleanup refused: %s is not inside a VideoGen workspace", p)
            return result
        if p.is_symlink() or p.is_file():
            if _remove_with_retry(os.unlink, long_path(p), retry_delays):
                result.removed_files += 1
            else:
                result.failed.append(str(p))
            return result

        deadline = time.monotonic() + deadline_s
        cancel = threading.Event()
        errors: list[BaseException] = []

        def _target() -> None:
            try:
                _walk_remove(p, deadline, result, retry_delays, cancel)
            except BaseException as exc:  # noqa: BLE001 - reported below
                errors.append(exc)

        t = threading.Thread(target=_target, name="cleanup", daemon=True)
        t.start()
        t.join(timeout=deadline_s + 1.0)
        if t.is_alive():
            cancel.set()
            result.timed_out = True
            log.error("cleanup of %s exceeded %.1fs; abandoned (filesystem blocked?)", p, deadline_s)
        if errors:
            result.failed.append(f"{p}: {errors[0]!r}")
        if result.failed:
            log.warning("cleanup of %s left %d item(s): %s", p, len(result.failed), result.failed[:5])
    except Exception as exc:  # noqa: BLE001 - cleanup must never raise
        result.failed.append(f"{path}: {exc!r}")
        log.exception("unexpected cleanup error for %s", path)
    return result


def cleanup_deadline(file_count: int, base_s: float, per_file_s: float) -> float:
    return base_s + file_count * per_file_s


def count_files(path: Path, limit: int = 1_000_000) -> int:
    n = 0
    for _dirpath, _dirnames, filenames in os.walk(long_path(path)):
        n += len(filenames)
        if n >= limit:
            break
    return n


def remove_stale_parts(output_dir: Path, known_good: set[str]) -> list[str]:
    """Delete our own leftover ``.<name>.part`` files in the output folder.

    Only files following our exact naming (leading dot, ``.part`` suffix)
    are touched; anything listed in ``known_good`` is kept.
    """
    removed: list[str] = []
    try:
        with os.scandir(output_dir) as it:
            entries = [e for e in it if e.is_file(follow_symlinks=False)]
    except OSError:
        return removed
    for e in entries:
        if e.name.startswith(".") and e.name.endswith(".part") and e.path not in known_good:
            if _remove_with_retry(os.unlink, e.path, RETRY_DELAYS_S):
                removed.append(e.path)
    return removed

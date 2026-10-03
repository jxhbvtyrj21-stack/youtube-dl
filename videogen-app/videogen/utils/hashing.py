"""Streaming file hashing (ARCHITECTURE.md §20). Never loads a file into RAM."""

from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1024 * 1024
PARTIAL_THRESHOLD = 500 * 1024 * 1024
PARTIAL_EDGE = 16 * 1024 * 1024


def sha256_file(path: Path, *, partial_threshold: int = PARTIAL_THRESHOLD) -> tuple[str, str]:
    """Return ``(hexdigest, mode)`` where mode is ``"full"`` or ``"partial"``.

    Files above ``partial_threshold`` are hashed as size + first 16 MB +
    last 16 MB to bound the time spent on huge inputs.
    """
    path = Path(path)
    size = path.stat().st_size
    h = hashlib.sha256()
    with path.open("rb") as fh:
        if size <= partial_threshold:
            for _ in range(size // CHUNK + 2):
                block = fh.read(CHUNK)
                if not block:
                    break
                h.update(block)
            return h.hexdigest(), "full"
        h.update(str(size).encode())
        _update_n(h, fh, PARTIAL_EDGE)
        fh.seek(max(0, size - PARTIAL_EDGE))
        _update_n(h, fh, PARTIAL_EDGE)
    return h.hexdigest(), "partial"


def _update_n(h: "hashlib._Hash", fh, n: int) -> None:  # type: ignore[name-defined]
    remaining = n
    for _ in range(n // CHUNK + 1):
        if remaining <= 0:
            break
        block = fh.read(min(CHUNK, remaining))
        if not block:
            break
        h.update(block)
        remaining -= len(block)

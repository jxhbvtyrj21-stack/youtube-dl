"""Path helpers safe for Windows: Unicode, reserved names, long paths
(ARCHITECTURE.md §22)."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_NATURAL_SPLIT = re.compile(r"(\d+)")
LONG_PATH_THRESHOLD = 240
MAX_NAME_LEN = 150


def safe_filename(name: str, *, max_len: int = MAX_NAME_LEN, fallback: str = "video") -> str:
    """Return a file name stem that is valid on Windows and POSIX.

    Keeps Unicode letters (Ukrainian names stay readable) and harmless
    punctuation such as ``& # % + ' ( )``; replaces characters that Windows
    forbids, strips trailing dots/spaces and avoids reserved device names.
    """
    name = unicodedata.normalize("NFC", name)
    name = _FORBIDDEN_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.rstrip(". ")
    if len(name) > max_len:
        name = name[:max_len].rstrip(". ")
    if not name:
        name = fallback
    if name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = "_" + name
    return name


def natural_sort_key(name: str) -> tuple[tuple[int, int | str], ...]:
    """``img2`` < ``img10``; case-insensitive; total order (no mixed-type compare)."""
    parts = _NATURAL_SPLIT.split(unicodedata.normalize("NFC", name).casefold())
    key: list[tuple[int, int | str]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        elif part:
            key.append((1, part))
    return tuple(key)


def long_path(path: Path | str) -> str:
    """Return a path string usable by Python I/O on Windows beyond MAX_PATH."""
    p = os.path.abspath(str(path))
    if os.name != "nt" or len(p) < LONG_PATH_THRESHOLD or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def is_within(path: Path | str, root: Path | str) -> bool:
    """True when ``path`` resolves to ``root`` itself or something below it."""
    try:
        rp = Path(os.path.realpath(path))
        rr = Path(os.path.realpath(root))
    except (OSError, ValueError):
        return False
    return rp == rr or rr in rp.parents


def unique_output_path(directory: Path, stem: str, suffix: str) -> Path:
    """``stem.mp4``, else ``stem (2).mp4``, ``stem (3).mp4`` … — never an
    existing file (also skips names whose ``.part`` exists)."""
    stem = safe_filename(stem)
    for n in range(1, 10_000):
        candidate = directory / (f"{stem}{suffix}" if n == 1 else f"{stem} ({n}){suffix}")
        part = candidate.with_name("." + candidate.name + ".part")
        if not candidate.exists() and not part.exists():
            return candidate
    raise FileExistsError(f"cannot find a free output name for {stem!r} in {directory}")

"""Parser for FFmpeg ``-progress pipe:1`` output (ARCHITECTURE.md §8.3).

FFmpeg writes blocks of ``key=value`` lines terminated by
``progress=continue`` or ``progress=end``. Malformed lines and ``N/A``
values are ignored — the parser never raises on input.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FFmpegProgress:
    frame: int = 0
    fps: float = 0.0
    out_time_s: float = 0.0
    speed: float = 0.0
    total_size: int = 0
    done: bool = False

    @property
    def marker(self) -> tuple[int, int]:
        """Changes iff real work advanced (frames or output time)."""
        return self.frame, int(self.out_time_s * 1000)


def _int(v: str) -> int | None:
    try:
        return int(v)
    except ValueError:
        return None


def _float(v: str) -> float | None:
    v = v.strip().rstrip("x")
    try:
        f = float(v)
    except ValueError:
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


class ProgressParser:
    def __init__(self) -> None:
        self._cur: dict[str, str] = {}
        self.last = FFmpegProgress()
        self.blocks = 0

    def feed_line(self, raw: str | bytes) -> FFmpegProgress | None:
        """Feed one line; returns a snapshot when a block completes."""
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if "=" not in line:
            return None
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key != "progress":
            if len(self._cur) < 64:
                self._cur[key] = value
            return None
        snap = self._snapshot(done=value == "end")
        self._cur = {}
        self.last = snap
        self.blocks += 1
        return snap

    def _snapshot(self, done: bool) -> FFmpegProgress:
        c, prev = self._cur, self.last
        frame = _int(c.get("frame", "")) if "frame" in c else None
        fps = _float(c.get("fps", "")) if "fps" in c else None
        out_us = _int(c.get("out_time_us", "")) if "out_time_us" in c else None
        if out_us is None and "out_time_ms" in c:      # despite the name, also µs
            out_us = _int(c["out_time_ms"])
        speed = _float(c.get("speed", "")) if "speed" in c else None
        size = _int(c.get("total_size", "")) if "total_size" in c else None
        out_s = out_us / 1e6 if out_us is not None and out_us >= 0 else None
        return FFmpegProgress(
            frame=frame if frame is not None and frame >= 0 else prev.frame,
            fps=fps if fps is not None else prev.fps,
            out_time_s=out_s if out_s is not None else prev.out_time_s,
            speed=speed if speed is not None else prev.speed,
            total_size=size if size is not None and size >= 0 else prev.total_size,
            done=done)

"""Derived timeouts (ARCHITECTURE.md §7.2).

Each timeout = base + expected work / minimal expected rate × safety factor.
Nothing here is a fixed "magic" number; all coefficients come from
:class:`~videogen.config.settings.TimeoutPolicy`.
"""

from __future__ import annotations

from dataclasses import dataclass

from videogen.config.settings import TimeoutPolicy


@dataclass(frozen=True)
class Limits:
    hard_s: float
    stall_s: float | None   # None: operation has no intermediate progress signal


def image_normalize(p: TimeoutPolicy, pixels: int) -> Limits:
    mpx = max(0, pixels) / 1e6
    return Limits(min(p.image_max_s, p.image_base_s + mpx * p.image_per_mpx_s), None)


def probe(p: TimeoutPolicy, size_bytes: int) -> Limits:
    return Limits(p.probe_base_s + max(0, size_bytes) / 1e9 * p.probe_per_gb_s, None)


def segment_render(p: TimeoutPolicy, frames: int, fps_min: float) -> Limits:
    fps_min = max(fps_min, p.fps_min_floor)
    expected = max(0, frames) / fps_min
    return Limits(p.segment_base_s + expected * p.safety_factor,
                  max(p.stall_min_s, expected / 2))


def audio_normalize(p: TimeoutPolicy, duration_s: float) -> Limits:
    expected = max(0.0, duration_s) / p.audio_min_speed
    return Limits(p.audio_base_s + expected * p.safety_factor, p.stall_default_s)


def mux(p: TimeoutPolicy, duration_s: float) -> Limits:
    expected = max(0.0, duration_s) / p.mux_min_speed
    return Limits(p.mux_base_s + expected * p.safety_factor, p.stall_default_s)


def verify(p: TimeoutPolicy, duration_s: float) -> Limits:
    expected = max(0.0, duration_s) / p.verify_min_speed
    return Limits(p.verify_base_s + expected * p.safety_factor, p.stall_default_s)


def archive(p: TimeoutPolicy, total_bytes: int) -> Limits:
    expected = max(0, total_bytes) / (p.archive_min_mb_s * 1024 * 1024)
    return Limits(p.archive_base_s + expected * p.safety_factor, p.stall_default_s)


def cleanup(p: TimeoutPolicy, files: int) -> float:
    return p.cleanup_base_s + max(0, files) * p.cleanup_per_file_s


def calibrated_fps_min(p: TimeoutPolicy, measured_fps: float | None) -> float:
    """§7.4: a quarter of the measured speed, never below the floor."""
    if not measured_fps or measured_fps <= 0:
        return p.fps_min_floor
    return max(p.fps_min_floor, measured_fps / 4)

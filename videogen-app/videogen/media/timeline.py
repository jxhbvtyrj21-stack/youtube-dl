"""Deterministic timeline: frames per image, crossfades, Ken Burns motion
(ARCHITECTURE.md §8.1.1, §9.3).

Pure functions of (audio duration, fps, image count, settings, seed). The
same inputs always give the same frame counts and motions — essential for
crash-resume (segments rendered before a crash must match the ones rendered
after it).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from videogen.config.settings import EffectsSettings
from videogen.core.errors import InputError

MOTIONS = ("zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up", "pan_down")


@dataclass(frozen=True)
class Motion:
    kind: str
    zoom_start: float
    zoom_end: float
    # focus point of the visible window, as a fraction of the canvas (0..1)
    x_start: float
    x_end: float
    y_start: float
    y_end: float


@dataclass(frozen=True)
class SegmentSpec:
    index: int                 # segment index == image index (0-based)
    image_index: int
    frames: int                # frames this segment contributes to the video
    transition_in: int         # first N frames crossfade from the previous image
    prev_image_index: int | None
    motion_frames: int         # frames over which this image's motion runs (incl. outgoing transition)
    motion: Motion


@dataclass(frozen=True)
class Timeline:
    fps: int
    audio_duration_s: float
    total_frames: int
    segments: tuple[SegmentSpec, ...]

    @property
    def video_duration_s(self) -> float:
        return self.total_frames / self.fps

    def to_dict(self) -> dict[str, object]:
        return {
            "fps": self.fps,
            "audio_duration_s": self.audio_duration_s,
            "total_frames": self.total_frames,
            "frames_per_image": [s.frames for s in self.segments],
            "transitions": [s.transition_in for s in self.segments],
            "motions": [s.motion.kind for s in self.segments],
        }


def total_frames_for(duration_s: float, fps: int) -> int:
    """ceil(D * fps): the video is never shorter than the audio. A tiny
    epsilon prevents float noise (e.g. 10.000000001 s) adding a frame."""
    return max(1, math.ceil(duration_s * fps - 1e-6))


def split_frames(total: int, n: int) -> list[int]:
    base, rem = divmod(total, n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def choose_motion(seed: str, index: int, effects: EffectsSettings) -> Motion:
    if not effects.ken_burns:
        return Motion("static", 1.0, 1.0, 0.5, 0.5, 0.5, 0.5)
    h = hashlib.sha256(f"{seed}:{index}".encode()).digest()
    kind = MOTIONS[h[0] % len(MOTIONS)]
    z = effects.max_zoom
    zp = 1.0 + (z - 1.0) * 0.75          # pans use a slightly smaller zoom
    # pan travel limited to the margin the zoom creates
    travel = (1.0 - 1.0 / zp) / 2 * 0.9
    c = 0.5
    if kind == "zoom_in":
        return Motion(kind, 1.0, z, c, c, c, c)
    if kind == "zoom_out":
        return Motion(kind, z, 1.0, c, c, c, c)
    if kind == "pan_left":
        return Motion(kind, zp, zp, c + travel, c - travel, c, c)
    if kind == "pan_right":
        return Motion(kind, zp, zp, c - travel, c + travel, c, c)
    if kind == "pan_up":
        return Motion(kind, zp, zp, c, c, c + travel, c - travel)
    return Motion(kind, zp, zp, c, c, c - travel, c + travel)


def build_timeline(audio_duration_s: float, fps: int, n_images: int, effects: EffectsSettings,
                   *, min_seconds_per_image: float, seed: str) -> Timeline:
    if n_images <= 0:
        raise InputError("Немає жодного придатного зображення для відео.", code="NO_VALID_IMAGES")
    if not (audio_duration_s > 0) or not math.isfinite(audio_duration_s):
        raise InputError("Тривалість аудіо некоректна.", code="INVALID_AUDIO")
    total = total_frames_for(audio_duration_s, fps)
    frames = split_frames(total, n_images)
    min_frames = math.ceil(min_seconds_per_image * fps)
    if min(frames) < min_frames:
        per = audio_duration_s / n_images
        raise InputError(
            f"Забагато зображень для тривалості аудіо: {n_images} зображень на "
            f"{audio_duration_s:.1f} с ({per:.2f} с на зображення, мінімум "
            f"{min_seconds_per_image:.1f} с). Зменште кількість зображень або подовжіть аудіо.",
            code="TOO_MANY_IMAGES")

    want = round(effects.transition_s * fps) if effects.transitions else 0
    trans = [0] * n_images
    for i in range(1, n_images):
        cap = math.floor(effects.transition_max_fraction * min(frames[i - 1], frames[i]))
        trans[i] = max(0, min(want, cap))

    segs = []
    for i in range(n_images):
        outgoing = trans[i + 1] if i + 1 < n_images else 0
        segs.append(SegmentSpec(
            index=i, image_index=i, frames=frames[i], transition_in=trans[i],
            prev_image_index=i - 1 if trans[i] > 0 else None,
            motion_frames=frames[i] + outgoing,
            motion=choose_motion(seed, i, effects)))
    tl = Timeline(fps, audio_duration_s, total, tuple(segs))
    assert sum(s.frames for s in tl.segments) == total
    return tl

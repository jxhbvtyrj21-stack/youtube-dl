from __future__ import annotations

import dataclasses
import json
import math
import os
from pathlib import Path

from videogen.config.settings import (
    ImageSettings, ResourceLimits, Settings, TimeoutPolicy, VideoSettings, settings_from_dict,
)
from videogen.media.media_validator import probe_media
from tests.production import monitor
from tests.production.conftest import FULL
from tests.production.media_sets import FFMPEG

FFPROBE = FFMPEG.replace("ffmpeg", "ffprobe") if FFMPEG else "ffprobe"


def prod_settings(**over) -> Settings:
    """Real production defaults (1080p30, medium) on full scale; small and
    fast for quick local runs."""
    s = Settings(resources=ResourceLimits(disk_reserve_mb=256))
    if not FULL:
        s = dataclasses.replace(s, video=VideoSettings(horizontal_width=640, horizontal_height=360,
                                                       vertical_width=360, vertical_height=640,
                                                       preset="veryfast"))
    return dataclasses.replace(s, **over)


def expected_frames(duration_s: float, fps: int = 30) -> int:
    return max(1, math.ceil(duration_s * fps - 1e-6))


def frames(path: Path) -> int:
    import shutil
    probe = shutil.which("ffprobe") or FFPROBE
    return probe_media(probe, Path(path), TimeoutPolicy()).video_frames


def ws_files(ws: Path) -> list[str]:
    return [str(p) for p in ws.rglob("*") if p.is_file() and p.name != ".videogen-workspace"] if ws.exists() else []


def assert_no_media_processes(baseline: dict) -> None:
    left = [p.pid for p in monitor.media_processes() if p.pid not in baseline["media_procs"]]
    assert not left, f"ffmpeg/ffprobe processes left: {left}"


def env_settings_json(s: Settings) -> str:
    return json.dumps(s.to_dict())

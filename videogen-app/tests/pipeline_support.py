"""Helpers for end-to-end pipeline tests (real FFmpeg, small resolution)."""

from __future__ import annotations

import dataclasses
import shutil
import threading
from pathlib import Path

from videogen.config.settings import (
    ArchiveSettings, EffectsSettings, ImageSettings, ResourceLimits, RetryPolicy, Settings, TimeoutPolicy,
    VideoSettings,
)
from videogen.core import events as ev
from tests.fixtures import factory as F


def small_settings(**sections) -> Settings:
    s = Settings(
        video=VideoSettings(horizontal_width=426, horizontal_height=240, vertical_width=240,
                            vertical_height=426, preset="ultrafast", fps=24),
        images=ImageSettings(min_seconds_per_image=0.5),
        effects=EffectsSettings(transition_s=0.25),
        timeouts=TimeoutPolicy(watchdog_poll_s=0.1, graceful_wait_s=0.5, terminate_wait_s=0.5, kill_wait_s=3),
        retry=RetryPolicy(transient_backoff_s=(0.0,)),
        archive=ArchiveSettings(enabled=True),
        resources=ResourceLimits(ram_available_min_mb=128, disk_reserve_mb=64),
    )
    return dataclasses.replace(s, **sections)


def make_job_folder(root: Path, name: str, *, n_images: int = 4, audio_s: float = 3.0, bad: tuple[int, ...] = (),
                    audio: str = "mp3", landscape: bool = True) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    makers = [F.jpg, F.png, F.webp]
    for i in range(n_images):
        p = d / f"img_{i + 1:03d}.{['jpg', 'png', 'webp'][i % 3]}"
        if i in bad:
            F.corrupted_png(p.with_suffix(".png"))
        else:
            makers[i % 3](p, size=(640, 360) if landscape else (360, 640))
    if audio == "mp3":
        F.tone(d / "voice.mp3", audio_s)
    elif audio == "none":
        pass
    elif audio == "bad":
        (d / "voice.mp3").write_bytes(b"ID3" + b"\x00" * 3000)
    return d


class Events:
    def __init__(self):
        self.items: list[ev.Event] = []
        self.lock = threading.Lock()

    def __call__(self, e):
        with self.lock:
            self.items.append(e)

    def of(self, cls):
        with self.lock:
            return [e for e in self.items if isinstance(e, cls)]


def start_cmd(inp: Path, out: Path, ws: Path, orientation: str = "16:9", mode: str = "A") -> ev.StartBatch:
    return ev.StartBatch(mode=mode, orientation=orientation, input_dir=str(inp), output_dir=str(out),
                         workspace_dir=str(ws))


def ffmpeg_wrapper(tmp: Path, behaviour: str) -> tuple[str, str]:
    """A POSIX wrapper around the real ffmpeg that misbehaves on segment
    renders (argv containing -filter_complex): 'hang' or 'crash'."""
    real = shutil.which("ffmpeg")
    py = tmp / "wrap.py"
    py.write_text(
        "import os, sys, time\n"
        f"real = {real!r}\n"
        "if '-filter_complex' in sys.argv:\n"
        f"    if {behaviour!r} == 'hang':\n"
        "        time.sleep(3600)\n"
        "    sys.stderr.write('simulated encoder crash\\n'); sys.exit(1)\n"
        "os.execv(real, [real] + sys.argv[1:])\n")
    sh = tmp / "ffmpeg"
    import sys
    sh.write_text(f"#!/bin/sh\nexec {sys.executable} {py} \"$@\"\n")
    sh.chmod(0o755)
    return str(sh), shutil.which("ffprobe") or ""

"""Offline providers for tests and dry runs (no network, deterministic)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import FFmpegUnavailableError
from videogen.providers.base import GeneratedAsset


class FakeTTS:
    name = "fake-tts"

    def __init__(self, seconds_per_char: float = 0.02, min_s: float = 2.0) -> None:
        self.seconds_per_char = seconds_per_char
        self.min_s = min_s
        self.calls = 0

    def synthesize(self, text: str, out_path: Path, *, timeout_s: float,
                   token: CancellationToken) -> GeneratedAsset:
        token.raise_if_cancelled()
        self.calls += 1
        ff = shutil.which("ffmpeg")
        if not ff:
            raise FFmpegUnavailableError("FFmpeg не знайдено.")
        dur = max(self.min_s, len(text) * self.seconds_per_char)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=330:d={dur:.3f}",
                        "-ac", "1", str(out_path)], check=True, timeout=timeout_s,
                       stdin=subprocess.DEVNULL)
        return GeneratedAsset(str(out_path), self.name)


class FakeImages:
    name = "fake-images"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, out_path: Path, *, width: int, height: int, timeout_s: float,
                 token: CancellationToken) -> GeneratedAsset:
        token.raise_if_cancelled()
        self.calls += 1
        h = hashlib.sha256(prompt.encode()).digest()
        img = Image.new("RGB", (width, height), (h[0], h[1], h[2]))
        ImageDraw.Draw(img).ellipse((width // 4, height // 4, width * 3 // 4, height * 3 // 4),
                                    fill=(h[3], h[4], h[5]))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(out_path, "PNG")
        return GeneratedAsset(str(out_path), self.name)

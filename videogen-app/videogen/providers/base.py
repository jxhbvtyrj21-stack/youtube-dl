"""MODE B provider interfaces (ARCHITECTURE.md §21).

The pipeline depends only on these protocols. Concrete adapters
(ElevenLabs for voice, OpenAI for images) live in their own modules and are
registered in :data:`REGISTRY`; adding one never touches the pipeline.

Contract for every adapter:
  * a timeout on every network call (connect + read);
  * write the result to ``out_path`` (stream to disk, never hold big
    payloads in memory) and return metadata;
  * raise :class:`TransientError` for retryable failures (network, 5xx,
    rate limit) and :class:`InputError` for permanent ones (content policy,
    invalid key) — the JobManager applies the uniform retry budget;
  * check the cancellation token between requests.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from videogen.core.cancellation import CancellationToken


@dataclass(frozen=True)
class GeneratedAsset:
    path: str
    provider: str
    cached: bool = False


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, out_path: Path, *, timeout_s: float,
                   token: CancellationToken) -> GeneratedAsset: ...


class ImageGenProvider(Protocol):
    name: str

    def generate(self, prompt: str, out_path: Path, *, width: int, height: int, timeout_s: float,
                 token: CancellationToken) -> GeneratedAsset: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self.tts: dict[str, TTSProvider] = {}
        self.images: dict[str, ImageGenProvider] = {}

    def register_tts(self, p: TTSProvider) -> None:
        self.tts[p.name] = p

    def register_images(self, p: ImageGenProvider) -> None:
        self.images[p.name] = p


REGISTRY = ProviderRegistry()


class AssetCache:
    """Content-addressed cache so a retried job does not regenerate (and pay
    for) the same voice/image twice."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def key(self, kind: str, provider: str, payload: dict[str, object]) -> str:
        raw = json.dumps({"k": kind, "p": provider, **payload}, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, key: str, suffix: str, dest: Path) -> bool:
        src = self.root / key[:2] / f"{key}{suffix}"
        if src.is_file() and src.stat().st_size > 0:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            return True
        return False

    def put(self, key: str, suffix: str, src: Path) -> None:
        dst = self.root / key[:2] / f"{key}{suffix}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)


def split_script(text: str) -> list[str]:
    """Scenes = paragraphs separated by blank lines."""
    parts = [p.strip() for p in text.replace("\r\n", "\n").split("\n\n")]
    return [p for p in parts if p]


def read_prompts(text: str) -> list[str]:
    return [line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip()]

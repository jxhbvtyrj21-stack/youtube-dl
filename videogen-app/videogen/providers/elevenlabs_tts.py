"""ElevenLabs text-to-speech adapter (ARCHITECTURE.md §21).

``POST {base}/v1/text-to-speech/{voice_id}?output_format=<fmt>`` with the
header ``xi-api-key`` and the JSON body ``{"text", "model_id"}``; the
response body is the audio (MP3 for the ``mp3_*`` formats).

A script longer than one request allows (the per-model character limit) is
split at paragraph, then sentence, then word boundaries; the MP3 parts are
joined frame-wise. Every part already received is kept in
``<out dir>/voice-parts/`` under a content hash until the whole voice is
assembled, so a retried job does not pay for the same part twice.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import InputError, TransientError, VideoGenError
from videogen.providers.base import GeneratedAsset
from videogen.providers.http import HttpPolicy, request

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
SERVICE = "ElevenLabs"
_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")


def split_text(text: str, max_chars: int) -> list[str]:
    """Pieces of at most ``max_chars`` characters, cut at the most natural
    boundary available; nothing is lost and the order is kept."""
    out: list[str] = []
    for para in [p.strip() for p in text.replace("\r\n", "\n").split("\n\n") if p.strip()]:
        units = [para] if len(para) <= max_chars else [s for s in _SENTENCE.split(para) if s]
        pieces: list[str] = []
        for u in units:
            while len(u) > max_chars:                 # a single over-long sentence: cut at a space
                cut = u.rfind(" ", 0, max_chars + 1)
                cut = cut if cut > max_chars // 2 else max_chars
                pieces.append(u[:cut].strip())
                u = u[cut:].strip()
            if u:
                pieces.append(u)
        cur = ""
        for p in pieces:
            if cur and len(cur) + 1 + len(p) > max_chars:
                out.append(cur)
                cur = p
            else:
                cur = f"{cur} {p}" if cur else p
        if cur:
            out.append(cur)
    # join short paragraphs into one request where they fit (fewer requests)
    merged: list[str] = []
    for piece in out:
        if merged and len(merged[-1]) + 2 + len(piece) <= max_chars:
            merged[-1] = f"{merged[-1]}\n\n{piece}"
        else:
            merged.append(piece)
    return merged


def _strip_id3(data: bytes) -> bytes:
    if len(data) >= 10 and data[:3] == b"ID3":
        size = (data[6] & 0x7F) << 21 | (data[7] & 0x7F) << 14 | (data[8] & 0x7F) << 7 | (data[9] & 0x7F)
        return data[10 + size:]
    return data


def looks_like_mp3(data: bytes) -> bool:
    body = _strip_id3(data)
    return len(body) >= 2 and body[0] == 0xFF and (body[1] & 0xE0) == 0xE0


def _classify(status: int, snippet: str) -> VideoGenError | None:
    low = snippet.lower()
    if "quota_exceeded" in low or ("insufficient" in low and "credit" in low):
        return InputError(f"{SERVICE}: вичерпано ліміт символів на рахунку.", code="PROVIDER_QUOTA",
                          detail=f"HTTP {status}: {snippet}")
    if "voice_not_found" in low or (status == 404 and "voice" in low):
        return InputError(f"{SERVICE}: голос не знайдено. Перевірте ідентифікатор голосу в налаштуваннях.",
                          code="PROVIDER_CONFIG", detail=f"HTTP {status}: {snippet}")
    if "max_character_limit" in low or "text_too_long" in low:
        return InputError(f"{SERVICE}: текст задовгий для одного запиту. Зменште providers.elevenlabs_max_chars.",
                          code="PROVIDER_CONFIG", detail=f"HTTP {status}: {snippet}")
    return None


class ElevenLabsTTS:
    name = "elevenlabs"

    def __init__(self, api_key: str, *, voice_id: str, model_id: str, output_format: str = "mp3_44100_128",
                 max_chars: int = 4500, policy: HttpPolicy | None = None,
                 base_url: str = DEFAULT_BASE_URL) -> None:
        if not _ID.match(voice_id or ""):
            raise InputError(f"{SERVICE}: некоректний ідентифікатор голосу.", code="PROVIDER_CONFIG")
        if not _ID.match(model_id or ""):
            raise InputError(f"{SERVICE}: некоректна назва моделі.", code="PROVIDER_CONFIG")
        if not output_format.startswith("mp3_"):
            raise InputError(f"{SERVICE}: підтримується лише формат MP3.", code="PROVIDER_CONFIG")
        self._key = api_key
        self.voice_id, self.model_id, self.output_format = voice_id, model_id, output_format
        self.max_chars = max_chars
        self.policy = policy or HttpPolicy()
        self.base_url = base_url.rstrip("/")
        # the cache key of the pipeline includes the voice and model: changing
        # them must not reuse a voice generated with other parameters
        self.name = f"elevenlabs:{voice_id}:{model_id}:{output_format}"

    def _part(self, text: str, dst: Path, token: CancellationToken) -> None:
        url = f"{self.base_url}/v1/text-to-speech/{self.voice_id}?output_format={self.output_format}"
        tmp = dst.with_name(dst.name + ".download")
        request("POST", url, service=SERVICE, token=token, policy=self.policy, classify=_classify,
                secrets=(self._key,), out_path=tmp,
                headers={"xi-api-key": self._key, "Content-Type": "application/json", "Accept": "audio/mpeg"},
                body=json.dumps({"text": text, "model_id": self.model_id}, ensure_ascii=False).encode("utf-8"))
        data = tmp.read_bytes()
        if not looks_like_mp3(data):
            tmp.unlink(missing_ok=True)
            raise TransientError(f"{SERVICE}: сервіс повернув не аудіо.", code="PROVIDER_BAD_RESPONSE",
                                 detail=f"{len(data)} bytes, starts with {data[:8]!r}")
        tmp.replace(dst)

    def synthesize(self, text: str, out_path: Path, *, timeout_s: float,
                   token: CancellationToken) -> GeneratedAsset:
        pieces = split_text(text, self.max_chars)
        if not pieces:
            raise InputError("Сценарій порожній.", code="SCRIPT_EMPTY")
        parts_dir = out_path.parent / "voice-parts"
        parts_dir.mkdir(parents=True, exist_ok=True)
        parts: list[Path] = []
        for piece in pieces:
            token.raise_if_cancelled()
            h = hashlib.sha256(json.dumps([self.name, piece], ensure_ascii=False).encode()).hexdigest()[:40]
            dst = parts_dir / f"{h}.mp3"
            if not (dst.is_file() and looks_like_mp3(dst.read_bytes()[:65536])):
                self._part(piece, dst, token)
            parts.append(dst)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as fh:
            for i, p in enumerate(parts):
                data = p.read_bytes()
                fh.write(data if i == 0 else _strip_id3(data))
        for p in parts:
            p.unlink(missing_ok=True)
        return GeneratedAsset(str(out_path), self.name)

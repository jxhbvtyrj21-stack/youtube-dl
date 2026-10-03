"""Audio inspection and normalisation (ARCHITECTURE.md §9.2).

The real duration is measured by *decoding* the whole file to PCM — metadata
duration is only used for timeouts and as a cross-check, because broken or
VBR files frequently report a wrong duration.
"""

from __future__ import annotations

import json
import logging
import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

from videogen.config.settings import AudioSettings, TimeoutPolicy
from videogen.core import timeouts
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import (
    FFmpegCrashError, InputError, JobCancelledError, OperationTimeoutError, TransientError,
)
from videogen.ffmpeg_ctl.runner import run_tool
from videogen.utils.hashing import sha256_file

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".aiff", ".aif"})
METADATA_MISMATCH_WARN = 0.02
SEVERE_LOSS = 0.5


@dataclass
class AudioInfo:
    source: str
    codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    metadata_duration_s: float | None = None
    decoded_duration_s: float = 0.0
    wav_path: str = ""
    sha256: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "codec": self.codec, "sample_rate": self.sample_rate, "channels": self.channels,
            "metadata_duration": self.metadata_duration_s, "decoded_duration": self.decoded_duration_s,
            "warnings": list(self.warnings),
        }


def _f(value: object) -> float | None:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return v if v > 0 and v == v and v != float("inf") else None


def probe_audio(ffprobe: str, path: Path, tp: TimeoutPolicy,
                token: CancellationToken | None = None) -> AudioInfo:
    """ffprobe the source. Raises InputError for missing/unusable files."""
    p = Path(path)
    if not p.exists():
        raise InputError(f"Аудіофайл не знайдено: {p.name}", code="AUDIO_MISSING")
    try:
        size = p.stat().st_size
    except OSError as exc:
        raise TransientError(f"Немає доступу до аудіофайлу: {p.name}", code="FILE_LOCKED") from exc
    if size == 0:
        raise InputError(f"Аудіофайл порожній (0 байт): {p.name}", code="INVALID_AUDIO")
    out = run_tool([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(p)],
                   timeout_s=timeouts.probe(tp, size).hard_s, token=token)
    if out.cancelled:
        raise JobCancelledError()
    if out.timed_out:
        raise OperationTimeoutError(f"Аналіз аудіо триває надто довго: {p.name}", code="PROBE_TIMEOUT")
    try:
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if out.result.returncode != 0 or not streams:
        raise InputError(
            f"Аудіофайл пошкоджений або не містить звуку: {p.name}", code="INVALID_AUDIO",
            detail=out.stderr[-2000:])
    s0 = streams[0]
    info = AudioInfo(
        source=str(p), codec=str(s0.get("codec_name", "")),
        sample_rate=int(_f(s0.get("sample_rate")) or 0), channels=int(s0.get("channels") or 0),
        metadata_duration_s=_f(s0.get("duration")) or _f(data.get("format", {}).get("duration")))
    if len(streams) > 1:
        info.warnings.append(f"у файлі {len(streams)} аудіодоріжки; використано першу")
    return info


def wav_duration(path: Path) -> tuple[float, int, int]:
    """Duration from the actual PCM data size (supports RIFF and RF64).
    Returns (seconds, sample_rate, channels)."""
    with open(path, "rb") as fh:
        hdr = fh.read(12)
        if len(hdr) < 12 or hdr[8:12] != b"WAVE" or hdr[:4] not in (b"RIFF", b"RF64"):
            raise ValueError("not a WAV file")
        file_size = os.fstat(fh.fileno()).st_size
        rate = channels = bits = 0
        ds64_data: int | None = None
        for _ in range(64):   # a WAV has only a handful of chunks
            ch = fh.read(8)
            if len(ch) < 8:
                break
            cid, csize = ch[:4], struct.unpack("<I", ch[4:])[0]
            if cid == b"ds64":
                body = fh.read(csize)
                ds64_data = struct.unpack("<Q", body[8:16])[0]
                continue
            if cid == b"fmt ":
                body = fh.read(csize)
                _fmt, channels, rate = struct.unpack("<HHI", body[:8])
                bits = struct.unpack("<H", body[14:16])[0]
                if csize % 2:
                    fh.read(1)
                continue
            if cid == b"data":
                start = fh.tell()
                if ds64_data is not None and csize == 0xFFFFFFFF:
                    data_size = ds64_data
                else:
                    # header may be stale if the writer was killed: trust the file size
                    data_size = min(csize, file_size - start) if csize != 0xFFFFFFFF else file_size - start
                if not (rate and channels and bits):
                    raise ValueError("fmt chunk missing")
                frame_bytes = channels * bits // 8
                return data_size / frame_bytes / rate, rate, channels
            fh.seek(csize + (csize % 2), 1)
    raise ValueError("data chunk not found")


def normalize_audio(ffmpeg: str, info: AudioInfo, out_wav: Path, cfg: AudioSettings, tp: TimeoutPolicy,
                    token: CancellationToken | None = None) -> AudioInfo:
    """Decode the full stream to PCM WAV (fixed rate/channels) and measure it."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_wav.with_name(out_wav.name + ".tmp.wav")
    est = info.metadata_duration_s or 3600.0
    argv = [ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-y", "-i", info.source,
            "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-ac", str(cfg.channels), "-ar", str(cfg.sample_rate),
            "-c:a", "pcm_s16le", "-rf64", "auto", "-f", "wav", str(tmp)]
    out = run_tool(argv, timeout_s=timeouts.audio_normalize(tp, est).hard_s, token=token)
    try:
        if out.cancelled:
            raise JobCancelledError()
        if out.timed_out:
            raise OperationTimeoutError("Декодування аудіо триває надто довго.", code="AUDIO_TIMEOUT")
        if out.result.returncode != 0 or not tmp.exists():
            if _looks_corrupt(out.stderr):
                raise InputError(f"Аудіофайл пошкоджений: {Path(info.source).name}",
                                 code="INVALID_AUDIO", detail=out.stderr[-2000:])
            raise FFmpegCrashError("FFmpeg аварійно завершився під час обробки аудіо.",
                                   detail=out.stderr[-2000:])
        try:
            dur, rate, ch = wav_duration(tmp)
        except (OSError, ValueError, struct.error) as exc:
            raise FFmpegCrashError("Не вдалося прочитати результат декодування аудіо.",
                                   detail=repr(exc)) from exc
        if dur < cfg.min_audio_s:
            raise InputError(
                f"Аудіо надто коротке або не містить звуку ({dur:.2f} с): {Path(info.source).name}",
                code="INVALID_AUDIO", detail=out.stderr[-2000:])
        if out.stderr.strip():
            md = info.metadata_duration_s
            if md and dur < md * SEVERE_LOSS:
                raise InputError(
                    f"Аудіофайл пошкоджений: вдалося прочитати лише {dur:.1f} с з {md:.1f} с "
                    f"({Path(info.source).name}).", code="INVALID_AUDIO", detail=out.stderr[-2000:])
            info.warnings.append("під час декодування аудіо були попередження; файл може бути частково пошкоджений")
        md = info.metadata_duration_s
        if md and abs(dur - md) / md > METADATA_MISMATCH_WARN:
            info.warnings.append(
                f"тривалість у метаданих ({md:.2f} с) не збігається з фактичною ({dur:.2f} с); "
                f"використано фактичну")
        os.replace(tmp, out_wav)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                log.warning("could not remove %s", tmp)
    info.decoded_duration_s = dur
    info.wav_path = str(out_wav)
    info.sample_rate, info.channels = rate, ch
    try:
        info.sha256, _ = sha256_file(Path(info.source))
    except OSError:
        info.warnings.append("не вдалося обчислити хеш аудіо")
    return info


def _looks_corrupt(stderr: str) -> bool:
    s = stderr.lower()
    return any(k in s for k in ("invalid data", "could not find codec", "header missing",
                                "error while decoding", "moov atom not found", "end of file",
                                "no such file", "invalid argument", "does not contain any stream",
                                "output file #0 does not contain"))

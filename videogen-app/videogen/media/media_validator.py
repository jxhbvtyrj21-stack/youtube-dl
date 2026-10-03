"""Post-render verification (ARCHITECTURE.md §10).

FFmpeg exiting with code 0 is *not* success. An output is accepted only if
ffprobe sees the expected streams, frame count and durations, and a full
decode produces no errors.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from videogen.config.settings import TimeoutPolicy
from videogen.core import timeouts
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import JobCancelledError, OperationTimeoutError, VerificationError
from videogen.core.models import MediaInfo
from videogen.ffmpeg_ctl.runner import run_tool

MIN_OUTPUT_BYTES = 10 * 1024
MIN_BITRATE_BPS = 10_000
AAC_SLACK_S = 0.05


@dataclass(frozen=True)
class ExpectedOutput:
    width: int
    height: int
    fps: int
    total_frames: int
    has_audio: bool
    sample_rate: int = 48000
    channels: int = 2
    video_codec: str = "h264"
    audio_codec: str = "aac"

    @property
    def duration_s(self) -> float:
        return self.total_frames / self.fps


def _num(v: object, default: float = 0.0) -> float:
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _rate(v: object) -> float:
    s = str(v or "0/1")
    if "/" in s:
        a, b = s.split("/", 1)
        return _num(a) / (_num(b) or 1.0)
    return _num(s)


def probe_media(ffprobe: str, path: Path, tp: TimeoutPolicy, token: CancellationToken | None = None,
                count_packets: bool = True) -> MediaInfo:
    size = path.stat().st_size
    argv = [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json"]
    if count_packets:
        argv.append("-count_packets")
    argv.append(str(path))
    out = run_tool(argv, timeout_s=timeouts.probe(tp, size).hard_s + 30, token=token)
    if out.cancelled:
        raise JobCancelledError()
    if out.timed_out:
        raise OperationTimeoutError("Перевірка відео триває надто довго.", code="VERIFY_TIMEOUT")
    try:
        data = json.loads(out.stdout.decode("utf-8", "replace") or "{}")
    except ValueError:
        data = {}
    if out.result.returncode != 0 or "format" not in data:
        raise VerificationError("Створений файл не є коректним відео.", code="BAD_CONTAINER",
                                detail=out.stderr[-2000:])
    fmt = data["format"]
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    return MediaInfo(
        path=str(path), container=str(fmt.get("format_name", "")),
        duration_s=_num(fmt.get("duration")), has_video=v is not None, has_audio=a is not None,
        video_codec=str(v.get("codec_name", "")) if v else "",
        width=int(_num(v.get("width"))) if v else 0, height=int(_num(v.get("height"))) if v else 0,
        fps=_rate(v.get("avg_frame_rate")) if v else 0.0,
        video_frames=int(_num(v.get("nb_read_packets") or v.get("nb_frames"))) if v else 0,
        audio_codec=str(a.get("codec_name", "")) if a else "",
        sample_rate=int(_num(a.get("sample_rate"))) if a else 0,
        channels=int(_num(a.get("channels"))) if a else 0,
        size_bytes=size)


def stream_durations(ffprobe: str, path: Path, tp: TimeoutPolicy,
                     token: CancellationToken | None = None) -> dict[str, float]:
    out = run_tool([ffprobe, "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
                    str(path)], timeout_s=timeouts.probe(tp, path.stat().st_size).hard_s, token=token)
    if out.cancelled:
        raise JobCancelledError()
    try:
        streams = json.loads(out.stdout.decode("utf-8", "replace") or "{}").get("streams", [])
    except ValueError:
        streams = []
    return {s.get("codec_type", "?"): _num(s.get("duration")) for s in streams}


def verify_output(ffmpeg: str, ffprobe: str, path: Path, expect: ExpectedOutput, tp: TimeoutPolicy,
                  token: CancellationToken | None = None, *, full_decode: bool = True) -> MediaInfo:
    """Raise VerificationError unless ``path`` is a complete, valid video."""
    if not path.exists():
        raise VerificationError("Відеофайл не створено.", code="OUTPUT_MISSING")
    size = path.stat().st_size
    min_size = max(MIN_OUTPUT_BYTES, int(expect.duration_s * MIN_BITRATE_BPS / 8))
    if size < min_size:
        raise VerificationError(f"Відеофайл підозріло малий ({size} байт).", code="OUTPUT_TOO_SMALL")
    info = probe_media(ffprobe, path, tp, token)
    problems: list[str] = []
    if "mp4" not in info.container and "mov" not in info.container:
        problems.append(f"контейнер {info.container}")
    if not info.has_video:
        problems.append("немає відеопотоку")
    else:
        if info.video_codec != expect.video_codec:
            problems.append(f"відеокодек {info.video_codec}")
        if (info.width, info.height) != (expect.width, expect.height):
            problems.append(f"роздільність {info.width}x{info.height}")
        if info.video_frames != expect.total_frames:
            problems.append(f"кадрів {info.video_frames} замість {expect.total_frames}")
    if expect.has_audio:
        if not info.has_audio:
            problems.append("немає аудіопотоку")
        elif (info.audio_codec, info.sample_rate, info.channels) != (
                expect.audio_codec, expect.sample_rate, expect.channels):
            problems.append(f"аудіо {info.audio_codec}/{info.sample_rate}/{info.channels}")
    if info.duration_s <= 0:
        problems.append("нульова тривалість")
    durs = stream_durations(ffprobe, path, tp, token)
    frame = 1.0 / expect.fps
    vd, ad = durs.get("video", 0.0), durs.get("audio", 0.0)
    if abs(vd - expect.duration_s) > frame + 1e-3:
        problems.append(f"тривалість відео {vd:.3f} с замість {expect.duration_s:.3f} с")
    if expect.has_audio and abs(ad - vd) > frame + AAC_SLACK_S:
        problems.append(f"тривалість аудіо {ad:.3f} с не збігається з відео {vd:.3f} с")
    if problems:
        raise VerificationError("Відео не пройшло перевірку: " + "; ".join(problems) + ".",
                                code="VERIFICATION_FAILED")
    if full_decode:
        out = run_tool([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-i", str(path), "-f", "null", "-"],
                       timeout_s=timeouts.verify(tp, expect.duration_s).hard_s, token=token)
        if out.cancelled:
            raise JobCancelledError()
        if out.timed_out:
            raise OperationTimeoutError("Перевірка відео триває надто довго.", code="VERIFY_TIMEOUT")
        if out.result.returncode != 0 or out.stderr.strip():
            raise VerificationError("Відео містить пошкоджені дані.", code="DECODE_ERRORS",
                                    detail=out.stderr[-2000:])
    return info

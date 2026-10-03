from __future__ import annotations

import struct
import zipfile
from pathlib import Path

import pytest

from videogen.config.settings import AudioSettings, EffectsSettings, TimeoutPolicy
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import ArchiveError, InputError, JobCancelledError, VerificationError
from videogen.media.archiver import ArchiveEntry, create_archive, _verify
from videogen.media.audio_processor import normalize_audio, probe_audio, wav_duration
from videogen.media.media_validator import ExpectedOutput, verify_output
from videogen.media.timeline import build_timeline, split_frames, total_frames_for
from tests.fixtures import factory as F

TP = TimeoutPolicy()
AS = AudioSettings()


def process(src: Path, tmp_path: Path):
    info = probe_audio(F.FFPROBE, src, TP)
    return normalize_audio(F.FFMPEG, info, tmp_path / "audio" / "a.wav", AS, TP)


# ---------------------------------------------------------------- audio

@pytest.mark.parametrize("ext,args", [
    ("mp3", ()), ("wav", ()), ("m4a", ("-c:a", "aac")), ("flac", ()), ("ogg", ("-c:a", "libvorbis")),
])
def test_valid_audio_formats(tmp_path, ext, args):
    src = F.tone(tmp_path / f"тон №1 & #%+.{ext}", 3.37, args)
    info = process(src, tmp_path)
    assert abs(info.decoded_duration_s - 3.37) < 0.06
    assert (info.sample_rate, info.channels) == (48000, 2)
    assert Path(info.wav_path).exists() and len(info.sha256) == 64
    assert not list((tmp_path / "audio").glob("*.tmp*"))


def test_missing_audio(tmp_path):
    with pytest.raises(InputError) as ei:
        probe_audio(F.FFPROBE, tmp_path / "none.mp3", TP)
    assert ei.value.code == "AUDIO_MISSING"


@pytest.mark.parametrize("content", [b"", b"\x00" * 5000, b"ID3" + bytes(range(256)) * 40])
def test_corrupted_audio(tmp_path, content):
    p = tmp_path / "bad.mp3"
    p.write_bytes(content)
    with pytest.raises(InputError) as ei:
        process(p, tmp_path)
    assert ei.value.code == "INVALID_AUDIO"


def test_video_without_audio_track(tmp_path):
    v = F.make_video(tmp_path / "v.mp4", audio_s=None)
    with pytest.raises(InputError):
        process(v, tmp_path)


def test_truncated_audio_uses_real_duration(tmp_path):
    good = F.tone(tmp_path / "g.wav", 6.0)
    data = good.read_bytes()
    bad = tmp_path / "cut.wav"
    bad.write_bytes(data[: len(data) // 2])            # header still claims 6 s
    info = process(bad, tmp_path)
    assert abs(info.decoded_duration_s - 3.0) < 0.1


def test_wav_parser_handles_rf64(tmp_path):
    p = tmp_path / "rf.wav"
    F.ff("-f", "lavfi", "-i", "sine=d=1.5", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le",
         "-rf64", "always", str(p))
    dur, rate, ch = wav_duration(p)
    assert abs(dur - 1.5) < 0.01 and rate == 48000 and ch == 2


def test_wav_parser_rejects_garbage(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(b"RIFF" + struct.pack("<I", 4) + b"WAVE")
    with pytest.raises(ValueError):
        wav_duration(p)


def test_audio_cancel(tmp_path):
    src = F.tone(tmp_path / "a.mp3", 2.0)
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelledError):
        probe_audio(F.FFPROBE, src, TP, token)


# ---------------------------------------------------------------- timeline

@pytest.mark.parametrize("dur,fps,n", [(7.37, 30, 3), (60.0, 30, 7), (3600.5, 25, 1000), (10.0, 24, 1)])
def test_timeline_is_exact_and_deterministic(dur, fps, n):
    eff = EffectsSettings()
    tl = build_timeline(dur, fps, n, eff, min_seconds_per_image=1.5, seed="job1")
    assert tl.total_frames == total_frames_for(dur, fps)
    assert sum(s.frames for s in tl.segments) == tl.total_frames
    assert tl.video_duration_s >= dur and tl.video_duration_s - dur < 1 / fps
    assert max(s.frames for s in tl.segments) - min(s.frames for s in tl.segments) <= 1
    assert tl.segments[0].transition_in == 0
    for s in tl.segments[1:]:
        assert 0 <= s.transition_in <= 0.25 * s.frames
    assert tl == build_timeline(dur, fps, n, eff, min_seconds_per_image=1.5, seed="job1")


def test_total_frames_float_noise():
    assert total_frames_for(10.0000000001, 30) == 300
    assert total_frames_for(10.01, 30) == 301
    assert total_frames_for(7.37, 30) == 222


def test_split_frames_property():
    for total in range(1, 400, 7):
        for n in range(1, 30):
            parts = split_frames(total, n)
            assert sum(parts) == total and max(parts) - min(parts) <= 1


def test_too_many_images_rejected():
    with pytest.raises(InputError) as ei:
        build_timeline(10.0, 30, 1000, EffectsSettings(), min_seconds_per_image=1.5, seed="x")
    assert ei.value.code == "TOO_MANY_IMAGES" and "1000" in ei.value.user_message


def test_motions_are_moderate_and_seeded():
    eff = EffectsSettings()
    a = build_timeline(60, 30, 20, eff, min_seconds_per_image=1, seed="a")
    b = build_timeline(60, 30, 20, eff, min_seconds_per_image=1, seed="b")
    assert [s.motion for s in a.segments] != [s.motion for s in b.segments]
    for s in a.segments:
        m = s.motion
        assert 1.0 <= m.zoom_start <= eff.max_zoom and 1.0 <= m.zoom_end <= eff.max_zoom
        half = 0.5 / max(m.zoom_start, m.zoom_end)
        for x in (m.x_start, m.x_end, m.y_start, m.y_end):
            assert half <= x <= 1 - half + 1e-9           # window never leaves the canvas
    static = build_timeline(60, 30, 5, EffectsSettings(ken_burns=False, transitions=False),
                            min_seconds_per_image=1, seed="a")
    assert {s.motion.kind for s in static.segments} == {"static"}
    assert all(s.transition_in == 0 for s in static.segments)


# ---------------------------------------------------------------- output verification

def expect(frames=60, audio=True, size=(320, 240)):
    return ExpectedOutput(size[0], size[1], 30, frames, audio)


def test_valid_output_passes(tmp_path):
    v = F.make_video(tmp_path / "v.mp4", frames=60, audio_s=2.0)
    info = verify_output(F.FFMPEG, F.FFPROBE, v, expect(), TP)
    assert info.video_frames == 60 and info.has_audio


@pytest.mark.parametrize("mutate,code", [
    (lambda p: p.write_bytes(p.read_bytes()[: p.stat().st_size // 2]), None),
    (lambda p: p.unlink(), "OUTPUT_MISSING"),
    (lambda p: p.write_bytes(b"\x00" * 50000), "BAD_CONTAINER"),
])
def test_broken_output_fails(tmp_path, mutate, code):
    v = F.make_video(tmp_path / "v.mp4", frames=90, audio_s=3.0)
    mutate(v)
    with pytest.raises(VerificationError) as ei:
        verify_output(F.FFMPEG, F.FFPROBE, v, expect(90), TP)
    if code:
        assert ei.value.code == code


def test_wrong_frame_count_fails(tmp_path):
    v = F.make_video(tmp_path / "v.mp4", frames=60)
    with pytest.raises(VerificationError, match="кадрів"):
        verify_output(F.FFMPEG, F.FFPROBE, v, expect(61), TP)


def test_missing_audio_stream_fails(tmp_path):
    v = F.make_video(tmp_path / "v.mp4", frames=60, audio_s=None)
    with pytest.raises(VerificationError, match="аудіо"):
        verify_output(F.FFMPEG, F.FFPROBE, v, expect(60, audio=True), TP)


def test_audio_shorter_than_video_fails(tmp_path):
    v = tmp_path / "v.mp4"
    F.ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30", "-f", "lavfi", "-i", "sine=d=1:sample_rate=48000",
         "-frames:v", "90", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-ac", "2", str(v))
    with pytest.raises(VerificationError, match="тривалість аудіо"):
        verify_output(F.FFMPEG, F.FFPROBE, v, expect(90), TP)


# ---------------------------------------------------------------- archive

def _files(tmp_path, n=5):
    d = tmp_path / "Вхідні файли"
    d.mkdir()
    out = []
    for i in range(n):
        p = d / f"зображення {i} & #%+.jpg"
        F.jpg(p)
        out.append(ArchiveEntry(p, f"inputs/{p.name}"))
    m = tmp_path / "manifest.json"
    m.write_text('{"ok": true}', encoding="utf-8")
    out.append(ArchiveEntry(m, "manifest.json"))
    return out


def test_archive_created_and_verified(tmp_path):
    entries = _files(tmp_path)
    dest = tmp_path / "out" / "_archive" / "Відео 1.zip"
    r = create_archive(dest, entries, max_size_bytes=10**9)
    assert r.path == str(dest) and r.entries == 6
    with zipfile.ZipFile(dest) as zf:
        assert zf.testzip() is None
        names = zf.namelist()
        assert "inputs/зображення 0 & #%+.jpg" in names
        assert zf.getinfo(names[0]).compress_type == zipfile.ZIP_STORED
        assert zf.getinfo("manifest.json").compress_type == zipfile.ZIP_DEFLATED
    assert not list(dest.parent.glob(".*.part"))


def test_archive_over_limit_is_skipped_not_created(tmp_path):
    entries = _files(tmp_path)
    dest = tmp_path / "a.zip"
    r = create_archive(dest, entries, max_size_bytes=1000)
    assert r.path is None and "перевищує" in r.skipped_reason and not dest.exists()


def test_archive_missing_source_fails_and_cleans_up(tmp_path):
    entries = _files(tmp_path)
    entries[2].source.unlink()
    dest = tmp_path / "a.zip"
    with pytest.raises(ArchiveError):
        create_archive(dest, entries, max_size_bytes=10**9)
    assert not dest.exists() and not list(tmp_path.glob(".*.part"))


def test_archive_cancel(tmp_path):
    token = CancellationToken()
    token.cancel()
    with pytest.raises(JobCancelledError):
        create_archive(tmp_path / "a.zip", _files(tmp_path), max_size_bytes=10**9, token=token)
    assert not list(tmp_path.glob(".*.part")) and not (tmp_path / "a.zip").exists()


def test_archive_verification_detects_corruption(tmp_path):
    entries = _files(tmp_path)
    dest = tmp_path / "a.zip"
    create_archive(dest, entries, max_size_bytes=10**9)
    data = bytearray(dest.read_bytes())
    data[200:260] = b"\xff" * 60                      # damage the first stored entry
    dest.write_bytes(bytes(data))
    with pytest.raises((ArchiveError, zipfile.BadZipFile)):
        _verify(dest, entries, sum(e.source.stat().st_size for e in entries), None)

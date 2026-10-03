"""FFmpeg command builders for segment rendering and final mux
(ARCHITECTURE.md §8.1, §8.1.1).

Every command is a pure function of typed inputs and returns an argv list.
Paths inside the workspace are short ASCII names relative to the job
directory (``cwd``), so no quoting or Unicode issues can arise.

Key stability properties:
  * ``-frames:v N`` on every segment: a wrong filter expression can never
    produce an endless stream;
  * frames never pass through Python;
  * one image per segment: each process is short and individually
    supervised.
"""

from __future__ import annotations

from dataclasses import dataclass

from videogen.media.timeline import Motion, SegmentSpec, Timeline

SUPERSAMPLE = 2   # zoompan works on a 2x canvas: sub-pixel smooth motion


@dataclass(frozen=True)
class EncodeSettings:
    width: int
    height: int
    fps: int
    crf: int = 20
    preset: str = "medium"
    encoder: str = "libx264"
    canvas_w: int = 0       # normalised image size (output × overscan)
    canvas_h: int = 0
    ffmpeg: str = "ffmpeg"

    @property
    def timescale(self) -> int:
        return self.fps * 512


def _num(x: float) -> str:
    return f"{x:.6f}".rstrip("0").rstrip(".") or "0"


def zoompan(motion: Motion, *, frames: int, offset: int, motion_frames: int, s: EncodeSettings) -> str:
    """zoompan filter producing ``frames`` frames starting at motion frame
    ``offset`` of a motion lasting ``motion_frames`` frames (smoothstep ease)."""
    span = max(1, motion_frames - 1)
    p = f"min(1,(on+{offset})/{span})"
    ease = f"({p}*{p}*(3-2*{p}))"
    z = f"{_num(motion.zoom_start)}+({_num(motion.zoom_end - motion.zoom_start)})*{ease}"
    cx = f"({_num(motion.x_start)}+({_num(motion.x_end - motion.x_start)})*{ease})"
    cy = f"({_num(motion.y_start)}+({_num(motion.y_end - motion.y_start)})*{ease})"
    x = f"max(0,min(iw-iw/zoom,iw*{cx}-iw/zoom/2))"
    y = f"max(0,min(ih-ih/zoom,ih*{cy}-ih/zoom/2))"
    return (f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={s.width}x{s.height}:fps={s.fps}")


def _image_chain(label_in: str, label_out: str, motion: Motion, *, frames: int, offset: int,
                 motion_frames: int, s: EncodeSettings) -> str:
    if motion.kind == "static":
        # no motion: scale once, zoompan only repeats the frame exactly `frames` times
        return (f"[{label_in}]scale={s.width}:{s.height}:flags=lanczos,"
                f"zoompan=z=1:x=0:y=0:d={frames}:s={s.width}x{s.height}:fps={s.fps},"
                f"setsar=1,format=yuv420p,settb=1/{s.fps}[{label_out}]")
    up_w, up_h = s.canvas_w * SUPERSAMPLE, s.canvas_h * SUPERSAMPLE
    return (f"[{label_in}]scale={up_w}:{up_h}:flags=lanczos,"
            f"{zoompan(motion, frames=frames, offset=offset, motion_frames=motion_frames, s=s)},"
            f"setsar=1,format=yuv420p,settb=1/{s.fps}[{label_out}]")


def segment_argv(seg: SegmentSpec, prev: SegmentSpec | None, *, image: str, prev_image: str | None,
                 out: str, s: EncodeSettings) -> list[str]:
    """argv for one segment (cwd = job dir). ``prev`` is the previous segment
    spec when this segment starts with a crossfade."""
    argv = [s.ffmpeg, "-hide_banner", "-v", "warning", "-y"]
    if seg.transition_in > 0:
        if prev is None or prev_image is None:
            raise ValueError("crossfade segment needs the previous image")
        argv += ["-i", prev_image, "-i", image]
        a = _image_chain("0:v", "a", prev.motion, frames=seg.transition_in, offset=prev.frames,
                         motion_frames=prev.motion_frames, s=s)
        b = _image_chain("1:v", "b", seg.motion, frames=seg.frames, offset=0,
                         motion_frames=seg.motion_frames, s=s)
        dur = _num(seg.transition_in / s.fps)
        graph = f"{a};{b};[a][b]xfade=transition=fade:duration={dur}:offset=0,format=yuv420p[v]"
    else:
        argv += ["-i", image]
        graph = _image_chain("0:v", "v", seg.motion, frames=seg.frames, offset=0,
                             motion_frames=seg.motion_frames, s=s)
    argv += ["-filter_complex", graph, "-map", "[v]", "-frames:v", str(seg.frames), "-an",
             "-c:v", s.encoder]
    if s.encoder == "libx264":
        argv += ["-preset", s.preset, "-crf", str(s.crf)]
    else:
        argv += ["-cq", str(s.crf)]
    argv += ["-pix_fmt", "yuv420p", "-r", str(s.fps), "-g", str(2 * s.fps),
             "-video_track_timescale", str(s.timescale), "-progress", "pipe:1", "-nostats", out]
    return argv


def concat_list(segment_names: list[str]) -> str:
    for n in segment_names:
        if not n.isascii() or "'" in n or "\n" in n:
            raise ValueError(f"unsafe segment name {n!r}")
    return "ffconcat version 1.0\n" + "".join(f"file '{n}'\n" for n in segment_names)


def mux_argv(*, concat_file: str, audio_wav: str, out: str, timeline: Timeline, s: EncodeSettings,
             audio_bitrate_kbps: int, sample_rate: int, channels: int) -> list[str]:
    """concat (stream copy) + AAC audio padded to the exact video length."""
    duration = _num(timeline.total_frames / s.fps)
    return [s.ffmpeg, "-hide_banner", "-v", "warning", "-y",
            "-f", "concat", "-safe", "0", "-i", concat_file, "-i", audio_wav,
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
            "-c:a", "aac", "-b:a", f"{audio_bitrate_kbps}k", "-ar", str(sample_rate), "-ac", str(channels),
            "-af", "apad", "-t", duration, "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", out]

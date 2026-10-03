"""Programmatic test media. Nothing binary is committed to the repo."""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

from PIL import Image

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


def halves(size=(320, 200), left=(220, 30, 30), right=(30, 30, 220), mode="RGB") -> Image.Image:
    img = Image.new(mode, size, left)
    img.paste(Image.new(mode, (size[0] // 2, size[1]), right), (size[0] // 2, 0))
    return img


def save(img: Image.Image, path: Path, fmt: str, **kw) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, fmt, **kw)
    return path


def jpg(path: Path, size=(320, 200), **kw) -> Path:
    return save(halves(size), path, "JPEG", quality=90, **kw)


def png(path: Path, size=(320, 200)) -> Path:
    return save(halves(size), path, "PNG")


def webp(path: Path, size=(320, 200)) -> Path:
    return save(halves(size), path, "WEBP", quality=90)


def bmp(path: Path, size=(320, 200)) -> Path:
    return save(halves(size), path, "BMP")


def tiff(path: Path, size=(320, 200)) -> Path:
    return save(halves(size), path, "TIFF")


def transparent_png(path: Path, size=(300, 300)) -> Path:
    img = Image.new("RGBA", size, (255, 0, 0, 0))
    img.paste(Image.new("RGBA", (size[0] // 2, size[1] // 2), (0, 255, 0, 255)), (0, 0))
    return save(img, path, "PNG")


def palette_png(path: Path) -> Path:
    img = halves((200, 200)).convert("P", palette=Image.Palette.ADAPTIVE, colors=4)
    return save(img, path, "PNG", transparency=0)


def gray16_png(path: Path) -> Path:
    img = Image.new("I;16", (200, 120))
    img.paste(40000, (0, 0, 100, 120))
    return save(img, path, "PNG")


def cmyk_jpg(path: Path) -> Path:
    return save(halves((200, 120)).convert("CMYK"), path, "JPEG")


def exif_rotated_jpg(path: Path, orientation: int = 6) -> Path:
    img = halves((320, 200))
    exif = Image.Exif()
    exif[0x0112] = orientation
    return save(img, path, "JPEG", exif=exif.tobytes())


def broken_exif_jpg(path: Path) -> Path:
    img = halves((320, 200))
    return save(img, path, "JPEG", exif=b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\xff\xff\x01\x12garbage")


def broken_icc_jpg(path: Path) -> Path:
    return save(halves((320, 200)), path, "JPEG", icc_profile=b"definitely not an icc profile" * 4)


def truncated(src: Path, dst: Path, keep: float = 0.5) -> Path:
    data = src.read_bytes()
    dst.write_bytes(data[: int(len(data) * keep)])
    return dst


def corrupted_png(path: Path) -> Path:
    good = io.BytesIO()
    halves((320, 200)).save(good, "PNG")
    data = bytearray(good.getvalue())
    idat = data.find(b"IDAT")
    for i in range(idat + 8, min(idat + 400, len(data) - 12)):
        data[i] ^= 0x5A
    path.write_bytes(bytes(data))
    return path


def corrupted_jpg(path: Path) -> Path:
    good = io.BytesIO()
    halves((320, 200)).save(good, "JPEG")
    data = bytearray(good.getvalue())
    sos = data.find(b"\xff\xda")
    data[sos + 20: len(data) - 2] = b"\x00" * (len(data) - 2 - sos - 20)
    data = data[:sos + 60]  # also truncate
    path.write_bytes(bytes(data))
    return path


def png_named_jpg(path: Path) -> Path:
    return save(halves((320, 200)), path, "PNG")  # path ends with .jpg


def zero_byte(path: Path) -> Path:
    path.write_bytes(b"")
    return path


def bomb_png(path: Path, side: int = 12000) -> Path:
    return save(Image.new("1", (side, side)), path, "PNG")


def ff(*args: str, timeout: float = 120) -> None:
    assert FFMPEG
    subprocess.run([FFMPEG, "-hide_banner", "-v", "error", "-y", *args], check=True, timeout=timeout,
                   stdin=subprocess.DEVNULL)


def tone(path: Path, seconds: float = 3.0, codec_args: tuple[str, ...] = ()) -> Path:
    ff("-f", "lavfi", "-i", f"sine=f=440:d={seconds}", "-ac", "2", *codec_args, str(path))
    return path


def make_video(path: Path, frames: int = 60, fps: int = 30, size: str = "320x240",
               audio_s: float | None = 2.0) -> Path:
    args = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}"]
    if audio_s:
        args += ["-f", "lavfi", "-i", f"sine=d={audio_s}:sample_rate=48000"]
    args += ["-frames:v", str(frames), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "ultrafast"]
    if audio_s:
        args += ["-c:a", "aac", "-ac", "2", "-ar", "48000", "-af", "apad", "-t", f"{frames / fps}"]
    ff(*args, str(path))
    return path

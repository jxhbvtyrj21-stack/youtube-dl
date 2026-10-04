"""Input generators for the production suite."""

from __future__ import annotations

import io
import os
import random
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageCms, ImageDraw

FFMPEG = shutil.which("ffmpeg") or os.path.join(os.environ.get("VIDEOGEN_FFMPEG_DIR", ""), "ffmpeg")


def tone(path: Path, seconds: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"sine=f=330:d={seconds:.3f}",
                    "-ac", "2", "-b:a", "128k", str(path)], check=True, timeout=600, stdin=subprocess.DEVNULL)
    return path


def picture(size: tuple[int, int], seed: int) -> Image.Image:
    rnd = random.Random(seed)
    img = Image.new("RGB", size, tuple(rnd.randrange(40, 200) for _ in range(3)))
    d = ImageDraw.Draw(img)
    w, h = size
    for _ in range(12):
        x, y = rnd.randrange(w), rnd.randrange(h)
        r = rnd.randrange(max(2, min(w, h) // 20), max(3, min(w, h) // 4))
        d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(rnd.randrange(256) for _ in range(3)))
    return img


def normal_set(folder: Path, n: int, *, seconds_per_image: float, sizes=((1920, 1080), (1280, 720),
               (1080, 1350), (1600, 1200))) -> float:
    folder.mkdir(parents=True, exist_ok=True)
    fmts = [("jpg", "JPEG", {"quality": 90}), ("png", "PNG", {}), ("webp", "WEBP", {"quality": 85})]
    for i in range(n):
        ext, fmt, kw = fmts[i % 3]
        picture(sizes[i % len(sizes)], i).save(folder / f"img_{i + 1:05d}.{ext}", fmt, **kw)
    dur = round(n * seconds_per_image, 3)
    tone(folder / "audio.mp3", dur)
    return dur


# ---------------------------------------------------------------- problem files

def _good_jpeg_bytes(size=(1280, 720), seed=1, **kw) -> bytes:
    b = io.BytesIO()
    picture(size, seed).save(b, "JPEG", quality=92, **kw)
    return b.getvalue()


def _good_png_bytes(size=(1280, 720), seed=2) -> bytes:
    b = io.BytesIO()
    picture(size, seed).save(b, "PNG")
    return b.getvalue()


def corrupted_png() -> bytes:
    data = bytearray(_good_png_bytes(seed=3))
    i = data.find(b"IDAT")
    for k in range(i + 8, min(i + 600, len(data) - 12)):
        data[k] ^= 0x5A
    return bytes(data)


def corrupted_jpeg() -> bytes:
    data = bytearray(_good_jpeg_bytes(seed=4))
    sos = data.find(b"\xff\xda")
    for k in range(sos + 30, len(data) - 2, 7):
        data[k] = 0xFF
    return bytes(data)


def problem_set(folder: Path) -> dict[str, str]:
    """Writes the 'real image stress' set. Returns {file name: expected class}
    where class is ok | invalid | recovered_or_invalid."""
    folder.mkdir(parents=True, exist_ok=True)
    exp: dict[str, str] = {}

    def put(name: str, data: bytes | Image.Image, cls: str, **save) -> None:
        p = folder / name
        if isinstance(data, Image.Image):
            data.save(p, **save)
        else:
            p.write_bytes(data)
        exp[name] = cls

    put("001_normal.jpg", _good_jpeg_bytes(seed=10), "ok")
    put("002_normal.png", _good_png_bytes(seed=11), "ok")
    rgba = Image.new("RGBA", (1200, 800), (255, 0, 0, 0))
    ImageDraw.Draw(rgba).ellipse((200, 100, 1000, 700), fill=(0, 200, 0, 180))
    put("003_alpha.png", rgba, "ok", format="PNG")
    put("004_photo.webp", picture((1600, 900), 12), "ok", format="WEBP", quality=85)
    put("005_very_large_108mpx.jpg", picture((12000, 9000), 13), "ok", format="JPEG", quality=80)
    put("006_large_81mpx.png", Image.new("RGB", (9000, 9000), (90, 120, 150)), "ok", format="PNG")
    put("007_panorama_8000x400.jpg", picture((8000, 400), 14), "ok", format="JPEG")
    put("008_tall_400x6000.png", picture((400, 6000), 15), "ok", format="PNG")
    for o in range(1, 9):
        ex = Image.Exif()
        ex[0x0112] = o
        put(f"01{o}_exif_orientation_{o}.jpg", picture((900, 600), 20 + o), "ok", format="JPEG", exif=ex.tobytes())
    put("020_broken_exif.jpg", _good_jpeg_bytes(seed=30, exif=b"Exif\x00\x00MM\x00*\x00\x00\x00\x08\xff\xff"), "ok")
    lab = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()
    put("021_unusual_icc_lab.jpg", _good_jpeg_bytes(seed=31, icc_profile=lab), "ok")
    put("022_cmyk.jpg", picture((800, 600), 32).convert("CMYK"), "ok", format="JPEG")
    g16 = Image.new("I;16", (800, 600))
    g16.paste(30000, (0, 0, 400, 600))
    put("023_gray16.png", g16, "ok", format="PNG")
    put("024_Фото №1 — тест (копія) ' & # % +.jpg", _good_jpeg_bytes(seed=33), "ok")
    put("025_" + "дуже_довга_назва_файлу_" * 5 + ".png", _good_png_bytes(seed=34), "ok")
    put("026_png_named_as.jpg", _good_png_bytes(seed=35), "ok")
    put("027_jpeg_named_as.webp", _good_jpeg_bytes(seed=36), "ok")
    put("030_corrupted.png", corrupted_png(), "recovered_or_invalid")
    put("031_corrupted.jpg", corrupted_jpeg(), "recovered_or_invalid")
    put("032_zero_byte.jpg", b"", "invalid")
    put("033_garbage.png", os.urandom(50_000), "invalid")
    good = _good_jpeg_bytes(size=(1600, 1200), seed=37)
    put("034_partially_readable.jpg", good[: int(len(good) * 0.6)], "recovered_or_invalid")
    goodp = _good_png_bytes(size=(1600, 1200), seed=38)
    put("035_partially_readable.png", goodp[: int(len(goodp) * 0.6)], "recovered_or_invalid")
    put("036_heic_signature.jpg", b"\x00\x00\x00\x18ftypheic" + os.urandom(4000), "invalid")
    put("037_decompression_bomb.png", Image.new("1", (15000, 15000)), "invalid", format="PNG")
    return exp

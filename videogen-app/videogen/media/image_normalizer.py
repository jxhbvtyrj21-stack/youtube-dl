"""Image normalisation with a chain of independent decoders
(ARCHITECTURE.md §6.2).

Each decoder turns the source into a clean 8-bit RGB ``PIL.Image``; the
shared tail composes the frame canvas (§6.4) and writes a baseline JPEG
without any metadata. The rendering pipeline only ever sees these
normalised files, never the user's originals.

Runs inside the ImageWorker process: a decoder that hangs or crashes kills
only that process.
"""

from __future__ import annotations

import io
import math
import logging
import os
import shutil
import tempfile
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageFile, ImageOps

from videogen.config.settings import ImageSettings
from videogen.ffmpeg_ctl.runner import run_tool
from videogen.media import canvas as canvas_mod
from videogen.storage.atomic import replace_with_retry
from videogen.utils.paths import long_path

log = logging.getLogger(__name__)

ImageFile.LOAD_TRUNCATED_IMAGES = False   # never hide a truncated file
DECODERS = ("pillow", "opencv", "ffmpeg")
_SRGB = ImageCms.createProfile("sRGB")


class DecodeError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class NormalizeTarget:
    width: int                     # output frame size (without overscan)
    height: int
    overscan: float
    vertical: bool
    bg_color: tuple[int, int, int] = (0, 0, 0)


@dataclass
class DecodeOutcome:
    decoder: str
    canvas_size: tuple[int, int]
    fit_mode: str
    cropped_fraction: float
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- helpers

def _to_rgb(img: Image.Image, bg: tuple[int, int, int], notes: list[str]) -> Image.Image:
    mode = img.mode
    if mode == "RGB":
        return img
    if mode == "P":
        img = img.convert("RGBA" if "transparency" in img.info else "RGB")
        mode = img.mode
    if mode.startswith("I;16") or mode in ("I", "F"):
        notes.append(f"глибина кольору {mode} зведена до 8 біт")
        if mode.startswith("I;16"):
            img = img.convert("I")
        lo, hi = img.getextrema()
        if mode.startswith("I;16"):
            lo, hi = 0, 65535
        scale = 255.0 / max(1e-9, float(hi) - float(lo))
        offset = -float(lo) * scale
        # Pillow only accepts linear expressions for I/F images
        img = img.point(lambda v: v * scale + offset).convert("L")
        mode = "L"
    if mode in ("RGBA", "LA", "PA", "RGBa", "La"):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, bg)
        base.paste(rgba, mask=rgba.getchannel("A"))
        return base
    if mode == "CMYK":
        notes.append("CMYK перетворено на RGB")
    return img.convert("RGB")


def _apply_icc(img: Image.Image, notes: list[str]) -> Image.Image:
    icc = img.info.get("icc_profile")
    if not icc:
        return img
    if img.mode not in ("RGB", "RGBA", "CMYK", "L"):
        return img
    try:
        src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        out_mode = "RGBA" if img.mode == "RGBA" else "RGB"
        converted = ImageCms.profileToProfile(img, src, _SRGB, outputMode=out_mode)
        return converted if converted is not None else img
    except Exception as exc:  # noqa: BLE001 - broken ICC must not fail the image
        notes.append(f"колірний профіль ICC пошкоджений, використано без перетворення ({exc.__class__.__name__})")
        return img


def _exif_transpose(img: Image.Image, notes: list[str]) -> Image.Image:
    try:
        out = ImageOps.exif_transpose(img)
        return out if out is not None else img
    except Exception as exc:  # noqa: BLE001 - broken EXIF: ignore orientation
        notes.append(f"пошкоджені EXIF-дані, орієнтацію проігноровано ({exc.__class__.__name__})")
        return img


# ---------------------------------------------------------------- decoders

def decode_pillow(src: str, target_px: tuple[int, int], settings: ImageSettings,
                  bg: tuple[int, int, int], notes: list[str]) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        old = Image.MAX_IMAGE_PIXELS
        try:
            with Image.open(src) as probe:
                big = probe.size[0] * probe.size[1] > settings.max_pixels
                fmt = probe.format
        except (Image.DecompressionBombError, Image.DecompressionBombWarning):
            big, fmt = True, "JPEG"
        try:
            if big:
                if fmt != "JPEG":
                    raise DecodeError("TOO_LARGE", "non-JPEG above pixel limit")
                Image.MAX_IMAGE_PIXELS = None
            with Image.open(src) as im:
                if im.format == "JPEG":
                    # Decode directly at reduced scale (1/2..1/8) when much larger
                    # than needed. draft() picks the smallest scale whose size is
                    # >= the request, so for over-limit images ask for half of the
                    # limit-preserving size to land under the pixel limit.
                    w, h = im.size
                    request = target_px
                    if w * h > settings.max_pixels:
                        k = math.sqrt(settings.max_pixels / (w * h)) / 2
                        request = (max(1, math.ceil(w * k)), max(1, math.ceil(h * k)))
                    im.draft("RGB", request)
                    if im.size[0] * im.size[1] > settings.max_pixels:
                        raise DecodeError("TOO_LARGE", f"{im.size} after draft")
                if getattr(im, "n_frames", 1) > 1:
                    notes.append("багатокадровий файл: використано перший кадр")
                    im.seek(0)
                im.load()
                img = _exif_transpose(im, notes)
                img = _apply_icc(img, notes)
                return _to_rgb(img, bg, notes)
        finally:
            Image.MAX_IMAGE_PIXELS = old


def decode_opencv(src: str, target_px: tuple[int, int], settings: ImageSettings,
                  bg: tuple[int, int, int], notes: list[str]) -> Image.Image:
    import cv2  # local import: heavy, only needed on fallback
    import numpy as np

    data = np.fromfile(src, dtype=np.uint8)   # cv2.imread cannot open Unicode paths on Windows
    if data.size == 0:
        raise DecodeError("EMPTY")
    arr = cv2.imdecode(data, cv2.IMREAD_UNCHANGED)
    del data
    if arr is None:
        raise DecodeError("CORRUPT", "cv2.imdecode returned None")
    if arr.shape[0] * arr.shape[1] > settings.max_pixels:
        raise DecodeError("TOO_LARGE", str(arr.shape))
    if arr.dtype != np.uint8:
        arr = cv2.normalize(arr, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
    if arr.ndim == 2:
        img = Image.fromarray(arr, "L").convert("RGB")
    elif arr.shape[2] == 4:
        rgba = cv2.cvtColor(arr, cv2.COLOR_BGRA2RGBA)
        img = _to_rgb(Image.fromarray(rgba, "RGBA"), bg, notes)
    else:
        img = Image.fromarray(cv2.cvtColor(arr[:, :, :3], cv2.COLOR_BGR2RGB), "RGB")
    notes.append("використано резервний декодер OpenCV")
    return img


def decode_ffmpeg(src: str, target_px: tuple[int, int], settings: ImageSettings,
                  bg: tuple[int, int, int], notes: list[str], *, ffmpeg: str | None,
                  timeout_s: float) -> Image.Image:
    if not ffmpeg:
        raise DecodeError("DECODE_FAILED", "ffmpeg unavailable")
    tmpdir = tempfile.mkdtemp(prefix="vg-ffdec-")
    try:
        # ASCII-only copy: no quoting/Unicode issues inside ffmpeg
        local_src = os.path.join(tmpdir, "src.bin")
        shutil.copyfile(src, local_src)
        out_png = os.path.join(tmpdir, "out.png")
        w, h = target_px
        vf = f"scale=w='min({w * 2},iw)':h='min({h * 2},ih)':force_original_aspect_ratio=decrease"
        res = run_tool([ffmpeg, "-hide_banner", "-v", "error", "-nostdin", "-i", local_src,
                        "-frames:v", "1", "-vf", vf, "-pix_fmt", "rgb24", "-y", out_png],
                       timeout_s=timeout_s)
        if not res.ok or not os.path.isfile(out_png):
            raise DecodeError("CORRUPT", f"ffmpeg rc={res.result.returncode} {res.stderr[-500:]}")
        with Image.open(out_png) as im:
            im.load()
            img = im.convert("RGB")
        notes.append("використано резервний декодер FFmpeg")
        return img
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ---------------------------------------------------------------- public API

def normalize_with(decoder: str, src: str | Path, dst: Path, target: NormalizeTarget,
                   settings: ImageSettings, *, ffmpeg: str | None = None,
                   ffmpeg_timeout_s: float = 30.0) -> DecodeOutcome:
    """Decode ``src`` with one decoder, compose, write ``dst`` atomically.

    Raises DecodeError (or any exception from the decoder) on failure.
    """
    notes: list[str] = []
    size = canvas_mod.canvas_size(target.width, target.height, target.overscan)
    s = long_path(src)
    if decoder == "pillow":
        img = decode_pillow(s, size, settings, target.bg_color, notes)
    elif decoder == "opencv":
        img = decode_opencv(s, size, settings, target.bg_color, notes)
    elif decoder == "ffmpeg":
        img = decode_ffmpeg(s, size, settings, target.bg_color, notes, ffmpeg=ffmpeg,
                            timeout_s=ffmpeg_timeout_s)
    else:
        raise ValueError(f"unknown decoder {decoder}")
    try:
        if img.size[0] < 2 or img.size[1] < 2:
            raise DecodeError("CORRUPT", f"degenerate size {img.size}")
        out, plan = canvas_mod.compose(img, size, vertical_output=target.vertical,
                                       cover_tolerance=settings.cover_tolerance,
                                       vertical_max_crop=settings.vertical_max_crop)
    finally:
        img.close()
    try:
        write_jpeg(out, dst, settings.jpeg_quality)
    finally:
        out.close()
    verify_written(dst, size)
    return DecodeOutcome(decoder, size, plan.mode, plan.cropped_fraction, notes)


def write_jpeg(img: Image.Image, dst: Path, quality: int) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp")
    # no exif/icc/xmp: pass nothing; subsampling 0 = 4:4:4 keeps edges sharp
    img.save(tmp, "JPEG", quality=quality, subsampling=0, optimize=False)
    replace_with_retry(tmp, dst)


def verify_written(dst: Path, size: tuple[int, int]) -> None:
    with Image.open(dst) as im:
        im.verify()
    with Image.open(dst) as im:
        if im.size != size or im.mode != "RGB":
            raise DecodeError("CORRUPT", f"written file has {im.size} {im.mode}")
        im.load()

"""Frame composition with a controlled blurred background (ARCHITECTURE.md §6.4).

Input: a decoded RGB image of any size. Output: an RGB canvas of exactly
``canvas_size`` where the main image stays fully (or almost fully) visible.
Pure function of its inputs — deterministic, no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageEnhance, ImageFilter

BLUR_DOWNSCALE = 8
BLUR_RADIUS = 6          # applied at 1/8 scale ~ radius 48 px at full size
BACKGROUND_BRIGHTNESS = 0.6


@dataclass(frozen=True)
class FitPlan:
    mode: str                 # "cover" | "contain_blur"
    fg_size: tuple[int, int]  # size of the scaled foreground before cropping
    crop_box: tuple[int, int, int, int] | None  # crop applied to the scaled fg
    offset: tuple[int, int]   # paste position on the canvas
    cropped_fraction: float   # max fraction of the source cut on one axis


def canvas_size(width: int, height: int, overscan: float) -> tuple[int, int]:
    """Output size × overscan, rounded to even numbers (yuv420p friendly)."""
    w = int(round(width * overscan / 2)) * 2
    h = int(round(height * overscan / 2)) * 2
    return max(w, 2), max(h, 2)


def plan_fit(src: tuple[int, int], dst: tuple[int, int], *, vertical_output: bool,
             cover_tolerance: float, vertical_max_crop: float) -> FitPlan:
    sw, sh = src
    dw, dh = dst
    if sw <= 0 or sh <= 0:
        raise ValueError(f"invalid source size {src}")
    r_src, r_dst = sw / sh, dw / dh
    ratio = r_src / r_dst

    if abs(ratio - 1.0) <= cover_tolerance:
        scale = max(dw / sw, dh / sh)
        fw, fh = max(dw, round(sw * scale)), max(dh, round(sh * scale))
        left, top = (fw - dw) // 2, (fh - dh) // 2
        cut = max(1 - dw / fw, 1 - dh / fh)
        return FitPlan("cover", (fw, fh), (left, top, left + dw, top + dh), (0, 0), cut)

    if vertical_output and r_src > r_dst:
        # Landscape source in a 9:16 frame: fit width, but allow trimming the
        # sides by up to vertical_max_crop so the picture is not a thin strip.
        keep = 1.0 - vertical_max_crop
        scale = dw / (sw * keep)
        # floor, not round: rounding up could exceed the allowed crop
        fw, fh = max(dw, math.floor(sw * scale)), round(sh * scale)
        if fh > dh:  # never taller than the frame
            scale = dh / sh
            fw, fh = round(sw * scale), dh
        left = max(0, (fw - dw) // 2)
        vis_w = min(fw, dw)
        crop = (left, 0, left + vis_w, fh)
        cut = 1 - vis_w / fw
        return FitPlan("contain_blur", (fw, fh), crop, ((dw - vis_w) // 2, (dh - fh) // 2), cut)

    scale = min(dw / sw, dh / sh)
    fw, fh = max(1, round(sw * scale)), max(1, round(sh * scale))
    return FitPlan("contain_blur", (fw, fh), None, ((dw - fw) // 2, (dh - fh) // 2), 0.0)


def _resize(img: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Two-step resize: cheap integer ``reduce`` for big shrink factors, then
    LANCZOS. Bounds memory and time for huge sources."""
    w, h = img.size
    factor = min(w // max(1, size[0]), h // max(1, size[1]))
    if factor >= 2:
        img = img.reduce(min(factor, 8))
    if img.size != size:
        img = img.resize(size, Image.Resampling.LANCZOS)
    return img


def _blurred_background(img: Image.Image, dst: tuple[int, int]) -> Image.Image:
    dw, dh = dst
    sw, sh = img.size
    small = (max(2, dw // BLUR_DOWNSCALE), max(2, dh // BLUR_DOWNSCALE))
    scale = max(small[0] / sw, small[1] / sh)
    cw, ch = max(small[0], round(sw * scale)), max(small[1], round(sh * scale))
    bg = _resize(img, (cw, ch))
    left, top = (cw - small[0]) // 2, (ch - small[1]) // 2
    bg = bg.crop((left, top, left + small[0], top + small[1]))
    bg = bg.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    bg = ImageEnhance.Brightness(bg).enhance(BACKGROUND_BRIGHTNESS)
    return bg.resize(dst, Image.Resampling.BILINEAR)


def compose(img: Image.Image, dst: tuple[int, int], *, vertical_output: bool,
            cover_tolerance: float, vertical_max_crop: float) -> tuple[Image.Image, FitPlan]:
    if img.mode != "RGB":
        raise ValueError(f"compose expects RGB, got {img.mode}")
    plan = plan_fit(img.size, dst, vertical_output=vertical_output,
                    cover_tolerance=cover_tolerance, vertical_max_crop=vertical_max_crop)
    fg = _resize(img, plan.fg_size)
    if plan.crop_box is not None:
        fg = fg.crop(plan.crop_box)
    if plan.mode == "cover":
        return fg, plan
    canvas = _blurred_background(img, dst)
    canvas.paste(fg, plan.offset)
    return canvas, plan

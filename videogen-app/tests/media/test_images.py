from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from videogen.config.settings import ImageSettings
from videogen.media import canvas
from videogen.media.image_normalizer import DecodeError, NormalizeTarget, normalize_with
from videogen.media.image_validator import sniff_format, validate_image
from tests.fixtures import factory as F

S = ImageSettings()
H = NormalizeTarget(1920, 1080, 1.0, vertical=False)
V = NormalizeTarget(1080, 1920, 1.0, vertical=True)


# ---------------------------------------------------------------- validation

@pytest.mark.parametrize("maker,fmt", [
    (F.jpg, "JPEG"), (F.png, "PNG"), (F.webp, "WEBP"), (F.bmp, "BMP"), (F.tiff, "TIFF"),
])
def test_valid_formats(tmp_path, maker, fmt):
    p = maker(tmp_path / f"x.{fmt.lower()}")
    r = validate_image(p, S)
    assert r.ok and r.detected_format == fmt and (r.width, r.height) == (320, 200)
    assert len(r.sha256) == 64 and r.warnings == []


def test_extension_is_not_trusted(tmp_path):
    r = validate_image(F.png_named_jpg(tmp_path / "photo.jpg"), S)
    assert r.ok and r.detected_format == "PNG"
    assert any("розширення" in w for w in r.warnings)


@pytest.mark.parametrize("maker,code", [
    (lambda p: F.zero_byte(p), "EMPTY"),
    (lambda p: p.write_bytes(b"hello world, not an image") and p, "UNSUPPORTED_FORMAT"),
    (lambda p: p, "NOT_FOUND"),
])
def test_rejected(tmp_path, maker, code):
    p = tmp_path / "bad.jpg"
    maker(p)
    r = validate_image(p, S)
    assert not r.ok and r.reason_code == code


def test_directory_named_like_image(tmp_path):
    (tmp_path / "dir.jpg").mkdir()
    assert validate_image(tmp_path / "dir.jpg", S).reason_code == "NOT_A_FILE"


def test_too_big_file(tmp_path):
    r = validate_image(F.jpg(tmp_path / "a.jpg"), ImageSettings(max_file_bytes=100))
    assert r.reason_code == "TOO_BIG_FILE"


def test_decompression_bomb_png_rejected(tmp_path):
    r = validate_image(F.bomb_png(tmp_path / "bomb.png"), S)
    assert not r.ok and r.reason_code == "TOO_LARGE"


def test_sniff_heif_is_unsupported():
    assert sniff_format(b"\x00\x00\x00\x18ftypheic\x00\x00") == "HEIF"


# ---------------------------------------------------------------- canvas planning

@pytest.mark.parametrize("src,dst,vertical,mode", [
    ((1920, 1080), (1920, 1080), False, "cover"),
    ((1600, 1000), (1920, 1080), False, "cover"),           # 1.6 vs 1.78: within 10 %
    ((1080, 1920), (1920, 1080), False, "contain_blur"),    # portrait in 16:9
    ((1000, 1000), (1920, 1080), False, "contain_blur"),
    ((1080, 1920), (1080, 1920), True, "cover"),
    ((1920, 1080), (1080, 1920), True, "contain_blur"),     # landscape in 9:16
    ((4000, 500), (1920, 1080), False, "contain_blur"),     # panorama
])
def test_plan_fit_modes(src, dst, vertical, mode):
    plan = canvas.plan_fit(src, dst, vertical_output=vertical, cover_tolerance=0.10, vertical_max_crop=0.15)
    assert plan.mode == mode
    assert plan.cropped_fraction <= 0.15 + 1e-9          # main image never aggressively cropped


def test_landscape_in_vertical_uses_limited_crop():
    plan = canvas.plan_fit((1920, 1080), (1080, 1920), vertical_output=True, cover_tolerance=0.1,
                           vertical_max_crop=0.15)
    assert 0.14 < plan.cropped_fraction <= 0.15
    # horizontal output never crops a portrait image
    plan_h = canvas.plan_fit((1080, 1920), (1920, 1080), vertical_output=False, cover_tolerance=0.1,
                             vertical_max_crop=0.15)
    assert plan_h.cropped_fraction == 0.0


def test_canvas_size_even():
    assert canvas.canvas_size(1920, 1080, 1.15) == (2208, 1242)
    w, h = canvas.canvas_size(1081, 1921, 1.13)
    assert w % 2 == 0 and h % 2 == 0


# ---------------------------------------------------------------- normalisation

def _norm(src: Path, tmp_path: Path, target=H, decoder="pillow", settings=S):
    dst = tmp_path / "out" / "i00001.jpg"
    out = normalize_with(decoder, src, dst, target, settings, ffmpeg=F.FFMPEG)
    with Image.open(dst) as im:
        assert im.format == "JPEG" and im.mode == "RGB"
        assert im.size == canvas.canvas_size(target.width, target.height, target.overscan)
        assert not im.info.get("exif") and not im.info.get("icc_profile")   # metadata stripped
        im.load()
        return out, im.copy()


@pytest.mark.parametrize("maker", [F.jpg, F.png, F.webp, F.bmp, F.tiff, F.png_named_jpg])
@pytest.mark.parametrize("decoder", ["pillow", "opencv", "ffmpeg"])
def test_every_decoder_handles_every_format(tmp_path, maker, decoder):
    src = maker(tmp_path / "src.jpg") if maker is F.png_named_jpg else maker(tmp_path / f"src{maker.__name__}")
    out, _ = _norm(src, tmp_path, decoder=decoder)
    assert out.decoder == decoder


@pytest.mark.parametrize("maker", [F.transparent_png, F.palette_png, F.gray16_png, F.cmyk_jpg,
                                   F.broken_exif_jpg, F.broken_icc_jpg])
def test_unusual_images(tmp_path, maker):
    out, img = _norm(maker(tmp_path / f"{maker.__name__}.img"), tmp_path)
    assert img.getbbox() is not None


def test_broken_icc_reported_as_warning(tmp_path):
    out, _ = _norm(F.broken_icc_jpg(tmp_path / "icc.jpg"), tmp_path)
    assert any("ICC" in w for w in out.warnings)


def test_transparent_png_has_no_alpha_artifacts(tmp_path):
    out, img = _norm(F.transparent_png(tmp_path / "t.png"), tmp_path, target=NormalizeTarget(400, 400, 1.0, False))
    # transparent area composited on black, opaque green kept
    assert img.getpixel((50, 50))[1] > 200
    assert max(img.getpixel((350, 350))) < 30


def test_exif_orientation_applied(tmp_path):
    # 320x200 landscape with orientation 6 -> displayed as 200x320 portrait
    out, img = _norm(F.exif_rotated_jpg(tmp_path / "rot.jpg"), tmp_path)
    assert out.fit_mode == "contain_blur"
    # after a 90° CW rotation the red (left) half is on top
    cx = img.size[0] // 2
    top, bottom = img.getpixel((cx, 200)), img.getpixel((cx, img.size[1] - 200))
    assert top[0] > top[2] and bottom[2] > bottom[0]


def test_vertical_output_blur_background(tmp_path):
    out, img = _norm(F.jpg(tmp_path / "land.jpg", size=(1600, 900)), tmp_path, target=V)
    assert out.fit_mode == "contain_blur"
    w, h = img.size
    centre = img.getpixel((w // 2 - 50, h // 2))
    top = img.getpixel((w // 2 - 50, 20))
    assert centre[0] > 150           # sharp red foreground
    assert top[0] < centre[0]        # darker blurred background


@pytest.mark.parametrize("maker", [F.corrupted_png, F.corrupted_jpg])
def test_corrupted_images_fail_cleanly_with_pillow(tmp_path, maker):
    src = maker(tmp_path / f"bad.{'png' if maker is F.corrupted_png else 'jpg'}")
    with pytest.raises(Exception):
        normalize_with("pillow", src, tmp_path / "o.jpg", H, S)
    assert not (tmp_path / "o.jpg").exists()


def test_truncated_jpeg_is_not_silently_accepted(tmp_path):
    good = F.jpg(tmp_path / "g.jpg", size=(800, 600))
    bad = F.truncated(good, tmp_path / "t.jpg", 0.4)
    with pytest.raises(OSError):
        normalize_with("pillow", bad, tmp_path / "o.jpg", H, S)


def test_huge_jpeg_decoded_via_draft(tmp_path):
    src = F.jpg(tmp_path / "big.jpg", size=(4000, 3000))      # 12 Mpx
    small_limit = ImageSettings(max_pixels=2_000_000)
    r = validate_image(src, small_limit)
    assert r.ok and any("зменшеному" in w for w in r.warnings)
    out, img = _norm(src, tmp_path, settings=small_limit)
    assert out.decoder == "pillow"


def test_huge_non_jpeg_rejected_by_pillow(tmp_path):
    with pytest.raises(DecodeError):
        normalize_with("pillow", F.png(tmp_path / "p.png", size=(3000, 2000)), tmp_path / "o.jpg", H,
                       ImageSettings(max_pixels=1_000_000))


def test_unicode_and_long_names(tmp_path):
    d = tmp_path / "Мої фото (2024) & #%+'"
    name = "Зображення №1 " + "ї" * 80 + " (копія).png"
    src = F.png(d / name)
    assert validate_image(src, S).ok
    out, _ = _norm(src, tmp_path)
    assert out.decoder == "pillow"
    out2, _ = _norm(src, tmp_path, decoder="opencv")
    out3, _ = _norm(src, tmp_path, decoder="ffmpeg")

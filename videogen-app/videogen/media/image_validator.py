"""Image validation (ARCHITECTURE.md §6.1).

Never trusts the file extension: the real format comes from the content's
magic bytes. All checks are cheap and run before any full decode. Runs
inside the ImageWorker process.
"""

from __future__ import annotations

import os
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image

from videogen.config.settings import ImageSettings
from videogen.utils.hashing import sha256_file
from videogen.utils.paths import long_path

SUPPORTED_FORMATS = ("JPEG", "PNG", "WEBP", "BMP", "TIFF")
EXT_FORMAT = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".jfif": "JPEG",
    ".png": "PNG", ".webp": "WEBP", ".bmp": "BMP", ".dib": "BMP", ".tif": "TIFF", ".tiff": "TIFF",
}
IMAGE_EXTENSIONS = frozenset(EXT_FORMAT)

# Human-readable reason codes -> Ukrainian user messages
REASONS = {
    "NOT_FOUND": "файл не знайдено",
    "NOT_A_FILE": "це не файл",
    "NO_ACCESS": "немає доступу до файлу (можливо, він відкритий в іншій програмі)",
    "EMPTY": "файл порожній (0 байт)",
    "TOO_BIG_FILE": "файл завеликий",
    "UNSUPPORTED_FORMAT": "непідтримуваний або нерозпізнаний формат",
    "TOO_LARGE": "надто велика роздільність зображення",
    "CORRUPT": "файл пошкоджений",
    "TRUNCATED": "файл обрізаний (неповний)",
    "DECODE_FAILED": "не вдалося декодувати жодним способом",
    "TIMEOUT": "декодування триває надто довго (можливо, пошкоджений файл)",
    "CRASH": "декодер аварійно завершився на цьому файлі",
}


def sniff_format(head: bytes) -> str | None:
    """Detect the real container format from the first bytes."""
    if head[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WEBP"
    if head[:2] == b"BM":
        return "BMP"
    if head[:4] in (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"):
        return "TIFF"
    if head[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypavif"):
        return "HEIF"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    return None


@dataclass
class ValidationResult:
    ok: bool
    reason_code: str = ""
    detail: str = ""
    detected_format: str = ""
    size_bytes: int = 0
    sha256: str = ""
    hash_mode: str = "full"
    width: int = 0
    height: int = 0
    mode: str = ""
    has_alpha: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def message(self) -> str:
        base = REASONS.get(self.reason_code, self.reason_code)
        return base


def _read_head(path: str, retries: tuple[float, ...] = (0.5, 0.5)) -> bytes:
    """Open with bounded retries: a file briefly locked by antivirus or a
    copy in progress is a transient condition on Windows."""
    last: OSError | None = None
    for attempt in range(len(retries) + 1):
        try:
            with open(path, "rb") as fh:
                return fh.read(32)
        except PermissionError as exc:
            last = exc
            if attempt < len(retries):
                time.sleep(retries[attempt])
    assert last is not None
    raise last


def validate_image(path: str | Path, settings: ImageSettings, *, compute_hash: bool = True) -> ValidationResult:
    p = long_path(path)
    r = ValidationResult(ok=False)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        r.reason_code = "NOT_FOUND"
        return r
    except OSError as exc:
        r.reason_code, r.detail = "NO_ACCESS", repr(exc)
        return r
    if not os.path.isfile(p):
        r.reason_code = "NOT_A_FILE"
        return r
    r.size_bytes = st.st_size
    if st.st_size == 0:
        r.reason_code = "EMPTY"
        return r
    if st.st_size > settings.max_file_bytes:
        r.reason_code, r.detail = "TOO_BIG_FILE", f"{st.st_size} bytes"
        return r
    try:
        head = _read_head(p)
    except OSError as exc:
        r.reason_code, r.detail = "NO_ACCESS", repr(exc)
        return r

    fmt = sniff_format(head)
    if fmt is None or fmt not in SUPPORTED_FORMATS:
        r.reason_code = "UNSUPPORTED_FORMAT"
        r.detail = f"signature={head[:12]!r} detected={fmt}"
        return r
    r.detected_format = fmt
    ext_fmt = EXT_FORMAT.get(Path(str(path)).suffix.lower())
    if ext_fmt != fmt:
        r.warnings.append(f"розширення не відповідає вмісту: фактичний формат {fmt}")

    # Header: dimensions/mode. Decompression-bomb warnings become errors.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(p) as im:
                r.width, r.height = im.size
                r.mode = im.mode
                r.has_alpha = im.mode in ("RGBA", "LA", "PA", "La", "RGBa") or (
                    im.mode == "P" and "transparency" in im.info)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        # Allowed only for JPEG, which can be decoded at reduced scale (draft).
        if fmt != "JPEG":
            r.reason_code, r.detail = "TOO_LARGE", repr(exc)
            return r
        r.warnings.append("дуже велике зображення: буде декодоване у зменшеному масштабі")
        dims = _header_dims_unchecked(p)
        if dims is None:
            r.reason_code, r.detail = "TOO_LARGE", repr(exc)
            return r
        r.width, r.height = dims
        r.mode = "RGB"
    except Exception as exc:  # noqa: BLE001 - header unreadable; fallback decoders may still work
        r.warnings.append(f"заголовок не читається Pillow: {exc!r}")
        r.detail = repr(exc)

    if r.width * r.height > settings.max_pixels and not any("зменшеному" in w for w in r.warnings):
        # explicit check: does not depend on Pillow's process-global limit
        if fmt != "JPEG":
            r.reason_code, r.detail = "TOO_LARGE", f"{r.width}x{r.height}"
            return r
        r.warnings.append("дуже велике зображення: буде декодоване у зменшеному масштабі")
    if r.width and (r.width > settings.max_side or r.height > settings.max_side):
        r.reason_code, r.detail = "TOO_LARGE", f"{r.width}x{r.height}"
        return r
    if r.width * r.height > settings.max_pixels * 64:
        # beyond what even 1/8 JPEG draft can reduce to the limit
        r.reason_code, r.detail = "TOO_LARGE", f"{r.width}x{r.height}"
        return r

    if compute_hash:
        try:
            r.sha256, r.hash_mode = sha256_file(Path(p))
        except OSError as exc:
            r.reason_code, r.detail = "NO_ACCESS", repr(exc)
            return r
    r.ok = True
    return r


def _header_dims_unchecked(p: str) -> tuple[int, int] | None:
    old = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = None
        with Image.open(p) as im:
            return im.size
    except Exception:  # noqa: BLE001
        return None
    finally:
        Image.MAX_IMAGE_PIXELS = old

"""Centralised, typed application settings.

All tunables live here (ARCHITECTURE.md §19). Every leaf field carries its
allowed range in ``metadata`` so that a damaged or hand-edited
``settings.json`` can never crash the application: invalid values fall back
to the default and a warning is reported.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from videogen.storage.atomic import atomic_write_text

log = logging.getLogger(__name__)


def _rng(lo: float, hi: float) -> dict[str, Any]:
    return {"min": lo, "max": hi}


def _choices(*values: str) -> dict[str, Any]:
    return {"choices": values}


@dataclass(frozen=True)
class VideoSettings:
    fps: int = field(default=30, metadata=_rng(10, 60))
    crf: int = field(default=20, metadata=_rng(12, 35))
    preset: str = field(
        default="medium",
        metadata=_choices("ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"),
    )
    horizontal_width: int = field(default=1920, metadata=_rng(320, 3840))
    horizontal_height: int = field(default=1080, metadata=_rng(240, 2160))
    vertical_width: int = field(default=1080, metadata=_rng(240, 2160))
    vertical_height: int = field(default=1920, metadata=_rng(320, 3840))
    encoder: str = field(default="libx264", metadata=_choices("libx264", "h264_nvenc"))
    partial_suffix: str = field(default=" [PARTIAL]", metadata={"max_len": 32})
    overwrite_existing: bool = False


@dataclass(frozen=True)
class EffectsSettings:
    ken_burns: bool = True
    max_zoom: float = field(default=1.08, metadata=_rng(1.0, 1.3))
    overscan: float = field(default=1.15, metadata=_rng(1.0, 1.5))
    transitions: bool = True
    transition_s: float = field(default=0.6, metadata=_rng(0.0, 3.0))
    transition_max_fraction: float = field(default=0.25, metadata=_rng(0.0, 0.5))


@dataclass(frozen=True)
class AudioSettings:
    sample_rate: int = field(default=48000, metadata=_rng(22050, 96000))
    channels: int = field(default=2, metadata=_rng(1, 2))
    aac_bitrate_kbps: int = field(default=192, metadata=_rng(64, 512))
    loudnorm: bool = False
    min_audio_s: float = field(default=0.5, metadata=_rng(0.1, 60.0))


@dataclass(frozen=True)
class ImageSettings:
    max_pixels: int = field(default=100_000_000, metadata=_rng(1_000_000, 1_000_000_000))
    max_side: int = field(default=30_000, metadata=_rng(1_000, 100_000))
    max_file_bytes: int = field(default=200 * 1024 * 1024, metadata=_rng(1024, 4 * 1024**3))
    on_invalid: str = field(default="skip_as_partial", metadata=_choices("skip_as_partial", "fail_job"))
    min_valid_images: int = field(default=1, metadata=_rng(1, 10_000))
    min_seconds_per_image: float = field(default=1.5, metadata=_rng(0.1, 60.0))
    cover_tolerance: float = field(default=0.10, metadata=_rng(0.0, 0.5))
    vertical_max_crop: float = field(default=0.15, metadata=_rng(0.0, 0.5))
    jpeg_quality: int = field(default=95, metadata=_rng(70, 100))


@dataclass(frozen=True)
class TimeoutPolicy:
    """Coefficients for derived timeouts (ARCHITECTURE.md §7.2).

    No operation uses a fixed "magic" timeout: each one is
    ``base + work / expected_rate * safety`` with an optional cap.
    """

    safety_factor: float = field(default=3.0, metadata=_rng(1.5, 20.0))
    image_base_s: float = field(default=10.0, metadata=_rng(1.0, 300.0))
    image_per_mpx_s: float = field(default=0.4, metadata=_rng(0.01, 10.0))
    image_max_s: float = field(default=120.0, metadata=_rng(5.0, 3600.0))
    probe_base_s: float = field(default=15.0, metadata=_rng(1.0, 300.0))
    probe_per_gb_s: float = field(default=10.0, metadata=_rng(0.0, 600.0))
    segment_base_s: float = field(default=15.0, metadata=_rng(1.0, 600.0))
    fps_min_floor: float = field(default=5.0, metadata=_rng(0.5, 1000.0))
    audio_base_s: float = field(default=20.0, metadata=_rng(1.0, 600.0))
    audio_min_speed: float = field(default=20.0, metadata=_rng(1.0, 1000.0))
    mux_base_s: float = field(default=30.0, metadata=_rng(1.0, 600.0))
    mux_min_speed: float = field(default=10.0, metadata=_rng(0.5, 1000.0))
    verify_base_s: float = field(default=30.0, metadata=_rng(1.0, 600.0))
    verify_min_speed: float = field(default=20.0, metadata=_rng(0.5, 1000.0))
    archive_base_s: float = field(default=30.0, metadata=_rng(1.0, 600.0))
    archive_min_mb_s: float = field(default=20.0, metadata=_rng(0.5, 10_000.0))
    cleanup_base_s: float = field(default=10.0, metadata=_rng(1.0, 600.0))
    cleanup_per_file_s: float = field(default=0.05, metadata=_rng(0.0, 10.0))
    stall_min_s: float = field(default=20.0, metadata=_rng(2.0, 3600.0))
    stall_default_s: float = field(default=30.0, metadata=_rng(2.0, 3600.0))
    watchdog_poll_s: float = field(default=0.5, metadata=_rng(0.05, 10.0))
    graceful_wait_s: float = field(default=3.0, metadata=_rng(0.1, 60.0))
    terminate_wait_s: float = field(default=3.0, metadata=_rng(0.1, 60.0))
    kill_wait_s: float = field(default=5.0, metadata=_rng(0.1, 60.0))
    stop_deadline_s: float = field(default=15.0, metadata=_rng(1.0, 300.0))


@dataclass(frozen=True)
class RetryPolicy:
    transient: int = field(default=2, metadata=_rng(0, 5))
    ffmpeg_crash: int = field(default=1, metadata=_rng(0, 3))
    timeout: int = field(default=1, metadata=_rng(0, 3))
    verification: int = field(default=1, metadata=_rng(0, 3))
    transient_backoff_s: tuple[float, ...] = (1.0, 3.0)


@dataclass(frozen=True)
class ResourceLimits:
    ram_available_min_mb: int = field(default=1024, metadata=_rng(128, 65536))
    ram_tree_max_mb: int = field(default=3072, metadata=_rng(256, 131072))
    disk_reserve_mb: int = field(default=1024, metadata=_rng(64, 1_048_576))
    cpu_max_pct: float = field(default=85.0, metadata=_rng(10.0, 100.0))
    resource_wait_max_s: float = field(default=600.0, metadata=_rng(5.0, 86400.0))
    monitor_interval_s: float = field(default=2.0, metadata=_rng(0.2, 60.0))


@dataclass(frozen=True)
class ConcurrencySettings:
    max_parallel_jobs: int = field(default=1, metadata=_rng(1, 4))
    prefetch_images: int = field(default=4, metadata=_rng(0, 32))
    image_worker_recycle_after: int = field(default=200, metadata=_rng(1, 100_000))


@dataclass(frozen=True)
class PathSettings:
    input_dir: str = ""
    output_dir: str = ""
    workspace_dir: str = ""


@dataclass(frozen=True)
class CleanupSettings:
    keep_failed_workspace: bool = False
    diagnostics_max_jobs: int = field(default=200, metadata=_rng(1, 100_000))
    diagnostics_max_mb: int = field(default=500, metadata=_rng(1, 1_000_000))
    remove_retries: int = field(default=3, metadata=_rng(0, 20))


@dataclass(frozen=True)
class LoggingSettings:
    level: str = field(default="INFO", metadata=_choices("DEBUG", "INFO", "WARNING", "ERROR"))
    max_bytes: int = field(default=10 * 1024 * 1024, metadata=_rng(64 * 1024, 1024**3))
    backup_count: int = field(default=5, metadata=_rng(1, 100))
    keep_job_logs: bool = False


@dataclass(frozen=True)
class ArchiveSettings:
    enabled: bool = True
    include_inputs: bool = True
    include_video: bool = False
    max_size_mb: int = field(default=4096, metadata=_rng(1, 1_048_576))


@dataclass(frozen=True)
class Settings:
    video: VideoSettings = field(default_factory=VideoSettings)
    effects: EffectsSettings = field(default_factory=EffectsSettings)
    audio: AudioSettings = field(default_factory=AudioSettings)
    images: ImageSettings = field(default_factory=ImageSettings)
    timeouts: TimeoutPolicy = field(default_factory=TimeoutPolicy)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    concurrency: ConcurrencySettings = field(default_factory=ConcurrencySettings)
    paths: PathSettings = field(default_factory=PathSettings)
    cleanup: CleanupSettings = field(default_factory=CleanupSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    archive: ArchiveSettings = field(default_factory=ArchiveSettings)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------
# (de)serialisation with per-field validation
# --------------------------------------------------------------------------

def _coerce_leaf(f: dataclasses.Field[Any], default: Any, raw: Any, where: str,
                 warnings: list[str]) -> Any:
    meta = f.metadata
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        warnings.append(f"{where}: очікувалось true/false, отримано {raw!r}")
        return default
    if isinstance(default, int):
        if isinstance(raw, bool) or not isinstance(raw, int):
            warnings.append(f"{where}: очікувалось ціле число, отримано {raw!r}")
            return default
        value: Any = raw
    elif isinstance(default, float):
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            warnings.append(f"{where}: очікувалось число, отримано {raw!r}")
            return default
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            warnings.append(f"{where}: недопустиме значення {raw!r}")
            return default
    elif isinstance(default, str):
        if not isinstance(raw, str):
            warnings.append(f"{where}: очікувався рядок, отримано {raw!r}")
            return default
        value = raw
        if "max_len" in meta and len(value) > meta["max_len"]:
            warnings.append(f"{where}: рядок задовгий")
            return default
    elif isinstance(default, tuple):
        if not isinstance(raw, (list, tuple)) or not all(
                isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 3600
                for x in raw) or len(raw) > 10:
            warnings.append(f"{where}: некоректний список {raw!r}")
            return default
        return tuple(float(x) for x in raw)
    else:  # pragma: no cover - guarded by test_all_leaf_types_supported
        warnings.append(f"{where}: непідтримуваний тип")
        return default

    if "choices" in meta and value not in meta["choices"]:
        warnings.append(f"{where}: {value!r} не входить до {list(meta['choices'])}")
        return default
    if "min" in meta and value < meta["min"]:
        warnings.append(f"{where}: {value!r} < мінімуму {meta['min']}")
        return default
    if "max" in meta and value > meta["max"]:
        warnings.append(f"{where}: {value!r} > максимуму {meta['max']}")
        return default
    return value


def _from_dict(cls: type[Any], data: Any, where: str, warnings: list[str]) -> Any:
    defaults = cls()
    if not isinstance(data, dict):
        if data is not None:
            warnings.append(f"{where or 'settings'}: очікувався об'єкт, використано значення за замовчуванням")
        return defaults
    kwargs: dict[str, Any] = {}
    known = set()
    for f in dataclasses.fields(cls):
        known.add(f.name)
        default = getattr(defaults, f.name)
        key = f"{where}.{f.name}" if where else f.name
        if f.name not in data:
            kwargs[f.name] = default
        elif dataclasses.is_dataclass(default):
            kwargs[f.name] = _from_dict(  # invariant-ok: depth bounded by static dataclass nesting
                type(default), data[f.name], key, warnings)
        else:
            kwargs[f.name] = _coerce_leaf(f, default, data[f.name], key, warnings)
    for extra in sorted(set(data) - known):
        warnings.append(f"{where + '.' if where else ''}{extra}: невідомий ключ проігноровано")
    return cls(**kwargs)


def settings_from_dict(data: Any) -> tuple[Settings, list[str]]:
    """Build Settings from untrusted data. Never raises."""
    warnings: list[str] = []
    settings = _from_dict(Settings, data, "", warnings)
    return settings, warnings


def load_settings(path: Path) -> tuple[Settings, list[str]]:
    """Load settings from JSON. A missing or broken file yields defaults."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Settings(), []
    except UnicodeDecodeError as exc:
        return Settings(), [_set_aside(path, f"файл налаштувань пошкоджений ({exc})")]
    except OSError as exc:
        msg = f"не вдалося прочитати {path}: {exc}"
        log.warning("settings: %s", msg)
        return Settings(), [msg]
    try:
        data = json.loads(text)
    except ValueError as exc:
        return Settings(), [_set_aside(path, f"файл налаштувань пошкоджений ({exc})")]
    settings, warnings = settings_from_dict(data)
    for w in warnings:
        log.warning("settings: %s", w)
    return settings, warnings


def _set_aside(path: Path, reason: str) -> str:
    """An unreadable settings file is kept as ``settings.json.corrupt-<time>``
    (the next save would otherwise overwrite it silently) and logged."""
    import time
    backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        os.replace(path, backup)
        msg = f"{reason}; використано значення за замовчуванням, файл збережено як {backup.name}"
    except OSError as exc:
        msg = f"{reason}; використано значення за замовчуванням (не вдалося зберегти копію: {exc})"
    log.warning("settings: %s", msg)
    return msg


def save_settings(settings: Settings, path: Path) -> None:
    atomic_write_text(path, json.dumps(settings.to_dict(), ensure_ascii=False, indent=2))

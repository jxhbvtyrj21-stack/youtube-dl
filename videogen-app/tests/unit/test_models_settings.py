from __future__ import annotations

import json
import math

import pytest

from videogen.config.settings import Settings, load_settings, save_settings, settings_from_dict
from videogen.core import timeouts
from videogen.core.errors import (
    ErrorClass, FFmpegCrashError, InputError, OperationTimeoutError, TransientError, VerificationError,
    classify, retry_budget,
)
from videogen.config.settings import RetryPolicy, TimeoutPolicy
from videogen.core.models import (
    ALLOWED_BATCH_TRANSITIONS, ALLOWED_TRANSITIONS, BatchState, IllegalTransition, JobStatus,
    TERMINAL_STATUSES, check_batch_transition, check_transition,
)


# ---------------------------------------------------------------- state machines

@pytest.mark.parametrize("src", list(JobStatus))
@pytest.mark.parametrize("dst", list(JobStatus))
def test_job_transition_table_is_enforced(src, dst):
    if dst in ALLOWED_TRANSITIONS[src]:
        check_transition(src, dst)
    else:
        with pytest.raises(IllegalTransition):
            check_transition(src, dst)


def test_success_is_absolutely_terminal_and_partial_is_not_success():
    assert ALLOWED_TRANSITIONS[JobStatus.SUCCESS] == frozenset()
    assert JobStatus.PARTIAL in TERMINAL_STATUSES
    assert JobStatus.PARTIAL is not JobStatus.SUCCESS
    # automatic code can never move a terminal state anywhere but a manual re-queue
    for s in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[s] <= {JobStatus.QUEUED}


def test_every_status_is_reachable_from_queued():
    seen = {JobStatus.QUEUED}
    frontier = [JobStatus.QUEUED]
    for _ in range(len(JobStatus)):
        frontier = [d for s in frontier for d in ALLOWED_TRANSITIONS[s] if d not in seen]
        seen.update(frontier)
    assert seen == set(JobStatus)


@pytest.mark.parametrize("src", list(BatchState))
@pytest.mark.parametrize("dst", list(BatchState))
def test_batch_transition_table(src, dst):
    if dst in ALLOWED_BATCH_TRANSITIONS[src]:
        check_batch_transition(src, dst)
    else:
        with pytest.raises(IllegalTransition):
            check_batch_transition(src, dst)


def test_batch_always_returns_to_idle():
    # from every state there is a path to IDLE (no dead ends)
    for start in BatchState:
        seen = {start}
        frontier = [start]
        for _ in range(len(BatchState)):
            frontier = [d for s in frontier for d in ALLOWED_BATCH_TRANSITIONS[s] if d not in seen]
            seen.update(frontier)
        assert BatchState.IDLE in seen or start is BatchState.IDLE, start


# ---------------------------------------------------------------- settings

def test_settings_roundtrip(tmp_path):
    s = Settings()
    p = tmp_path / "settings.json"
    save_settings(s, p)
    loaded, warnings = load_settings(p)
    assert loaded == s
    assert warnings == []


def test_settings_invalid_values_fall_back_to_defaults():
    data = {
        "video": {"fps": 1000, "crf": "high", "preset": "turbo", "overwrite_existing": "yes"},
        "effects": {"max_zoom": float("nan")},
        "images": {"on_invalid": "explode", "max_pixels": True},
        "retry": {"transient_backoff_s": ["x"]},
        "unknown_section": {},
        "timeouts": 5,
    }
    s, warnings = settings_from_dict(data)
    d = Settings()
    assert s.video.fps == d.video.fps
    assert s.video.crf == d.video.crf
    assert s.video.preset == d.video.preset
    assert s.video.overwrite_existing is False
    assert s.effects.max_zoom == d.effects.max_zoom
    assert s.images.on_invalid == "skip_as_partial"
    assert s.images.max_pixels == d.images.max_pixels
    assert s.retry.transient_backoff_s == d.retry.transient_backoff_s
    assert s.timeouts == d.timeouts
    assert len(warnings) >= 9


def test_settings_valid_override_applies():
    s, w = settings_from_dict({"video": {"fps": 25}, "retry": {"transient_backoff_s": [0, 0.5]}})
    assert s.video.fps == 25
    assert s.retry.transient_backoff_s == (0.0, 0.5)
    assert w == []


@pytest.mark.parametrize("content", [b"", b"{not json", b"\xff\xfe\x00garbage", b"[1,2,3]"])
def test_broken_settings_file_never_crashes(tmp_path, content):
    p = tmp_path / "settings.json"
    p.write_bytes(content)
    s, _ = load_settings(p)
    assert s == Settings()


def test_missing_settings_file_gives_defaults(tmp_path):
    s, w = load_settings(tmp_path / "nope.json")
    assert s == Settings() and w == []


# ---------------------------------------------------------------- errors / retry

def test_retry_budgets_match_requirements():
    p = RetryPolicy()
    assert retry_budget(ErrorClass.TRANSIENT, p) == 2
    assert retry_budget(ErrorClass.FFMPEG_CRASH, p) == 1
    assert retry_budget(ErrorClass.TIMEOUT, p) == 1
    assert retry_budget(ErrorClass.VERIFICATION, p) == 1
    for cls in (ErrorClass.INPUT, ErrorClass.RESOURCE, ErrorClass.INTERNAL, ErrorClass.CANCELLED,
                ErrorClass.FFMPEG_UNAVAILABLE, ErrorClass.ARCHIVE):
        assert retry_budget(cls, p) == 0


def test_classify():
    assert classify(InputError("x")).error_class == "INPUT"
    assert classify(TransientError("x")).error_class == "TRANSIENT"
    assert classify(FFmpegCrashError("x")).error_class == "FFMPEG_CRASH"
    assert classify(OperationTimeoutError("x")).error_class == "TIMEOUT"
    assert classify(VerificationError("x")).error_class == "VERIFICATION"
    assert classify(PermissionError("locked")).error_class == "TRANSIENT"
    assert classify(OSError(28, "No space left")).code == "DISK_SPACE"
    assert classify(MemoryError()).code == "OUT_OF_MEMORY"
    info = classify(ZeroDivisionError("x"))
    assert info.error_class == "INTERNAL"
    assert "ZeroDivisionError" in info.detail      # traceback kept for the log
    assert "ZeroDivisionError" not in info.message  # not shown to the user


# ---------------------------------------------------------------- timeouts

def test_timeouts_scale_with_work_and_are_finite():
    p = TimeoutPolicy()
    small = timeouts.segment_render(p, 30, 10)
    big = timeouts.segment_render(p, 3000, 10)
    assert small.hard_s < big.hard_s
    assert math.isfinite(big.hard_s) and big.stall_s and big.stall_s >= p.stall_min_s
    # fps_min below the floor is clamped (no division blow-up)
    assert math.isfinite(timeouts.segment_render(p, 100, 0).hard_s)
    assert timeouts.image_normalize(p, 10**12).hard_s == p.image_max_s
    assert timeouts.mux(p, 3600).hard_s > timeouts.mux(p, 10).hard_s
    assert timeouts.calibrated_fps_min(p, 120) == 30
    assert timeouts.calibrated_fps_min(p, None) == p.fps_min_floor
    assert timeouts.calibrated_fps_min(p, 4) == p.fps_min_floor


def test_settings_json_is_utf8_and_human_readable(tmp_path):
    p = tmp_path / "s.json"
    save_settings(Settings(), p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["video"]["fps"] == 30


def test_settings_roundtrip_through_dict_without_warnings():
    """GUI -> Engine passes Settings.to_dict() (tuples, not lists)."""
    s = Settings()
    back, warnings = settings_from_dict(s.to_dict())
    assert back == s and warnings == []


@pytest.mark.parametrize("raw", [b"{ not json", b"\xff\xfe\x00garbage"])
def test_unreadable_settings_file_is_set_aside_not_lost(tmp_path, raw, caplog):
    """Regression (gap B): a damaged settings.json was replaced by defaults
    without any record; the next save overwrote the user's file."""
    p = tmp_path / "settings.json"
    p.write_bytes(raw)
    s, warnings = load_settings(p)
    assert s == Settings() and warnings
    backups = list(tmp_path.glob("settings.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    assert any("settings" in r.getMessage() for r in caplog.records)

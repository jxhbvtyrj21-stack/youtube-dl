from __future__ import annotations

import dataclasses
import json
import os
import zipfile
from pathlib import Path

import pytest

from videogen.config.settings import ArchiveSettings, ImageSettings, TimeoutPolicy
from videogen.core import events as ev
from videogen.core.engine import Engine
from videogen.core.errors import ArchiveError, VerificationError
from videogen.core.models import BatchState, JobStatus
from videogen.ffmpeg_ctl.locator import FFmpegTools
from videogen.ffmpeg_ctl.process_manager import REGISTRY
from videogen.media.media_validator import probe_media
from videogen.media.timeline import total_frames_for
from tests.fixtures import factory as F
from tests.helpers import wait_until
from tests.pipeline_support import Events, ffmpeg_wrapper, make_job_folder, small_settings, start_cmd


@pytest.fixture()
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("input", "output", "ws", "appdata")}
    d["input"].mkdir()
    return d


def make_engine(dirs, settings=None, **kw):
    events = Events()
    eng = Engine(dirs["appdata"], settings or small_settings(), events, **kw)
    eng.startup()
    return eng, events


def run_batch(eng, dirs, orientation="16:9", mode="A", timeout=240):
    batch = eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"], orientation, mode))
    assert batch is not None
    assert eng.wait_idle(timeout)
    return {j.name: j for j in eng.state.list_jobs(batch_id=batch)}


def frames_of(path):
    return probe_media(F.FFPROBE, Path(path), TimeoutPolicy()).video_frames


def assert_clean(dirs):
    assert REGISTRY.snapshot() == {}
    leftovers = [p for p in dirs["ws"].rglob("*") if p.is_file() and p.name != ".videogen-workspace"]
    assert leftovers == [], leftovers
    assert not list(dirs["output"].glob(".*.part"))


@pytest.fixture()
def engine_ctx(dirs):
    holder = {}
    yield holder
    if "eng" in holder:
        holder["eng"].shutdown()


def test_single_job_success(dirs, engine_ctx):
    make_job_folder(dirs["input"], "Відео 1 (тест) & #%+", n_images=4, audio_s=3.3)
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    jobs = run_batch(eng, dirs)
    job = jobs["Відео 1 (тест) & #%+"]
    assert job.status is JobStatus.SUCCESS, job.error
    out = Path(job.output_file)
    assert out.parent == dirs["output"] and out.name == "Відео 1 (тест) & #%+.mp4"
    assert frames_of(out) == total_frames_for(3.3, 24)
    archive = dirs["output"] / "_archive" / "Відео 1 (тест) & #%+.zip"
    with zipfile.ZipFile(archive) as zf:
        names = zf.namelist()
        assert "manifest.json" in names and sum(n.startswith("inputs/") for n in names) == 5
        assert not any(n.endswith(".mp4") for n in names)       # video not duplicated by default
        man = json.loads(zf.read("manifest.json"))
        assert man["status"] == "SUCCESS" and man["skipped_count"] == 0
    stages = [e.stage.value for e in events.of(ev.JobStageChanged)]
    assert stages[:3] == ["NONE", "VALIDATING", "AUDIO"] or "RENDERING" in stages
    progress = events.of(ev.JobProgress)
    assert progress and max(p.percent for p in progress) > 90
    assert events.of(ev.BatchStateChanged)[-1].state is BatchState.IDLE
    assert_clean(dirs)


def test_vertical_output(dirs, engine_ctx):
    make_job_folder(dirs["input"], "vert", n_images=3, audio_s=2.5, landscape=True)
    eng, _ = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs, orientation="9:16")["vert"]
    assert job.status is JobStatus.SUCCESS
    info = probe_media(F.FFPROBE, Path(job.output_file), TimeoutPolicy())
    assert (info.width, info.height) == (240, 426)


def test_bad_images_give_partial_not_success(dirs, engine_ctx):
    make_job_folder(dirs["input"], "partial", n_images=5, audio_s=3.0, bad=(1, 3))
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["partial"]
    assert job.status is JobStatus.PARTIAL
    assert job.skipped_images == 2
    assert Path(job.output_file).name == "partial [PARTIAL].mp4"
    skipped = events.of(ev.ImageSkipped)
    assert sorted(e.index for e in skipped) == [2, 4]
    log_text = (dirs["appdata"] / "logs" / "application.log").read_text(encoding="utf-8")
    assert "Не вдалося використати зображення №2" in log_text
    diag = json.loads((dirs["appdata"] / "diagnostics" / job.job_id / "manifest.json").read_text(encoding="utf-8"))
    assert diag["status"] == "PARTIAL"
    assert [i["status"] for i in diag["input_files"]].count("INVALID") == 2
    assert_clean(dirs)


def test_one_bad_job_does_not_stop_batch(dirs, engine_ctx):
    make_job_folder(dirs["input"], "a_good", n_images=3, audio_s=2)
    make_job_folder(dirs["input"], "b_bad_audio", n_images=3, audio="bad")
    make_job_folder(dirs["input"], "c_all_images_bad", n_images=2, bad=(0, 1))
    make_job_folder(dirs["input"], "d_no_audio", n_images=2, audio="none")
    make_job_folder(dirs["input"], "e_good", n_images=2, audio_s=2)
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    jobs = run_batch(eng, dirs)
    st = {k: v.status for k, v in jobs.items()}
    assert st == {"a_good": JobStatus.SUCCESS, "b_bad_audio": JobStatus.FAILED,
                  "c_all_images_bad": JobStatus.FAILED, "d_no_audio": JobStatus.FAILED,
                  "e_good": JobStatus.SUCCESS}
    assert jobs["b_bad_audio"].error.code == "INVALID_AUDIO" and jobs["b_bad_audio"].attempts == 1
    assert jobs["c_all_images_bad"].error.code == "NO_VALID_IMAGES"
    assert "аудіофайлу" in jobs["d_no_audio"].error.message
    counters = events.of(ev.BatchCounters)[-1]
    assert (counters.succeeded, counters.failed) == (2, 3)
    assert_clean(dirs)


def test_stop_mid_render(dirs, engine_ctx):
    make_job_folder(dirs["input"], "long", n_images=8, audio_s=16)
    make_job_folder(dirs["input"], "next", n_images=2, audio_s=2)
    eng, events = make_engine(dirs, small_settings(video=dataclasses.replace(small_settings().video,
                                                                               preset="medium")))
    engine_ctx["eng"] = eng
    eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"]))
    assert wait_until(lambda: any(e.stage.value == "RENDERING" for e in events.of(ev.JobStageChanged)), 60)
    eng.handle(ev.Stop())
    assert eng.wait_idle(30)
    jobs = {j.name: j.status for j in eng.state.list_jobs()}
    assert jobs == {"long": JobStatus.CANCELLED, "next": JobStatus.CANCELLED}
    assert not list(dirs["output"].glob("*.mp4"))
    assert_clean(dirs)


def test_pause_and_resume(dirs, engine_ctx):
    make_job_folder(dirs["input"], "p", n_images=6, audio_s=6)
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"]))
    assert wait_until(lambda: any(e.stage.value in ("NORMALIZING", "RENDERING")
                                  for e in events.of(ev.JobStageChanged)), 60)
    eng.handle(ev.Pause())
    assert wait_until(lambda: eng.batch_state is BatchState.PAUSED, 60)
    assert REGISTRY.snapshot() == {} or all(v == "ImageWorker" for v in REGISTRY.snapshot().values())
    eng.handle(ev.Resume())
    assert eng.wait_idle(240)
    assert eng.state.list_jobs()[0].status is JobStatus.SUCCESS


def test_archive_failure_marks_job_failed_but_keeps_video(dirs, engine_ctx, monkeypatch):
    from videogen.core import pipeline as pl

    def boom(*a, **k):
        raise ArchiveError("Не вдалося створити архів.", code="ARCHIVE_FAILED")
    monkeypatch.setattr(pl, "_run_archive_process", boom)
    make_job_folder(dirs["input"], "arc", n_images=2, audio_s=2)
    eng, _ = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["arc"]
    assert job.status is JobStatus.FAILED and job.error.error_class == "ARCHIVE"
    assert (dirs["output"] / "arc.mp4").exists()          # the verified video stays


def test_archive_size_limit_skips_with_warning(dirs, engine_ctx, monkeypatch):
    from videogen.core import pipeline as pl
    monkeypatch.setattr(pl, "estimate_size", lambda entries: 5 * 1024 * 1024)
    make_job_folder(dirs["input"], "big", n_images=2, audio_s=2)
    eng, _ = make_engine(dirs, small_settings(archive=ArchiveSettings(enabled=True, max_size_mb=1)))
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["big"]
    assert job.status is JobStatus.SUCCESS
    assert not (dirs["output"] / "_archive").exists() or not list((dirs["output"] / "_archive").glob("*.zip"))


def test_output_validation_failure_is_never_success(dirs, engine_ctx, monkeypatch):
    from videogen.core import pipeline as pl
    calls = {"n": 0}

    def bad_verify(*a, **k):
        calls["n"] += 1
        raise VerificationError("Відео не пройшло перевірку: тест.", code="VERIFICATION_FAILED")
    monkeypatch.setattr(pl, "verify_output", bad_verify)
    make_job_folder(dirs["input"], "v", n_images=2, audio_s=2)
    eng, _ = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["v"]
    assert job.status is JobStatus.FAILED and job.error.error_class == "VERIFICATION"
    assert calls["n"] == 2 and job.attempts == 2               # exactly one retry
    assert not list(dirs["output"].glob("*.mp4"))
    assert_clean(dirs)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrapper")
@pytest.mark.parametrize("behaviour,error_class", [("hang", "TIMEOUT"), ("crash", "FFMPEG_CRASH")])
def test_ffmpeg_hang_and_crash_are_bounded(dirs, engine_ctx, tmp_path, behaviour, error_class):
    ff, fp = ffmpeg_wrapper(tmp_path, behaviour)
    s = small_settings(timeouts=TimeoutPolicy(segment_base_s=2, stall_min_s=1.5, fps_min_floor=500,
                                              watchdog_poll_s=0.1, graceful_wait_s=0.3, terminate_wait_s=0.3,
                                              kill_wait_s=3))
    make_job_folder(dirs["input"], "x", n_images=2, audio_s=2)
    make_job_folder(dirs["input"], "y", n_images=2, audio_s=2)
    eng, _ = make_engine(dirs, s, tools=FFmpegTools(ff, fp, "wrapped"))
    engine_ctx["eng"] = eng
    jobs = run_batch(eng, dirs, timeout=120)
    for j in jobs.values():
        assert j.status is JobStatus.FAILED and j.error.error_class == error_class
        assert j.attempts == 2                                  # one retry, not infinite
    assert REGISTRY.snapshot() == {}
    if behaviour == "hang":
        snaps = list((dirs["appdata"] / "diagnostics").rglob("snapshot-*.json"))
        assert snaps, "a diagnostic snapshot must be written on stall"


def test_ffmpeg_unavailable(dirs, engine_ctx, monkeypatch):
    monkeypatch.delenv("VIDEOGEN_FFMPEG_DIR", raising=False)
    make_job_folder(dirs["input"], "x", n_images=2, audio_s=2)
    eng, events = make_engine(dirs, tools=None)
    engine_ctx["eng"] = eng
    os.environ["PATH_BACKUP"] = os.environ["PATH"]
    try:
        os.environ["PATH"] = str(dirs["appdata"])
        eng._tools = None
        assert eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"])) is None
    finally:
        os.environ["PATH"] = os.environ.pop("PATH_BACKUP")
    errs = events.of(ev.EngineError)
    assert errs and "FFmpeg не знайдено" in errs[-1].message


def test_insufficient_disk_preflight(dirs, engine_ctx, monkeypatch):
    """Monitor sees just enough space, but the job's own estimate does not fit:
    the job must not start rendering, and the batch pauses instead of burning
    through the remaining jobs with the same error."""
    from videogen.workers import resource_monitor as rm
    monkeypatch.setattr(rm, "free_disk_bytes", lambda p: 66 * 1024 * 1024)
    make_job_folder(dirs["input"], "a", n_images=2, audio_s=2)
    make_job_folder(dirs["input"], "b", n_images=2, audio_s=2)
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"]))
    assert wait_until(lambda: eng.batch_state is BatchState.PAUSED, 60)
    jobs = {j.name: j for j in eng.state.list_jobs()}
    assert jobs["a"].status is JobStatus.FAILED and jobs["a"].error.code == "DISK_SPACE"
    assert "Недостатньо вільного місця на диску" in jobs["a"].error.message
    assert jobs["b"].status is JobStatus.QUEUED
    stages = [e.stage.value for e in events.of(ev.JobStageChanged) if e.job_id == jobs["a"].job_id]
    assert "RENDERING" not in stages
    eng.handle(ev.Stop())
    assert eng.wait_idle(30)
    assert eng.state.get_job(jobs["b"].job_id).status is JobStatus.CANCELLED


def test_low_resources_wait_is_bounded(dirs, engine_ctx, monkeypatch):
    from videogen.config.settings import ResourceLimits
    from videogen.workers import resource_monitor as rm
    monkeypatch.setattr(rm, "free_disk_bytes", lambda p: 10 * 1024 * 1024)
    make_job_folder(dirs["input"], "a", n_images=2, audio_s=2)
    s = small_settings(resources=ResourceLimits(ram_available_min_mb=128, disk_reserve_mb=64,
                                                resource_wait_max_s=1.0, monitor_interval_s=0.2))
    eng, events = make_engine(dirs, s)
    engine_ctx["eng"] = eng
    eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"]))
    assert eng.wait_idle(30)
    job = eng.state.list_jobs()[0]
    assert job.status is JobStatus.CANCELLED and job.error.code == "RESOURCE_TIMEOUT"
    assert any(e.kind == "disk" for e in events.of(ev.ResourceWarning))
    assert BatchState.RESOURCE_WAIT in [e.state for e in events.of(ev.BatchStateChanged)]


def test_mode_b_with_fake_providers(dirs, engine_ctx):
    from videogen.providers.fake import FakeImages, FakeTTS
    d = dirs["input"] / "story"
    d.mkdir()
    (d / "script.txt").write_text("Перша сцена.\n\nДруга сцена.\n\nТретя сцена.", encoding="utf-8")
    (d / "prompts.txt").write_text("a red fox\na blue lake\na green hill\n", encoding="utf-8")
    tts, imgs = FakeTTS(min_s=3.0), FakeImages()
    eng, _ = make_engine(dirs, tts=tts, image_provider=imgs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs, mode="B")["story"]
    assert job.status is JobStatus.SUCCESS, job.error
    assert (tts.calls, imgs.calls) == (1, 3)
    # second run: everything comes from the cache, nothing is regenerated
    run_batch(eng, dirs, mode="B")
    assert (tts.calls, imgs.calls) == (1, 3)


def test_mode_b_without_provider_fails_clearly(dirs, engine_ctx):
    d = dirs["input"] / "story"
    d.mkdir()
    (d / "script.txt").write_text("Сцена.", encoding="utf-8")
    (d / "prompts.txt").write_text("x\n", encoding="utf-8")
    eng, _ = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs, mode="B")["story"]
    assert job.status is JobStatus.FAILED and job.error.code == "PROVIDER_NOT_CONFIGURED"


def test_existing_output_is_never_overwritten(dirs, engine_ctx):
    make_job_folder(dirs["input"], "same", n_images=2, audio_s=2)
    (dirs["output"]).mkdir()
    (dirs["output"] / "same.mp4").write_bytes(b"user file")
    eng, _ = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["same"]
    assert Path(job.output_file).name == "same (2).mp4"
    assert (dirs["output"] / "same.mp4").read_bytes() == b"user file"


def test_fail_job_policy(dirs, engine_ctx):
    make_job_folder(dirs["input"], "strict", n_images=3, audio_s=2, bad=(1,))
    eng, _ = make_engine(dirs, small_settings(images=ImageSettings(min_seconds_per_image=0.5,
                                                                    on_invalid="fail_job")))
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["strict"]
    assert job.status is JobStatus.FAILED and job.error.code == "INVALID_IMAGES"
    assert "№2" in job.error.message


def test_missing_input_folder(dirs, engine_ctx):
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    assert eng.start_batch(start_cmd(dirs["input"] / "немає такої", dirs["output"], dirs["ws"])) is None
    errs = events.of(ev.EngineError)
    assert errs and "Вхідну папку не знайдено" in errs[-1].message
    assert eng.batch_state is BatchState.IDLE


def test_empty_input_folder(dirs, engine_ctx):
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    assert eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"])) is None
    assert "не знайдено матеріалів" in events.of(ev.EngineError)[-1].message


def test_input_file_vanishes_after_discovery(dirs, engine_ctx, monkeypatch):
    """An image deleted between discovery and processing is reported, not fatal."""
    from videogen.core import engine as engine_mod
    d = make_job_folder(dirs["input"], "gone", n_images=3, audio_s=2)
    real = engine_mod.discover

    def discover_then_delete(*a, **k):
        res = real(*a, **k)
        (d / "img_002.png").unlink()
        return res
    monkeypatch.setattr(engine_mod, "discover", discover_then_delete)
    eng, events = make_engine(dirs)
    engine_ctx["eng"] = eng
    job = run_batch(eng, dirs)["gone"]
    assert job.status is JobStatus.PARTIAL
    assert any(e.index == 2 for e in events.of(ev.ImageSkipped))

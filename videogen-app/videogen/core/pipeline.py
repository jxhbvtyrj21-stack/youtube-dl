"""The media pipeline for one job (ARCHITECTURE.md §4).

VALIDATING -> [GENERATING] -> AUDIO -> NORMALIZING -> TIMELINE -> RENDERING
-> MUXING -> VERIFYING -> FINALIZING -> ARCHIVING; CLEANUP in ``finalize``.

Every stage is recorded in the state DB *before* it starts (write-ahead) and
its results in ``manifest.json``, so a crash at any point resumes from the
last verified artefact: normalised images and rendered segments are reused,
a final file that was already moved is recognised by its hash.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from videogen.applog.diagnostics import write_snapshot
from videogen.applog.logger import LogSystem
from videogen.config.settings import Settings
from videogen.core import events as ev
from videogen.core import timeouts
from videogen.core.errors import (
    ArchiveError, ErrorClass, InputError, JobCancelledError, VerificationError,
)
from videogen.core.job_manager import JobContext, JobOutcome
from videogen.core.models import (
    ErrorInfo, ImageItem, ImageStatus, JobConfig, JobState, JobStatus, Mode, Orientation, Stage,
)
from videogen.core.state_manager import StateManager
from videogen.ffmpeg_ctl.locator import FFmpegTools
from videogen.ffmpeg_ctl.process_manager import JobObject, kill_tree
from videogen.ffmpeg_ctl.progress import FFmpegProgress
from videogen.ffmpeg_ctl.runner import raise_for, run_ffmpeg
from videogen.media import canvas as canvas_mod
from videogen.media.archiver import ArchiveEntry, estimate_size
from videogen.media.audio_processor import AudioInfo, normalize_audio, probe_audio, wav_duration
from videogen.media.media_validator import ExpectedOutput, probe_media, verify_output
from videogen.media.timeline import Timeline, build_timeline
from videogen.media.video_renderer import EncodeSettings, concat_list, mux_argv, segment_argv
from videogen.providers.base import AssetCache, ImageGenProvider, TTSProvider, read_prompts, split_script
from videogen.storage.atomic import atomic_write_text, replace_with_retry
from videogen.storage.cleanup import count_files, remove_tree
from videogen.storage.manifest import JobManifest, SegmentRecord, read_manifest, utc_now, write_manifest
from videogen.storage.workspace import JobWorkspace
from videogen.utils.hashing import sha256_file
from videogen.utils.paths import safe_filename, unique_output_path
from videogen.utils.system import same_volume
from videogen.workers.image_worker import ImageWorkerClient
from videogen.workers.resource_monitor import check_disk, estimate_job_disk

log = logging.getLogger(__name__)

PROGRESS_INTERVAL_S = 0.2


def _test_hook(name: str) -> str:
    """Fault injection for the production failure suite. Inactive unless
    VIDEOGEN_TEST_HOOKS=1 is set explicitly (never in normal use). A value
    may be given directly (VIDEOGEN_TEST_<NAME>) or via a control file
    (VIDEOGEN_TEST_<NAME>_FILE) so a running engine can be steered."""
    if os.environ.get("VIDEOGEN_TEST_HOOKS") != "1":
        return ""
    ctl = os.environ.get(f"VIDEOGEN_TEST_{name}_FILE")
    if ctl:
        try:
            return Path(ctl).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.environ.get(f"VIDEOGEN_TEST_{name}", "")


def _stall_requested(spec: str, job_name: str, index: int) -> bool:
    """Spec "<index>" or "<job-name substring>:<index>"."""
    if not spec:
        return False
    name, _, idx = spec.rpartition(":")
    return idx == str(index) and (not name or name in job_name)


def _stalling_ffmpeg(ffmpeg: str) -> list[str]:
    """A real ffmpeg that waits forever for input on stdin: alive, no progress."""
    return [ffmpeg, "-hide_banner", "-v", "warning", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "64x64",
            "-i", "pipe:0", "-f", "null", "-", "-progress", "pipe:1", "-nostats"]


def _corrupt_output(mode: str, path: Path, ffmpeg: str) -> None:
    import subprocess
    if mode == "missing":
        path.unlink(missing_ok=True)
    elif mode == "zero":
        path.write_bytes(b"")
    elif mode == "invalid":
        path.write_bytes(os.urandom(200_000))
    elif mode == "incomplete":
        data = path.read_bytes()
        path.write_bytes(data[: len(data) // 2])
    elif mode in ("noaudio", "duration"):
        tmp = path.with_name("hook.tmp.mp4")
        extra = ["-an"] if mode == "noaudio" else ["-t", "1"]
        subprocess.run([ffmpeg, "-v", "error", "-y", "-i", str(path), "-c", "copy", *extra, str(tmp)],
                       check=True, timeout=120, stdin=subprocess.DEVNULL)
        os.replace(tmp, path)
STAGE_WEIGHTS: dict[Stage, tuple[float, float]] = {
    Stage.VALIDATING: (0, 1), Stage.GENERATING: (1, 5), Stage.AUDIO: (5, 8),
    Stage.NORMALIZING: (8, 25), Stage.TIMELINE: (25, 25), Stage.RENDERING: (25, 90),
    Stage.MUXING: (90, 95), Stage.VERIFYING: (95, 99), Stage.FINALIZING: (99, 99.5),
    Stage.ARCHIVING: (99.5, 100),
}


@dataclass
class PipelineEnv:
    settings: Settings
    tools: FFmpegTools
    state: StateManager
    appdata: Path
    emit: Callable[[ev.Event], None] = lambda e: None
    log_system: LogSystem | None = None
    tts: TTSProvider | None = None
    image_provider: ImageGenProvider | None = None
    use_job_objects: bool = True

    @property
    def diagnostics_root(self) -> Path:
        return self.appdata / "diagnostics"

    @property
    def cache_root(self) -> Path:
        return self.appdata / "cache"


class _Progress:
    def __init__(self, job_id: str, emit: Callable[[ev.Event], None]) -> None:
        self.job_id = job_id
        self.emit = emit
        self._last = 0.0

    def report(self, stage: Stage, fraction: float, *, force: bool = False, p: FFmpegProgress | None = None,
               total_frames: int = 0, frame_base: int = 0) -> None:
        now = time.monotonic()
        if not force and now - self._last < PROGRESS_INTERVAL_S:
            return
        self._last = now
        lo, hi = STAGE_WEIGHTS.get(stage, (0, 100))
        pct = lo + (hi - lo) * max(0.0, min(1.0, fraction))
        self.emit(ev.JobProgress(
            time.time(), self.job_id, stage, round(pct, 2),
            frame=frame_base + (p.frame if p else 0), total_frames=total_frames,
            fps=p.fps if p else 0.0, speed=p.speed if p else 0.0, out_time_s=p.out_time_s if p else 0.0))


@dataclass
class _Run:
    """Per-attempt mutable context."""
    ctx: JobContext
    cfg: JobConfig
    ws: JobWorkspace
    manifest: JobManifest
    progress: _Progress
    diag_dir: Path
    job_object: JobObject | None = None
    warnings: list[str] = field(default_factory=list)


class MediaPipeline:
    """JobExecutor implementation."""

    def __init__(self, env: PipelineEnv) -> None:
        self.env = env
        self.s = env.settings
        self._measured_fps: float | None = None
        self._runs: dict[str, _Run] = {}

    # ================================================================ executor API

    def execute(self, ctx: JobContext) -> JobOutcome:
        cfg = ctx.job.config
        ws = JobWorkspace(Path(self.env.state.workspace_dir(cfg.job_id))).create()
        diag = self.env.diagnostics_root / cfg.job_id
        diag.mkdir(parents=True, exist_ok=True)
        if self.env.log_system is not None and ctx.job_id not in self._runs:
            self.env.log_system.open_job_log(cfg.job_id, diag / "job.log")
        m = self._load_manifest(ws, cfg)
        m.attempts, m.status, m.error = ctx.attempt, JobStatus.RUNNING, None
        m.start_time = m.start_time or utc_now()
        m.ffmpeg_version = self.env.tools.version
        m.settings_snapshot = self.s.to_dict()
        run = _Run(ctx, cfg, ws, m, _Progress(cfg.job_id, self.env.emit), diag,
                   JobObject(cfg.job_id) if self.env.use_job_objects else None)
        self._runs[cfg.job_id] = run
        try:
            return self._execute(run)
        finally:
            m.stage = ctx.job.stage
            self._save(run)
            if run.job_object is not None:
                run.job_object.close()

    def prepare_retry(self, ctx: JobContext, error: ErrorInfo) -> None:
        run = self._runs.get(ctx.job_id)
        if run is None:
            return
        # Keep only verified artefacts (normalised images, finished segments);
        # remove every temporary / partial file.
        for d in (run.ws.seg_dir, run.ws.out_dir, run.ws.audio_dir, run.ws.norm_dir, run.ws.gen_dir):
            for p in d.glob("*.tmp*"):
                try:
                    p.unlink()
                except OSError:
                    log.warning("could not remove %s", p, extra={"job_id": ctx.job_id})
        if error.error_class == ErrorClass.VERIFICATION.value:
            # a bad final file: re-mux from the (verified) segments
            run.manifest.output_file = None
            run.manifest.output_sha256 = ""
        run.manifest.warnings.append(f"спроба {ctx.attempt} невдала: {error.message}")
        self._save(run)

    def finalize(self, ctx: JobContext, final: JobState) -> None:
        run = self._runs.pop(ctx.job_id, None)
        if run is None:
            return
        m = run.manifest
        m.status, m.end_time, m.error = final.status, utc_now(), final.error
        if final.status is JobStatus.INTERRUPTED:
            # resumable: keep the workspace (verified images/segments) and the job log
            self._save(run)
            if self.env.log_system is not None:
                self.env.log_system.close_job_log(ctx.job_id)
            log.warning("job interrupted; workspace kept for resume", extra={"job_id": ctx.job_id})
            return
        m.stage = Stage.CLEANUP
        self._save(run)
        keep_diag = final.status is not JobStatus.SUCCESS or self.s.logging.keep_job_logs
        try:
            if keep_diag:
                shutil.copyfile(run.ws.manifest_path, run.diag_dir / "manifest.json")
        except OSError:
            log.warning("could not copy manifest to diagnostics", extra={"job_id": ctx.job_id})
        if self.env.log_system is not None:
            self.env.log_system.close_job_log(ctx.job_id)
        if not keep_diag:
            remove_tree(run.diag_dir, deadline_s=10, require_managed=False)
        keep_ws = final.status is JobStatus.FAILED and self.s.cleanup.keep_failed_workspace
        if not keep_ws:
            deadline = timeouts.cleanup(self.s.timeouts, count_files(run.ws.root, limit=200_000))
            res = remove_tree(run.ws.root, deadline_s=deadline)
            if not res.ok:
                self.env.state.add_pending_cleanup(str(run.ws.root))
                log.warning("workspace cleanup incomplete; scheduled for next start",
                            extra={"job_id": ctx.job_id})

    # ================================================================ stages

    def _execute(self, run: _Run) -> JobOutcome:
        ctx, cfg, m = run.ctx, run.cfg, run.manifest

        ctx.stage(Stage.VALIDATING)
        self._preflight(cfg)

        if cfg.mode is Mode.SCRIPT_PROMPTS:
            ctx.stage(Stage.GENERATING)
            audio_src, image_srcs = self._generate(run)
        else:
            if not cfg.audio_file:
                raise InputError("Для цього відео не знайдено аудіофайл.", code="AUDIO_MISSING")
            audio_src, image_srcs = Path(cfg.audio_file), [Path(p) for p in cfg.image_files]

        ctx.stage(Stage.AUDIO)
        audio = self._audio(run, audio_src)
        self._disk_check(run, len(image_srcs), audio.decoded_duration_s)

        ctx.stage(Stage.NORMALIZING)
        items = self._normalize(run, image_srcs)
        valid = [i for i in items if i.status is ImageStatus.NORMALIZED]
        invalid = [i for i in items if i.status is ImageStatus.INVALID]
        recovered = [i for i in valid if i.recovered]
        if invalid and self.s.images.on_invalid == "fail_job":
            raise InputError(self._invalid_summary(invalid), code="INVALID_IMAGES")
        if len(valid) < self.s.images.min_valid_images:
            raise InputError(
                "Жодне зображення не вдалося використати." if not valid else
                f"Придатних зображень лише {len(valid)} (потрібно щонайменше "
                f"{self.s.images.min_valid_images}).", code="NO_VALID_IMAGES")

        ctx.stage(Stage.TIMELINE)
        tl = build_timeline(audio.decoded_duration_s, cfg.fps, len(valid), self.s.effects,
                            min_seconds_per_image=self.s.images.min_seconds_per_image, seed=cfg.job_id)
        self._apply_timeline(run, tl)

        ctx.stage(Stage.RENDERING)
        self._render(run, tl, valid)

        degraded = bool(invalid or recovered)
        out_tmp = run.ws.temp_output()
        reuse = self._existing_final(run)
        if reuse is None:
            ctx.stage(Stage.MUXING)
            self._mux(run, tl, audio)
            corrupt = _test_hook("CORRUPT_OUTPUT")
            if corrupt:
                log.warning("TEST HOOK: corrupting output (%s)", corrupt, extra={"job_id": run.cfg.job_id})
                _corrupt_output(corrupt, out_tmp, self.env.tools.ffmpeg)
            ctx.stage(Stage.VERIFYING)
            info = verify_output(self.env.tools.ffmpeg, self.env.tools.ffprobe, out_tmp, self._expected(cfg, tl),
                                 self.s.timeouts, ctx.token)
            m.actual_duration = info.duration_s
            run.progress.report(Stage.VERIFYING, 1.0, force=True)
            ctx.stage(Stage.FINALIZING)
            final = self._publish(run, out_tmp, degraded)
        else:
            final = reuse
            ctx.stage(Stage.FINALIZING)

        if self.s.archive.enabled:
            ctx.stage(Stage.ARCHIVING)
            self._archive(run, final, image_srcs, audio_src,
                          JobStatus.PARTIAL if degraded else JobStatus.SUCCESS)

        status = JobStatus.PARTIAL if degraded else JobStatus.SUCCESS
        if degraded:
            parts = []
            if invalid:
                parts.append(f"пропущено зображень: {len(invalid)}")
            if recovered:
                parts.append(f"відновлено з пошкоджених файлів: {len(recovered)}")
            msg = "Відео створено не з повного набору матеріалів (" + "; ".join(parts) + ")."
            m.warnings.append(msg)
            log.warning(msg, extra={"job_id": cfg.job_id, "event": "partial"})
        return JobOutcome(status, str(final), skipped_images=len(invalid) + len(recovered),
                          warnings=tuple(m.warnings))

    # ---------------------------------------------------------------- preflight

    def _preflight(self, cfg: JobConfig) -> None:
        if not Path(cfg.input_dir).is_dir():
            raise InputError(f"Папку з матеріалами не знайдено: {cfg.input_dir}", code="INPUT_MISSING")
        out = Path(cfg.output_dir)
        try:
            out.mkdir(parents=True, exist_ok=True)
            probe = out / f".vg-write-test-{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise InputError(f"Неможливо записати у папку результатів: {out}", code="OUTPUT_NOT_WRITABLE",
                             detail=repr(exc)) from exc

    def _disk_check(self, run: _Run, n_images: int, duration: float) -> None:
        cw, ch = canvas_mod.canvas_size(run.cfg.width, run.cfg.height, self.s.effects.overscan)
        archive = 0
        if self.s.archive.enabled and self.s.archive.include_inputs:
            archive = sum(_size(Path(p)) for p in run.cfg.image_files) + _size(Path(run.cfg.audio_file or ""))
        est = estimate_job_disk(n_images=n_images, width=cw, height=ch, overscan=1.0, duration_s=duration,
                                archive_bytes=archive)
        check_disk(est, run.ws.root, Path(run.cfg.output_dir), self.s.resources.disk_reserve_mb)

    # ---------------------------------------------------------------- MODE B

    def _generate(self, run: _Run) -> tuple[Path, list[Path]]:
        cfg, ctx = run.cfg, run.ctx
        tts, imgs = self.env.tts, self.env.image_provider
        if tts is None or imgs is None:
            raise InputError("Провайдер озвучки або генерації зображень не налаштований.",
                             code="PROVIDER_NOT_CONFIGURED")
        try:
            script = Path(cfg.script_file or "").read_text(encoding="utf-8-sig")
            prompts = read_prompts(Path(cfg.prompts_file or "").read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeDecodeError) as exc:
            raise InputError("Не вдалося прочитати script.txt або prompts.txt (потрібне кодування UTF-8).",
                             code="SCRIPT_UNREADABLE", detail=repr(exc)) from exc
        scenes = split_script(script)
        if not scenes:
            raise InputError("Файл script.txt порожній.", code="SCRIPT_EMPTY")
        if len(prompts) != len(scenes):
            raise InputError(f"Кількість промптів ({len(prompts)}) не збігається з кількістю сцен "
                             f"у сценарії ({len(scenes)}).", code="PROMPT_COUNT")
        cache = AssetCache(self.env.cache_root)
        timeout = self.s.timeouts.mux_base_s * 4
        voice = run.ws.gen_dir / "voice.mp3"
        if not voice.exists():
            key = cache.key("tts", tts.name, {"text": "\n\n".join(scenes)})
            if not cache.get(key, ".mp3", voice):
                tmp = voice.with_name("voice.tmp.mp3")
                tts.synthesize("\n\n".join(scenes), tmp, timeout_s=timeout, token=ctx.token)
                tmp.replace(voice)
                cache.put(key, ".mp3", voice)
        images: list[Path] = []
        for i, prompt in enumerate(prompts):
            ctx.checkpoint()
            dst = run.ws.gen_dir / f"g{i:05d}.png"
            if not dst.exists():
                key = cache.key("img", imgs.name, {"prompt": prompt, "w": run.cfg.width, "h": run.cfg.height})
                if not cache.get(key, ".png", dst):
                    tmp = dst.with_name(dst.stem + ".tmp.png")
                    imgs.generate(prompt, tmp, width=run.cfg.width, height=run.cfg.height,
                                  timeout_s=timeout, token=ctx.token)
                    tmp.replace(dst)
                    cache.put(key, ".png", dst)
            images.append(dst)
            run.progress.report(Stage.GENERATING, (i + 1) / len(prompts))
        return voice, images

    # ---------------------------------------------------------------- audio

    def _audio(self, run: _Run, src: Path) -> AudioInfo:
        m, wav = run.manifest, run.ws.audio_dir / "a.wav"
        if m.audio.get("decoded_duration") and wav.exists():
            try:
                dur, rate, ch = wav_duration(wav)
                if abs(dur - float(m.audio["decoded_duration"])) < 1e-3:
                    return AudioInfo(source=str(src), sample_rate=rate, channels=ch,
                                     decoded_duration_s=dur, wav_path=str(wav), sha256=m.audio_sha256)
            except (OSError, ValueError):
                log.warning("cached audio unusable; decoding again", extra={"job_id": run.cfg.job_id})
        info = probe_audio(self.env.tools.ffprobe, src, self.s.timeouts, run.ctx.token)
        info = normalize_audio(self.env.tools.ffmpeg, info, wav, self.s.audio, self.s.timeouts, run.ctx.token)
        for w in info.warnings:
            log.warning("audio: %s", w, extra={"job_id": run.cfg.job_id, "stage": "AUDIO"})
            m.warnings.append(f"аудіо: {w}")
        m.audio, m.audio_sha256 = info.to_dict(), info.sha256
        self._save(run)
        run.progress.report(Stage.AUDIO, 1.0, force=True)
        return info

    # ---------------------------------------------------------------- images

    def _normalize(self, run: _Run, sources: list[Path]) -> list[ImageItem]:
        cfg, m, ctx = run.cfg, run.manifest, run.ctx
        previous = {it.index: it for it in m.images if it.source_path == str(sources[it.index])} \
            if all(it.index < len(sources) for it in m.images) else {}
        vertical = cfg.orientation is Orientation.VERTICAL
        items: list[ImageItem] = []
        with ImageWorkerClient(self.s.images, self.s.timeouts, ffmpeg=self.env.tools.ffmpeg,
                               log_queue=self.env.log_system.queue if self.env.log_system else None,
                               recycle_after=self.s.concurrency.image_worker_recycle_after) as worker:
            for i, src in enumerate(sources):
                ctx.checkpoint()
                dst = run.ws.normalized_image(i)
                prev = previous.get(i)
                if prev and prev.status is ImageStatus.NORMALIZED and dst.is_file() and dst.stat().st_size > 0:
                    items.append(prev)
                    continue
                if prev and prev.status is ImageStatus.INVALID and prev.reason_code not in ("TIMEOUT", "CRASH"):
                    items.append(prev)          # deterministic failure: do not retry
                    continue
                res = worker.normalize(i, str(src), str(dst), width=cfg.width, height=cfg.height,
                                       overscan=self.s.effects.overscan, vertical=vertical, token=ctx.token)
                item = res.to_image_item()
                item.normalized_path = str(dst.relative_to(run.ws.root)) if res.ok else ""
                items.append(item)
                self._report_image(run, item, src)
                if (i + 1) % 25 == 0:
                    m.images = items + [it for it in m.images if it.index > i]
                    self._save(run)
                run.progress.report(Stage.NORMALIZING, (i + 1) / max(1, len(sources)))
        m.images = items
        self._save(run)
        return items

    def _report_image(self, run: _Run, item: ImageItem, src: Path) -> None:
        jid = run.cfg.job_id
        if item.status is ImageStatus.INVALID:
            msg = (f"Не вдалося використати зображення №{item.index + 1}. Файл: {src.name}. "
                   f"Причина: {item.message}.")
            log.warning(msg, extra={"job_id": jid, "stage": "NORMALIZING", "event": "image_invalid",
                                    "error": item.reason_code})
            self.env.emit(ev.ImageSkipped(time.time(), jid, item.index + 1, src.name, item.message))
        elif item.recovered:
            msg = (f"Зображення №{item.index + 1} ({src.name}) пошкоджене; відновлено резервним "
                   f"декодером {item.decoder} — можливі артефакти.")
            log.warning(msg, extra={"job_id": jid, "stage": "NORMALIZING", "event": "image_recovered"})
            self.env.emit(ev.ImageSkipped(time.time(), jid, item.index + 1, src.name,
                                          "відновлено з пошкодженого файлу"))
        for w in item.warnings:
            log.info("image %d: %s", item.index + 1, w, extra={"job_id": jid, "stage": "NORMALIZING"})

    @staticmethod
    def _invalid_summary(invalid: list[ImageItem]) -> str:
        head = "; ".join(f"№{i.index + 1} ({Path(i.source_path).name}): {i.message}" for i in invalid[:5])
        more = f" та ще {len(invalid) - 5}" if len(invalid) > 5 else ""
        return f"Пошкоджені зображення: {head}{more}."

    # ---------------------------------------------------------------- timeline / render

    def _apply_timeline(self, run: _Run, tl: Timeline) -> None:
        m = run.manifest
        new = tl.to_dict()
        if m.timeline and m.timeline != new:
            log.warning("timeline changed since last attempt; discarding old segments",
                        extra={"job_id": run.cfg.job_id})
            for p in run.ws.seg_dir.glob("s*.mp4"):
                p.unlink(missing_ok=True)
            m.segments = []
        m.timeline = new
        m.expected_duration = tl.video_duration_s
        if len(m.segments) != len(tl.segments):
            m.segments = [SegmentRecord(s.index, s.frames) for s in tl.segments]
        self._save(run)

    def _encode_settings(self, cfg: JobConfig) -> EncodeSettings:
        cw, ch = canvas_mod.canvas_size(cfg.width, cfg.height, self.s.effects.overscan)
        v = self.s.video
        return EncodeSettings(cfg.width, cfg.height, cfg.fps, v.crf, v.preset, v.encoder, cw, ch,
                              self.env.tools.ffmpeg)

    def _render(self, run: _Run, tl: Timeline, valid: list[ImageItem]) -> None:
        ctx, m, ws = run.ctx, run.manifest, run.ws
        enc = self._encode_settings(run.cfg)
        tp = self.s.timeouts
        n = len(tl.segments)
        done_frames = 0
        for seg in tl.segments:
            ctx.checkpoint()
            rec = m.segments[seg.index]
            final = ws.segment(seg.index)
            if rec.status == "DONE" and final.is_file() and final.stat().st_size == rec.size:
                done_frames += seg.frames
                continue
            tmp = final.with_name(final.stem + ".tmp.mp4")
            prev = tl.segments[seg.index - 1] if seg.index > 0 else None
            argv = segment_argv(
                seg, prev if seg.transition_in else None,
                image=valid[seg.image_index].normalized_path,
                prev_image=valid[prev.image_index].normalized_path if (prev and seg.transition_in) else None,
                out=str(tmp.relative_to(ws.root)), s=enc)
            if _stall_requested(_test_hook("STALL_SEGMENT"), run.cfg.name, seg.index):
                argv = _stalling_ffmpeg(self.env.tools.ffmpeg)
                log.warning("TEST HOOK: segment %d replaced by a stalling ffmpeg", seg.index + 1,
                            extra={"job_id": run.cfg.job_id})
            fps_min = timeouts.calibrated_fps_min(tp, self._measured_fps)
            lim = timeouts.segment_render(tp, seg.frames + seg.transition_in, fps_min)
            base = done_frames
            t0 = time.monotonic()
            res = run_ffmpeg(
                argv, hard_s=lim.hard_s, stall_s=lim.stall_s, cwd=ws.root, output_path=tmp, token=ctx.token,
                on_progress=lambda p, b=base: run.progress.report(
                    Stage.RENDERING, (b + p.frame) / tl.total_frames, p=p, total_frames=tl.total_frames,
                    frame_base=b),
                on_snapshot=self._snapshot_cb(run), policy=tp, job=run.job_object,
                label=f"ffmpeg-seg{seg.index}")
            raise_for(res, f"Рендер фрагмента {seg.index + 1} з {n}")
            frames = probe_media(self.env.tools.ffprobe, tmp, tp, ctx.token).video_frames
            if frames != seg.frames:
                tmp.unlink(missing_ok=True)
                raise VerificationError(
                    f"Фрагмент {seg.index + 1} містить {frames} кадрів замість {seg.frames}.",
                    code="SEGMENT_FRAMES")
            replace_with_retry(tmp, final)
            elapsed = max(1e-3, time.monotonic() - t0)
            if self._measured_fps is None:
                self._measured_fps = seg.frames / elapsed
                log.info("calibrated render speed %.1f fps", self._measured_fps,
                         extra={"job_id": run.cfg.job_id, "stage": "RENDERING"})
            rec.status, rec.size = "DONE", final.stat().st_size
            log.info("segment %d/%d rendered: %d frames in %.1fs", seg.index + 1, n, seg.frames, elapsed,
                     extra={"job_id": run.cfg.job_id, "stage": "RENDERING", "event": "segment_done",
                            "duration_ms": int(elapsed * 1000)})
            done_frames += seg.frames
            self._save(run)
            self.env.state.set_resume_point(run.cfg.job_id, seg.index + 1)

    def _mux(self, run: _Run, tl: Timeline, audio: AudioInfo) -> None:
        ws, tp = run.ws, self.s.timeouts
        names = [run.ws.segment(s.index).name for s in tl.segments]
        atomic_write_text(ws.seg_dir / "concat.txt", concat_list(names))
        out = ws.temp_output()
        out.unlink(missing_ok=True)
        enc = self._encode_settings(run.cfg)
        argv = mux_argv(concat_file=str(Path("seg") / "concat.txt"), audio_wav=str(Path("audio") / "a.wav"),
                        out=str(out.relative_to(ws.root)), timeline=tl, s=enc,
                        audio_bitrate_kbps=self.s.audio.aac_bitrate_kbps, sample_rate=self.s.audio.sample_rate,
                        channels=self.s.audio.channels)
        lim = timeouts.mux(tp, tl.video_duration_s)
        dur = tl.video_duration_s
        res = run_ffmpeg(argv, hard_s=lim.hard_s, stall_s=lim.stall_s, cwd=ws.root, output_path=out,
                         token=run.ctx.token, policy=tp, job=run.job_object, label="ffmpeg-mux",
                         on_progress=lambda p: run.progress.report(Stage.MUXING, p.out_time_s / dur, p=p),
                         on_snapshot=self._snapshot_cb(run))
        raise_for(res, "Збирання відео")

    def _expected(self, cfg: JobConfig, tl: Timeline) -> ExpectedOutput:
        enc = self.s.video.encoder
        return ExpectedOutput(cfg.width, cfg.height, cfg.fps, tl.total_frames, True,
                              self.s.audio.sample_rate, self.s.audio.channels,
                              video_codec="h264" if enc in ("libx264", "h264_nvenc") else enc)

    # ---------------------------------------------------------------- publish

    def _existing_final(self, run: _Run) -> Path | None:
        """Resume after a crash *after* the final rename: recognise our file."""
        m = run.manifest
        if not m.output_file or not m.output_sha256:
            return None
        p = Path(m.output_file)
        try:
            if p.is_file() and sha256_file(p)[0] == m.output_sha256:
                log.info("final output already published: %s", p, extra={"job_id": run.cfg.job_id})
                return p
        except OSError:
            return None
        return None

    def _publish(self, run: _Run, tmp: Path, degraded: bool) -> Path:
        cfg, m = run.cfg, run.manifest
        stem = safe_filename(cfg.name) + (self.s.video.partial_suffix if degraded else "")
        out_dir = Path(cfg.output_dir)
        final = unique_output_path(out_dir, stem, ".mp4")
        digest, _ = sha256_file(tmp, partial_threshold=1 << 62)
        # write-ahead: record the target and hash before touching the output folder
        m.output_file, m.output_sha256 = str(final), digest
        self._save(run)
        part = final.with_name("." + final.name + ".part")
        try:
            if same_volume(tmp, out_dir):
                replace_with_retry(tmp, part)
            else:
                _copy_fsync(tmp, part, run.ctx)
                if sha256_file(part, partial_threshold=1 << 62)[0] != digest:
                    raise VerificationError("Копія відео у папці результатів пошкоджена.", code="COPY_MISMATCH")
            replace_with_retry(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            m.output_file, m.output_sha256 = None, ""
            self._save(run)
            raise
        log.info("published %s", final, extra={"job_id": cfg.job_id, "stage": "FINALIZING", "event": "published"})
        return final

    # ---------------------------------------------------------------- archive

    def _archive(self, run: _Run, video: Path, images: list[Path], audio: Path, status: JobStatus) -> None:
        cfg, m, a = run.cfg, run.manifest, self.s.archive
        m.status = status
        self._save(run)
        entries = [ArchiveEntry(run.ws.manifest_path, "manifest.json")]
        job_log = run.diag_dir / "job.log"
        if job_log.is_file():
            entries.append(ArchiveEntry(job_log, "job.log"))
        if a.include_inputs:
            for i, p in enumerate(images):
                if p.is_file():
                    entries.append(ArchiveEntry(p, f"inputs/{i + 1:05d}_{safe_filename(p.stem)}{p.suffix.lower()}"))
            if audio.is_file():
                entries.append(ArchiveEntry(audio, f"inputs/audio_{safe_filename(audio.stem)}{audio.suffix.lower()}"))
        if a.include_video:
            entries.append(ArchiveEntry(video, f"video/{video.name}"))
        dest = unique_output_path(Path(cfg.output_dir) / "_archive", video.stem, ".zip")
        result = _run_archive_process(dest, entries, a.max_size_mb * 1024 * 1024, self.s, run.ctx)
        if result.get("skipped"):
            m.warnings.append(str(result["skipped"]))
            log.warning("archive skipped: %s", result["skipped"], extra={"job_id": cfg.job_id})
        else:
            m.archive_file = str(dest)
        self._save(run)

    # ---------------------------------------------------------------- helpers

    def _load_manifest(self, ws: JobWorkspace, cfg: JobConfig) -> JobManifest:
        m = read_manifest(ws.manifest_path)
        if m is not None and m.config.job_id == cfg.job_id:
            return m
        m = JobManifest(config=cfg)
        write_manifest(ws.manifest_path, m)
        return m

    def _save(self, run: _Run) -> None:
        try:
            run.manifest.stage = self.env.state.get_job(run.cfg.job_id).stage
            write_manifest(run.ws.manifest_path, run.manifest)
        except OSError:
            log.exception("could not write manifest", extra={"job_id": run.cfg.job_id})

    def _snapshot_cb(self, run: _Run) -> Callable[[str, dict[str, Any]], None]:
        def cb(reason: str, data: dict[str, Any]) -> None:
            write_snapshot(run.diag_dir, reason=reason, pid=data.get("pid"), extra=data,
                           paths=[run.ws.root, Path(run.cfg.output_dir)])
        return cb


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _copy_fsync(src: Path, dst: Path, ctx: JobContext) -> None:
    block = 8 * 1024 * 1024
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        for _ in range(src.stat().st_size // block + 2):
            ctx.token.raise_if_cancelled()
            chunk = fi.read(block)
            if not chunk:
                break
            fo.write(chunk)
        fo.flush()
        os.fsync(fo.fileno())


# ---------------------------------------------------------------- archive process

def _archive_child(conn: Any, dest: str, entries: list[tuple[str, str]], max_bytes: int) -> None:
    from videogen.media.archiver import ArchiveEntry as AE, create_archive
    try:
        res = create_archive(Path(dest), [AE(Path(s), a) for s, a in entries], max_size_bytes=max_bytes)
        conn.send({"ok": True, "path": res.path, "skipped": res.skipped_reason})
    except ArchiveError as exc:
        conn.send({"ok": False, "message": exc.user_message, "detail": exc.detail})
    except Exception as exc:  # noqa: BLE001 - report everything to the parent
        conn.send({"ok": False, "message": "Не вдалося створити архів.", "detail": repr(exc)})


def _run_archive_process(dest: Path, entries: list[ArchiveEntry], max_bytes: int, s: Settings,
                         ctx: JobContext) -> dict[str, Any]:
    """Archive in a separate, killable process with a size-based timeout."""
    from videogen.ffmpeg_ctl.process_manager import REGISTRY
    from videogen.workers.watchdog import Watchdog

    total = estimate_size(entries)
    if total > max_bytes:
        msg = (f"Архів не створено: очікуваний розмір {total / 1048576:.0f} МБ перевищує ліміт "
               f"{max_bytes / 1048576:.0f} МБ.")
        return {"ok": True, "skipped": msg}
    lim = timeouts.archive(s.timeouts, total)
    mp = multiprocessing.get_context("spawn")
    parent, child = mp.Pipe(duplex=False)
    proc = mp.Process(target=_archive_child, name="Archiver", daemon=True,
                      args=(child, str(dest), [(str(e.source), e.arcname) for e in entries], max_bytes))
    proc.start()
    child.close()
    REGISTRY.add(proc.pid, "Archiver")
    part = dest.with_name("." + dest.name + ".part")
    wd = Watchdog(hard_s=lim.hard_s, stall_s=lim.stall_s)
    result: dict[str, Any] | None = None
    try:
        for _ in range(int(lim.hard_s / 0.2) + 10):
            if parent.poll(0.2):
                try:
                    result = parent.recv()
                except EOFError:
                    result = None
                break
            if not proc.is_alive():
                break
            if ctx.token.cancelled:
                raise JobCancelledError()
            wd.observe(out_size=_size(part))
            v = wd.verdict()
            if v is not None:
                raise ArchiveError(f"Архівування зависло ({v.kind}).", code="ARCHIVE_TIMEOUT")
    finally:
        if result is not None:
            # The child reported its result and is exiting normally: let it
            # finish instead of killing a healthy process.
            proc.join(timeout=s.timeouts.terminate_wait_s * 3)
        if proc.is_alive():
            kill_tree(proc.pid, wait_s=5)
        proc.join(timeout=5)
        REGISTRY.remove(proc.pid)
        parent.close()
        try:
            proc.close()                  # releases the sentinel pipe immediately
        except ValueError:
            log.error("archiver process %s could not be reaped", proc.pid)
        if result is None or not result.get("ok"):
            part.unlink(missing_ok=True)
    if result is None:
        raise ArchiveError("Процес архівування аварійно завершився.", code="ARCHIVE_CRASH")
    if not result.get("ok"):
        raise ArchiveError(str(result.get("message")), detail=str(result.get("detail", "")))
    return result

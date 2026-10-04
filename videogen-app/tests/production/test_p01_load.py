"""1. Long render, 2. 100 sequential jobs."""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import psutil
import pytest

from videogen.core import events as ev
from videogen.core.models import JobStatus
from tests.production import monitor
from tests.production.common import (
    assert_no_media_processes, expected_frames, frames, prod_settings, ws_files,
)
from tests.production.conftest import FULL, scale
from tests.production.harness import EngineHarness
from tests.production.media_sets import normal_set, picture, tone

LONG_SIZES = [scale(500, 40), scale(1000, 0)]


@pytest.mark.timeout(4 * 3600)
@pytest.mark.parametrize("n", [x for x in LONG_SIZES if x])
def test_01_long_render(work, record, baseline, n):
    record.update(number="1", title=f"LONG RENDER: {n} зображень", input=(
        f"{n} зображень JPG/PNG/WEBP 1280×720…1920×1080, аудіо {n}×1,6 с; реальні налаштування "
        f"{'1920×1080, 30 fps, preset medium' if FULL else '640×360 (quick)'}; окремий процес Engine"),
        expected="SUCCESS; точна кількість кадрів; RAM Engine без постійного зростання; ≤ 1 FFmpeg "
                 "одночасно; тимчасові файли видалено; жодного процесу після завершення")
    inp, out, ws = work / "in", work / "out", work / "ws"
    dur = normal_set(inp / f"long_{n}", n, seconds_per_image=1.6)
    h = EngineHarness(work / "appdata", prod_settings()).start()
    sampler = monitor.Sampler(lambda: h.pid, work, ws, interval=2.0)
    next_mark = [1000]

    def on_event(e):
        if isinstance(e, ev.JobProgress) and e.stage.value == "RENDERING" and e.frame >= next_mark[0]:
            sampler.sample("frames", frame=e.frame)
            next_mark[0] = (e.frame // 1000 + 1) * 1000
    h.on_event = on_event
    sampler.start()
    t0 = time.monotonic()
    try:
        fin = h.run_batch(inp, out, ws, timeout=4 * 3600 - 600)
    finally:
        samples = sampler.stop()
        h.stop()
    elapsed = time.monotonic() - t0
    usage = monitor.summarize(samples)
    frame_rows = [(s["frame"], s["engine_rss_mb"]) for s in samples if s["mark"] == "frames"]
    usage.update(elapsed_s=round(elapsed), rss_every_1000_frames=frame_rows[:60])
    record["resource_usage"] = usage
    assert len(fin) == 1
    job = fin[0]
    want = expected_frames(dur)
    got = frames(Path(job.output_file)) if job.output_file else -1
    record["actual"] = (f"{job.status.value}; кадрів {got}/{want}; {elapsed / 60:.1f} хв; RSS Engine "
                        f"{usage.get('engine_rss_mb_start')}→{usage.get('engine_rss_mb_end')} МБ (макс. "
                        f"{usage.get('engine_rss_mb_max')}), нахил {usage.get('engine_rss_mb_slope_per_min')} МБ/хв; "
                        f"дерево процесів (Engine+FFmpeg+воркери) макс. {usage.get('tree_rss_mb_max')} МБ; "
                        f"мін. доступна RAM системи {usage.get('sys_avail_mb_min')} МБ; "
                        f"FFmpeg одночасно ≤ {usage.get('ffmpeg_procs_max')}; тимчасових файлів макс. "
                        f"{usage.get('temp_files_max')} → {usage.get('temp_files_end')}")
    assert job.status is JobStatus.SUCCESS, job.error
    assert got == want
    rss = [s["engine_rss_mb"] for s in samples if s["engine_rss_mb"]]
    half = len(rss) // 2
    assert sorted(rss[half:])[len(rss[half:]) // 2] - sorted(rss[:half])[half // 2] < 60, rss
    assert usage["ffmpeg_procs_max"] <= 2          # one ffmpeg (+ a transient ffprobe)
    assert ws_files(ws) == []
    assert_no_media_processes(baseline)
    assert monitor.orphans() == []
    shutil.rmtree(inp, ignore_errors=True)


@pytest.mark.timeout(4 * 3600)
def test_02_sequential_jobs(work, record, baseline):
    n_jobs = scale(100, 12)
    record.update(number="2", title=f"SEQUENTIAL JOBS: {n_jobs} послідовних jobs", input=(
        f"{n_jobs} окремих запусків (StartBatch) у тому самому процесі Engine; кожен — 3 зображення + "
        "аудіо 4,5 с"), expected="кожен вихід валідний; workspace очищено; немає сиріт FFmpeg/Engine; "
        "RAM, кількість процесів і місце на диску не накопичуються")
    audio = tone(work / "tone.mp3", 4.5)
    h = EngineHarness(work / "appdata", prod_settings()).start()
    rows = []
    engine_pids = set()
    disk0 = psutil.disk_usage(str(work)).free
    sampler = monitor.Sampler(lambda: h.pid, work, work / "ws", interval=1.0).start()
    try:
        for i in range(n_jobs):
            inp = work / "in" / f"{i:03d}"
            d = inp / f"Відео {i + 1:03d}"
            d.mkdir(parents=True)
            for k in range(3):
                picture((1280, 720), i * 3 + k).save(d / f"{k}.jpg", "JPEG", quality=88)
            shutil.copyfile(audio, d / "a.mp3")
            out, ws = work / "out", work / "ws"
            fin = h.run_batch(inp, out, ws, timeout=900)
            assert len(fin) == 1 and fin[0].status is JobStatus.SUCCESS, fin
            outp = Path(fin[0].output_file)
            ok_frames = frames(outp) == expected_frames(4.5)
            engine = psutil.Process(h.pid)
            engine_pids.add(engine.pid)
            kids = [c for c in engine.children(recursive=True) if "resource_tracker" not in monitor._cmd(c)]
            produced = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
            rows.append({
                "job": i + 1, "output_valid": ok_frames, "ws_files": len(ws_files(ws)),
                "ffmpeg": len(monitor.media_processes()), "engine_children": len(kids),
                "engine_rss_mb": round(monitor.rss_mb(engine.pid), 1),
                "engine_handles": _handles(engine),
                "disk_used_mb_excl_outputs": round((disk0 - psutil.disk_usage(str(work)).free - produced) / 2**20, 1),
            })
            shutil.rmtree(inp, ignore_errors=True)
            assert ok_frames and rows[-1]["ws_files"] == 0 and rows[-1]["ffmpeg"] == 0
            assert rows[-1]["engine_children"] == 0, kids
    finally:
        samples = sampler.stop()
        h.stop()
    tree = monitor.summarize(samples)
    from tests.production.monitor import slope
    warm = rows[min(10, len(rows) // 3):]
    rss_slope = slope([r["job"] for r in warm], [r["engine_rss_mb"] for r in warm])
    handle_slope = slope([r["job"] for r in warm], [r["engine_handles"] for r in warm]) if warm[0]["engine_handles"] >= 0 else 0
    record["resource_usage"] = {
        "jobs": len(rows), "engine_processes_seen": len(engine_pids),
        "engine_rss_first_mb": rows[0]["engine_rss_mb"], "engine_rss_last_mb": rows[-1]["engine_rss_mb"],
        "engine_rss_slope_mb_per_job": round(rss_slope, 4),
        "engine_handles_first": rows[0]["engine_handles"], "engine_handles_last": rows[-1]["engine_handles"],
        "handles_slope_per_job": round(handle_slope, 3),
        "disk_used_excl_outputs_last_mb": rows[-1]["disk_used_mb_excl_outputs"],
        "engine_rss_mb_max": tree.get("engine_rss_mb_max"), "engine_peak_wset_mb": tree.get("engine_peak_wset_mb"),
        "tree_rss_mb_max": tree.get("tree_rss_mb_max"), "tree_rss_mb_median": tree.get("tree_rss_mb_median"),
        "sys_avail_mb_start": tree.get("sys_avail_mb_start"), "sys_avail_mb_min": tree.get("sys_avail_mb_min"),
        "samples": tree.get("samples"),
        "per_job": rows,
    }
    record["actual"] = (f"{len(rows)}/{n_jobs} SUCCESS, усі виходи валідні; workspace 0 файлів після кожного; "
                        f"FFmpeg 0; дочірніх процесів Engine 0; RSS {rows[0]['engine_rss_mb']}→"
                        f"{rows[-1]['engine_rss_mb']} МБ (нахил {rss_slope:.3f} МБ/job); дескриптори "
                        f"{rows[0]['engine_handles']}→{rows[-1]['engine_handles']}")
    assert len(engine_pids) == 1                      # the same engine served every job
    assert rss_slope < 0.3, rss_slope
    assert handle_slope < 1.0, handle_slope
    assert rows[-1]["disk_used_mb_excl_outputs"] < 100
    assert_no_media_processes(baseline)
    assert monitor.orphans() == []


def _handles(p: psutil.Process) -> int:
    try:
        return p.num_handles() if hasattr(p, "num_handles") else p.num_fds()
    except psutil.Error:
        return -1

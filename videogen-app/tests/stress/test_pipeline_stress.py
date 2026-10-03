"""Stress / leak tests (ТЗ §43–45). Run explicitly:

    pytest -m stress tests/stress                       # 100, 500, 1000 images + 50 jobs
    VIDEOGEN_STRESS_SIZES=100 pytest -m stress ...      # quicker

Each test writes a JSON/CSV report to tests/stress/reports/.
"""

from __future__ import annotations

import csv
import gc
import json
import math
import os
import statistics
import threading
import time
from pathlib import Path

import psutil
import pytest

from videogen.config.settings import ImageSettings
from videogen.core import events as ev
from videogen.core.engine import Engine
from videogen.core.models import JobStatus
from videogen.ffmpeg_ctl.process_manager import REGISTRY
from videogen.media.media_validator import probe_media
from videogen.config.settings import TimeoutPolicy
from tests.fixtures import factory as F
from tests.pipeline_support import Events, small_settings, start_cmd

SIZES = [int(x) for x in os.environ.get("VIDEOGEN_STRESS_SIZES", "100,500,1000").split(",")]
REPORTS = Path(__file__).parent / "reports"
SECONDS_PER_IMAGE = 0.4


class Sampler(threading.Thread):
    def __init__(self, ws: Path, interval: float = 2.0) -> None:
        super().__init__(daemon=True)
        self.ws, self.interval = ws, interval
        self.stop = threading.Event()
        self.rows: list[dict] = []
        self.me = psutil.Process()
        self.t0 = time.monotonic()

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            kids = self.me.children(recursive=True)
            child_rss = 0
            for k in kids:
                try:
                    child_rss += k.memory_info().rss
                except psutil.Error:
                    continue
            files = sum(len(f) for _, _, f in os.walk(self.ws)) if self.ws.exists() else 0
            self.rows.append({"t": round(time.monotonic() - self.t0, 1),
                              "engine_rss_mb": round(self.me.memory_info().rss / 1048576, 1),
                              "children_rss_mb": round(child_rss / 1048576, 1),
                              "children": len(kids), "workspace_files": files})


def _live_children() -> list[str]:
    """Child processes still running, excluding multiprocessing's resource_tracker
    (a stdlib helper that lives as long as the interpreter)."""
    out = []
    for c in psutil.Process().children(recursive=True):
        try:
            cmd = " ".join(c.cmdline())
            if c.status() != psutil.STATUS_ZOMBIE and "resource_tracker" not in cmd:
                out.append(cmd[:120])
        except psutil.Error:
            continue
    return out


def _make_input(root: Path, n: int, bad_every: int = 20) -> tuple[Path, int]:
    d = root / f"Стрес {n} зображень"
    d.mkdir(parents=True)
    makers = [F.jpg, F.png, F.webp]
    bad = 0
    for i in range(n):
        if i % bad_every == bad_every - 3:
            F.corrupted_png(d / f"img_{i + 1:05d}.png")
            bad += 1
        else:
            makers[i % 3](d / f"img_{i + 1:05d}.{['jpg', 'png', 'webp'][i % 3]}", size=(640, 360))
    F.tone(d / "audio.mp3", n * SECONDS_PER_IMAGE)
    return d, bad


@pytest.mark.stress
@pytest.mark.timeout(7200)
@pytest.mark.parametrize("n", SIZES)
def test_large_job(tmp_path, n):
    inp, out, ws = tmp_path / "in", tmp_path / "out", tmp_path / "ws"
    folder, bad = _make_input(inp, n)
    settings = small_settings(images=ImageSettings(min_seconds_per_image=0.3))
    events = Events()
    eng = Engine(tmp_path / "appdata", settings, events)
    eng.startup()
    sampler = Sampler(ws)
    sampler.start()
    t0 = time.monotonic()
    try:
        eng.start_batch(start_cmd(inp, out, ws))
        assert eng.wait_idle(7000)
    finally:
        sampler.stop.set()
        sampler.join(10)
        elapsed = time.monotonic() - t0
        job = eng.state.list_jobs()[0]
        eng.shutdown()

    rows = sampler.rows
    report = {"images": n, "corrupted": bad, "status": job.status.value, "elapsed_s": round(elapsed, 1),
              "skipped": job.skipped_images, "samples": rows,
              "error": job.error.message if job.error else None}
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / f"large_job_{n}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))

    assert job.status is JobStatus.PARTIAL, job.error            # 5 % corrupted -> PARTIAL, never SUCCESS
    assert job.skipped_images == bad
    info = probe_media(F.FFPROBE, Path(job.output_file), TimeoutPolicy())
    assert info.video_frames == math.ceil(n * SECONDS_PER_IMAGE * 24 - 1e-6)
    # memory: engine RSS must not keep growing with the number of images
    rss = [r["engine_rss_mb"] for r in rows]
    if len(rss) >= 6:
        first, second = rss[: len(rss) // 2], rss[len(rss) // 2:]
        assert statistics.median(second) - statistics.median(first) < 40, rss
    assert max(r["children"] for r in rows) <= 4 if rows else True   # never dozens of ffmpeg
    # nothing left behind
    assert not [p for p in ws.rglob("*") if p.is_file() and p.name != ".videogen-workspace"]
    assert REGISTRY.snapshot() == {}
    assert _live_children() == []
    assert len(events.of(ev.ImageSkipped)) == bad


@pytest.mark.stress
@pytest.mark.timeout(7200)
def test_memory_leak_50_jobs(tmp_path):
    inp, out, ws = tmp_path / "in", tmp_path / "out", tmp_path / "ws"
    for j in range(50):
        d = inp / f"job_{j + 1:02d}"
        d.mkdir(parents=True)
        for i in range(3):
            F.jpg(d / f"{i}.jpg", size=(640, 360))
        F.tone(d / "a.mp3", 1.5)
    me = psutil.Process()
    rows: list[tuple[int, float, float]] = []
    before = {"rss": me.memory_info().rss / 1048576}
    lock = threading.Lock()

    def on_event(e):
        if isinstance(e, ev.JobStageChanged) and e.stage.value == "VALIDATING":
            with lock:
                before["rss"] = me.memory_info().rss / 1048576
        if isinstance(e, ev.JobFinished):
            gc.collect()
            with lock:
                after = me.memory_info().rss / 1048576
                rows.append((len(rows) + 1, round(before["rss"], 2), round(after, 2)))

    eng = Engine(tmp_path / "appdata", small_settings(), on_event)
    eng.startup()
    try:
        eng.start_batch(start_cmd(inp, out, ws))
        assert eng.wait_idle(7000)
        statuses = {j.status for j in eng.state.list_jobs()}
    finally:
        eng.shutdown()
    REPORTS.mkdir(exist_ok=True)
    with open(REPORTS / "memory_50_jobs.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["job", "rss_before_mb", "rss_after_mb", "delta_mb"])
        for r in rows:
            w.writerow([r[0], r[1], r[2], round(r[2] - r[1], 2)])
    assert statuses == {JobStatus.SUCCESS} and len(rows) == 50
    after = [r[2] for r in rows]
    # warm-up excluded; linear trend over jobs 10..50
    xs = list(range(10, 51))
    ys = after[9:]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs)
    (REPORTS / "memory_50_jobs_summary.json").write_text(json.dumps(
        {"slope_mb_per_job": round(slope, 4), "rss_job10": after[9], "rss_job50": after[-1]}))
    assert slope < 0.3, f"memory grows {slope:.3f} MB/job"
    assert after[-1] - after[9] < 20

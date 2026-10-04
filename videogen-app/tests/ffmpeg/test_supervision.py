from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import psutil
import pytest

from videogen.config.settings import ResourceLimits, TimeoutPolicy
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import (
    DiskSpaceError, FFmpegCrashError, JobCancelledError, OperationTimeoutError,
)
from videogen.ffmpeg_ctl.process_manager import REGISTRY, JobObject
from videogen.ffmpeg_ctl.progress import ProgressParser
from videogen.ffmpeg_ctl.runner import raise_for, run_ffmpeg
from videogen.workers.resource_monitor import (
    DiskEstimate, ResourceMonitor, ResourceSample, check_disk, evaluate,
)
from videogen.workers.watchdog import Watchdog

FAKE = str(Path(__file__).resolve().parents[1] / "fake_ffmpeg.py")
FAST = TimeoutPolicy(watchdog_poll_s=0.05, graceful_wait_s=0.5, terminate_wait_s=0.5, kill_wait_s=3)


def fake(*args: str) -> list[str]:
    return [sys.executable, FAKE, *args]


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.fixture(autouse=True)
def no_orphans():
    yield
    assert REGISTRY.snapshot() == {}, "a supervised process is still registered"


# ---------------------------------------------------------------- progress parser

def test_parser_blocks_and_garbage():
    p = ProgressParser()
    lines = ["frame=10", "fps=29.5", "out_time_us=333333", "speed=1.25x", "total_size=1000",
             "progress=continue", "garbage line", "frame=N/A", "out_time_us=N/A", "speed=N/A",
             "fps=nan", "progress=continue", "frame=20", "out_time_ms=666666", "progress=end"]
    snaps = [s for s in (p.feed_line(x) for x in lines) if s is not None]
    assert len(snaps) == 3
    assert (snaps[0].frame, snaps[0].fps, snaps[0].speed, snaps[0].total_size) == (10, 29.5, 1.25, 1000)
    assert abs(snaps[0].out_time_s - 0.333333) < 1e-9
    assert snaps[1].frame == 10 and snaps[1].fps == 29.5         # N/A keeps previous values
    assert snaps[2].frame == 20 and snaps[2].done and abs(snaps[2].out_time_s - 0.666666) < 1e-9
    assert snaps[0].marker != snaps[2].marker and snaps[0].marker == snaps[1].marker


def test_parser_bytes_and_negative():
    p = ProgressParser()
    p.feed_line(b"frame=-5\n")
    p.feed_line(b"out_time_us=-100\n")
    s = p.feed_line(b"progress=continue\n")
    assert s.frame == 0 and s.out_time_s == 0


# ---------------------------------------------------------------- watchdog (fake clock)

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_watchdog_running_vs_progress():
    c = Clock()
    wd = Watchdog(hard_s=1000, stall_s=10, clock=c)
    for i in range(50):                     # steady progress: never stalled
        c.t += 5
        wd.observe(marker=(i, 0), cpu_time=float(i))
        assert wd.verdict() is None
    for _ in range(3):                      # alive, no progress, no CPU -> stall
        c.t += 4
        wd.observe(marker=(49, 0), cpu_time=49.0)
    v = wd.verdict()
    assert v and v.kind == "stall"


def test_watchdog_livelock():
    c = Clock()
    wd = Watchdog(hard_s=1000, stall_s=10, clock=c)
    cpu = 0.0
    verdicts = []
    for _ in range(30):                     # CPU busy, no frames
        c.t += 1
        cpu += 1
        wd.observe(marker=(1, 0), cpu_time=cpu)
        verdicts.append(wd.verdict())
    assert all(v is None for v in verdicts[:19])
    assert verdicts[-1] and verdicts[-1].kind == "livelock"


def test_watchdog_output_growth_counts_as_progress():
    c = Clock()
    wd = Watchdog(hard_s=1000, stall_s=10, clock=c)
    for i in range(40):
        c.t += 2
        wd.observe(out_size=i * 100)
        assert wd.verdict() is None


def test_watchdog_hard_timeout_even_with_progress():
    c = Clock()
    wd = Watchdog(hard_s=30, stall_s=10, clock=c)
    for i in range(40):
        c.t += 1
        wd.observe(marker=(i, 0))
    assert wd.verdict().kind == "hard_timeout"


def test_watchdog_without_stall_only_hard():
    c = Clock()
    wd = Watchdog(hard_s=30, stall_s=None, clock=c)
    c.t = 29
    assert wd.verdict() is None
    c.t = 31
    assert wd.verdict().kind == "hard_timeout"


# ---------------------------------------------------------------- supervised runs

def test_normal_run_reports_real_progress(tmp_path):
    seen = []
    out = tmp_path / "o.bin"
    run = run_ffmpeg(fake("normal", "90", str(out)), hard_s=30, stall_s=5, output_path=out,
                     on_progress=seen.append, policy=FAST)
    assert run.ok and run.progress.frame == 90 and run.progress.done
    assert [s.frame for s in seen] == list(range(10, 91, 10))
    assert out.exists()
    raise_for(run, "test")       # no exception


@pytest.mark.parametrize("mode,kind", [("hang", "stall"), ("livelock", "livelock")])
def test_hung_process_is_detected_and_killed(tmp_path, mode, kind):
    out = tmp_path / "o.bin"
    snaps = []
    t0 = time.monotonic()
    run = run_ffmpeg(fake(mode, str(out)), hard_s=60, stall_s=1.0, output_path=out, policy=FAST,
                     on_snapshot=lambda r, d: snaps.append((r, d)))
    assert time.monotonic() - t0 < 15
    assert run.verdict and run.verdict.kind == kind and not run.ok
    assert gone(run.result.pid) and run.survivors == ()
    assert not out.exists()                                  # corrupted temp output removed
    assert snaps and snaps[0][0] == kind and "watchdog" in snaps[0][1]
    with pytest.raises(OperationTimeoutError):
        raise_for(run, "Рендер сегмента 3")


def test_hard_timeout(tmp_path):
    run = run_ffmpeg(fake("livelock"), hard_s=1.0, stall_s=None, policy=FAST)
    assert run.verdict.kind == "hard_timeout" and gone(run.result.pid)


def test_crash_is_reported_as_crash(tmp_path):
    run = run_ffmpeg(fake("crash"), hard_s=30, stall_s=5, policy=FAST)
    assert run.result.returncode == 1 and run.verdict is None
    assert "Error while decoding" in run.result.stderr_tail
    with pytest.raises(FFmpegCrashError):
        raise_for(run, "x")


def test_grandchild_is_killed_with_tree(tmp_path):
    run = run_ffmpeg(fake("child"), hard_s=60, stall_s=1.0, policy=FAST)
    child = int(re.search(r"CHILD=(\d+)", run.result.stderr_tail).group(1))
    assert gone(run.result.pid) and gone(child)
    assert run.survivors == ()


def test_stubborn_process_ignoring_q_and_sigterm_is_killed(tmp_path):
    t0 = time.monotonic()
    run = run_ffmpeg(fake("stubborn"), hard_s=60, stall_s=1.0, policy=FAST)
    assert time.monotonic() - t0 < 15
    assert gone(run.result.pid) and run.survivors == ()


def test_graceful_q_is_tried_first(tmp_path):
    run = run_ffmpeg(fake("hang"), hard_s=60, stall_s=1.0, policy=FAST)
    assert run.result.returncode == 255        # exited via 'q', not killed


def test_cancel_stops_run(tmp_path):
    token = CancellationToken()
    threading.Timer(0.5, token.cancel).start()
    t0 = time.monotonic()
    run = run_ffmpeg(fake("hang"), hard_s=60, stall_s=30, token=token, policy=FAST)
    assert time.monotonic() - t0 < 5
    assert run.cancelled and not run.ok and gone(run.result.pid)
    with pytest.raises(JobCancelledError):
        raise_for(run, "x")


def test_stderr_flood_no_deadlock(tmp_path):
    run = run_ffmpeg(fake("flood"), hard_s=120, stall_s=30, policy=FAST)
    assert run.ok and len(run.result.stderr_tail.splitlines()) == 400


def test_output_growth_without_progress_is_not_a_stall(tmp_path):
    out = tmp_path / "o.bin"
    run = run_ffmpeg(fake("slowgrow", str(out)), hard_s=30, stall_s=1.0, output_path=out, policy=FAST)
    assert run.ok


def test_job_object_is_noop_off_windows():
    with JobObject("t") as job:
        assert job.assign(1) is False or job.supported


def test_real_ffmpeg_progress(tmp_path):
    import shutil
    ff = shutil.which("ffmpeg")
    out = tmp_path / "o.mp4"
    seen = []
    run = run_ffmpeg([ff, "-hide_banner", "-v", "warning", "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30",
                      "-frames:v", "150", "-c:v", "libx264", "-preset", "ultrafast", "-progress", "pipe:1",
                      "-nostats", str(out)], hard_s=60, stall_s=10, output_path=out,
                     on_progress=seen.append, policy=FAST)
    assert run.ok and run.progress.frame == 150 and run.progress.done
    assert seen and all(b.frame >= a.frame for a, b in zip(seen, seen[1:]))


def test_real_ffmpeg_cancel_mid_encode(tmp_path):
    import shutil
    ff = shutil.which("ffmpeg")
    out = tmp_path / "o.mp4"
    token = CancellationToken()
    threading.Timer(1.0, token.cancel).start()
    run = run_ffmpeg([ff, "-hide_banner", "-v", "warning", "-y", "-re", "-f", "lavfi", "-i",
                      "testsrc2=s=640x360:r=30", "-t", "60", "-c:v", "libx264", "-preset", "ultrafast",
                      "-progress", "pipe:1", "-nostats", str(out)],
                     hard_s=120, stall_s=10, output_path=out, token=token, policy=FAST)
    assert run.cancelled and gone(run.result.pid) and not out.exists()
    assert 0 < run.progress.frame < 1800


# ---------------------------------------------------------------- resources

def _s(**kw):
    base = dict(ts=0, ram_available_mb=8000, ram_percent=40, tree_rss_mb=300, cpu_percent=20, disk_free_mb={})
    base.update(kw)
    return ResourceSample(**base)


def test_resource_evaluation():
    lim = ResourceLimits()
    assert evaluate(_s(), lim).ok
    assert evaluate(_s(ram_available_mb=500), lim).kind == "ram"
    assert evaluate(_s(tree_rss_mb=5000), lim).kind == "ram"
    st = evaluate(_s(disk_free_mb={"/ws": 100}), lim)
    assert st.kind == "disk" and "Недостатньо вільного місця" in st.message


def test_resource_monitor_runs_and_stops(tmp_path):
    m = ResourceMonitor(ResourceLimits(monitor_interval_s=0.05), [tmp_path])
    m.start()
    time.sleep(0.3)
    assert m.probe().ok in (True, False)
    m.stop()
    assert len(m.history) >= 3
    assert not any(t.name == "resource-monitor" for t in threading.enumerate())


def test_disk_check(tmp_path, monkeypatch):
    from videogen.workers import resource_monitor as rm
    monkeypatch.setattr(rm, "free_disk_bytes", lambda p: 500 * 1024 * 1024)
    with pytest.raises(DiskSpaceError) as ei:
        check_disk(DiskEstimate(400 * 1024 * 1024, 200 * 1024 * 1024), tmp_path, tmp_path, reserve_mb=100)
    assert "Недостатньо вільного місця на диску" in ei.value.user_message
    check_disk(DiskEstimate(100 * 1024 * 1024, 100 * 1024 * 1024), tmp_path, tmp_path, reserve_mb=100)


def test_disk_full_is_classified_as_disk_space_not_crash():
    from videogen.core.errors import DiskSpaceError
    run = run_ffmpeg(fake("nospace"), hard_s=30, stall_s=5, policy=FAST)
    assert run.result.returncode == 1
    with pytest.raises(DiskSpaceError):
        raise_for(run, "Рендер фрагмента 1")

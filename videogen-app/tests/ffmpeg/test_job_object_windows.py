"""Windows-only: verifies the Job Object guarantee (ТЗ §13, §45) on a real
Windows host (runs in the windows-latest CI job)."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import psutil
import pytest

from videogen.ffmpeg_ctl.process_manager import JobObject, popen_kwargs

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows Job Objects")

SPAWN = ("import subprocess, sys, time; "
         "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
         "print(c.pid, flush=True); time.sleep(120)")


def _gone(pid: int, timeout: float = 10) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not psutil.pid_exists(pid):
            return True
        time.sleep(0.1)
    return not psutil.pid_exists(pid)


def test_closing_job_kills_child_and_grandchild():
    job = JobObject("t")
    assert job.supported
    p = subprocess.Popen([sys.executable, "-c", SPAWN], stdout=subprocess.PIPE, **popen_kwargs())
    assert job.assign(p.pid)
    grandchild = int(p.stdout.readline())
    job.close()                                   # == our process dying
    assert _gone(p.pid) and _gone(grandchild)


def test_terminate_job():
    with JobObject("t2") as job:
        p = subprocess.Popen([sys.executable, "-c", SPAWN], stdout=subprocess.PIPE, **popen_kwargs())
        job.assign(p.pid)
        grandchild = int(p.stdout.readline())
        assert job.terminate()
        assert _gone(p.pid) and _gone(grandchild)


def test_engine_process_dies_with_gui_job(tmp_path):
    """Killing the 'GUI' (job handle owner) kills the engine and its ffmpeg."""
    code = (
        "import subprocess, sys, time\n"
        "from videogen.ffmpeg_ctl.process_manager import JobObject, popen_kwargs\n"
        "job = JobObject('gui')\n"
        f"c = subprocess.Popen([sys.executable, '-c', {SPAWN!r}], stdout=subprocess.PIPE, **popen_kwargs())\n"
        "job.assign(c.pid)\n"
        "print(c.pid, c.stdout.readline().decode().strip(), flush=True)\n"
        "time.sleep(120)\n")
    gui = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                           cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    engine, ffmpeg_like = map(int, gui.stdout.readline().split())
    gui.kill()                                     # like Task Manager "End task"
    gui.wait(10)
    assert _gone(engine) and _gone(ffmpeg_like)


def _in_job(pid: int, job_handle) -> bool:
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    ph = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not ph:
        raise OSError(f"cannot open process {pid}")
    try:
        res = ctypes.c_int(0)
        if not k32.IsProcessInJob(ctypes.c_void_p(ph), job_handle, ctypes.byref(res)):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(res.value)
    finally:
        k32.CloseHandle(ctypes.c_void_p(ph))


def test_engine_children_are_created_inside_the_gui_job(tmp_path):
    """Gap I: the Engine is attached to the GUI's Job Object right after it
    is created (not CREATE_SUSPENDED). Evidence that no child escapes: the
    Engine and every child process it creates during start-up and a real job
    (ffmpeg probe, ImageWorker, FFmpeg renders, Archiver) are members of the
    job, and the first child appears only after the attachment."""
    import threading
    from videogen.core import events as ev
    from tests.pipeline_support import make_job_folder, small_settings
    from tests.production.harness import EngineHarness

    inp = tmp_path / "in"
    make_job_folder(inp, "j", n_images=3, audio_s=3.0)
    h = EngineHarness(tmp_path / "appdata", small_settings())
    seen: dict[int, tuple[float, bool, str]] = {}
    stop = threading.Event()
    h.client.start()
    t_attached = time.time()                     # EngineClient.start() returns after assign()
    engine = psutil.Process(h.pid)
    job_handle = h.client._job._handle

    def watch():
        while not stop.wait(0.02):
            try:
                kids = engine.children(recursive=True)
            except psutil.Error:
                return
            for c in kids:
                if c.pid in seen:
                    continue
                try:
                    seen[c.pid] = (c.create_time(), _in_job(c.pid, job_handle), c.name())
                except (psutil.Error, OSError):
                    continue
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    h._reader = threading.Thread(target=h._read, daemon=True)
    h._reader.start()
    try:
        assert h.wait_for(lambda: h.of(ev.EngineReady), 120)
        h.run_batch(inp, tmp_path / "out", tmp_path / "ws", timeout=300)
    finally:
        stop.set()
        t.join(5)
        engine_in_job = _in_job(engine.pid, job_handle) if engine.is_running() else None
        h.stop()
    first_child = min((v[0] for v in seen.values()), default=None)
    print(f"\nI/JobObject: engine_created={engine.create_time():.3f} attached_by={t_attached:.3f} "
          f"window_s={t_attached - engine.create_time():.3f} first_child_after_attach_s="
          f"{(first_child - t_attached) if first_child else None} children={len(seen)} "
          f"names={sorted({v[2] for v in seen.values()})} all_in_job={all(v[1] for v in seen.values())}")
    assert engine_in_job is True
    assert seen, "no child process was observed"
    assert all(v[1] for v in seen.values()), seen
    assert first_child >= t_attached - 0.05, (first_child, t_attached)

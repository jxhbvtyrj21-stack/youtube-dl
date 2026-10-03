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

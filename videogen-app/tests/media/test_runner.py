from __future__ import annotations

import sys
import threading
import time

import psutil
import pytest

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import FFmpegUnavailableError
from videogen.ffmpeg_ctl.locator import locate
from videogen.ffmpeg_ctl.process_manager import kill_tree
from videogen.ffmpeg_ctl.runner import run_tool

PY = sys.executable
SPAWN_CHILD = ("import subprocess, sys, time; "
               "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
               "print(c.pid, flush=True); time.sleep(120)")


def _dead(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def test_success_captures_output():
    out = run_tool([PY, "-c", "import sys; print('hi'); print('err', file=sys.stderr)"], timeout_s=20)
    assert out.ok and out.stdout.strip() == b"hi" and "err" in out.stderr


def test_nonzero_exit():
    out = run_tool([PY, "-c", "import sys; sys.exit(3)"], timeout_s=20)
    assert not out.ok and out.result.returncode == 3 and not out.timed_out


def test_timeout_kills_whole_tree():
    t0 = time.monotonic()
    out = run_tool([PY, "-c", SPAWN_CHILD], timeout_s=1.5)
    assert time.monotonic() - t0 < 10
    assert out.timed_out and out.result.killed_by_watchdog
    child = int(out.stdout.split()[0])
    assert _dead(out.result.pid) and _dead(child)        # no orphan grandchild


def test_cancellation_kills_tree():
    token = CancellationToken()
    threading.Timer(0.7, token.cancel).start()
    t0 = time.monotonic()
    out = run_tool([PY, "-c", SPAWN_CHILD], timeout_s=60, token=token)
    assert time.monotonic() - t0 < 10
    assert out.cancelled and not out.ok
    assert _dead(int(out.stdout.split()[0]))


def test_huge_stderr_does_not_deadlock():
    code = "import sys\nfor i in range(400000): sys.stderr.write('x' * 120 + '\\n')\nprint('done')"
    out = run_tool([PY, "-c", code], timeout_s=60)
    assert out.ok and out.stdout.strip() == b"done"
    assert len(out.stderr.splitlines()) == 400                 # bounded tail


def test_huge_stdout_is_bounded(monkeypatch):
    from videogen.ffmpeg_ctl import runner
    monkeypatch.setattr(runner, "STDOUT_MAX_BYTES", 1000)
    out = run_tool([PY, "-c", "print('y' * 100000)"], timeout_s=30)
    assert out.ok and out.stdout_truncated and len(out.stdout) <= 1000


def test_argv_must_be_list_of_str():
    with pytest.raises(TypeError):
        run_tool("ffmpeg -version", timeout_s=5)  # type: ignore[arg-type]


def test_missing_executable():
    with pytest.raises(FileNotFoundError):
        run_tool(["/nonexistent/ffmpeg"], timeout_s=5)


def test_kill_tree_does_not_kill_caller():
    import subprocess
    p = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])   # same process group as us
    assert kill_tree(p.pid, wait_s=5) == []
    p.wait(timeout=5)
    assert psutil.Process().is_running()


def test_locator_finds_system_ffmpeg():
    tools = locate()
    assert tools.ffmpeg and tools.ffprobe and tools.version


def test_locator_reports_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("VIDEOGEN_FFMPEG_DIR", raising=False)
    with pytest.raises(FFmpegUnavailableError) as ei:
        locate()
    assert "FFmpeg не знайдено" in ei.value.user_message


def test_locator_rejects_broken_binary(monkeypatch, tmp_path):
    for name in ("ffmpeg", "ffprobe"):
        f = tmp_path / name
        f.write_text("#!/bin/sh\nexit 1\n")
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("VIDEOGEN_FFMPEG_DIR", raising=False)
    with pytest.raises(FFmpegUnavailableError):
        locate()


def test_kill_tree_leaves_exit_status_to_the_owner():
    """Regression: kill_tree must not reap our own child (Popen would then
    report returncode 0 for a killed process)."""
    import subprocess
    p = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
    kill_tree(p.pid, wait_s=5)
    rc = p.wait(timeout=5)
    assert rc != 0                      # -9 on POSIX, 1 on Windows — never a fake success


def test_kill_tree_on_multiprocessing_child_does_not_leak():
    import multiprocessing
    import time as _t
    from multiprocessing import process as mp_process
    ctx = multiprocessing.get_context("spawn")
    p = ctx.Process(target=_t.sleep, args=(60,))
    p.start()
    kill_tree(p.pid, wait_s=5)
    p.join(5)
    assert p.exitcode is not None and p not in mp_process._children
    p.close()

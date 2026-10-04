"""Gap B: the real application entry point (``python -m videogen.main`` — the
same ``main()`` the packaged EXE runs) with a damaged ``settings.json``. The
GUI and the Engine start for real (offscreen Qt) and close via the packaging
smoke switch."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run_entry(appdata: Path) -> tuple[int, str, list[int]]:
    env = dict(os.environ, VIDEOGEN_APPDATA=str(appdata), QT_QPA_PLATFORM="offscreen",
               VIDEOGEN_SMOKE_EXIT_MS="6000", PYTHONIOENCODING="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "videogen.main"], cwd=str(ROOT), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    seen: set[int] = set()
    end = time.monotonic() + 90
    while proc.poll() is None and time.monotonic() < end:
        try:
            seen.update(c.pid for c in psutil.Process(proc.pid).children(recursive=True))
        except psutil.Error:
            pass
        time.sleep(0.2)
    if proc.poll() is None:
        proc.kill()
    out = proc.communicate(timeout=30)[0].decode("utf-8", "replace")
    time.sleep(2)
    left = [p for p in seen if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE]
    return proc.returncode, out, left


@pytest.mark.timeout(180)
def test_real_entry_point_with_valid_settings_keeps_them(tmp_path):
    """Control for gap B: a valid settings.json is used as is, not set aside."""
    import json
    from videogen.config.settings import Settings
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    data = Settings().to_dict()
    data["video"]["fps"] = 25
    text = json.dumps(data, ensure_ascii=False, indent=2)
    (appdata / "settings.json").write_text(text, encoding="utf-8")
    code, out, left = _run_entry(appdata)
    assert code == 0 and not (appdata / "crash.log").exists(), out[-3000:]
    assert left == []
    assert not list(appdata.glob("settings.json.corrupt-*"))
    assert (appdata / "settings.json").read_text(encoding="utf-8") == text
    assert "settings:" not in out


@pytest.mark.timeout(180)
@pytest.mark.parametrize("content", [
    "{ not json",                                         # syntactically broken
    '{"video": {"fps": "abc", "preset": 42}, "x": 1}',    # valid JSON, invalid values
    "�\x00\x01 garbage",                             # binary junk
])
def test_real_entry_point_with_damaged_settings(tmp_path, content):
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    (appdata / "settings.json").write_text(content, encoding="utf-8")
    code, out, left = _run_entry(appdata)
    app_log = (appdata / "logs" / "application.log")
    assert code == 0, out[-3000:]
    assert not (appdata / "crash.log").exists(), (appdata / "crash.log").read_text(encoding="utf-8")
    assert app_log.exists() and "engine started" in app_log.read_text(encoding="utf-8")
    assert left == [], f"processes left behind: {left}"
    # the problem is recorded, and the user's file is not silently lost
    assert "settings" in out.lower() or "налаштуван" in out.lower(), out[-3000:]
    if content.startswith("{ not") or "garbage" in content:
        backups = list(appdata.glob("settings.json.corrupt-*"))
        assert backups and backups[0].read_text(encoding="utf-8", errors="replace").startswith(content[:4]), backups

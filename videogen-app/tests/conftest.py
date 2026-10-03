from __future__ import annotations

import sys
from pathlib import Path

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("VIDEOGEN_APPDATA", str(tmp_path / "appdata"))
    yield


def _our_children():
    import psutil
    try:
        kids = psutil.Process().children(recursive=True)
    except psutil.Error:
        return []
    out = []
    for k in kids:
        try:
            if k.status() != psutil.STATUS_ZOMBIE:
                out.append((k.pid, " ".join(k.cmdline())[:120]))
        except psutil.Error:
            continue
    return out


@pytest.fixture(scope="session", autouse=True)
def _no_orphan_processes_at_end():
    """Acceptance criterion: no child process may outlive the test session."""
    import time
    yield
    deadline = time.monotonic() + 10
    left = _our_children()
    while left and time.monotonic() < deadline:
        time.sleep(0.2)
        left = [c for c in _our_children()
                if "multiprocessing" not in c[1] or "resource_tracker" not in c[1]]
    left = [c for c in left if "resource_tracker" not in c[1]]
    assert not left, f"orphan child processes: {left}"

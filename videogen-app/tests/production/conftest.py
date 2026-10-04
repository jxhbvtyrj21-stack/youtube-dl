"""Production stress / failure suite (run explicitly: ``pytest -m production``).

Scale: VIDEOGEN_PROD_SCALE=full (CI on windows-latest) or quick (default,
for development). Every test fills the ``record`` fixture; results are
written to tests/production/reports/*.json and aggregated into
STRESS_TEST_REPORT.md by report.py.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import psutil
import pytest

from tests.production import monitor

REPORTS = Path(__file__).parent / "reports"
FULL = os.environ.get("VIDEOGEN_PROD_SCALE", "quick") == "full"


def pytest_collection_modifyitems(items):
    for it in items:
        if "tests/production" in str(it.fspath).replace("\\", "/"):
            it.add_marker(pytest.mark.production)


def scale(full: int, quick: int) -> int:
    return full if FULL else quick


@pytest.fixture(scope="session")
def baseline():
    snap = {
        "media_procs": [p.pid for p in monitor.media_processes()],
        "videogen_procs": [p.pid for p in monitor.videogen_processes()],
        "rss_mb": monitor.rss_mb(os.getpid()),
        "ts": time.time(),
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / "_baseline.json").write_text(json.dumps(snap))
    return snap


@pytest.fixture()
def work(tmp_path_factory, request):
    base = os.environ.get("VIDEOGEN_PROD_DIR")
    root = Path(base) / request.node.name if base else tmp_path_factory.mktemp(request.node.name[:40])
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    yield root


@pytest.fixture()
def record(request, baseline):
    rec = {"id": request.node.name, "number": "", "title": "", "input": "", "expected": "", "actual": "",
           "resource_usage": {}, "failure_mode": "", "fix": "", "notes": []}
    t0 = time.monotonic()
    yield rec
    rec["duration_s"] = round(time.monotonic() - t0, 1)
    rep = getattr(request.node, "rep_call", None)
    if rep is None:
        rec["status"] = "ERROR"
    elif rep.skipped:
        rec["status"] = "SKIPPED"
        rec["actual"] = rec["actual"] or str(rep.longrepr)[-300:]
    else:
        rec["status"] = "PASS" if rep.passed else "FAIL"
        if rep.failed:
            rec["failure_text"] = str(rep.longrepr)[-3000:]
    rec["orphans_after"] = monitor.orphans()
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"{rec['number'] or '99'}_{request.node.name[:60]}.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when == "setup" and rep.skipped):
        item.rep_call = rep

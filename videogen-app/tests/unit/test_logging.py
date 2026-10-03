from __future__ import annotations

import json
import logging
import multiprocessing

from videogen.applog.diagnostics import prune_diagnostics, write_snapshot
from videogen.applog.logger import CallbackHandler, LogSystem, configure_child_logging
from videogen.config.settings import LoggingSettings


def _child(q, n, tag):
    configure_child_logging(q)
    lg = logging.getLogger("child")
    for i in range(n):
        lg.info("line %s %d", tag, i, extra={"job_id": tag, "event": "tick"})


def _start(tmp_path, **kw):
    sys = LogSystem(tmp_path / "logs", LoggingSettings(**kw))
    sys.install()
    return sys


def test_files_levels_and_structured_fields(tmp_path):
    ls = _start(tmp_path)
    lg = logging.getLogger("videogen.test")
    lg.debug("debug hidden at INFO")
    lg.info("рендер почато", extra={"job_id": "j1", "stage": "RENDERING", "event": "start"})
    lg.warning("повільно")
    try:
        1 / 0
    except ZeroDivisionError:
        lg.exception("помилка", extra={"job_id": "j1"})
    ls.shutdown()
    app = (tmp_path / "logs" / "application.log").read_text(encoding="utf-8")
    err = (tmp_path / "logs" / "errors.log").read_text(encoding="utf-8")
    assert "debug hidden" not in app
    assert "job=j1 stage=RENDERING event=start | рендер почато" in app
    assert "job=- stage=- event=-" in app
    assert "рендер почато" not in err
    assert "повільно" in err and "ZeroDivisionError" in err   # traceback preserved


def test_rotation_bounds_disk_usage(tmp_path):
    ls = _start(tmp_path, max_bytes=2000, backup_count=2)
    lg = logging.getLogger("videogen.rot")
    for i in range(500):
        lg.info("message number %05d with some padding text", i)
    ls.shutdown()
    names = sorted(p.name for p in (tmp_path / "logs").iterdir())
    assert "application.log" in names and "application.log.1" in names and "application.log.2" in names
    assert "application.log.3" not in names
    for p in (tmp_path / "logs").iterdir():
        assert p.stat().st_size <= 2200


def test_multiple_processes_log_through_one_listener(tmp_path):
    ls = _start(tmp_path)
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_child, args=(ls.queue, 100, f"p{i}")) for i in range(3)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(30)
        assert p.exitcode == 0
    ls.shutdown()
    app = (tmp_path / "logs" / "application.log").read_text(encoding="utf-8")
    for i in range(3):
        assert app.count(f"line p{i} ") == 100


def test_job_log_is_json_lines_with_traceback(tmp_path):
    ls = _start(tmp_path)
    ls.open_job_log("j7", tmp_path / "diag" / "j7" / "job.log")
    lg = logging.getLogger("videogen.job")
    lg.info("for j7", extra={"job_id": "j7", "stage": "MUXING", "event": "mux", "duration_ms": 12})
    lg.info("for other job", extra={"job_id": "j8"})
    try:
        raise RuntimeError("ffmpeg died")
    except RuntimeError:
        lg.exception("failed", extra={"job_id": "j7", "error": "FFMPEG_CRASH"})
    ls.close_job_log("j7")
    lg.info("after close", extra={"job_id": "j7"})
    ls.shutdown()
    lines = [json.loads(x) for x in (tmp_path / "diag" / "j7" / "job.log").read_text(encoding="utf-8").splitlines()]
    assert [x["message"] for x in lines] == ["for j7", "failed"]
    assert lines[0]["stage"] == "MUXING" and lines[0]["duration_ms"] == 12
    assert lines[1]["error"] == "FFMPEG_CRASH"
    assert "RuntimeError: ffmpeg died" in lines[1]["traceback"]


def test_callback_handler_receives_info(tmp_path):
    ls = _start(tmp_path)
    got = []
    ls.add_handler(CallbackHandler(lambda r: got.append(r.getMessage())))
    logging.getLogger("videogen.cb").info("to gui")
    logging.getLogger("videogen.cb").debug("not to gui")
    ls.shutdown()
    assert got == ["to gui"]


def test_snapshot_and_prune(tmp_path):
    import os
    import time
    diag = tmp_path / "diagnostics"
    for i in range(5):
        d = diag / f"job{i}"
        d.mkdir(parents=True)
        p = write_snapshot(d, reason="stall", pid=os.getpid(), extra={"argv": ["ffmpeg", "-i", "x"]},
                           paths=[tmp_path])
        assert p is not None
        data = json.loads(p.read_text())
        assert data["reason"] == "stall" and data["process_tree"][0]["pid"] == os.getpid()
        t = time.time() - 100 + i
        os.utime(d, (t, t))
    removed = prune_diagnostics(diag, max_jobs=2, max_mb=100)
    assert sorted(p.name for p in removed) == ["job0", "job1", "job2"]
    assert sorted(p.name for p in diag.iterdir()) == ["job3", "job4"]

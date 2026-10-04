"""14. Final resource audit — runs last."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import psutil

from tests.production import monitor
from tests.production.conftest import REPORTS


def test_14_final_resource_audit(record, baseline, tmp_path_factory):
    record.update(number="14", title="FINAL RESOURCE AUDIT", input="стан машини після всієї серії",
                  expected="0 сиріт, 0 FFmpeg, 0 процесів VideoGen, 0 відкритих файлів тестових даних, 0 "
                           "тимчасових каталогів VideoGen, цілісні БД стану, обмежені журнали")
    media = [p.pid for p in monitor.media_processes() if p.pid not in baseline["media_procs"]]
    vg = [f"{p.pid} {monitor._cmd(p)[:80]}" for p in monitor.videogen_processes()
          if p.pid not in baseline["videogen_procs"] and "resource_tracker" not in monitor._cmd(p)]
    orph = monitor.orphans()
    base = Path(os.environ.get("VIDEOGEN_PROD_DIR") or tmp_path_factory.getbasetemp())
    open_files = [f.path for f in psutil.Process().open_files() if str(base) in f.path]
    tmp = Path(tempfile.gettempdir())
    tmp_left = [p.name for p in tmp.glob("vg-ffdec-*")] + [p.name for p in tmp.glob("videogen-selftest-*")]
    ws_left, dbs, logs = [], [], []
    for ws in base.rglob("vg-*"):
        if ws.is_dir() and (ws / ".videogen-workspace").exists():
            ws_left += [str(p) for p in ws.rglob("*") if p.is_file() and p.name != ".videogen-workspace"]
    for db in base.rglob("state.db"):
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
            dbs.append((db.parent.parent.name, con.execute("PRAGMA integrity_check").fetchone()[0]))
            con.close()
        except sqlite3.Error as exc:
            dbs.append((str(db), repr(exc)))
    for lg in base.rglob("application.log*"):
        logs.append(lg.stat().st_size)
    rss = monitor.rss_mb(os.getpid())
    record["resource_usage"] = {
        "ffmpeg_left": len(media), "videogen_procs_left": len(vg), "orphans": len(orph),
        "open_test_files": len(open_files), "temp_dirs_left": len(tmp_left), "workspace_files_left": len(ws_left),
        "state_dbs_checked": len(dbs), "state_dbs_ok": sum(1 for _, r in dbs if r == "ok"),
        "largest_log_mb": round(max(logs or [0]) / 2**20, 2), "test_process_rss_mb": round(rss),
        "test_process_rss_start_mb": round(baseline["rss_mb"]),
        "disk_free_gb": round(psutil.disk_usage(str(base)).free / 2**30, 1),
    }
    record["actual"] = json.dumps(record["resource_usage"], ensure_ascii=False)
    assert not media and not vg and not orph, (media, vg, orph)
    assert not open_files and not tmp_left and not ws_left, (open_files, tmp_left, ws_left[:5])
    assert all(r == "ok" for _, r in dbs), dbs
    assert max(logs or [0]) <= 10 * 2**20 + 2**20

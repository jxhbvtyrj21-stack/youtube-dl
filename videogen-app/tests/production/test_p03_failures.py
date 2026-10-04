"""5. FFmpeg stall, 6. GUI crash, 7. Engine failure, 8. Event channel,
9. Restart/recovery, 10. State DB failures, 11. Disk space."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import psutil
import pytest

from videogen.config.settings import ResourceLimits, TimeoutPolicy
from videogen.core import events as ev
from videogen.core.models import JobStatus
from videogen.core.state_manager import StateManager
from videogen.storage.cleanup import remove_tree
from videogen.utils.hashing import sha256_file
from videogen.utils.system import InstanceLock
from tests.production import monitor
from tests.production.common import assert_no_media_processes, env_settings_json, prod_settings, ws_files
from tests.production.conftest import FULL
from tests.production.guiproc import GuiProcess
from tests.production.harness import EngineHarness
from tests.production.media_sets import normal_set

IS_WIN = os.name == "nt"


def _gone(pids, timeout=20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        alive = [p for p in pids if psutil.pid_exists(p) and _status(p) != psutil.STATUS_ZOMBIE]
        if not alive:
            return True
        time.sleep(0.2)
    return False


def _status(pid):
    try:
        return psutil.Process(pid).status()
    except psutil.Error:
        return psutil.STATUS_ZOMBIE


def _tree(pid) -> list[int]:
    try:
        p = psutil.Process(pid)
        return [p.pid] + [c.pid for c in p.children(recursive=True)]
    except psutil.Error:
        return []


def _db_rows(db: Path):
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            return con.execute("SELECT name, status, stage, resume_from_segment, output_file FROM jobs").fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return []


def _resolve_interrupted(appdata: Path, action: str = "IGNORE") -> list[str]:
    """Next start of the application: answer the recovery prompt so the
    machine returns to a clean state (IGNORE removes the kept workspace)."""
    from videogen.core.models import RecoveryAction
    h = EngineHarness(appdata, prod_settings()).start()
    try:
        found = h.of(ev.InterruptedJobsFound)
        ids = list(found[0].job_ids) if found else []
        for jid in ids:
            h.client.send(ev.RecoveryDecision(jid, RecoveryAction(action)))
        time.sleep(3)
    finally:
        h.stop()
    return ids


def _long_job(root: Path, name: str = "Довге відео", n: int = 12, spi: float = 4.0) -> Path:
    inp = root / "in"
    normal_set(inp / name, n, seconds_per_image=spi)
    return inp


# ---------------------------------------------------------------- 5

@pytest.mark.timeout(1800)
def test_05_ffmpeg_stall_lifecycle(work, record, baseline):
    record.update(number="5", title="FFMPEG STALL: повний життєвий цикл", input=(
        "2 jobs; у першому фрагмент №2 рендерить справжній ffmpeg, що чекає вхідних даних на stdin "
        "(живий, без прогресу); stall timeout 8 с"),
        expected="STALL → WATCHDOG → TIMEOUT → м'яке завершення (q) → kill дерева → cleanup → повтор → "
                 "FAILED(TIMEOUT) → наступний job SUCCESS; жодного ffmpeg у списку процесів Windows")
    inp = work / "in"
    normal_set(inp / "1 з зависанням", 4, seconds_per_image=2.0)
    normal_set(inp / "2 нормальний", 3, seconds_per_image=2.0)
    os.environ.update(VIDEOGEN_TEST_HOOKS="1", VIDEOGEN_TEST_STALL_SEGMENT="з зависанням:1")
    s = prod_settings(timeouts=TimeoutPolicy(stall_min_s=8.0, graceful_wait_s=3, terminate_wait_s=3))
    h = EngineHarness(work / "appdata", s).start()
    media_seen = []
    h.on_event = lambda e: media_seen.append(len(monitor.media_processes())) if isinstance(e, ev.JobProgress) else None
    t0 = time.monotonic()
    try:
        fin = h.run_batch(inp, work / "out", work / "ws", timeout=1500)
    finally:
        h.stop()
        os.environ.pop("VIDEOGEN_TEST_HOOKS", None)
        os.environ.pop("VIDEOGEN_TEST_STALL_SEGMENT", None)
    elapsed = time.monotonic() - t0
    by = {Path(f.output_file).stem if f.output_file else "": f for f in fin}
    stalled = [f for f in fin if f.status is JobStatus.FAILED]
    ok = [f for f in fin if f.status is JobStatus.SUCCESS]
    log = (work / "appdata" / "logs" / "application.log").read_text(encoding="utf-8")
    phases = {
        "stall_hook": "TEST HOOK: segment 2 replaced" in log,
        "watchdog_detected": bool(re.search(r"ffmpeg-seg1 pid=\d+: stall", log)),
        "retry_after_timeout": "attempt 2 started" in log,
        "failed_timeout": bool(stalled) and stalled[0].error is not None and stalled[0].error.error_class == "TIMEOUT",
        "next_job_success": bool(ok),
    }
    snaps = list((work / "appdata" / "diagnostics").rglob("snapshot-*.json"))
    snap = json.loads(snaps[0].read_text(encoding="utf-8")) if snaps else {}
    record["resource_usage"] = {"elapsed_s": round(elapsed, 1), "snapshots": len(snaps),
                                "snapshot_reason": snap.get("reason"), "max_media_procs_seen": max(media_seen or [0]),
                                **phases}
    record["actual"] = (f"фази: {', '.join(k for k, v in phases.items() if v)}; знімків стану {len(snaps)} "
                        f"(причина «{snap.get('reason')}»); перший job {stalled[0].status.value if stalled else '?'} "
                        f"({stalled[0].error.code if stalled and stalled[0].error else ''}), другий "
                        f"{ok[0].status.value if ok else '?'}; разом {elapsed:.0f} с")
    assert all(phases.values()), phases
    assert snaps and snap.get("reason") == "stall"
    assert ws_files(work / "ws") == []
    assert_no_media_processes(baseline)
    assert monitor.orphans() == []


# ---------------------------------------------------------------- 6

@pytest.mark.timeout(1800)
def test_06_gui_crash_during_render(work, record, baseline):
    record.update(number="6", title="GUI CRASH: примусове завершення GUI під час рендерингу", input=(
        "окремий процес GUI (QApplication + MainWindow + Engine) рендерить довге відео; GUI вбито "
        "(TerminateProcess) посеред рендерингу"),
        expected="GUI → Engine → FFmpeg завершуються (Windows Job Object); немає сиріт і заблокованих "
                 "файлів; немає пошкодженого виходу; workspace очищується; стан INTERRUPTED")
    inp = _long_job(work)
    g = GuiProcess(work / "appdata", inp, work / "out", work / "ws", "start", settings_json=env_settings_json(prod_settings()))
    assert g.wait(lambda s: s.get("stage") == "RENDERING" and s.get("frame", 0) > 0, 600), g.other[-20:]
    time.sleep(2)
    engine_pid = g.last["engine_pid"]
    tree = _tree(engine_pid)
    ffmpeg_pids = [p for p in tree if p in [m.pid for m in monitor.media_processes()]]
    t0 = time.monotonic()
    g.kill()
    all_gone = _gone(tree, 30)
    gone_s = time.monotonic() - t0
    out_files = [p.name for p in (work / "out").glob("*")] if (work / "out").exists() else []
    lock_free = InstanceLock(work / "appdata" / "engine.lock")
    lock_ok = lock_free.acquire()
    lock_free.release()
    rows_before = _db_rows(work / "appdata" / "state.db")
    batch_dirs = [p for p in (work / "ws").glob("vg-*")]
    cleaned = all(remove_tree(d, deadline_s=60).ok for d in batch_dirs)
    s = StateManager(work / "appdata" / "state.db")
    s.mark_running_as_interrupted()        # what the next start-up does
    interrupted = [j.name for j in s.list_jobs() if j.status is JobStatus.INTERRUPTED]
    s.close()
    mech = "Windows Job Object" if IS_WIN else "heartbeat (POSIX)"
    record["resource_usage"] = {"engine_tree_pids": len(tree), "ffmpeg_at_kill": len(ffmpeg_pids),
                                "all_gone_s": round(gone_s, 2), "mechanism": mech}
    record["actual"] = (f"дерево з {len(tree)} процесів (ffmpeg: {len(ffmpeg_pids)}) завершилося за {gone_s:.1f} с "
                        f"({mech}); у output: {out_files or 'нічого'}; блокування engine.lock вільне: {lock_ok}; "
                        f"workspace видалено: {cleaned}; стан у БД {rows_before[0][1] if rows_before else '?'} → "
                        f"INTERRUPTED: {bool(interrupted)}")
    assert all_gone and ffmpeg_pids
    assert not [f for f in out_files if f.endswith((".mp4", ".part"))]
    assert lock_ok and cleaned and interrupted
    assert_no_media_processes(baseline)
    assert monitor.orphans() == []


# ---------------------------------------------------------------- 7

@pytest.mark.timeout(1800)
def test_07_engine_failure_gui_survives(work, record, baseline):
    record.update(number="7", title="ENGINE FAILURE: обробник убито, GUI живий", input=(
        "окремий процес GUI рендерить довге відео; процес Engine вбито ззовні"),
        expected="GUI визначає втрату обробника, не зависає в очікуванні подій, показує банер; FFmpeg "
                 "убитого обробника завершується")
    inp = _long_job(work)
    g = GuiProcess(work / "appdata", inp, work / "out", work / "ws", "start", settings_json=env_settings_json(prod_settings()))
    try:
        assert g.wait(lambda s: s.get("stage") == "RENDERING" and s.get("frame", 0) > 0, 600), g.other[-20:]
        engine_pid = g.last["engine_pid"]
        tree = _tree(engine_pid)
        t0 = time.monotonic()
        psutil.Process(engine_pid).kill()
        detected = g.wait(lambda s: s.get("banner") and not s.get("engine_alive"), 15)
        detect_s = time.monotonic() - t0
        n0 = len(g.status)
        time.sleep(10)
        after = g.status[n0:]
        max_gap = max((s["max_gap_ms"] for s in after), default=-1)
        children_gone = _gone([p for p in tree if p != engine_pid], 20)
        orphan_list = monitor.orphans()
        record["resource_usage"] = {"detect_s": round(detect_s, 2), "status_lines_in_10s": len(after),
                                    "max_ui_gap_ms": max_gap, "engine_children_gone": children_gone}
        record["actual"] = (f"GUI виявив втрату за {detect_s:.1f} с; за наступні 10 с — {len(after)} оновлень "
                            f"стану, найбільша пауза циклу подій {max_gap} мс; дочірні процеси обробника "
                            f"{'завершилися' if children_gone else 'ЛИШИЛИСЯ'}")
        if not IS_WIN and not children_gone:
            record["failure_mode"] = "POSIX без Job Object: FFmpeg убитого Engine лишається (відоме обмеження)"
            for p in tree:
                if psutil.pid_exists(p):
                    psutil.Process(p).kill()
            g.kill()
            _resolve_interrupted(work / "appdata")
            pytest.skip("POSIX limitation (Job Objects are Windows-only)")
        assert detected and len(after) >= 30 and max_gap < 500
        assert children_gone and orphan_list == []
    finally:
        g.kill()
    resolved = _resolve_interrupted(work / "appdata")
    record["notes"].append(f"після перезапуску перерваних завдань: {len(resolved)} (обрано «Ігнорувати»)")
    assert resolved and ws_files(work / "ws") == []
    assert_no_media_processes(baseline)


# ---------------------------------------------------------------- 8

@pytest.mark.timeout(1800)
def test_08_event_channel_consumer_disappears(work, record, baseline):
    from videogen.gui.engine_client import EVENTS_QUEUE_MAX
    record.update(number="8", title="EVENT CHANNEL: споживач подій зник, канал переповнюється", input=(
        "GUI-процес без Job Object (VIDEOGEN_TEST_NO_JOB_OBJECT=1) рендерить довге відео; GUI-потік "
        f"«зависає» і перестає читати події, доки канал (ємність {EVENTS_QUEUE_MAX}) не заповниться і Engine "
        "не почне відкидати події; потім GUI-процес вбивається"),
        expected="поки GUI завис, Engine не блокується (рендеринг триває, канал заповнений, події "
                 "відкидаються); після зникнення GUI Engine сам це виявляє, зупиняє роботу і завершується; "
                 "FFmpeg завершено; job INTERRUPTED. Постійний регресійний тест "
                 "test_engine_exits_even_if_nobody_reads_events лишається в основному наборі")
    # The job must outlast filling the channel at its production capacity: ~3 events/s reach
    # the channel while a job runs (measured), so 1000 events need ~6 min. Rendering runs at
    # ~0.8x real time at 1080p (Windows CI) and ~15x at quick scale; 2x margin on both.
    inp = _long_job(work, n=40, spi=20.0) if FULL else _long_job(work, n=50, spi=120.0)
    db = work / "appdata" / "state.db"
    g = GuiProcess(work / "appdata", inp, work / "out", work / "ws", "freeze",
                   settings_json=env_settings_json(prod_settings()),
                   extra_env={"VIDEOGEN_TEST_NO_JOB_OBJECT": "1"})
    try:
        assert g.wait(lambda s: s.get("frozen"), 600), (g.status[-3:], g.other[-20:])
        engine_pid = next(s["engine_pid"] for s in g.status if "engine_pid" in s)
        t_freeze = time.monotonic()
        seg_at_freeze = (_db_rows(db) or [("", "", "", 0)])[0][3]
        full = g.wait(lambda s: s.get("events_queued", 0) >= EVENTS_QUEUE_MAX, 1200)
        fill_s = time.monotonic() - t_freeze
        status_at_fill = (_db_rows(db) or [("", "")])[0][1]
        time.sleep(15)                       # Engine keeps producing into the full channel
        seg_at_kill = (_db_rows(db) or [("", "", "", 0)])[0][3]
        queued = g.last.get("events_queued")
        tree = _tree(engine_pid)
    finally:
        g.kill()
    t0 = time.monotonic()
    engine_exited = _gone([engine_pid], 60)
    exit_s = time.monotonic() - t0
    rest_gone = _gone(tree, 30)
    log = (work / "appdata" / "logs" / "application.log").read_text(encoding="utf-8")
    m = re.search(r"undelivered GUI events dropped: (\d+)", log)
    dropped = int(m.group(1)) if m else -1
    record["resource_usage"] = {"channel_capacity": EVENTS_QUEUE_MAX, "events_queued_at_kill": queued,
                                "channel_fill_s": round(fill_s, 1), "job_status_when_full": status_at_fill,
                                "events_dropped": dropped,
                                "segments_rendered_while_frozen": seg_at_kill - seg_at_freeze,
                                "engine_exit_s": round(exit_s, 1),
                                "gui_gone_detected": "GUI process" in log and "is gone" in log}
    record["actual"] = (f"канал заповнено ({queued}/{EVENTS_QUEUE_MAX}) за {fill_s:.0f} с після зависання GUI; "
                        f"за час зависання відрендерено ще {seg_at_kill - seg_at_freeze} фрагм.; "
                        f"відкинуто подій: {dropped}; Engine завершився за {exit_s:.1f} с після зникнення GUI; "
                        f"дерево процесів завершено: {rest_gone}")
    s = StateManager(db)
    st = [j.status.value for j in s.list_jobs()]
    s.close()
    record["notes"].append(f"стан job після зникнення GUI: {st}")
    assert full, f"event channel never filled: {g.last}"
    assert status_at_fill == "RUNNING", "the channel must fill up while the job is still rendering"
    assert dropped > 0, "the channel was full, so the Engine must have dropped events"
    assert seg_at_kill > seg_at_freeze, "a frozen GUI must not block rendering"
    assert engine_exited and rest_gone
    assert exit_s < 15, "with the GUI gone the engine must not wait for room in the channel"
    assert "is gone; engine shutting down" in log
    assert st == ["INTERRUPTED"]                     # resumable, not cancelled
    assert _resolve_interrupted(work / "appdata") and ws_files(work / "ws") == []
    assert_no_media_processes(baseline)
    assert monitor.orphans() == []


# ---------------------------------------------------------------- 9

@pytest.mark.timeout(2400)
def test_09_restart_recovery(work, record, baseline):
    record.update(number="9", title="RESTART / RECOVERY: аварійне завершення програми і повторний запуск",
                  input="2 jobs (короткий і довгий); програму вбито, коли перший готовий, а другий рендериться",
                  expected="після запуску: INTERRUPTED знайдено, manifest і БД прочитано, відновлення "
                           "запропоновано; Resume рендерить лише решту; готовий вихід не змінено; "
                           "немає «завислих» блокувань")
    inp = work / "in"
    normal_set(inp / "1 короткий", 2, seconds_per_image=2.0)
    normal_set(inp / "2 довгий", 12, seconds_per_image=4.0)
    sj = env_settings_json(prod_settings())
    db = work / "appdata" / "state.db"
    g = GuiProcess(work / "appdata", inp, work / "out", work / "ws", "start", settings_json=sj)

    def mid(_s):
        rows = {r[0]: r for r in _db_rows(db)}
        return rows.get("1 короткий", ("", ""))[1] == "SUCCESS" and rows.get("2 довгий", ("", "", "", 0))[3] >= 2
    assert g.wait(mid, 900), (_db_rows(db), g.other[-20:])
    tree = _tree(g.last["engine_pid"])
    g.kill()
    assert _gone(tree, 30)
    rows = {r[0]: r for r in _db_rows(db)}
    first_out = Path(rows["1 короткий"][4])
    first_sha = sha256_file(first_out)[0]
    resume_point = rows["2 довгий"][3]
    log_path = work / "appdata" / "logs" / "application.log"
    log_before = log_path.read_text(encoding="utf-8")
    g2 = GuiProcess(work / "appdata", inp, work / "out", work / "ws", "recover", settings_json=sj)
    try:
        assert g2.wait(lambda s: s.get("recovered"), 120), g2.other[-20:]

        def done(_s):
            return {r[0]: r[1] for r in _db_rows(db)}.get("2 довгий") == "SUCCESS"
        assert g2.wait(done, 1500)
    finally:
        g2.kill()
    new_log = log_path.read_text(encoding="utf-8")[len(log_before):] if log_path.exists() else ""
    rendered_after = len(re.findall(r"segment \d+/12 rendered", new_log))
    final_rows = {r[0]: r for r in _db_rows(db)}
    record["resource_usage"] = {"resume_point": resume_point, "segments_rendered_after_restart": rendered_after,
                                "first_output_unchanged": sha256_file(first_out)[0] == first_sha}
    record["actual"] = (f"після перезапуску GUI отримав пропозицію відновлення і обрав Resume; "
                        f"відрендерено {rendered_after} фрагментів із 12 (готових на момент аварії: {resume_point}); "
                        f"другий job {final_rows['2 довгий'][1]}; перший вихід незмінний (SHA-256): "
                        f"{record['resource_usage']['first_output_unchanged']}; блокування engine.lock не завадило запуску")
    assert final_rows["2 довгий"][1] == "SUCCESS"
    assert sha256_file(first_out)[0] == first_sha
    assert rendered_after <= 12 - resume_point
    assert sorted(p.name for p in (work / "out").glob("*.mp4")) == ["1 короткий.mp4", "2 довгий.mp4"]
    assert_no_media_processes(baseline)


# ---------------------------------------------------------------- 10

def _hold_lock(db: str, ready, release) -> None:
    con = sqlite3.connect(db, isolation_level=None)
    con.execute("BEGIN EXCLUSIVE")
    ready.set()
    release.wait(120)
    con.execute("ROLLBACK")
    con.close()


def _writer(db: str, ready) -> None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from tests.helpers import add_jobs
    s = StateManager(Path(db))
    ids = add_jobs(s, 3000, batch_id="w")
    ready.set()
    for jid in ids:
        s.transition(jid, JobStatus.RUNNING)
        s.transition(jid, JobStatus.SUCCESS, output_file=jid)


@pytest.mark.timeout(1800)
def test_10_state_database_failures(work, record, baseline):
    record.update(number="10", title="STATE DATABASE FAILURE: 7 сценаріїв", input=(
        "пошкоджена БД; БД заблокована іншим процесом; запис перервано TerminateProcess; некоректний JSON у "
        "записі; відсутній файл стану; пошкоджений settings.json; пошкоджений manifest.json перерваного job"),
        expected="жодного зависання; де можливо — відновлення; валідна заблокована БД не переноситься в карантин")
    ctx = multiprocessing.get_context("spawn")
    results = {}

    # a) corrupted database -> real engine starts, reports, works
    a = work / "a"
    (a / "appdata").mkdir(parents=True)
    (a / "appdata" / "state.db").write_bytes(b"\x00garbage" * 2000)
    normal_set(a / "in" / "x", 2, seconds_per_image=2.0)
    t0 = time.monotonic()
    h = EngineHarness(a / "appdata", prod_settings()).start()
    try:
        errs = [e.message for e in h.of(ev.EngineError)]
        fin = h.run_batch(a / "in", a / "out", a / "ws", timeout=600)
    finally:
        h.stop()
    results["corrupted_db"] = (fin[0].status.value, any("пошкоджено" in m for m in errs), round(time.monotonic() - t0, 1))
    assert fin[0].status is JobStatus.SUCCESS and results["corrupted_db"][1]

    # b) locked database -> clear error, bounded, DB untouched
    b = work / "b" / "appdata"
    b.mkdir(parents=True)
    s = StateManager(b / "state.db")
    from tests.helpers import add_jobs
    add_jobs(s, 5)
    s.close()
    ready, release = ctx.Event(), ctx.Event()
    p = ctx.Process(target=_hold_lock, args=(str(b / "state.db"), ready, release))
    p.start()
    assert ready.wait(60)
    t0 = time.monotonic()
    h = EngineHarness(b, prod_settings())
    h.client.start()
    import threading
    h._reader = threading.Thread(target=h._read, daemon=True)
    h._reader.start()
    got = h.wait_for(lambda: h.of(ev.EngineError), 60)
    lock_s = time.monotonic() - t0
    msg = h.of(ev.EngineError)[0].message if got else ""
    exited = h.wait_for(lambda: not h.client.is_alive(), 30)
    h.client.kill()
    h._stop.set()
    release.set()
    p.join(30)
    s = StateManager(b / "state.db")
    intact = s.counters().total == 5
    s.close()
    results["locked_db"] = (msg[:80], round(lock_s, 1), exited, intact,
                            not list(b.glob("state.db.corrupt-*")))
    assert got and "заблокована" in msg and exited and intact and results["locked_db"][4]

    # c) interrupted write (process killed mid-transactions)
    c = work / "c" / "state.db"
    c.parent.mkdir(parents=True)
    r2 = ctx.Event()
    w = ctx.Process(target=_writer, args=(str(c), r2))
    w.start()
    assert r2.wait(120)
    time.sleep(0.3)
    w.kill()
    w.join(30)
    s = StateManager(c)
    cnt = s.counters("w")
    s.mark_running_as_interrupted()
    results["interrupted_write"] = (cnt.total, cnt.succeeded, s.open_report.recovered_from_corruption)
    s.close()
    assert cnt.total == 3000 and not results["interrupted_write"][2]

    # d) invalid JSON in a record
    d = work / "d" / "state.db"
    d.parent.mkdir(parents=True)
    s = StateManager(d)
    ids = add_jobs(s, 3)
    s._conn.execute("UPDATE jobs SET config_json='{oops' WHERE job_id=?", (ids[0],))
    s.close()
    s = StateManager(d)
    jobs = s.list_jobs()
    results["invalid_json_row"] = (len(jobs), s.corrupt_rows)
    s.close()
    assert len(jobs) == 2

    # e) missing state file, f) broken settings.json -> engine starts with defaults
    e_dir = work / "e" / "appdata"
    e_dir.mkdir(parents=True)
    (e_dir / "settings.json").write_text("{ not json", encoding="utf-8")
    h = EngineHarness(e_dir, prod_settings()).start()
    h.stop()
    results["missing_db_and_bad_settings"] = ((e_dir / "state.db").exists(),)
    assert (e_dir / "state.db").exists()

    # g) broken manifest.json of an interrupted job -> resume still produces a valid video
    g_root = work / "g"
    normal_set(g_root / "in" / "manifest", 3, seconds_per_image=2.0)
    h = EngineHarness(g_root / "appdata", prod_settings()).start()
    fin = h.run_batch(g_root / "in", g_root / "out", g_root / "ws", timeout=600)
    h.stop()
    s = StateManager(g_root / "appdata" / "state.db")
    jid = s.list_jobs()[0].job_id
    s._conn.execute("UPDATE jobs SET status='RUNNING', stage='RENDERING', output_file=NULL WHERE job_id=?", (jid,))
    wsd = Path(s.workspace_dir(jid))
    s.close()
    wsd.mkdir(parents=True, exist_ok=True)
    (wsd / "manifest.json").write_text("{broken", encoding="utf-8")
    for f in (g_root / "out").glob("*.mp4"):
        f.unlink()
    h = EngineHarness(g_root / "appdata", prod_settings()).start()
    try:
        assert h.wait_for(lambda: h.of(ev.InterruptedJobsFound), 60)
        from videogen.core.models import RecoveryAction
        h.client.send(ev.RecoveryDecision(jid, RecoveryAction.RESUME))
        assert h.wait_for(lambda: any(f.status is JobStatus.SUCCESS for f in h.of(ev.JobFinished)), 600)
    finally:
        h.stop()
    results["broken_manifest_resume"] = ("SUCCESS",)
    record["resource_usage"] = {k: str(v) for k, v in results.items()}
    record["actual"] = "; ".join(f"{k}: {v}" for k, v in results.items())
    assert_no_media_processes(baseline)


# ---------------------------------------------------------------- 11

SMALL = os.environ.get("VIDEOGEN_SMALL_DISK")


@pytest.mark.timeout(1800)
@pytest.mark.skipif(not SMALL, reason="needs VIDEOGEN_SMALL_DISK (a small real volume, created in CI)")
def test_11_disk_space(work, record, baseline):
    record.update(number="11", title="DISK SPACE: справжній малий том (VHD 400 МБ)", input=(
        f"workspace і output на томі {SMALL}; (a) job, що завідомо не вміщується; (b) job стартує, а під час "
        "рендерингу диск заповнюється файлом-баластом"),
        expected="(a) job не стартує, зрозуміле повідомлення, пакет на паузі; (b) активний job завершується "
                 "коректно (FAILED DISK_SPACE, без повторів), cleanup, діагностика записана, програма реагує")
    vol = Path(SMALL)
    base = vol / f"vg-{int(time.time())}"
    base.mkdir()
    s = prod_settings(resources=ResourceLimits(disk_reserve_mb=64, ram_available_min_mb=256))
    results = {}
    h = EngineHarness(work / "appdata", s).start()
    try:
        # (a) estimate does not fit
        inp_a = work / "in_a"
        normal_set(inp_a / "Не вміститься", 3, seconds_per_image=200.0)
        h.client.send(ev.StartBatch("A", "16:9", str(inp_a), str(base / "out"), str(base / "ws")))
        assert h.wait_for(lambda: any(e.status is JobStatus.FAILED for e in h.of(ev.JobFinished)), 300)
        fa = h.of(ev.JobFinished)[-1]
        assert h.wait_for(lambda: any(e.state.value == "PAUSED" for e in h.of(ev.BatchStateChanged)), 60)
        stages_a = [e.stage.value for e in h.of(ev.JobStageChanged) if e.job_id == fa.job_id]
        h.client.send(ev.Stop())
        assert h.wait_for(lambda: h.of(ev.BatchStateChanged)[-1].state.value == "IDLE", 60)
        results["a"] = (fa.error.code, fa.error.message[:90], "RENDERING" in stages_a)
        assert fa.error.code == "DISK_SPACE" and "RENDERING" not in stages_a

        # (b) disk fills up during rendering
        n0 = len(h.of(ev.JobFinished))
        inp_b = work / "in_b"
        normal_set(inp_b / "Диск заповнюється", 8, seconds_per_image=6.0)
        h.client.send(ev.StartBatch("A", "16:9", str(inp_b), str(base / "out"), str(base / "ws")))
        assert h.wait_for(lambda: any(isinstance(e, ev.JobProgress) and e.stage.value == "RENDERING"
                                      for e in h.events[-50:]), 600)
        free = psutil.disk_usage(str(vol)).free
        ballast = vol / "ballast.bin"
        with open(ballast, "wb") as fh:
            left = free - 2 * 1024 * 1024
            chunk = b"\0" * (8 * 1024 * 1024)
            while left > 0:
                fh.write(chunk[: min(len(chunk), left)])
                left -= len(chunk)
        assert h.wait_for(lambda: len(h.of(ev.JobFinished)) > n0, 600)
        fb = h.of(ev.JobFinished)[-1]
        alive = h.client.is_alive()
        ping_ok = h.client.send(ev.Ping())
        ws_left = ws_files(base / "ws")
        diag = list((work / "appdata" / "diagnostics" / fb.job_id).glob("*"))
        ballast.unlink()
        h.client.send(ev.Stop())
        h.wait_for(lambda: h.of(ev.BatchStateChanged)[-1].state.value == "IDLE", 60)
        results["b"] = (fb.status.value, fb.error.code if fb.error else "", len(ws_left), [d.name for d in diag], alive)
        assert fb.status is JobStatus.FAILED and fb.error.code == "DISK_SPACE"
        assert ws_left == [] and alive and ping_ok and diag
    finally:
        h.stop()
        shutil.rmtree(base, ignore_errors=True)
    record["resource_usage"] = {k: str(v) for k, v in results.items()}
    record["actual"] = f"(a) {results.get('a')}; (b) {results.get('b')}"
    assert_no_media_processes(baseline)

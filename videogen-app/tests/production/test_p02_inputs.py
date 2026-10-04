"""3. Real image stress, 4. Corrupted input batch, 12. Output validation, 13. Large archive."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
import zipfile
from pathlib import Path

import psutil
import pytest

from videogen.config.settings import ArchiveSettings, ImageSettings, Settings, TimeoutPolicy
from videogen.core import events as ev
from videogen.core.cancellation import CancellationToken
from videogen.core.models import JobStatus
from videogen.workers.image_worker import ImageWorkerClient
from tests.production import monitor
from tests.production.common import assert_no_media_processes, frames, prod_settings, ws_files
from tests.production.conftest import scale
from tests.production.harness import EngineHarness
from tests.production.media_sets import FFMPEG, corrupted_jpeg, corrupted_png, normal_set, problem_set, tone


@pytest.mark.timeout(3600)
def test_03_real_image_stress(work, record, baseline):
    deep = work / ("глибока папка " * 6).strip() / "Фото (2026) & #%+"
    exp = problem_set(deep)
    record.update(number="3", title=f"REAL IMAGE STRESS: {len(exp)} проблемних і незвичних файлів", input=(
        "JPG, PNG, PNG з альфою, WEBP, 108 Мп JPEG, 81 Мп PNG, 8000×400, 400×6000, EXIF 1–8, битий EXIF, "
        "LAB ICC-профіль, CMYK, 16 біт, Unicode-ім'я, ім'я 170+ символів у глибокій папці, PNG як .jpg, "
        "JPEG як .webp, пошкоджені PNG/JPEG, 0 байт, сміття, обрізані JPEG/PNG, сигнатура HEIC, "
        "«декомпресійна бомба» 225 Мп"),
        expected="жоден файл не обробляється нескінченно; кожен — валідний, відновлений або INVALID з причиною; "
                 "відео створено, статус PARTIAL (не SUCCESS)")
    tp = TimeoutPolicy()
    table = []
    worker_peak = [0.0]
    stop = threading.Event()

    def watch(client):
        while not stop.wait(0.2):
            if client._proc is not None:
                worker_peak[0] = max(worker_peak[0], monitor.rss_mb(client._proc.pid))
    with ImageWorkerClient(ImageSettings(), tp, ffmpeg=FFMPEG) as w:
        t = threading.Thread(target=watch, args=(w,), daemon=True)
        t.start()
        for i, name in enumerate(sorted(exp)):
            t0 = time.monotonic()
            r = w.normalize(i, str(deep / name), str(work / "norm" / f"i{i:05d}.jpg"), width=1920, height=1080,
                            overscan=1.15, vertical=False)
            dt = time.monotonic() - t0
            table.append({"file": name, "expected": exp[name], "ok": r.ok, "recovered": r.recovered,
                          "decoder": r.decoder, "reason": r.reason_code, "seconds": round(dt, 2)})
            assert dt < tp.image_max_s * 4, f"{name} took {dt:.0f}s"
        stop.set()
    for row in table:
        if row["expected"] == "ok":
            assert row["ok"] and not row["recovered"], row
        elif row["expected"] == "invalid":
            assert not row["ok"] and row["reason"], row
        else:
            assert (not row["ok"] and row["reason"]) or row["recovered"], row
    # the same set as one job through the real engine
    folder = work / "in" / "Проблемні файли"
    shutil.copytree(deep, folder)
    tone(folder / "audio.mp3", len(exp) * 1.6)
    h = EngineHarness(work / "appdata", prod_settings()).start()
    try:
        fin = h.run_batch(work / "in", work / "out", work / "ws", timeout=3000)
    finally:
        h.stop()
    job = fin[0]
    bad = sum(1 for r in table if not r["ok"] or r["recovered"])
    record["resource_usage"] = {"slowest_file_s": max(r["seconds"] for r in table),
                                "slowest_file": max(table, key=lambda r: r["seconds"])["file"],
                                "image_worker_peak_rss_mb": round(worker_peak[0]), "files": table}
    record["actual"] = (f"усі {len(table)} файлів оброблено; найдовше {record['resource_usage']['slowest_file_s']} с "
                        f"({record['resource_usage']['slowest_file']}); пікова RAM процесу декодування "
                        f"{round(worker_peak[0])} МБ; job: {job.status.value}, проблемних {job.skipped_images}")
    assert job.status is JobStatus.PARTIAL and job.skipped_images == bad
    assert ws_files(work / "ws") == []
    assert_no_media_processes(baseline)


@pytest.mark.timeout(3600)
def test_04_corrupted_input_batch(work, record, baseline):
    n = scale(120, 30)
    record.update(number="4", title=f"CORRUPTED INPUT: {n} нормальних + 6 пошкоджених", input=(
        f"{n} нормальних зображень і 6 пошкоджених (битий PNG, битий JPEG, 0 байт, сміття, обрізаний JPEG, "
        "чужа сигнатура) в одній папці"), expected="обробка триває; проблемні файли записано в manifest і "
        "журнал; статус PARTIAL (не SUCCESS); немає витоку процесів і тимчасових файлів")
    folder = work / "in" / "Пакет з пошкодженими"
    normal_set(folder, n, seconds_per_image=1.6)
    good = (folder / "img_00001.jpg").read_bytes()
    bad = {"img_00010_bad.png": corrupted_png(), "img_00020_bad.jpg": corrupted_jpeg(),
           "img_00030_zero.jpg": b"", "img_00040_garbage.webp": os.urandom(30000),
           "img_00050_cut.jpg": good[: len(good) // 3],
           "img_00060_heic.jpg": b"\x00\x00\x00\x18ftypheic" + os.urandom(2000)}
    for name, data in bad.items():
        (folder / name).write_bytes(data)
    tone(folder / "audio.mp3", (n + 6) * 1.6)
    h = EngineHarness(work / "appdata", prod_settings()).start()
    try:
        fin = h.run_batch(work / "in", work / "out", work / "ws", timeout=3000)
        skipped = h.of(ev.ImageSkipped)
    finally:
        h.stop()
    job = fin[0]
    diag = list((work / "appdata" / "diagnostics").glob("*/manifest.json"))
    man = json.loads(diag[0].read_text(encoding="utf-8"))
    statuses = {Path(i["source_path"]).name: (i["status"], i["reason_code"], i.get("recovered"))
                for i in man["input_files"]}
    flagged = {k for k, v in statuses.items() if v[0] == "INVALID" or v[2]}
    arch = list((work / "out" / "_archive").glob("*.zip"))
    with zipfile.ZipFile(arch[0]) as zf:
        assert zf.testzip() is None
        man_zip = json.loads(zf.read("manifest.json"))
    log_text = (work / "appdata" / "logs" / "application.log").read_text(encoding="utf-8")
    record["resource_usage"] = {"skipped_events": len(skipped), "manifest_flagged": sorted(flagged)}
    record["actual"] = (f"{job.status.value}; позначено {len(flagged)} файлів у manifest: "
                        f"{', '.join(sorted(flagged))}; вихід «{Path(job.output_file).name}»; "
                        f"архів містить manifest зі статусом {man_zip['status']}")
    assert job.status is JobStatus.PARTIAL                      # never a false SUCCESS
    assert flagged == set(bad)
    assert man["status"] == "PARTIAL" and man_zip["status"] == "PARTIAL"
    assert "[PARTIAL]" in Path(job.output_file).name
    for name in bad:
        assert name in log_text
    assert ws_files(work / "ws") == []
    assert_no_media_processes(baseline)
    assert monitor.orphans() == []


@pytest.mark.timeout(3600)
def test_12_output_validation(work, record, baseline):
    modes = ["missing", "zero", "invalid", "incomplete", "noaudio", "duration"]
    record.update(number="12", title="OUTPUT VALIDATION: 6 видів зіпсованого виходу", input=(
        "після збирання відео навмисно: файл видалено / 0 байт / випадкові байти / обрізано наполовину / "
        "без аудіо / тривалість 1 с замість 6 с"),
        expected="жоден варіант не отримує SUCCESS; у папці результатів нічого (кількість повторних спроб "
                 "тут не перевіряється — її перевіряє test_output_validation_failure_is_never_success)")
    ctl = work / "corrupt_mode.txt"
    os.environ["VIDEOGEN_TEST_HOOKS"] = "1"
    os.environ["VIDEOGEN_TEST_CORRUPT_OUTPUT_FILE"] = str(ctl)
    results = {}
    h = EngineHarness(work / "appdata", prod_settings()).start()
    try:
        for m in modes:
            ctl.write_text(m, encoding="utf-8")
            inp = work / "in" / m
            normal_set(inp / f"out_{m}", 3, seconds_per_image=2.0)
            fin = h.run_batch(inp, work / "out", work / "ws", timeout=900)
            j = fin[0]
            results[m] = (j.status.value, j.error.code if j.error else "")
    finally:
        h.stop()
        os.environ.pop("VIDEOGEN_TEST_HOOKS", None)
        os.environ.pop("VIDEOGEN_TEST_CORRUPT_OUTPUT_FILE", None)
    record["actual"] = "; ".join(f"{m}: {s} ({c})" for m, (s, c) in results.items())
    record["resource_usage"] = {"results": results}
    assert all(s == "FAILED" for s, _ in results.values()), results
    assert not list((work / "out").glob("*.mp4")) and not list((work / "out").glob(".*.part"))
    assert ws_files(work / "ws") == []
    assert_no_media_processes(baseline)


@pytest.mark.timeout(3600)
def test_13_large_archive(work, record, baseline):
    from videogen.core.pipeline import _run_archive_process
    from videogen.media.archiver import ArchiveEntry
    from PIL import Image
    n = scale(300, 30)
    record.update(number="13", title=f"LARGE ARCHIVE: {n} нестискуваних файлів", input=(
        f"{n} JPEG 3000×2000 з випадковим шумом, якість 97 (практично нестискувані, ~8 МБ кожен; "
        "фактичний обсяг — у RESOURCE USAGE)"),
        expected="архів створено потоково окремим процесом; RAM не залежить від розміру архіву; "
                 "цілісність (CRC усіх записів) підтверджено")
    src = work / "src"
    src.mkdir()
    entries = []
    for i in range(n):
        p = src / f"шум_{i:04d}.jpg"
        Image.frombytes("RGB", (3000, 2000), os.urandom(3000 * 2000 * 3)).save(p, "JPEG", quality=97)
        entries.append(ArchiveEntry(p, f"inputs/{p.name}"))
    total = sum(e.source.stat().st_size for e in entries)
    dest = work / "out" / "_archive" / "Великий архів.zip"

    class Ctx:
        token = CancellationToken()
    peak = {"child": 0.0, "test": 0.0}
    stop = threading.Event()

    def watch():
        me = psutil.Process()
        while not stop.wait(0.2):
            for c in me.children(recursive=True):
                if "resource_tracker" not in monitor._cmd(c):
                    peak["child"] = max(peak["child"], monitor.rss_mb(c.pid))
            peak["test"] = max(peak["test"], monitor.rss_mb(me.pid))
    t = threading.Thread(target=watch, daemon=True)
    t.start()
    t0 = time.monotonic()
    s = Settings(archive=ArchiveSettings(max_size_mb=100_000))
    res = _run_archive_process(dest, entries, 100_000 * 2**20, s, Ctx())
    dt = time.monotonic() - t0
    stop.set()
    t.join(5)
    t1 = time.monotonic()
    with zipfile.ZipFile(dest) as zf:
        bad = zf.testzip()
        count = len(zf.infolist())
    verify_s = time.monotonic() - t1
    size = dest.stat().st_size
    record["resource_usage"] = {"input_mb": round(total / 2**20), "archive_mb": round(size / 2**20),
                                "archive_s": round(dt, 1), "verify_s": round(verify_s, 1),
                                "archiver_peak_rss_mb": round(peak["child"]), "test_peak_rss_mb": round(peak["test"])}
    record["actual"] = (f"архів {round(size / 2**20)} МБ з {count} записів за {dt:.0f} с; CRC OK; пікова RAM "
                        f"процесу архівування {round(peak['child'])} МБ при {round(total / 2**20)} МБ даних")
    assert res.get("ok") and bad is None and count == n
    assert peak["child"] < 250, peak
    assert not list(dest.parent.glob(".*.part"))
    shutil.rmtree(src, ignore_errors=True)
    dest.unlink()

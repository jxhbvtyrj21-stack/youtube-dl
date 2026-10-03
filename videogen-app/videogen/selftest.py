"""Headless self-test of an installed / packaged build.

``videogen.exe --selftest [workdir]`` renders a tiny video end to end with
the bundled FFmpeg and checks the acceptance invariants: valid output,
exact frame count, PARTIAL for a corrupted image, no temporary files, no
child processes left, Job Objects available on Windows. Exit code 0 = OK.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path


def _make_inputs(root: Path, ffmpeg: str) -> None:
    import subprocess

    from PIL import Image

    d = root / "input" / "Самоперевірка №1"
    d.mkdir(parents=True)
    for i, (size, col) in enumerate((((640, 360), (200, 60, 60)), ((360, 640), (60, 60, 200)),
                                     ((800, 400), (60, 160, 60)))):
        Image.new("RGB", size, col).save(d / f"img_{i + 1}.{('jpg', 'png', 'webp')[i]}")
    (d / "img_4.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)        # corrupted on purpose
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3", "-ac", "2",
                    str(d / "audio.mp3")], check=True, timeout=60, stdin=subprocess.DEVNULL)


def run(workdir: str | None = None) -> int:
    import psutil

    from videogen.config.settings import EffectsSettings, ImageSettings, Settings, VideoSettings
    from videogen.core import events as ev
    from videogen.core.engine import Engine
    from videogen.core.models import JobStatus
    from videogen.ffmpeg_ctl.locator import locate
    from videogen.ffmpeg_ctl.process_manager import REGISTRY, JobObject
    from videogen.media.media_validator import probe_media
    from videogen.config.settings import TimeoutPolicy

    root = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="videogen-selftest-"))
    root.mkdir(parents=True, exist_ok=True)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"[{'OK' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}", flush=True)

    try:
        tools = locate(os.environ.get("VIDEOGEN_FFMPEG_DIR"))
        check("FFmpeg знайдено", True, f"{tools.ffmpeg} ({tools.version})")
        if os.name == "nt":
            job = JobObject("selftest")
            check("Windows Job Object створено", job.supported)
            job.close()
        _make_inputs(root, tools.ffmpeg)
        settings = Settings(
            video=VideoSettings(horizontal_width=640, horizontal_height=360, preset="veryfast"),
            images=ImageSettings(min_seconds_per_image=0.5), effects=EffectsSettings(transition_s=0.3))
        events: list[ev.Event] = []
        eng = Engine(root / "appdata", settings, events.append, tools=tools)
        t0 = time.monotonic()
        try:
            eng.startup()
            batch = eng.start_batch(ev.StartBatch("A", "16:9", str(root / "input"), str(root / "output"),
                                                  str(root / "workspace")))
            check("Пакет запущено", batch is not None)
            check("Пакет завершено вчасно", eng.wait_idle(600))
            job_state = eng.state.list_jobs()[0]
        finally:
            eng.shutdown()
        check("Статус PARTIAL (одне зображення пошкоджене)", job_state.status is JobStatus.PARTIAL,
              f"{job_state.status.value} {job_state.error.message if job_state.error else ''}")
        out = Path(job_state.output_file or "")
        check("Відео створено", out.is_file(), str(out))
        if out.is_file():
            info = probe_media(tools.ffprobe, out, TimeoutPolicy())
            check("Точна кількість кадрів", info.video_frames == 90, f"{info.video_frames} кадрів")
            check("Є аудіо", info.has_audio)
        leftovers = [p for p in (root / "workspace").rglob("*") if p.is_file() and p.name != ".videogen-workspace"]
        check("Тимчасові файли видалено", not leftovers, f"{len(leftovers)} файлів")
        kids = [c for c in psutil.Process().children(recursive=True)
                if "resource_tracker" not in " ".join(_cmd(c))]
        check("Немає залишкових процесів", not kids and not REGISTRY.snapshot(), str([k.pid for k in kids]))
        check("Час виконання", True, f"{time.monotonic() - t0:.1f} с")
    except Exception as exc:  # noqa: BLE001 - report every failure as a failed check
        check("Виняток", False, f"{exc!r}\n{traceback.format_exc()}")
    failed = [c for c in checks if not c[1]]
    print(f"\nSELFTEST {'PASSED' if not failed else 'FAILED'}: {len(checks) - len(failed)}/{len(checks)}", flush=True)
    if not workdir and not failed:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed else 1


def _cmd(p) -> list[str]:  # type: ignore[no-untyped-def]
    try:
        return p.cmdline()
    except Exception:  # noqa: BLE001
        return []


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))

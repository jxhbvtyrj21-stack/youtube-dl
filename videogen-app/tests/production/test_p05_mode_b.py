"""PHASE 11 production test: MODE B under the real production settings.

The Engine runs in its own process exactly as under the GUI (EngineClient)
and reads the API keys itself from the real Windows Credential Manager (a
test-only target prefix keeps the runner's own credentials untouched). The
services are a local imitation of ElevenLabs and OpenAI (tests/provider_mock.py)
reached through the loopback-only test hook: real API keys are not available
to CI, so the live services are never called."""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

from videogen.core.models import JobStatus
from videogen.providers.elevenlabs_tts import split_text
from tests.production.common import (
    assert_no_media_processes, expected_frames, frames, prod_settings, ws_files,
)
from tests.production.conftest import scale
from tests.production.harness import EngineHarness
from tests.provider_mock import IMG_KEY, SECONDS_PER_CHAR, TTS_KEY, MockService

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the real key store is Windows Credential Manager")

SCENE = ("Сцена {i}. Ранкове місто прокидається: трамваї рушають із депо, кав'ярні відчиняють двері, "
         "а над річкою повільно розсіюється туман.")


def _story(root: Path, n: int) -> tuple[Path, list[str], list[str]]:
    d = root / "Історія міста"
    d.mkdir(parents=True)
    scenes = [SCENE.format(i=i + 1) for i in range(n)]
    prompts = [f"Ранкове місто, кадр {i + 1}, м'яке світло, туман над річкою" for i in range(n)]
    (d / "script.txt").write_text("\n\n".join(scenes), encoding="utf-8")
    (d / "prompts.txt").write_text("\n".join(prompts), encoding="utf-8")
    return d, scenes, prompts


def _key_hits(roots: list[Path]) -> list[str]:
    hits = []
    for root in roots:
        for p in root.rglob("*") if root.exists() else []:
            if p.is_file():
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if TTS_KEY.encode() in data or IMG_KEY.encode() in data:
                    hits.append(str(p))
    return hits


@pytest.mark.timeout(3600)
def test_15_mode_b_real_settings(work, record, baseline, monkeypatch):
    from videogen.utils.credentials import WindowsCredentialStore
    n = scale(40, 6)
    record.update(number="15", title=f"MODE B: {n} сцен через адаптери ElevenLabs / OpenAI", input=(
        f"script.txt з {n} сценами і prompts.txt з {n} промптами; реальні налаштування; Engine в окремому "
        "процесі читає ключі зі справжнього Windows Credential Manager; сервіси — локальний імітатор"),
        expected="SUCCESS; точна кількість кадрів за тривалістю озвучки; рівно стільки запитів, скільки "
                 "потрібно (озвучка частинами за лімітом, одне зображення на промпт); повтор пакета — жодного "
                 "нового запиту (кеш); неправильний ключ — FAILED без повторів; ключів немає в жодному файлі; "
                 "жодного процесу FFmpeg після тесту")
    prefix = f"VideoGen-prodtest-{uuid.uuid4().hex}/"
    store = WindowsCredentialStore(prefix=prefix)
    inp, out, ws, appdata = (work / k for k in ("in", "out", "ws", "appdata"))
    _, scenes, prompts = _story(inp, n)
    s = prod_settings()
    results: dict[str, object] = {}
    with MockService() as svc:
        monkeypatch.setenv("VIDEOGEN_TEST_HOOKS", "1")
        monkeypatch.setenv("VIDEOGEN_TEST_PROVIDER_BASE_URL", svc.url)
        monkeypatch.setenv("VIDEOGEN_CREDENTIAL_PREFIX", prefix)
        try:
            store.set("elevenlabs", TTS_KEY)
            store.set("openai", IMG_KEY)
            h = EngineHarness(appdata, s).start()
            try:
                t0 = time.monotonic()
                [fin] = h.run_batch(inp, out, ws, mode="B", timeout=3000)
                first_s = time.monotonic() - t0
                calls_first = (len(svc.calls_to("tts")), len(svc.calls_to("images")))
                [again] = h.run_batch(inp, out, ws, mode="B", timeout=3000)
                calls_again = (len(svc.calls_to("tts")), len(svc.calls_to("images")))
                store.set("openai", "sk-wrong-key-000000000000")
                (inp / "Історія міста" / "prompts.txt").write_text(
                    "\n".join(p + " (інший варіант)" for p in prompts), encoding="utf-8")   # not in the cache
                [bad] = h.run_batch(inp, out, ws, mode="B", timeout=600)
                calls_bad = len(svc.calls_to("images")) - calls_again[1]
            finally:
                h.stop()
        finally:
            store.delete("elevenlabs")
            store.delete("openai")
    voice_s = sum(len(p) for p in split_text("\n\n".join(scenes), s.providers.elevenlabs_max_chars)) \
        * SECONDS_PER_CHAR
    video = Path(fin.output_file or "")
    got = frames(video) if video.is_file() else 0
    want = expected_frames(voice_s, s.video.fps)
    tts_parts = len(split_text("\n\n".join(scenes), s.providers.elevenlabs_max_chars))
    leaked = _key_hits([appdata, out, ws])
    results.update(first=(fin.status.value, round(first_s, 1)), frames=(got, want), calls_first=calls_first,
                   expected_calls=(tts_parts, n), calls_again=calls_again, again=again.status.value,
                   bad=(bad.status.value, bad.error.code if bad.error else "", calls_bad), key_files=leaked,
                   ws_files=len(ws_files(ws)), keys_left=(store.get("elevenlabs"), store.get("openai")))
    record["resource_usage"] = {k: str(v) for k, v in results.items()}
    record["actual"] = (f"{fin.status.value} за {first_s:.0f} с; кадрів {got}/{want}; запитів озвучки "
                        f"{calls_first[0]} (частин {tts_parts}), зображень {calls_first[1]}/{n}; повтор: "
                        f"{again.status.value}, нових запитів {calls_again[0] - calls_first[0]} + "
                        f"{calls_again[1] - calls_first[1]}; неправильний ключ: {bad.status.value} "
                        f"({bad.error.code if bad.error else ''}), запитів {calls_bad}; файлів із ключем: "
                        f"{len(leaked)}")
    assert fin.status is JobStatus.SUCCESS, fin.error
    # each MP3 part carries its own encoder delay/padding (tens of ms)
    assert abs(got - want) <= 3 * tts_parts, (got, want)
    assert calls_first == (tts_parts, n)
    assert again.status is JobStatus.SUCCESS and calls_again == calls_first
    assert bad.status is JobStatus.FAILED and bad.error and bad.error.code == "PROVIDER_AUTH"
    assert calls_bad == 1
    assert leaked == []
    assert ws_files(ws) == []
    assert results["keys_left"] == (None, None)
    assert_no_media_processes(baseline)
    assert os.environ.get("VIDEOGEN_TEST_PROVIDER_BASE_URL") == svc.url    # still the mock, never a real host

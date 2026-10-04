"""PHASE 11: MODE B end to end through the real ElevenLabs / OpenAI adapters,
the registry and the key store, against a local imitation of the services
(tests/provider_mock.py — no network). The Engine finds the services through
the loopback-only test hook, exactly as the packaged program would through
the real addresses."""

from __future__ import annotations

import dataclasses
import threading
import time
from pathlib import Path

import pytest

from videogen.config.settings import ProviderSettings, RetryPolicy
from videogen.core import events as ev
from videogen.core.engine import Engine
from videogen.core.models import JobStatus, Stage
from videogen.media.media_validator import probe_media
from videogen.config.settings import TimeoutPolicy
from videogen.utils.credentials import MemoryCredentialStore
from tests.fixtures import factory as F
from tests.pipeline_support import Events, small_settings, start_cmd
from tests.provider_mock import IMG_KEY, SECONDS_PER_CHAR, TTS_KEY, Behaviour, MockService

SCENES = ["Перша сцена: світанок над містом.", "Друга сцена: людний ринок.", "Третя сцена: тихий вечір."]
PROMPTS = ["Світанок над старим містом", "Людний ринок, яскраві кольори", "Тихий вечір біля річки"]


@pytest.fixture()
def svc(monkeypatch):
    with MockService() as s:
        monkeypatch.setenv("VIDEOGEN_TEST_HOOKS", "1")
        monkeypatch.setenv("VIDEOGEN_TEST_PROVIDER_BASE_URL", s.url)
        yield s


@pytest.fixture()
def dirs(tmp_path):
    d = {k: tmp_path / k for k in ("input", "output", "ws", "appdata")}
    d["input"].mkdir()
    return d


def _story(root: Path, name: str = "історія", scenes=SCENES, prompts=PROMPTS) -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "script.txt").write_text("\n\n".join(scenes), encoding="utf-8")
    (d / "prompts.txt").write_text("\n".join(prompts), encoding="utf-8")
    return d


def _settings(**prov):
    s = small_settings()
    return dataclasses.replace(s, providers=dataclasses.replace(ProviderSettings(), **prov))


class _Run:
    def __init__(self, dirs, settings=None, keys=None):
        self.events = Events()
        self.eng = Engine(dirs["appdata"], settings or _settings(), self.events,
                          credential_store=MemoryCredentialStore(keys if keys is not None else
                                                                 {"elevenlabs": TTS_KEY, "openai": IMG_KEY}))
        self.eng.startup()
        self.dirs = dirs

    def batch(self, timeout=300):
        b = self.eng.start_batch(start_cmd(self.dirs["input"], self.dirs["output"], self.dirs["ws"], mode="B"))
        assert b is not None, self.events.of(ev.EngineError)
        assert self.eng.wait_idle(timeout)
        return self.eng.state.list_jobs(batch_id=b)

    def close(self):
        self.eng.shutdown()


def _files_containing(root: Path, needle: bytes) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                if needle in p.read_bytes():
                    hits.append(str(p))
            except OSError:
                continue
    return hits


@pytest.mark.timeout(600)
def test_mode_b_job_succeeds_through_the_real_adapters(svc, dirs):
    _story(dirs["input"])
    r = _Run(dirs)
    try:
        [job] = r.batch()
    finally:
        r.close()
    assert job.status is JobStatus.SUCCESS, job.error
    out = Path(job.output_file)
    info = probe_media(F.FFPROBE, out, TimeoutPolicy())
    voice_s = len("\n\n".join(SCENES)) * SECONDS_PER_CHAR
    assert abs(info.duration_s - max(1.0, voice_s)) < 0.5 and info.video_frames > 0
    assert len(svc.calls_to("tts")) == 1 and len(svc.calls_to("images")) == len(PROMPTS)
    assert [c.body["prompt"] for c in svc.calls_to("images")] == PROMPTS
    assert svc.calls_to("images")[0].body["size"] == "1536x1024"
    assert any(e.stage is Stage.GENERATING for e in r.events.of(ev.JobStageChanged))
    # the keys are nowhere on disk: not in logs, manifest, archive, settings, workspace or output
    for root in (dirs["appdata"], dirs["output"], dirs["ws"]):
        assert _files_containing(root, TTS_KEY.encode()) == []
        assert _files_containing(root, IMG_KEY.encode()) == []


@pytest.mark.timeout(600)
def test_repeating_the_batch_does_not_call_the_services_again(svc, dirs):
    _story(dirs["input"])
    r = _Run(dirs)
    try:
        [first] = r.batch()
        calls = len(svc.calls)
        [second] = r.batch()
    finally:
        r.close()
    assert first.status is second.status is JobStatus.SUCCESS
    assert len(svc.calls) == calls                                 # all from the cache: nothing paid twice


@pytest.mark.timeout(600)
def test_temporary_server_errors_are_retried_and_the_job_succeeds(svc, dirs):
    _story(dirs["input"])
    svc.push("tts", Behaviour("status", 503, b"{}"))
    svc.push("images", Behaviour("status", 500, b"{}"))
    r = _Run(dirs)
    try:
        [job] = r.batch()
    finally:
        r.close()
    assert job.status is JobStatus.SUCCESS, job.error
    assert len(svc.calls_to("tts")) == 2 and len(svc.calls_to("images")) == len(PROMPTS) + 1


@pytest.mark.timeout(600)
def test_invalid_key_fails_the_job_once_without_retries(svc, dirs):
    _story(dirs["input"])
    r = _Run(dirs, keys={"elevenlabs": "sk_wrong_000000000000", "openai": IMG_KEY})
    try:
        [job] = r.batch()
    finally:
        r.close()
    assert job.status is JobStatus.FAILED and job.error.code == "PROVIDER_AUTH"
    assert job.error.error_class == "INPUT" and job.attempts == 1
    assert len(svc.calls_to("tts")) == 1 and svc.calls_to("images") == []
    assert "sk_wrong" not in job.error.message + job.error.detail
    assert not list(dirs["output"].glob("*.mp4"))


@pytest.mark.timeout(600)
def test_refused_prompt_fails_the_job_with_a_clear_reason(svc, dirs):
    _story(dirs["input"])
    r = _Run(dirs)
    real = svc._payload

    def refuse_second(route, h, body):
        if route == "images" and body.get("prompt") == PROMPTS[1]:
            return 400, b'{"error": {"code": "moderation_blocked"}}', "application/json"
        return real(route, h, body)
    svc._payload = refuse_second
    try:
        [job] = r.batch()
    finally:
        r.close()
    assert job.status is JobStatus.FAILED and job.error.code == "PROVIDER_REJECTED"
    assert job.attempts == 1 and "prompts.txt" in job.error.message
    assert len([c for c in svc.calls_to("images") if c.body["prompt"] == PROMPTS[1]]) == 1


@pytest.mark.timeout(600)
def test_hanging_service_is_bounded(svc, dirs):
    _story(dirs["input"])
    svc.push("tts", *[Behaviour("hang", seconds=60)] * 5)
    s = dataclasses.replace(_settings(read_timeout_s=5.0), retry=RetryPolicy(transient=0, transient_backoff_s=(0.0,)))
    r = _Run(dirs, settings=s)
    t0 = time.monotonic()
    try:
        [job] = r.batch(timeout=200)
    finally:
        r.close()
    took = time.monotonic() - t0
    assert job.status is JobStatus.FAILED and job.error.code == "PROVIDER_TIMEOUT"
    assert job.error.error_class == "TRANSIENT"
    assert len(svc.calls_to("tts")) == 3                     # 1 + 2 bounded repeats, then the job fails
    assert took < 3 * 5 + 2 + 6 + 30, took


@pytest.mark.timeout(600)
def test_stop_during_generation_cancels_promptly(svc, dirs):
    _story(dirs["input"])
    svc.push("images", Behaviour("hang", seconds=60))
    r = _Run(dirs)
    try:
        b = r.eng.start_batch(start_cmd(dirs["input"], dirs["output"], dirs["ws"], mode="B"))
        assert b is not None
        deadline = time.monotonic() + 60
        while not svc.calls_to("images") and time.monotonic() < deadline:
            time.sleep(0.05)
        assert svc.calls_to("images"), "generation never started"
        t0 = time.monotonic()
        threading.Thread(target=r.eng.handle, args=(ev.Stop(),), daemon=True).start()
        assert r.eng.wait_idle(30)
        stopped_in = time.monotonic() - t0
        [job] = r.eng.state.list_jobs(batch_id=b)
    finally:
        r.close()
    assert job.status is JobStatus.CANCELLED
    assert stopped_in < 10, stopped_in                     # not the 180 s read timeout
    assert not list(dirs["output"].glob("*.mp4"))

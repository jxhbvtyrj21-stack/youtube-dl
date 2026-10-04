"""ElevenLabs / OpenAI adapters and the provider registry against the local
imitation of the services (tests/provider_mock.py)."""

from __future__ import annotations

import json

import pytest
from PIL import Image

from videogen.config.settings import ProviderSettings
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import InputError, TransientError
from videogen.providers.elevenlabs_tts import ElevenLabsTTS, looks_like_mp3, split_text
from videogen.providers.http import HttpPolicy
from videogen.providers.openai_images import OpenAIImages, image_size
from videogen.providers.registry import build_providers
from videogen.utils.credentials import MemoryCredentialStore, UnavailableCredentialStore
from tests.provider_mock import IMG_KEY, SECONDS_PER_CHAR, TTS_KEY, Behaviour, MockService

FAST = HttpPolicy(connect_timeout_s=2, read_timeout_s=5, retries=2, backoff_s=(0.01, 0.01))


@pytest.fixture()
def svc():
    with MockService() as s:
        yield s


def _tts(svc, **kw):
    return ElevenLabsTTS(TTS_KEY, voice_id="voice1", model_id="eleven_multilingual_v2", policy=FAST,
                         base_url=svc.url, **kw)


# ------------------------------------------------------------------ text split

def test_split_text_keeps_everything_in_order_and_respects_the_limit():
    text = "\n\n".join([("Речення номер %d. " % i) * 30 for i in range(5)] + ["Коротке."])
    parts = split_text(text, 500)
    assert all(len(p) <= 500 for p in parts)
    assert " ".join(p.replace("\n\n", " ") for p in parts).split() == text.split()


def test_split_text_hard_cuts_an_endless_sentence():
    parts = split_text("слово " * 400, 100)
    assert all(len(p) <= 100 for p in parts) and " ".join(parts).split() == ("слово " * 400).split()


def test_split_text_merges_short_paragraphs():
    assert split_text("Один.\n\nДва.\n\nТри.", 100) == ["Один.\n\nДва.\n\nТри."]


# ------------------------------------------------------------------ ElevenLabs

def test_elevenlabs_request_contract(svc, tmp_path):
    out = tmp_path / "voice.mp3"
    _tts(svc).synthesize("Привіт, світе.", out, timeout_s=60, token=CancellationToken())
    [c] = svc.calls_to("tts")
    assert c.path == "/v1/text-to-speech/voice1?output_format=mp3_44100_128"
    assert c.headers["xi-api-key"] == TTS_KEY and c.headers["content-type"] == "application/json"
    assert c.body == {"text": "Привіт, світе.", "model_id": "eleven_multilingual_v2"}
    assert looks_like_mp3(out.read_bytes())
    assert not list((tmp_path / "voice-parts").iterdir())


def test_long_script_is_split_and_joined(svc, tmp_path):
    text = "\n\n".join(f"Абзац {i}. " + "Текст сцени. " * 20 for i in range(6))
    out = tmp_path / "voice.mp3"
    _tts(svc, max_chars=300).synthesize(text, out, timeout_s=60, token=CancellationToken())
    calls = svc.calls_to("tts")
    assert len(calls) == len(split_text(text, 300)) > 1
    assert all(len(c.body["text"]) <= 300 for c in calls)
    from videogen.media.media_validator import probe_media
    from videogen.config.settings import TimeoutPolicy
    from tests.fixtures import factory as F
    single = sum(len(c.body["text"]) for c in calls) * SECONDS_PER_CHAR
    dur = probe_media(F.FFPROBE, out, TimeoutPolicy()).duration_s
    assert abs(dur - single) < 0.5 * len(calls)          # the parts are all there, in one playable file


def test_parts_already_paid_for_are_not_requested_again(svc, tmp_path):
    """A job attempt that fails on the last part keeps the parts it received;
    the next attempt requests only the missing one."""
    text = "\n\n".join(f"Абзац {i}. " + "Текст. " * 40 for i in range(3))
    pieces = split_text(text, 300)
    assert len(pieces) >= 3
    real = svc._payload
    state = {"failed": False}

    def fail_last_once(route, h, body):
        if route == "tts" and body.get("text") == pieces[-1] and not state["failed"]:
            state["failed"] = True
            return 503, b"{}", "application/json"
        return real(route, h, body)
    svc._payload = fail_last_once
    out = tmp_path / "voice.mp3"
    no_retry = HttpPolicy(connect_timeout_s=2, read_timeout_s=5, retries=0)
    with pytest.raises(TransientError):
        ElevenLabsTTS(TTS_KEY, voice_id="voice1", model_id="m1", base_url=svc.url, max_chars=300,
                      policy=no_retry).synthesize(text, out, timeout_s=60, token=CancellationToken())
    assert len(svc.calls_to("tts")) == len(pieces)
    ElevenLabsTTS(TTS_KEY, voice_id="voice1", model_id="m1", base_url=svc.url, max_chars=300,
                  policy=no_retry).synthesize(text, out, timeout_s=60, token=CancellationToken())
    assert len(svc.calls_to("tts")) == len(pieces) + 1          # only the missing part
    assert looks_like_mp3(out.read_bytes())


def test_elevenlabs_quota_and_auth_are_permanent(svc, tmp_path):
    svc.push("tts", Behaviour("status", 401, json.dumps({"detail": {"status": "quota_exceeded"}}).encode()))
    with pytest.raises(InputError) as ei:
        _tts(svc).synthesize("Текст.", tmp_path / "v.mp3", timeout_s=60, token=CancellationToken())
    assert ei.value.code == "PROVIDER_QUOTA" and len(svc.calls) == 1
    with pytest.raises(InputError) as ei:
        ElevenLabsTTS("sk_bad_key_000000", voice_id="v", model_id="m", base_url=svc.url,
                      policy=FAST).synthesize("Текст.", tmp_path / "v.mp3", timeout_s=60, token=CancellationToken())
    assert ei.value.code == "PROVIDER_AUTH" and "sk_bad_key_000000" not in ei.value.detail


def test_elevenlabs_non_audio_answer_is_rejected(svc, tmp_path):
    svc.push("tts", *[Behaviour("garbage")] * 3)
    with pytest.raises(TransientError) as ei:
        _tts(svc).synthesize("Текст.", tmp_path / "v.mp3", timeout_s=60, token=CancellationToken())
    assert ei.value.code == "PROVIDER_BAD_RESPONSE"


def test_elevenlabs_rejects_unsafe_ids():
    for bad in ("../x", "a/b", "", "v?x=1"):
        with pytest.raises(InputError):
            ElevenLabsTTS("k", voice_id=bad, model_id="m")


# ------------------------------------------------------------------ OpenAI

@pytest.mark.parametrize("model,w,h,size", [
    ("gpt-image-1", 1920, 1080, "1536x1024"), ("gpt-image-1", 1080, 1920, "1024x1536"),
    ("dall-e-3", 1920, 1080, "1792x1024"), ("dall-e-3", 1080, 1920, "1024x1792"), ("dall-e-2", 1920, 1080, "1024x1024"),
])
def test_image_size_follows_orientation(model, w, h, size):
    assert image_size(model, w, h) == size


def test_openai_request_contract_and_result(svc, tmp_path):
    out = tmp_path / "g.png"
    OpenAIImages(IMG_KEY, policy=FAST, base_url=svc.url).generate(
        "Захід сонця над морем", out, width=1920, height=1080, timeout_s=60, token=CancellationToken())
    [c] = svc.calls_to("images")
    assert c.path == "/v1/images/generations" and c.headers["authorization"] == f"Bearer {IMG_KEY}"
    assert c.body == {"model": "gpt-image-1", "prompt": "Захід сонця над морем", "n": 1, "size": "1536x1024",
                      "quality": "medium"}
    with Image.open(out) as im:
        assert im.size == (1536, 1024)
    assert not list(tmp_path.glob("*.json"))


def test_dalle3_asks_for_base64(svc, tmp_path):
    OpenAIImages(IMG_KEY, model="dall-e-3", quality="high", policy=FAST, base_url=svc.url).generate(
        "Гори", tmp_path / "g.png", width=1080, height=1920, timeout_s=60, token=CancellationToken())
    b = svc.calls_to("images")[0].body
    assert b["response_format"] == "b64_json" and b["quality"] == "hd" and b["size"] == "1024x1792"


@pytest.mark.parametrize("body,code", [
    ({"error": {"code": "moderation_blocked", "message": "rejected by the safety system"}}, "PROVIDER_REJECTED"),
    ({"error": {"code": "content_policy_violation"}}, "PROVIDER_REJECTED"),
    ({"error": {"code": "insufficient_quota"}}, "PROVIDER_QUOTA"),
])
def test_openai_permanent_refusals(svc, tmp_path, body, code):
    svc.push("images", Behaviour("status", 400 if code != "PROVIDER_QUOTA" else 429, json.dumps(body).encode()))
    with pytest.raises(InputError) as ei:
        OpenAIImages(IMG_KEY, policy=FAST, base_url=svc.url).generate(
            "x", tmp_path / "g.png", width=1920, height=1080, timeout_s=60, token=CancellationToken())
    assert ei.value.code == code and len(svc.calls) == 1        # never retried


def test_openai_auth_error_does_not_leak_the_key(svc, tmp_path):
    with pytest.raises(InputError) as ei:
        OpenAIImages("sk-wrong-000000000000", policy=FAST, base_url=svc.url).generate(
            "x", tmp_path / "g.png", width=1920, height=1080, timeout_s=60, token=CancellationToken())
    assert ei.value.code == "PROVIDER_AUTH"
    assert "sk-" not in ei.value.detail and "sk-" not in ei.value.user_message


@pytest.mark.parametrize("payload", [b"{not json", b'{"data": []}', b'{"data": [{"b64_json": "!!!"}]}',
                                     b'{"data": [{"b64_json": "aGVsbG8="}]}'])
def test_openai_bad_answers_are_transient(svc, tmp_path, payload):
    svc.push("images", Behaviour("status", 200, payload))
    with pytest.raises(TransientError) as ei:
        OpenAIImages(IMG_KEY, policy=HttpPolicy(retries=0), base_url=svc.url).generate(
            "x", tmp_path / "g.png", width=1920, height=1080, timeout_s=60, token=CancellationToken())
    assert ei.value.code == "PROVIDER_BAD_RESPONSE" and not (tmp_path / "g.png").exists()


def test_empty_and_overlong_prompts_are_input_errors(tmp_path):
    p = OpenAIImages(IMG_KEY, model="dall-e-3")
    for prompt, code in (("  ", "PROMPT_EMPTY"), ("x" * 5000, "PROMPT_TOO_LONG")):
        with pytest.raises(InputError) as ei:
            p.generate(prompt, tmp_path / "g.png", width=10, height=10, timeout_s=1, token=CancellationToken())
        assert ei.value.code == code


# ------------------------------------------------------------------ registry

def test_registry_reports_missing_keys_without_values():
    setup = build_providers(ProviderSettings(), MemoryCredentialStore({"openai": IMG_KEY}))
    assert not setup.ready and setup.missing == ["ElevenLabs (озвучка)"]
    assert "ElevenLabs" in setup.explanation() and IMG_KEY not in setup.explanation()


def test_registry_without_a_secure_store():
    setup = build_providers(ProviderSettings(), UnavailableCredentialStore())
    assert not setup.ready and "Windows" in setup.explanation()


def test_registry_builds_both_providers():
    setup = build_providers(ProviderSettings(), MemoryCredentialStore({"openai": IMG_KEY, "elevenlabs": TTS_KEY}))
    assert setup.ready and setup.explanation() == ""
    assert setup.tts.base_url == "https://api.elevenlabs.io" and setup.images.base_url == "https://api.openai.com"


def test_registry_bad_voice_id_is_a_clear_problem():
    import dataclasses
    s = dataclasses.replace(ProviderSettings(), elevenlabs_voice_id="../../x")
    setup = build_providers(s, MemoryCredentialStore({"openai": IMG_KEY, "elevenlabs": TTS_KEY}))
    assert not setup.ready and "голос" in setup.explanation()


def test_test_base_url_hook_accepts_only_loopback(monkeypatch):
    store = MemoryCredentialStore({"openai": IMG_KEY, "elevenlabs": TTS_KEY})
    monkeypatch.setenv("VIDEOGEN_TEST_PROVIDER_BASE_URL", "http://127.0.0.1:9")
    assert build_providers(ProviderSettings(), store).tts.base_url == "https://api.elevenlabs.io"   # hooks off
    monkeypatch.setenv("VIDEOGEN_TEST_HOOKS", "1")
    assert build_providers(ProviderSettings(), store).tts.base_url == "http://127.0.0.1:9"
    monkeypatch.setenv("VIDEOGEN_TEST_PROVIDER_BASE_URL", "https://evil.example.com")
    assert build_providers(ProviderSettings(), store).images.base_url == "https://api.openai.com"

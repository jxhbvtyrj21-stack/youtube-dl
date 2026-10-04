"""Bounded HTTP layer of the MODE B providers against a local mock service."""

from __future__ import annotations

import json
import threading
import time

import pytest

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import InputError, JobCancelledError, TransientError
from videogen.providers.http import HttpPolicy, redact, request, retry_after_s
from tests.provider_mock import TTS_KEY, Behaviour, MockService

FAST = HttpPolicy(connect_timeout_s=2, read_timeout_s=2, total_timeout_s=10, retries=2, backoff_s=(0.01, 0.02))


@pytest.fixture()
def svc():
    with MockService() as s:
        yield s


def _post(svc, tmp_path, token=None, policy=FAST, key=TTS_KEY, sleeps=None):
    def sleep(s):
        if sleeps is not None:
            sleeps.append(s)
        return False
    return request("POST", f"{svc.url}/v1/text-to-speech/voice1", service="Тест",
                   headers={"xi-api-key": key, "Content-Type": "application/json"},
                   body=json.dumps({"text": "Привіт"}).encode(), out_path=tmp_path / "out.bin",
                   policy=policy, token=token or CancellationToken(), secrets=(key,), sleep=sleep)


def test_success_streams_body_to_file(svc, tmp_path):
    r = _post(svc, tmp_path)
    assert r.status == 200 and r.size == r.path.stat().st_size > 0
    assert r.path.read_bytes()[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2") or r.path.read_bytes()[0] == 0xFF
    assert len(svc.calls) == 1


def test_5xx_is_retried_with_backoff_then_succeeds(svc, tmp_path):
    svc.push("tts", Behaviour("status", 502, b"{}"), Behaviour("status", 503, b"{}"))
    sleeps: list[float] = []
    r = _post(svc, tmp_path, sleeps=sleeps)
    assert r.status == 200 and len(svc.calls) == 3
    assert sleeps == [0.01, 0.02]


def test_retries_are_bounded_and_the_error_is_transient(svc, tmp_path):
    svc.push("tts", *[Behaviour("status", 500, b"boom")] * 5)
    with pytest.raises(TransientError) as ei:
        _post(svc, tmp_path)
    assert ei.value.code == "PROVIDER_SERVER"
    assert len(svc.calls) == 3                      # 1 + retries(2), never more
    assert not (tmp_path / "out.bin").exists()


def test_auth_failure_is_permanent_and_never_retried(svc, tmp_path):
    with pytest.raises(InputError) as ei:
        _post(svc, tmp_path, key="sk_wrong_key_123456")
    assert ei.value.code == "PROVIDER_AUTH" and len(svc.calls) == 1
    assert "sk_wrong_key_123456" not in ei.value.detail + ei.value.user_message


def test_rate_limit_honours_retry_after_with_a_cap(svc, tmp_path):
    svc.push("tts", Behaviour("status", 429, b"{}", {"Retry-After": "5"}))
    sleeps: list[float] = []
    pol = HttpPolicy(connect_timeout_s=2, read_timeout_s=2, retries=1, backoff_s=(0.01,), retry_after_max_s=3)
    _post(svc, tmp_path, policy=pol, sleeps=sleeps)
    assert sleeps == [3]


def test_read_timeout_is_bounded(svc, tmp_path):
    svc.push("tts", Behaviour("hang", seconds=5))
    pol = HttpPolicy(connect_timeout_s=1, read_timeout_s=0.5, retries=0)
    t0 = time.monotonic()
    with pytest.raises(TransientError) as ei:
        _post(svc, tmp_path, policy=pol)
    assert ei.value.code == "PROVIDER_TIMEOUT" and time.monotonic() - t0 < 3


def test_truncated_body_is_a_transient_failure_not_a_result(svc, tmp_path):
    svc.push("tts", Behaviour("truncate"))
    pol = HttpPolicy(connect_timeout_s=2, read_timeout_s=2, retries=0)
    with pytest.raises(TransientError):
        _post(svc, tmp_path, policy=pol)
    assert not (tmp_path / "out.bin").exists()


def test_response_size_is_limited(svc, tmp_path):
    pol = HttpPolicy(connect_timeout_s=2, read_timeout_s=2, retries=0, max_bytes=1000)
    with pytest.raises(InputError) as ei:
        _post(svc, tmp_path, policy=pol)
    assert ei.value.code == "PROVIDER_RESPONSE_TOO_LARGE" and not (tmp_path / "out.bin").exists()


def test_connection_refused_is_transient(tmp_path):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(TransientError) as ei:
        request("POST", f"http://127.0.0.1:{port}/x", service="Тест", headers={}, body=b"{}",
                out_path=tmp_path / "o", policy=HttpPolicy(connect_timeout_s=1, retries=1, backoff_s=(0.01,)),
                token=CancellationToken())
    assert ei.value.code == "PROVIDER_NETWORK"


def test_stop_interrupts_a_request_waiting_for_the_service(svc, tmp_path):
    svc.push("tts", Behaviour("hang", seconds=20))
    token = CancellationToken()
    threading.Timer(0.5, token.cancel).start()
    t0 = time.monotonic()
    with pytest.raises(JobCancelledError):
        _post(svc, tmp_path, token=token, policy=HttpPolicy(connect_timeout_s=2, read_timeout_s=30, retries=2))
    assert time.monotonic() - t0 < 3                  # not the 30 s read timeout
    assert not (tmp_path / "out.bin").exists()


def test_stop_interrupts_the_pause_between_retries(svc, tmp_path):
    svc.push("tts", Behaviour("status", 500, b"{}"))
    token = CancellationToken()
    threading.Timer(0.3, token.cancel).start()
    t0 = time.monotonic()
    with pytest.raises(JobCancelledError):
        request("POST", f"{svc.url}/v1/text-to-speech/v", service="Тест", headers={"xi-api-key": TTS_KEY},
                body=b"{}", out_path=tmp_path / "o", token=token,
                policy=HttpPolicy(connect_timeout_s=2, read_timeout_s=2, retries=1, backoff_s=(30.0,)))
    assert time.monotonic() - t0 < 3


def test_provider_classifier_overrides_the_generic_mapping(svc, tmp_path):
    svc.push("tts", Behaviour("status", 400, b'{"error": {"code": "content_policy_violation"}}'))

    def classify(status, snippet):
        if "content_policy" in snippet:
            return InputError("відхилено", code="PROVIDER_REJECTED", detail=snippet)
        return None
    with pytest.raises(InputError) as ei:
        request("POST", f"{svc.url}/v1/text-to-speech/v", service="Тест", headers={"xi-api-key": TTS_KEY},
                body=b"{}", out_path=tmp_path / "o", policy=FAST, token=CancellationToken(), classify=classify)
    assert ei.value.code == "PROVIDER_REJECTED" and len(svc.calls) == 1


def test_redact_removes_keys_and_key_shapes():
    assert "abc" not in redact("key abcdef1234 used", ("abcdef1234",))
    assert redact("Incorrect API key provided: sk-tes***cdef") == "Incorrect API key provided: ***"
    assert redact("sk_" + "a1" * 24) == "***"


def test_retry_after_parsing():
    assert retry_after_s("7", 60) == 7
    assert retry_after_s("700", 60) == 60
    assert retry_after_s(None, 60) is None and retry_after_s("soon", 60) is None

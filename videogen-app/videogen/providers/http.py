"""Bounded HTTP for MODE B providers (ARCHITECTURE.md §21).

``http.client`` from the standard library (no new dependency) because the
caller must own the socket: STOP shuts it down at once through the
cancellation token, and the read timeout is set separately from the connect
timeout. Proxies come from the system settings (``urllib.request.getproxies``:
environment variables, and the registry on Windows).

Every request:
  * connect timeout; a timeout on every socket read (including the wait for
    the service to generate the result); a deadline for the whole request;
  * response body streamed to a file, never more than ``max_bytes``;
  * at most ``retries`` repetitions with the pauses ``backoff_s`` (or the
    server's ``Retry-After``, capped) — only for retryable failures;
  * failures classified: network / timeout / 5xx / 429 → ``TransientError``;
    401/403, quota, content refusal and other 4xx → ``InputError`` (never
    repeated);
  * API keys never appear in messages: response snippets are redacted.
"""

from __future__ import annotations

import base64
import email.utils
import http.client
import logging
import re
import socket
import ssl
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from videogen.core.cancellation import CancellationToken
from videogen.core.errors import InputError, JobCancelledError, TransientError, VideoGenError

log = logging.getLogger(__name__)

CHUNK = 64 * 1024
SNIPPET = 2048
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
_KEY_PATTERN = re.compile(r"(sk[-_][A-Za-z0-9_\-*]{4,}|[A-Fa-f0-9]{32,})")   # OpenAI / ElevenLabs key shapes


@dataclass(frozen=True)
class HttpPolicy:
    connect_timeout_s: float = 15.0
    read_timeout_s: float = 180.0
    total_timeout_s: float = 600.0
    retries: int = 2
    backoff_s: tuple[float, ...] = field(default=(2.0, 6.0))
    retry_after_max_s: float = 60.0
    max_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class HttpResult:
    status: int
    headers: dict[str, str]
    path: Path
    size: int


# Hook for provider-specific error mapping: (status, redacted body snippet) ->
# a classified error, or None for the generic mapping.
Classifier = Callable[[int, str], "VideoGenError | None"]


def redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return _KEY_PATTERN.sub("***", text)


def generic_error(service: str, status: int, snippet: str) -> VideoGenError:
    if status in (401, 403):
        return InputError(f"{service}: ключ API недійсний або не має доступу. Перевірте ключ у меню «Ключі API».",
                          code="PROVIDER_AUTH", detail=f"HTTP {status}: {snippet}")
    if status == 429:
        return TransientError(f"{service}: перевищено ліміт запитів.", code="PROVIDER_RATE_LIMIT",
                              detail=f"HTTP 429: {snippet}")
    if status >= 500:
        return TransientError(f"{service}: сервіс тимчасово недоступний (HTTP {status}).", code="PROVIDER_SERVER",
                              detail=snippet)
    return InputError(f"{service}: сервіс відхилив запит (HTTP {status}).", code="PROVIDER_BAD_REQUEST",
                      detail=snippet)


def retry_after_s(value: str | None, cap: float) -> float | None:
    if not value:
        return None
    try:
        secs = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        secs = when.timestamp() - time.time()
    return max(0.0, min(secs, cap))


def _connection(url: str, policy: HttpPolicy) -> tuple[http.client.HTTPConnection, str, dict[str, str]]:
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise InputError("Некоректна адреса сервісу.", code="PROVIDER_BAD_URL", detail=url)
    host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
    extra: dict[str, str] = {}
    proxy = None
    if host not in _LOOPBACK and not urllib.request.proxy_bypass(host):
        proxy = urllib.request.getproxies().get(u.scheme)
    ctx = ssl.create_default_context()
    if not proxy:
        cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
        kw = {"context": ctx} if u.scheme == "https" else {}
        return cls(host, port, timeout=policy.connect_timeout_s, **kw), path, extra
    p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if p.username:
        cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
        extra["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
    pport = p.port or (443 if p.scheme == "https" else 80)
    if u.scheme == "https":
        conn = http.client.HTTPSConnection(p.hostname or "", pport, timeout=policy.connect_timeout_s, context=ctx)
        conn.set_tunnel(host, port, headers=dict(extra))
        return conn, path, {}
    return http.client.HTTPConnection(p.hostname or "", pport, timeout=policy.connect_timeout_s), url, extra


def _shutdown(conn: http.client.HTTPConnection) -> None:
    sock = conn.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            log.debug("socket already closed")


def _attempt(method: str, url: str, headers: dict[str, str], body: bytes | None, out_path: Path,
             policy: HttpPolicy, token: CancellationToken) -> tuple[int, dict[str, str], int]:
    conn, path, extra = _connection(url, policy)
    handle = token.register(lambda: _shutdown(conn))
    deadline = time.monotonic() + policy.total_timeout_s
    try:
        conn.connect()
        if conn.sock is not None:
            conn.sock.settimeout(policy.read_timeout_s)
        conn.request(method, path, body=body, headers={**headers, **extra})
        resp = conn.getresponse()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        size = 0
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as fh:
            for _ in range(policy.max_bytes // CHUNK + 2):     # bounded by max_bytes
                if token.cancelled:
                    break
                if time.monotonic() > deadline:
                    raise socket.timeout("total request deadline exceeded")
                block = resp.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > policy.max_bytes:
                    raise InputError("Відповідь сервісу завелика.", code="PROVIDER_RESPONSE_TOO_LARGE",
                                     detail=f"> {policy.max_bytes} bytes")
                fh.write(block)
        declared = hdrs.get("content-length")
        if not token.cancelled and declared and declared.isdigit() and int(declared) != size:
            raise http.client.IncompleteRead(b"", int(declared) - size)
        return resp.status, hdrs, size
    finally:
        token.unregister(handle)
        conn.close()


def request(method: str, url: str, *, service: str, headers: dict[str, str], body: bytes | None,
            out_path: Path, policy: HttpPolicy, token: CancellationToken,
            classify: Classifier | None = None, secrets: tuple[str, ...] = (),
            sleep: Callable[[float], bool] | None = None) -> HttpResult:
    """Perform the request with bounded retries; on success the 2xx body is in
    ``out_path``. Raises a classified ``VideoGenError`` or ``JobCancelledError``."""
    wait = sleep or token.wait
    last: VideoGenError | None = None
    for attempt in range(policy.retries + 1):
        token.raise_if_cancelled()
        pause: float | None = None
        try:
            status, hdrs, size = _attempt(method, url, headers, body, out_path, policy, token)
        except InputError:
            out_path.unlink(missing_ok=True)
            raise
        except (OSError, http.client.HTTPException) as exc:
            out_path.unlink(missing_ok=True)
            if token.cancelled:
                raise JobCancelledError() from exc
            timed_out = isinstance(exc, (socket.timeout, TimeoutError))
            last = TransientError(
                f"{service}: сервіс не відповів вчасно." if timed_out else f"{service}: помилка мережі.",
                code="PROVIDER_TIMEOUT" if timed_out else "PROVIDER_NETWORK",
                detail=redact(repr(exc), secrets))
        else:
            if token.cancelled:
                out_path.unlink(missing_ok=True)
                raise JobCancelledError()
            if 200 <= status < 300:
                return HttpResult(status, hdrs, out_path, size)
            snippet = redact(out_path.read_bytes()[:SNIPPET].decode("utf-8", "replace"), secrets)
            out_path.unlink(missing_ok=True)
            err = (classify(status, snippet) if classify else None) or generic_error(service, status, snippet)
            if not isinstance(err, TransientError):
                raise err
            last = err
            if status == 429 or status == 503:
                pause = retry_after_s(hdrs.get("retry-after"), policy.retry_after_max_s)
        log.warning("%s: attempt %d failed: %s", service, attempt + 1, last.code)
        if attempt < policy.retries:
            backoff = policy.backoff_s[min(attempt, len(policy.backoff_s) - 1)] if policy.backoff_s else 0.0
            if wait(max(backoff, pause or 0.0)):
                raise JobCancelledError()
    assert last is not None
    raise last

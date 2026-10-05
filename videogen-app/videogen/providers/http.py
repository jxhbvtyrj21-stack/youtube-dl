"""Bounded HTTP for MODE B providers (ARCHITECTURE.md §21).

``http.client`` from the standard library (no new dependency) parses HTTP,
but the program owns every socket operation: the socket is non-blocking and
each wait (connect, TLS handshake, send, every read) happens in steps of
``POLL_S`` that check the cancellation token and the deadlines. STOP is
therefore seen within ``POLL_S`` in the thread that runs the request, the
socket is closed there and the partial file removed, and only then does the
caller get ``JobCancelledError``. No background thread exists and nothing
depends on how the OS treats a socket closed under a blocked read (Windows
does not wake it — Windows CI, e96ad07). The one call that cannot be
interrupted is the host name lookup (``getaddrinfo``), bounded by the OS
resolver. Proxies come from the system settings
(``urllib.request.getproxies``: environment variables, and the registry on
Windows).

Every request:
  * connect timeout; at most ``read_timeout_s`` without any data (including
    the wait for the service to generate the result); a deadline for the
    whole request;
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
import errno
import http.client
import io
import logging
import re
import select
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
POLL_S = 0.1                     # longest single wait on a socket: STOP is seen this soon
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


class _Cancelled(Exception):
    """STOP seen inside a socket wait; becomes JobCancelledError in request()."""


def _wait(sock: socket.socket, *, write: bool, token: CancellationToken, deadline: float, what: str) -> None:
    """Wait until ``sock`` is ready, in steps of POLL_S, checking STOP and the deadline."""
    for _ in range(int(max(0.0, deadline - time.monotonic()) / POLL_S) + 2):
        if token.cancelled:
            raise _Cancelled()
        left = deadline - time.monotonic()
        if left <= 0:
            break
        step = min(POLL_S, left)
        r, w, x = select.select([] if write else [sock], [sock] if write else [], [sock], step)
        if r or w or x:
            return
    if token.cancelled:
        raise _Cancelled()
    raise socket.timeout(f"{what} timed out")


class _PollingSocket:
    """A non-blocking socket whose blocking-style methods (used by
    http.client) wait only through :func:`_wait`."""

    def __init__(self, raw: socket.socket, token: CancellationToken, policy: HttpPolicy) -> None:
        raw.setblocking(False)
        self.raw, self.token, self.policy = raw, token, policy

    def __getattr__(self, name: str) -> object:          # setsockopt, fileno, ...
        return getattr(self.raw, name)

    def recv_into(self, buf: memoryview | bytearray) -> int:
        deadline = time.monotonic() + self.policy.read_timeout_s
        for _ in range(int(self.policy.read_timeout_s / POLL_S) + 3):
            if self.token.cancelled:
                raise _Cancelled()
            try:
                return self.raw.recv_into(buf)
            except (BlockingIOError, ssl.SSLWantReadError):
                _wait(self.raw, write=False, token=self.token, deadline=deadline, what="read")
            except ssl.SSLWantWriteError:
                _wait(self.raw, write=True, token=self.token, deadline=deadline, what="read")
        raise socket.timeout("read timed out")

    def sendall(self, data: bytes) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + self.policy.read_timeout_s
        for _ in range(len(view) + int(self.policy.read_timeout_s / POLL_S) + 3):
            if not view:
                return
            if self.token.cancelled:
                raise _Cancelled()
            try:
                view = view[self.raw.send(view):]
            except (BlockingIOError, ssl.SSLWantWriteError):
                _wait(self.raw, write=True, token=self.token, deadline=deadline, what="send")
            except ssl.SSLWantReadError:
                _wait(self.raw, write=False, token=self.token, deadline=deadline, what="send")
        if view:
            raise socket.timeout("send timed out")

    def makefile(self, mode: str = "rb", *args: object, **kwargs: object) -> io.BufferedReader:
        return io.BufferedReader(_PollingReader(self))

    def close(self) -> None:
        self.raw.close()


class _PollingReader(io.RawIOBase):
    """The response stream of http.client over a :class:`_PollingSocket`;
    closing it does not close the socket (as with ``socket.makefile``)."""

    def __init__(self, sock: _PollingSocket) -> None:
        self._sock = sock

    def readable(self) -> bool:
        return True

    def readinto(self, buf: memoryview | bytearray) -> int:  # type: ignore[override]
        return self._sock.recv_into(buf)


def _polled_connect(address: tuple[str, int], token: CancellationToken, policy: HttpPolicy) -> _PollingSocket:
    host, port = address
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)   # not interruptible: OS resolver
    deadline = time.monotonic() + policy.connect_timeout_s
    last: OSError | None = None
    for family, kind, proto, _, sockaddr in infos:
        raw = socket.socket(family, kind, proto)
        try:
            raw.setblocking(False)
            err = raw.connect_ex(sockaddr)
            if err not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, getattr(errno, "WSAEWOULDBLOCK", -1)):
                raise OSError(err, f"connect failed ({err})")
            if err:
                _wait(raw, write=True, token=token, deadline=deadline, what="connect")
                err = raw.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err:
                    raise OSError(err, f"connect failed ({err})")
            return _PollingSocket(raw, token, policy)
        except _Cancelled:
            raw.close()
            raise
        except OSError as exc:
            raw.close()
            last = exc
    raise last or OSError("no address to connect to")


class _HTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, *, token: CancellationToken, policy: HttpPolicy) -> None:
        super().__init__(host, port, timeout=policy.connect_timeout_s)
        self._create_connection = lambda address, *_: _polled_connect(address, token, policy)


class _HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, *, token: CancellationToken, policy: HttpPolicy,
                 context: ssl.SSLContext) -> None:
        super().__init__(host, port, timeout=policy.connect_timeout_s, context=context)
        self._create_connection = lambda address, *_: _polled_connect(address, token, policy)
        self._token, self._policy = token, policy

    def connect(self) -> None:
        http.client.HTTPConnection.connect(self)            # TCP (+ proxy CONNECT), polled
        plain = self.sock
        assert isinstance(plain, _PollingSocket)
        tls = self._context.wrap_socket(plain.raw, server_hostname=self._tunnel_host or self.host,
                                        do_handshake_on_connect=False)
        tls.setblocking(False)
        deadline = time.monotonic() + self._policy.connect_timeout_s
        for _ in range(int(self._policy.connect_timeout_s / POLL_S) + 3):
            try:
                tls.do_handshake()
                break
            except ssl.SSLWantReadError:
                _wait(tls, write=False, token=self._token, deadline=deadline, what="TLS handshake")
            except ssl.SSLWantWriteError:
                _wait(tls, write=True, token=self._token, deadline=deadline, what="TLS handshake")
        else:
            raise socket.timeout("TLS handshake timed out")
        self.sock = _PollingSocket(tls, self._token, self._policy)


def _connection(url: str, policy: HttpPolicy,
                token: CancellationToken) -> tuple[http.client.HTTPConnection, str, dict[str, str]]:
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
        if u.scheme == "https":
            return _HTTPSConnection(host, port, token=token, policy=policy, context=ctx), path, extra
        return _HTTPConnection(host, port, token=token, policy=policy), path, extra
    p = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    if p.username:
        cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
        extra["Proxy-Authorization"] = "Basic " + base64.b64encode(cred).decode()
    pport = p.port or (443 if p.scheme == "https" else 80)
    if u.scheme == "https":
        conn = _HTTPSConnection(p.hostname or "", pport, token=token, policy=policy, context=ctx)
        conn.set_tunnel(host, port, headers=dict(extra))
        return conn, path, {}
    return _HTTPConnection(p.hostname or "", pport, token=token, policy=policy), url, extra


def _attempt(method: str, url: str, headers: dict[str, str], body: bytes | None, out_path: Path,
             policy: HttpPolicy, token: CancellationToken) -> tuple[int, dict[str, str], int]:
    """One exchange, in the calling thread. Every socket wait is a
    cancellation point; on any exit the socket is closed and, unless the
    exchange completed, the partial file removed."""
    conn, path, extra = _connection(url, policy, token)
    deadline = time.monotonic() + policy.total_timeout_s
    completed = False
    try:
        conn.request(method, path, body=body, headers={**headers, **extra})
        resp = conn.getresponse()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        size = 0
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "wb") as fh:
            for _ in range(policy.max_bytes // CHUNK + 2):     # bounded by max_bytes
                if token.cancelled:
                    raise _Cancelled()
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
        if declared and declared.isdigit() and int(declared) != size:
            raise http.client.IncompleteRead(b"", int(declared) - size)
        completed = True
        return resp.status, hdrs, size
    finally:
        conn.close()
        if not completed:
            out_path.unlink(missing_ok=True)


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
        except _Cancelled as exc:
            raise JobCancelledError() from exc
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

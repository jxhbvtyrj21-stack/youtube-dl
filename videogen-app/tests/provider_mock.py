"""Local stand-in for the ElevenLabs and OpenAI HTTP APIs (no network).

Built from the public API contracts the adapters implement:
  * ``POST /v1/text-to-speech/{voice_id}?output_format=...`` with header
    ``xi-api-key`` and JSON ``{"text", "model_id"}`` → ``audio/mpeg`` body;
  * ``POST /v1/images/generations`` with ``Authorization: Bearer <key>`` and
    JSON ``{"model", "prompt", "size", ...}`` → ``{"data": [{"b64_json"}]}``.

Each request is recorded. A test can queue behaviours per route
(``"tts"`` / ``"images"`` / ``"any"``) that are consumed in order; when the
queue is empty the route answers normally.
"""

from __future__ import annotations

import base64
import io
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from PIL import Image, ImageDraw

TTS_KEY = "sk_test_eleven_0123456789abcdef"
IMG_KEY = "sk-test-openai-0123456789abcdef"
SECONDS_PER_CHAR = 0.05          # duration of the imitated voice


@dataclass
class Call:
    route: str
    path: str
    headers: dict[str, str]
    body: dict[str, Any]


@dataclass
class Behaviour:
    kind: str                      # ok | status | hang | truncate | garbage
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0


def mp3_for(text: str, seconds_per_char: float = SECONDS_PER_CHAR, min_s: float = 1.0) -> bytes:
    ff = shutil.which("ffmpeg")
    assert ff, "ffmpeg required for the TTS mock"
    dur = max(min_s, len(text) * seconds_per_char)
    return subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", f"sine=f=300:d={dur:.3f}", "-ac", "1",
                           "-ar", "44100", "-b:a", "128k", "-f", "mp3", "-"],
                          capture_output=True, check=True, timeout=60, stdin=subprocess.DEVNULL).stdout


def png_for(prompt: str, size: str) -> bytes:
    w, h = (int(x) for x in size.split("x"))
    seed = sum(prompt.encode()) % 200
    im = Image.new("RGB", (w, h), (40 + seed, 120, 200 - seed // 2))
    ImageDraw.Draw(im).rectangle((w // 3, h // 3, 2 * w // 3, 2 * h // 3), fill=(250, 250, 250))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


class MockService:
    def __init__(self, *, tts_key: str = TTS_KEY, img_key: str = IMG_KEY, tts_chars_limit: int = 10_000) -> None:
        self.tts_key, self.img_key = tts_key, img_key
        self.tts_chars_limit = tts_chars_limit
        self.calls: list[Call] = []
        self.queue: dict[str, list[Behaviour]] = {"tts": [], "images": [], "any": []}
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ control
    def push(self, route: str, *behaviours: Behaviour) -> None:
        with self._lock:
            self.queue[route].extend(behaviours)

    def calls_to(self, route: str) -> list[Call]:
        return [c for c in self.calls if c.route == route]

    @property
    def url(self) -> str:
        assert self._server is not None
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "MockService":
        svc = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:   # keep test output clean
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                svc._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.1},
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)

    def __enter__(self) -> "MockService":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # ------------------------------------------------------------ serving
    def _next(self, route: str) -> Behaviour | None:
        with self._lock:
            for key in (route, "any"):
                if self.queue[key]:
                    return self.queue[key].pop(0)
        return None

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, body: bytes, ctype: str,
              headers: dict[str, str] | None = None, declared: int | None = None) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body) if declared is None else declared))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        try:
            h.wfile.write(body)
            h.wfile.flush()
        except OSError:
            return

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        n = int(h.headers.get("Content-Length") or 0)
        raw = h.rfile.read(n) if n else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"_raw": raw.decode("utf-8", "replace")}
        route = "tts" if h.path.startswith("/v1/text-to-speech/") else \
            "images" if h.path.startswith("/v1/images/generations") else "other"
        with self._lock:
            self.calls.append(Call(route, h.path, {k.lower(): v for k, v in h.headers.items()}, body))
        b = self._next(route)
        if b is not None:
            if b.kind == "hang":
                time.sleep(b.seconds)
                return self._send(h, 500, b"late", "text/plain")
            if b.kind == "status":
                return self._send(h, b.status, b.body, "application/json", b.headers)
            if b.kind == "truncate":
                payload = self._payload(route, h, body)[1]
                return self._send(h, 200, payload[: len(payload) // 2], "application/octet-stream",
                                  declared=len(payload))
            if b.kind == "garbage":
                return self._send(h, 200, b.body or b"\x00garbage" * 50, "application/octet-stream")
        status, payload, ctype = self._payload(route, h, body)
        self._send(h, status, payload, ctype)

    def _payload(self, route: str, h: BaseHTTPRequestHandler, body: dict[str, Any]) -> tuple[int, bytes, str]:
        if route == "tts":
            if h.headers.get("xi-api-key") != self.tts_key:
                return 401, json.dumps({"detail": {"status": "invalid_api_key",
                                                   "message": "Invalid API key"}}).encode(), "application/json"
            text = str(body.get("text", ""))
            if len(text) > self.tts_chars_limit:
                return 400, json.dumps({"detail": {"status": "max_character_limit_exceeded"}}).encode(), \
                    "application/json"
            return 200, mp3_for(text), "audio/mpeg"
        if route == "images":
            if h.headers.get("Authorization") != f"Bearer {self.img_key}":
                return 401, json.dumps({"error": {"message": "Incorrect API key provided: sk-tes***cdef",
                                                  "code": "invalid_api_key"}}).encode(), "application/json"
            png = png_for(str(body.get("prompt", "")), str(body.get("size", "1024x1024")))
            return 200, json.dumps({"created": int(time.time()),
                                    "data": [{"b64_json": base64.b64encode(png).decode()}]}).encode(), \
                "application/json"
        return 404, b"{}", "application/json"

"""Isolated image validation + normalisation process (ARCHITECTURE.md §3, §6.3).

Why a process: a native decoder that hangs inside C code cannot be
interrupted from a thread, and a segfault would take the Engine down. Here
the worst case is "kill this worker, start a fresh one, try the next
decoder".

Protocol (``multiprocessing.Pipe``):
  parent -> child: ``NormalizeRequest`` | ``None`` (shutdown)
  child -> parent: ``("validated", ValidationResult)``,
                   ``("trying", decoder_name)``,
                   ``("result", NormalizeResult)``

The parent never blocks without a deadline: it polls the pipe in short
slices and checks cancellation, hard timeouts and worker liveness.
"""

from __future__ import annotations

import dataclasses
import logging
import multiprocessing
import os
import time
from dataclasses import dataclass, field
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

from videogen.config.settings import ImageSettings, TimeoutPolicy
from videogen.core import timeouts
from videogen.core.cancellation import CancellationToken
from videogen.core.errors import JobCancelledError
from videogen.core.models import ImageItem, ImageStatus
from videogen.ffmpeg_ctl.process_manager import REGISTRY, kill_tree

log = logging.getLogger(__name__)

TEST_HOOKS_ENV = "VIDEOGEN_TEST_HOOKS"
#: failure codes that mean "the data is damaged" (as opposed to "unsupported")
DAMAGE_CODES = frozenset({"CORRUPT", "TRUNCATED", "DECODE_FAILED", "TIMEOUT", "CRASH"})
IDLE_EXIT_S = 900.0          # a forgotten worker exits by itself
POLL_SLICE_S = 0.2


@dataclass(frozen=True)
class NormalizeRequest:
    index: int
    src: str
    dst: str
    width: int
    height: int
    overscan: float
    vertical: bool
    decoders: tuple[str, ...]
    debug_action: str = ""
    prior_damage: bool = False   # an earlier decoder hung/crashed on this file


@dataclass
class NormalizeResult:
    index: int
    ok: bool
    src: str = ""
    dst: str = ""
    decoder: str = ""
    decoders_tried: list[str] = field(default_factory=list)
    detected_format: str = ""
    size_bytes: int = 0
    sha256: str = ""
    hash_mode: str = "full"
    width: int = 0
    height: int = 0
    mode: str = ""
    has_alpha: bool = False
    fit_mode: str = ""
    cropped_fraction: float = 0.0
    recovered: bool = False
    warnings: list[str] = field(default_factory=list)
    reason_code: str = ""
    message: str = ""
    detail: str = ""

    def to_image_item(self) -> ImageItem:
        return ImageItem(
            index=self.index, source_path=self.src,
            status=ImageStatus.NORMALIZED if self.ok else ImageStatus.INVALID,
            size_bytes=self.size_bytes, sha256=self.sha256, hash_mode=self.hash_mode,
            detected_format=self.detected_format, width=self.width, height=self.height,
            mode=self.mode, has_alpha=self.has_alpha, decoder=self.decoder,
            decoders_tried=list(self.decoders_tried), recovered=self.recovered,
            normalized_path=self.dst if self.ok else "",
            reason_code=self.reason_code, message=self.message, warnings=list(self.warnings))


# ====================================================================== child

def _hook(req: NormalizeRequest, decoder: str) -> None:
    if not req.debug_action or os.environ.get(TEST_HOOKS_ENV) != "1":
        return
    action, _, target = req.debug_action.partition(":")
    if target and target != decoder:
        return
    if action == "hang":
        time.sleep(3600)
    elif action == "crash":
        os._exit(3)
    elif action == "fail":
        raise RuntimeError("test hook failure")


def _child_main(conn: Connection, settings_dict: dict[str, Any], timeout_dict: dict[str, Any],
                ffmpeg: str | None, log_queue: Any, max_requests: int) -> None:
    from PIL import Image

    from videogen.applog.logger import configure_child_logging
    from videogen.media.image_normalizer import DecodeError, NormalizeTarget, normalize_with
    from videogen.media.image_validator import REASONS, validate_image

    if log_queue is not None:
        configure_child_logging(log_queue)
    settings = ImageSettings(**settings_dict)
    tpolicy = TimeoutPolicy(**timeout_dict)
    Image.MAX_IMAGE_PIXELS = settings.max_pixels
    parent = os.getppid()

    for _ in range(max_requests):
        req = None
        idle_deadline = time.monotonic() + IDLE_EXIT_S
        while time.monotonic() < idle_deadline:
            if conn.poll(1.0):
                req = conn.recv()
                break
            if os.getppid() != parent:   # parent died: do not linger as an orphan
                return
        if req is None:
            return
        res = NormalizeResult(index=req.index, ok=False, src=req.src, dst=req.dst)
        try:
            v = validate_image(req.src, settings)
            conn.send(("validated", v))
            for f in ("detected_format", "size_bytes", "sha256", "hash_mode", "width", "height",
                      "mode", "has_alpha"):
                setattr(res, f, getattr(v, f))
            res.warnings.extend(v.warnings)
            if not v.ok:
                res.reason_code, res.detail = v.reason_code, v.detail
                res.message = REASONS.get(v.reason_code, v.reason_code)
                conn.send(("result", res))
                continue
            target = NormalizeTarget(req.width, req.height, req.overscan, req.vertical)
            ff_timeout = timeouts.image_normalize(tpolicy, v.width * v.height).hard_s
            last_code, last_detail = "DECODE_FAILED", ""
            damage_seen = req.prior_damage
            for decoder in req.decoders:
                conn.send(("trying", decoder))
                res.decoders_tried.append(decoder)
                try:
                    _hook(req, decoder)
                    outcome = normalize_with(decoder, req.src, Path(req.dst), target, settings,
                                             ffmpeg=ffmpeg, ffmpeg_timeout_s=ff_timeout)
                except DecodeError as exc:
                    last_code, last_detail = exc.code, exc.detail
                    damage_seen = damage_seen or exc.code in DAMAGE_CODES
                    res.warnings.append(f"декодер {decoder} не впорався: {exc.code}")
                    continue
                except MemoryError:
                    last_code, last_detail = "TOO_LARGE", "MemoryError"
                    res.warnings.append(f"декодер {decoder}: недостатньо пам'яті")
                    continue
                except Exception as exc:  # noqa: BLE001 - any decoder failure -> next decoder
                    last_code, last_detail = _classify_decode_exc(exc)
                    damage_seen = damage_seen or last_code in DAMAGE_CODES
                    res.warnings.append(f"декодер {decoder} не впорався: {exc.__class__.__name__}")
                    continue
                res.ok, res.decoder = True, decoder
                if damage_seen:
                    res.recovered = True
                    res.warnings.append(
                        f"файл пошкоджений; відновлено резервним декодером {decoder} — можливі артефакти")
                res.fit_mode, res.cropped_fraction = outcome.fit_mode, outcome.cropped_fraction
                res.warnings.extend(outcome.warnings)
                break
            if not res.ok:
                res.reason_code = "DECODE_FAILED" if last_code in ("DECODE_FAILED", "") else last_code
                res.message = REASONS.get(res.reason_code, res.reason_code)
                res.detail = last_detail
                try:
                    os.unlink(req.dst)
                except OSError:
                    pass  # invariant-ok: nothing was written
        except Exception as exc:  # noqa: BLE001 - protocol must always answer
            res.ok = False
            res.reason_code, res.detail = "DECODE_FAILED", repr(exc)
            res.message = REASONS["DECODE_FAILED"]
        conn.send(("result", res))


def _classify_decode_exc(exc: BaseException) -> tuple[str, str]:
    text = repr(exc)
    low = text.lower()
    if "truncated" in low or "premature end" in low:
        return "TRUNCATED", text
    if "decompressionbomb" in low:
        return "TOO_LARGE", text
    if isinstance(exc, (SyntaxError, ValueError, OSError)):
        return "CORRUPT", text
    return "DECODE_FAILED", text


# ===================================================================== parent

class ImageWorkerClient:
    """Owns one worker process; restarts it on hang/crash/recycle.

    Use one client per job (``with ImageWorkerClient(...) as w:``).
    """

    def __init__(self, settings: ImageSettings, tpolicy: TimeoutPolicy, *, ffmpeg: str | None = None,
                 log_queue: Any = None, recycle_after: int = 200,
                 decoders: tuple[str, ...] = ("pillow", "opencv", "ffmpeg")) -> None:
        self.settings = settings
        self.tpolicy = tpolicy
        self.ffmpeg = ffmpeg
        self.log_queue = log_queue
        self.recycle_after = max(1, recycle_after)
        self.decoders = decoders
        self._ctx = multiprocessing.get_context("spawn")
        self._proc: Any = None
        self._conn: Connection | None = None
        self._served = 0
        self.restarts = 0
        self.pids: list[int] = []

    # ---------------------------------------------------------- lifecycle

    def __enter__(self) -> "ImageWorkerClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _start(self) -> None:
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_child_main, name="ImageWorker", daemon=True,
            args=(child_conn, dataclasses.asdict(self.settings), dataclasses.asdict(self.tpolicy),
                  self.ffmpeg, self.log_queue, self.recycle_after))
        proc.start()
        child_conn.close()
        self._proc, self._conn, self._served = proc, parent_conn, 0
        self.pids.append(proc.pid)
        REGISTRY.add(proc.pid, "ImageWorker")

    def _kill(self, reason: str, *, count_restart: bool = True) -> None:
        proc, conn = self._proc, self._conn
        self._proc, self._conn = None, None
        if conn is not None:
            conn.close()
        if proc is not None:
            if proc.is_alive():
                log.warning("killing image worker pid=%s: %s", proc.pid, reason,
                            extra={"event": "worker_kill"})
                kill_tree(proc.pid, wait_s=5.0)
            proc.join(timeout=5.0)
            REGISTRY.remove(proc.pid)
            try:
                proc.close()
            except ValueError:
                pass  # invariant-ok: process object still considered running; GC will reap
            if count_restart:
                self.restarts += 1

    def close(self) -> None:
        proc, conn = self._proc, self._conn
        if proc is None:
            return
        try:
            if conn is not None:
                conn.send(None)
        except (OSError, ValueError):
            pass  # invariant-ok: worker already gone
        proc.join(timeout=3.0)
        self._kill("close", count_restart=False)

    def _ensure(self) -> None:
        if self._proc is not None and (not self._proc.is_alive() or self._served >= self.recycle_after):
            if self._served >= self.recycle_after:
                self.close()
            else:
                self._kill("worker died")
        if self._proc is None:
            self._start()

    # ---------------------------------------------------------- request

    def normalize(self, index: int, src: str, dst: str, *, width: int, height: int, overscan: float,
                  vertical: bool, token: CancellationToken | None = None,
                  debug_action: str = "") -> NormalizeResult:
        remaining = list(self.decoders)
        tried: list[str] = []
        failures: list[str] = []
        validated: Any = None
        for _ in range(len(self.decoders) + 1):
            self._ensure()
            assert self._conn is not None
            req = NormalizeRequest(index, src, dst, width, height, overscan, vertical,
                                   tuple(remaining), debug_action, prior_damage=bool(tried))
            self._conn.send(req)
            self._served += 1
            outcome = self._await(req, token)
            kind = outcome[0]
            if kind == "result":
                res: NormalizeResult = outcome[1]
                res.decoders_tried = tried + res.decoders_tried
                res.warnings = failures + res.warnings
                return res
            # hang or crash on a specific decoder: restart and skip that decoder
            stage_validated, current = outcome[1], outcome[2]
            validated = stage_validated or validated
            if current is None:
                # failed before any decoder ran (validation itself hung/crashed)
                return self._invalid(index, src, dst, "TIMEOUT" if kind == "timeout" else "CRASH",
                                     validated, tried, failures)
            tried.append(current)
            failures.append(f"декодер {current}: {'перевищено час' if kind == 'timeout' else 'аварійне завершення'}")
            remaining = [d for d in remaining if d != current and d not in tried]
            if not remaining:
                return self._invalid(index, src, dst, "TIMEOUT" if kind == "timeout" else "CRASH",
                                     validated, tried, failures)
        return self._invalid(index, src, dst, "DECODE_FAILED", validated, tried, failures)

    def _invalid(self, index: int, src: str, dst: str, code: str, validated: Any,
                 tried: list[str], failures: list[str]) -> NormalizeResult:
        from videogen.media.image_validator import REASONS
        res = NormalizeResult(index=index, ok=False, src=src, dst=dst, decoders_tried=tried,
                              reason_code=code, message=REASONS.get(code, code), warnings=failures)
        if validated is not None:
            for f in ("detected_format", "size_bytes", "sha256", "hash_mode", "width", "height", "mode",
                      "has_alpha"):
                setattr(res, f, getattr(validated, f))
        try:
            os.unlink(dst)
        except OSError:
            pass  # invariant-ok: partial output may not exist
        return res

    def _await(self, req: NormalizeRequest, token: CancellationToken | None) -> tuple[Any, ...]:
        """Returns ("result", res) | ("timeout"|"crash", validated, current_decoder)."""
        assert self._conn is not None and self._proc is not None
        validated = None
        current: str | None = None
        deadline = time.monotonic() + self.tpolicy.image_base_s * 2   # validation + hashing
        hard_stop = time.monotonic() + self.tpolicy.image_max_s * (len(self.decoders) + 2)
        while time.monotonic() < hard_stop:
            if token is not None and token.cancelled:
                self._kill("cancelled")
                raise JobCancelledError()
            now = time.monotonic()
            if now > deadline:
                self._kill(f"timeout on image #{req.index + 1} decoder={current}")
                return ("timeout", validated, current)
            try:
                ready = self._conn.poll(min(POLL_SLICE_S, max(0.0, deadline - now)))
            except (OSError, EOFError):
                ready = True
            if not ready:
                if not self._proc.is_alive():
                    self._kill("worker exited")
                    return ("crash", validated, current)
                continue
            try:
                msg = self._conn.recv()
            except (EOFError, OSError):
                self._kill(f"worker crashed on image #{req.index + 1} decoder={current}")
                return ("crash", validated, current)
            if msg[0] == "validated":
                validated = msg[1]
                pixels = max(1, validated.width * validated.height)
                deadline = time.monotonic() + timeouts.image_normalize(self.tpolicy, pixels).hard_s
            elif msg[0] == "trying":
                current = msg[1]
                pixels = max(1, (validated.width * validated.height) if validated else 0)
                deadline = time.monotonic() + timeouts.image_normalize(self.tpolicy, pixels).hard_s
            elif msg[0] == "result":
                return ("result", msg[1])
        self._kill("hard stop")
        return ("timeout", validated, current)

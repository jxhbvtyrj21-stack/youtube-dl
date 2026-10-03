"""Cancellation and pause primitives shared by queue, jobs and runners.

``CancellationToken`` — one per job. Cancelling it sets a flag *and* calls
the registered kill callbacks (e.g. "kill the current FFmpeg tree"), so a
blocked subprocess is actively terminated rather than politely waited for.

``PauseGate`` — one per batch. Pipelines call :meth:`PauseGate.checkpoint`
between atomic operations (one image, one segment). Pause therefore never
freezes FFmpeg mid-encode (ARCHITECTURE.md §5.2).
"""

from __future__ import annotations

import itertools
import logging
import threading
from collections.abc import Callable

from videogen.core.errors import JobCancelledError

log = logging.getLogger(__name__)


class CancellationToken:
    def __init__(self, parent: "CancellationToken | None" = None) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._ids = itertools.count(1)
        self._parent = parent
        self._parent_handle: int | None = None
        if parent is not None:
            self._parent_handle = parent.register(lambda: self.cancel(parent.reason))

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()
            callbacks = list(self._callbacks.values())
        for cb in callbacks:
            try:
                cb()
            except Exception:  # noqa: BLE001 - a broken callback must not stop the others
                log.exception("cancellation callback failed")

    def register(self, callback: Callable[[], None]) -> int:
        """Register a callback; it runs immediately if already cancelled."""
        with self._lock:
            handle = next(self._ids)
            self._callbacks[handle] = callback
            already = self._event.is_set()
        if already:
            try:
                callback()
            except Exception:  # noqa: BLE001
                log.exception("cancellation callback failed")
        return handle

    def unregister(self, handle: int) -> None:
        with self._lock:
            self._callbacks.pop(handle, None)

    def detach(self) -> None:
        """Drop the link to the parent token (call when the job ends)."""
        if self._parent is not None and self._parent_handle is not None:
            self._parent.unregister(self._parent_handle)
            self._parent_handle = None

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise JobCancelledError()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; returns True early if cancelled."""
        return self._event.wait(timeout)


class PauseGate:
    """Cooperative pause between atomic operations."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._pause_requested = False
        self._paused_waiters = 0
        self.on_paused: Callable[[], None] | None = None

    @property
    def pause_requested(self) -> bool:
        with self._cond:
            return self._pause_requested

    @property
    def someone_paused(self) -> bool:
        with self._cond:
            return self._paused_waiters > 0

    def request_pause(self) -> None:
        with self._cond:
            self._pause_requested = True
            self._cond.notify_all()

    def release(self) -> None:
        with self._cond:
            self._pause_requested = False
            self._cond.notify_all()

    def checkpoint(self, token: CancellationToken, poll_s: float = 0.25) -> None:
        """Raise if cancelled; block while a pause is requested.

        The wait ends only by resume (``release``) or cancellation — both are
        user/engine actions, and STOP always cancels every job token.
        """
        token.raise_if_cancelled()
        with self._cond:
            if not self._pause_requested:
                return
            self._paused_waiters += 1
            notify = self.on_paused
        if notify is not None:
            try:
                notify()
            except Exception:  # noqa: BLE001
                log.exception("on_paused callback failed")
        try:
            with self._cond:
                while self._pause_requested and not token.cancelled:
                    self._cond.wait(poll_s)
        finally:
            with self._cond:
                self._paused_waiters -= 1
        token.raise_if_cancelled()

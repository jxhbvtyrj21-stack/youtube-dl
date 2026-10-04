"""API keys for MODE B providers (ARCHITECTURE.md §21).

Keys live in the Windows Credential Manager of the current user (generic
credentials, ``CredWriteW`` / ``CredReadW`` through ctypes — no extra
dependency). They are never written to ``settings.json``, never sent through
the GUI/Engine queues and never logged: the Engine process reads them itself
when a MODE B batch starts.

On other operating systems there is no secure store; MODE B then reports
"not configured". Tests use :class:`MemoryCredentialStore`.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Protocol

SERVICES: dict[str, str] = {
    "elevenlabs": "ElevenLabs (озвучка)",
    "openai": "OpenAI (зображення)",
}
TARGET_PREFIX = "VideoGen/"
MAX_KEY_LEN = 512


class CredentialStoreError(Exception):
    """The secure store cannot be used (unsupported OS, access denied)."""


class CredentialStore(Protocol):
    available: bool

    def get(self, service: str) -> str | None:
        ...

    def set(self, service: str, secret: str) -> None:
        ...

    def delete(self, service: str) -> bool:
        ...


def validate_key(secret: str) -> str:
    """A pasted key is trimmed; empty, overlong or multi-line values are rejected."""
    s = (secret or "").strip()
    if not s:
        raise ValueError("Ключ порожній.")
    if len(s) > MAX_KEY_LEN:
        raise ValueError("Ключ задовгий.")
    if any(ord(c) < 32 or ord(c) == 127 or c.isspace() for c in s):
        raise ValueError("Ключ містить пробіли або керівні символи.")
    return s


def _check_service(service: str) -> None:
    if service not in SERVICES:
        raise KeyError(f"unknown service {service!r}")


class MemoryCredentialStore:
    """In-process store for tests."""

    available = True

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._data: dict[str, str] = dict(initial or {})

    def get(self, service: str) -> str | None:
        _check_service(service)
        return self._data.get(service)

    def set(self, service: str, secret: str) -> None:
        _check_service(service)
        self._data[service] = validate_key(secret)

    def delete(self, service: str) -> bool:
        _check_service(service)
        return self._data.pop(service, None) is not None


class UnavailableCredentialStore:
    available = False

    def get(self, service: str) -> str | None:
        _check_service(service)
        return None

    def set(self, service: str, secret: str) -> None:
        raise CredentialStoreError("Безпечне сховище ключів доступне лише в Windows.")

    def delete(self, service: str) -> bool:
        _check_service(service)
        return False


# ---------------------------------------------------------------- Windows

CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168


def _win_api() -> tuple[Any, Any]:
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

    class CREDENTIALW(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)  # type: ignore[attr-defined]
    adv.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIALW), wintypes.DWORD]
    adv.CredWriteW.restype = wintypes.BOOL
    adv.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIALW))]
    adv.CredReadW.restype = wintypes.BOOL
    adv.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    adv.CredDeleteW.restype = wintypes.BOOL
    adv.CredFree.argtypes = [ctypes.c_void_p]
    adv.CredFree.restype = None
    return adv, CREDENTIALW


class WindowsCredentialStore:
    """Generic credentials ``VideoGen/<service>`` of the current Windows user."""

    available = True

    def __init__(self, prefix: str = TARGET_PREFIX) -> None:
        self.prefix = prefix
        self._adv, self._cred = _win_api()

    def _target(self, service: str) -> str:
        _check_service(service)
        return self.prefix + service

    def get(self, service: str) -> str | None:
        import ctypes
        ptr = ctypes.POINTER(self._cred)()
        if not self._adv.CredReadW(self._target(service), CRED_TYPE_GENERIC, 0, ctypes.byref(ptr)):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            if err == ERROR_NOT_FOUND:
                return None
            raise CredentialStoreError(f"Не вдалося прочитати ключ із Windows Credential Manager (код {err}).")
        try:
            c = ptr.contents
            raw = ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize)
        finally:
            self._adv.CredFree(ptr)
        try:
            return raw.decode("utf-8") or None
        except UnicodeDecodeError:
            return None

    def set(self, service: str, secret: str) -> None:
        import ctypes
        blob = validate_key(secret).encode("utf-8")
        buf = (ctypes.c_ubyte * len(blob)).from_buffer_copy(blob)
        c = self._cred()
        c.Type = CRED_TYPE_GENERIC
        c.TargetName = self._target(service)
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        c.Persist = CRED_PERSIST_LOCAL_MACHINE
        c.UserName = "VideoGen"
        if not self._adv.CredWriteW(ctypes.byref(c), 0):
            err = ctypes.get_last_error()  # type: ignore[attr-defined]
            raise CredentialStoreError(f"Не вдалося зберегти ключ у Windows Credential Manager (код {err}).")

    def delete(self, service: str) -> bool:
        import ctypes
        if self._adv.CredDeleteW(self._target(service), CRED_TYPE_GENERIC, 0):
            return True
        err = ctypes.get_last_error()  # type: ignore[attr-defined]
        if err == ERROR_NOT_FOUND:
            return False
        raise CredentialStoreError(f"Не вдалося видалити ключ із Windows Credential Manager (код {err}).")


def default_store() -> CredentialStore:
    """The store the application uses. ``VIDEOGEN_CREDENTIAL_PREFIX`` (tests
    only) isolates test credentials from the user's real ones."""
    if sys.platform != "win32":
        return UnavailableCredentialStore()
    return WindowsCredentialStore(os.environ.get("VIDEOGEN_CREDENTIAL_PREFIX") or TARGET_PREFIX)


def configured(store: CredentialStore) -> dict[str, bool]:
    """Which services have a key — never the keys themselves."""
    out: dict[str, bool] = {}
    for s in SERVICES:
        try:
            out[s] = bool(store.get(s))
        except CredentialStoreError:
            out[s] = False
    return out

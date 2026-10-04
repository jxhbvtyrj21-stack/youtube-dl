"""The real Windows Credential Manager (runs on Windows CI only). Uses a
unique target prefix so the user's own VideoGen keys are never touched."""

from __future__ import annotations

import sys
import uuid

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager")


def test_write_read_overwrite_delete_in_credential_manager():
    from videogen.utils.credentials import WindowsCredentialStore, configured
    store = WindowsCredentialStore(prefix=f"VideoGen-test-{uuid.uuid4().hex}/")
    try:
        assert store.get("openai") is None
        store.set("openai", "sk-test-111")
        assert store.get("openai") == "sk-test-111"
        store.set("openai", "sk-test-222")                 # overwrite, not a duplicate
        assert store.get("openai") == "sk-test-222"
        assert configured(store) == {"elevenlabs": False, "openai": True}
        assert store.delete("openai") is True
        assert store.get("openai") is None
        assert store.delete("openai") is False
    finally:
        store.delete("openai")
        store.delete("elevenlabs")


def test_default_store_on_windows_honours_the_test_prefix(monkeypatch):
    from videogen.utils.credentials import WindowsCredentialStore, default_store
    prefix = f"VideoGen-test-{uuid.uuid4().hex}/"
    monkeypatch.setenv("VIDEOGEN_CREDENTIAL_PREFIX", prefix)
    s = default_store()
    assert isinstance(s, WindowsCredentialStore) and s.prefix == prefix

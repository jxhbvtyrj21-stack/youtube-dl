from __future__ import annotations

import sys

import pytest

from videogen.utils.credentials import (
    CredentialStoreError, MemoryCredentialStore, UnavailableCredentialStore, configured, default_store,
    validate_key,
)


def test_key_validation():
    assert validate_key("  sk-abc123 \n") == "sk-abc123"
    for bad in ("", "   ", "sk-a b", "sk-a\nb", "x" * 600):
        with pytest.raises(ValueError):
            validate_key(bad)


def test_memory_store_roundtrip_and_status():
    s = MemoryCredentialStore()
    assert configured(s) == {"elevenlabs": False, "openai": False}
    s.set("openai", " sk-1234 ")
    assert s.get("openai") == "sk-1234"
    assert configured(s) == {"elevenlabs": False, "openai": True}
    assert s.delete("openai") and not s.delete("openai")
    with pytest.raises(KeyError):
        s.get("other")


def test_unavailable_store_reports_not_configured():
    s = UnavailableCredentialStore()
    assert not s.available and s.get("openai") is None and not s.delete("openai")
    with pytest.raises(CredentialStoreError):
        s.set("openai", "sk-1234")


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows behaviour")
def test_default_store_outside_windows_is_unavailable():
    assert not default_store().available

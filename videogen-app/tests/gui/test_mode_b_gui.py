"""PHASE 11 GUI: START in MODE B without keys is refused with a reason; the
«Ключі API…» dialog stores keys without ever showing them."""

from __future__ import annotations

import time

import pytest
from PySide6.QtWidgets import QLabel

from videogen.config.settings import Settings
from videogen.core import events as ev
from videogen.gui.api_keys_dialog import ApiKeysDialog
from videogen.gui.main_window import MainWindow
from videogen.utils.credentials import MemoryCredentialStore, UnavailableCredentialStore
from tests.gui.test_main_window import FakeClient

T = time.time()
SECRET = "sk-secret-1234567890"


def _window(qtbot, tmp_path, store):
    client = FakeClient()
    w = MainWindow(tmp_path / "appdata", Settings(), client, autostart_engine=False, credential_store=store)
    qtbot.addWidget(w)
    w.timer.stop()
    client.inbox.append(ev.EngineReady(T, "0.6", "9.0"))
    w.tick()
    warnings: list[str] = []
    w._warn = warnings.append                      # type: ignore[method-assign]
    (tmp_path / "in").mkdir()
    w.in_pick.set_path(str(tmp_path / "in"))
    w.out_pick.set_path(str(tmp_path / "out"))
    w.mode_b.setChecked(True)
    return w, client, warnings


def _starts(client):
    return [c for c in client.sent if isinstance(c, ev.StartBatch)]


def test_start_in_mode_b_without_keys_is_refused_with_the_reason(qtbot, tmp_path):
    w, client, warnings = _window(qtbot, tmp_path, MemoryCredentialStore({"openai": SECRET}))
    w.on_start()
    assert _starts(client) == []
    assert warnings and "ElevenLabs" in warnings[-1] and "Ключі API" in warnings[-1]
    assert "ElevenLabs" in w.mode_b_status.text()
    w.mode_a.setChecked(True)                      # MODE A is not affected
    w.on_start()
    assert [c.mode for c in _starts(client)] == ["A"]


def test_start_in_mode_b_with_keys_sends_the_batch(qtbot, tmp_path):
    w, client, warnings = _window(qtbot, tmp_path, MemoryCredentialStore({"openai": SECRET, "elevenlabs": SECRET}))
    w.on_start()
    assert [c.mode for c in _starts(client)] == ["B"] and warnings == []
    assert w.mode_b_status.text() == "Режим B: ключі API задано."


def test_keys_dialog_saves_and_deletes_without_showing_the_key(qtbot, tmp_path):
    store = MemoryCredentialStore()
    w, client, _ = _window(qtbot, tmp_path, store)
    w.on_api_keys()
    dlg = w._keys_dialog
    assert isinstance(dlg, ApiKeysDialog) and not dlg.isModal()
    for svc in ("elevenlabs", "openai"):
        dlg.edits[svc].setText(f"  {SECRET}-{svc}  ")
        dlg.save(svc)
        assert store.get(svc) == f"{SECRET}-{svc}"
        assert dlg.edits[svc].text() == "" and dlg.status[svc].text() == "задано"
    texts = " ".join(lbl.text() for lbl in w.findChildren(QLabel)) + " ".join(
        lbl.text() for lbl in dlg.findChildren(QLabel))
    assert SECRET not in texts
    assert w.mode_b_status.text() == "Режим B: ключі API задано."     # main window updated at once
    w.on_start()
    assert [c.mode for c in _starts(client)] == ["B"]
    dlg.delete("openai")
    assert store.get("openai") is None and dlg.status["openai"].text() == "не задано"
    assert "OpenAI" in w.mode_b_status.text()


@pytest.mark.parametrize("bad", ["", "with space", "a\nb"])
def test_keys_dialog_rejects_bad_input(qtbot, tmp_path, bad):
    store = MemoryCredentialStore()
    dlg = ApiKeysDialog(store)
    qtbot.addWidget(dlg)
    dlg.edits["openai"].setText(bad)
    dlg.save("openai")
    assert store.get("openai") is None and dlg.edits["openai"].text() == ""
    assert "OpenAI" in dlg.message.text()


def test_keys_dialog_without_a_secure_store_is_read_only(qtbot):
    dlg = ApiKeysDialog(UnavailableCredentialStore())
    qtbot.addWidget(dlg)
    assert not dlg.edits["openai"].isEnabled() and not dlg.save_buttons["elevenlabs"].isEnabled()
    assert "Windows" in dlg.message.text()

from __future__ import annotations

import statistics
import time

import pytest

from videogen.config.settings import Settings
from videogen.core import events as ev
from videogen.core.models import BatchState, JobStatus, RecoveryAction, Stage
from videogen.gui.main_window import EVENTS_PER_TICK, MainWindow

T = time.time()


class FakeClient:
    def __init__(self):
        self.inbox: list[ev.Event] = []
        self.sent: list[ev.Command] = []
        self.alive = True
        self.restarted = 0

    def start(self):
        pass

    def is_alive(self):
        return self.alive

    def send(self, cmd):
        self.sent.append(cmd)
        if isinstance(cmd, ev.Shutdown):
            self.alive = False
        return True

    def poll(self, limit=200):
        out, self.inbox = self.inbox[:limit], self.inbox[limit:]
        return out

    def request_shutdown(self):
        self.send(ev.Shutdown())

    def finish_shutdown(self, deadline):
        self.alive = False
        return True

    def restart(self):
        self.restarted += 1
        self.alive = True

    def kill(self):
        self.alive = False


@pytest.fixture()
def win(qtbot, tmp_path):
    client = FakeClient()
    w = MainWindow(tmp_path / "appdata", Settings(), client, autostart_engine=False)
    qtbot.addWidget(w)
    w.timer.stop()               # ticks driven manually for determinism
    client.inbox.append(ev.EngineReady(T, "0.5", "6.1"))
    w.tick()
    return w, client


def test_all_required_controls_exist(win):
    w, _ = win
    for attr in ("mode_a", "mode_b", "fmt_h", "fmt_v", "in_pick", "out_pick", "ws_pick", "btn_start",
                 "btn_pause", "btn_resume", "btn_stop", "btn_cancel", "btn_open_out", "btn_open_log",
                 "stage_label", "overall", "job_bar", "c_ok", "c_failed", "c_skipped", "c_eta", "log_view"):
        assert hasattr(w, attr), attr


def test_start_validates_and_sends_command(win, tmp_path):
    w, client = win
    w.on_start()
    assert not any(isinstance(c, ev.StartBatch) for c in client.sent)       # no input chosen
    (tmp_path / "in").mkdir()
    w.in_pick.set_path(str(tmp_path / "in"))
    w.out_pick.set_path(str(tmp_path / "in"))
    w.on_start()
    assert not any(isinstance(c, ev.StartBatch) for c in client.sent)       # output == input refused
    w.out_pick.set_path(str(tmp_path / "out"))
    w.fmt_v.setChecked(True)
    w.on_start()
    cmd = [c for c in client.sent if isinstance(c, ev.StartBatch)][-1]
    assert cmd.orientation == "9:16" and cmd.mode == "A" and cmd.input_dir == str(tmp_path / "in")
    assert (tmp_path / "appdata" / "settings.json").exists()                # paths remembered


def test_buttons_follow_batch_state(win):
    w, client = win
    assert w.btn_start.isEnabled() and not w.btn_stop.isEnabled()
    client.inbox.append(ev.BatchStateChanged(T, BatchState.RUNNING))
    w.tick()
    assert not w.btn_start.isEnabled() and w.btn_stop.isEnabled() and w.btn_pause.isEnabled()
    assert not w.in_pick.edit.isEnabled()
    client.inbox.append(ev.BatchStateChanged(T, BatchState.PAUSED))
    w.tick()
    assert w.btn_resume.isEnabled() and not w.btn_pause.isEnabled()
    w.btn_stop.click()
    assert isinstance(client.sent[-1], ev.Stop)
    client.inbox.append(ev.BatchStateChanged(T, BatchState.IDLE))
    w.tick()
    assert w.btn_start.isEnabled()


def test_close_while_running_asks_without_blocking(win):
    from PySide6.QtWidgets import QMessageBox
    w, client = win
    client.inbox.append(ev.BatchStateChanged(T, BatchState.RUNNING))
    w.tick()
    w.close()                                   # returns immediately
    assert w._confirm is not None and w._confirm.isVisible() and not w._confirm.isModal()
    assert not any(isinstance(c, ev.Shutdown) for c in client.sent)
    w._confirm.button(QMessageBox.StandardButton.Yes).click()
    assert any(isinstance(c, ev.Shutdown) for c in client.sent)
    w.tick()
    assert w._may_close


def test_event_flood_keeps_ticks_short(win):
    w, client = win
    client.inbox.append(ev.JobQueued(T, "j", "Відео"))
    for i in range(20000):
        client.inbox.append(ev.JobProgress(T, "j", Stage.RENDERING, i / 200, frame=i, total_frames=20000))
        if i % 10 == 0:
            client.inbox.append(ev.LogLine(T, "INFO", f"рядок {i}"))
    durations = []
    for _ in range(200):
        t0 = time.perf_counter()
        w.tick()
        durations.append(time.perf_counter() - t0)
        if not client.inbox:
            break
    assert not client.inbox
    assert max(len(w.client.inbox), 0) == 0
    assert statistics.median(durations) < 0.05, durations[:10]
    assert max(durations) < 0.25
    assert w.log_view.document().blockCount() <= 2001
    assert "кадр" in w.stage_label.text()
    assert len(durations) >= 22000 // EVENTS_PER_TICK - 1     # never more than N events per tick


def test_engine_death_is_shown_and_restart_offered(win):
    w, client = win
    client.alive = False
    w.tick()
    assert not w.engine_banner.isHidden()
    assert not w.btn_start.isEnabled()
    w.btn_restart_engine.click()
    assert client.restarted == 1
    client.inbox.append(ev.EngineReady(T, "0.5", "6.1"))
    w.tick()
    assert w.engine_banner.isHidden() and w.btn_start.isEnabled()


def test_unresponsive_engine_warning(win, monkeypatch):
    w, client = win
    monkeypatch.setattr(w.model, "heartbeat_age", lambda: 60.0)
    w.tick()
    assert not w.engine_banner.isHidden() and "не відповідає" in w.engine_banner_label.text()


def test_recovery_dialog_sends_decisions(win, qtbot):
    w, client = win
    client.inbox.append(ev.InterruptedJobsFound(T, ("j1", "j2"), ("Відео 1", "Відео 2")))
    w.tick()
    dlg = w._recovery
    assert dlg is not None and dlg.isVisible()
    dlg._groups["j2"].buttons()[2].setChecked(True)          # Ignore for j2
    dlg.accept()
    sent = [c for c in client.sent if isinstance(c, ev.RecoveryDecision)]
    assert {(c.job_id, c.action) for c in sent} == {("j1", RecoveryAction.RESUME), ("j2", RecoveryAction.IGNORE)}


def test_error_popup_is_non_modal(win, qtbot):
    w, client = win
    client.inbox.append(ev.EngineError(T, "Недостатньо вільного місця на диску."))
    w.tick()
    from PySide6.QtWidgets import QMessageBox
    boxes = [x for x in w.findChildren(QMessageBox) if x.isVisible()]
    assert boxes and not boxes[0].isModal()
    assert "Недостатньо вільного місця" in boxes[0].text()


def test_close_shuts_engine_down(win, qtbot):
    w, client = win
    w.close()
    assert any(isinstance(c, ev.Shutdown) for c in client.sent)
    w.tick()
    assert w._may_close and not w.isVisible()


def test_counters_and_finish(win):
    w, client = win
    client.inbox += [ev.JobQueued(T, "a", "A"), ev.JobFinished(T, "a", JobStatus.PARTIAL, "/o/A [PARTIAL].mp4"),
                     ev.BatchCounters(T, 1, 0, 1, 0, 0, 2, None)]
    w.tick()
    assert w.c_partial.value.text() == "1" and w.c_skipped.value.text() == "2"
    assert "PARTIAL" in w.log_view.toPlainText()

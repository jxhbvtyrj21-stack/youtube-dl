"""Main window (ARCHITECTURE.md §3, ТЗ §3, §41).

The Qt main thread only: reads at most ``EVENTS_PER_TICK`` events per timer
tick (non-blocking), updates widgets, sends commands. It never waits for the
Engine, FFmpeg, the filesystem or a subprocess.
"""

from __future__ import annotations

import dataclasses
import logging
import time
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

from videogen import APP_NAME, __version__
from videogen.config.settings import PathSettings, Settings, save_settings
from videogen.core import events as ev
from videogen.core.models import BatchState
from videogen.gui.api_keys_dialog import ApiKeysDialog
from videogen.gui.engine_client import EngineClient
from videogen.gui.progress import BATCH_NAMES, ViewModel
from videogen.gui.recovery_dialog import RecoveryDialog
from videogen.gui.widgets import Counter, FolderPicker
from videogen.providers.registry import build_providers
from videogen.utils.credentials import CredentialStore, default_store

log = logging.getLogger(__name__)

TICK_MS = 100
EVENTS_PER_TICK = 200
HEARTBEAT_WARN_S = 15.0
SHUTDOWN_GRACE_S = 25.0


class MainWindow(QMainWindow):
    def __init__(self, appdata: Path, settings: Settings, client: EngineClient | None = None,
                 *, autostart_engine: bool = True, credential_store: CredentialStore | None = None) -> None:
        super().__init__()
        self.appdata = Path(appdata)
        self.settings = settings
        self.credentials = credential_store or default_store()
        self._keys_dialog: ApiKeysDialog | None = None
        self.client = client or EngineClient(self.appdata, settings)
        self.model = ViewModel()
        self._shown_log_seq = 0
        self._closing = False
        self._close_deadline = 0.0
        self._may_close = False
        self._recovery: RecoveryDialog | None = None
        self._confirm: QMessageBox | None = None
        self.tick_durations: list[float] = []
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(980, 760)
        self._build()
        self._restore_paths()
        self.timer = QTimer(self)
        self.timer.setInterval(TICK_MS)
        self.timer.timeout.connect(self.tick)
        if autostart_engine:
            self.client.start()
        self.timer.start()
        self._render()

    # ================================================================ layout

    def _build(self) -> None:
        root = QWidget()
        lay = QVBoxLayout(root)

        opts = QHBoxLayout()
        mode_box = QGroupBox("Режим")
        mb = QVBoxLayout(mode_box)
        self.mode_a = QRadioButton("A: Аудіо + зображення → відео")
        self.mode_b = QRadioButton("B: Сценарій + промпти → озвучка + зображення → відео")
        self.mode_a.setChecked(True)
        self._mode_group = QButtonGroup(self)
        for w in (self.mode_a, self.mode_b):
            self._mode_group.addButton(w)
            mb.addWidget(w)
        keys_row = QHBoxLayout()
        self.mode_b_status = QLabel()
        self.mode_b_status.setWordWrap(True)
        self.btn_keys = QPushButton("Ключі API…")
        self.btn_keys.clicked.connect(self.on_api_keys)
        keys_row.addWidget(self.mode_b_status, 1)
        keys_row.addWidget(self.btn_keys)
        mb.addLayout(keys_row)
        fmt_box = QGroupBox("Формат")
        fb = QVBoxLayout(fmt_box)
        self.fmt_h = QRadioButton("Горизонтальний 16:9")
        self.fmt_v = QRadioButton("Вертикальний 9:16")
        self.fmt_h.setChecked(True)
        self._fmt_group = QButtonGroup(self)
        for w in (self.fmt_h, self.fmt_v):
            self._fmt_group.addButton(w)
            fb.addWidget(w)
        opts.addWidget(mode_box, 2)
        opts.addWidget(fmt_box, 1)
        lay.addLayout(opts)

        self.in_pick = FolderPicker("Вхідна папка:", "Виберіть папку з матеріалами")
        self.out_pick = FolderPicker("Папка результатів:", "Виберіть папку для готових відео")
        self.ws_pick = FolderPicker("Тимчасова робоча папка:", "Виберіть тимчасову робочу папку")
        self.ws_pick.edit.setPlaceholderText(f"За замовчуванням: {self.appdata / 'workspace'}")
        for p in (self.in_pick, self.out_pick, self.ws_pick):
            lay.addWidget(p)

        btns = QHBoxLayout()
        self.btn_start = QPushButton("START")
        self.btn_pause = QPushButton("PAUSE")
        self.btn_resume = QPushButton("RESUME")
        self.btn_stop = QPushButton("STOP")
        self.btn_cancel = QPushButton("CANCEL CURRENT JOB")
        self.btn_open_out = QPushButton("OPEN OUTPUT")
        self.btn_open_log = QPushButton("OPEN LOG")
        for b in (self.btn_start, self.btn_pause, self.btn_resume, self.btn_stop, self.btn_cancel,
                  self.btn_open_out, self.btn_open_log):
            btns.addWidget(b)
        lay.addLayout(btns)
        self.btn_start.clicked.connect(self.on_start)
        self.btn_pause.clicked.connect(lambda: self.client.send(ev.Pause()))
        self.btn_resume.clicked.connect(lambda: self.client.send(ev.Resume()))
        self.btn_stop.clicked.connect(self.on_stop)
        self.btn_cancel.clicked.connect(lambda: self.client.send(ev.CancelCurrentJob()))
        self.btn_open_out.clicked.connect(self.on_open_output)
        self.btn_open_log.clicked.connect(self.on_open_log)
        self._update_mode_b_status()

        self.engine_banner = QWidget()
        bl = QHBoxLayout(self.engine_banner)
        self.engine_banner_label = QLabel()
        self.engine_banner_label.setStyleSheet("color: #b00020; font-weight: bold")
        self.btn_restart_engine = QPushButton("Перезапустити обробник")
        self.btn_restart_engine.clicked.connect(self.on_restart_engine)
        bl.addWidget(self.engine_banner_label, 1)
        bl.addWidget(self.btn_restart_engine)
        self.engine_banner.hide()
        lay.addWidget(self.engine_banner)

        prog = QGroupBox("Прогрес")
        g = QGridLayout(prog)
        self.stage_label = QLabel("Очікування")
        self.stage_label.setWordWrap(True)
        self.overall = QProgressBar()
        self.job_bar = QProgressBar()
        for bar in (self.overall, self.job_bar):
            bar.setRange(0, 1000)
            bar.setTextVisible(True)
        g.addWidget(QLabel("Поточний етап:"), 0, 0)
        g.addWidget(self.stage_label, 0, 1, 1, 3)
        g.addWidget(QLabel("Загальний прогрес:"), 1, 0)
        g.addWidget(self.overall, 1, 1, 1, 3)
        g.addWidget(QLabel("Поточне завдання:"), 2, 0)
        g.addWidget(self.job_bar, 2, 1, 1, 3)
        self.c_ok = Counter("Успішно:")
        self.c_partial = Counter("Неповні (PARTIAL):")
        self.c_failed = Counter("З помилкою:")
        self.c_skipped = Counter("Пропущені проблемні файли:")
        self.c_eta = Counter("Залишилось часу:")
        for i, c in enumerate((self.c_ok, self.c_partial, self.c_failed, self.c_skipped)):
            g.addWidget(c, 3 + i // 2, (i % 2) * 2, 1, 2)
        g.addWidget(self.c_eta, 5, 0, 1, 2)
        lay.addWidget(prog)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)       # bounded memory
        lay.addWidget(QLabel("Журнал:"))
        lay.addWidget(self.log_view, 1)
        self.setCentralWidget(root)
        self.statusBar().showMessage("Запуск обробника…")

    # ================================================================ engine events

    def tick(self) -> None:
        t0 = time.perf_counter()
        try:
            for e in self.client.poll(EVENTS_PER_TICK):
                self.model.apply(e)
            self._check_engine()
            if self.model.dirty:
                self._render()
                self.model.dirty = False
            self._drain_popups()
            if self._closing:
                self._continue_close()
        except Exception:  # noqa: BLE001 - the UI loop must survive any bug
            log.exception("GUI tick failed")
        self.tick_durations.append(time.perf_counter() - t0)
        if len(self.tick_durations) > 5000:
            del self.tick_durations[:2500]

    def _check_engine(self) -> None:
        alive = self.client.is_alive()
        if not alive and not self._closing:
            if self.model.engine_alive:
                self.model.engine_alive = False
                self.model.dirty = True
                self.model.errors.append("Обробник відео аварійно завершився. Незавершені завдання буде "
                                         "запропоновано відновити після перезапуску.")
            self.engine_banner_label.setText("Обробник відео не працює.")
            self.engine_banner.show()
        elif alive and self.model.engine_ready and self.model.heartbeat_age() > HEARTBEAT_WARN_S:
            self.engine_banner_label.setText("Обробник відео не відповідає…")
            self.engine_banner.show()
        elif alive:
            self.engine_banner.hide()

    def _drain_popups(self) -> None:
        if self.model.errors:
            text = "\n\n".join(self.model.errors[-3:])
            self.model.errors.clear()
            box = QMessageBox(QMessageBox.Icon.Warning, APP_NAME, text, parent=self)
            box.setModal(False)
            box.show()                       # non-blocking
        if self.model.interrupted and self._recovery is None:
            jobs, self.model.interrupted = self.model.interrupted, []
            self._recovery = RecoveryDialog(jobs, self)
            self._recovery.accepted.connect(self._on_recovery_done)
            self._recovery.open()            # window-modal, non-blocking

    def _on_recovery_done(self) -> None:
        if self._recovery is None:
            return
        for job_id, action in self._recovery.decisions().items():
            self.client.send(ev.RecoveryDecision(job_id, action))
        self._recovery = None

    # ================================================================ rendering

    def _render(self) -> None:
        m = self.model
        b = m.buttons()
        self.btn_start.setEnabled(b.start)
        self.btn_pause.setEnabled(b.pause)
        self.btn_resume.setEnabled(b.resume)
        self.btn_stop.setEnabled(b.stop)
        self.btn_cancel.setEnabled(b.cancel_current)
        for w in (self.mode_a, self.mode_b, self.fmt_h, self.fmt_v, self.in_pick, self.out_pick, self.ws_pick):
            w.setEnabled(b.inputs)
        self.stage_label.setText(m.stage_text)
        self.overall.setValue(int(m.overall_percent * 10))
        self.overall.setFormat(f"{m.overall_percent:.1f} %")
        cj = m.current_job
        jp = cj.percent if cj else (100.0 if m.batch_state is BatchState.IDLE and m.jobs else 0.0)
        self.job_bar.setValue(int(jp * 10))
        self.job_bar.setFormat(f"{jp:.1f} %")
        self.c_ok.set(m.succeeded)
        self.c_partial.set(m.partial)
        self.c_failed.set(m.failed)
        self.c_skipped.set(m.skipped_files)
        self.c_eta.set(m.eta_text())
        new = m.log_seq - self._shown_log_seq
        if new > 0:
            lines = list(m.log)[-min(new, len(m.log)):]
            self.log_view.appendPlainText("\n".join(lines))
            self._shown_log_seq = m.log_seq
        state = "готовий" if m.engine_ready else "запуск…"
        if not m.engine_alive:
            state = "не працює"
        self.statusBar().showMessage(f"Обробник: {state} | {BATCH_NAMES.get(m.batch_state, '')}"
                                     + (f" | FFmpeg {m.ffmpeg_version}" if m.ffmpeg_version else ""))

    # ================================================================ actions

    def on_start(self) -> None:
        inp, out = self.in_pick.path(), self.out_pick.path()
        if not inp or not Path(inp).is_dir():
            self._warn("Виберіть вхідну папку з матеріалами.")
            return
        if not out:
            self._warn("Виберіть папку для готових відео.")
            return
        if Path(out).resolve() == Path(inp).resolve():
            self._warn("Папка результатів не може збігатися з вхідною папкою.")
            return
        if self.mode_b.isChecked():
            why = self._mode_b_problem()
            if why:
                self._warn(why)
                return
        self._save_paths()
        self.model.reset_batch()
        self.client.send(ev.StartBatch(
            mode="A" if self.mode_a.isChecked() else "B",
            orientation="16:9" if self.fmt_h.isChecked() else "9:16",
            input_dir=inp, output_dir=out, workspace_dir=self.ws_pick.path()))

    def _mode_b_problem(self) -> str:
        """Why MODE B cannot start now ('' when it can). Only whether the keys
        exist is checked here; the Engine reads the keys itself."""
        return build_providers(self.settings.providers, self.credentials).explanation()

    def _update_mode_b_status(self) -> None:
        why = self._mode_b_problem()
        self.mode_b_status.setText("Режим B: ключі API задано." if not why else why)
        self.mode_b_status.setStyleSheet("" if not why else "color: #8a5a00")

    def on_api_keys(self) -> None:
        if self._keys_dialog is None:
            self._keys_dialog = ApiKeysDialog(self.credentials, self, on_change=self._update_mode_b_status)
            self._keys_dialog.setModal(False)
        self._keys_dialog.refresh()
        self._keys_dialog.show()
        self._keys_dialog.raise_()

    def on_stop(self) -> None:
        self.client.send(ev.Stop())

    def on_open_output(self) -> None:
        out = self.out_pick.path()
        if out and Path(out).is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(out))
        else:
            self._warn("Папку результатів ще не вибрано або вона не існує.")

    def on_open_log(self) -> None:
        p = self.appdata / "logs" / "application.log"
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p if p.exists() else p.parent)))

    def on_restart_engine(self) -> None:
        self.engine_banner.hide()
        self.model = ViewModel()
        self._shown_log_seq = 0
        self.client.restart()

    def _warn(self, text: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Information, APP_NAME, text, parent=self)
        box.setModal(False)
        box.show()

    # ================================================================ settings

    def _restore_paths(self) -> None:
        p = self.settings.paths
        self.in_pick.set_path(p.input_dir)
        self.out_pick.set_path(p.output_dir)
        self.ws_pick.set_path(p.workspace_dir)

    def _save_paths(self) -> None:
        self.settings = dataclasses.replace(self.settings, paths=PathSettings(
            self.in_pick.path(), self.out_pick.path(), self.ws_pick.path()))
        try:
            save_settings(self.settings, self.appdata / "settings.json")
        except OSError:
            log.warning("could not save settings")

    # ================================================================ closing

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._may_close:
            event.accept()
            return
        event.ignore()
        if self._closing:
            return
        busy = self.model.batch_state not in (BatchState.IDLE, BatchState.COMPLETED, BatchState.STOPPED)
        if busy:
            if self._confirm is None:
                # non-blocking confirmation: no nested modal event loop
                box = QMessageBox(QMessageBox.Icon.Question, APP_NAME,
                                  "Обробка ще виконується. Зупинити її та закрити програму?",
                                  QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, self)
                box.setModal(False)
                box.finished.connect(self._on_close_confirmed)
                self._confirm = box
                box.show()
            return
        self._begin_close()

    def _on_close_confirmed(self, result: int) -> None:
        box, self._confirm = self._confirm, None
        if box is not None and box.standardButton(box.clickedButton()) == QMessageBox.StandardButton.Yes:
            self._begin_close()

    def _begin_close(self) -> None:
        self._closing = True
        self._close_deadline = time.monotonic() + SHUTDOWN_GRACE_S
        self.statusBar().showMessage("Завершення роботи…")
        self.client.request_shutdown()

    def _continue_close(self) -> None:
        if self.client.is_alive() and time.monotonic() < self._close_deadline:
            return
        self.client.finish_shutdown(0.0)    # kills the tree if still alive
        self.timer.stop()
        self._may_close = True
        self.close()

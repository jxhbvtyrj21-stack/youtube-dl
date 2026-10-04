"""«Ключі API…» — keys for MODE B (ARCHITECTURE.md §21).

A key is typed (or pasted) once and saved straight to the Windows Credential
Manager. A saved key is never shown again, never logged and never put into
``settings.json``; the dialog shows only whether a key is set.
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QGridLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from videogen.utils.credentials import SERVICES, CredentialStore, CredentialStoreError, configured


class ApiKeysDialog(QDialog):
    def __init__(self, store: CredentialStore, parent: QWidget | None = None,
                 on_change: Callable[[], None] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Ключі API (режим B)")
        self._store = store
        self._on_change = on_change
        self.status: dict[str, QLabel] = {}
        self.edits: dict[str, QLineEdit] = {}
        self.save_buttons: dict[str, QPushButton] = {}
        self.delete_buttons: dict[str, QPushButton] = {}
        lay = QVBoxLayout(self)
        intro = QLabel(
            "Ключі зберігаються в Windows Credential Manager вашого облікового запису Windows.\n"
            "Збережений ключ більше не показується; щоб змінити його, вставте новий і натисніть «Зберегти».")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        grid = QGridLayout()
        for row, (svc, title) in enumerate(SERVICES.items()):
            grid.addWidget(QLabel(title), row, 0)
            st = QLabel()
            grid.addWidget(st, row, 1)
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText("Вставте ключ")
            grid.addWidget(edit, row, 2)
            save = QPushButton("Зберегти")
            delete = QPushButton("Видалити")
            save.clicked.connect(lambda _=False, s=svc: self.save(s))
            delete.clicked.connect(lambda _=False, s=svc: self.delete(s))
            grid.addWidget(save, row, 3)
            grid.addWidget(delete, row, 4)
            self.status[svc], self.edits[svc] = st, edit
            self.save_buttons[svc], self.delete_buttons[svc] = save, delete
        lay.addLayout(grid)
        self.message = QLabel()
        self.message.setWordWrap(True)
        lay.addWidget(self.message)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        if not store.available:
            self.message.setText("Безпечне сховище ключів доступне лише в Windows. Режим B тут недоступний.")
            for w in (*self.edits.values(), *self.save_buttons.values(), *self.delete_buttons.values()):
                w.setEnabled(False)
        self.refresh()

    def refresh(self) -> None:
        state = configured(self._store)
        for svc, label in self.status.items():
            label.setText("задано" if state[svc] else "не задано")
            label.setStyleSheet("color: #1b5e20" if state[svc] else "color: #b00020")

    def save(self, service: str) -> None:
        edit = self.edits[service]
        try:
            self._store.set(service, edit.text())
        except ValueError as exc:
            self.message.setText(f"{SERVICES[service]}: {exc}")
            return
        except CredentialStoreError as exc:
            self.message.setText(str(exc))
            return
        finally:
            edit.clear()                       # the key does not stay in the widget
        self.message.setText(f"{SERVICES[service]}: ключ збережено.")
        self._changed()

    def delete(self, service: str) -> None:
        try:
            removed = self._store.delete(service)
        except CredentialStoreError as exc:
            self.message.setText(str(exc))
            return
        self.message.setText(f"{SERVICES[service]}: ключ видалено." if removed else
                             f"{SERVICES[service]}: ключ не було задано.")
        self._changed()

    def _changed(self) -> None:
        self.refresh()
        if self._on_change is not None:
            self._on_change()

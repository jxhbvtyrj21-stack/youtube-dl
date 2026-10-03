"""Small reusable widgets."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget


class FolderPicker(QWidget):
    changed = Signal(str)

    def __init__(self, label: str, dialog_title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._title = dialog_title
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel(label)
        self.label.setMinimumWidth(170)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Не вибрано")
        self.button = QPushButton("Вибрати…")
        lay.addWidget(self.label)
        lay.addWidget(self.edit, 1)
        lay.addWidget(self.button)
        self.button.clicked.connect(self._browse)
        self.edit.textChanged.connect(self.changed)

    def _browse(self) -> None:
        # the native dialog is modal but runs a nested event loop: timers keep firing
        path = QFileDialog.getExistingDirectory(self, self._title, self.edit.text())
        if path:
            self.edit.setText(path)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, p: str) -> None:
        self.edit.setText(p)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        self.edit.setEnabled(enabled)
        self.button.setEnabled(enabled)


class Counter(QWidget):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title)
        self.value = QLabel("0")
        self.value.setStyleSheet("font-weight: bold")
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addStretch(1)

    def set(self, v: int | str) -> None:
        text = str(v)
        if self.value.text() != text:
            self.value.setText(text)

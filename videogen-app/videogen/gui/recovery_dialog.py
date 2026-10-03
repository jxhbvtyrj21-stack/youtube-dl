"""Dialog offered after an unclean shutdown (ARCHITECTURE.md §14)."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup, QDialog, QDialogButtonBox, QGridLayout, QLabel, QRadioButton, QVBoxLayout, QWidget,
)

from videogen.core.models import RecoveryAction


class RecoveryDialog(QDialog):
    def __init__(self, jobs: list[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Відновлення після аварійного завершення")
        self._groups: dict[str, QButtonGroup] = {}
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(
            "Попередній запуск програми було перервано. Готові відео не змінено.\n"
            "Оберіть, що зробити з незавершеними завданнями:\n"
            "  • Продовжити — з місця зупинки (готові фрагменти використовуються повторно);\n"
            "  • Почати заново — з нуля;\n"
            "  • Ігнорувати — скасувати завдання."))
        grid = QGridLayout()
        for row, (job_id, name) in enumerate(jobs):
            grid.addWidget(QLabel(name), row, 0)
            g = QButtonGroup(self)
            for col, (text, action) in enumerate((("Продовжити", RecoveryAction.RESUME),
                                                  ("Почати заново", RecoveryAction.RETRY),
                                                  ("Ігнорувати", RecoveryAction.IGNORE)), start=1):
                rb = QRadioButton(text)
                rb.setProperty("action", action.value)
                g.addButton(rb)
                grid.addWidget(rb, row, col)
                if action is RecoveryAction.RESUME:
                    rb.setChecked(True)
            self._groups[job_id] = g
        lay.addLayout(grid)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        bb.accepted.connect(self.accept)
        lay.addWidget(bb)

    def decisions(self) -> dict[str, RecoveryAction]:
        out = {}
        for job_id, g in self._groups.items():
            b = g.checkedButton()
            out[job_id] = RecoveryAction(b.property("action")) if b else RecoveryAction.RESUME
        return out

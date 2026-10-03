"""Application entry point.

``freeze_support()`` and the ``__main__`` guard are mandatory: on Windows
child processes are started with "spawn" and re-import the main module
(ARCHITECTURE.md §15a).
"""

from __future__ import annotations

import multiprocessing
import os
import sys


def main() -> int:
    from PySide6.QtWidgets import QApplication

    from videogen import APP_NAME
    from videogen.config.settings import load_settings
    from videogen.gui.main_window import MainWindow
    from videogen.utils.system import app_data_dir

    appdata = app_data_dir()
    appdata.mkdir(parents=True, exist_ok=True)
    settings, _warnings = load_settings(appdata / "settings.json")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    win = MainWindow(appdata, settings)
    win.show()
    smoke_ms = os.environ.get("VIDEOGEN_SMOKE_EXIT_MS")
    if smoke_ms:
        # packaging smoke test: start GUI + engine, then close cleanly
        from PySide6.QtCore import QTimer
        QTimer.singleShot(int(smoke_ms), win.close)
    return app.exec()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    multiprocessing.set_start_method("spawn", force=True)
    sys.exit(main())

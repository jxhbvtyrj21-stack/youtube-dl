"""A real GUI process (QApplication + MainWindow + EngineClient) driven by a
test. Prints one JSON status line every 200 ms to stdout.

    python gui_harness.py <appdata> <input> <output> <workspace> start|recover|idle
"""

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main() -> None:
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from videogen.config.settings import settings_from_dict
    from videogen.gui.main_window import MainWindow

    appdata, inp, out, ws, mode = sys.argv[1:6]
    settings, _ = settings_from_dict(json.loads(os.environ.get("VIDEOGEN_TEST_SETTINGS", "{}")))
    app = QApplication([])
    w = MainWindow(Path(appdata), settings)
    w.in_pick.set_path(inp)
    w.out_pick.set_path(out)
    w.ws_pick.set_path(ws)
    state = {"started": False, "last": time.monotonic(), "max_gap": 0.0, "recovered": False}

    def beat() -> None:
        now = time.monotonic()
        state["max_gap"] = max(state["max_gap"], now - state["last"] - 0.05)
        state["last"] = now

    def report() -> None:
        m = w.model
        if mode == "start" and m.engine_ready and not state["started"]:
            w.btn_start.click()
            state["started"] = True
        if mode == "recover" and w._recovery is not None and not state["recovered"]:
            w._recovery.accept()
            state["recovered"] = True
        cj = m.current_job
        print(json.dumps({
            "t": time.time(), "engine_pid": w.client.pid, "engine_alive": w.client.is_alive(),
            "engine_ready": m.engine_ready, "model_engine_alive": m.engine_alive,
            "banner": not w.engine_banner.isHidden(), "batch": m.batch_state.value,
            "stage": cj.stage.value if cj else "", "frame": cj.frame if cj else 0,
            "succeeded": m.succeeded, "partial": m.partial, "failed": m.failed,
            "recovered": state["recovered"], "max_gap_ms": round(state["max_gap"] * 1000, 1),
        }), flush=True)
        state["max_gap"] = 0.0

    t1 = QTimer()
    t1.timeout.connect(beat)
    t1.start(50)
    t2 = QTimer()
    t2.timeout.connect(report)
    t2.start(200)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()

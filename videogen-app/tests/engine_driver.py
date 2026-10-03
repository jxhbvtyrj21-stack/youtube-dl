"""Run an Engine batch in this process (used by the crash-recovery test,
which kills this process with SIGKILL mid-render)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from videogen.config.settings import settings_from_dict  # noqa: E402
from videogen.core.engine import Engine  # noqa: E402
from tests.pipeline_support import start_cmd  # noqa: E402

def main() -> None:
    appdata, inp, out, ws, settings_file = sys.argv[1:6]
    settings, _ = settings_from_dict(json.loads(Path(settings_file).read_text()))
    eng = Engine(Path(appdata), settings, lambda e: None)
    eng.startup()
    eng.start_batch(start_cmd(Path(inp), Path(out), Path(ws)))
    eng.wait_idle(600)
    eng.shutdown()


# REQUIRED with multiprocessing "spawn" (always used on Windows): child
# processes re-import the main module; without this guard each child would
# start another engine.
if __name__ == "__main__":
    main()

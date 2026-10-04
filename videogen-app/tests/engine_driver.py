"""Run an Engine batch in this process (used by the crash-recovery test,
which kills this process with SIGKILL mid-render)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from videogen.config.settings import settings_from_dict  # noqa: E402
from videogen.core.engine import Engine  # noqa: E402
from tests.pipeline_support import start_cmd  # noqa: E402


def _hang_after_publish(marker: Path) -> None:
    """Test-only: stop right after the final video is published (before the
    archive and before SUCCESS is written), so the test can kill us there."""
    import time
    from videogen.core import pipeline as pl
    real = pl.MediaPipeline._publish

    def publish(self, run, tmp, degraded):
        final = real(self, run, tmp, degraded)
        marker.write_text(str(final), encoding="utf-8")
        for _ in range(1200):            # bounded: the test kills this process long before
            time.sleep(0.1)
        return final
    pl.MediaPipeline._publish = publish


def main() -> None:
    appdata, inp, out, ws, settings_file = sys.argv[1:6]
    import os
    if os.environ.get("VIDEOGEN_DRIVER_HANG_AFTER_PUBLISH"):
        _hang_after_publish(Path(os.environ["VIDEOGEN_DRIVER_HANG_AFTER_PUBLISH"]))
    settings, _ = settings_from_dict(json.loads(Path(settings_file).read_text(encoding="utf-8")))
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

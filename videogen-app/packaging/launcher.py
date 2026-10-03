"""PyInstaller entry script (kept outside the package on purpose)."""

import multiprocessing
import sys

if __name__ == "__main__":
    # Runs in every process (including spawned children) before
    # freeze_support() hands control to the child's target.
    from videogen.utils.system import app_data_dir, redirect_missing_std_streams
    redirect_missing_std_streams(app_data_dir() / "logs")
    multiprocessing.freeze_support()          # mandatory for spawn-based child processes in a frozen EXE
    multiprocessing.set_start_method("spawn", force=True)
    from videogen.main import entry
    sys.exit(entry())

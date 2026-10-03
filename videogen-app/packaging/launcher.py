"""PyInstaller entry script (kept outside the package on purpose)."""

import multiprocessing
import sys

if __name__ == "__main__":
    multiprocessing.freeze_support()          # mandatory for spawn-based child processes in a frozen EXE
    multiprocessing.set_start_method("spawn", force=True)
    from videogen.main import entry
    sys.exit(entry())

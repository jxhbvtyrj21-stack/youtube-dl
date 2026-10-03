# PyInstaller spec — onedir build (onefile would unpack into %TEMP% on every
# start: slow and repeatedly scanned by antivirus software).
# Build:  pyinstaller packaging/videogen.spec --noconfirm   (from videogen-app/)
import os
from pathlib import Path

ROOT = Path(SPECPATH).parent
FFMPEG_DIR = Path(os.environ.get("VIDEOGEN_BUNDLE_FFMPEG", ROOT / "packaging" / "third_party" / "ffmpeg"))

binaries = []
for exe in ("ffmpeg.exe", "ffprobe.exe"):
    p = FFMPEG_DIR / exe
    if p.exists():
        binaries.append((str(p), "ffmpeg"))
datas = []
for f in ("LICENSE.txt", "README.txt"):
    p = FFMPEG_DIR / f
    if p.exists():
        datas.append((str(p), "ffmpeg"))
for doc in ("README.md", "TROUBLESHOOTING.md"):
    datas.append((str(ROOT / doc), "docs"))

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["videogen.engine_main", "videogen.workers.image_worker", "videogen.core.pipeline",
                   "PIL.WebPImagePlugin", "PIL.TiffImagePlugin", "PIL.BmpImagePlugin"],
    excludes=["tkinter", "matplotlib", "pytest", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore",
              "PySide6.QtQml", "PySide6.QtQuick"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VideoGen",
    console=False,
    manifest=str(ROOT / "packaging" / "videogen.exe.manifest"),
    upx=False,                       # UPX-packed binaries trigger antivirus false positives
)
cli = EXE(                           # console twin for --selftest / diagnostics
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="VideoGen-cli",
    console=True,
    manifest=str(ROOT / "packaging" / "videogen.exe.manifest"),
    upx=False,
)
coll = COLLECT(exe, cli, a.binaries, a.datas, name="VideoGen", upx=False)

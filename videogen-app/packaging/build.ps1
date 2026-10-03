# Full Windows build: venv -> deps -> tests -> FFmpeg -> PyInstaller -> self-test -> zip.
# Run from videogen-app\:   powershell -ExecutionPolicy Bypass -File packaging\build.ps1
param([switch]$SkipTests)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

py -3.11 -m venv .venv-build
.\.venv-build\Scripts\python -m pip install --upgrade pip
.\.venv-build\Scripts\python -m pip install -e ".[media,gui,dev]" "pyinstaller>=6.6"

& "$PSScriptRoot\fetch_ffmpeg.ps1"
$env:VIDEOGEN_FFMPEG_DIR = "$PSScriptRoot\third_party\ffmpeg"

if (-not $SkipTests) { .\.venv-build\Scripts\python -m pytest -q }

.\.venv-build\Scripts\pyinstaller packaging\videogen.spec --noconfirm --clean
Remove-Item Env:\VIDEOGEN_FFMPEG_DIR
.\dist\VideoGen\VideoGen-cli.exe --selftest "$env:TEMP\videogen-selftest"
if ($LASTEXITCODE -ne 0) { throw "self-test of the packaged build failed" }

$version = (.\dist\VideoGen\VideoGen-cli.exe --version)
Compress-Archive -Path dist\VideoGen -DestinationPath "dist\VideoGen-$version-win64.zip" -Force
Write-Host "Built dist\VideoGen-$version-win64.zip"

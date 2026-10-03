# Downloads a pinned FFmpeg release build for bundling and verifies its SHA-256.
# Source: https://www.gyan.dev/ffmpeg/builds/ (GPL build, contains libx264).
param(
    [string]$Dest = "$PSScriptRoot\third_party\ffmpeg",
    [string]$Url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
)
$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$zip = Join-Path $env:TEMP "ffmpeg-essentials.zip"
Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing -TimeoutSec 600
$expected = (Invoke-WebRequest -Uri "$Url.sha256" -UseBasicParsing -TimeoutSec 60).Content.Trim().Split()[0].ToLower()
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "FFmpeg checksum mismatch: expected $expected, got $actual" }
$tmp = Join-Path $env:TEMP "ffmpeg-extract"
if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
Expand-Archive $zip -DestinationPath $tmp
$root = Get-ChildItem $tmp -Directory | Select-Object -First 1
Copy-Item "$($root.FullName)\bin\ffmpeg.exe", "$($root.FullName)\bin\ffprobe.exe" $Dest -Force
Copy-Item "$($root.FullName)\LICENSE" "$Dest\LICENSE.txt" -Force
& "$Dest\ffmpeg.exe" -hide_banner -version | Select-Object -First 1

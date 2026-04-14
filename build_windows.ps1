# Build script for Ouroboros on Windows
# Run from repo root: powershell -ExecutionPolicy Bypass -File build_windows.ps1

$ErrorActionPreference = "Stop"

$Version = (Get-Content VERSION).Trim()
$ArchiveName = "Ouroboros-${Version}-windows-x64.zip"

Write-Host "=== Building Ouroboros for Windows (v${Version}) ==="

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: uv not found. Install: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`""
    exit 1
}

if (-not (Test-Path "python-standalone\python.exe")) {
    Write-Host "ERROR: python-standalone\ not found."
    Write-Host "Run first: powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1"
    exit 1
}

Write-Host "--- Installing launcher dependencies ---"
uv pip install --system --python python -q ".[desktop,build]"

Write-Host "--- Installing agent dependencies into python-standalone ---"
uv pip install --system --python python-standalone\python.exe -q -r pyproject.toml

if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

$env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location) ".pyinstaller-cache"
New-Item -ItemType Directory -Force -Path $env:PYINSTALLER_CONFIG_DIR | Out-Null

Write-Host "--- Running PyInstaller ---"
python -m PyInstaller Ouroboros.spec --clean --noconfirm

Write-Host ""
Write-Host "=== Creating archive ==="
Compress-Archive -Path "dist\Ouroboros" -DestinationPath "dist\$ArchiveName" -Force

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Archive: dist\$ArchiveName"
Write-Host ""
Write-Host "To run: extract and execute Ouroboros\Ouroboros.exe"

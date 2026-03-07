# Build script for Ouroboros on Windows
# Run from repo root: powershell -ExecutionPolicy Bypass -File build_windows.ps1

$ErrorActionPreference = "Stop"

$Version = (Get-Content VERSION).Trim()
$ArchiveName = "Ouroboros-${Version}-windows-x64.zip"

Write-Host "=== Building Ouroboros for Windows (v${Version}) ==="

if (-not (Test-Path "python-standalone\python.exe")) {
    Write-Host "ERROR: python-standalone\ not found."
    Write-Host "Run first:  powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1"
    exit 1
}

Write-Host "--- Installing launcher dependencies ---"
pip install -q -r requirements-launcher.txt

Write-Host "--- Installing agent dependencies into python-standalone ---"
& "python-standalone\python.exe" -m pip install -q -r requirements.txt

if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

Write-Host "--- Running PyInstaller ---"
python -m PyInstaller Ouroboros.spec --clean --noconfirm

# -- Package ---------------------------------------------------------------

Write-Host ""
Write-Host "=== Creating archive ==="
Compress-Archive -Path "dist\Ouroboros" -DestinationPath "dist\$ArchiveName" -Force

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Archive: dist\$ArchiveName"
Write-Host ""
Write-Host "To run: extract and execute Ouroboros\Ouroboros.exe"

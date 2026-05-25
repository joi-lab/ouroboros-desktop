# Build script for Ouroboros on Windows
# Run from repo root: powershell -ExecutionPolicy Bypass -File build_windows.ps1

$ErrorActionPreference = "Stop"

$Version = (Get-Content VERSION).Trim()
$ArchiveName = "Ouroboros-${Version}-windows-x64.zip"
$ManagedSourceBranch = if ($env:OUROBOROS_MANAGED_SOURCE_BRANCH) { $env:OUROBOROS_MANAGED_SOURCE_BRANCH } else { "ouroboros" }
$env:PYTHONDONTWRITEBYTECODE = "1"
if (-not $env:PYTHONPYCACHEPREFIX) {
    $env:PYTHONPYCACHEPREFIX = Join-Path ([System.IO.Path]::GetTempPath()) "OuroborosBuildPycache"
}
New-Item -ItemType Directory -Force -Path $env:PYTHONPYCACHEPREFIX | Out-Null

Write-Host "=== Building Ouroboros for Windows (v${Version}) ==="

if (-not (Test-Path "python-standalone\python.exe")) {
    Write-Host "ERROR: python-standalone\ not found."
    Write-Host "Run first: powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1"
    exit 1
}

Write-Host "--- Installing launcher dependencies ---"
python -m pip install -q -r requirements-launcher.txt

Write-Host "--- Installing agent dependencies into python-standalone ---"
& "python-standalone\python.exe" -m pip install -q -r requirements.txt

if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist") { Remove-Item -Recurse -Force "dist" }

$env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location) ".pyinstaller-cache"
New-Item -ItemType Directory -Force -Path $env:PYINSTALLER_CONFIG_DIR | Out-Null

Write-Host "--- Installing Chromium for browser tools (bundled into python-standalone) ---"
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
& "python-standalone\python.exe" -m playwright install --only-shell chromium

Write-Host "--- Pruning optional Chromium resources with long Windows paths ---"
$LocalBrowsers = "python-standalone\Lib\site-packages\playwright\driver\package\.local-browsers"
if (Test-Path $LocalBrowsers) {
    Get-ChildItem -Path $LocalBrowsers -Directory -Filter "chromium_headless_shell-*" | ForEach-Object {
        $ShellRoot = $_.FullName
        $OptionalPaths = @(
            "chrome-headless-shell-win64\PrivacySandboxAttestationsPreloaded",
            "chrome-headless-shell-win64\resources\accessibility\reading_mode_gdocs_helper",
            "chrome-headless-shell-win64\resources\accessibility\reading_mode_gdocs_helper_manifest.json"
        )
        foreach ($Rel in $OptionalPaths) {
            $Target = Join-Path $ShellRoot $Rel
            if (Test-Path $Target) {
                Remove-Item -Recurse -Force $Target
            }
        }
    }
}

Write-Host "--- Building embedded managed repo bundle ---"
python scripts/build_repo_bundle.py --source-branch $ManagedSourceBranch

Write-Host "--- Running PyInstaller ---"
python -m PyInstaller Ouroboros.spec --clean --noconfirm

Write-Host "--- Installing packaged CLI wrappers ---"
New-Item -ItemType Directory -Force -Path "dist\Ouroboros\bin" | Out-Null
Copy-Item "packaging\cli\ouroboros.cmd" "dist\Ouroboros\bin\ouroboros.cmd" -Force
Copy-Item "packaging\cli\install-ouroboros-cli.cmd" "dist\Ouroboros\bin\install-ouroboros-cli.cmd" -Force

Write-Host "--- Removing Python bytecode caches from archive payload ---"
Get-ChildItem -Path "dist\Ouroboros" -Recurse -Force -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
Get-ChildItem -Path "dist\Ouroboros" -Recurse -Force -File -Filter "*.pyc" | Remove-Item -Force

Write-Host "--- Checking Windows archive path lengths ---"
$TooLong = Get-ChildItem -Path "dist\Ouroboros" -Recurse -Force | Where-Object {
    $_.FullName.Substring((Resolve-Path "dist\Ouroboros").Path.Length).TrimStart('\').Length -gt 200
}
if ($TooLong) {
    $Sample = ($TooLong | Select-Object -First 10 | ForEach-Object { $_.FullName }) -join "`n"
    throw "Windows build contains paths longer than 200 chars under dist\Ouroboros:`n$Sample"
}

Write-Host ""
Write-Host "=== Creating archive ==="
Compress-Archive -Path "dist\Ouroboros" -DestinationPath "dist\$ArchiveName" -Force

Write-Host ""
Write-Host "=== Done ==="
Write-Host "Archive: dist\$ArchiveName"
Write-Host ""
Write-Host "To run: extract and execute Ouroboros\Ouroboros.exe"
Write-Host "To install CLI: Ouroboros\bin\install-ouroboros-cli.cmd"

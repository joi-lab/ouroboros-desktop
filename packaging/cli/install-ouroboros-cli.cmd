@echo off
rem Ouroboros packaged CLI installer
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\repo.bundle" goto root_found
if exist "%ROOT%\_internal\repo.bundle" (
  set "ROOT=%ROOT%\_internal"
  goto root_found
)
  echo ouroboros CLI install: could not locate packaged bundle root 1>&2
  exit /b 2
:root_found
if exist "%ROOT%\python-standalone\python.exe" (
  set "PY=%ROOT%\python-standalone\python.exe"
) else (
  set "PY=%ROOT%\python-standalone\python3.exe"
)
set "PYTHONPATH=%ROOT%"
set "OUROBOROS_PACKAGED_BUNDLE_ROOT=%ROOT%"
set "OUROBOROS_PACKAGED_CLI_WRAPPER=%ROOT%\bin\ouroboros.cmd"
"%PY%" -m ouroboros.packaged_cli_install %*
exit /b %ERRORLEVEL%

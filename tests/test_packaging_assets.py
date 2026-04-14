"""Regression checks for packaging asset completeness.

These tests read files that exist only in the app bundle (launcher.py,
Ouroboros.spec) and are skipped when running from a bare repo checkout.
"""

import os
import pathlib

import pytest

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_BUNDLE_FILES_PRESENT = (REPO / "Ouroboros.spec").exists() and (REPO / "launcher.py").exists()
_SKIP_REASON = "Bundle-only files (Ouroboros.spec, launcher.py) not present in repo"

def _launcher_has_bootstrap() -> bool:
    launcher = REPO / "launcher.py"
    bootstrap = REPO / "ouroboros" / "launcher_bootstrap.py"
    if not launcher.exists() or not bootstrap.exists():
        return False
    launcher_src = launcher.read_text(encoding="utf-8")
    bootstrap_src = bootstrap.read_text(encoding="utf-8")
    return (
        "from ouroboros.launcher_bootstrap import" in launcher_src
        and "MANAGED_BUNDLE_PATHS = (" in bootstrap_src
        and '"server.py"' in bootstrap_src
        and '"web"' in bootstrap_src
        and '"webview"' in bootstrap_src
        and '"assets"' in bootstrap_src
    )

_LAUNCHER_HAS_BOOTSTRAP = _launcher_has_bootstrap()


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_spec_bundles_assets_and_icon():
    source = _read("Ouroboros.spec")
    assert "('assets', 'assets')" in source
    assert "icon='assets/icon.icns'" in source


@pytest.mark.skipif(
    not _LAUNCHER_HAS_BOOTSTRAP,
    reason="launcher.py does not import launcher_bootstrap (may be a newer version without bootstrap bridge)",
)
def test_launcher_does_not_exclude_assets_on_bootstrap():
    launcher_source = _read("launcher.py")
    bootstrap_source = _read("ouroboros/launcher_bootstrap.py")
    assert '"python-standalone", "assets"' not in launcher_source
    assert "from ouroboros.launcher_bootstrap import" in launcher_source
    assert "MANAGED_BUNDLE_PATHS = (" in bootstrap_source
    assert '"server.py"' in bootstrap_source
    assert '"web"' in bootstrap_source
    assert '"webview"' in bootstrap_source
    assert '"assets"' in bootstrap_source


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_spec_retains_cross_platform_packaging_hooks():
    source = _read("Ouroboros.spec")
    assert "assets/icon.ico" in source
    assert "collect_all as _collect_all" in source
    assert "scripts/pyi_rth_pythonnet.py" in source
    assert "pythonnet" in source
    assert "clr_loader" in source


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_launcher_retains_cross_platform_runtime_hooks():
    launcher_source = _read("launcher.py")
    assert "embedded_python_candidates" in launcher_source
    assert "_prepare_windows_webview_runtime" in launcher_source
    assert "git_install_hint()" in launcher_source
    assert "create_kill_on_close_job" in launcher_source
    assert "kill_process_on_port(port)" in launcher_source
    assert "force_kill_pid(child.pid)" in launcher_source


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_launcher_preserves_macos_git_setup_path():
    launcher_source = _read("launcher.py")
    assert 'subprocess.Popen(["xcode-select", "--install"])' in launcher_source
    assert "Install Git (Xcode CLI Tools)" in launcher_source
    assert "Installing... A system dialog may appear." in launcher_source
    assert '["lsof", "-ti", f"tcp:{port}"]' in launcher_source


def test_cross_platform_build_scripts_are_present():
    assert (REPO / "build_linux.sh").exists()
    assert (REPO / "build_windows.ps1").exists()
    assert (REPO / "scripts" / "download_python_standalone.ps1").exists()
    assert (REPO / "scripts" / "pyi_rth_pythonnet.py").exists()


def test_build_sh_supports_unsigned_macos_release():
    build_source = _read("build.sh")
    assert 'OUROBOROS_SIGN' in build_source
    assert 'Skipping signing' in build_source
    assert 'Unsigned DMG:' in build_source


def test_requirements_files_removed():
    """requirements.txt and requirements-launcher.txt must not exist — pyproject.toml is the single source."""
    assert not (REPO / "requirements.txt").exists(), "requirements.txt should be removed (use pyproject.toml)"
    assert not (REPO / "requirements-launcher.txt").exists(), "requirements-launcher.txt should be removed (use pyproject.toml)"


def test_pyproject_has_desktop_optional_deps():
    """Desktop launcher dependencies must be in pyproject.toml [project.optional-dependencies]."""
    toml = _read("pyproject.toml")
    assert "desktop" in toml
    assert "pywebview" in toml
    assert "pythonnet" in toml


def test_build_scripts_use_uv_and_pyproject():
    """Build scripts must use uv, check for uv presence, pass --system, and reference pyproject.toml."""
    build_sh = _read("build.sh")
    assert "uv pip install" in build_sh
    assert "command -v uv" in build_sh
    assert "pip install" not in build_sh.replace("uv pip install", "")
    assert "--system" in build_sh, "build.sh must pass --system to uv pip install"
    assert "-r pyproject.toml" in build_sh, "build.sh must use -r pyproject.toml for agent deps"
    assert "-r requirements.txt" not in build_sh, "build.sh must not reference requirements.txt"
    assert ".[desktop,build]" in build_sh, "build.sh must install desktop+build extras"

    build_linux = _read("build_linux.sh")
    assert "uv pip install" in build_linux
    assert "command -v uv" in build_linux
    assert "pip install" not in build_linux.replace("uv pip install", "")
    assert "--system" in build_linux, "build_linux.sh must pass --system to uv pip install"
    assert "-r pyproject.toml" in build_linux, "build_linux.sh must use -r pyproject.toml for agent deps"
    assert "-r requirements.txt" not in build_linux, "build_linux.sh must not reference requirements.txt"
    assert ".[desktop,build]" in build_linux, "build_linux.sh must install desktop+build extras"

    build_win = _read("build_windows.ps1")
    assert "uv pip install" in build_win
    assert "Get-Command uv" in build_win
    assert "pip install" not in build_win.replace("uv pip install", "")
    assert "--system" in build_win, "build_windows.ps1 must pass --system to uv pip install"
    assert "-r pyproject.toml" in build_win, "build_windows.ps1 must use -r pyproject.toml for agent deps"
    assert "-r requirements.txt" not in build_win, "build_windows.ps1 must not reference requirements.txt"
    assert ".[desktop,build]" in build_win, "build_windows.ps1 must install desktop+build extras"


def test_download_scripts_dont_install_deps():
    """Download scripts should only download Python, not install deps (build scripts handle that)."""
    dl_sh = _read("scripts/download_python_standalone.sh")
    assert "pip install" not in dl_sh.replace("uv pip install", ""), \
        "download_python_standalone.sh should not install deps"
    assert "requirements.txt" not in dl_sh

    dl_ps1 = _read("scripts/download_python_standalone.ps1")
    assert "pip install" not in dl_ps1.replace("uv pip install", ""), \
        "download_python_standalone.ps1 should not install deps"
    assert "requirements.txt" not in dl_ps1


def test_ci_uses_uv_and_pyproject():
    """CI workflow must use uv via astral-sh/setup-uv and consume uv.lock for deterministic builds."""
    ci = _read(".github/workflows/ci.yml")
    assert "astral-sh/setup-uv" in ci
    # CI test jobs must use uv sync --frozen (lockfile-deterministic)
    assert "uv sync --frozen" in ci, "CI test jobs must use uv sync --frozen for lockfile determinism"
    assert "uv run pytest" in ci, "CI must use uv run pytest"
    assert "-r requirements.txt" not in ci, "CI must not reference requirements.txt"
    assert "requirements.txt" not in ci, "CI must not reference requirements.txt anywhere"


def test_git_ops_prefers_pyproject(tmp_path, monkeypatch):
    """git_ops.sync_runtime_dependencies prefers pyproject.toml over requirements.txt."""
    import subprocess as _sp
    import sys as _sys
    import supervisor.git_ops as git_ops
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.delattr(_sys, "frozen", raising=False)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\nversion="0.1"\ndependencies=[]')
    (tmp_path / "requirements.txt").write_text("openai\n")
    calls = []
    def mock_run(cmd, **kw):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(_sp, "run", mock_run)
    git_ops.sync_runtime_dependencies("test")
    assert len(calls) == 1
    assert str(tmp_path) in calls[0], "Should install from project dir (pip install .)"
    assert "-r" not in calls[0], "Should NOT use -r flag with pyproject"


def test_git_ops_falls_back_to_requirements(tmp_path, monkeypatch):
    """git_ops.sync_runtime_dependencies falls back to requirements.txt when pyproject absent."""
    import subprocess as _sp
    import sys as _sys
    import supervisor.git_ops as git_ops
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.delattr(_sys, "frozen", raising=False)
    (tmp_path / "requirements.txt").write_text("openai\n")
    calls = []
    def mock_run(cmd, **kw):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(_sp, "run", mock_run)
    git_ops.sync_runtime_dependencies("test")
    assert len(calls) == 1
    assert "-r" in calls[0], "Should use -r flag for requirements.txt"


def test_launcher_bootstrap_prefers_pyproject(tmp_path):
    """launcher_bootstrap.install_deps prefers pyproject.toml → 'pip install <dir>'."""
    from ouroboros.launcher_bootstrap import install_deps, BootstrapContext
    import logging
    calls = []
    def mock_run(cmd, **kw):
        calls.append(cmd)
    ctx = BootstrapContext(
        bundle_dir=tmp_path,
        repo_dir=tmp_path,
        data_dir=tmp_path / "data",
        settings_path=tmp_path / "settings.json",
        embedded_python="python3",
        app_version="0.0.0",
        hidden_run=mock_run,
        save_settings=lambda d: None,
        log=logging.getLogger("test"),
    )
    (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\nversion="0.1"\ndependencies=[]')
    install_deps(ctx)
    assert len(calls) == 1
    assert str(tmp_path) in calls[0], "Should install from project dir"
    assert "-r" not in calls[0]


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_launcher_install_deps_prefers_pyproject(tmp_path, monkeypatch):
    """launcher.py _install_deps prefers pyproject.toml → 'pip install <dir>'."""
    import importlib
    import subprocess as _sp
    import launcher
    importlib.reload(launcher)
    monkeypatch.setattr(launcher, "REPO_DIR", tmp_path)
    monkeypatch.setattr(launcher, "EMBEDDED_PYTHON", "python3")
    calls = []
    def mock_run(cmd, **kw):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(_sp, "run", mock_run)
    (tmp_path / "pyproject.toml").write_text('[project]\nname="t"\nversion="0.1"\ndependencies=[]')
    (tmp_path / "requirements.txt").write_text("openai\n")
    launcher._install_deps()
    assert len(calls) == 1
    assert str(tmp_path) in calls[0], "Should install from project dir"
    assert "-r" not in calls[0], "Should NOT use -r flag with pyproject"


@pytest.mark.skipif(not _BUNDLE_FILES_PRESENT, reason=_SKIP_REASON)
def test_launcher_install_deps_falls_back_to_requirements(tmp_path, monkeypatch):
    """launcher.py _install_deps falls back to requirements.txt when pyproject absent."""
    import importlib
    import subprocess as _sp
    import launcher
    importlib.reload(launcher)
    monkeypatch.setattr(launcher, "REPO_DIR", tmp_path)
    monkeypatch.setattr(launcher, "EMBEDDED_PYTHON", "python3")
    calls = []
    def mock_run(cmd, **kw):
        calls.append(cmd)
        return _sp.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")
    monkeypatch.setattr(_sp, "run", mock_run)
    (tmp_path / "requirements.txt").write_text("openai\n")
    launcher._install_deps()
    assert len(calls) == 1
    assert "-r" in calls[0], "Should use -r flag for requirements.txt"


def test_dockerfile_uses_uv_and_lockfile():
    """Dockerfile must use uv sync --frozen with lockfile for deterministic builds."""
    dockerfile = _read("Dockerfile")
    assert "ghcr.io/astral-sh/uv" in dockerfile
    assert "uv sync --frozen" in dockerfile, "Dockerfile must use uv sync --frozen"
    assert "uv.lock" in dockerfile, "Dockerfile must COPY uv.lock for lockfile determinism"
    assert "pyproject.toml" in dockerfile, "Dockerfile must COPY pyproject.toml"
    assert "requirements.txt" not in dockerfile, "Dockerfile must not reference requirements.txt"
    assert "pip install uv" not in dockerfile.lower()
    # Entrypoint must use uv run to activate the managed venv
    assert '"uv"' in dockerfile and '"run"' in dockerfile, "Dockerfile ENTRYPOINT must use uv run for venv activation"

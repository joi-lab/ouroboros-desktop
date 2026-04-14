"""Bundle-to-repo bootstrap and managed sync helpers for the launcher."""

from __future__ import annotations

import os
import pathlib
import shutil
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Callable


_CORE_SYNC_PATHS = (
    "ouroboros/safety.py",
    "prompts/SAFETY.md",
    "ouroboros/tools/registry.py",
)

REPO_GITIGNORE = """\
# Secrets
.env
.env.*
*.key
*.pem

# IDE
.cursor/
.vscode/
.idea/

# Python bytecode
__pycache__/
*.pyc
*.pyo
*.egg-info/

# Build artifacts
dist/
build/
.pytest_cache/
.mypy_cache/

# Native / binary artifacts (PyInstaller, compiled extensions)
*.so
*.dylib
*.dll
*.dist-info/
base_library.zip

# OS
.DS_Store
Thumbs.db

# Release artifacts
.create_release.py
.release_notes.md
python-standalone/
"""

MANAGED_BUNDLE_PATHS = (
    "VERSION",
    ".gitignore",
    "BIBLE.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "Makefile",
    "server.py",
    "ouroboros",
    "supervisor",
    "prompts",
    "web",
    "webview",
    "docs",
    "tests",
    "assets",
)

SYNC_IGNORE_PATTERNS = ("__pycache__", "*.pyc", "*.pyo")


@dataclass(frozen=True)
class BootstrapContext:
    bundle_dir: pathlib.Path
    repo_dir: pathlib.Path
    data_dir: pathlib.Path
    settings_path: pathlib.Path
    embedded_python: str
    app_version: str
    hidden_run: Callable[..., Any]
    save_settings: Callable[[dict], None]
    log: Any


def check_git(is_windows: bool) -> bool:
    if shutil.which("git") is not None:
        return True
    if is_windows:
        for candidate in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
        ):
            if os.path.isfile(candidate):
                git_dir = os.path.dirname(candidate)
                os.environ["PATH"] = git_dir + ";" + os.environ.get("PATH", "")
                return True
    return False


def _ensure_repo_gitignore(repo_dir: pathlib.Path) -> None:
    """Write .gitignore if missing before any broad git staging."""
    gitignore = repo_dir / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(REPO_GITIGNORE, encoding="utf-8")


def _is_sync_ignored(name: str) -> bool:
    return any(fnmatch(name, pattern) for pattern in SYNC_IGNORE_PATTERNS)


def _sync_bundle_tree(src_path: pathlib.Path, dst_path: pathlib.Path, *, overwrite_existing: bool) -> None:
    if not src_path.exists():
        return
    if src_path.is_file():
        if overwrite_existing or not dst_path.exists():
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
        return
    if overwrite_existing:
        shutil.copytree(
            src_path,
            dst_path,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(*SYNC_IGNORE_PATTERNS),
        )
        return

    for root, dirs, files in os.walk(src_path):
        root_path = pathlib.Path(root)
        rel_root = root_path.relative_to(src_path)
        dirs[:] = [name for name in dirs if not _is_sync_ignored(name)]
        target_root = dst_path / rel_root if str(rel_root) != "." else dst_path
        target_root.mkdir(parents=True, exist_ok=True)
        for name in files:
            if _is_sync_ignored(name):
                continue
            dst_file = target_root / name
            if dst_file.exists():
                continue
            shutil.copy2(root_path / name, dst_file)


def _stage_paths(context: BootstrapContext, rel_paths: tuple[str, ...]) -> None:
    _ensure_repo_gitignore(context.repo_dir)
    context.hidden_run(
        ["git", "add", "--", *rel_paths],
        cwd=str(context.repo_dir),
        check=False,
        capture_output=True,
    )


def _status_for_paths(context: BootstrapContext, rel_paths: tuple[str, ...]) -> str:
    status = context.hidden_run(
        ["git", "status", "--porcelain", "--", *rel_paths],
        cwd=str(context.repo_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    return (status.stdout or "").strip()


def sync_core_files(context: BootstrapContext) -> None:
    for rel in _CORE_SYNC_PATHS:
        src = context.bundle_dir / rel
        dst = context.repo_dir / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    context.log.info("Synced %d core files to %s", len(_CORE_SYNC_PATHS), context.repo_dir)


def commit_synced_files(context: BootstrapContext) -> None:
    """Commit protected files so hard resets do not silently drop them."""
    try:
        _stage_paths(context, _CORE_SYNC_PATHS)
        if not _status_for_paths(context, _CORE_SYNC_PATHS):
            return
        context.hidden_run(
            ["git", "commit", "-m", "safety-sync: restore protected files from bundle"],
            cwd=str(context.repo_dir),
            check=False,
            capture_output=True,
        )
        context.log.info("Committed synced safety files.")
    except Exception as exc:
        context.log.warning("Failed to commit synced files: %s", exc)


def sync_bundle_managed_paths(context: BootstrapContext, *, overwrite_existing: bool) -> None:
    for rel in MANAGED_BUNDLE_PATHS:
        _sync_bundle_tree(context.bundle_dir / rel, context.repo_dir / rel, overwrite_existing=overwrite_existing)
    context.log.info("Synced managed bundle paths to %s (overwrite=%s)", context.repo_dir, overwrite_existing)


def read_version_file(root_dir: pathlib.Path) -> str:
    try:
        return (root_dir / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def repo_has_pending_changes(context: BootstrapContext) -> bool:
    try:
        status = context.hidden_run(
            ["git", "status", "--porcelain"],
            cwd=str(context.repo_dir),
            check=False,
            capture_output=True,
            text=True,
        )
        return bool((status.stdout or "").strip())
    except Exception:
        return True


def create_bundle_backup_branch(context: BootstrapContext, repo_version: str) -> str:
    branch_name = f"bundle-backup/{repo_version or 'unknown'}-{int(time.time())}"
    result = context.hidden_run(
        ["git", "branch", branch_name],
        cwd=str(context.repo_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        context.log.info("Created pre-sync backup branch %s", branch_name)
        return branch_name
    context.log.warning(
        "Failed to create pre-sync backup branch %s: %s",
        branch_name,
        (result.stderr or "").strip(),
    )
    return ""


def commit_bundle_sync(context: BootstrapContext, old_version: str, new_version: str) -> None:
    try:
        _stage_paths(context, MANAGED_BUNDLE_PATHS)
        if not _status_for_paths(context, MANAGED_BUNDLE_PATHS):
            return
        commit = context.hidden_run(
            [
                "git",
                "commit",
                "-m",
                f"bundle-sync: upgrade repo from {old_version or 'unknown'} to {new_version or context.app_version}",
            ],
            cwd=str(context.repo_dir),
            check=False,
            capture_output=True,
            text=True,
        )
        if commit.returncode == 0:
            context.log.info(
                "Committed managed bundle sync from %s to %s",
                old_version or "unknown",
                new_version or context.app_version,
            )
        else:
            context.log.warning("Managed bundle sync commit failed: %s", (commit.stderr or "").strip())
    except Exception as exc:
        context.log.warning("Failed to commit managed bundle sync: %s", exc)


def sync_existing_repo_from_bundle(context: BootstrapContext) -> None:
    """Sync safety-critical files from the bundle into an existing repo.

    Only the 3 protected core files (safety.py, SAFETY.md, registry.py) are
    overwritten — everything else in the repo is left untouched so that the
    agent's self-modifications are never clobbered by an older bundle.
    """
    sync_core_files(context)
    commit_synced_files(context)


def _migrate_old_settings(context: BootstrapContext) -> None:
    """Migrate old env-only installs into settings.json on first modern boot."""
    if context.settings_path.exists():
        return

    migrated = {}
    env_keys = [
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY", "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "OUROBOROS_NETWORK_PASSWORD", "OUROBOROS_FILE_BROWSER_DEFAULT",
        "OUROBOROS_MODEL", "OUROBOROS_MODEL_CODE", "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK", "TOTAL_BUDGET", "OUROBOROS_MAX_WORKERS",
        "OUROBOROS_SOFT_TIMEOUT_SEC", "OUROBOROS_HARD_TIMEOUT_SEC",
        "GITHUB_TOKEN", "GITHUB_REPO",
    ]
    for key in env_keys:
        val = os.environ.get(key, "")
        if val:
            migrated[key] = val
    if not migrated:
        return
    try:
        context.save_settings(migrated)
        context.log.info("Migrated %d env settings into %s", len(migrated), context.settings_path)
    except Exception as exc:
        context.log.warning("Failed to migrate old settings: %s", exc)


def install_deps(context: BootstrapContext) -> None:
    """Install/update Python deps inside the embedded interpreter."""
    try:
        pyproject = context.repo_dir / "pyproject.toml"
        requirements = context.repo_dir / "requirements.txt"  # legacy fallback
        if pyproject.exists():
            context.hidden_run(
                [context.embedded_python, "-m", "pip", "install", str(context.repo_dir)],
                timeout=240,
                capture_output=True,
            )
        elif requirements.exists():
            context.hidden_run(
                [context.embedded_python, "-m", "pip", "install", "-r", str(requirements)],
                timeout=240,
                capture_output=True,
            )
    except Exception as exc:
        context.log.warning("Dependency install/update failed: %s", exc)


_CLAUDE_SDK_BASELINE = "claude-agent-sdk>=0.1.50"


def verify_claude_runtime(context: BootstrapContext) -> bool:
    """Ensure the Claude runtime baseline is present in the app-managed interpreter.

    Checks that ``claude-agent-sdk`` is importable and its bundled CLI binary
    exists. If not, attempts a repair install. Returns True on success.
    """
    import sys as _sys
    cli_name = "claude.exe" if _sys.platform == "win32" else "claude"
    try:
        result = context.hidden_run(
            [context.embedded_python, "-c",
             "import claude_agent_sdk; "
             "from pathlib import Path; "
             f"cli = Path(claude_agent_sdk.__file__).parent / '_bundled' / '{cli_name}'; "
             "print('ok' if cli.exists() else 'no_cli')"],
            capture_output=True, text=True, timeout=30,
        )
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout == "ok":
            context.log.info("Claude runtime verified: SDK importable, bundled CLI present.")
            return True
        context.log.warning("Claude runtime check: %s (exit %d)", stdout, result.returncode)
    except Exception as exc:
        context.log.warning("Claude runtime probe failed: %s", exc)

    context.log.info("Repairing Claude runtime baseline...")
    try:
        repair = context.hidden_run(
            [context.embedded_python, "-m", "pip", "install", "--upgrade", _CLAUDE_SDK_BASELINE],
            timeout=120,
            capture_output=True,
        )
        if repair.returncode != 0:
            context.log.warning("Claude runtime repair pip returned exit %d", repair.returncode)
            return False
        context.log.info("Claude runtime repair install complete.")
        return True
    except Exception as exc:
        context.log.warning("Claude runtime repair failed: %s", exc)
        return False


def bootstrap_repo(context: BootstrapContext) -> None:
    """Copy the bundled source tree to REPO_DIR on first run and upgrade safely later."""
    context.data_dir.mkdir(parents=True, exist_ok=True)
    if context.repo_dir.exists() and (context.repo_dir / "server.py").exists():
        sync_existing_repo_from_bundle(context)
        verify_claude_runtime(context)
        return

    needs_full_bootstrap = not context.repo_dir.exists()
    context.log.info("Bootstrapping repository to %s (full=%s)", context.repo_dir, needs_full_bootstrap)
    context.repo_dir.mkdir(parents=True, exist_ok=True)
    sync_bundle_managed_paths(context, overwrite_existing=needs_full_bootstrap)

    if needs_full_bootstrap:
        _ensure_repo_gitignore(context.repo_dir)
        try:
            context.hidden_run(["git", "init"], cwd=str(context.repo_dir), check=True, capture_output=True)
            context.hidden_run(["git", "config", "user.name", "Ouroboros"], cwd=str(context.repo_dir), check=True, capture_output=True)
            context.hidden_run(["git", "config", "user.email", "ouroboros@local.mac"], cwd=str(context.repo_dir), check=True, capture_output=True)
            context.hidden_run(["git", "add", "-A"], cwd=str(context.repo_dir), check=True, capture_output=True)
            context.hidden_run(["git", "commit", "-m", "Initial commit from app bundle"], cwd=str(context.repo_dir), check=False, capture_output=True)
            context.hidden_run(["git", "branch", "-M", "ouroboros"], cwd=str(context.repo_dir), check=False, capture_output=True)
            context.hidden_run(["git", "branch", "ouroboros-stable"], cwd=str(context.repo_dir), check=False, capture_output=True)
        except Exception as exc:
            context.log.error("Git init failed: %s", exc)

    try:
        memory_dir = context.data_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        world_path = memory_dir / "WORLD.md"
        if not world_path.exists():
            env = os.environ.copy()
            env["PYTHONPATH"] = str(context.repo_dir)
            context.hidden_run(
                [
                    context.embedded_python,
                    "-c",
                    f"import sys; sys.path.insert(0, '{context.repo_dir}'); "
                    f"from ouroboros.world_profiler import generate_world_profile; "
                    f"generate_world_profile('{world_path}')",
                ],
                env=env,
                timeout=30,
                capture_output=True,
            )
    except Exception as exc:
        context.log.warning("World profile generation failed: %s", exc)

    _migrate_old_settings(context)
    install_deps(context)
    verify_claude_runtime(context)
    context.log.info("Bootstrap complete.")

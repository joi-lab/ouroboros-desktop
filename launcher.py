"""
Ouroboros Launcher — Immutable process manager.

This file is bundled into the .app via PyInstaller. It never self-modifies.
All agent logic lives in REPO_DIR and is launched as a subprocess via the
embedded python-build-standalone interpreter.

Responsibilities:
  - PID lock (single instance)
  - Bootstrap REPO_DIR on first run
  - Start/restart agent subprocess (server.py)
  - Display pywebview window pointing at agent's local HTTP server
  - Handle restart signals (agent exits with code 42)
"""

import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

from ouroboros.platform_layer import (
    IS_WINDOWS,
    IS_MACOS,
    embedded_python_candidates,
    kill_process_on_port,
    force_kill_pid,
    merge_hidden_kwargs,
    git_install_hint,
    create_kill_on_close_job,
    assign_pid_to_job,
    terminate_job,
    close_job,
    resume_process,
)

# ---------------------------------------------------------------------------
# Paths (single source of truth: ouroboros.config)
# ---------------------------------------------------------------------------
from ouroboros.config import (
    HOME, APP_ROOT, REPO_DIR, DATA_DIR, SETTINGS_PATH, PID_FILE, PORT_FILE,
    RESTART_EXIT_CODE, PANIC_EXIT_CODE, AGENT_SERVER_PORT,
    read_version, load_settings, save_settings, acquire_pid_lock, release_pid_lock,
)
MAX_CRASH_RESTARTS = 5
CRASH_WINDOW_SEC = 120

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_dir = DATA_DIR / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)

from logging.handlers import RotatingFileHandler

_file_handler = RotatingFileHandler(
    _log_dir / "launcher.log", maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_handlers: list = [_file_handler]
if not getattr(sys, "frozen", False):
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=_handlers)
log = logging.getLogger("launcher")


APP_VERSION = read_version()


def _hidden_run(command, **kwargs):
    """subprocess.run() with platform-appropriate hidden-window flags."""
    return subprocess.run(command, **merge_hidden_kwargs(kwargs))


def _hidden_popen(command, **kwargs):
    """subprocess.Popen() with platform-appropriate hidden-window flags."""
    return subprocess.Popen(command, **merge_hidden_kwargs(kwargs))


# ---------------------------------------------------------------------------
# Embedded Python
# ---------------------------------------------------------------------------
def _find_embedded_python() -> str:
    """Locate the embedded python-build-standalone interpreter."""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(sys._MEIPASS)
    else:
        base = pathlib.Path(__file__).parent
    for p in embedded_python_candidates(base):
        if p.exists():
            return str(p)
    return sys.executable


EMBEDDED_PYTHON = _find_embedded_python()


# ---------------------------------------------------------------------------
# Windows UI runtime
# ---------------------------------------------------------------------------
_windows_dll_dir_handles: list = []


def _show_windows_message(title: str, message: str) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _prepare_windows_webview_runtime() -> tuple[bool, str]:
    """Prepare pythonnet/pywebview runtime before importing webview on Windows."""
    if not IS_WINDOWS:
        return True, ""

    base_dir = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).parent))
    exe_dir = pathlib.Path(sys.executable).parent
    runtime_dir = base_dir / "pythonnet" / "runtime"
    webview_lib_dir = base_dir / "webview" / "lib"
    py_dll_name = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"

    def _unblock_file(path: pathlib.Path) -> None:
        try:
            os.remove(f"{path}:Zone.Identifier")
        except OSError:
            pass

    def _unblock_tree(root: pathlib.Path) -> None:
        if not root.is_dir():
            return
        for child in root.rglob("*"):
            if child.is_file() and child.suffix.lower() in {".dll", ".exe", ".pyd"}:
                _unblock_file(child)

    py_dll_candidates = [
        base_dir / py_dll_name,
        exe_dir / py_dll_name,
    ]
    for root, _dirs, files in os.walk(base_dir):
        if py_dll_name in files:
            py_dll_candidates.append(pathlib.Path(root) / py_dll_name)
            if len(py_dll_candidates) >= 6:
                break

    py_dll_path = next((p for p in py_dll_candidates if p.is_file()), None)
    runtime_dll_path = runtime_dir / "Python.Runtime.dll"
    if not runtime_dll_path.is_file():
        for root, _dirs, files in os.walk(base_dir):
            if "Python.Runtime.dll" in files:
                runtime_dll_path = pathlib.Path(root) / "Python.Runtime.dll"
                break

    if py_dll_path is None:
        return False, f"Bundled {py_dll_name} was not found."
    if not runtime_dll_path.is_file():
        return False, "Bundled Python.Runtime.dll was not found."

    _unblock_file(py_dll_path)
    _unblock_file(runtime_dll_path)
    _unblock_tree(runtime_dll_path.parent)
    _unblock_tree(webview_lib_dir)

    os.environ["PYTHONNET_RUNTIME"] = "netfx"
    os.environ["PYTHONNET_PYDLL"] = str(py_dll_path)

    search_dirs = []
    for candidate in (base_dir, exe_dir, runtime_dir, runtime_dll_path.parent, py_dll_path.parent, webview_lib_dir):
        candidate_str = str(candidate)
        if candidate.is_dir() and candidate_str not in search_dirs:
            search_dirs.append(candidate_str)

    current_path_parts = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    os.environ["PATH"] = os.pathsep.join(search_dirs + [p for p in current_path_parts if p and p not in search_dirs])

    if hasattr(os, "add_dll_directory"):
        global _windows_dll_dir_handles
        for candidate in search_dirs:
            try:
                _windows_dll_dir_handles.append(os.add_dll_directory(candidate))
            except (FileNotFoundError, OSError):
                pass

    try:
        from clr_loader import get_netfx
        from pythonnet import set_runtime
        set_runtime(get_netfx())
    except Exception as exc:
        return False, f"Windows .NET runtime init failed: {exc}"

    return True, ""


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def check_git() -> bool:
    if shutil.which("git") is not None:
        return True
    if IS_WINDOWS:
        for _candidate in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
        ):
            if os.path.isfile(_candidate):
                git_dir = os.path.dirname(_candidate)
                os.environ["PATH"] = git_dir + ";" + os.environ.get("PATH", "")
                return True
    return False


def _sync_core_files() -> None:
    """Sync core files from bundle to REPO_DIR on every launch."""
    if getattr(sys, "frozen", False):
        bundle_dir = pathlib.Path(sys._MEIPASS)
    else:
        bundle_dir = pathlib.Path(__file__).parent

    sync_paths = [
        "ouroboros/safety.py",
        "prompts/SAFETY.md",
        "ouroboros/tools/registry.py",
    ]
    for rel in sync_paths:
        src = bundle_dir / rel
        dst = REPO_DIR / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    log.info("Synced %d core files to %s", len(sync_paths), REPO_DIR)


def _commit_synced_files() -> None:
    """Commit sync'd safety files so git reset --hard doesn't revert them."""
    try:
        for rel in ["ouroboros/safety.py", "prompts/SAFETY.md", "ouroboros/tools/registry.py"]:
            subprocess.run(["git", "add", rel], cwd=str(REPO_DIR),
                           check=False, capture_output=True)
        status = subprocess.run(["git", "status", "--porcelain", "--",
                                 "ouroboros/safety.py", "prompts/SAFETY.md",
                                 "ouroboros/tools/registry.py"],
                                cwd=str(REPO_DIR), capture_output=True, text=True)
        if status.stdout.strip():
            subprocess.run(["git", "commit", "-m",
                            "safety-sync: restore protected files from bundle"],
                           cwd=str(REPO_DIR), check=False, capture_output=True)
            log.info("Committed synced safety files.")
    except Exception as e:
        log.warning("Failed to commit synced files: %s", e)


_REPO_GITIGNORE = """\
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


def _ensure_repo_gitignore(repo_dir: pathlib.Path) -> None:
    """Write .gitignore if missing — MUST run before any git add -A."""
    gi = repo_dir / ".gitignore"
    if not gi.exists():
        gi.write_text(_REPO_GITIGNORE, encoding="utf-8")


def bootstrap_repo() -> None:
    """Copy bundled codebase to REPO_DIR on first run, sync core files always."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if REPO_DIR.exists() and (REPO_DIR / "server.py").exists():
        _sync_core_files()
        _commit_synced_files()
        return

    needs_full_bootstrap = not REPO_DIR.exists()
    log.info("Bootstrapping repository to %s (full=%s)", REPO_DIR, needs_full_bootstrap)

    if getattr(sys, "frozen", False):
        bundle_dir = pathlib.Path(sys._MEIPASS)
    else:
        bundle_dir = pathlib.Path(__file__).parent

    if needs_full_bootstrap:
        shutil.copytree(bundle_dir, REPO_DIR, ignore=shutil.ignore_patterns(
            "repo", "data", "build", "dist", ".git", "__pycache__", "venv", ".venv",
            "Ouroboros.spec", "run_demo.sh", "demo_app.py", "app.py", "launcher.py",
            "colab_launcher.py", "colab_bootstrap_shim.py",
            "python-standalone",
            "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll",
            "*.dist-info", "base_library.zip",
        ))
    else:
        for item in ("server.py", "web", "assets"):
            src = bundle_dir / item
            dst = REPO_DIR / item
            if src.exists() and not dst.exists():
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

    # Initialize git repo if new
    if needs_full_bootstrap:
        _ensure_repo_gitignore(REPO_DIR)
        try:
            subprocess.run(["git", "init"], cwd=str(REPO_DIR), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Ouroboros"], cwd=str(REPO_DIR), check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "ouroboros@local.mac"], cwd=str(REPO_DIR), check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=str(REPO_DIR), check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "Initial commit from app bundle"], cwd=str(REPO_DIR), check=False, capture_output=True)
            subprocess.run(["git", "branch", "-M", "ouroboros"], cwd=str(REPO_DIR), check=False, capture_output=True)
            subprocess.run(["git", "branch", "ouroboros-stable"], cwd=str(REPO_DIR), check=False, capture_output=True)
        except Exception as e:
            log.error("Git init failed: %s", e)

    # Generate world profile
    try:
        memory_dir = DATA_DIR / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        world_path = memory_dir / "WORLD.md"
        if not world_path.exists():
            env = os.environ.copy()
            env["PYTHONPATH"] = str(REPO_DIR)
            subprocess.run(
                [EMBEDDED_PYTHON, "-c",
                 f"import sys; sys.path.insert(0, '{REPO_DIR}'); "
                 f"from ouroboros.world_profiler import generate_world_profile; "
                 f"generate_world_profile('{world_path}')"],
                env=env, timeout=30, capture_output=True,
            )
    except Exception as e:
        log.warning("World profile generation failed: %s", e)

    # Migrate old settings if needed
    _migrate_old_settings()

    # Install dependencies
    _install_deps()
    log.info("Bootstrap complete.")


def _migrate_old_settings() -> None:
    """Migrate old-style env-only settings to settings.json for existing users."""
    if SETTINGS_PATH.exists():
        return

    migrated = {}
    env_keys = [
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "OUROBOROS_MODEL", "OUROBOROS_MODEL_CODE", "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK", "TOTAL_BUDGET", "OUROBOROS_MAX_WORKERS",
        "OUROBOROS_SOFT_TIMEOUT_SEC", "OUROBOROS_HARD_TIMEOUT_SEC",
        "GITHUB_TOKEN", "GITHUB_REPO",
    ]
    for key in env_keys:
        val = os.environ.get(key, "")
        if val:
            try:
                if key in ("TOTAL_BUDGET",):
                    migrated[key] = float(val)
                elif key in ("OUROBOROS_MAX_WORKERS", "OUROBOROS_SOFT_TIMEOUT_SEC", "OUROBOROS_HARD_TIMEOUT_SEC"):
                    migrated[key] = int(val)
                else:
                    migrated[key] = val
            except (ValueError, TypeError):
                migrated[key] = val

    # Also check for old settings.json in data/state/
    old_settings = DATA_DIR / "state" / "settings.json"
    if old_settings.exists():
        try:
            old = json.loads(old_settings.read_text(encoding="utf-8"))
            for key in env_keys:
                if key in old and key not in migrated:
                    migrated[key] = old[key]
        except Exception:
            pass

    if migrated:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SETTINGS_PATH.write_text(json.dumps(migrated, indent=2), encoding="utf-8")
        log.info("Migrated %d settings to %s", len(migrated), SETTINGS_PATH)


def _install_deps() -> None:
    """Install Python dependencies for the agent."""
    pyproject = REPO_DIR / "pyproject.toml"
    req_file = REPO_DIR / "requirements.txt"  # legacy fallback
    if pyproject.exists():
        log.info("Installing agent dependencies from pyproject.toml ...")
        cmd = [EMBEDDED_PYTHON, "-m", "pip", "install", "-q", str(REPO_DIR)]
    elif req_file.exists():
        log.info("Installing agent dependencies from requirements.txt ...")
        cmd = [EMBEDDED_PYTHON, "-m", "pip", "install", "-q", "-r", str(req_file)]
    else:
        return
    try:
        subprocess.run(cmd, timeout=300, capture_output=True)
    except Exception as e:
        log.warning("Dependency install failed: %s", e)


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------
_agent_proc: Optional[subprocess.Popen] = None
_agent_job = None
_agent_lock = threading.Lock()
_shutdown_event = threading.Event()


def start_agent(port: int = AGENT_SERVER_PORT) -> subprocess.Popen:
    """Start the agent server.py as a subprocess."""
    global _agent_proc, _agent_job
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR)
    env["OUROBOROS_SERVER_PORT"] = str(port)
    env["OUROBOROS_DATA_DIR"] = str(DATA_DIR)
    env["OUROBOROS_REPO_DIR"] = str(REPO_DIR)
    env["OUROBOROS_APP_VERSION"] = str(APP_VERSION)
    env["OUROBOROS_MANAGED_BY_LAUNCHER"] = "1"

    # Pass settings as env vars
    settings = _load_settings()
    for key, val in settings.items():
        if val:
            env[key] = str(val)

    server_py = REPO_DIR / "server.py"
    log.info("Starting agent: %s %s (port=%d)", EMBEDDED_PYTHON, server_py, port)

    proc = _hidden_popen(
        [EMBEDDED_PYTHON, str(server_py)],
        cwd=str(REPO_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _agent_proc = proc
    _agent_job = None

    if IS_WINDOWS:
        try:
            job = create_kill_on_close_job()
            if job is not None and assign_pid_to_job(job, proc.pid):
                _agent_job = job
            elif job is not None:
                close_job(job)
            resume_process(proc.pid)
        except Exception:
            if _agent_job is not None:
                close_job(_agent_job)
                _agent_job = None

    # Stream agent stdout to log file in background
    def _stream_output():
        log_path = DATA_DIR / "logs" / "agent_stdout.log"
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                for line in iter(proc.stdout.readline, b""):
                    decoded = line.decode("utf-8", errors="replace")
                    f.write(decoded)
                    f.flush()
        except Exception:
            pass

    threading.Thread(target=_stream_output, daemon=True).start()
    return proc


def stop_agent() -> None:
    """Gracefully stop the agent process."""
    global _agent_proc, _agent_job
    with _agent_lock:
        if _agent_proc is None:
            return
        proc = _agent_proc
        job = _agent_job
        _agent_proc = None
        _agent_job = None
    log.info("Stopping agent (pid=%s)...", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if IS_WINDOWS and job is not None:
            terminate_job(job)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    if IS_WINDOWS and job is not None:
        close_job(job)


def _read_port_file() -> int:
    """Read the active port from PORT_FILE (written by server.py)."""
    try:
        if PORT_FILE.exists():
            return int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pass
    return AGENT_SERVER_PORT


def _kill_stale_on_port(port: int) -> None:
    """Kill any process listening on the given port (cleanup from previous runs)."""
    if IS_WINDOWS:
        kill_process_on_port(port)
        return
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True, text=True, timeout=5,
        )
        pids = result.stdout.strip().split()
        for pid_str in pids:
            try:
                pid = int(pid_str)
                if pid != os.getpid():
                    force_kill_pid(pid)
                    log.info("Killed stale process %d on port %d", pid, port)
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except Exception:
        pass


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """Wait for the agent HTTP server to become responsive."""
    import urllib.request
    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _poll_port_file(timeout: float = 30.0) -> int:
    """Poll port file until it's freshly written (mtime within last 10s)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if PORT_FILE.exists():
                age = time.time() - PORT_FILE.stat().st_mtime
                if age < 10:
                    return int(PORT_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
        time.sleep(0.5)
    return _read_port_file()


_webview_window = None  # set by main(), used by lifecycle loop


def agent_lifecycle_loop(port: int = AGENT_SERVER_PORT) -> None:
    """Main loop: start agent, monitor, restart on exit code 42 or crash."""
    global _agent_job
    crash_times: list = []

    # Kill anything left over from a previous launcher session
    _kill_stale_on_port(port)

    while not _shutdown_event.is_set():
        # Delete stale port file so _poll_port_file waits for a fresh write
        try:
            PORT_FILE.unlink(missing_ok=True)
        except OSError:
            pass

        proc = start_agent(port)

        # Wait for the server to write a fresh port file, then check health
        actual_port = _poll_port_file(timeout=30)
        if not _wait_for_server(actual_port, timeout=45):
            log.warning("Agent server did not become responsive within 45s (port %d)", actual_port)

        proc.wait()
        exit_code = proc.returncode
        log.info("Agent exited with code %d", exit_code)

        with _agent_lock:
            _agent_proc = None
            if IS_WINDOWS and _agent_job is not None:
                close_job(_agent_job)
                _agent_job = None

        if _shutdown_event.is_set():
            break

        # Panic stop: kill everything, close app, no restart
        if exit_code == PANIC_EXIT_CODE:
            log.info("Panic stop (exit code %d) — shutting down completely.", PANIC_EXIT_CODE)
            _shutdown_event.set()
            _kill_stale_on_port(port)
            import multiprocessing as _mp
            for child in _mp.active_children():
                try:
                    force_kill_pid(child.pid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if _webview_window:
                try:
                    _webview_window.destroy()
                except Exception:
                    pass
            break

        # Wait for port to fully release after process exit
        time.sleep(2)

        if exit_code == RESTART_EXIT_CODE:
            log.info("Agent requested restart (exit code 42). Restarting...")
            _sync_core_files()
            _commit_synced_files()
            _install_deps()
            _kill_stale_on_port(port)
            continue

        # Crash detection
        now = time.time()
        crash_times.append(now)
        crash_times[:] = [t for t in crash_times if (now - t) < CRASH_WINDOW_SEC]
        if len(crash_times) >= MAX_CRASH_RESTARTS:
            log.error("Agent crashed %d times in %ds. Stopping.", MAX_CRASH_RESTARTS, CRASH_WINDOW_SEC)
            break

        log.info("Agent crashed. Restarting in 3s...")
        _kill_stale_on_port(port)
        time.sleep(3)


# ---------------------------------------------------------------------------
# Settings (delegated to ouroboros.config)
# ---------------------------------------------------------------------------
def _load_settings() -> dict:
    return load_settings()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if IS_WINDOWS:
        ok, reason = _prepare_windows_webview_runtime()
        if not ok:
            log.error("Windows UI runtime initialization failed: %s", reason)
            _show_windows_message(
                "Ouroboros — Startup Failed",
                "Windows UI runtime initialization failed.\n\n"
                f"{reason}\n\n"
                "Check launcher.log for details.",
            )
            return

    import webview

    if not acquire_pid_lock():
        log.error("Another instance already running.")
        webview.create_window(
            "Ouroboros",
            html="<html><body style='background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                 "<div style='text-align:center'><h2>Ouroboros is already running</h2><p>Only one instance can run at a time.</p></div></body></html>",
            width=420, height=200,
        )
        webview.start()
        return

    import atexit
    atexit.register(release_pid_lock)

    # Check git
    if not check_git():
        log.warning("Git not found.")
        _result = {"installed": False}
        _hint = git_install_hint()
        _install_status = (
            "Installing... A system dialog may appear."
            if IS_MACOS else
            "Installing... Please wait."
        )

        def _git_page(window):
            window.evaluate_js("""
                document.getElementById('install-btn').onclick = function() {
                    document.getElementById('status').textContent = '__INSTALL_STATUS__';
                    window.pywebview.api.install_git();
                };
            """.replace("__INSTALL_STATUS__", _install_status))

        class GitApi:
            def install_git(self):
                if IS_MACOS:
                    subprocess.Popen(["xcode-select", "--install"])
                elif IS_WINDOWS:
                    _hidden_popen(["winget", "install", "Git.Git", "--source", "winget", "--accept-source-agreements"])
                else:
                    for cmd in [["sudo", "apt", "install", "-y", "git"],
                                ["sudo", "dnf", "install", "-y", "git"]]:
                        try:
                            _hidden_popen(cmd)
                            break
                        except FileNotFoundError:
                            continue
                for _ in range(300):
                    time.sleep(3)
                    if shutil.which("git"):
                        _result["installed"] = True
                        return "installed"
                return "timeout"

        git_window = webview.create_window(
            "Ouroboros — Setup Required",
            html=(
                """<html><body style="background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center">
                <h2>Git is required</h2>
                <p>Ouroboros needs Git to manage its local repository.</p>
                <button id="install-btn" style="padding:10px 24px;border-radius:8px;border:none;background:#0ea5e9;color:white;cursor:pointer;font-size:14px">
                    Install Git (Xcode CLI Tools)
                </button>
                <p id="status" style="color:#fbbf24;margin-top:12px"></p>
            </div></body></html>"""
                if IS_MACOS else
                f"""<html><body style="background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center">
                <h2>Git is required</h2>
                <p>Ouroboros needs Git to manage its local repository.</p>
                <p style="color:#94a3b8;font-size:13px;margin-top:8px">{_hint}</p>
                <button id="install-btn" style="padding:10px 24px;border-radius:8px;border:none;background:#0ea5e9;color:white;cursor:pointer;font-size:14px;margin-top:12px">
                    Install Git
                </button>
                <p id="status" style="color:#fbbf24;margin-top:12px"></p>
            </div></body></html>"""
            ),
            js_api=GitApi(),
            width=520, height=300,
        )
        webview.start(func=_git_page, args=[git_window])
        if not check_git():
            sys.exit(1)

    # Bootstrap
    bootstrap_repo()

    global _webview_window
    port = AGENT_SERVER_PORT

    # Start agent lifecycle in background
    lifecycle_thread = threading.Thread(target=agent_lifecycle_loop, args=(port,), daemon=True)
    lifecycle_thread.start()

    # Wait for server to be ready, then read actual port (may differ if default was busy)
    _wait_for_server(port, timeout=15)
    actual_port = _read_port_file()
    if actual_port != port:
        _wait_for_server(actual_port, timeout=45)
    else:
        _wait_for_server(port, timeout=45)

    url = f"http://127.0.0.1:{actual_port}"

    window = webview.create_window(
        f"Ouroboros v{APP_VERSION}",
        url=url,
        width=1100,
        height=750,
        min_size=(800, 500),
        background_color="#0d0b0f",
        text_select=True,
    )

    def _on_closing():
        log.info("Window closing — graceful shutdown.")
        _shutdown_event.set()
        stop_agent()
        _kill_orphaned_children()
        release_pid_lock()
        os._exit(0)

    def _kill_orphaned_children():
        """Final safety net: kill any processes still on the server port.

        After stop_agent() terminates server.py, worker grandchildren may
        survive as orphans. Sweeping the port guarantees nothing lingers.
        """
        _kill_stale_on_port(port)
        _kill_stale_on_port(8766)
        for child in __import__('multiprocessing').active_children():
            try:
                force_kill_pid(child.pid)
                log.info("Killed orphaned child pid=%d", child.pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    window.events.closing += _on_closing
    _webview_window = window

    webview.start(debug=False)


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()

    if sys.platform == "darwin":
        try:
            _shell_path = subprocess.check_output(
                ["/bin/bash", "-l", "-c", "echo $PATH"], text=True, timeout=5,
            ).strip()
            if _shell_path:
                os.environ["PATH"] = _shell_path
        except Exception:
            pass

    main()

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

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME = pathlib.Path.home()
APP_ROOT = HOME / "Ouroboros"
REPO_DIR = APP_ROOT / "repo"
DATA_DIR = APP_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
PID_FILE = APP_ROOT / "ouroboros.pid"
PORT_FILE = DATA_DIR / "state" / "server_port"

_OLD_LOCATIONS = [
    HOME / "Library" / "Application Support" / "Ouroboros",
    HOME / "Ouroboros",
]

RESTART_EXIT_CODE = 42
AGENT_SERVER_PORT = 8765
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


def _read_version() -> str:
    try:
        if getattr(sys, "frozen", False):
            vp = pathlib.Path(sys._MEIPASS) / "VERSION"
        else:
            vp = pathlib.Path(__file__).parent / "VERSION"
        return vp.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


APP_VERSION = _read_version()


# ---------------------------------------------------------------------------
# PID lock
# ---------------------------------------------------------------------------
def acquire_pid_lock() -> bool:
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if existing_pid != os.getpid():
                os.kill(existing_pid, 0)
                return False
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_pid_lock() -> None:
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Embedded Python
# ---------------------------------------------------------------------------
def _find_embedded_python() -> str:
    """Locate the embedded python-build-standalone interpreter."""
    if getattr(sys, "frozen", False):
        candidates = [
            pathlib.Path(sys._MEIPASS) / "python-standalone" / "bin" / "python3",
            pathlib.Path(sys._MEIPASS) / "python-standalone" / "bin" / "python",
        ]
    else:
        candidates = [
            pathlib.Path(__file__).parent / "python-standalone" / "bin" / "python3",
            pathlib.Path(__file__).parent / "python-standalone" / "bin" / "python",
        ]
    for p in candidates:
        if p.exists():
            return str(p)
    return sys.executable


EMBEDDED_PYTHON = _find_embedded_python()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def check_git() -> bool:
    return shutil.which("git") is not None


def _sync_core_files() -> None:
    """Sync core files from bundle to REPO_DIR on every launch."""
    if getattr(sys, "frozen", False):
        bundle_dir = pathlib.Path(sys._MEIPASS)
    else:
        bundle_dir = pathlib.Path(__file__).parent

    sync_paths = [
        "prompts/SAFETY.md",
        "prompts/SYSTEM.md",
        "ouroboros/safety.py",
        "ouroboros/tools/registry.py",
        "ouroboros/loop.py",
        "server.py",
    ]
    for rel in sync_paths:
        src = bundle_dir / rel
        dst = REPO_DIR / rel
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    log.info("Synced %d core files to %s", len(sync_paths), REPO_DIR)


def _migrate_from_old_location() -> None:
    """Move data from any old location to ~/Ouroboros/."""
    if APP_ROOT.exists():
        return
    for old in _OLD_LOCATIONS:
        if old.exists():
            log.info("Migrating data from %s to %s", old, APP_ROOT)
            try:
                shutil.move(str(old), str(APP_ROOT))
                return
            except OSError as e:
                log.warning("Migration from %s failed: %s", old, e)


def bootstrap_repo() -> None:
    """Copy bundled codebase to REPO_DIR on first run, sync core files always."""
    _migrate_from_old_location()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if REPO_DIR.exists() and (REPO_DIR / "server.py").exists():
        _sync_core_files()
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
            "python-standalone", "assets", "*.pyc",
        ))
    else:
        for item in ("server.py", "web"):
            src = bundle_dir / item
            dst = REPO_DIR / item
            if src.exists() and not dst.exists():
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)

    # Initialize git repo if new
    if needs_full_bootstrap:
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
    req_file = REPO_DIR / "requirements.txt"
    if not req_file.exists():
        return
    log.info("Installing agent dependencies...")
    try:
        subprocess.run(
            [EMBEDDED_PYTHON, "-m", "pip", "install", "-q", "-r", str(req_file)],
            timeout=300, capture_output=True,
        )
    except Exception as e:
        log.warning("Dependency install failed: %s", e)


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------
_agent_proc: Optional[subprocess.Popen] = None
_agent_lock = threading.Lock()
_shutdown_event = threading.Event()


def start_agent(port: int = AGENT_SERVER_PORT) -> subprocess.Popen:
    """Start the agent server.py as a subprocess."""
    global _agent_proc
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR)
    env["OUROBOROS_SERVER_PORT"] = str(port)
    env["OUROBOROS_DATA_DIR"] = str(DATA_DIR)
    env["OUROBOROS_REPO_DIR"] = str(REPO_DIR)

    # Pass settings as env vars
    settings = _load_settings()
    for key, val in settings.items():
        if val:
            env[key] = str(val)

    server_py = REPO_DIR / "server.py"
    log.info("Starting agent: %s %s (port=%d)", EMBEDDED_PYTHON, server_py, port)

    proc = subprocess.Popen(
        [EMBEDDED_PYTHON, str(server_py)],
        cwd=str(REPO_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _agent_proc = proc

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
    global _agent_proc
    with _agent_lock:
        if _agent_proc is None:
            return
        proc = _agent_proc
    log.info("Stopping agent (pid=%s)...", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass
    with _agent_lock:
        _agent_proc = None


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
                    os.kill(pid, 9)
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

        if _shutdown_event.is_set():
            break

        # Wait for port to fully release after process exit
        time.sleep(2)

        if exit_code == RESTART_EXIT_CODE:
            log.info("Agent requested restart (exit code 42). Restarting...")
            _sync_core_files()
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
# Settings
# ---------------------------------------------------------------------------
def _load_settings() -> dict:
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


# ---------------------------------------------------------------------------
# First-run wizard
# ---------------------------------------------------------------------------
_WIZARD_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0f0f1a; color:#e2e8f0; font-family:-apple-system,system-ui,sans-serif;
       display:flex; align-items:center; justify-content:center; height:100vh; }
.card { background:rgba(255,255,255,.06); border-radius:16px; padding:32px; width:440px; }
h2 { font-size:22px; margin-bottom:4px; }
.sub { color:rgba(255,255,255,.5); font-size:13px; margin-bottom:20px; }
label { display:block; font-size:12px; color:rgba(255,255,255,.5); margin-bottom:4px; margin-top:14px; }
input { width:100%; padding:8px 12px; border-radius:8px; border:1px solid rgba(255,255,255,.12);
        background:#1a1a2e; color:#e2e8f0; font-size:14px; outline:none; font-family:inherit; }
input:focus { border-color:#2dd4bf; }
.row { display:flex; gap:12px; }
.row .field { flex:1; }
.btn { margin-top:20px; width:100%; padding:10px; border-radius:8px; border:none;
       background:#2dd4bf; color:#0f0f1a; font-size:14px; font-weight:600; cursor:pointer; font-family:inherit; }
.btn:hover { opacity:.9; }
.btn:disabled { opacity:.5; cursor:default; }
.err { color:#ef4444; font-size:12px; margin-top:8px; display:none; }
a { color:#2dd4bf; }
</style></head><body>
<div class="card">
  <h2>Welcome to Ouroboros</h2>
  <p class="sub">Enter your API key to get started. You can change all settings later.</p>
  <label>OpenRouter API Key <span style="color:#ef4444">*</span></label>
  <input id="api-key" type="password" placeholder="sk-or-v1-..." autofocus>
  <p style="font-size:11px;color:rgba(255,255,255,.38);margin-top:4px">
    Get one at <a href="https://openrouter.ai/keys" target="_blank">openrouter.ai/keys</a>
  </p>
  <label>Total Budget ($)</label>
  <input id="budget" type="number" value="10" min="1" step="1">
  <div class="row">
    <div class="field"><label>Main Model</label><input id="model" value="anthropic/claude-sonnet-4.6"></div>
  </div>
  <p class="err" id="err"></p>
  <button class="btn" id="save-btn" disabled>Start Ouroboros</button>
</div>
<script>
const keyInput = document.getElementById('api-key');
const btn = document.getElementById('save-btn');
keyInput.addEventListener('input', () => { btn.disabled = keyInput.value.trim().length < 10; });
btn.addEventListener('click', async () => {
    btn.disabled = true; btn.textContent = 'Saving...';
    const result = await window.pywebview.api.save_wizard({
        OPENROUTER_API_KEY: keyInput.value.trim(),
        TOTAL_BUDGET: parseFloat(document.getElementById('budget').value) || 10,
        OUROBOROS_MODEL: document.getElementById('model').value.trim() || 'anthropic/claude-sonnet-4.6',
    });
    if (result === 'ok') { btn.textContent = 'Starting...'; }
    else { document.getElementById('err').style.display='block';
           document.getElementById('err').textContent=result; btn.disabled=false; btn.textContent='Start Ouroboros'; }
});
</script></body></html>"""


def _save_settings(settings: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(SETTINGS_PATH))


def _run_first_run_wizard() -> bool:
    """Show setup wizard if no API key configured. Returns True if key was saved."""
    settings = _load_settings()
    if settings.get("OPENROUTER_API_KEY"):
        return True

    import webview
    _wizard_done = {"ok": False}

    class WizardApi:
        def save_wizard(self, data: dict) -> str:
            key = str(data.get("OPENROUTER_API_KEY", "")).strip()
            if len(key) < 10:
                return "API key is too short."
            settings.update(data)
            try:
                _save_settings(settings)
                _wizard_done["ok"] = True
                for w in webview.windows:
                    w.destroy()
                return "ok"
            except Exception as e:
                return f"Failed to save: {e}"

    webview.create_window(
        "Ouroboros — Setup",
        html=_WIZARD_HTML,
        js_api=WizardApi(),
        width=520,
        height=480,
    )
    webview.start()
    return _wizard_done["ok"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
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

        def _git_page(window):
            window.evaluate_js("""
                document.getElementById('install-btn').onclick = function() {
                    document.getElementById('status').textContent = 'Installing... A system dialog may appear.';
                    window.pywebview.api.install_git();
                };
            """)

        class GitApi:
            def install_git(self):
                subprocess.Popen(["xcode-select", "--install"])
                for _ in range(300):
                    time.sleep(3)
                    if shutil.which("git"):
                        _result["installed"] = True
                        return "installed"
                return "timeout"

        git_window = webview.create_window(
            "Ouroboros — Setup Required",
            html="""<html><body style="background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center">
                <h2>Git is required</h2>
                <p>Ouroboros needs Git to manage its local repository.</p>
                <button id="install-btn" style="padding:10px 24px;border-radius:8px;border:none;background:#0ea5e9;color:white;cursor:pointer;font-size:14px">
                    Install Git (Xcode CLI Tools)
                </button>
                <p id="status" style="color:#fbbf24;margin-top:12px"></p>
            </div></body></html>""",
            js_api=GitApi(),
            width=520, height=300,
        )
        webview.start(func=_git_page, args=[git_window])
        if not check_git():
            sys.exit(1)

    # Bootstrap
    bootstrap_repo()

    # First-run wizard (API key)
    if not _run_first_run_wizard():
        log.info("Wizard was closed without saving. Launching anyway (Settings page available).")

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
        background_color="#0f0f1a",
        text_select=True,
    )

    def _on_closing():
        log.info("Window closing — graceful shutdown.")
        _shutdown_event.set()
        stop_agent()
        release_pid_lock()

    window.events.closing += _on_closing
    _webview_window = window

    webview.start(debug=(not getattr(sys, "frozen", False)))


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

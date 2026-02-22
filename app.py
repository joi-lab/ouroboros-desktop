"""
Ouroboros Desktop App — Flet UI & Main Launcher
"""

import asyncio
import json
import logging
import os
import pathlib
import shutil
import sys
import subprocess
import threading
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List, Tuple
import flet as ft

# ---------------------------------------------------------------------------
# Setup Logging
# ---------------------------------------------------------------------------
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_dir = pathlib.Path.home() / "Documents" / "Ouroboros" / "data" / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)
_file_handler = RotatingFileHandler(
    _log_dir / "app.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_handlers: list = [_file_handler]
if not getattr(sys, 'frozen', False):
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=_handlers)
log = logging.getLogger(__name__)


def _read_version() -> str:
    try:
        if getattr(sys, 'frozen', False):
            vp = pathlib.Path(sys._MEIPASS) / "VERSION"
        else:
            vp = pathlib.Path(__file__).parent / "VERSION"
        return vp.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


APP_VERSION = _read_version()
APP_START = time.time()
ASSETS_DIR = str(pathlib.Path(sys._MEIPASS) / "assets") if getattr(sys, 'frozen', False) else str(pathlib.Path(__file__).parent / "assets")

# ---------------------------------------------------------------------------
# Paths and Bootstrapping
# ---------------------------------------------------------------------------
HOME = pathlib.Path.home()
APP_ROOT = HOME / "Documents" / "Ouroboros"
REPO_DIR = APP_ROOT / "repo"
DATA_DIR = APP_ROOT / "data"
SETTINGS_PATH = DATA_DIR / "settings.json"
PID_FILE = APP_ROOT / "ouroboros.pid"

_SETTINGS_LOCK = pathlib.Path(str(SETTINGS_PATH) + ".lock")
_SETTINGS_DEFAULTS = {
    "OPENROUTER_API_KEY": "",
    "OPENAI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "OUROBOROS_MODEL": "anthropic/claude-sonnet-4.6",
    "OUROBOROS_MODEL_CODE": "anthropic/claude-sonnet-4.6",
    "OUROBOROS_MODEL_LIGHT": "google/gemini-2.5-flash",
    "OUROBOROS_MODEL_FALLBACK": "google/gemini-2.5-flash",
    "OUROBOROS_MAX_WORKERS": 5,
    "TOTAL_BUDGET": 10.0,
    "OUROBOROS_SOFT_TIMEOUT_SEC": 600,
    "OUROBOROS_HARD_TIMEOUT_SEC": 1800,
    "OUROBOROS_BG_MAX_ROUNDS": 5,
    "OUROBOROS_BG_WAKEUP_MIN": 30,
    "OUROBOROS_BG_WAKEUP_MAX": 7200,
    "OUROBOROS_EVO_COST_THRESHOLD": 0.10,
    "OUROBOROS_WEBSEARCH_MODEL": "gpt-5",
    "GITHUB_TOKEN": "",
    "GITHUB_REPO": "",
}


def _acquire_settings_lock(timeout: float = 2.0) -> Optional[int]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            fd = os.open(str(_SETTINGS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            return fd
        except FileExistsError:
            try:
                if time.time() - _SETTINGS_LOCK.stat().st_mtime > 10:
                    _SETTINGS_LOCK.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.01)
        except Exception:
            break
    return None


def _release_settings_lock(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        _SETTINGS_LOCK.unlink()
    except Exception:
        pass


def load_settings() -> dict:
    fd = _acquire_settings_lock()
    try:
        if SETTINGS_PATH.exists():
            try:
                return json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            except Exception:
                pass
        return dict(_SETTINGS_DEFAULTS)
    finally:
        _release_settings_lock(fd)


def save_settings(settings: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = _acquire_settings_lock()
    try:
        try:
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(SETTINGS_PATH))
        except OSError:
            SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    finally:
        _release_settings_lock(fd)


def acquire_pid_lock() -> bool:
    """Try to acquire single-instance PID lock. Returns True if acquired."""
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    if PID_FILE.exists():
        try:
            existing_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
            if existing_pid != os.getpid():
                os.kill(existing_pid, 0)
                return False  # different process alive — another instance
        except (ProcessLookupError, PermissionError, ValueError, OSError):
            PID_FILE.unlink(missing_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_pid_lock() -> None:
    """Remove PID file on clean shutdown."""
    try:
        if PID_FILE.exists() and PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def bootstrap_repo():
    """Copy the bundled codebase to the local repo directory on first run."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not REPO_DIR.exists():
        log.info(f"First run detected. Bootstrapping repository to {REPO_DIR}...")

        if getattr(sys, 'frozen', False):
            bundle_dir = pathlib.Path(sys._MEIPASS)
        else:
            bundle_dir = pathlib.Path(__file__).parent

        shutil.copytree(bundle_dir, REPO_DIR, ignore=shutil.ignore_patterns(
            "repo", "data", "build", "dist", ".git", "__pycache__", "venv", ".venv",
            "Ouroboros.spec", "run_demo.sh", "demo_app.py",
            "colab_launcher.py", "colab_bootstrap_shim.py",
            "assets", "*.pyc",
        ))

        from ouroboros.world_profiler import generate_world_profile
        memory_dir = DATA_DIR / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        generate_world_profile(str(memory_dir / "WORLD.md"))

        import dulwich.repo
        from supervisor.git_ops import git_capture
        repo = dulwich.repo.Repo.init(str(REPO_DIR))

        subprocess.run(["git", "config", "user.name", "Ouroboros"], cwd=str(REPO_DIR), check=True)
        subprocess.run(["git", "config", "user.email", "ouroboros@local.mac"], cwd=str(REPO_DIR), check=True)
        subprocess.run(["git", "add", "-A"], cwd=str(REPO_DIR), check=True)
        subprocess.run(["git", "commit", "-m", "Initial commit from app bundle"], cwd=str(REPO_DIR), check=False)
        subprocess.run(["git", "branch", "-M", "ouroboros"], cwd=str(REPO_DIR), check=False)
        subprocess.run(["git", "branch", "ouroboros-stable"], cwd=str(REPO_DIR), check=False)
        log.info("Bootstrap complete.")

# ---------------------------------------------------------------------------
# Background Supervisor Loop
# ---------------------------------------------------------------------------
SUPERVISOR_THREAD = None
CHAT_BRIDGE = None
SUPERVISOR_READY = threading.Event()
SUPERVISOR_ERROR: Optional[str] = None
_SUPERVISOR_CRASH_COUNT = 0


def run_supervisor(settings: dict):
    global CHAT_BRIDGE, SUPERVISOR_ERROR, _SUPERVISOR_CRASH_COUNT

    os.environ["OPENROUTER_API_KEY"] = str(settings.get("OPENROUTER_API_KEY", ""))
    os.environ["OPENAI_API_KEY"] = str(settings.get("OPENAI_API_KEY", ""))
    os.environ["ANTHROPIC_API_KEY"] = str(settings.get("ANTHROPIC_API_KEY", ""))
    os.environ["OUROBOROS_MODEL"] = str(settings.get("OUROBOROS_MODEL", "anthropic/claude-sonnet-4.6"))
    os.environ["OUROBOROS_MODEL_CODE"] = str(settings.get("OUROBOROS_MODEL_CODE", "anthropic/claude-sonnet-4.6"))
    os.environ["OUROBOROS_MODEL_LIGHT"] = str(settings.get("OUROBOROS_MODEL_LIGHT", "google/gemini-2.5-flash"))
    os.environ["OUROBOROS_MODEL_FALLBACK"] = str(settings.get("OUROBOROS_MODEL_FALLBACK", "google/gemini-2.5-flash"))
    os.environ["TOTAL_BUDGET"] = str(settings.get("TOTAL_BUDGET", 10.0))
    for _gh_k in ("GITHUB_TOKEN", "GITHUB_REPO"):
        if settings.get(_gh_k):
            os.environ[_gh_k] = str(settings[_gh_k])
        else:
            os.environ.pop(_gh_k, None)
    for _env_k in ("OUROBOROS_BG_MAX_ROUNDS", "OUROBOROS_BG_WAKEUP_MIN",
                    "OUROBOROS_BG_WAKEUP_MAX", "OUROBOROS_EVO_COST_THRESHOLD",
                    "OUROBOROS_WEBSEARCH_MODEL"):
        if _env_k in settings:
            os.environ[_env_k] = str(settings[_env_k])

    import queue as _queue_mod
    from supervisor.telegram import LocalChatBridge, init as telegram_init
    CHAT_BRIDGE = LocalChatBridge()

    from ouroboros.utils import set_log_sink
    set_log_sink(CHAT_BRIDGE.push_log)

    try:
        telegram_init(
            drive_root=DATA_DIR,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            budget_report_every=10,
            tg_client=CHAT_BRIDGE,
        )

        from supervisor.state import init as state_init, init_state, load_state, save_state, append_jsonl, update_budget_from_usage, rotate_chat_log_if_needed
        state_init(DATA_DIR, float(settings.get("TOTAL_BUDGET", 10.0)))
        init_state()

        from supervisor.git_ops import init as git_ops_init, ensure_repo_present, safe_restart
        git_ops_init(
            repo_dir=REPO_DIR, drive_root=DATA_DIR, remote_url="",
            branch_dev="ouroboros", branch_stable="ouroboros-stable",
        )

        ensure_repo_present()
        ok, msg = safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")
        if not ok:
            log.error(f"Supervisor Bootstrap failed: {msg}")

        from supervisor.queue import enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed, persist_queue_snapshot, restore_pending_from_snapshot, cancel_task_by_id, queue_review_task, sort_pending
        from supervisor.workers import init as workers_init, get_event_q, WORKERS, PENDING, RUNNING, spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy, handle_chat_direct, _get_chat_agent, auto_resume_after_restart

        max_workers = int(settings.get("OUROBOROS_MAX_WORKERS", 5))
        soft_timeout = int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600))
        hard_timeout = int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800))

        workers_init(
            repo_dir=REPO_DIR, drive_root=DATA_DIR, max_workers=max_workers,
            soft_timeout=soft_timeout, hard_timeout=hard_timeout,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            branch_dev="ouroboros", branch_stable="ouroboros-stable",
        )

        from supervisor.events import dispatch_event
        import types

        kill_workers()
        spawn_workers(max_workers)
        restored_pending = restore_pending_from_snapshot()
        persist_queue_snapshot(reason="startup")

        from supervisor.telegram import send_with_budget

        if restored_pending > 0:
            st_boot = load_state()
            if st_boot.get("owner_chat_id"):
                send_with_budget(int(st_boot["owner_chat_id"]), f"\u267b\ufe0f Restored pending queue from snapshot: {restored_pending} tasks.")

        auto_resume_after_restart()

        from ouroboros.consciousness import BackgroundConsciousness
        def _get_owner_chat_id() -> Optional[int]:
            try:
                st = load_state()
                cid = st.get("owner_chat_id")
                return int(cid) if cid else None
            except Exception:
                return None

        _consciousness = BackgroundConsciousness(
            drive_root=DATA_DIR,
            repo_dir=REPO_DIR,
            event_queue=get_event_q(),
            owner_chat_id_fn=_get_owner_chat_id,
        )

        log.info("Background consciousness initialized but not started (default: off).")

        _event_ctx = types.SimpleNamespace(
            DRIVE_ROOT=DATA_DIR, REPO_DIR=REPO_DIR,
            BRANCH_DEV="ouroboros", BRANCH_STABLE="ouroboros-stable",
            TG=CHAT_BRIDGE, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
            MAX_WORKERS=max_workers,
            send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
            update_budget_from_usage=update_budget_from_usage, append_jsonl=append_jsonl,
            enqueue_task=enqueue_task, cancel_task_by_id=cancel_task_by_id,
            queue_review_task=queue_review_task, persist_queue_snapshot=persist_queue_snapshot,
            safe_restart=safe_restart, kill_workers=kill_workers, spawn_workers=spawn_workers,
            sort_pending=sort_pending, consciousness=_consciousness,
        )
    except Exception as exc:
        SUPERVISOR_ERROR = f"Supervisor init failed: {exc}"
        log.critical("Supervisor initialization failed", exc_info=True)
        SUPERVISOR_READY.set()
        if CHAT_BRIDGE:
            CHAT_BRIDGE.send_message(1, f"\u26a0\ufe0f Supervisor failed to start: {exc}")
        return

    SUPERVISOR_READY.set()

    MAX_CRASH_RETRIES = 3
    offset = 0
    while True:
        try:
            rotate_chat_log_if_needed(DATA_DIR)
            ensure_workers_healthy()

            event_q = get_event_q()
            while True:
                try:
                    evt = event_q.get_nowait()
                except _queue_mod.Empty:
                    break
                dispatch_event(evt, _event_ctx)

            enforce_task_timeouts()
            enqueue_evolution_task_if_needed()
            assign_tasks()
            persist_queue_snapshot(reason="main_loop")

            updates = CHAT_BRIDGE.get_updates(offset=offset, timeout=1)
            for upd in updates:
                offset = int(upd["update_id"]) + 1
                msg = upd.get("message") or {}
                if not msg:
                    continue

                chat_id = 1
                user_id = 1
                text = str(msg.get("text") or "")
                now_iso = datetime.now(timezone.utc).isoformat()

                st = load_state()
                if st.get("owner_id") is None:
                    st["owner_id"] = user_id
                    st["owner_chat_id"] = chat_id

                from supervisor.telegram import log_chat
                log_chat("in", chat_id, user_id, text)
                st["last_owner_message_at"] = now_iso
                save_state(st)

                if not text:
                    continue

                lowered = text.strip().lower()
                if lowered.startswith("/panic"):
                    send_with_budget(chat_id, "\U0001f6d1 PANIC: stopping everything now.")
                    kill_workers(force=True)
                    release_pid_lock()
                    try:
                        import signal
                        os.killpg(os.getpgid(os.getpid()), signal.SIGKILL)
                    except Exception:
                        os._exit(1)
                elif lowered.startswith("/restart"):
                    send_with_budget(chat_id, "\u267b\ufe0f Restarting (soft).")
                    ok, restart_msg = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
                    if not ok:
                        send_with_budget(chat_id, f"\u26a0\ufe0f Restart cancelled: {restart_msg}")
                        continue
                    kill_workers()
                    if getattr(sys, 'frozen', False):
                        os.execv(sys.executable, [sys.executable])
                    else:
                        os.execv(sys.executable, [sys.executable, __file__])
                elif lowered.startswith("/review"):
                    queue_review_task(reason="owner:/review", force=True)
                    continue
                elif lowered.startswith("/evolve"):
                    parts = lowered.split()
                    action = parts[1] if len(parts) > 1 else "on"
                    turn_on = action not in ("off", "stop", "0")
                    st2 = load_state()
                    st2["evolution_mode_enabled"] = bool(turn_on)
                    save_state(st2)
                    if not turn_on:
                        PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
                        sort_pending()
                        persist_queue_snapshot(reason="evolve_off")
                    state_str = "ON" if turn_on else "OFF"
                    send_with_budget(chat_id, f"\U0001f9ec Evolution: {state_str}")
                    continue
                elif lowered.startswith("/bg"):
                    parts = lowered.split()
                    action = parts[1] if len(parts) > 1 else "status"
                    if action in ("start", "on", "1"):
                        result = _consciousness.start()
                        send_with_budget(chat_id, f"\U0001f9e0 {result}")
                    elif action in ("stop", "off", "0"):
                        result = _consciousness.stop()
                        send_with_budget(chat_id, f"\U0001f9e0 {result}")
                    else:
                        bg_status = "running" if _consciousness.is_running else "stopped"
                        send_with_budget(chat_id, f"\U0001f9e0 Background consciousness: {bg_status}")
                    continue
                elif lowered.startswith("/status"):
                    from supervisor.state import status_text
                    status = status_text(WORKERS, PENDING, RUNNING, soft_timeout, hard_timeout)
                    send_with_budget(chat_id, status, force_budget=True)
                    continue

                _consciousness.inject_observation(f"Owner message: {text[:100]}")
                agent = _get_chat_agent()

                if agent._busy:
                    agent.inject_message(text)
                else:
                    _consciousness.pause()
                    def _run_task_and_resume(cid, txt):
                        try:
                            handle_chat_direct(cid, txt, None)
                        finally:
                            _consciousness.resume()
                    _t = threading.Thread(
                        target=_run_task_and_resume, args=(chat_id, text), daemon=True,
                    )
                    _t.start()

            _SUPERVISOR_CRASH_COUNT = 0
            time.sleep(0.5)

        except Exception as exc:
            _SUPERVISOR_CRASH_COUNT += 1
            SUPERVISOR_ERROR = repr(exc)
            log.error("Supervisor loop crash #%d: %s", _SUPERVISOR_CRASH_COUNT, exc, exc_info=True)

            try:
                save_state(load_state())
            except Exception:
                pass

            if CHAT_BRIDGE:
                try:
                    CHAT_BRIDGE.send_message(1, f"\u26a0\ufe0f Supervisor error (attempt {_SUPERVISOR_CRASH_COUNT}/{MAX_CRASH_RETRIES}): {exc}")
                except Exception:
                    pass

            if _SUPERVISOR_CRASH_COUNT >= MAX_CRASH_RETRIES:
                SUPERVISOR_ERROR = f"Supervisor crashed {MAX_CRASH_RETRIES} times. Last error: {exc}"
                log.critical("Supervisor exceeded max retries. Stopping.")
                if CHAT_BRIDGE:
                    try:
                        CHAT_BRIDGE.send_message(1, f"\U0001f6d1 Supervisor stopped after {MAX_CRASH_RETRIES} crashes. Please restart the app.")
                    except Exception:
                        pass
                return

            backoff = min(30, 2 ** _SUPERVISOR_CRASH_COUNT)
            log.info("Retrying supervisor in %ds...", backoff)
            time.sleep(backoff)


# ---------------------------------------------------------------------------
# Flet Application
# ---------------------------------------------------------------------------
from ui.components import ChatBubble, status_card
from ui.log_format import LOG_CATEGORIES, categorize_event, format_log_entry
from ui.notifications import notify_macos


def _logo(size: int = 48, radius: int = 12) -> ft.Container:
    return ft.Container(
        width=size, height=size, border_radius=radius,
        clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
        content=ft.Image(src="logo.jpg", width=size, height=size, fit=ft.ImageFit.COVER),
    )


def main(page: ft.Page):
    page.title = f"Ouroboros v{APP_VERSION}"
    page.theme_mode = ft.ThemeMode.DARK
    page.window.width = 1100
    page.window.height = 750
    page.padding = 0
    page.spacing = 0

    settings = load_settings()

    # CHAT PAGE
    chat_list = ft.ListView(auto_scroll=True, spacing=4, padding=20, expand=True)
    chat_input = ft.TextField(hint_text="Message Ouroboros...", border_radius=24, filled=True, expand=True, shift_enter=True)

    def send_message(_e):
        text = chat_input.value
        if not text or not text.strip():
            return
        chat_input.value = ""
        chat_list.controls.append(ChatBubble(text, is_user=True))
        page.update()
        if CHAT_BRIDGE:
            CHAT_BRIDGE.ui_send(text)
        else:
            chat_list.controls.append(ChatBubble(
                "Agent is not running. Please add your OpenRouter API key in Settings and click Save.",
                is_user=False,
            ))
            page.update()

    chat_input.on_submit = send_message
    send_btn = ft.IconButton(icon=ft.Icons.SEND_ROUNDED, icon_color=ft.Colors.BLUE_400, on_click=send_message)

    status_label = ft.Text("Online", size=11, color=ft.Colors.GREEN_200)
    status_badge = ft.Container(bgcolor=ft.Colors.GREEN_900, border_radius=12, padding=ft.padding.symmetric(horizontal=10, vertical=4), content=status_label)

    def _update_status():
        if SUPERVISOR_ERROR:
            status_label.value = "Error"
            status_label.color = ft.Colors.RED_200
            status_badge.bgcolor = ft.Colors.RED_900
        elif CHAT_BRIDGE is not None and SUPERVISOR_READY.is_set():
            status_label.value = "Online"
            status_label.color = ft.Colors.GREEN_200
            status_badge.bgcolor = ft.Colors.GREEN_900
        elif CHAT_BRIDGE is not None:
            status_label.value = "Starting..."
            status_label.color = ft.Colors.AMBER_200
            status_badge.bgcolor = ft.Colors.AMBER_900
        else:
            status_label.value = "Not configured"
            status_label.color = ft.Colors.AMBER_200
            status_badge.bgcolor = ft.Colors.AMBER_900

    _update_status()
    if CHAT_BRIDGE is None:
        chat_list.controls.append(ChatBubble("Welcome! To get started, go to the Settings tab and enter your OpenRouter API key, then click Save.", is_user=False))

    chat_page = ft.Column(expand=True, controls=[
        ft.Container(bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE), padding=ft.padding.symmetric(horizontal=20, vertical=12),
                     content=ft.Row([ft.Icon(ft.Icons.SMART_TOY_OUTLINED, color=ft.Colors.TEAL_200), ft.Text("Chat", size=16, weight=ft.FontWeight.BOLD), ft.Container(expand=True), status_badge])),
        chat_list,
        ft.Container(padding=ft.padding.only(left=16, right=16, bottom=16, top=8),
                     content=ft.Row(controls=[chat_input, send_btn], vertical_alignment=ft.CrossAxisAlignment.CENTER)),
    ])

    # DASHBOARD PAGE
    uptime_text = ft.Text("0s", size=22, weight=ft.FontWeight.BOLD)
    workers_text = ft.Text("...", size=16, weight=ft.FontWeight.BOLD)
    workers_bar = ft.ProgressBar(value=0.0, color=ft.Colors.TEAL_400, width=200)
    budget_text = ft.Text("...", size=16, weight=ft.FontWeight.BOLD)
    budget_bar = ft.ProgressBar(value=0.0, color=ft.Colors.AMBER_400, width=200)
    branch_text = ft.Text("ouroboros", size=16, weight=ft.FontWeight.BOLD, color=ft.Colors.GREEN_300)
    evo_switch = ft.Switch(label="Evolution Mode", value=True)
    bg_switch = ft.Switch(label="Background Consciousness", value=False)
    consciousness_dot = ft.Container(width=10, height=10, border_radius=5, bgcolor=ft.Colors.TEAL_400, animate=ft.Animation(1000, ft.AnimationCurve.EASE_IN_OUT))

    def on_evo_change(e):
        if CHAT_BRIDGE:
            CHAT_BRIDGE.ui_send(f"/evolve {'start' if evo_switch.value else 'stop'}")
    def on_bg_change(e):
        if CHAT_BRIDGE:
            CHAT_BRIDGE.ui_send(f"/bg {'start' if bg_switch.value else 'stop'}")
    evo_switch.on_change = on_evo_change
    bg_switch.on_change = on_bg_change

    def on_review_click(_e):
        if CHAT_BRIDGE: CHAT_BRIDGE.ui_send("/review")
        page.open(ft.SnackBar(ft.Text("Review queued"), duration=2000)); page.update()
    def on_restart_click(_e):
        if CHAT_BRIDGE: CHAT_BRIDGE.ui_send("/restart")
        page.open(ft.SnackBar(ft.Text("Restart sent"), duration=2000)); page.update()
    def on_panic_click(_e):
        def _close(_e2): dialog.open = False; page.update()
        def _confirm(_e2):
            if CHAT_BRIDGE: CHAT_BRIDGE.ui_send("/panic")
            dialog.open = False; page.update()
        dialog = ft.AlertDialog(modal=True, title=ft.Text("PANIC STOP"), content=ft.Text("Kill all workers immediately?"),
                                actions=[ft.TextButton("Cancel", on_click=_close), ft.TextButton("PANIC", on_click=_confirm, style=ft.ButtonStyle(color=ft.Colors.RED_400))])
        page.open(dialog); page.update()

    # --- Version Management Panel ---
    from ui.version_panel import create_version_panel
    version_panel, _ = create_version_panel(page, settings, REPO_DIR)

    dashboard_page = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, controls=[
        ft.Container(bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE), padding=ft.padding.symmetric(horizontal=20, vertical=12),
                     content=ft.Row([ft.Icon(ft.Icons.DASHBOARD_OUTLINED, color=ft.Colors.TEAL_200), ft.Text("Dashboard", size=16, weight=ft.FontWeight.BOLD), ft.Container(expand=True)])),
        ft.Container(padding=20, content=ft.Column(spacing=20, controls=[
            ft.Text(f"Ouroboros v{APP_VERSION}", size=24, weight=ft.FontWeight.BOLD),
            ft.ResponsiveRow([
                ft.Container(col={"sm": 6, "md": 3}, content=status_card("UPTIME", uptime_text, ft.Icons.TIMER_OUTLINED)),
                ft.Container(col={"sm": 6, "md": 3}, content=status_card("WORKERS", ft.Column([workers_text, workers_bar], spacing=6), ft.Icons.MEMORY)),
                ft.Container(col={"sm": 6, "md": 3}, content=status_card("BUDGET", ft.Column([budget_text, budget_bar], spacing=6), ft.Icons.ATTACH_MONEY, icon_color=ft.Colors.AMBER_400)),
                ft.Container(col={"sm": 6, "md": 3}, content=status_card("BRANCH", ft.Row([ft.Container(width=8, height=8, border_radius=4, bgcolor=ft.Colors.GREEN_400), branch_text], spacing=8), ft.Icons.ACCOUNT_TREE_OUTLINED, icon_color=ft.Colors.GREEN_300)),
            ]),
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.Text("Controls", size=14, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE54),
            ft.Row([evo_switch, ft.Container(width=20), bg_switch, consciousness_dot], wrap=True),
            ft.Row([
                ft.ElevatedButton("Force Review", icon=ft.Icons.RATE_REVIEW_OUTLINED, on_click=on_review_click),
                ft.ElevatedButton("Restart Agent", icon=ft.Icons.REFRESH, on_click=on_restart_click),
                ft.ElevatedButton("Panic Stop", icon=ft.Icons.DANGEROUS_OUTLINED, color=ft.Colors.RED_300, on_click=on_panic_click),
            ], wrap=True),
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            version_panel,
        ])),
    ])

    # SETTINGS PAGE
    api_key_field = ft.TextField(label="OpenRouter API Key", value=settings.get("OPENROUTER_API_KEY", ""), password=True, can_reveal_password=True, width=500)
    openai_key_field = ft.TextField(label="OpenAI API Key (optional)", value=settings.get("OPENAI_API_KEY", ""), password=True, can_reveal_password=True, width=500)
    anthropic_key_field = ft.TextField(label="Anthropic API Key (optional)", value=settings.get("ANTHROPIC_API_KEY", ""), password=True, can_reveal_password=True, width=500)
    model_main = ft.TextField(label="Main Model", value=settings.get("OUROBOROS_MODEL", ""), width=300)
    model_code = ft.TextField(label="Code Model", value=settings.get("OUROBOROS_MODEL_CODE", ""), width=300)
    model_light = ft.TextField(label="Light Model", value=settings.get("OUROBOROS_MODEL_LIGHT", ""), width=300)
    model_fallback = ft.TextField(label="Fallback Model", value=settings.get("OUROBOROS_MODEL_FALLBACK", "google/gemini-2.5-flash"), width=300, hint_text="Single model for when others fail")
    model_websearch = ft.TextField(label="Web Search Model (OpenAI)", value=settings.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"), width=300, hint_text="gpt-5, gpt-4o, gpt-4o-mini")
    github_token_field = ft.TextField(label="GitHub Token", value=settings.get("GITHUB_TOKEN", ""), password=True, can_reveal_password=True, width=500, hint_text="ghp_... or github_pat_...")
    github_repo_field = ft.TextField(label="GitHub Repo", value=settings.get("GITHUB_REPO", ""), width=500, hint_text="owner/repo-name")
    workers_slider = ft.Slider(min=1, max=10, value=int(settings.get("OUROBOROS_MAX_WORKERS", 5)), divisions=9, label="{value}", width=350)
    budget_field = ft.TextField(label="Total Budget ($)", value=str(settings.get("TOTAL_BUDGET", 10.0)), width=200)
    soft_timeout_slider = ft.Slider(min=60, max=3600, value=int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600)), divisions=59, label="{value}s", width=350)
    hard_timeout_slider = ft.Slider(min=120, max=7200, value=int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800)), divisions=59, label="{value}s", width=350)
    bg_max_rounds_slider = ft.Slider(min=1, max=20, value=int(settings.get("OUROBOROS_BG_MAX_ROUNDS", 5)), divisions=19, label="{value}", width=350)
    bg_wakeup_min_field = ft.TextField(label="BG Wakeup Min (s)", value=str(settings.get("OUROBOROS_BG_WAKEUP_MIN", 30)), width=150)
    bg_wakeup_max_field = ft.TextField(label="BG Wakeup Max (s)", value=str(settings.get("OUROBOROS_BG_WAKEUP_MAX", 7200)), width=150)
    evo_cost_field = ft.TextField(label="Evo Cost Threshold ($)", value=str(settings.get("OUROBOROS_EVO_COST_THRESHOLD", 0.10)), width=150)

    def _on_reset_click(_e):
        def _close(_e2): dialog.open = False; page.update()
        def _confirm(_e2):
            dialog.open = False; page.update()
            try:
                from supervisor.workers import kill_workers; kill_workers()
            except Exception: pass
            shutil.rmtree(DATA_DIR, ignore_errors=True); shutil.rmtree(REPO_DIR, ignore_errors=True)
            page.open(ft.SnackBar(ft.Text("Data cleared. Restarting..."), duration=2000)); page.update()
            time.sleep(1)
            if getattr(sys, 'frozen', False): os.execv(sys.executable, [sys.executable])
            else: os.execv(sys.executable, [sys.executable, __file__])
        dialog = ft.AlertDialog(modal=True, title=ft.Text("Reset All Data"), content=ft.Text("Delete all data, logs, memory, and the local repo?"),
                                actions=[ft.TextButton("Cancel", on_click=_close), ft.TextButton("DELETE EVERYTHING", on_click=_confirm, style=ft.ButtonStyle(color=ft.Colors.RED_400))])
        page.open(dialog); page.update()

    def _num(val, fallback, as_float=False):
        try: return float(val) if as_float else int(float(val))
        except (ValueError, TypeError): return fallback

    def on_save(_e):
        try:
            settings.update({
                "OPENROUTER_API_KEY": api_key_field.value, "OPENAI_API_KEY": openai_key_field.value,
                "ANTHROPIC_API_KEY": anthropic_key_field.value, "OUROBOROS_MODEL": model_main.value,
                "OUROBOROS_MODEL_CODE": model_code.value, "OUROBOROS_MODEL_LIGHT": model_light.value,
                "OUROBOROS_MODEL_FALLBACK": model_fallback.value,
                "OUROBOROS_WEBSEARCH_MODEL": model_websearch.value,
                "GITHUB_TOKEN": github_token_field.value.strip(), "GITHUB_REPO": github_repo_field.value.strip(),
                "OUROBOROS_MAX_WORKERS": _num(workers_slider.value, 5),
                "TOTAL_BUDGET": _num(budget_field.value, 10.0, as_float=True),
                "OUROBOROS_SOFT_TIMEOUT_SEC": _num(soft_timeout_slider.value, 600),
                "OUROBOROS_HARD_TIMEOUT_SEC": _num(hard_timeout_slider.value, 1800),
                "OUROBOROS_BG_MAX_ROUNDS": _num(bg_max_rounds_slider.value, 5),
                "OUROBOROS_BG_WAKEUP_MIN": _num(bg_wakeup_min_field.value, 30),
                "OUROBOROS_BG_WAKEUP_MAX": _num(bg_wakeup_max_field.value, 7200),
                "OUROBOROS_EVO_COST_THRESHOLD": _num(evo_cost_field.value, 0.10, as_float=True),
            })
            save_settings(settings)
        except Exception as exc:
            page.open(ft.SnackBar(ft.Text(f"Save failed: {exc}"), duration=4000))
            page.update()
            return
        for k in ("OUROBOROS_BG_MAX_ROUNDS", "OUROBOROS_BG_WAKEUP_MIN", "OUROBOROS_BG_WAKEUP_MAX",
                   "OUROBOROS_EVO_COST_THRESHOLD", "OUROBOROS_WEBSEARCH_MODEL", "OUROBOROS_MODEL_FALLBACK"):
            os.environ[k] = str(settings[k])
        for gk in ("GITHUB_TOKEN", "GITHUB_REPO"):
            if settings.get(gk):
                os.environ[gk] = settings[gk]
            else:
                os.environ.pop(gk, None)
        if CHAT_BRIDGE is None and settings.get("OPENROUTER_API_KEY"):
            started = start_supervisor_if_configured()
            if started:
                async def _wait():
                    for _ in range(20):
                        if SUPERVISOR_READY.is_set():
                            break
                        await asyncio.sleep(0.5)
                    _update_status()
                    page.open(ft.SnackBar(ft.Text("Settings saved. Agent is starting..."), duration=3000))
                    page.update()
                page.run_task(_wait)
            else:
                page.open(ft.SnackBar(ft.Text("Settings saved but agent failed to start."), duration=4000))
        else:
            page.open(ft.SnackBar(ft.Text("Settings saved. Restart for changes to take effect."), duration=3000))
        page.update()

    settings_page = ft.Column(expand=True, scroll=ft.ScrollMode.AUTO, controls=[
        ft.Container(bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE), padding=ft.padding.symmetric(horizontal=20, vertical=12),
                     content=ft.Row([ft.Icon(ft.Icons.SETTINGS_OUTLINED, color=ft.Colors.TEAL_200), ft.Text("Settings", size=16, weight=ft.FontWeight.BOLD)])),
        ft.Container(padding=20, content=ft.Column(spacing=24, controls=[
            ft.Text("API Keys", size=18, weight=ft.FontWeight.BOLD),
            api_key_field, openai_key_field, anthropic_key_field,
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.Text("Models", size=18, weight=ft.FontWeight.BOLD),
            ft.Row([model_main, model_code, model_light], wrap=True, spacing=12),
            ft.Row([model_fallback, model_websearch], wrap=True, spacing=12),
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.Text("GitHub", size=18, weight=ft.FontWeight.BOLD),
            ft.Text("Connect a GitHub repo to push Ouroboros versions remotely.", size=12, color=ft.Colors.WHITE38),
            github_token_field, github_repo_field,
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.Text("Runtime", size=18, weight=ft.FontWeight.BOLD),
            ft.Row([
                ft.Column([ft.Text("Max Workers", size=13, color=ft.Colors.WHITE54), workers_slider]),
                ft.Column([ft.Text("Soft Timeout", size=13, color=ft.Colors.WHITE54), soft_timeout_slider]),
                ft.Column([ft.Text("Hard Timeout", size=13, color=ft.Colors.WHITE54), hard_timeout_slider]),
            ], wrap=True, spacing=24),
            budget_field,
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.Text("Consciousness & Evolution", size=18, weight=ft.FontWeight.BOLD),
            ft.Row([ft.Column([ft.Text("BG Max Rounds", size=13, color=ft.Colors.WHITE54), bg_max_rounds_slider])], wrap=True, spacing=24),
            ft.Row([bg_wakeup_min_field, bg_wakeup_max_field, evo_cost_field], wrap=True, spacing=16),
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.FilledButton("Save", icon=ft.Icons.SAVE, on_click=on_save),
            ft.Divider(height=1, color=ft.Colors.WHITE10),
            ft.Text("Danger Zone", size=18, weight=ft.FontWeight.BOLD, color=ft.Colors.RED_300),
            ft.ElevatedButton("Reset All Data", icon=ft.Icons.DELETE_FOREVER_OUTLINED, color=ft.Colors.RED_300, on_click=lambda _e: _on_reset_click(_e)),
        ])),
    ])

    # LOGS PAGE
    log_list_view = ft.ListView(auto_scroll=True, spacing=1, padding=10, expand=True)
    log_controls: list = log_list_view.controls
    LOG_BUFFER_MAX = 500
    active_filters: dict = {"tools": True, "llm": True, "errors": True, "tasks": True, "system": False, "consciousness": False}

    def _make_chip(key):
        cat = LOG_CATEGORIES[key]
        return ft.Chip(label=ft.Text(cat["label"], size=12), selected=active_filters[key], selected_color=cat["color"],
                       on_select=lambda e, k=key: _toggle_filter(k, e))
    def _toggle_filter(key, e):
        active_filters[key] = e.control.selected; _rebuild(); page.update()

    _all_log_events: list = []
    def _rebuild():
        log_controls.clear()
        for evt in _all_log_events:
            cat_key, _ = categorize_event(evt)
            if active_filters.get(cat_key, True):
                log_controls.append(format_log_entry(evt))
    def _on_clear(_e): _all_log_events.clear(); log_controls.clear(); page.update()
    def _on_export(_e):
        try:
            export_dir = pathlib.Path.home() / "Desktop" / "Ouroboros_Logs"
            export_dir.mkdir(parents=True, exist_ok=True)
            log_src = DATA_DIR / "logs"
            if log_src.exists():
                for f in log_src.iterdir():
                    if f.suffix in (".log", ".jsonl"):
                        shutil.copy2(f, export_dir / f.name)
            page.open(ft.SnackBar(ft.Text(f"Logs exported to {export_dir}"), duration=4000))
        except Exception as exc:
            page.open(ft.SnackBar(ft.Text(f"Export failed: {exc}"), duration=4000))
        page.update()

    logs_page = ft.Column(expand=True, controls=[
        ft.Container(bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE), padding=ft.padding.symmetric(horizontal=20, vertical=12),
                     content=ft.Row([ft.Icon(ft.Icons.TERMINAL_OUTLINED, color=ft.Colors.TEAL_200), ft.Text("Logs", size=16, weight=ft.FontWeight.BOLD),
                                     ft.Container(expand=True), ft.TextButton("Export", icon=ft.Icons.DOWNLOAD_OUTLINED, on_click=_on_export),
                                     ft.TextButton("Clear", icon=ft.Icons.DELETE_OUTLINE, on_click=_on_clear)])),
        ft.Container(padding=ft.padding.symmetric(horizontal=20, vertical=8), content=ft.Row(controls=[_make_chip(k) for k in LOG_CATEGORIES], spacing=8, wrap=True)),
        ft.Container(expand=True, padding=ft.padding.only(left=12, right=12, bottom=12), bgcolor=ft.Colors.with_opacity(0.04, ft.Colors.WHITE), border_radius=8, content=log_list_view),
    ])

    # NAVIGATION
    pages = [chat_page, dashboard_page, settings_page, logs_page]
    content_area = ft.Container(expand=True, content=chat_page)
    def on_nav_change(e): content_area.content = pages[e.control.selected_index]; page.update()

    nav_rail = ft.NavigationRail(
        selected_index=0, label_type=ft.NavigationRailLabelType.ALL, min_width=80, group_alignment=-0.9, on_change=on_nav_change,
        leading=ft.Container(padding=ft.padding.only(top=10, bottom=6), content=ft.Column(horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=2,
                             controls=[_logo(36, 8), ft.Text(f"v{APP_VERSION}", size=9, color=ft.Colors.WHITE38)])),
        destinations=[
            ft.NavigationRailDestination(icon=ft.Icons.CHAT_OUTLINED, selected_icon=ft.Icons.CHAT, label="Chat"),
            ft.NavigationRailDestination(icon=ft.Icons.DASHBOARD_OUTLINED, selected_icon=ft.Icons.DASHBOARD, label="Dashboard"),
            ft.NavigationRailDestination(icon=ft.Icons.SETTINGS_OUTLINED, selected_icon=ft.Icons.SETTINGS, label="Settings"),
            ft.NavigationRailDestination(icon=ft.Icons.TERMINAL_OUTLINED, selected_icon=ft.Icons.TERMINAL, label="Logs"),
        ],
    )
    page.add(ft.Row(expand=True, spacing=0, controls=[nav_rail, ft.VerticalDivider(width=1, color=ft.Colors.WHITE10), content_area]))

    # BACKGROUND TASKS
    async def process_chat_inbox():
        while True:
            if CHAT_BRIDGE:
                msg = CHAT_BRIDGE.ui_receive(timeout=0.1)
                if msg:
                    if msg["type"] == "text":
                        chat_list.controls.append(ChatBubble(msg["content"], is_user=False, markdown=msg["markdown"]))
                        if not page.window.focused:
                            notify_macos("Ouroboros", msg["content"][:100])
                    try: page.update()
                    except Exception: pass
            await asyncio.sleep(0.1)

    async def update_dashboard():
        while True:
            _update_status()
            elapsed = int(time.time() - APP_START)
            h, rem = divmod(elapsed, 3600); m, s = divmod(rem, 60)
            uptime_text.value = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
            try:
                from supervisor.state import load_state as _ls
                st = _ls()
                from supervisor.workers import WORKERS as _W, PENDING as _P, RUNNING as _R
                alive = sum(1 for w in _W.values() if w.proc.is_alive()); total = len(_W)
                workers_text.value = f"{alive} / {total} active"
                workers_bar.value = (alive / total) if total else 0
                spent = float(st.get("spent_usd") or 0.0); limit = float(settings.get("TOTAL_BUDGET", 10.0))
                budget_text.value = f"${spent:.2f} / ${limit:.2f}"
                budget_bar.value = min(1.0, (spent / limit)) if limit else 0
                branch_text.value = st.get("current_branch", "ouroboros")
                evo_switch.value = bool(st.get("evolution_mode_enabled"))
            except Exception: pass
            try: page.update()
            except Exception: break
            await asyncio.sleep(2)

    async def poll_logs():
        while True:
            if CHAT_BRIDGE:
                batch = CHAT_BRIDGE.ui_poll_logs()
                if batch:
                    for evt in batch:
                        _all_log_events.append(evt)
                        cat_key, _ = categorize_event(evt)
                        if active_filters.get(cat_key, True):
                            log_controls.append(format_log_entry(evt))
                    if len(_all_log_events) > LOG_BUFFER_MAX:
                        _all_log_events[:] = _all_log_events[-LOG_BUFFER_MAX:]
                        _rebuild()
                    try: page.update()
                    except Exception: pass
            await asyncio.sleep(0.15)

    def _on_window_event(e):
        if e.data == "close":
            log.info("Window closing \u2014 graceful shutdown.")
            try:
                from supervisor.state import load_state as _ls2, save_state as _ss2; _ss2(_ls2())
            except Exception: pass
            try:
                from supervisor.workers import kill_workers as _kw; _kw()
            except Exception: pass
            release_pid_lock()
            for h in logging.getLogger().handlers: h.flush()
            page.window.destroy()

    page.window.on_event = _on_window_event
    page.window.prevent_close = True

    page.run_task(process_chat_inbox)
    page.run_task(update_dashboard)
    page.run_task(poll_logs)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def check_prerequisites():
    """Verify that external dependencies (git) are available before bootstrap."""
    if shutil.which("git"):
        return

    log.warning("Git not found. Prompting user to install.")

    def _prereq_page(page: ft.Page):
        page.title = "Ouroboros \u2014 Setup Required"
        page.theme_mode = ft.ThemeMode.DARK
        page.window.width = 520
        page.window.height = 340
        page.padding = 30
        status_text = ft.Text("", size=13, color=ft.Colors.AMBER_300)

        def on_install(_e):
            status_text.value = "Installing... A system dialog may appear."
            page.update()
            subprocess.Popen(["xcode-select", "--install"])
            status_text.value = "Waiting for Git to become available..."
            page.update()
            def _poll():
                import time as _t
                for _ in range(300):
                    _t.sleep(3)
                    if shutil.which("git"):
                        status_text.value = "Git installed! Starting Ouroboros..."
                        page.update(); _t.sleep(1); page.window.close(); return
                status_text.value = "Timed out. Please install Git manually and restart."
                page.update()
            threading.Thread(target=_poll, daemon=True).start()

        page.add(ft.Column(spacing=20, horizontal_alignment=ft.CrossAxisAlignment.CENTER, controls=[
            _logo(64, 14),
            ft.Text("Git is required", size=20, weight=ft.FontWeight.BOLD),
            ft.Text("Ouroboros needs Git to manage its local repository.\nClick below to install Apple's Command Line Tools (includes Git).",
                     size=13, color=ft.Colors.WHITE70, text_align=ft.TextAlign.CENTER),
            ft.FilledButton("Install Git (Xcode CLI Tools)", on_click=on_install, icon=ft.Icons.DOWNLOAD),
            status_text,
        ]))

    ft.app(target=_prereq_page, assets_dir=ASSETS_DIR)
    if not shutil.which("git"):
        log.error("Git still not available after prereq dialog. Exiting.")
        sys.exit(1)


def start_supervisor_if_configured() -> bool:
    global SUPERVISOR_THREAD, CHAT_BRIDGE
    settings = load_settings()
    log.info(f"Settings path: {SETTINGS_PATH} (exists={SETTINGS_PATH.exists()})")
    if not settings.get("OPENROUTER_API_KEY"):
        log.warning("OPENROUTER_API_KEY not set. Supervisor will not start until configured.")
        return False
    if SUPERVISOR_THREAD and SUPERVISOR_THREAD.is_alive():
        log.info("Supervisor already running, skipping.")
        return True
    log.info("Starting Supervisor thread...")
    t = threading.Thread(target=run_supervisor, args=(settings,), daemon=True)
    t.start()
    SUPERVISOR_THREAD = t
    return True


if __name__ == "__main__":
    from multiprocessing import freeze_support
    freeze_support()

    if sys.platform == "darwin":
        try:
            _shell_path = subprocess.check_output(["/bin/bash", "-l", "-c", "echo $PATH"], text=True, timeout=5).strip()
            if _shell_path:
                os.environ["PATH"] = _shell_path
        except Exception:
            pass

    if not acquire_pid_lock():
        log.error("Another instance already running.")
        def _dup(p: ft.Page):
            p.title = "Ouroboros"; p.theme_mode = ft.ThemeMode.DARK
            p.window.width = 420; p.window.height = 200; p.padding = 30
            p.add(ft.Column(spacing=16, horizontal_alignment=ft.CrossAxisAlignment.CENTER, controls=[
                ft.Text("Ouroboros is already running", size=18, weight=ft.FontWeight.BOLD),
                ft.Text("Only one instance can run at a time.", size=13, color=ft.Colors.WHITE70),
                ft.FilledButton("OK", on_click=lambda _: p.window.destroy())]))
        ft.app(target=_dup, assets_dir=ASSETS_DIR); sys.exit(0)
    import atexit; atexit.register(release_pid_lock)

    check_prerequisites()
    bootstrap_repo()

    settings = load_settings()
    if not settings.get("OPENROUTER_API_KEY"):
        log.info("No API key configured. Launching first-run wizard.")
        from ui.first_run import run_first_run_wizard
        if not run_first_run_wizard(_SETTINGS_DEFAULTS, save_settings, assets_dir=ASSETS_DIR):
            log.info("Wizard cancelled. Launching main UI anyway.")

    start_supervisor_if_configured()

    log.info("Starting Flet UI...")
    ft.app(target=main, assets_dir=ASSETS_DIR)

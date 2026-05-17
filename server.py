"""
Ouroboros Agent Server — Self-editable entry point.

This file lives in REPO_DIR and can be modified by the agent.
It runs as a subprocess of the launcher, serving the web UI and
coordinating the supervisor/worker system.

Starlette + uvicorn on configurable host:port (default localhost:8765; non-loopback binding supported via OUROBOROS_SERVER_HOST).
"""

import asyncio
import logging

import os
import pathlib
import sys
import threading
import time
import uuid
from ouroboros.utils import utc_now_iso
from typing import Any, Dict, Optional

from starlette.applications import Starlette
from starlette.routing import Route, Mount

import uvicorn

from ouroboros.server_control import (
    execute_panic_stop as _execute_panic_stop_impl,
    restart_current_process as _restart_current_process_impl,
)
from ouroboros.server_auth import (
    NetworkAuthGate,
    get_network_auth_startup_warning,
    validate_network_auth_configuration,
)
from ouroboros.server_entrypoint import find_free_port, parse_server_args, write_port_file
from ouroboros.server_web import NoCacheStaticFiles, make_index_page, make_pwa_routes, resolve_web_dir
from ouroboros.gateway import collect_routes
from ouroboros.gateway import settings as _gateway_settings
from ouroboros.gateway.ws import (
    broadcast_ws,
    broadcast_ws_sync,
    close_all_ws,
    has_ws_clients as _has_ws_clients,
    set_event_loop as _set_ws_event_loop,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", pathlib.Path(__file__).parent))
DATA_DIR = pathlib.Path(os.environ.get("OUROBOROS_DATA_DIR",
    pathlib.Path.home() / "Ouroboros" / "data"))
DEFAULT_HOST = os.environ.get("OUROBOROS_SERVER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("OUROBOROS_SERVER_PORT", "8765"))
PORT_FILE = DATA_DIR / "state" / "server_port"

sys.path.insert(0, str(REPO_DIR))
if not os.environ.get("OUROBOROS_AGENT_PYTHON"):
    _agent_python = sys.executable
    if isinstance(_agent_python, str) and _agent_python:
        os.environ["OUROBOROS_AGENT_PYTHON"] = _agent_python

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_pytest_default_real_data_dir = (
    "pytest" in sys.modules
    and not os.environ.get("OUROBOROS_DATA_DIR")
    and DATA_DIR == pathlib.Path.home() / "Ouroboros" / "data"
)
if _pytest_default_real_data_dir:
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=[logging.StreamHandler()])
else:
    _log_dir = DATA_DIR / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    _file_handler = RotatingFileHandler(
        _log_dir / "server.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    _file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=[_file_handler, logging.StreamHandler()])
log = logging.getLogger("server")

# ---------------------------------------------------------------------------
# Restart signal
# ---------------------------------------------------------------------------
RESTART_EXIT_CODE = 42
PANIC_EXIT_CODE = 99
_restart_requested = threading.Event()
_LAUNCHER_MANAGED = str(os.environ.get("OUROBOROS_MANAGED_BY_LAUNCHER", "") or "").strip() == "1"

# ---------------------------------------------------------------------------
# Runtime network binding (captured in main() from parse_server_args)
# Used by /api/settings to expose a LAN reachability hint to the Settings UI.
# ---------------------------------------------------------------------------
_BIND_HOST = DEFAULT_HOST


def _restart_current_process(host: str, port: int) -> None:
    _restart_current_process_impl(host, port, repo_dir=REPO_DIR, log=log)


# ---------------------------------------------------------------------------
# Settings (single source of truth: ouroboros.config)
# ---------------------------------------------------------------------------
from ouroboros.config import (
    SETTINGS_DEFAULTS as _SETTINGS_DEFAULTS,
    load_settings, save_settings, apply_settings_to_env as _apply_settings_to_env,
)
from ouroboros.server_runtime import (
    apply_runtime_provider_defaults,
    has_local_routing,
    has_supervisor_provider,
    setup_remote_if_configured,
    ws_heartbeat_loop,
)
from ouroboros.onboarding_wizard import build_onboarding_html


# ---------------------------------------------------------------------------
# Supervisor integration
# ---------------------------------------------------------------------------
_supervisor_ready = threading.Event()
_supervisor_error: Optional[str] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None
_supervisor_thread: Optional[threading.Thread] = None
_consciousness: Any = None


def _describe_bg_consciousness_state(requested_enabled: bool) -> dict:
    snapshot = _consciousness.status_snapshot() if _consciousness else {}
    running = bool(snapshot.get("running"))
    paused = bool(snapshot.get("paused"))
    next_wakeup_sec = int(snapshot.get("next_wakeup_sec") or 0)
    idle_reason = str(snapshot.get("last_idle_reason") or "")
    detail = "Background consciousness is off."
    status = "disabled"

    if requested_enabled and running and paused:
        status = "paused"
        detail = "Paused while another foreground task is active."
    elif requested_enabled and running and idle_reason == "thinking":
        status = "running"
        detail = "Background consciousness is thinking now."
    elif requested_enabled and running and idle_reason == "budget_blocked":
        status = "budget_blocked"
        detail = "Background consciousness hit its budget allocation and is waiting."
    elif requested_enabled and running:
        status = "running"
        detail = (
            f"Background consciousness is idle between wakeups."
            + (f" Next wakeup in {next_wakeup_sec}s." if next_wakeup_sec > 0 else "")
        )
    elif requested_enabled:
        status = "stopped"
        detail = "Enabled in state, but the background thread is not running."

    if idle_reason == "error_backoff" and snapshot.get("last_error"):
        status = "error_backoff"
        detail = f"Waiting to retry after an internal error: {snapshot['last_error']}"

    return {
        "enabled": requested_enabled,
        "status": status,
        "detail": detail,
        **snapshot,
    }


def _start_supervisor_if_needed(settings: dict) -> bool:
    """Start the supervisor once when runtime providers become available."""
    global _supervisor_thread, _supervisor_error
    if not has_supervisor_provider(settings):
        return False
    if _supervisor_thread and _supervisor_thread.is_alive():
        return False
    _supervisor_error = None
    _supervisor_thread = threading.Thread(
        target=_run_supervisor,
        args=(settings,),
        daemon=True,
        name="supervisor-main",
    )
    _supervisor_thread.start()
    return True


def _process_bridge_updates(bridge, offset: int, ctx: Any) -> int:
    updates = bridge.get_updates(offset=offset, timeout=1)
    for upd in updates:
        offset = int(upd["update_id"]) + 1
        msg = upd.get("message") or {}
        if not msg:
            continue

        chat_id = int((msg.get("chat") or {}).get("id") or 1)
        user_id = int((msg.get("from") or {}).get("id") or chat_id or 1)
        text = str(msg.get("text") or "")
        source = str(msg.get("source") or "web")
        sender_label = str(msg.get("sender_label") or "")
        sender_session_id = str(msg.get("sender_session_id") or "")
        client_message_id = str(msg.get("client_message_id") or "")
        transport = msg.get("transport") if isinstance(msg.get("transport"), dict) else {}
        image_base64 = str(msg.get("image_base64") or "")
        image_mime = str(msg.get("image_mime") or "image/jpeg")
        image_caption = str(msg.get("image_caption") or "")
        suppress_chat_log = bool(msg.get("suppress_chat_log"))
        task_constraint = msg.get("task_constraint") if isinstance(msg.get("task_constraint"), dict) else None
        image_data = (
            (image_base64, image_mime, image_caption)
            if image_base64
            else None
        )
        log_text = text or image_caption or ("(image attached)" if image_base64 else "")
        now_iso = utc_now_iso()

        st = ctx.load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id

        from supervisor.message_bus import log_chat

        if not suppress_chat_log:
            log_chat(
                "in",
                chat_id,
                user_id,
                log_text,
                source=source,
                sender_label=sender_label,
                sender_session_id=sender_session_id,
                client_message_id=client_message_id,
                transport=transport,
            )
            if source != "web":
                bridge.broadcast({
                    "type": "photo" if image_base64 else "chat",
                    "role": "user",
                    "content": text,
                    "caption": image_caption,
                    "image_base64": image_base64,
                    "mime": image_mime,
                    "ts": now_iso,
                    "source": source,
                    "sender_label": sender_label,
                    "sender_session_id": sender_session_id,
                    "client_message_id": client_message_id,
                    "transport": transport,
                    "chat_id": chat_id,
                })
        st["last_owner_message_at"] = now_iso
        ctx.save_state(st)

        if not text and not image_base64:
            continue

        lowered = text.strip().lower()
        if lowered.startswith("/panic"):
            ctx.send_with_budget(chat_id, "🛑 PANIC: killing everything. App will close.")
            _execute_panic_stop(ctx.consciousness, ctx.kill_workers)
        elif lowered.startswith("/restart"):
            ctx.send_with_budget(chat_id, "♻️ Restarting.")
            ok, restart_msg = ctx.safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
            if not ok:
                ctx.send_with_budget(chat_id, f"⚠️ Restart cancelled: {restart_msg}")
                continue
            state_dir = DATA_DIR / "state"
            owner_restart_flag = state_dir / "owner_restart_no_resume.flag"
            stable_skip_flag = state_dir / "panic_stop.flag"
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                owner_restart_flag.write_text("owner_restart", encoding="utf-8")
                # Stable fallback builds already skip auto-resume on panic_stop.flag.
                # Pair it with the owner flag so current builds can distinguish
                # this from real panic while stable builds still avoid auto-resume.
                stable_skip_flag.write_text("owner_restart_no_resume", encoding="utf-8")
            except Exception:
                owner_restart_flag.unlink(missing_ok=True)
                stable_skip_flag.unlink(missing_ok=True)
                log.warning("Failed to write owner restart no-resume flag", exc_info=True)
                ctx.send_with_budget(chat_id, "⚠️ Restart cancelled: could not write restart state.")
                continue
            try:
                ctx.kill_workers(
                    force=True,
                    result_status="cancelled",
                    result_reason="Owner restart stopped this task before process restart.",
                )
            except Exception:
                owner_restart_flag.unlink(missing_ok=True)
                stable_skip_flag.unlink(missing_ok=True)
                log.warning("Restart cancelled because worker shutdown failed", exc_info=True)
                try:
                    ctx.send_with_budget(chat_id, "⚠️ Restart cancelled: failed to stop workers.")
                except Exception:
                    pass
                continue
            try:
                ctx.send_with_budget(chat_id, "Stopping active task. New settings apply to the next message.")
            except Exception:
                log.warning("Failed to send owner restart stop notice; continuing restart", exc_info=True)
            _request_restart_exit()
        elif lowered == "/review" or lowered.startswith("/review "):
            # Only ``/review`` (with no suffix) or ``/review <args>``
            # maps to deep_self_review. The slash-commands ``/review-skill``
            # and any future ``/review-*`` flow through the agent's
            # normal chat pipeline so they can route to their own
            # tools.
            ctx.queue_deep_self_review_task(reason="owner:/review", force=True)
        elif lowered.startswith("/evolve"):
            parts = lowered.split()
            action = parts[1] if len(parts) > 1 else "on"
            turn_on = action not in ("off", "stop", "0")
            st2 = ctx.load_state()
            st2["evolution_mode_enabled"] = bool(turn_on)
            if turn_on:
                st2["evolution_consecutive_failures"] = 0
            ctx.save_state(st2)
            if not turn_on:
                ctx.PENDING[:] = [t for t in ctx.PENDING if str(t.get("type")) != "evolution"]
                ctx.sort_pending()
                ctx.persist_queue_snapshot(reason="evolve_off")
            ctx.send_with_budget(chat_id, f"🧬 Evolution: {'ON' if turn_on else 'OFF'}")
        elif lowered.startswith("/bg"):
            parts = lowered.split()
            action = parts[1] if len(parts) > 1 else "status"
            if action in ("start", "on", "1"):
                result = ctx.consciousness.start()
                _bg_s = ctx.load_state()
                _bg_s["bg_consciousness_enabled"] = True
                ctx.save_state(_bg_s)
                ctx.send_with_budget(chat_id, f"🧠 {result}")
            elif action in ("stop", "off", "0"):
                result = ctx.consciousness.stop()
                _bg_s = ctx.load_state()
                _bg_s["bg_consciousness_enabled"] = False
                ctx.save_state(_bg_s)
                ctx.send_with_budget(chat_id, f"🧠 {result}")
            else:
                bg_status = "running" if ctx.consciousness.is_running else "stopped"
                ctx.send_with_budget(chat_id, f"🧠 Background consciousness: {bg_status}")
        elif lowered.startswith("/status"):
            from supervisor.state import status_text
            from supervisor.queue import SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC

            status = status_text(ctx.WORKERS, ctx.PENDING, ctx.RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
            ctx.send_with_budget(chat_id, status)
        else:
            ctx.consciousness.inject_observation(f"Message from my human: {log_text}")
            agent = ctx.get_chat_agent()

            def _run_constrained_or_resume(cid, txt, img, constraint, resume_consciousness: bool):
                try:
                    ctx.handle_chat_direct(cid, txt, img, task_constraint=constraint)
                finally:
                    if resume_consciousness:
                        ctx.consciousness.resume()

            if agent._busy:
                if task_constraint:
                    threading.Thread(
                        target=_run_constrained_or_resume,
                        args=(chat_id, text or image_caption, image_data, task_constraint, False),
                        daemon=True,
                    ).start()
                else:
                    agent.inject_message(text or image_caption, image_data=image_data)
            else:
                ctx.consciousness.pause()
                threading.Thread(
                    target=_run_constrained_or_resume,
                    args=(chat_id, text or image_caption, image_data, task_constraint, True),
                    daemon=True,
                ).start()
    return offset


def _runtime_branch_defaults() -> tuple[str, str]:
    branch_dev = "ouroboros-redesign"
    branch_stable = "ouroboros-stable"
    if not _LAUNCHER_MANAGED:
        return branch_dev, branch_stable
    try:
        from supervisor import git_ops as git_ops_module
        if hasattr(git_ops_module, "managed_branch_defaults"):
            return git_ops_module.managed_branch_defaults(REPO_DIR)
    except Exception:
        pass
    return branch_dev, branch_stable


def _bootstrap_supervisor_repo(settings: dict, git_ops_module=None):
    if git_ops_module is None:
        from supervisor import git_ops as git_ops_module

    branch_dev, branch_stable = _runtime_branch_defaults()

    git_ops_module.init(
        repo_dir=REPO_DIR,
        drive_root=DATA_DIR,
        remote_url="",
        branch_dev=branch_dev,
        branch_stable=branch_stable,
    )
    git_ops_module.ensure_repo_present()
    setup_remote_if_configured(settings, log)

    if _LAUNCHER_MANAGED:
        return git_ops_module.safe_restart(reason="bootstrap", unsynced_policy="rescue_and_reset")

    log.info("Local-dev server start detected — skipping bootstrap git reset.")
    deps_ok, deps_msg = git_ops_module.sync_runtime_dependencies(reason="bootstrap_local_dev")
    if not deps_ok:
        return False, f"Failed local-dev deps sync: {deps_msg}"

    import_result = git_ops_module.import_test()
    if import_result.get("ok"):
        return True, "OK: local-dev bootstrap"
    return False, f"Local-dev import test failed (rc={import_result.get('returncode', -1)})"


def _run_supervisor(settings: dict) -> None:
    """Initialize and run the supervisor loop. Called in a background thread."""
    global _supervisor_error, _supervisor_thread, _consciousness

    _apply_settings_to_env(settings)

    try:
        from supervisor.message_bus import init as bus_init
        from supervisor.message_bus import LocalChatBridge

        bridge = LocalChatBridge(settings)
        bridge._broadcast_fn = broadcast_ws_sync

        from ouroboros.utils import set_log_sink
        set_log_sink(bridge.push_log)

        bus_init(
            drive_root=DATA_DIR,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            budget_report_every=10,
            chat_bridge=bridge,
        )

        from supervisor.state import init as state_init, init_state, load_state, save_state
        from supervisor.state import append_jsonl, update_budget_from_usage, rotate_chat_log_if_needed
        state_init(DATA_DIR, float(settings.get("TOTAL_BUDGET", 10.0)))
        init_state()

        from supervisor.git_ops import safe_restart
        ok, msg = _bootstrap_supervisor_repo(settings)
        if not ok:
            log.error("Supervisor bootstrap failed: %s", msg)

        from supervisor.queue import (
            enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
            persist_queue_snapshot, restore_pending_from_snapshot,
            cancel_task_by_id, queue_deep_self_review_task, sort_pending,
        )
        from supervisor.workers import (
            init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
            spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
            handle_chat_direct, _get_chat_agent, auto_resume_after_restart,
        )

        max_workers = int(settings.get("OUROBOROS_MAX_WORKERS", 5))
        soft_timeout = int(settings.get("OUROBOROS_SOFT_TIMEOUT_SEC", 600))
        hard_timeout = int(settings.get("OUROBOROS_HARD_TIMEOUT_SEC", 1800))

        # Branch names come from the managed-repo manifest defaults so a
        # bundle built with non-default ``--managed-local-branch`` /
        # ``--managed-local-stable-branch`` drives the worker pool too —
        # hardcoding ``ouroboros`` / ``ouroboros-stable`` here would bootstrap
        # one branch set but leave the worker-side commit/restart flows
        # targeting the old names.
        _workers_branch_dev, _workers_branch_stable = _runtime_branch_defaults()
        workers_init(
            repo_dir=REPO_DIR, drive_root=DATA_DIR, max_workers=max_workers,
            soft_timeout=soft_timeout, hard_timeout=hard_timeout,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            branch_dev=_workers_branch_dev, branch_stable=_workers_branch_stable,
        )

        from supervisor.events import dispatch_event
        from supervisor.message_bus import send_with_budget
        from ouroboros.consciousness import BackgroundConsciousness
        import types
        import queue as _queue_mod

        kill_workers()
        spawn_workers(max_workers)
        restored_pending = restore_pending_from_snapshot()
        persist_queue_snapshot(reason="startup")

        if restored_pending > 0:
            st_boot = load_state()
            if st_boot.get("owner_chat_id"):
                send_with_budget(int(st_boot["owner_chat_id"]),
                    f"♻️ Restored pending queue from snapshot: {restored_pending} tasks.")

        auto_resume_after_restart()

        def _get_owner_chat_id() -> Optional[int]:
            try:
                st = load_state()
                cid = st.get("owner_chat_id")
                return int(cid) if cid else None
            except Exception:
                return None

        _consciousness = BackgroundConsciousness(
            drive_root=DATA_DIR, repo_dir=REPO_DIR,
            event_queue=get_event_q(), owner_chat_id_fn=_get_owner_chat_id,
        )

        _bg_st = load_state()
        if _bg_st.get("bg_consciousness_enabled"):
            _consciousness.start()
            log.info("Background consciousness auto-restored from saved state.")

        branch_dev, branch_stable = _runtime_branch_defaults()
        _event_ctx = types.SimpleNamespace(
            DRIVE_ROOT=DATA_DIR, REPO_DIR=REPO_DIR,
            BRANCH_DEV=branch_dev, BRANCH_STABLE=branch_stable,
            bridge=bridge, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
            MAX_WORKERS=max_workers,
            send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
            update_budget_from_usage=update_budget_from_usage, append_jsonl=append_jsonl,
            enqueue_task=enqueue_task, cancel_task_by_id=cancel_task_by_id,
            queue_deep_self_review_task=queue_deep_self_review_task, persist_queue_snapshot=persist_queue_snapshot,
            safe_restart=safe_restart, kill_workers=kill_workers, spawn_workers=spawn_workers,
            sort_pending=sort_pending, consciousness=_consciousness,
            soft_timeout=soft_timeout, hard_timeout=hard_timeout,
            get_chat_agent=_get_chat_agent, handle_chat_direct=handle_chat_direct,
            request_restart=_request_restart_exit,
        )
    except Exception as exc:
        _supervisor_error = f"Supervisor init failed: {exc}"
        _consciousness = None
        log.critical("Supervisor initialization failed", exc_info=True)
        _supervisor_ready.set()
        _supervisor_thread = None
        return

    _supervisor_ready.set()
    log.info("Supervisor ready.")

    # Main supervisor loop
    offset = 0
    crash_count = 0
    while not _restart_requested.is_set():
        try:
            rotate_chat_log_if_needed(DATA_DIR)
            ensure_workers_healthy()

            event_q = get_event_q()
            while True:
                try:
                    evt = event_q.get_nowait()
                except _queue_mod.Empty:
                    break
                if evt.get("type") == "restart_request":
                    _handle_restart_in_supervisor(evt, _event_ctx)
                    continue
                dispatch_event(evt, _event_ctx)

            enforce_task_timeouts()
            enqueue_evolution_task_if_needed()
            assign_tasks()
            persist_queue_snapshot(reason="main_loop")

            offset = _process_bridge_updates(bridge, offset, _event_ctx)

            crash_count = 0
            time.sleep(0.5)

        except Exception as exc:
            crash_count += 1
            log.error("Supervisor loop crash #%d: %s", crash_count, exc, exc_info=True)
            if crash_count >= 3:
                log.critical("Supervisor exceeded max retries.")
                return
            time.sleep(min(30, 2 ** crash_count))
    _supervisor_thread = None


def _handle_restart_in_supervisor(evt: Dict[str, Any], ctx: Any) -> None:
    """Handle restart request from agent — graceful shutdown + exit(42)."""
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            f"♻️ Restart requested by agent: {evt.get('reason')}",
        )
    ok, msg = ctx.safe_restart(
        reason="agent_restart_request", unsynced_policy="rescue_and_reset",
    )
    if not ok:
        if st.get("owner_chat_id"):
            ctx.send_with_budget(int(st["owner_chat_id"]), f"⚠️ Restart skipped: {msg}")
        return
    ctx.kill_workers(force=True)
    st2 = ctx.load_state()
    st2["session_id"] = uuid.uuid4().hex
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    _request_restart_exit()


def _request_restart_exit() -> None:
    """Signal the server to shut down with restart exit code."""
    _restart_requested.set()


def _execute_panic_stop(consciousness, kill_workers_fn) -> None:
    _execute_panic_stop_impl(
        consciousness,
        kill_workers_fn,
        data_dir=DATA_DIR,
        panic_exit_code=PANIC_EXIT_CODE,
        log=log,
    )


# ---------------------------------------------------------------------------
# HTTP/WebSocket routes
# ---------------------------------------------------------------------------
APP_START = time.time()


def _sync_gateway_settings_module() -> None:
    """Keep legacy server.* monkeypatch tests wired to gateway.settings."""
    _gateway_settings.load_settings = load_settings
    _gateway_settings.save_settings = save_settings
    _gateway_settings._apply_settings_to_env = _apply_settings_to_env
    _gateway_settings.apply_runtime_provider_defaults = apply_runtime_provider_defaults


async def api_settings_get(request):
    _sync_gateway_settings_module()
    return await _gateway_settings.api_settings_get(request)


async def api_settings_post(request):
    _sync_gateway_settings_module()
    return await _gateway_settings.api_settings_post(request)


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
web_dir = resolve_web_dir(REPO_DIR)
web_dir.mkdir(parents=True, exist_ok=True)
index_page = make_index_page(web_dir)

routes = [
    Route("/", endpoint=index_page),
    *make_pwa_routes(web_dir),
    *collect_routes(
        data_dir=DATA_DIR,
        settings_handlers={
            "api_onboarding": _gateway_settings.api_onboarding,
            "api_claude_code_status": _gateway_settings.api_claude_code_status,
            "api_claude_code_install": _gateway_settings.api_claude_code_install,
            "api_settings_get": api_settings_get,
            "api_settings_post": api_settings_post,
        },
    ),
    Mount("/static", app=NoCacheStaticFiles(directory=str(web_dir)), name="static"),
]

from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def lifespan(app):
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    _set_ws_event_loop(_event_loop)
    ws_heartbeat_task = asyncio.create_task(
        ws_heartbeat_loop(_has_ws_clients, broadcast_ws),
        name="ws-heartbeat",
    )

    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings, allow_elevation=True)
    _apply_settings_to_env(settings)
    # v5.1.2 elevation ratchet: pin the boot-time runtime-mode baseline AFTER
    # initial settings load + env apply so the ``save_settings`` chokepoint
    # compares incoming saves against this owner-fixed value rather than
    # against on-disk old (which an out-of-process write could corrupt).
    from ouroboros.config import initialize_runtime_mode_baseline
    initialize_runtime_mode_baseline()
    has_local = has_local_routing(settings)
    lifespan_drive_root = pathlib.Path(
        app.state.drive_root
        if hasattr(app, "state") and hasattr(app.state, "drive_root")
        else DATA_DIR
    )
    default_real_data_dir = pathlib.Path.home() / "Ouroboros" / "data"
    pytest_default_real_data_dir = (
        (bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules)
        and lifespan_drive_root == default_real_data_dir
        and not os.environ.get("OUROBOROS_DATA_DIR")
    )

    # v4.50: seed ``data/skills/native/`` from ``repo/skills/`` on first
    # launch. The launcher already does this for packaged builds; calling
    # it here makes source-mode (``python server.py``) installs land at
    # the same layout so the Skills/Marketplace UI sees a consistent tree.
    try:
        if pytest_default_real_data_dir:
            log.info("Skipping native skills bootstrap against real DATA_DIR during pytest")
        else:
            from ouroboros.launcher_bootstrap import ensure_data_skills_seeded
            ensure_data_skills_seeded()
    except Exception:
        log.warning("Native skills bootstrap failed", exc_info=True)

    if has_supervisor_provider(settings):
        _start_supervisor_if_needed(settings)
    else:
        _supervisor_ready.set()
        log.info("No supported provider or local routing configured. Supervisor not started.")

    if has_local and settings.get("LOCAL_MODEL_SOURCE"):
        from ouroboros.local_model_autostart import auto_start_local_model
        threading.Thread(
            target=auto_start_local_model, args=(settings,),
            daemon=True, name="local-model-autostart",
        ).start()

    host_service_task = None
    host_service_server = None
    try:
        from ouroboros.event_bus import init_global_event_bus
        from ouroboros.extension_companion import init_global_supervisor
        from ouroboros.gateway.host_service import (
            DEFAULT_HOST_SERVICE_HOST,
            create_host_service_app,
            host_service_port,
        )

        init_global_event_bus().set_loop(_event_loop)
        init_global_supervisor(lifespan_drive_root)
        host_service_app = create_host_service_app(lifespan_drive_root)
        host_port = host_service_port()
        host_service_config = uvicorn.Config(
            host_service_app,
            host=DEFAULT_HOST_SERVICE_HOST,
            port=host_port,
            log_level="warning",
        )
        host_service_server = uvicorn.Server(host_service_config)
        host_service_task = asyncio.create_task(
            host_service_server.serve(),
            name="host-service-api",
        )
        log.info("Host Service API listening on %s:%d", DEFAULT_HOST_SERVICE_HOST, host_port)
    except Exception:
        log.warning("Failed to start Host Service API", exc_info=True)

    # Phase 4: reload enabled + reviewed extensions so their
    # ``register(api)`` runs across process restarts. Without this,
    # ``toggle_skill(enabled=True)`` would be the only path that loads
    # plugins, and a simple restart would silently unload every
    # extension until the operator toggled each one again.
    try:
        from ouroboros.config import (
            get_skills_repo_path,
            load_settings as _load_settings,
        )
        from ouroboros.extension_loader import reload_all as _reload_extensions
        from ouroboros.extension_loader import set_ws_broadcaster as _set_extension_ws_broadcaster
        _set_extension_ws_broadcaster(broadcast_ws_sync)
        repo_path = get_skills_repo_path()
        if pytest_default_real_data_dir:
            log.info("Skipping extension reload_all against real DATA_DIR during pytest")
        else:
            _reload_extensions(lifespan_drive_root, _load_settings, repo_path=repo_path or None)
    except Exception:
        log.warning("Extension reload_all at startup failed", exc_info=True)

    try:
        from ouroboros.mcp_client import (
            reconfigure_from_settings as _mcp_reconfigure_startup,
            refresh_all_background as _mcp_refresh_background_startup,
        )
        _mcp_reconfigure_startup(settings)
        _mcp_refresh_background_startup(reason="startup")
    except Exception:
        log.warning("MCP startup reconfigure failed", exc_info=True)

    try:
        yield
    finally:
        if host_service_server is not None:
            try:
                host_service_server.should_exit = True
            except Exception:
                pass
        if host_service_task is not None:
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(host_service_task, timeout=5)
            if not host_service_task.done():
                host_service_task.cancel()
                with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(host_service_task, timeout=2)
        ws_heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await ws_heartbeat_task

        log.info("Server shutting down...")
        try:
            from ouroboros.local_model import get_manager
            get_manager().stop_server()
        except Exception:
            pass
        try:
            from ouroboros.tools.shell import kill_all_tracked_subprocesses
            kill_all_tracked_subprocesses()
        except Exception:
            pass
        try:
            from ouroboros.extension_companion import get_global_supervisor
            supervisor = get_global_supervisor()
            if supervisor is not None:
                supervisor.stop_all()
        except Exception:
            pass
        try:
            from supervisor.workers import kill_workers
            kill_workers(force=True)
        except Exception:
            pass
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().shutdown()
        except Exception:
            pass


app = NetworkAuthGate(Starlette(routes=routes, lifespan=lifespan))
app.app.state.drive_root = pathlib.Path(DATA_DIR)  # type: ignore[attr-defined]
app.app.state.repo_dir = pathlib.Path(REPO_DIR)  # type: ignore[attr-defined]
app.app.state.broadcast_ws_sync = broadcast_ws_sync  # type: ignore[attr-defined]
app.app.state.app_start = APP_START  # type: ignore[attr-defined]
app.app.state.supervisor_ready_event = _supervisor_ready  # type: ignore[attr-defined]
app.app.state.get_supervisor_error = lambda: _supervisor_error  # type: ignore[attr-defined]
app.app.state.describe_bg_consciousness_state = _describe_bg_consciousness_state  # type: ignore[attr-defined]
app.app.state.request_restart = _request_restart_exit  # type: ignore[attr-defined]
app.app.state.runtime_branch_defaults = _runtime_branch_defaults  # type: ignore[attr-defined]
app.app.state.bind_host = _BIND_HOST  # type: ignore[attr-defined]
app.app.state.port_file = PORT_FILE  # type: ignore[attr-defined]
app.app.state.default_port = DEFAULT_PORT  # type: ignore[attr-defined]
app.app.state.start_supervisor_if_needed = _start_supervisor_if_needed  # type: ignore[attr-defined]


def _emergency_process_cleanup(*, port_sweep: bool = True) -> None:
    """Kill all child processes, workers, and port holders. Called before any os._exit()."""
    try:
        from ouroboros.tools.shell import kill_all_tracked_subprocesses
        kill_all_tracked_subprocesses()
    except Exception:
        pass
    try:
        from supervisor.workers import kill_workers
        kill_workers(force=True)
    except Exception:
        pass
    import multiprocessing
    from ouroboros.platform_layer import force_kill_pid, kill_process_on_port
    for child in multiprocessing.active_children():
        try:
            force_kill_pid(child.pid)
        except (ProcessLookupError, PermissionError):
            pass
    if port_sweep:
        kill_process_on_port(DEFAULT_PORT)
        kill_process_on_port(8766)
    try:
        from ouroboros.extension_companion import panic_kill_all
        from ouroboros.gateway.host_service import host_service_port
        panic_kill_all()
        if port_sweep:
            kill_process_on_port(host_service_port())
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    try:
        saved_host = str(load_settings().get("OUROBOROS_SERVER_HOST") or "").strip()
    except Exception:
        saved_host = ""
    default_host = os.environ.get("OUROBOROS_SERVER_HOST", "").strip() or saved_host or DEFAULT_HOST
    args = parse_server_args(default_host, DEFAULT_PORT)
    global _BIND_HOST
    _BIND_HOST = args.host
    app.app.state.bind_host = args.host  # type: ignore[attr-defined]
    auth_warning = get_network_auth_startup_warning(args.host)
    if auth_warning:
        log.warning(auth_warning)
    auth_error = validate_network_auth_configuration(args.host)
    if auth_error:
        log.error(auth_error)
        return 2
    actual_port = find_free_port(args.host, args.port)
    if actual_port != args.port:
        log.info("Port %d busy on %s, using %d instead", args.port, args.host, actual_port)
    write_port_file(PORT_FILE, actual_port)
    log.info("Starting Ouroboros server on %s:%d", args.host, actual_port)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=actual_port,
        log_level="warning",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    server = uvicorn.Server(config)
    _uvicorn_exited = threading.Event()

    def _check_restart():
        """Monitor for restart signal, then shut down uvicorn."""
        while not _restart_requested.is_set():
            time.sleep(0.5)
        log.info("Restart requested — closing WebSocket clients and shutting down server.")

        # Close all WebSocket connections so uvicorn can shut down cleanly.
        loop = _event_loop
        if loop:
            try:
                future = asyncio.run_coroutine_threadsafe(close_all_ws(), loop)
                future.result(timeout=3)
            except Exception:
                pass

        server.should_exit = True

        # Safety net: only force-exit if uvicorn itself never returns control.
        # In direct-server mode, the main thread still needs time to run cleanup
        # and re-exec the process after server.run() exits.
        force_exit_timeout_sec = 5 if _LAUNCHER_MANAGED else 30
        if _uvicorn_exited.wait(timeout=force_exit_timeout_sec):
            return
        log.warning(
            "Uvicorn did not exit within %ss — running emergency cleanup before os._exit(%d)",
            force_exit_timeout_sec,
            RESTART_EXIT_CODE,
        )
        _emergency_process_cleanup()
        os._exit(RESTART_EXIT_CODE)

    threading.Thread(target=_check_restart, daemon=True).start()

    try:
        server.run()
    finally:
        _uvicorn_exited.set()

    if _restart_requested.is_set():
        log.info("Exiting with code %d (restart signal).", RESTART_EXIT_CODE)
        _emergency_process_cleanup(port_sweep=False)
        if not _LAUNCHER_MANAGED:
            _restart_current_process(args.host, actual_port)
        os._exit(RESTART_EXIT_CODE)

    return 0


if __name__ == "__main__":
    sys.exit(main())

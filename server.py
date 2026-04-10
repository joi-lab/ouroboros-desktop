"""
Ouroboros Agent Server — Self-editable entry point.

This file lives in REPO_DIR and can be modified by the agent.
It runs as a subprocess of the launcher, serving the web UI and
coordinating the supervisor/worker system.

Starlette + uvicorn on localhost:{PORT}.
"""

import asyncio
import collections
import json
import logging

import os
import pathlib
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

import uvicorn

from ouroboros import get_version
from ouroboros.file_browser_api import file_browser_routes
from ouroboros.model_catalog_api import api_model_catalog
from ouroboros.server_control import (
    execute_panic_stop as _execute_panic_stop_impl,
    restart_current_process as _restart_current_process_impl,
)
from ouroboros.server_history_api import make_chat_history_endpoint, make_cost_breakdown_endpoint
from ouroboros.server_auth import (
    NetworkAuthGate,
    get_network_auth_startup_warning,
    validate_network_auth_configuration,
)
from ouroboros.server_entrypoint import find_free_port, parse_server_args, write_port_file
from ouroboros.server_web import NoCacheStaticFiles, make_index_page, resolve_web_dir

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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
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
_SECRET_SETTING_KEYS = {
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "ANTHROPIC_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "GITHUB_TOKEN",
    "OUROBOROS_NETWORK_PASSWORD",
}

# ---------------------------------------------------------------------------
# WebSocket connections manager
# ---------------------------------------------------------------------------
_ws_clients: List[WebSocket] = []
_ws_lock = threading.Lock()
def _has_ws_clients() -> bool:
    with _ws_lock:
        return bool(_ws_clients)

async def broadcast_ws(msg: dict) -> None:
    """Send a message to all connected WebSocket clients."""
    data = json.dumps(msg, ensure_ascii=False, default=str)
    with _ws_lock:
        clients = list(_ws_clients)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception:
            log.debug("Dropping dead WebSocket client during broadcast", exc_info=True)
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                try:
                    _ws_clients.remove(ws)
                except ValueError:
                    pass


def broadcast_ws_sync(msg: dict) -> None:
    """Thread-safe sync wrapper for broadcasting.

    Uses the saved _event_loop reference (set in startup_event) rather than
    asyncio.get_event_loop(), which is unreliable from non-main threads
    in Python 3.10+.
    """
    loop = _event_loop
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast_ws(msg), loop)
    except RuntimeError:
        pass


def _mask_secret_value(value: Any) -> str:
    text = str(value or "")
    return text[:8] + "..." if len(text) > 8 else "***"


def _looks_masked_secret(value: Any) -> bool:
    text = str(value or "").strip()
    return text == "***" or text.endswith("...")


def _merge_settings_payload(current: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    merged = {k: v for k, v in current.items()}
    for key in _SETTINGS_DEFAULTS:
        if key not in body:
            continue
        if key in _SECRET_SETTING_KEYS and _looks_masked_secret(body[key]) and merged.get(key):
            continue
        merged[key] = body[key]
    return merged


def _restart_current_process(host: str, port: int) -> None:
    _restart_current_process_impl(host, port, repo_dir=REPO_DIR, log=log)


def _claude_code_status_payload() -> Dict[str, Any]:
    """Return Claude runtime status using the app-managed runtime contract.

    Replaces the old SDK-only installed/missing check with a richer
    payload that reports: runtime source, interpreter path, SDK version,
    CLI path/version, app-managed vs legacy state, API key readiness,
    and the most recent stderr output on failure.
    """
    from ouroboros.platform_layer import resolve_claude_runtime

    rt = resolve_claude_runtime()
    label = rt.status_label()

    stderr_tail = ""
    try:
        from ouroboros.gateways.claude_code import get_last_stderr as gw_stderr
        stderr_tail = gw_stderr(max_chars=2000)
    except Exception:
        pass

    message_map = {
        "ready": f"Claude runtime ready (SDK {rt.sdk_version}, CLI {rt.cli_version})",
        "no_api_key": f"Claude runtime available (SDK {rt.sdk_version}) but ANTHROPIC_API_KEY is not set. Add it in Settings.",
        "error": f"Claude runtime error: {rt.error}",
        "degraded": f"Claude runtime degraded (SDK {rt.sdk_version}, CLI {'found' if rt.cli_path else 'missing'}). Try Repair.",
        "missing": "Claude runtime not available. Use Repair in Settings or reinstall the app.",
    }

    return {
        "status": label,
        "installed": bool(rt.sdk_version),
        "ready": rt.ready,
        "busy": False,
        "version": rt.sdk_version,
        "cli_version": rt.cli_version,
        "cli_path": rt.cli_path,
        "interpreter_path": rt.interpreter_path,
        "app_managed": rt.app_managed,
        "legacy_detected": rt.legacy_detected,
        "legacy_sdk_version": rt.legacy_sdk_version,
        "api_key_set": rt.api_key_set,
        "message": message_map.get(label, f"Claude runtime: {label}"),
        "error": rt.error,
        "stderr_tail": stderr_tail,
    }


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
    has_startup_ready_provider,
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
        telegram_chat_id = int(msg.get("telegram_chat_id") or 0)
        image_base64 = str(msg.get("image_base64") or "")
        image_mime = str(msg.get("image_mime") or "image/jpeg")
        image_caption = str(msg.get("image_caption") or "")
        image_data = (
            (image_base64, image_mime, image_caption)
            if image_base64
            else None
        )
        log_text = text or image_caption or ("(image attached)" if image_base64 else "")
        now_iso = datetime.now(timezone.utc).isoformat()

        st = ctx.load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id

        from supervisor.message_bus import log_chat

        log_chat(
            "in",
            chat_id,
            user_id,
            log_text,
            source=source,
            sender_label=sender_label,
            sender_session_id=sender_session_id,
            client_message_id=client_message_id,
            telegram_chat_id=telegram_chat_id,
        )
        st["last_owner_message_at"] = now_iso
        ctx.save_state(st)

        if not text and not image_base64:
            continue

        lowered = text.strip().lower()
        if lowered.startswith("/panic"):
            ctx.send_with_budget(chat_id, "🛑 PANIC: killing everything. App will close.")
            _execute_panic_stop(ctx.consciousness, ctx.kill_workers)
        elif lowered.startswith("/restart"):
            ctx.send_with_budget(chat_id, "♻️ Restarting (soft).")
            ok, restart_msg = ctx.safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
            if not ok:
                ctx.send_with_budget(chat_id, f"⚠️ Restart cancelled: {restart_msg}")
                continue
            ctx.kill_workers(force=True)
            _request_restart_exit()
        elif lowered.startswith("/review"):
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

            status = status_text(ctx.WORKERS, ctx.PENDING, ctx.RUNNING, ctx.soft_timeout, ctx.hard_timeout)
            ctx.send_with_budget(chat_id, status, force_budget=True)
        else:
            ctx.consciousness.inject_observation(f"Owner message: {log_text}")
            agent = ctx.get_chat_agent()
            if agent._busy:
                agent.inject_message(text or image_caption, image_data=image_data)
            else:
                ctx.consciousness.pause()

                def _run_and_resume(cid, txt, img):
                    try:
                        ctx.handle_chat_direct(cid, txt, img)
                    finally:
                        ctx.consciousness.resume()

                threading.Thread(
                    target=_run_and_resume,
                    args=(chat_id, text or image_caption, image_data),
                    daemon=True,
                ).start()
    return offset


def _bootstrap_supervisor_repo(settings: dict, git_ops_module=None):
    if git_ops_module is None:
        from supervisor import git_ops as git_ops_module

    git_ops_module.init(
        repo_dir=REPO_DIR,
        drive_root=DATA_DIR,
        remote_url="",
        branch_dev="ouroboros",
        branch_stable="ouroboros-stable",
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

        workers_init(
            repo_dir=REPO_DIR, drive_root=DATA_DIR, max_workers=max_workers,
            soft_timeout=soft_timeout, hard_timeout=hard_timeout,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            branch_dev="ouroboros", branch_stable="ouroboros-stable",
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

        _event_ctx = types.SimpleNamespace(
            DRIVE_ROOT=DATA_DIR, REPO_DIR=REPO_DIR,
            BRANCH_DEV="ouroboros", BRANCH_STABLE="ouroboros-stable",
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
api_cost_breakdown = make_cost_breakdown_endpoint(DATA_DIR)
api_chat_history = make_chat_history_endpoint(DATA_DIR)


async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    with _ws_lock:
        _ws_clients.append(websocket)
    log.info("WebSocket client connected (total: %d)", len(_ws_clients))
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            payload = msg.get("content", "") if msg_type == "chat" else msg.get("cmd", "")
            if msg_type in ("chat", "command") and payload:
                try:
                    from supervisor.message_bus import get_bridge
                    bridge = get_bridge()
                    if msg_type == "chat":
                        bridge.ui_send(
                            payload,
                            sender_session_id=str(msg.get("sender_session_id", "") or ""),
                            client_message_id=str(msg.get("client_message_id", "") or ""),
                        )
                    else:
                        bridge.ui_send(payload, broadcast=False)
                except Exception:
                    ts = datetime.now(timezone.utc).isoformat()
                    await websocket.send_text(json.dumps({
                        "type": "chat", "role": "assistant",
                        "content": "⚠️ System is still initializing. Please wait a moment and try again.",
                        "ts": ts,
                    }))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("WebSocket error: %s", e)
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(websocket)
            except ValueError:
                pass
        log.info("WebSocket client disconnected (total: %d)", len(_ws_clients))


async def api_health(request: Request) -> JSONResponse:
    runtime_version = get_version()
    app_version = os.environ.get("OUROBOROS_APP_VERSION", "").strip() or runtime_version
    return JSONResponse({
        "status": "ok",
        # legacy field for backward compatibility
        "version": runtime_version,
        "runtime_version": runtime_version,
        "app_version": app_version,
    })


async def api_state(request: Request) -> JSONResponse:
    try:
        from supervisor.state import load_state, budget_remaining, budget_pct, TOTAL_BUDGET_LIMIT
        from supervisor.workers import WORKERS, PENDING, RUNNING
        from supervisor.queue import get_evolution_status_snapshot
        st = load_state()
        alive = 0
        total_w = 0
        try:
            alive = sum(1 for w in WORKERS.values() if w.proc.is_alive())
            total_w = len(WORKERS)
        except Exception:
            pass
        spent = float(st.get("spent_usd") or 0.0)
        limit = float(TOTAL_BUDGET_LIMIT or 10.0)
        evolution_state = get_evolution_status_snapshot()
        bg_requested = bool(st.get("bg_consciousness_enabled"))
        bg_state = _describe_bg_consciousness_state(bg_requested)
        return JSONResponse({
            "uptime": int(time.time() - APP_START),
            "workers_alive": alive,
            "workers_total": total_w,
            "pending_count": len(PENDING),
            "running_count": len(RUNNING),
            "spent_usd": round(spent, 4),
            "budget_limit": limit,
            "budget_pct": round((spent / limit * 100) if limit > 0 else 0, 1),
            "branch": st.get("current_branch", "ouroboros"),
            "sha": (st.get("current_sha") or "")[:8],
            "evolution_enabled": bool(st.get("evolution_mode_enabled")),
            "bg_consciousness_enabled": bg_requested,
            "evolution_cycle": int(st.get("evolution_cycle") or 0),
            "evolution_state": evolution_state,
            "bg_consciousness_state": bg_state,
            "spent_calls": int(st.get("spent_calls") or 0),
            "supervisor_ready": _supervisor_ready.is_set(),
            "supervisor_error": _supervisor_error,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_settings_get(request: Request) -> JSONResponse:
    settings, _, _ = apply_runtime_provider_defaults(load_settings())
    safe = {k: v for k, v in settings.items()}
    for key in _SECRET_SETTING_KEYS:
        if safe.get(key):
            safe[key] = _mask_secret_value(safe[key])
    return JSONResponse(safe)


async def api_onboarding(request: Request) -> Response:
    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings)
    if has_startup_ready_provider(settings):
        return Response(status_code=204)
    return HTMLResponse(build_onboarding_html(settings, host_mode="web"))


async def api_claude_code_status(request: Request) -> JSONResponse:
    try:
        payload = await asyncio.to_thread(_claude_code_status_payload)
        return JSONResponse(payload)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "installed": False,
            "busy": False,
            "message": "Failed to read Claude Agent SDK status.",
            "error": str(e),
        }, status_code=500)


async def api_claude_code_install(request: Request) -> JSONResponse:
    """Repair/update the app-managed Claude runtime.

    Replaces the old "pip install SDK" endpoint. Now operates on the
    app-managed interpreter (prefers embedded python-standalone) and
    always reinstalls/upgrades to the pinned baseline version.
    """
    try:
        import subprocess as _sp
        import sys as _sys

        interpreter = _sys.executable
        try:
            from ouroboros.platform_layer import resolve_claude_runtime
            rt = resolve_claude_runtime()
            if rt.interpreter_path:
                interpreter = rt.interpreter_path
        except Exception:
            pass

        result = await asyncio.to_thread(
            lambda: _sp.run(
                [interpreter, "-m", "pip", "install", "--upgrade",
                 "claude-agent-sdk>=0.1.50"],
                capture_output=True, text=True, timeout=120,
            )
        )
        if result.returncode == 0:
            payload = await asyncio.to_thread(_claude_code_status_payload)
            payload["repaired"] = True
            return JSONResponse(payload)
        return JSONResponse({
            "status": "error",
            "installed": False,
            "ready": False,
            "busy": False,
            "message": "Claude runtime repair failed.",
            "error": (result.stderr or result.stdout or "")[:500],
        }, status_code=500)
    except Exception as e:
        return JSONResponse({
            "status": "error",
            "installed": False,
            "ready": False,
            "busy": False,
            "message": "Claude runtime repair failed.",
            "error": f"{type(e).__name__}: {e}",
        }, status_code=500)


async def api_settings_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        current = _merge_settings_payload(load_settings(), body)
        current, provider_defaults_changed, provider_default_keys = apply_runtime_provider_defaults(current)
        if str(current.get("LOCAL_MODEL_SOURCE", "") or "").strip() and not has_supervisor_provider(current):
            return JSONResponse(
                {"error": "Local-only setups must route at least one model to the local runtime."},
                status_code=400,
            )
        save_settings(current)
        _apply_settings_to_env(current)
        _start_supervisor_if_needed(current)
        warnings = []
        if provider_defaults_changed:
            warnings.append(
                "Normalized direct-provider routing because OpenRouter is not configured for the active provider."
            )
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().configure_from_settings(current)
        except Exception:
            pass
        _repo_slug = current.get("GITHUB_REPO", "")
        _gh_token = current.get("GITHUB_TOKEN", "")
        if _repo_slug and _gh_token:
            from supervisor.git_ops import configure_remote, migrate_remote_credentials
            remote_ok, remote_msg = configure_remote(_repo_slug, _gh_token)
            if not remote_ok:
                log.warning("Remote configuration failed on settings save: %s", remote_msg)
                warnings.append(f"Remote config failed: {remote_msg}")
            else:
                mig_ok, mig_msg = migrate_remote_credentials()
                if not mig_ok:
                    log.warning("Credential migration failed: %s", mig_msg)
        resp = {"status": "saved"}
        if warnings:
            resp["warnings"] = warnings
        return JSONResponse(resp)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_reset(request: Request) -> JSONResponse:
    """Reset all runtime data (state, memory, logs, settings) but keep repo.

    After reset the launcher will show the onboarding wizard on next start.
    """
    import shutil
    try:
        deleted = []
        for subdir in ("state", "memory", "logs", "archive", "locks", "task_results", "uploads"):
            p = DATA_DIR / subdir
            if p.exists():
                shutil.rmtree(p, ignore_errors=True)
                deleted.append(subdir)
        settings_file = DATA_DIR / "settings.json"
        if settings_file.exists():
            settings_file.unlink()
            deleted.append("settings.json")
        _request_restart_exit()
        return JSONResponse({"status": "ok", "deleted": deleted, "restarting": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_command(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        if cmd:
            from supervisor.message_bus import get_bridge
            bridge = get_bridge()
            bridge.ui_send(cmd, broadcast=False)
        return JSONResponse({"status": "ok"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_git_log(request: Request) -> JSONResponse:
    """Return recent commits, tags, and current branch/sha."""
    try:
        from supervisor.git_ops import list_commits, list_versions, git_capture
        commits = list_commits(max_count=30)
        tags = list_versions(max_count=20)
        rc, branch, _ = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        rc2, sha, _ = git_capture(["git", "rev-parse", "--short", "HEAD"])
        return JSONResponse({
            "commits": commits,
            "tags": tags,
            "branch": branch.strip() if rc == 0 else "unknown",
            "sha": sha.strip() if rc2 == 0 else "",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_git_rollback(request: Request) -> JSONResponse:
    """Roll back to a specific commit or tag, then restart."""
    try:
        body = await request.json()
        target = body.get("target", "").strip()
        if not target:
            return JSONResponse({"error": "missing target"}, status_code=400)
        from supervisor.git_ops import rollback_to_version
        ok, msg = rollback_to_version(target, reason="ui_rollback")
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        _request_restart_exit()
        return JSONResponse({"status": "ok", "message": msg})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_git_promote(request: Request) -> JSONResponse:
    """Promote current ouroboros branch to ouroboros-stable."""
    try:
        import subprocess as sp
        sp.run(["git", "branch", "-f", "ouroboros-stable", "ouroboros"],
               cwd=str(REPO_DIR), check=True, capture_output=True)
        return JSONResponse({"status": "ok", "message": "ouroboros-stable updated to match ouroboros"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_evo_cache: Dict[str, Any] = {}


async def api_evolution_data(request: Request) -> JSONResponse:
    """Collect evolution metrics for each git tag."""
    from ouroboros.utils import collect_evolution_metrics
    import time as _t

    now = _t.time()
    force_refresh = str(request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
    if not force_refresh and _evo_cache.get("ts") and now - _evo_cache["ts"] < 60:
        return JSONResponse({
            "points": _evo_cache["points"],
            "generated_at": _evo_cache.get("generated_at", ""),
            "cached": True,
        })

    data_dir = os.environ.get("OUROBOROS_DATA_DIR", os.path.expanduser("~/Ouroboros/data"))
    data_points = await collect_evolution_metrics(str(REPO_DIR), data_dir=data_dir)
    _evo_cache["ts"] = now
    _evo_cache["points"] = data_points
    _evo_cache["generated_at"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse({
        "points": data_points,
        "generated_at": _evo_cache["generated_at"],
        "cached": False,
    })


from ouroboros.local_model_api import (
    api_local_model_start, api_local_model_stop,
    api_local_model_status, api_local_model_test,
)
from ouroboros.chat_upload_api import api_chat_upload, api_chat_upload_delete


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
web_dir = resolve_web_dir(REPO_DIR)
web_dir.mkdir(parents=True, exist_ok=True)
index_page = make_index_page(web_dir)

routes = [
    Route("/", endpoint=index_page),
    Route("/api/health", endpoint=api_health),
    Route("/api/state", endpoint=api_state),
    *file_browser_routes(),
    Route("/api/onboarding", endpoint=api_onboarding),
    Route("/api/claude-code/status", endpoint=api_claude_code_status),
    Route("/api/claude-code/install", endpoint=api_claude_code_install, methods=["POST"]),
    Route("/api/settings", endpoint=api_settings_get, methods=["GET"]),
    Route("/api/settings", endpoint=api_settings_post, methods=["POST"]),
    Route("/api/model-catalog", endpoint=api_model_catalog),
    Route("/api/command", endpoint=api_command, methods=["POST"]),
    Route("/api/reset", endpoint=api_reset, methods=["POST"]),
    Route("/api/git/log", endpoint=api_git_log),
    Route("/api/git/rollback", endpoint=api_git_rollback, methods=["POST"]),
    Route("/api/git/promote", endpoint=api_git_promote, methods=["POST"]),
    Route("/api/cost-breakdown", endpoint=api_cost_breakdown),
    Route("/api/evolution-data", endpoint=api_evolution_data),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
    Route("/api/chat/upload", endpoint=api_chat_upload_delete, methods=["DELETE"]),
    Route("/api/local-model/start", endpoint=api_local_model_start, methods=["POST"]),
    Route("/api/local-model/stop", endpoint=api_local_model_stop, methods=["POST"]),
    Route("/api/local-model/status", endpoint=api_local_model_status),
    Route("/api/local-model/test", endpoint=api_local_model_test, methods=["POST"]),
    WebSocketRoute("/ws", endpoint=ws_endpoint),
    Mount("/static", app=NoCacheStaticFiles(directory=str(web_dir)), name="static"),
]

from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def lifespan(app):
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    ws_heartbeat_task = asyncio.create_task(
        ws_heartbeat_loop(_has_ws_clients, broadcast_ws),
        name="ws-heartbeat",
    )

    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings)
    has_local = has_local_routing(settings)

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

    # A2A server
    a2a_server_task = None
    if settings.get("A2A_ENABLED", True):
        try:
            from ouroboros.a2a_server import start_a2a_server
            a2a_port = int(settings.get("A2A_PORT", 18800))
            a2a_server_task = asyncio.create_task(
                start_a2a_server(settings), name="a2a-server"
            )
            log.info("A2A server task created on port %d", a2a_port)
        except Exception:
            log.warning("Failed to start A2A server", exc_info=True)

    try:
        yield
    finally:
        # Stop A2A server
        if a2a_server_task:
            try:
                from ouroboros.a2a_server import stop_a2a_server
                stop_a2a_server()
                a2a_server_task.cancel()
            except Exception:
                pass
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


def _emergency_process_cleanup() -> None:
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
    kill_process_on_port(DEFAULT_PORT)
    kill_process_on_port(8766)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    args = parse_server_args(DEFAULT_HOST, DEFAULT_PORT)
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

        # Close all WebSocket connections so uvicorn can shut down cleanly
        loop = _event_loop
        if loop:
            async def _close_all_ws():
                with _ws_lock:
                    clients = list(_ws_clients)
                for ws in clients:
                    try:
                        await ws.close(code=1012, reason="Server restarting")
                    except Exception:
                        pass
            try:
                future = asyncio.run_coroutine_threadsafe(_close_all_ws(), loop)
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
        _emergency_process_cleanup()
        if not _LAUNCHER_MANAGED:
            _restart_current_process(args.host, actual_port)
        os._exit(RESTART_EXIT_CODE)

    _emergency_process_cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())

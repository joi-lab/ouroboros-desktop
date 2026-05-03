"""
Ouroboros Agent Server — Self-editable entry point.

This file lives in REPO_DIR and can be modified by the agent.
It runs as a subprocess of the launcher, serving the web UI and
coordinating the supervisor/worker system.

Starlette + uvicorn on configurable host:port (default localhost:8765; non-loopback binding supported via OUROBOROS_SERVER_HOST).
"""

import asyncio
import collections
import json
import logging

import os
import inspect
import pathlib
import socket
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
# Runtime network binding (captured in main() from parse_server_args)
# Used by /api/settings to expose a LAN reachability hint to the Settings UI.
# ---------------------------------------------------------------------------
_BIND_HOST = DEFAULT_HOST


def _get_lan_ip() -> str:
    """Return the LAN IP using a UDP socket trick (no packet sent). Returns '' on failure."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # RFC 5737 TEST-NET-1, no packet sent
            return s.getsockname()[0]
    except OSError:
        return ""


# IPv4 wildcard hosts that mean "listen on all interfaces"
_WILDCARD_HOSTS = frozenset({"0.0.0.0", ""})


def _is_wildcard_host(host: str) -> bool:
    return host in _WILDCARD_HOSTS


from ouroboros.platform_layer import is_container_env


def _build_network_meta(bind_host: str, bind_port: int) -> dict:
    """Build the _meta dict for /api/settings response."""
    from ouroboros.server_auth import get_network_auth_startup_warning, is_loopback_host
    # Strip surrounding brackets from IPv6 literals (e.g. "[::1]" → "::1") so
    # is_loopback_host can correctly classify bracketed IPv6 loopback addresses.
    unbracketed = bind_host[1:-1] if bind_host.startswith("[") and bind_host.endswith("]") else bind_host
    loopback = is_loopback_host(unbracketed)
    if loopback:
        return {
            "bind_host": bind_host,
            "bind_port": bind_port,
            "lan_ip": "",
            "reachability": "loopback_only",
            "recommended_url": "",
            "warning": "Server is bound to localhost — not accessible from other devices.",
        }
    # Non-loopback: determine the advertised IP
    wildcard = _is_wildcard_host(bind_host)
    if wildcard:
        if is_container_env():
            # Container bridge IPs are typically not reachable from the user's LAN
            lan_ip = ""
        else:
            lan_ip = _get_lan_ip()
    elif bind_host in ("::", "[::]"):
        # IPv6 wildcard — startup uses AF_INET only (server_entrypoint.py), so we
        # cannot reliably detect or advertise an IPv6 LAN IP. Degrade gracefully.
        lan_ip = ""
    else:
        # Specific non-loopback bind address — use it directly (IPv4 or hostname).
        # Use unbracketed form so URL construction can uniformly re-bracket IPv6.
        lan_ip = unbracketed

    auth_warning = get_network_auth_startup_warning(bind_host) or ""
    if lan_ip:
        # Handle IPv6 addresses (bracket them for URL)
        host_in_url = f"[{lan_ip}]" if ":" in lan_ip else lan_ip
        reachability = "lan_reachable"
        recommended_url = f"http://{host_in_url}:{bind_port}"
        warning = auth_warning
    else:
        reachability = "host_ip_unknown"
        recommended_url = f"http://your-host-ip:{bind_port}"
        warning = " ".join(
            part for part in [
                "Could not detect LAN IP automatically." if wildcard else "",
                auth_warning,
            ]
            if part
        )
    return {
        "bind_host": bind_host,
        "bind_port": bind_port,
        "lan_ip": lan_ip,
        "reachability": reachability,
        "recommended_url": recommended_url,
        "warning": warning,
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


# Keys that refresh immediately in the running supervisor (no restart, no task boundary).
_IMMEDIATE_KEYS = frozenset({
    "TOTAL_BUDGET",
    "OUROBOROS_SOFT_TIMEOUT_SEC",
    "OUROBOROS_HARD_TIMEOUT_SEC",
    "OUROBOROS_TOOL_TIMEOUT_SEC",
    # Integration settings reconfigured inline in api_settings_post
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "GITHUB_TOKEN",
    "GITHUB_REPO",
})

# Keys that require a full process restart when changed.
# Everything else is hot-reloadable (takes effect on the next task).
_RESTART_REQUIRED_KEYS = frozenset({
    "OUROBOROS_MAX_WORKERS",
    "OUROBOROS_SERVER_HOST",
    "LOCAL_MODEL_SOURCE",
    "LOCAL_MODEL_FILENAME",
    "LOCAL_MODEL_PORT",
    "LOCAL_MODEL_N_GPU_LAYERS",
    "LOCAL_MODEL_CONTEXT_LENGTH",
    "LOCAL_MODEL_CHAT_FORMAT",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_BASE_URL",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
    # A2A server requires restart when toggled or reconfigured
    "A2A_ENABLED",
    "A2A_PORT",
    "A2A_HOST",
    "A2A_AGENT_NAME",
    "A2A_AGENT_DESCRIPTION",
    "A2A_MAX_CONCURRENT",
    "A2A_TASK_TTL_HOURS",
})


def _classify_settings_changes(
    old: Dict[str, Any],
    new: Dict[str, Any],
) -> list:
    """Return list of changed keys that require a process restart.

    Keys that changed but are NOT in ``_RESTART_REQUIRED_KEYS`` are
    hot-reloadable — they take effect at the start of the next task.
    """
    return [
        k for k in _RESTART_REQUIRED_KEYS
        if str(new.get(k, "") or "") != str(old.get(k, "") or "")
    ]


def _merge_settings_payload(current: Dict[str, Any], body: Dict[str, Any]) -> Dict[str, Any]:
    merged = {k: v for k, v in current.items()}
    for key in _SETTINGS_DEFAULTS:
        # v5.1.2 elevation ratchet: ``OUROBOROS_RUNTIME_MODE`` is owner-only.
        # The runtime mode axis controls how far Ouroboros may self-modify;
        # accepting it from /api/settings POST gives the agent a same-process
        # path to raise its own privilege scope (loopback POST has no auth).
        # Mode changes happen only through direct ``settings.json`` edits while
        # the agent is stopped, plus restart. The desktop UI uses a
        # launcher-native confirmation bridge instead of this HTTP path.
        if key == "OUROBOROS_RUNTIME_MODE":
            continue
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
    classify_runtime_provider_change,
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
        suppress_chat_log = bool(msg.get("suppress_chat_log"))
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


def _runtime_branch_defaults() -> tuple[str, str]:
    branch_dev = "ouroboros"
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

    # Module watcher — auto-restart workers when supervisor/agent source
    # files change on disk. See supervisor/module_watcher.py for rationale.
    # Off by default behaviour preserved via ``OUROBOROS_AUTO_RESTART_ON_MODULE_CHANGE``;
    # default ``true`` since this prevents the 2026-05-02 cascade.
    _watcher_enabled = (
        os.environ.get("OUROBOROS_AUTO_RESTART_ON_MODULE_CHANGE", "true")
        .strip().lower() in ("true", "1", "yes")
    )
    _module_watcher = None
    if _watcher_enabled:
        try:
            from supervisor.module_watcher import (
                ModuleWatcher, default_watch_roots,
            )
            _watch_roots = default_watch_roots(REPO_DIR)
            if _watch_roots:
                _module_watcher = ModuleWatcher(_watch_roots)
                _module_watcher.baseline()
                append_jsonl(
                    DATA_DIR / "logs" / "supervisor.jsonl",
                    {"ts": time.time(), "type": "module_watcher_baseline",
                     "watched_roots": [str(r) for r in _watch_roots]},
                )
                log.info("Module watcher armed; %d source root(s) tracked.",
                         len(_watch_roots))
        except Exception:
            log.warning("Module watcher init failed; auto-restart disabled.",
                        exc_info=True)
            _module_watcher = None

    # Main supervisor loop
    offset = 0
    crash_count = 0
    while not _restart_requested.is_set():
        try:
            rotate_chat_log_if_needed(DATA_DIR)
            ensure_workers_healthy()

            # Source-tree drift check: if a supervisor/agent file has
            # changed on disk, synthesize a restart_request so the
            # existing event-channel handles process exit + relaunch.
            if _module_watcher is not None:
                try:
                    _drift = _module_watcher.check()
                except Exception:
                    log.warning("Module watcher tick failed", exc_info=True)
                    _drift = None
                if _drift is not None:
                    append_jsonl(
                        DATA_DIR / "logs" / "supervisor.jsonl",
                        {"ts": time.time(), "type": "module_drift_detected",
                         "changed_paths": list(_drift.changed_paths),
                         "summary": _drift.summary_sentence()},
                    )
                    log.warning("Source drift detected (%s) — queuing restart.",
                                _drift.summary_sentence())
                    get_event_q().put({
                        "type": "restart_request",
                        "reason": f"module_drift: {_drift.summary_sentence()}",
                    })

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
            # Phase 5 WS dispatch for extensions: route provider-safe
            # ``ext_<len>_<token>_<msg>`` message types
            # message types to handlers registered via
            # ``PluginAPI.register_ws_handler``. The handler receives the
            # full payload dict; responses (if any) are sent back to the
            # originating websocket as a best-effort one-shot reply.
            parsed_ext_type = None
            if isinstance(msg_type, str):
                try:
                    from ouroboros.extension_loader import parse_extension_surface_name as _parse_ext_name
                    parsed_ext_type = _parse_ext_name(msg_type)
                except Exception:
                    parsed_ext_type = None
            if parsed_ext_type:
                state = None
                try:
                    from ouroboros.config import get_skills_repo_path, load_settings
                    from ouroboros.extension_loader import (
                        extension_name_prefix as _extension_name_prefix,
                        list_ws_handlers as _ws_handlers,
                        reconcile_extension as _reconcile_extension,
                    )
                    from ouroboros.skill_loader import discover_skills as _discover_skills
                    drive_root = pathlib.Path(
                        websocket.app.state.drive_root  # type: ignore[attr-defined]
                        if hasattr(websocket.app, "state") and hasattr(websocket.app.state, "drive_root")
                        else DATA_DIR
                    )
                    repo_path = get_skills_repo_path()
                    handler_spec = _ws_handlers().get(msg_type)
                    skill_name = str((handler_spec or {}).get("skill") or "")
                    if not skill_name:
                        for skill in _discover_skills(drive_root, repo_path=repo_path):
                            if msg_type.startswith(_extension_name_prefix(skill.name)):
                                skill_name = skill.name
                                break
                    if not skill_name:
                        raise KeyError(msg_type)
                    state = _reconcile_extension(skill_name, drive_root, load_settings, repo_path=repo_path)
                    if not state.get("desired_live"):
                        await websocket.send_text(json.dumps({
                            "type": "log",
                            "data": {
                                "level": "warning",
                                "message": (
                                    f"extension WS handler {msg_type!r} is not live: "
                                    f"{state.get('reason')}"
                                ),
                            },
                        }))
                        continue
                    if state.get("action") == "extension_load_error" or not state.get("live_loaded"):
                        await websocket.send_text(json.dumps({
                            "type": "log",
                            "data": {
                                "level": "warning",
                                "message": (
                                    f"extension WS handler {msg_type!r} failed to go live: "
                                    f"{state.get('load_error') or state.get('reason')}"
                                ),
                            },
                        }))
                        continue
                    handler_spec = _ws_handlers().get(msg_type)
                except Exception:
                    handler_spec = None
                if handler_spec is None:
                    extra = ""
                    if isinstance(state, dict) and state.get("action") == "extension_load_error":
                        extra = f" (load_error={state.get('load_error')})"
                    await websocket.send_text(json.dumps({
                        "type": "log",
                        "data": {
                            "level": "warning",
                            "message": f"no extension WS handler for {msg_type!r}{extra}",
                        },
                    }))
                    continue
                handler = handler_spec.get("handler")
                try:
                    result = handler(msg) if callable(handler) else None
                    if inspect.iscoroutine(result):
                        result = await result
                    if result is not None:
                        await websocket.send_text(
                            json.dumps({"type": msg_type + ".reply", "data": result})
                        )
                except Exception as exc:
                    await websocket.send_text(json.dumps({
                        "type": "log",
                        "data": {
                            "level": "error",
                            "message": f"extension WS handler {msg_type!r} raised: {type(exc).__name__}: {exc}",
                        },
                    }))
                continue

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
        from ouroboros.config import get_runtime_mode, get_skills_repo_path
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
            # Phase 2 plumbing: surface the runtime-mode axis to the UI
            # so Settings and the nav shell can render mode-aware copy
            # without re-reading settings.json. Skills-repo path is
            # surfaced as a boolean so the UI can show "configured"
            # without leaking the absolute path.
            "runtime_mode": get_runtime_mode(),
            "skills_repo_configured": bool(get_skills_repo_path()),
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_settings_get(request: Request) -> JSONResponse:
    settings, _, _ = apply_runtime_provider_defaults(load_settings())
    safe = {k: v for k, v in settings.items()}
    for key in _SECRET_SETTING_KEYS:
        if safe.get(key):
            safe[key] = _mask_secret_value(safe[key])
    # Inject read-only runtime network metadata for the Settings UI hint
    try:
        port = int(PORT_FILE.read_text().strip()) if PORT_FILE.exists() else DEFAULT_PORT
    except (ValueError, OSError):
        port = DEFAULT_PORT
    safe["_meta"] = _build_network_meta(_BIND_HOST, port)
    return JSONResponse(safe)


async def api_onboarding(request: Request) -> Response:
    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(load_settings())
    if provider_defaults_changed:
        save_settings(settings, allow_elevation=True)
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

        # Single source of truth for the SDK baseline — mirrors the launcher
        # bootstrap probe so web/onboarding repair installs the same version
        # that the launcher repair path installs. Imported at call time (rather
        # than at module load) so the error raises a clean 500 from the install
        # endpoint instead of breaking server startup; but NO defaulted literal
        # fallback is kept — that would reintroduce the drift this SSOT was
        # meant to eliminate (one edit to `_CLAUDE_SDK_BASELINE` and one here
        # would diverge silently). If the import truly fails, the runtime is
        # already unusable and the caller should see the error.
        from ouroboros.launcher_bootstrap import _CLAUDE_SDK_BASELINE as sdk_baseline

        result = await asyncio.to_thread(
            lambda: _sp.run(
                [interpreter, "-m", "pip", "install", "--upgrade", sdk_baseline],
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
        old_settings = load_settings()
        current = _merge_settings_payload(old_settings, body)
        # Phase 2: normalize the new runtime-mode axis on the save path so a
        # typo like ``{"OUROBOROS_RUNTIME_MODE": "turbo"}`` cannot land in
        # settings.json. The same normalizer runs on the read side
        # (``get_runtime_mode``), so /api/settings, /api/state, and the UI
        # segmented control stay in lockstep.
        from ouroboros.config import normalize_runtime_mode as _norm_runtime_mode
        # v5.1.2 elevation ratchet: belt-and-braces. ``_merge_settings_payload``
        # already skips ``OUROBOROS_RUNTIME_MODE`` so the body cannot influence
        # it, but if a future contributor adds a side channel we still want
        # the saved mode to match the on-disk old value, not the request body.
        current["OUROBOROS_RUNTIME_MODE"] = _norm_runtime_mode(
            old_settings.get("OUROBOROS_RUNTIME_MODE")
        )
        # Skills-repo path is opaque text; trim incidental whitespace so the
        # "configured vs empty" boolean in /api/state stays deterministic.
        current["OUROBOROS_SKILLS_REPO_PATH"] = str(
            current.get("OUROBOROS_SKILLS_REPO_PATH") or ""
        ).strip()
        try:
            from ouroboros.server_auth import is_loopback_host
            desired_host = str(current.get("OUROBOROS_SERVER_HOST") or "").strip()
            desired_password = str(current.get("OUROBOROS_NETWORK_PASSWORD") or "").strip()
            allowed_saved_hosts = {"", "127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0", "::", "[::]"}
            if desired_host and desired_host not in allowed_saved_hosts:
                return JSONResponse(
                    {
                        "error": (
                            "Server Bind Host in Settings supports localhost or wildcard "
                            "binds only (127.0.0.1 or 0.0.0.0). Specific LAN IP binds "
                            "are manual/env-only so the desktop launcher can keep using "
                            "a reliable loopback health check."
                        )
                    },
                    status_code=400,
                )
            if desired_host and not is_loopback_host(desired_host) and not desired_password:
                return JSONResponse(
                    {
                        "error": (
                            "Setting a non-localhost Server Bind Host through the web UI "
                            "requires a Network Password in the same save. For manual "
                            "trusted-lab/Docker setups, stop Ouroboros and edit "
                            "settings.json or environment variables directly."
                        )
                    },
                    status_code=400,
                )
            current_effective_host = (
                str(_BIND_HOST or "").strip()
                or str(os.environ.get("OUROBOROS_SERVER_HOST") or "").strip()
            )
            old_password = str(old_settings.get("OUROBOROS_NETWORK_PASSWORD") or "").strip()
            if (
                current_effective_host
                and not is_loopback_host(current_effective_host)
                and old_password
                and not desired_password
            ):
                return JSONResponse(
                    {
                        "error": (
                            "Cannot clear Network Password while the running server is "
                            "still bound to a non-localhost interface. First save a "
                            "loopback Server Bind Host and restart, then clear the password."
                        )
                    },
                    status_code=400,
                )
        except Exception:
            log.warning("Could not validate network bind settings", exc_info=True)
        current, provider_defaults_changed, provider_default_keys = apply_runtime_provider_defaults(current)
        if str(current.get("LOCAL_MODEL_SOURCE", "") or "").strip() and not has_supervisor_provider(current):
            return JSONResponse(
                {"error": "Local-only setups must route at least one model to the local runtime."},
                status_code=400,
            )
        # Detect what actually changed before saving.
        all_changed = [
            k for k in current
            if str(current.get(k, "") or "") != str(old_settings.get(k, "") or "")
        ]
        restart_keys = _classify_settings_changes(old_settings, current)

        save_settings(current)
        _apply_settings_to_env(current)
        _start_supervisor_if_needed(current)

        # Phase 4: when OUROBOROS_SKILLS_REPO_PATH changed, reconcile the
        # extension loader against the new path so stale registrations
        # from the previous path are torn down and any enabled +
        # PASS-reviewed extensions at the new path come up live. Hot-
        # reload pattern mirrors the other "next task" plumbing below.
        try:
            from ouroboros.extension_loader import reload_all as _reload_extensions
            new_path = str(current.get("OUROBOROS_SKILLS_REPO_PATH") or "").strip()
            old_path = str(old_settings.get("OUROBOROS_SKILLS_REPO_PATH") or "").strip()
            new_runtime_mode = str(current.get("OUROBOROS_RUNTIME_MODE") or "").strip()
            old_runtime_mode = str(old_settings.get("OUROBOROS_RUNTIME_MODE") or "").strip()
            if new_path != old_path or new_runtime_mode != old_runtime_mode:
                # Use ``load_settings`` rather than ``lambda: current``
                # so extensions see fresh settings on subsequent reads
                # (capturing ``current`` would freeze the snapshot at
                # settings-save time and drift from disk on later
                # edits).
                from ouroboros.config import load_settings as _load_settings
                _reload_extensions(
                    pathlib.Path(DATA_DIR),
                    _load_settings,
                    repo_path=new_path or None,
                )
        except Exception:
            log.warning("Extension reload after settings change failed", exc_info=True)

        # Hot-reload supervisor globals that can change without restart.
        try:
            from supervisor.state import refresh_budget_from_settings
            refresh_budget_from_settings(current)
        except Exception:
            pass
        try:
            from supervisor.queue import refresh_timeouts_from_settings
            refresh_timeouts_from_settings(current)
        except Exception:
            pass
        try:
            from supervisor.message_bus import refresh_budget_limit
            raw_budget = current.get("TOTAL_BUDGET")
            new_budget = float(raw_budget) if raw_budget is not None else 0.0
            refresh_budget_limit(new_budget)
        except Exception:
            pass

        warnings = []
        if provider_defaults_changed:
            change_kind = classify_runtime_provider_change(old_settings, current)
            # Reverse migration (OpenRouter added back, :: → /) is silent
            # housekeeping — the old warning text was misleading in that case.
            if change_kind == "direct_normalize":
                warnings.append(
                    "Normalized direct-provider routing because OpenRouter is not configured for the active provider."
                )
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().configure_from_settings(current)
        except Exception:
            pass
        try:
            from ouroboros.server_auth import is_loopback_host
            desired_host = str(current.get("OUROBOROS_SERVER_HOST") or "").strip()
            desired_password = str(current.get("OUROBOROS_NETWORK_PASSWORD") or "").strip()
            if desired_host and not is_loopback_host(desired_host) and not desired_password:
                warnings.append(
                    "Server Bind Host is non-localhost and Network Password is empty; "
                    "after restart the app will be reachable on the network without a password."
                )
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
        immediate_changed = [k for k in all_changed if k in _IMMEDIATE_KEYS]
        next_task_changed = [
            k for k in all_changed
            if k not in _IMMEDIATE_KEYS and k not in _RESTART_REQUIRED_KEYS
        ]
        resp: Dict[str, Any] = {"status": "saved"}
        if not all_changed:
            resp["no_changes"] = True
        if restart_keys:
            resp["restart_required"] = True
            resp["restart_keys"] = restart_keys
        if immediate_changed:
            resp["immediate_changed"] = True
        if next_task_changed:
            resp["next_task_changed"] = True
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
            from supervisor.message_bus import get_bridge, log_chat
            bridge = get_bridge()
            visible_text = str(body.get("visible_text") or "").strip()
            bridge.ui_send(cmd, broadcast=False, suppress_chat_log=bool(visible_text))
            if visible_text:
                task_id = str(body.get("visible_task_id") or "skill_repair")
                ts = datetime.now(timezone.utc).isoformat()
                payload = {
                    "type": "chat",
                    "role": "system",
                    "content": visible_text,
                    "ts": ts,
                    "source": "skill_repair",
                    "system_type": "skill_repair",
                    "task_id": task_id,
                }
                broadcast_ws_sync(payload)
                log_chat(
                    "system",
                    0,
                    0,
                    visible_text,
                    ts=ts,
                    source="skill_repair",
                    task_id=task_id,
                )
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
    """Promote the current dev branch to the runtime's stable branch."""
    try:
        import subprocess as sp
        branch_dev, branch_stable = _runtime_branch_defaults()
        sp.run(["git", "branch", "-f", branch_stable, branch_dev],
               cwd=str(REPO_DIR), check=True, capture_output=True)
        return JSONResponse({"status": "ok", "message": f"{branch_stable} updated to match {branch_dev}"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_update_status(request: Request) -> JSONResponse:
    """Return passive managed-update status without fetching."""
    try:
        from supervisor.git_ops import compute_managed_update_status, git_capture
        status = compute_managed_update_status(fetch=False)
        latest_version = ""
        target_ref = status.get("target_ref") or ""
        if target_ref and status.get("latest_sha"):
            rc, version_text, _ = git_capture(["git", "show", f"{target_ref}:VERSION"])
            if rc == 0:
                latest_version = version_text.strip()
        return JSONResponse({
            "current_version": get_version(),
            "latest_version": latest_version,
            "official_tags": [],
            **status,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_update_check(request: Request) -> JSONResponse:
    """Fetch the managed remote and return fresh update status."""
    try:
        from supervisor.git_ops import compute_managed_update_status, git_capture, list_official_update_tags
        status = compute_managed_update_status(fetch=True)
        latest_version = ""
        target_ref = status.get("target_ref") or ""
        if target_ref and status.get("latest_sha"):
            rc, version_text, _ = git_capture(["git", "show", f"{target_ref}:VERSION"])
            if rc == 0:
                latest_version = version_text.strip()
        return JSONResponse({
            "current_version": get_version(),
            "latest_version": latest_version,
            "official_tags": list_official_update_tags(),
            **status,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_update_apply(request: Request) -> JSONResponse:
    """Prepare a managed update and restart so safe_restart applies it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        strategy = str(body.get("strategy") or "replace")
        from supervisor.git_ops import BRANCH_DEV, checkout_and_reset, prepare_managed_update
        ok, payload = prepare_managed_update(strategy)
        if not ok:
            return JSONResponse(payload, status_code=409)
        checkout_ok, checkout_msg = checkout_and_reset(
            BRANCH_DEV,
            reason="ui_update_apply",
            unsynced_policy="ignore",
        )
        if not checkout_ok:
            return JSONResponse(
                {"error": f"Prepared update but checkout failed: {checkout_msg}", **payload},
                status_code=409,
            )
        _request_restart_exit()
        return JSONResponse({"status": "ok", "restarting": True, **payload})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


_evo_cache: Dict[str, Any] = {}
_evo_task: Optional[asyncio.Task] = None


async def api_evolution_data(request: Request) -> JSONResponse:
    """Collect evolution metrics for each git tag."""
    from ouroboros.utils import collect_evolution_metrics
    import time as _t
    global _evo_task

    now = _t.time()
    force_refresh = str(request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
    if not force_refresh and _evo_cache.get("ts") and now - _evo_cache["ts"] < 60:
        return JSONResponse({
            "points": _evo_cache["points"],
            "generated_at": _evo_cache.get("generated_at", ""),
            "cached": True,
        })

    data_dir = os.environ.get("OUROBOROS_DATA_DIR", os.path.expanduser("~/Ouroboros/data"))
    if _evo_task is None or _evo_task.done():
        _evo_task = asyncio.create_task(
            collect_evolution_metrics(str(REPO_DIR), data_dir=data_dir)
        )
    data_points = await _evo_task
    _evo_cache["ts"] = _t.time()
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
    api_local_model_install_runtime,
)
from ouroboros.chat_upload_api import api_chat_upload, api_chat_upload_delete


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
web_dir = resolve_web_dir(REPO_DIR)
web_dir.mkdir(parents=True, exist_ok=True)
index_page = make_index_page(web_dir)

from ouroboros.extensions_api import (
    api_extensions_index,
    api_extension_manifest,
    api_extension_dispatch,
    api_skill_toggle,
    api_skill_review,
    api_skill_grants,
    api_skill_reconcile,
    api_skill_lifecycle_queue,
)
from ouroboros.marketplace_api import (
    api_marketplace_search,
    api_marketplace_info,
    api_marketplace_preview,
    api_marketplace_install,
    api_marketplace_update,
    api_marketplace_uninstall,
    api_marketplace_installed,
    api_ouroboroshub_catalog,
    api_ouroboroshub_preview,
    api_ouroboroshub_install,
    api_ouroboroshub_update,
    api_ouroboroshub_installed,
    api_ouroboroshub_uninstall,
)


# v5.0.0: native-skill version-upgrade migration banner endpoints.
# When ``ensure_data_skills_seeded`` upgrades a launcher-shipped seed
# skill in place (e.g. weather 0.1 -> 0.2), it writes a record to
# ``data/state/migrations.json`` so the Skills UI can render a banner
# explaining the change. These endpoints expose that record + a
# dismiss path so the banner doesn't re-fire forever.
async def api_migrations_list(request: Request) -> JSONResponse:
    """Return the list of unread upgrade migration records."""
    import json as _json
    from starlette.responses import JSONResponse as _JR
    from ouroboros.config import DATA_DIR
    target = pathlib.Path(DATA_DIR) / "state" / "migrations.json"
    if not target.is_file():
        return _JR({"migrations": []})
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return _JR({"migrations": []})
    except Exception:
        return _JR({"migrations": []})
    out = []
    for key, record in data.items():
        if not isinstance(record, dict):
            continue
        if record.get("dismissed"):
            continue
        out.append({"key": str(key), **{k: v for k, v in record.items() if k != "dismissed"}})
    return _JR({"migrations": out})


async def api_migrations_dismiss(request: Request) -> JSONResponse:
    """Mark a migration record as dismissed so the banner stops firing."""
    import json as _json
    from starlette.responses import JSONResponse as _JR
    from ouroboros.config import DATA_DIR
    key = (request.path_params.get("key") or "").strip()
    # Same path-param hygiene as the marketplace surface.
    if not key or key in {".", ".."} or "/" in key or "\\" in key or "\x00" in key:
        return _JR({"error": "invalid migration key"}, status_code=400)
    target = pathlib.Path(DATA_DIR) / "state" / "migrations.json"
    if not target.is_file():
        return _JR({"ok": True, "key": key, "note": "no migrations file"})
    try:
        data = _json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    record = data.get(key)
    if isinstance(record, dict):
        record["dismissed"] = True
        data[key] = record
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            _json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return _JR({"error": str(exc)}, status_code=500)
    return _JR({"ok": True, "key": key})

routes = [
    Route("/", endpoint=index_page),
    Route("/api/health", endpoint=api_health),
    Route("/api/state", endpoint=api_state),
    # Phase 5: extension catalogue + per-skill manifest endpoint + a
    # catch-all dispatcher that forwards to whatever an extension
    # registered via ``PluginAPI.register_route``. The catch-all MUST
    # appear after the more specific routes so ``/manifest`` is
    # preferred over ``/<rest>``.
    Route("/api/extensions", endpoint=api_extensions_index, methods=["GET"]),
    Route(
        "/api/extensions/{skill}/manifest",
        endpoint=api_extension_manifest,
        methods=["GET"],
    ),
    Route(
        "/api/extensions/{skill}/{rest:path}",
        endpoint=api_extension_dispatch,
        methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"],
    ),
    Route(
        "/api/skills/{skill}/toggle",
        endpoint=api_skill_toggle,
        methods=["POST"],
    ),
    Route(
        "/api/skills/lifecycle-queue",
        endpoint=api_skill_lifecycle_queue,
        methods=["GET"],
    ),
    Route(
        "/api/skills/{skill}/review",
        endpoint=api_skill_review,
        methods=["POST"],
    ),
    Route(
        "/api/skills/{skill}/grants",
        endpoint=api_skill_grants,
        methods=["POST"],
    ),
    Route(
        "/api/skills/{skill}/reconcile",
        endpoint=api_skill_reconcile,
        methods=["POST"],
    ),
    # v4.50+: ClawHub marketplace surface (always-on, registry-host gated).
    Route(
        "/api/marketplace/clawhub/search",
        endpoint=api_marketplace_search,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/clawhub/installed",
        endpoint=api_marketplace_installed,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/clawhub/info/{slug:path}",
        endpoint=api_marketplace_info,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/clawhub/preview/{slug:path}",
        endpoint=api_marketplace_preview,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/clawhub/install",
        endpoint=api_marketplace_install,
        methods=["POST"],
    ),
    Route(
        "/api/marketplace/clawhub/update/{name}",
        endpoint=api_marketplace_update,
        methods=["POST"],
    ),
    Route(
        "/api/marketplace/clawhub/uninstall/{name}",
        endpoint=api_marketplace_uninstall,
        methods=["POST"],
    ),
    Route(
        "/api/marketplace/ouroboroshub/catalog",
        endpoint=api_ouroboroshub_catalog,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/ouroboroshub/installed",
        endpoint=api_ouroboroshub_installed,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/ouroboroshub/preview/{slug:path}",
        endpoint=api_ouroboroshub_preview,
        methods=["GET"],
    ),
    Route(
        "/api/marketplace/ouroboroshub/install",
        endpoint=api_ouroboroshub_install,
        methods=["POST"],
    ),
    Route(
        "/api/marketplace/ouroboroshub/update/{name}",
        endpoint=api_ouroboroshub_update,
        methods=["POST"],
    ),
    Route(
        "/api/marketplace/ouroboroshub/uninstall/{name}",
        endpoint=api_ouroboroshub_uninstall,
        methods=["POST"],
    ),
    # v5: native-skill upgrade migration banner endpoints (Opus
    # critic finding O-2 — operator must be told when a launcher
    # bump silently rewrites an installed skill type).
    Route(
        "/api/migrations",
        endpoint=api_migrations_list,
        methods=["GET"],
    ),
    Route(
        "/api/migrations/{key}/dismiss",
        endpoint=api_migrations_dismiss,
        methods=["POST"],
    ),
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
    Route("/api/update/status", endpoint=api_update_status),
    Route("/api/update/check", endpoint=api_update_check, methods=["POST"]),
    Route("/api/update/apply", endpoint=api_update_apply, methods=["POST"]),
    Route("/api/cost-breakdown", endpoint=api_cost_breakdown),
    Route("/api/evolution-data", endpoint=api_evolution_data),
    Route("/api/chat/history", endpoint=api_chat_history),
    Route("/api/chat/upload", endpoint=api_chat_upload, methods=["POST"]),
    Route("/api/chat/upload", endpoint=api_chat_upload_delete, methods=["DELETE"]),
    Route("/api/local-model/start", endpoint=api_local_model_start, methods=["POST"]),
    Route("/api/local-model/stop", endpoint=api_local_model_stop, methods=["POST"]),
    Route("/api/local-model/status", endpoint=api_local_model_status),
    Route("/api/local-model/test", endpoint=api_local_model_test, methods=["POST"]),
    Route("/api/local-model/install-runtime", endpoint=api_local_model_install_runtime, methods=["POST"]),
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
        save_settings(settings, allow_elevation=True)
    _apply_settings_to_env(settings)
    # v5.1.2 elevation ratchet: pin the boot-time runtime-mode baseline AFTER
    # initial settings load + env apply so the ``save_settings`` chokepoint
    # compares incoming saves against this owner-fixed value rather than
    # against on-disk old (which an out-of-process write could corrupt).
    from ouroboros.config import initialize_runtime_mode_baseline
    initialize_runtime_mode_baseline()
    has_local = has_local_routing(settings)

    # v4.50: seed ``data/skills/native/`` from ``repo/skills/`` on first
    # launch. The launcher already does this for packaged builds; calling
    # it here makes source-mode (``python server.py``) installs land at
    # the same layout so the Skills/Marketplace UI sees a consistent tree.
    try:
        from ouroboros.launcher_bootstrap import ensure_data_skills_seeded
        ensure_data_skills_seeded()
        from ouroboros.skill_migrations import migrate_generation_skill_names
        migrate_generation_skill_names()
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
        drive_root = pathlib.Path(DATA_DIR)
        _reload_extensions(drive_root, _load_settings, repo_path=repo_path or None)
    except Exception:
        log.warning("Extension reload_all at startup failed", exc_info=True)

    # A2A server — disabled by default; enable in Settings → Integrations
    a2a_server_task = None
    if settings.get("A2A_ENABLED", False):
        try:
            from ouroboros.a2a_server import start_a2a_server
            from ouroboros.server_auth import is_loopback_host
            a2a_host = str(settings.get("A2A_HOST", "127.0.0.1")).strip()
            a2a_port = int(settings.get("A2A_PORT", 18800))
            if not is_loopback_host(a2a_host):
                from ouroboros.server_auth import get_configured_network_password
                if not get_configured_network_password():
                    log.warning(
                        "A2A server binding to non-loopback host %s without a network password. "
                        "NetworkAuthGate is applied — set OUROBOROS_NETWORK_PASSWORD to require "
                        "authentication, or keep A2A_HOST=127.0.0.1 for loopback-only access.",
                        a2a_host,
                    )
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
                with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                    await asyncio.wait_for(a2a_server_task, timeout=5)
                # Sweep the port in case uvicorn left it in TIME_WAIT so the
                # next launch can bind A2A_PORT immediately (mirrors panic-stop).
                try:
                    from ouroboros.platform_layer import kill_process_on_port
                    kill_process_on_port(a2a_port)
                except Exception:
                    pass
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
app.app.state.drive_root = pathlib.Path(DATA_DIR)  # type: ignore[attr-defined]
app.app.state.repo_dir = pathlib.Path(REPO_DIR)  # type: ignore[attr-defined]


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
    try:
        saved_host = str(load_settings().get("OUROBOROS_SERVER_HOST") or "").strip()
    except Exception:
        saved_host = ""
    default_host = os.environ.get("OUROBOROS_SERVER_HOST", "").strip() or saved_host or DEFAULT_HOST
    args = parse_server_args(default_host, DEFAULT_PORT)
    global _BIND_HOST
    _BIND_HOST = args.host
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

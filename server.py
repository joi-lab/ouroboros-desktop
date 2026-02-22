"""
Ouroboros Agent Server — Self-editable entry point.

This file lives in REPO_DIR and can be modified by the agent.
It runs as a subprocess of the launcher, serving the web UI and
coordinating the supervisor/worker system.

Starlette + uvicorn on localhost:{PORT}.
"""

import asyncio
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
from starlette.responses import JSONResponse, HTMLResponse, FileResponse
from starlette.routing import Route, Mount, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

import uvicorn

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", pathlib.Path(__file__).parent))
DATA_DIR = pathlib.Path(os.environ.get("OUROBOROS_DATA_DIR",
    pathlib.Path.home() / "Ouroboros" / "data"))
PORT = int(os.environ.get("OUROBOROS_SERVER_PORT", "8765"))

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
_restart_requested = threading.Event()

# ---------------------------------------------------------------------------
# WebSocket connections manager
# ---------------------------------------------------------------------------
_ws_clients: List[WebSocket] = []
_ws_lock = threading.Lock()


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


# ---------------------------------------------------------------------------
# Settings (single source of truth: ouroboros.config)
# ---------------------------------------------------------------------------
from ouroboros.config import (
    SETTINGS_DEFAULTS as _SETTINGS_DEFAULTS,
    load_settings, save_settings, apply_settings_to_env as _apply_settings_to_env,
)


# ---------------------------------------------------------------------------
# Supervisor integration
# ---------------------------------------------------------------------------
_supervisor_ready = threading.Event()
_supervisor_error: Optional[str] = None
_event_loop: Optional[asyncio.AbstractEventLoop] = None


def _run_supervisor(settings: dict) -> None:
    """Initialize and run the supervisor loop. Called in a background thread."""
    global _supervisor_error

    _apply_settings_to_env(settings)

    try:
        from supervisor.message_bus import init as telegram_init
        from supervisor.message_bus import LocalChatBridge

        bridge = LocalChatBridge()
        bridge._broadcast_fn = broadcast_ws_sync

        from ouroboros.utils import set_log_sink
        set_log_sink(bridge.push_log)

        telegram_init(
            drive_root=DATA_DIR,
            total_budget_limit=float(settings.get("TOTAL_BUDGET", 10.0)),
            budget_report_every=10,
            tg_client=bridge,
        )

        from supervisor.state import init as state_init, init_state, load_state, save_state
        from supervisor.state import append_jsonl, update_budget_from_usage, rotate_chat_log_if_needed
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
            log.error("Supervisor bootstrap failed: %s", msg)

        from supervisor.queue import (
            enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
            persist_queue_snapshot, restore_pending_from_snapshot,
            cancel_task_by_id, queue_review_task, sort_pending,
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

        _event_ctx = types.SimpleNamespace(
            DRIVE_ROOT=DATA_DIR, REPO_DIR=REPO_DIR,
            BRANCH_DEV="ouroboros", BRANCH_STABLE="ouroboros-stable",
            TG=bridge, WORKERS=WORKERS, PENDING=PENDING, RUNNING=RUNNING,
            MAX_WORKERS=max_workers,
            send_with_budget=send_with_budget, load_state=load_state, save_state=save_state,
            update_budget_from_usage=update_budget_from_usage, append_jsonl=append_jsonl,
            enqueue_task=enqueue_task, cancel_task_by_id=cancel_task_by_id,
            queue_review_task=queue_review_task, persist_queue_snapshot=persist_queue_snapshot,
            safe_restart=safe_restart, kill_workers=kill_workers, spawn_workers=spawn_workers,
            sort_pending=sort_pending, consciousness=_consciousness,
            request_restart=_request_restart_exit,
        )
    except Exception as exc:
        _supervisor_error = f"Supervisor init failed: {exc}"
        log.critical("Supervisor initialization failed", exc_info=True)
        _supervisor_ready.set()
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

            # Process messages from WebSocket bridge
            updates = bridge.get_updates(offset=offset, timeout=1)
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

                from supervisor.message_bus import log_chat
                log_chat("in", chat_id, user_id, text)
                st["last_owner_message_at"] = now_iso
                save_state(st)

                if not text:
                    continue

                lowered = text.strip().lower()
                if lowered.startswith("/panic"):
                    send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
                    kill_workers(force=True)
                    _request_restart_exit()
                elif lowered.startswith("/restart"):
                    send_with_budget(chat_id, "♻️ Restarting (soft).")
                    ok, restart_msg = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
                    if not ok:
                        send_with_budget(chat_id, f"⚠️ Restart cancelled: {restart_msg}")
                        continue
                    kill_workers()
                    _request_restart_exit()
                elif lowered.startswith("/review"):
                    queue_review_task(reason="owner:/review", force=True)
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
                    send_with_budget(chat_id, f"🧬 Evolution: {state_str}")
                elif lowered.startswith("/bg"):
                    parts = lowered.split()
                    action = parts[1] if len(parts) > 1 else "status"
                    if action in ("start", "on", "1"):
                        result = _consciousness.start()
                        send_with_budget(chat_id, f"🧠 {result}")
                    elif action in ("stop", "off", "0"):
                        result = _consciousness.stop()
                        send_with_budget(chat_id, f"🧠 {result}")
                    else:
                        bg_status = "running" if _consciousness.is_running else "stopped"
                        send_with_budget(chat_id, f"🧠 Background consciousness: {bg_status}")
                elif lowered.startswith("/status"):
                    from supervisor.state import status_text
                    status = status_text(WORKERS, PENDING, RUNNING, soft_timeout, hard_timeout)
                    send_with_budget(chat_id, status, force_budget=True)
                else:
                    _consciousness.inject_observation(f"Owner message: {text[:100]}")
                    agent = _get_chat_agent()
                    if agent._busy:
                        agent.inject_message(text)
                    else:
                        _consciousness.pause()
                        def _run_and_resume(cid, txt):
                            try:
                                handle_chat_direct(cid, txt, None)
                            finally:
                                _consciousness.resume()
                        threading.Thread(
                            target=_run_and_resume, args=(chat_id, text), daemon=True,
                        ).start()

            crash_count = 0
            time.sleep(0.5)

        except Exception as exc:
            crash_count += 1
            log.error("Supervisor loop crash #%d: %s", crash_count, exc, exc_info=True)
            if crash_count >= 3:
                log.critical("Supervisor exceeded max retries.")
                return
            time.sleep(min(30, 2 ** crash_count))


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
    ctx.kill_workers()
    st2 = ctx.load_state()
    st2["session_id"] = uuid.uuid4().hex
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    _request_restart_exit()


def _request_restart_exit() -> None:
    """Signal the server to shut down with restart exit code."""
    _restart_requested.set()


# ---------------------------------------------------------------------------
# HTTP/WebSocket routes
# ---------------------------------------------------------------------------
APP_START = time.time()


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
            if msg_type == "chat":
                text = msg.get("content", "")
                if text:
                    from supervisor.message_bus import get_tg
                    tg = get_tg()
                    tg.ui_send(text)
            elif msg_type == "command":
                cmd = msg.get("cmd", "")
                if cmd:
                    from supervisor.message_bus import get_tg
                    tg = get_tg()
                    tg.ui_send(cmd)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("WebSocket error: %s", e)
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(websocket)
            except ValueError:
                pass
        log.info("WebSocket client disconnected (total: %d)", len(_ws_clients))


async def api_health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": _read_version()})


async def api_state(request: Request) -> JSONResponse:
    try:
        from supervisor.state import load_state, budget_remaining, budget_pct, TOTAL_BUDGET_LIMIT
        from supervisor.workers import WORKERS, PENDING, RUNNING
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
            "evolution_cycle": int(st.get("evolution_cycle") or 0),
            "spent_calls": int(st.get("spent_calls") or 0),
            "supervisor_ready": _supervisor_ready.is_set(),
            "supervisor_error": _supervisor_error,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_settings_get(request: Request) -> JSONResponse:
    settings = load_settings()
    safe = {k: v for k, v in settings.items()}
    for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GITHUB_TOKEN"):
        if safe.get(key):
            safe[key] = safe[key][:8] + "..." if len(safe[key]) > 8 else "***"
    return JSONResponse(safe)


async def api_settings_post(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        current = load_settings()
        for key in _SETTINGS_DEFAULTS:
            if key in body:
                current[key] = body[key]
        save_settings(current)
        _apply_settings_to_env(current)
        return JSONResponse({"status": "saved"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=400)


async def api_reset(request: Request) -> JSONResponse:
    """Reset all runtime data (state, memory, logs, settings) but keep repo.

    After reset the launcher will show the onboarding wizard on next start.
    """
    import shutil
    try:
        deleted = []
        for subdir in ("state", "memory", "logs", "archive", "locks", "task_results"):
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
            from supervisor.message_bus import get_tg
            tg = get_tg()
            tg.ui_send(cmd)
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


async def index_page(request: Request) -> FileResponse:
    index = REPO_DIR / "web" / "index.html"
    if index.exists():
        return FileResponse(str(index), media_type="text/html")
    return HTMLResponse("<html><body><h1>Ouroboros — web/ not found</h1></body></html>", status_code=404)


from ouroboros.config import read_version as _read_version


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
web_dir = REPO_DIR / "web"
web_dir.mkdir(parents=True, exist_ok=True)

routes = [
    Route("/", endpoint=index_page),
    Route("/api/health", endpoint=api_health),
    Route("/api/state", endpoint=api_state),
    Route("/api/settings", endpoint=api_settings_get, methods=["GET"]),
    Route("/api/settings", endpoint=api_settings_post, methods=["POST"]),
    Route("/api/command", endpoint=api_command, methods=["POST"]),
    Route("/api/reset", endpoint=api_reset, methods=["POST"]),
    Route("/api/git/log", endpoint=api_git_log),
    Route("/api/git/rollback", endpoint=api_git_rollback, methods=["POST"]),
    Route("/api/git/promote", endpoint=api_git_promote, methods=["POST"]),
    WebSocketRoute("/ws", endpoint=ws_endpoint),
    Mount("/static", app=StaticFiles(directory=str(web_dir)), name="static"),
]

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    settings = load_settings()
    if settings.get("OPENROUTER_API_KEY"):
        threading.Thread(target=_run_supervisor, args=(settings,), daemon=True).start()
    else:
        _supervisor_ready.set()
        log.info("No API key configured. Supervisor not started.")

    yield

    log.info("Server shutting down...")
    try:
        from supervisor.workers import kill_workers
        kill_workers(force=True)
    except Exception:
        pass


app = Starlette(routes=routes, lifespan=lifespan)


# ---------------------------------------------------------------------------
# Port selection
# ---------------------------------------------------------------------------
PORT_FILE = DATA_DIR / "state" / "server_port"


def _find_free_port(start: int = 8765, max_tries: int = 10) -> int:
    """Try binding to *start* with SO_REUSEADDR (survives TIME_WAIT after restart).
    Falls back to scanning subsequent ports if the default is truly occupied."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", start))
            return start
        except OSError:
            pass
    for offset in range(1, max_tries):
        port = start + offset
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    return start


def _write_port_file(port: int) -> None:
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    PORT_FILE.write_text(str(port), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    actual_port = _find_free_port(PORT)
    if actual_port != PORT:
        log.info("Port %d busy, using %d instead", PORT, actual_port)
    _write_port_file(actual_port)
    log.info("Starting Ouroboros server on port %d", actual_port)
    config = uvicorn.Config(app, host="127.0.0.1", port=actual_port, log_level="warning")
    server = uvicorn.Server(config)

    def _check_restart():
        """Monitor for restart signal, then shut down uvicorn."""
        while not _restart_requested.is_set():
            time.sleep(0.5)
        log.info("Restart requested — shutting down server.")
        server.should_exit = True

    threading.Thread(target=_check_restart, daemon=True).start()

    server.run()

    if _restart_requested.is_set():
        log.info("Exiting with code %d (restart signal).", RESTART_EXIT_CODE)
        # Force-kill any remaining worker processes before exit
        try:
            from supervisor.workers import kill_workers
            kill_workers(force=True)
        except Exception:
            pass
        # Kill any remaining multiprocessing children
        import multiprocessing, signal
        for child in multiprocessing.active_children():
            try:
                os.kill(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        # Hard exit — sys.exit() can hang if threads/children are stuck
        os._exit(RESTART_EXIT_CODE)

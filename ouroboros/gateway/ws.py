"""WebSocket gateway dispatch and broadcast state."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import pathlib
import threading
from typing import Any

from starlette.websockets import WebSocket, WebSocketDisconnect

from ouroboros.config import DATA_DIR
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_ws_clients: list[WebSocket] = []
_ws_lock = threading.Lock()
_event_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """Set the server event loop used by ``broadcast_ws_sync``."""
    global _event_loop
    _event_loop = loop


def has_ws_clients() -> bool:
    with _ws_lock:
        return bool(_ws_clients)


async def close_all_ws(*, code: int = 1012, reason: str = "Server restarting") -> None:
    """Close every connected browser websocket best-effort."""
    with _ws_lock:
        clients = list(_ws_clients)
    for ws in clients:
        try:
            await ws.close(code=code, reason=reason)
        except Exception:
            pass


async def broadcast_ws(msg: dict) -> None:
    """Send a message to all connected WebSocket clients."""
    data = json.dumps(msg, ensure_ascii=False, default=str)
    msg_type = str(msg.get("type", "unknown"))
    with _ws_lock:
        clients = list(_ws_clients)
        total_clients = len(clients)
    dead = []
    for ws in clients:
        try:
            await ws.send_text(data)
        except Exception as exc:
            log.info(
                "WebSocket send failed for msg type=%s; dropping client (%s)",
                msg_type,
                type(exc).__name__,
            )
            dead.append(ws)
    if dead:
        with _ws_lock:
            for ws in dead:
                try:
                    _ws_clients.remove(ws)
                except ValueError:
                    pass
        try:
            from ouroboros.utils import append_jsonl

            append_jsonl(
                pathlib.Path(DATA_DIR) / "logs" / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "broadcast_partial_failure",
                    "msg_type": msg_type,
                    "dead_clients": len(dead),
                    "total_clients": total_clients,
                },
            )
        except Exception:
            log.debug("Failed to emit broadcast_partial_failure event", exc_info=True)


def broadcast_ws_sync(msg: dict) -> None:
    """Thread-safe sync wrapper for broadcasting."""
    loop = _event_loop
    if loop is None:
        return
    try:
        asyncio.run_coroutine_threadsafe(broadcast_ws(msg), loop)
    except RuntimeError:
        pass


async def _dispatch_extension_message(
    websocket: WebSocket,
    msg: dict[str, Any],
    msg_type: str,
) -> bool:
    """Return True when an extension handler owned this message."""
    parsed_ext_type = None
    if isinstance(msg_type, str):
        try:
            from ouroboros.extension_loader import parse_extension_surface_name

            parsed_ext_type = parse_extension_surface_name(msg_type)
        except Exception:
            parsed_ext_type = None
    if not parsed_ext_type:
        return False

    state = None
    try:
        from ouroboros.config import get_skills_repo_path, load_settings
        from ouroboros.extension_loader import (
            extension_name_prefix,
            list_ws_handlers,
            reconcile_extension,
        )
        from ouroboros.skill_loader import discover_skills

        drive_root = pathlib.Path(
            websocket.app.state.drive_root  # type: ignore[attr-defined]
            if hasattr(websocket.app, "state") and hasattr(websocket.app.state, "drive_root")
            else DATA_DIR
        )
        repo_path = get_skills_repo_path()
        handler_spec = list_ws_handlers().get(msg_type)
        skill_name = str((handler_spec or {}).get("skill") or "")
        if not skill_name:
            for skill in discover_skills(drive_root, repo_path=repo_path):
                if msg_type.startswith(extension_name_prefix(skill.name)):
                    skill_name = skill.name
                    break
        if not skill_name:
            raise KeyError(msg_type)
        state = reconcile_extension(skill_name, drive_root, load_settings, repo_path=repo_path)
        if not state.get("desired_live"):
            await websocket.send_text(json.dumps({"type": "log", "data": {"level": "warning", "message": f"extension WS handler {msg_type!r} is not live: {state.get('reason')}"}}))
            return True
        if state.get("action") == "extension_load_error" or not state.get("live_loaded"):
            await websocket.send_text(json.dumps({"type": "log", "data": {"level": "warning", "message": f"extension WS handler {msg_type!r} failed to go live: {state.get('load_error') or state.get('reason')}"}}))
            return True
        handler_spec = list_ws_handlers().get(msg_type)
    except Exception:
        handler_spec = None

    if handler_spec is None:
        extra = ""
        if isinstance(state, dict) and state.get("action") == "extension_load_error":
            extra = f" (load_error={state.get('load_error')})"
        await websocket.send_text(json.dumps({"type": "log", "data": {"level": "warning", "message": f"no extension WS handler for {msg_type!r}{extra}"}}))
        return True

    handler = handler_spec.get("handler")
    try:
        result = handler(msg) if callable(handler) else None
        if inspect.iscoroutine(result):
            result = await result
        if result is not None:
            await websocket.send_text(json.dumps({"type": msg_type + ".reply", "data": result}))
    except Exception as exc:
        await websocket.send_text(json.dumps({"type": "log", "data": {"level": "error", "message": f"extension WS handler {msg_type!r} raised: {type(exc).__name__}: {exc}"}}))
    return True


async def ws_endpoint(websocket: WebSocket) -> None:
    await websocket.accept()
    with _ws_lock:
        _ws_clients.append(websocket)
        total = len(_ws_clients)
    log.info("WebSocket client connected (total: %d)", total)
    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue

            msg_type = str(msg.get("type", "") or "")
            if await _dispatch_extension_message(websocket, msg, msg_type):
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
                    await websocket.send_text(json.dumps({
                        "type": "chat",
                        "role": "assistant",
                        "content": "⚠️ System is still initializing. Please wait a moment and try again.",
                        "ts": utc_now_iso(),
                    }))
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        log.warning("WebSocket error: %s", exc)
    finally:
        with _ws_lock:
            try:
                _ws_clients.remove(websocket)
            except ValueError:
                pass
            total = len(_ws_clients)
        log.info("WebSocket client disconnected (total: %d)", total)


__all__ = [
    "broadcast_ws",
    "broadcast_ws_sync",
    "close_all_ws",
    "has_ws_clients",
    "set_event_loop",
    "ws_endpoint",
]

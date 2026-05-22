"""Loopback-only Host Service API for privileged skill callbacks."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import pathlib
import re
import threading
import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict, Optional

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ouroboros.contracts.chat_id_policy import A2A_CHAT_ID_MAX, A2A_CHAT_ID_MIN
from ouroboros.event_bus import get_global_event_bus
from ouroboros.skill_loader import (
    find_skill,
    grant_status_for_skill,
    load_enabled,
    review_status_allows_execution,
)
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)
_json_error = lambda message, status=500: JSONResponse({"ok": False, "error": message}, status_code=status)

DEFAULT_HOST_SERVICE_HOST = "127.0.0.1"
DEFAULT_HOST_SERVICE_PORT = 8767
AUTH_TOKEN_FILENAME = "auth_token.json"
_SLASH_COMMAND_RE = re.compile(r"^\s*/[A-Za-z]")


class HostServiceAuthError(Exception):
    """Raised when a skill token cannot be authenticated."""


class _RateLimiter:
    def __init__(self, limit: int = 60, window_sec: float = 60.0):
        self.limit = limit
        self.window_sec = window_sec
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > self.window_sec:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


class HostServiceContext:
    """Mutable host-service dependencies kept injectable for tests."""

    def __init__(
        self,
        data_dir: pathlib.Path,
        *,
        bridge_getter: Optional[Callable[[], Any]] = None,
        tool_schemas_getter: Optional[Callable[[], list[dict[str, Any]]]] = None,
    ):
        self.data_dir = pathlib.Path(data_dir)
        self.bridge_getter = bridge_getter or self._default_bridge
        self.tool_schemas_getter = tool_schemas_getter or self._default_tool_schemas
        self.rate_limiter = _RateLimiter()
        self._inflight: Dict[str, int] = defaultdict(int)
        self._inflight_lock = threading.Lock()
        self._counter_lock = threading.Lock()

    def _default_bridge(self) -> Any:
        from supervisor.message_bus import try_get_bridge

        bridge = try_get_bridge()
        if bridge is None:
            raise RuntimeError("message bridge is not initialized")
        return bridge

    def _default_tool_schemas(self) -> list[dict[str, Any]]:
        try:
            from supervisor.workers import _get_chat_agent

            return list(_get_chat_agent().tools.schemas())
        except Exception:
            log.debug("Host service could not read tool schemas", exc_info=True)
            return []

    @property
    def skills_state_dir(self) -> pathlib.Path:
        return self.data_dir / "state" / "skills"

    def authenticate_token(self, raw_token: str) -> str:
        return self.authenticate_token_payload(raw_token)[0]

    def authenticate_token_payload(self, raw_token: str) -> tuple[str, Dict[str, Any]]:
        token = str(raw_token or "").strip()
        if not token:
            raise HostServiceAuthError("missing skill token")
        root = self.skills_state_dir
        if not root.exists():
            raise HostServiceAuthError("no skill tokens are registered")
        for skill_dir in root.iterdir():
            if not skill_dir.is_dir():
                continue
            payload = read_json_dict(skill_dir / AUTH_TOKEN_FILENAME) or {}
            expected = str(payload.get("token") or "")
            if expected and hmac.compare_digest(expected, token):
                self._assert_active_token(skill_dir.name, payload)
                return skill_dir.name, payload
        raise HostServiceAuthError("invalid skill token")

    def _assert_active_token(self, skill_name: str, token_payload: Dict[str, Any]) -> None:
        loaded = find_skill(self.data_dir, skill_name)
        if loaded is None:
            raise HostServiceAuthError(f"skill {skill_name!r} is not installed")
        if not review_status_allows_execution(loaded.review.status) or loaded.review.is_stale_for(loaded.content_hash):
            raise HostServiceAuthError(f"skill {skill_name!r} does not have a fresh executable review")
        if not load_enabled(self.data_dir, skill_name):
            raise HostServiceAuthError(f"skill {skill_name!r} is disabled")
        if str(token_payload.get("content_hash") or "") != str(loaded.content_hash or ""):
            raise HostServiceAuthError(f"skill {skill_name!r} token is stale")

    def require_permission(self, skill_name: str, token_payload: Dict[str, Any], permission: str) -> None:
        loaded = find_skill(self.data_dir, skill_name)
        if loaded is not None:
            status = grant_status_for_skill(self.data_dir, loaded)
            granted = set(status.get("granted_permissions") or [])
        else:
            raise HostServiceAuthError(f"skill {skill_name!r} is not installed")
        if permission.startswith("subscribe_event:"):
            topic = permission.split(":", 1)[1]
            declared = set(str(item or "").strip() for item in (loaded.manifest.subscribe_events or []))
            permissions = set(str(item or "").strip() for item in (loaded.manifest.permissions or []))
            if topic == "skill.lifecycle" and "subscribe_event" in permissions and topic in declared:
                return
        if permission not in granted:
            raise HostServiceAuthError(f"skill {skill_name!r} lacks grant {permission!r}")

    def _enter_inflight(self, skill_name: str, limit: int = 5) -> bool:
        with self._inflight_lock:
            current = self._inflight[skill_name]
            if current >= limit:
                return False
            self._inflight[skill_name] = current + 1
            return True

    def _leave_inflight(self, skill_name: str) -> None:
        with self._inflight_lock:
            self._inflight[skill_name] = max(0, self._inflight[skill_name] - 1)

    def allocate_internal_chat_id(self, skill_name: str, range_name: str) -> int:
        if range_name != "a2a":
            raise ValueError("unsupported internal chat id range")
        counter_path = self.skills_state_dir / skill_name / "chat_id_counter.json"
        with self._counter_lock:
            data = read_json_dict(counter_path) or {}
            next_id = int(data.get("next_chat_id") or A2A_CHAT_ID_MAX)
            chat_id = next_id
            if chat_id < A2A_CHAT_ID_MIN:
                chat_id = A2A_CHAT_ID_MAX
            atomic_write_json(
                counter_path,
                {
                    "range_name": range_name,
                    "last_chat_id": chat_id,
                    "next_chat_id": chat_id - 1,
                    "updated_at": utc_now_iso(),
                },
            )
            return chat_id


def _token_from_request(request: Request) -> str:
    return request.headers.get("x-skill-token", "")


def _token_from_websocket(websocket: WebSocket) -> str:
    header = websocket.headers.get("x-skill-token", "")
    if header:
        return header
    for protocol in websocket.scope.get("subprotocols") or []:
        text = str(protocol or "")
        prefix = "ouroboros.host.events.v1."
        if text.startswith(prefix):
            return text[len(prefix):]
    return ""


async def _api_identity(request: Request) -> JSONResponse:
    ctx: HostServiceContext = request.app.state.host_service_context
    try:
        ctx.authenticate_token(_token_from_request(request))
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    identity_path = ctx.data_dir / "memory" / "identity.md"
    name = "Ouroboros"
    description = ""
    try:
        if identity_path.exists():
            lines = identity_path.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if line.startswith("# "):
                    name = line.lstrip("# ").strip() or name
                    continue
                if line.strip() and not description:
                    description = line.strip()
                    break
    except Exception:
        log.debug("Failed to read identity for host service", exc_info=True)
    return JSONResponse({"ok": True, "name": name, "description": description})


async def _api_tool_schemas(request: Request) -> JSONResponse:
    ctx: HostServiceContext = request.app.state.host_service_context
    try:
        skill_name = ctx.authenticate_token(_token_from_request(request))
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    if not ctx.rate_limiter.allow(f"{skill_name}:tools"):
        return _json_error("rate limit exceeded", 429)
    schemas = ctx.tool_schemas_getter()
    return JSONResponse({"ok": True, "tools": schemas})


async def _api_allocate_internal(request: Request) -> JSONResponse:
    ctx: HostServiceContext = request.app.state.host_service_context
    try:
        skill_name, token_payload = ctx.authenticate_token_payload(_token_from_request(request))
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    try:
        ctx.require_permission(skill_name, token_payload, "inject_chat")
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    try:
        payload = await request.json()
        chat_id = ctx.allocate_internal_chat_id(skill_name, str(payload.get("range_name") or "a2a"))
    except Exception as exc:
        return _json_error(str(exc), 400)
    return JSONResponse({"ok": True, "chat_id": chat_id})


async def _api_chat_inject(request: Request) -> JSONResponse:
    ctx: HostServiceContext = request.app.state.host_service_context
    try:
        skill_name, token_payload = ctx.authenticate_token_payload(_token_from_request(request))
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    try:
        ctx.require_permission(skill_name, token_payload, "inject_chat")
    except HostServiceAuthError as exc:
        return _json_error(str(exc), 403)
    if not ctx.rate_limiter.allow(f"{skill_name}:inject"):
        return _json_error("rate limit exceeded", 429)
    if not ctx._enter_inflight(skill_name):
        return _json_error("too many in-flight inject requests", 429)
    subscription_id = ""
    try:
        payload = await request.json()
        text = str(payload.get("text") or "")
        image_caption = str(payload.get("image_caption") or "")
        if _SLASH_COMMAND_RE.match(text) or _SLASH_COMMAND_RE.match(image_caption):
            return _json_error("slash commands are reserved for direct owner input", 400)
        bridge = ctx.bridge_getter()
        chat_id = int(payload.get("chat_id") or 1)
        wait_for_response = bool(payload.get("wait_for_response", False))
        response_event: asyncio.Event = asyncio.Event()
        response_holder: dict[str, str] = {}
        if wait_for_response:
            loop = asyncio.get_running_loop()

            def on_response(response_text: str) -> None:
                response_holder["text"] = response_text
                loop.call_soon_threadsafe(response_event.set)

            subscription_id = bridge.subscribe_response(chat_id, on_response)
        bridge.enqueue_local_message(
            text,
            chat_id=chat_id,
            user_id=int(payload.get("user_id") or 1),
            source=f"skill:{skill_name}",
            sender_label=str(payload.get("sender_label") or skill_name),
            image_base64=str(payload.get("image_base64") or ""),
            image_mime=str(payload.get("image_mime") or ""),
            image_caption=image_caption,
            transport=payload.get("transport") if isinstance(payload.get("transport"), dict) else {},
        )
        if not wait_for_response:
            return JSONResponse({"ok": True, "status": "queued"}, status_code=202)
        timeout = max(1, min(int(payload.get("timeout_sec") or 1800), 1800))
        deadline = time.monotonic() + timeout
        while not response_event.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _json_error("timed out waiting for response", 504)
            try:
                if await request.is_disconnected():
                    return _json_error("client disconnected", 499)
                await asyncio.wait_for(response_event.wait(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue
        return JSONResponse({"ok": True, "response": response_holder.get("text", "")})
    except json.JSONDecodeError:
        return _json_error("invalid json", 400)
    except Exception as exc:
        log.debug("Host service chat inject failed", exc_info=True)
        return _json_error(str(exc), 500)
    finally:
        if subscription_id:
            try:
                ctx.bridge_getter().unsubscribe_response(subscription_id)
            except Exception:
                log.debug("Failed to unsubscribe host-service response callback", exc_info=True)
        ctx._leave_inflight(skill_name)


async def _ws_events(websocket: WebSocket) -> None:
    ctx: HostServiceContext = websocket.app.state.host_service_context
    try:
        skill_name, token_payload = ctx.authenticate_token_payload(_token_from_websocket(websocket))
    except HostServiceAuthError:
        await websocket.close(code=1008)
        return
    offered = set(websocket.scope.get("subprotocols") or [])
    selected_protocol = "ouroboros.host.events.v1" if "ouroboros.host.events.v1" in offered else None
    await websocket.accept(subprotocol=selected_protocol)
    subscriptions: list[str] = []
    loop = asyncio.get_running_loop()
    try:
        while True:
            message = await websocket.receive_json()
            if message.get("type") == "ping":
                await websocket.send_json({"type": "pong", "skill": skill_name})
            elif message.get("type") == "subscribe":
                topic = str(message.get("topic") or "")
                try:
                    ctx.require_permission(skill_name, token_payload, f"subscribe_event:{topic}")
                except HostServiceAuthError as exc:
                    await websocket.send_json({"type": "error", "error": str(exc)})
                    continue

                subscribed_topic = topic

                def _send_event(payload: Dict[str, Any], event_topic: str = subscribed_topic) -> None:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "event", "topic": event_topic, "data": payload}),
                        loop,
                    )

                sub_id = get_global_event_bus().subscribe(skill_name, topic, _send_event)
                subscriptions.append(sub_id)
                await websocket.send_json({"type": "subscribed", "topic": topic})
            else:
                await websocket.send_json({"type": "error", "error": "unsupported message type"})
    except WebSocketDisconnect:
        return
    finally:
        bus = get_global_event_bus()
        for sub_id in subscriptions:
            bus.unsubscribe(sub_id)


def create_host_service_app(
    data_dir: pathlib.Path,
    *,
    bridge_getter: Optional[Callable[[], Any]] = None,
    tool_schemas_getter: Optional[Callable[[], list[dict[str, Any]]]] = None,
) -> Starlette:
    app = Starlette(
        routes=[
            Route("/identity", _api_identity, methods=["GET"]),
            Route("/tools/schemas", _api_tool_schemas, methods=["GET"]),
            Route("/chat/allocate-internal", _api_allocate_internal, methods=["POST"]),
            Route("/chat/inject", _api_chat_inject, methods=["POST"]),
            WebSocketRoute("/events", _ws_events),
        ]
    )
    app.state.host_service_context = HostServiceContext(
        pathlib.Path(data_dir),
        bridge_getter=bridge_getter,
        tool_schemas_getter=tool_schemas_getter,
    )
    return app


def host_service_port() -> int:
    return int(os.environ.get("OUROBOROS_HOST_SERVICE_PORT", str(DEFAULT_HOST_SERVICE_PORT)))


__all__ = [
    "AUTH_TOKEN_FILENAME",
    "DEFAULT_HOST_SERVICE_HOST",
    "DEFAULT_HOST_SERVICE_PORT",
    "HostServiceContext",
    "create_host_service_app",
    "host_service_port",
]

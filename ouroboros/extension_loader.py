"""Load reviewed in-process ``type: extension`` skills through PluginAPI.

Extensions run inside Ouroboros, so imports are allowed only after a fresh
executable skill review, manifest permissions, and owner grants pass. All
registered surfaces are provider-safe namespaced and tracked per skill so
disable/reload can tear them down and purge modules cleanly.
"""

from __future__ import annotations

import copy
import importlib
import importlib.util
import inspect
import hashlib
import logging
import os
import pathlib
import re
import secrets
import shutil
import sys
import threading
import uuid
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, Dict, List, Optional, Sequence

from ouroboros.contracts.plugin_api import ExtensionRegistrationError, FORBIDDEN_EXTENSION_SETTINGS, VALID_EXTENSION_PERMISSIONS, VALID_EXTENSION_ROUTE_METHODS
from ouroboros.event_bus import get_global_event_bus
from ouroboros.extension_companion import CompanionDescriptor, get_global_supervisor, is_server_process
from ouroboros.extension_ui_validation import _assert_ws_message_type, validate_ui_render as _validate_ui_render
from ouroboros.gateway.host_service import AUTH_TOKEN_FILENAME
from ouroboros.extension_isolated_deps import _isolated_python_site_dirs, async_isolated_site_dirs_scope, isolated_site_dirs_scope, is_skill_cache_path
from ouroboros.skill_loader import _SKILL_DIR_CACHE_NAMES, LoadedSkill, SkillPayloadUnreadable, compute_content_hash, discover_skills, find_skill, grant_status_for_skill, requested_core_setting_keys, skill_review_gate, skill_state_dir
from ouroboros.skill_token import SkillToken
from ouroboros.tools.skill_exec import _scrub_env
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)


# Registration bookkeeping.


@dataclass
class _ExtensionRegistrations:
    """Attached surfaces owned by one loaded extension."""

    tools: List[str] = field(default_factory=list)
    routes: List[str] = field(default_factory=list)
    ws_handlers: List[str] = field(default_factory=list)
    ui_tabs: List[str] = field(default_factory=list)
    settings_sections: List[str] = field(default_factory=list)
    unload_callbacks: List[Callable[[], Any]] = field(default_factory=list)
    event_subscriptions: List[str] = field(default_factory=list)
    companion_names: List[str] = field(default_factory=list)
    supervised_futures: List[Any] = field(default_factory=list)
    api_instances: List[Any] = field(default_factory=list)
    content_hash: Optional[str] = None
    skill_dir: Optional[str] = None
    import_root: Optional[str] = None


@dataclass
class _ExtensionLoadFailure:
    content_hash: str
    skill_dir: str
    error: str


@dataclass
class _PluginAPIConfig:
    skill_name: str
    permissions: Sequence[str]
    env_allowlist: Sequence[str]
    state_dir: pathlib.Path
    settings_reader: Callable[[], Dict[str, Any]]
    granted_keys: Sequence[str] | None = None
    subscribe_events: Sequence[str] | None = None
    companion_processes: Sequence[Dict[str, Any]] | None = None
    skill_dir: pathlib.Path | None = None
    runtime_skill_dir: pathlib.Path | None = None
    dependency_site_dirs_enabled: bool = False


# Lock-guarded registries; per-surface maps keep unload proportional to one extension.
_lock = threading.RLock()
_extensions: Dict[str, _ExtensionRegistrations] = {}
_extension_modules: Dict[str, ModuleType] = {}
_load_failures: Dict[str, _ExtensionLoadFailure] = {}
_unloading: set[str] = set()
_lifecycle_locks: Dict[str, threading.RLock] = {}
_tools: Dict[str, Any] = {}            # {"ext_<len>_<token>_<name>": ToolEntry-like}
_routes: Dict[str, Any] = {}           # {"/api/extensions/<skill>/<path>": handler_spec}
_ws_handlers: Dict[str, Any] = {}      # {"ext_<len>_<token>_<message_type>": handler}
_ui_tabs: Dict[str, Any] = {}          # {"<skill>:<tab_id>": tab_spec}
# Declarative settings sections keyed like UI tabs.
_settings_sections: Dict[str, Any] = {}
_ws_broadcaster: Optional[Callable[[dict], None]] = None
_EXTENSION_NAME_PREFIX = "ext_"
_EXTENSION_SKILL_TOKEN_MAX = 32
_EXTENSION_SHORT_MAX = 24
_EXTENSION_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _extension_skill_token(skill_name: str) -> str:
    """Return a short ASCII token without changing skill identity."""
    text = str(skill_name or "").strip()
    safe = "".join(ch if (ch.isascii() and (ch.isalnum() or ch in "-_")) else "_" for ch in text)
    safe = re.sub(r"_+", "_", safe).strip("_-")
    raw_budget = _EXTENSION_SKILL_TOKEN_MAX - 2
    if safe and safe == text and len(safe) <= raw_budget:
        return f"r_{safe}"
    digest = hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    prefix_budget = _EXTENSION_SKILL_TOKEN_MAX - len(digest) - 3
    prefix = (safe or "skill")[:prefix_budget].strip("_-") or "skill"
    return f"h_{prefix}_{digest}"


def extension_name_prefix(skill_name: str) -> str:
    """Return the provider-safe prefix for one extension."""
    token = _extension_skill_token(skill_name)
    return f"{_EXTENSION_NAME_PREFIX}{len(token)}_{token}_"


def extension_surface_name(skill_name: str, short_name: str) -> str:
    """Return a provider-safe canonical surface name."""
    full = f"{extension_name_prefix(skill_name)}{short_name}"
    if not _EXTENSION_NAME_RE.match(full):
        raise ExtensionRegistrationError(
            f"extension surface name {full!r} must match provider tool-name limits"
        )
    return full


def parse_extension_surface_name(name: str) -> tuple[str, str] | None:
    """Return ``(encoded_skill_token, short_name)`` for extension surface names."""
    text = str(name or "").strip()
    if not _EXTENSION_NAME_RE.match(text) or not text.startswith(_EXTENSION_NAME_PREFIX):
        return None
    rest = text[len(_EXTENSION_NAME_PREFIX):]
    length_text, sep, remainder = rest.partition("_")
    if sep != "_" or not length_text.isdigit():
        return None
    token_len = int(length_text)
    if token_len < 1 or len(remainder) <= token_len or remainder[token_len] != "_":
        return None
    token = remainder[:token_len]
    short = remainder[token_len + 1:]
    return token, short


def _lifecycle_lock_for(skill_name: str) -> threading.RLock:
    with _lock:
        lock = _lifecycle_locks.get(skill_name)
        if lock is None:
            lock = threading.RLock()
            _lifecycle_locks[skill_name] = lock
        return lock


def _run_unload_callback(skill_name: str, callback: Callable[[], Any], timeout_sec: float = 2.0) -> None:
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            callback()
        except BaseException as exc:  # pragma: no cover - surfaced via log
            errors.append(exc)

    thread = threading.Thread(target=runner, name=f"ouroboros-ext-unload-{skill_name}", daemon=True)
    thread.start()
    thread.join(timeout=timeout_sec)
    if thread.is_alive():
        log.warning("extension %s unload callback timed out after %.1fs", skill_name, timeout_sec)
        return
    if errors:
        exc = errors[0]
        log.warning("extension %s unload callback failed", skill_name, exc_info=(type(exc), exc, exc.__traceback__))


# PluginAPI implementation.


def _assert_namespace_path(path: str) -> str:
    """Return a normalised relative path for route registration or raise."""
    rel = str(path or "").strip()
    if not rel:
        raise ExtensionRegistrationError("path must be non-empty")
    if rel.startswith("/"):
        raise ExtensionRegistrationError(
            f"path must be relative, not absolute: {rel!r}"
        )
    if ".." in pathlib.PurePosixPath(rel).parts:
        raise ExtensionRegistrationError(
            f"path must not contain '..' segments: {rel!r}"
        )
    return rel


def _assert_tool_name(name: str) -> str:
    candidate = str(name or "").strip()
    if not candidate:
        raise ExtensionRegistrationError("tool name must be non-empty")
    if len(candidate) > _EXTENSION_SHORT_MAX:
        raise ExtensionRegistrationError(
            f"tool name must be <= {_EXTENSION_SHORT_MAX} characters: {candidate!r}"
        )
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(
            f"tool name must be alnum/underscore only: {candidate!r}"
        )
    return candidate


def _widget_span_from_render(render: Dict[str, Any]) -> int:
    """Normalize optional UI-card width metadata from a render declaration."""
    raw = render.get("span", render.get("grid_span", 1))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return 2 if value >= 2 else 1


def set_ws_broadcaster(broadcaster: Callable[[dict], None] | None) -> None:
    """Install the host WebSocket broadcaster used by PluginAPI.send_ws_message."""
    global _ws_broadcaster
    with _lock:
        _ws_broadcaster = broadcaster


class PluginAPIImpl:
    """PluginAPI bound to one skill, permission set, and state dir."""

    def __init__(self, config: _PluginAPIConfig | None = None, **legacy: Any) -> None:
        if config is None:
            config = _PluginAPIConfig(**legacy)
        self._skill = config.skill_name
        self._permissions = frozenset(str(p).strip() for p in (config.permissions or []))
        self._env_allow = frozenset(str(k).strip() for k in (config.env_allowlist or []))
        self._env_allow_upper = frozenset(k.upper() for k in self._env_allow)
        self._state_dir = pathlib.Path(config.state_dir)
        self._subscribe_events = frozenset(str(t).strip() for t in (config.subscribe_events or []) if str(t).strip())
        self._companion_specs = {
            str(item.get("name") or "").strip(): dict(item)
            for item in (config.companion_processes or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        }
        # Keep runtime_info cheap and tied to the loaded payload.
        self._skill_dir = pathlib.Path(config.skill_dir) if config.skill_dir is not None else None
        self._runtime_skill_dir = pathlib.Path(config.runtime_skill_dir) if config.runtime_skill_dir is not None else self._skill_dir
        self._dependency_site_dirs_enabled = bool(config.dependency_site_dirs_enabled)
        self._settings_reader = config.settings_reader
        self._registration_closed = False
        self._runtime_closing = False
        self._runtime_closed = False
        self._api_lock = threading.RLock()
        # Core settings are exposed only when a content-hash-bound owner grant
        # was already verified; otherwise the denylist silently drops them.
        self._granted_upper = frozenset(
            str(k).strip().upper() for k in (config.granted_keys or []) if str(k).strip()
        )

    # --- internal helpers ---

    def _require(self, perm: str) -> None:
        with _lock:
            self._require_open_locked()
        if perm not in VALID_EXTENSION_PERMISSIONS:
            raise ExtensionRegistrationError(
                f"unknown extension permission {perm!r}"
            )
        if perm not in self._permissions:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot {perm!r} "
                f"— manifest permissions={sorted(self._permissions)}"
            )

    def _require_open_locked(self) -> None:
        if self._registration_closed or self._runtime_closing or self._runtime_closed or self._skill in _unloading:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot register after unload has started"
            )

    def _wrap_runtime_handler(self, handler: Callable[..., Any]) -> Callable[..., Any]:
        if self._skill_dir is None:
            return handler

        if inspect.iscoroutinefunction(handler):
            async def _async_wrapped(*args: Any, **kwargs: Any) -> Any:
                async with async_isolated_site_dirs_scope(
                    self._skill_dir,
                    enabled=self._dependency_site_dirs_enabled,
                ):
                    return await handler(*args, **kwargs)

            return _async_wrapped

        def _wrapped(*args: Any, **kwargs: Any) -> Any:
            with isolated_site_dirs_scope(self._skill_dir, enabled=self._dependency_site_dirs_enabled):
                result = handler(*args, **kwargs)
                return result

        return _wrapped

    def _register_surface_locked(
        self,
        registry: Dict[str, Any],
        key: str,
        value: Dict[str, Any],
        bundle_attr: str,
        label: str,
    ) -> None:
        self._require_open_locked()
        if key in registry:
            raise ExtensionRegistrationError(f"{label} {key!r} already registered")
        registry[key] = value
        getattr(_extensions.setdefault(self._skill, _ExtensionRegistrations()), bundle_attr).append(key)

    # --- registration ---

    def register_tool(
        self,
        name: str,
        handler: Callable[..., str],
        *,
        description: str,
        schema: Dict[str, Any],
        timeout_sec: int = 60,
    ) -> None:
        self._require("tool")
        short = _assert_tool_name(name)
        full = extension_surface_name(self._skill, short)
        with _lock:
            self._register_surface_locked(_tools, full, {
                "name": full,
                "handler": self._wrap_runtime_handler(handler),
                "description": str(description or ""),
                "schema": dict(schema or {}),
                "timeout_sec": max(1, int(timeout_sec)),
                "skill": self._skill,
            }, "tools", "tool")

    def register_route(
        self,
        path: str,
        handler: Callable[..., Any],
        *,
        methods: Sequence[str] = ("GET",),
    ) -> None:
        self._require("route")
        rel = _assert_namespace_path(path)
        methods_iter = (methods,) if isinstance(methods, str) else (methods or ())
        norm_methods = tuple(
            dict.fromkeys(
                str(m).strip().upper()
                for m in methods_iter
                if str(m).strip()
            )
        )
        if not norm_methods:
            raise ExtensionRegistrationError("route methods must be non-empty")
        invalid_methods = [m for m in norm_methods if m not in VALID_EXTENSION_ROUTE_METHODS]
        if invalid_methods:
            raise ExtensionRegistrationError(
                f"route methods {invalid_methods!r} are unsupported; "
                f"expected subset of {sorted(VALID_EXTENSION_ROUTE_METHODS)}"
            )
        mount = f"/api/extensions/{self._skill}/{rel}"
        with _lock:
            self._register_surface_locked(_routes, mount, {
                "path": mount,
                "handler": self._wrap_runtime_handler(handler),
                "methods": norm_methods,
                "skill": self._skill,
            }, "routes", "route")

    def register_ws_handler(
        self,
        message_type: str,
        handler: Callable[..., Any],
    ) -> None:
        self._require("ws_handler")
        short = _assert_ws_message_type(message_type)
        full = extension_surface_name(self._skill, short)
        with _lock:
            self._register_surface_locked(_ws_handlers, full, {
                "type": full,
                "handler": self._wrap_runtime_handler(handler),
                "skill": self._skill,
            }, "ws_handlers", "ws handler")

    def register_ui_tab(
        self,
        tab_id: str,
        title: str,
        *,
        icon: str = "extension",
        render: Dict[str, Any] | None = None,
    ) -> None:
        self._require("widget")
        clean_tab = _assert_tool_name(tab_id)  # same syntax rules
        key = f"{self._skill}:{clean_tab}"
        validated_render = _validate_ui_render({} if render is None else render)
        span = _widget_span_from_render(validated_render)
        with _lock:
            self._register_surface_locked(_ui_tabs, key, {
                "skill": self._skill,
                "tab_id": clean_tab,
                "title": str(title or clean_tab),
                "icon": str(icon or "extension"),
                "ws_prefix": extension_name_prefix(self._skill),
                "render": validated_render,
                "span": span,
                "grid_span": span,
                "ui_host_pending": True,
            }, "ui_tabs", "ui tab")

    def register_settings_section(
        self,
        section_id: str,
        title: str,
        *,
        schema: Dict[str, Any],
    ) -> None:
        """Validate and register a declarative Settings UI section."""
        # Settings sections share the widget permission and host-rendered schema.
        self._require("widget")
        clean_id = _assert_tool_name(section_id)
        key = f"{self._skill}:{clean_id}"
        # Settings stay declarative-only and narrower than widgets.
        allowed = {"form", "action", "markdown", "json"}
        components = list((schema or {}).get("components") or [])
        for idx, component in enumerate(components):
            if not isinstance(component, dict):
                raise ExtensionRegistrationError(
                    f"settings section component {idx} must be an object"
                )
            ctype = str(component.get("type") or "").strip()
            if ctype not in allowed:
                raise ExtensionRegistrationError(
                    f"settings section component {idx} type {ctype!r} is unsupported; "
                    f"expected one of {sorted(allowed)}"
                )
        validated = _validate_ui_render({
            "kind": "declarative",
            "schema_version": 1,
            "components": components,
        })
        with _lock:
            self._register_surface_locked(_settings_sections, key, {
                "skill": self._skill,
                "section_id": clean_id,
                "title": str(title or clean_id),
                "render": validated,
            }, "settings_sections", "settings section")

    def register_supervised_task(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        restart_policy: str = "on_failure",
        max_restarts: int = 5,
        backoff_seconds: float = 2.0,
    ) -> None:
        """Declare a server-owned supervised task; workers only record it."""
        self._require("supervised_task")
        clean_name = _assert_tool_name(name)
        future = None
        if is_server_process():
            loop = getattr(get_global_event_bus(), "_loop", None)
            if loop is not None and loop.is_running():
                import asyncio

                async def _runner() -> None:
                    restarts = 0
                    while True:
                        try:
                            result = factory()
                            if inspect.isawaitable(result):
                                await result
                            return
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            restarts += 1
                            if restart_policy != "on_failure" or restarts > max_restarts:
                                log.warning("supervised task %s/%s stopped after failure", self._skill, clean_name, exc_info=True)
                                return
                            await asyncio.sleep(max(0.1, float(backoff_seconds)))

                future = asyncio.run_coroutine_threadsafe(_runner(), loop)
        with _lock:
            self._require_open_locked()
            bundle = _extensions.setdefault(self._skill, _ExtensionRegistrations())
            bundle.companion_names.append(f"task:{clean_name}")
            if future is not None:
                bundle.supervised_futures.append(future)

    def register_companion_process(
        self,
        name: str,
    ) -> None:
        self._require("companion_process")
        clean_name = _assert_tool_name(name)
        spec = self._companion_specs.get(clean_name)
        if spec is None:
            raise ExtensionRegistrationError(
                f"companion {clean_name!r} is not declared in manifest.companion_processes"
            )
        expected_cmd = [str(part) for part in (spec.get("command") or []) if str(part)]
        expected_runtime = str(spec.get("runtime") or "").strip()
        cmd = list(expected_cmd)
        if not cmd:
            raise ExtensionRegistrationError("companion command must be declared in manifest")
        if expected_runtime in {"python", "python3"} and cmd[0] in {"python", "python3"}:
            cmd = [sys.executable, *cmd[1:]]
        if not is_server_process():
            with _lock:
                _extensions.setdefault(self._skill, _ExtensionRegistrations()).companion_names.append(
                    f"worker-skip:{clean_name}"
                )
            return
        supervisor = get_global_supervisor()
        if supervisor is None:
            raise ExtensionRegistrationError("companion supervisor is not initialized")
        base_env = _scrub_env(
            list(self._env_allow),
            self._state_dir,
            self._skill,
            granted_keys=list(self._granted_upper),
        )
        reserved_env = {"HOST_SERVICE_TOKEN", "HOST_SERVICE_URL"}
        for key, value in (spec.get("env") or {}).items():
            key_text = str(key)
            if key_text.upper() in FORBIDDEN_EXTENSION_SETTINGS or key_text.upper() in reserved_env:
                continue
            base_env[key_text] = str(value)
        token = self.get_skill_token()
        base_env["HOST_SERVICE_TOKEN"] = token.use_in_request()
        from ouroboros.gateway.host_service import DEFAULT_HOST_SERVICE_HOST, host_service_port
        base_env["HOST_SERVICE_URL"] = f"http://{DEFAULT_HOST_SERVICE_HOST}:{host_service_port()}"
        if self._skill_dir is not None:
            site_dirs = [str(path) for path in _isolated_python_site_dirs(self._skill_dir)]
            if site_dirs:
                existing_pythonpath = base_env.get("PYTHONPATH")
                base_env["PYTHONPATH"] = os.pathsep.join(
                    [*site_dirs, existing_pythonpath] if existing_pythonpath else site_dirs
                )
        workdir = self._runtime_skill_dir or self._skill_dir or self._state_dir
        descriptor = CompanionDescriptor(
            skill_name=self._skill,
            name=clean_name,
            command=cmd,
            cwd=workdir,
            env=base_env,
            ports=[int(port) for port in (spec.get("ports") or []) if str(port).isdigit()],
            restart_policy=str(spec.get("restart_policy") or "on_failure"),
            max_restarts=max(0, int(spec.get("max_restarts") or 5)),
        )
        supervisor.start(descriptor)
        with _lock:
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).companion_names.append(clean_name)

    def subscribe_event(self, topic: str, handler: Callable[[Dict[str, Any]], Any]) -> str:
        self._require("subscribe_event")
        topic = str(topic or "").strip()
        if topic not in self._subscribe_events:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot subscribe to undeclared topic {topic!r}"
            )
        sub_id = get_global_event_bus().subscribe(self._skill, topic, self._wrap_runtime_handler(handler))
        with _lock:
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).event_subscriptions.append(sub_id)
        return sub_id

    def send_ws_message(self, message_type: str, data: Dict[str, Any]) -> None:
        if "ws_handler" not in self._permissions:
            raise ExtensionRegistrationError(
                f"skill {self._skill!r} cannot 'ws_handler' "
                f"— manifest permissions={sorted(self._permissions)}"
            )
        short = _assert_ws_message_type(message_type)
        full = extension_surface_name(self._skill, short)
        payload = {"type": full, "data": dict(data or {}), "skill": self._skill}
        with self._api_lock:
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return
            broadcaster = _ws_broadcaster
            if broadcaster is None:
                log.debug("extension %s dropped WS message %s: no broadcaster", self._skill, full)
                return
            try:
                broadcaster(payload)
            except Exception:
                log.warning("extension %s WS broadcast failed for %s", self._skill, full, exc_info=True)

    def on_unload(self, callback: Callable[[], Any]) -> None:
        if not callable(callback):
            raise ExtensionRegistrationError("on_unload callback must be callable")
        with _lock:
            if self._registration_closed or self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                raise ExtensionRegistrationError(
                    f"skill {self._skill!r} cannot register unload callbacks after unload has started"
                )
            _extensions.setdefault(self._skill, _ExtensionRegistrations()).unload_callbacks.append(callback)

    def _close_registration(self) -> None:
        with _lock:
            self._registration_closed = True

    def _close_runtime_access(self) -> None:
        with _lock:
            self._registration_closed = True
            self._runtime_closing = True
        with self._api_lock:
            with _lock:
                self._runtime_closed = True

    # --- runtime access ---

    def log(self, level: str, message: str, **fields: Any) -> None:
        lvl = str(level or "info").lower()
        levels = {"debug": 10, "info": 20, "warning": 30, "error": 40}
        log.log(
            levels.get(lvl, 20),
            "[ext %s] %s %s",
            self._skill,
            message,
            fields if fields else "",
        )

    def get_settings(self, keys: Sequence[str]) -> Dict[str, Any]:
        with self._api_lock:
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return {}
            if "read_settings" not in self._permissions:
                # Missing permission fails closed without leaking key presence.
                return {}
            settings = self._settings_reader() or {}
            with _lock:
                if self._runtime_closing or self._runtime_closed or self._skill in _unloading:
                    return {}
            out: Dict[str, Any] = {}
            protected_upper = {k.upper() for k in FORBIDDEN_EXTENSION_SETTINGS}
            protected_upper.update(requested_core_setting_keys(list(self._env_allow)))
            for raw_key in keys or ():
                key = str(raw_key).strip()
                canonical = key.upper()
                if not key:
                    continue
                if canonical in protected_upper and canonical not in self._granted_upper:
                    # Do not reveal forbidden/core key presence without a grant.
                    continue
                if key not in self._env_allow and canonical not in self._env_allow_upper:
                    continue
                settings_key = canonical if canonical in protected_upper else key
                if settings_key in settings:
                    out[settings_key] = settings[settings_key]
            return out

    def get_state_dir(self) -> str:
        return str(self._state_dir)

    def skill_job_dir(self, job_id: str) -> pathlib.Path:
        raw = str(job_id or "").strip()
        safe = "".join(
            ch if ch.isalnum() or ch in "-_." else "_"
            for ch in raw
        ).strip("._")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        prefix = (safe or "_job")[:55].rstrip("._-") or "_job"
        safe = f"{prefix}-{digest}"
        root = self._state_dir / "jobs" / safe
        for child in ("assets", "output", "tmp"):
            (root / child).mkdir(parents=True, exist_ok=True)
        return root

    def get_skill_token(self) -> SkillToken:
        token_path = self._state_dir / AUTH_TOKEN_FILENAME
        payload = read_json_dict(token_path) or {}
        token = str(payload.get("token") or "")
        content_hash = ""
        if self._skill_dir is not None:
            try:
                content_hash = compute_content_hash(self._skill_dir)
            except Exception:
                content_hash = ""
        if not token or str(payload.get("content_hash") or "") != content_hash:
            token = secrets.token_urlsafe(32)
            atomic_write_json(
                token_path,
                {
                    "token": token,
                    "issued_at": utc_now_iso(),
                    "skill": self._skill,
                    "content_hash": content_hash,
                },
            )
            try:
                token_path.chmod(0o600)
            except OSError:
                log.debug("Failed to chmod skill token file %s", token_path, exc_info=True)
        return SkillToken(token)

    def get_runtime_info(self) -> Dict[str, Any]:
        """Return the PluginAPI runtime-info snapshot without manifest I/O."""
        try:
            from ouroboros.config import (
                get_runtime_mode as _get_runtime_mode,
                DATA_DIR as _DATA_DIR,
            )
            runtime_mode = _get_runtime_mode()
            data_dir = str(_DATA_DIR)
        except Exception:
            runtime_mode = "advanced"
            data_dir = ""
        try:
            from ouroboros import get_version as _get_version
            app_version = str(_get_version())
        except Exception:
            app_version = ""
        try:
            from ouroboros.config import AGENT_SERVER_PORT as _agent_port, PORT_FILE as _PORT_FILE
            server_port = 0
            try:
                port_text = pathlib.Path(_PORT_FILE).read_text(encoding="utf-8").strip()
                if port_text:
                    server_port = int(port_text)
            except Exception:
                server_port = 0
            if server_port <= 0:
                server_port = int(_agent_port)
        except Exception:
            server_port = 0
        skill_dir = str(getattr(self, "_skill_dir", "") or "")
        return {
            "runtime_mode": runtime_mode,
            "app_version": app_version,
            "data_dir": data_dir,
            "skill_dir": skill_dir,
            "state_dir": str(self._state_dir),
            "server_port": server_port,
        }


# Loader.


def _plugin_entry_path(skill: LoadedSkill) -> Optional[pathlib.Path]:
    """Resolve manifest.entry inside the skill directory."""
    entry = str(skill.manifest.entry or "").strip()
    if not entry:
        return None
    candidate = (skill.skill_dir / entry).resolve()
    try:
        candidate.relative_to(skill.skill_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def _module_key(skill_name: str) -> str:
    digest = hashlib.sha1(str(skill_name or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"ouroboros._extensions.m_{digest}"


def _purge_extension_bytecode(skill_dir: pathlib.Path) -> None:
    """Drop bytecode so rapid edits reload fresh source."""
    for pycache in skill_dir.rglob("__pycache__"):
        if pycache.is_dir():
            shutil.rmtree(pycache, ignore_errors=True)


def _stage_extension_import_tree(
    skill: LoadedSkill,
    *,
    state_dir: pathlib.Path,
    entry_path: pathlib.Path,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Stage an extension under a fresh import root to avoid stale module reuse."""
    resolved_root = skill.skill_dir.resolve()
    relative_entry = entry_path.relative_to(resolved_root)
    for path in sorted(skill.skill_dir.rglob("*")):
        if is_skill_cache_path(path, resolved_root):
            continue
        if not path.is_symlink():
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(resolved_root)
        except Exception as exc:
            raise RuntimeError(
                f"extension {skill.name!r} contains a symlink that resolves outside the skill tree: {path}"
            ) from exc
    import_root = state_dir / "__extension_imports" / uuid.uuid4().hex
    staged_skill_dir = import_root / "skill"
    import_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        skill.skill_dir,
        staged_skill_dir,
        ignore=shutil.ignore_patterns(*_SKILL_DIR_CACHE_NAMES),
    )
    _purge_extension_bytecode(staged_skill_dir)
    staged_entry = (staged_skill_dir / relative_entry).resolve()
    staged_entry.relative_to(staged_skill_dir.resolve())
    return import_root, staged_entry


def _sweep_stale_extension_imports(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    keep: Sequence[pathlib.Path] = (),
) -> None:
    """Remove orphan staged import trees without touching skill state/payload."""
    root = skill_state_dir(drive_root, skill_name) / "__extension_imports"
    if not root.exists() or not root.is_dir():
        return
    keep_resolved = set()
    for path in keep or ():
        try:
            keep_resolved.add(path.resolve(strict=False))
        except OSError:
            pass
    with _lock:
        bundle = _extensions.get(skill_name)
        if bundle and bundle.import_root:
            try:
                keep_resolved.add(pathlib.Path(bundle.import_root).resolve(strict=False))
            except OSError:
                pass
    for child in list(root.iterdir()):
        try:
            resolved = child.resolve(strict=False)
        except OSError:
            resolved = child
        if resolved in keep_resolved:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)


def _extension_runtime_state(
    skill: LoadedSkill,
    *,
    current_hash: str | None = None,
) -> Dict[str, Any]:
    """Return the liveness authority for one extension."""
    from ouroboros.config import get_runtime_mode

    hash_now = current_hash or skill.content_hash
    skill_dir_now = str(skill.skill_dir.resolve())
    review_stale = skill.review.is_stale_for(hash_now)
    with _lock:
        live_bundle = _extensions.get(skill.name)
        live_loaded = bool(
            live_bundle
            and live_bundle.content_hash == hash_now
            and live_bundle.skill_dir == skill_dir_now
        )
        loaded_present = live_bundle is not None
        load_failure = _load_failures.get(skill.name)
        matched_failure = bool(
            load_failure
            and load_failure.content_hash == hash_now
            and load_failure.skill_dir == skill_dir_now
        )

    review_gate = skill_review_gate(skill.review.status, stale=review_stale)
    reason = "ready"
    desired_live = True
    if not skill.manifest.is_extension():
        desired_live = False
        reason = "not_extension"
    elif skill.load_error:
        desired_live = False
        reason = "load_error"
    elif not skill.enabled:
        desired_live = False
        reason = "disabled"
    elif not review_gate["executable_review"]:
        desired_live = False
        reason = review_gate["blocking_reason"]
    # Light mode allows reviewed skills; it only gates repo mutation/escalation.
    elif matched_failure:
        reason = "load_error"

    return {
        "skill": skill.name,
        "type": skill.manifest.type,
        "runtime_mode": get_runtime_mode(),
        "enabled": skill.enabled,
        "review_status": skill.review.status,
        "review_stale": review_stale,
        "review_gate": review_gate,
        "executable_review": review_gate["executable_review"],
        "load_error": skill.load_error or (load_failure.error if matched_failure and load_failure else None),
        "desired_live": desired_live,
        "live_loaded": live_loaded,
        "loaded_present": loaded_present,
        "loaded_matches_current": live_loaded,
        "reason": reason,
    }


def _deps_block_reason(drive_root: pathlib.Path, skill: LoadedSkill) -> str:
    """Return the dependency block reason, if live dispatch must refuse load."""
    try:
        from ouroboros.marketplace.install_specs import install_specs_hash
        from ouroboros.marketplace.isolated_deps import read_deps_state
        from ouroboros.skill_dependencies import auto_install_specs_for_skill

        auto_specs = auto_install_specs_for_skill(drive_root, skill)
        if not auto_specs:
            return ""
        deps_state = read_deps_state(drive_root, skill.name, skill.skill_dir)
        status = str(deps_state.get("status") or "")
        if status != "installed":
            if status == "stale":
                return "deps_stale"
            return "deps_failed" if status == "failed" else "deps_missing"
        if deps_state.get("specs_hash") != install_specs_hash(auto_specs):
            return "deps_stale"
        return ""
    except Exception:
        log.debug("extension deps readiness probe failed", exc_info=True)
        return ""


def _apply_deps_block(state: Dict[str, Any], drive_root: pathlib.Path, skill: LoadedSkill) -> Dict[str, Any]:
    if state.get("desired_live"):
        deps_reason = _deps_block_reason(pathlib.Path(drive_root), skill)
        if deps_reason:
            state.update(desired_live=False, reason=deps_reason, load_error=deps_reason)
    return state


def runtime_state_for_skill_name(
    skill_name: str,
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    from ouroboros.config import get_skills_repo_path

    resolved_repo_path = get_skills_repo_path() if repo_path is None else repo_path
    skill = find_skill(drive_root, skill_name, repo_path=resolved_repo_path)
    if skill is None:
        with _lock:
            live_loaded = skill_name in _extensions
        return {
            "skill": skill_name,
            "type": "extension",
            "runtime_mode": "",
            "enabled": False,
            "review_status": "missing",
            "review_stale": True,
            "load_error": "skill not found",
            "desired_live": False,
            "live_loaded": live_loaded,
            "loaded_present": live_loaded,
            "loaded_matches_current": False,
            "reason": "missing",
        }
    return _apply_deps_block(_extension_runtime_state(skill), pathlib.Path(drive_root), skill)


def runtime_state_for_loaded_skill(skill: "LoadedSkill", drive_root: pathlib.Path | None = None) -> Dict[str, Any]:
    """Runtime state for an already-discovered skill; avoids repeated FS walks."""
    state = _extension_runtime_state(skill)
    return _apply_deps_block(state, pathlib.Path(drive_root), skill) if drive_root is not None else state


def is_extension_live(
    skill_name: str,
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
) -> bool:
    state = runtime_state_for_skill_name(skill_name, drive_root, repo_path=repo_path)
    return bool(state.get("desired_live")) and bool(state.get("live_loaded"))


def reconcile_extension(
    skill_name: str,
    drive_root: pathlib.Path,
    settings_reader: Callable[[], Dict[str, Any]],
    *,
    repo_path: str | None = None,
    retry_load_error: bool = False,
) -> Dict[str, Any]:
    """Reconcile one extension's desired and actual live state."""
    lifecycle_lock = _lifecycle_lock_for(skill_name)
    with lifecycle_lock:
        state = runtime_state_for_skill_name(skill_name, drive_root, repo_path=repo_path)
        loaded_present = bool(state.get("loaded_present"))
        was_live = bool(state.get("live_loaded"))
        if retry_load_error and state.get("reason") == "load_error" and not was_live:
            with _lock:
                _load_failures.pop(skill_name, None)
            state = runtime_state_for_skill_name(skill_name, drive_root, repo_path=repo_path)
            loaded_present = bool(state.get("loaded_present"))
            was_live = bool(state.get("live_loaded"))
        elif state.get("reason") == "load_error" and not loaded_present:
            state["action"] = "extension_load_error"
            return state
        if state.get("reason") == "missing" or state.get("reason") == "not_extension":
            if loaded_present:
                unload_extension(skill_name)
            state["action"] = "extension_unloaded" if loaded_present else "extension_inactive"
            state["live_loaded"] = False
            state["loaded_present"] = False
            return state

        if not state.get("desired_live"):
            if loaded_present:
                unload_extension(skill_name)
            state["action"] = "extension_unloaded" if loaded_present else "extension_inactive"
            state["live_loaded"] = False
            state["loaded_present"] = False
            return state

        if was_live:
            state["action"] = "extension_already_live"
            return state

        from ouroboros.config import get_skills_repo_path

        resolved_repo_path = get_skills_repo_path() if repo_path is None else repo_path
        loaded = find_skill(drive_root, skill_name, repo_path=resolved_repo_path)
        if loaded is None:
            state["reason"] = "missing"
            state["action"] = "extension_inactive"
            return state
        if loaded_present:
            unload_extension(skill_name)
        err = load_extension(loaded, settings_reader, drive_root=drive_root)
        if err:
            with _lock:
                _load_failures[skill_name] = _ExtensionLoadFailure(
                    content_hash=loaded.content_hash,
                    skill_dir=str(loaded.skill_dir.resolve()),
                    error=err,
                )
            state["reason"] = "load_error"
            state["load_error"] = err
            state["action"] = "extension_load_error"
            return state
        refreshed = runtime_state_for_skill_name(skill_name, drive_root, repo_path=resolved_repo_path)
        refreshed["action"] = "extension_loaded"
        return refreshed


def load_extension(
    skill: LoadedSkill,
    settings_reader: Callable[[], Dict[str, Any]],
    *,
    drive_root: Optional[pathlib.Path] = None,
) -> Optional[str]:
    """Load a fresh-reviewed enabled extension, returning a UI-safe error.

    ``drive_root`` must be explicit; defaulting to owner data would pollute
    tests and alternate-drive runtimes.
    """
    if drive_root is None:
        raise TypeError("load_extension requires explicit drive_root")
    if not skill.manifest.is_extension():
        return f"skill {skill.name!r} is not type=extension"
    if skill.load_error:
        return f"skill {skill.name!r} has load_error: {skill.load_error}"
    if not skill.enabled:
        return f"skill {skill.name!r} is disabled"
    try:
        current_hash = compute_content_hash(
            skill.skill_dir,
            manifest_entry=skill.manifest.entry,
            manifest_scripts=skill.manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        return (
            f"skill {skill.name!r} payload unreadable at load time: "
            f"{exc}. Fix filesystem state and re-enable."
        )
    runtime_state = _extension_runtime_state(skill, current_hash=current_hash)
    # Light mode permits reviewed extensions; stale review and other gates remain.
    gate = runtime_state.get("review_gate") or skill_review_gate(
        skill.review.status,
        stale=skill.review.content_hash != current_hash,
    )
    if not gate.get("executable_review", False):
        return (
            f"skill {skill.name!r} must carry a fresh executable review "
            f"(status={skill.review.status!r}, "
            f"stale={skill.review.content_hash != current_hash}, "
            f"reason={gate.get('blocking_reason')})"
        )
    if runtime_state["reason"] == "disabled":
        return f"skill {skill.name!r} is disabled"
    entry_path = _plugin_entry_path(skill)
    if entry_path is None:
        return (
            f"skill {skill.name!r} manifest.entry does not resolve to a "
            "file inside the skill directory"
        )

    drive_root = pathlib.Path(drive_root)
    state_dir = skill_state_dir(drive_root, skill.name)
    _sweep_stale_extension_imports(drive_root, skill.name)
    try:
        from ouroboros.skill_dependencies import auto_install_specs_for_skill

        auto_specs = auto_install_specs_for_skill(pathlib.Path(drive_root), skill)
    except Exception:
        log.debug("extension dependency spec probe failed for %s", skill.name, exc_info=True)
        auto_specs = []
    if auto_specs:
        deps_reason = _deps_block_reason(pathlib.Path(drive_root), skill)
        if deps_reason:
            return f"skill {skill.name!r} cannot load until isolated dependencies are ready: {deps_reason}"

    # Core settings and privileged host capabilities require hash-bound grants.
    grant_status = grant_status_for_skill(pathlib.Path(drive_root), skill)
    if not grant_status.get("all_granted", True):
        missing_bits = []
        if grant_status.get("missing_keys"):
            missing_bits.append(f"keys={grant_status.get('missing_keys')}")
        if grant_status.get("missing_permissions"):
            missing_bits.append(f"permissions={grant_status.get('missing_permissions')}")
        return (
            f"skill {skill.name!r} is missing owner grants for "
            f"{', '.join(missing_bits)}. Grant access from the Skills tab."
        )
    granted_core = list(grant_status.get("granted_keys") or [])
    staged_import_root: Optional[pathlib.Path] = None
    module_key = _module_key(skill.name)
    try:
        importlib.invalidate_caches()
        staged_import_root, entry_path = _stage_extension_import_tree(
            skill,
            state_dir=state_dir,
            entry_path=entry_path,
        )
        # Package-style spec preserves relative imports from the staged entry dir.
        spec = importlib.util.spec_from_file_location(
            module_key,
            entry_path,
            submodule_search_locations=[str(entry_path.parent)],
        )
        if spec is None or spec.loader is None:
            return f"skill {skill.name!r}: importlib could not build spec"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = module
        with isolated_site_dirs_scope(skill.skill_dir, enabled=bool(auto_specs)):
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if not callable(register):
                # Sibling imports may already be in sys.modules; purge the package.
                unload_extension(skill.name)
                return (
                    f"skill {skill.name!r} plugin.py does not export a "
                    "register(api) callable"
                )
            api = PluginAPIImpl(_PluginAPIConfig(
                skill_name=skill.name,
                permissions=list(skill.manifest.permissions or []),
                env_allowlist=list(skill.manifest.env_from_settings or []),
                state_dir=state_dir,
                settings_reader=settings_reader,
                granted_keys=granted_core,
                subscribe_events=list(getattr(skill.manifest, "subscribe_events", []) or []),
                companion_processes=list(getattr(skill.manifest, "companion_processes", []) or []),
                skill_dir=skill.skill_dir,
                runtime_skill_dir=(staged_import_root / "skill") if staged_import_root is not None else None,
                dependency_site_dirs_enabled=bool(auto_specs),
            ))
            with _lock:
                bundle = _extensions.get(skill.name)
                if bundle is None:
                    bundle = _ExtensionRegistrations()
                    _extensions[skill.name] = bundle
                bundle.content_hash = current_hash
                bundle.skill_dir = str(skill.skill_dir.resolve())
                bundle.import_root = str(staged_import_root) if staged_import_root is not None else None
                bundle.api_instances.append(api)
                _extension_modules[skill.name] = module
                _load_failures.pop(skill.name, None)
            register(api)
            api._close_registration()
    except ExtensionRegistrationError as exc:
        # Registration may be partial; always tear it down.
        unload_extension(skill.name)
        return f"skill {skill.name!r} registration error: {exc}"
    except Exception as exc:
        unload_extension(skill.name)
        log.exception("extension %s failed to load", skill.name)
        return f"skill {skill.name!r} load failure: {type(exc).__name__}: {exc}"
    finally:
        if skill.name not in _extensions:
            if staged_import_root is not None:
                shutil.rmtree(staged_import_root, ignore_errors=True)
    return None


def unload_extension(skill_name: str) -> None:
    lifecycle_lock = _lifecycle_lock_for(skill_name)
    with lifecycle_lock:
        _unload_extension_locked(skill_name)


def _unload_extension_locked(skill_name: str) -> None:
    """Remove one extension's surfaces and purge its package from sys.modules."""
    with _lock:
        bundle = _extensions.pop(skill_name, None)
        _extension_modules.pop(skill_name, None)
        import_root = pathlib.Path(bundle.import_root) if bundle and bundle.import_root else None
        callbacks = list(bundle.unload_callbacks) if bundle else []
        api_instances = list(bundle.api_instances) if bundle else []
        event_subscriptions = list(bundle.event_subscriptions) if bundle else []
        companion_names = list(bundle.companion_names) if bundle else []
        supervised_futures = list(bundle.supervised_futures) if bundle else []
        if bundle:
            _unloading.add(skill_name)
        if bundle:
            for key in bundle.tools:
                _tools.pop(key, None)
            for key in bundle.routes:
                _routes.pop(key, None)
            for key in bundle.ws_handlers:
                _ws_handlers.pop(key, None)
            for key in bundle.ui_tabs:
                _ui_tabs.pop(key, None)
            for key in bundle.settings_sections:
                _settings_sections.pop(key, None)
    bus = get_global_event_bus()
    for sub_id in event_subscriptions:
        bus.unsubscribe(sub_id)
    for future in supervised_futures:
        try:
            future.cancel()
        except Exception:
            log.debug("Failed to cancel supervised task for %s", skill_name, exc_info=True)
    supervisor = get_global_supervisor()
    if supervisor is not None:
        for raw_name in companion_names:
            name = str(raw_name or "")
            if name and not name.startswith(("task:", "worker-skip:")):
                supervisor.stop(skill_name, name)
    for api in api_instances:
        close = getattr(api, "_close_runtime_access", None)
        if callable(close):
            close()
    try:
        for callback in callbacks:
            _run_unload_callback(skill_name, callback)
        prefix = _module_key(skill_name)
        # Copy keys before mutating sys.modules.
        for mod_name in list(sys.modules.keys()):
            if mod_name == prefix or mod_name.startswith(prefix + "."):
                sys.modules.pop(mod_name, None)
        if import_root is not None:
            shutil.rmtree(import_root, ignore_errors=True)
    finally:
        with _lock:
            _unloading.discard(skill_name)


def reload_all(
    drive_root: pathlib.Path,
    settings_reader: Callable[[], Dict[str, Any]],
    *,
    repo_path: str | None = None,
) -> Dict[str, Any]:
    """Refresh all extension liveness and return ``skill: error_or_None``."""
    skills = discover_skills(drive_root, repo_path=repo_path)
    skill_names = {s.name for s in skills if s.manifest.is_extension()}
    with _lock:
        loaded_names = set(_extensions.keys())
    for gone in loaded_names - skill_names:
        unload_extension(gone)
        _sweep_stale_extension_imports(drive_root, gone)
    results: Dict[str, Any] = {}
    for skill in skills:
        if not skill.manifest.is_extension():
            continue
        _sweep_stale_extension_imports(drive_root, skill.name)
        state = reconcile_extension(
            skill.name,
            drive_root,
            settings_reader,
            repo_path=repo_path,
            retry_load_error=True,
        )
        results[skill.name] = state.get("load_error") or (None if state.get("desired_live") else state.get("reason"))
    return results


def snapshot() -> Dict[str, Any]:
    """Return a read-only snapshot of live extension surfaces."""
    with _lock:
        return {
            "extensions": sorted(_extensions.keys()),
            "tools": sorted(_tools.keys()),
            "routes": sorted(_routes.keys()),
            "ws_handlers": sorted(_ws_handlers.keys()),
            "ui_tabs": [
                dict(copy.deepcopy(value), key=key)
                for key, value in sorted(_ui_tabs.items())
            ],
            "ui_tabs_pending": [],
            # Settings sections follow the same host-surfaced shape as UI tabs.
            "settings_sections": [
                dict(copy.deepcopy(value), key=key)
                for key, value in sorted(_settings_sections.items())
            ],
        }


def get_tool(name: str) -> Optional[Dict[str, Any]]:
    """Return the registered extension tool, if any."""
    with _lock:
        return dict(_tools.get(name) or {}) or None


def list_ws_handlers() -> Dict[str, Any]:
    with _lock:
        return {k: dict(v) for k, v in _ws_handlers.items()}


def list_routes() -> Dict[str, Any]:
    with _lock:
        return {k: dict(v) for k, v in _routes.items()}


__all__ = [
    "PluginAPIImpl", "is_extension_live", "load_extension", "reconcile_extension",
    "unload_extension", "reload_all", "runtime_state_for_skill_name", "snapshot",
    "get_tool", "list_ws_handlers", "list_routes",
]

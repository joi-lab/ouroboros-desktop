"""Ouroboros — Frozen PluginAPI contract (v1, Phase 4).

Every ``type: extension`` skill's ``plugin.py`` module exports a single
entry point::

    def register(api: PluginAPI) -> None:
        api.register_tool(...)
        api.register_route(...)
        api.register_ws_handler(...)

``PluginAPI`` is the ONLY surface an extension may call. The ABI
declared here is frozen between releases in the same sense as
``ouroboros/contracts/tool_abi.py`` — breaking any method signature or
tightening a permission allowlist requires a deliberate schema/version
bump and a release note in ``docs/ARCHITECTURE.md`` §12. Additive
methods are allowed within schema v1 when they are optional for older
skills, documented here, and pinned by contract tests.

The surface intentionally mirrors what the Phase 3 plan approved:

- ``register_tool``      — add a tool callable via the normal tool
                           dispatch surface, namespaced as
                           ``ext_<len>_<token>_<name>``.
- ``register_route``     — register an HTTP handler mounted under
                           ``/api/extensions/<skill>/<path>``.
- ``register_ws_handler``— attach a handler for WS message types
                           namespaced the same provider-safe way.
- ``register_ui_tab``    — declare a reviewed Widgets-page surface.
- ``send_ws_message``    — broadcast a namespaced extension event to
                           connected browser clients.
- ``on_unload``          — register cleanup for background resources.
- ``log``                — structured logger (the extension does not
                           touch ``logging``/``print`` directly).
- ``get_settings``       — read-only view of settings keys the skill's
                           manifest ``env_from_settings`` allowlist
                           permits AND the extension-safe denylist does
                           not block.

All registrations are declarative — an extension that is later disabled
via ``toggle_skill`` is reloaded with all of its registrations torn
down, so the extension layer has no persistent side effects beyond the
skill's own state directory.
"""

from __future__ import annotations

import pathlib
from typing import Any, Awaitable, Callable, Dict, List, Protocol, Sequence, runtime_checkable

from ouroboros.skill_token import SkillToken

PLUGIN_API_VERSION = "1.2"


# Forbidden / "core" settings keys. Both in-process extensions
# (``PluginAPI.get_settings``) and out-of-process script skills
# (``_scrub_env``) drop these keys from their normal allowlist flow;
# the runtime forwards them only through explicit, content-hash-bound
# owner grants captured by the desktop launcher's native confirmation
# bridge (v5.2.2 dual-track grants — see ``docs/ARCHITECTURE.md``
# §12.5). Type ``instruction`` skills never receive them.
FORBIDDEN_SKILL_SETTINGS: frozenset[str] = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "ANTHROPIC_API_KEY",
        "GITHUB_TOKEN",
        "OUROBOROS_NETWORK_PASSWORD",
    }
)
# Backwards-compatible alias for the frozen Phase 4 name.
FORBIDDEN_EXTENSION_SETTINGS: frozenset[str] = FORBIDDEN_SKILL_SETTINGS


# Permission names an extension may declare in its manifest. The values
# also live in ``ouroboros.contracts.skill_manifest.VALID_SKILL_PERMISSIONS``
# from Phase 1, kept in sync here for a frozen ABI surface.
VALID_EXTENSION_PERMISSIONS: frozenset[str] = frozenset(
    {
        "net",
        "fs",
        "subprocess",
        "widget",
        "ws_handler",
        "route",
        "tool",
        "read_settings",
        "companion_process",
        "supervised_task",
        "subscribe_event",
        "inject_chat",
    }
)

VALID_EXTENSION_ROUTE_METHODS: frozenset[str] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}
)


@runtime_checkable
class PluginAPI(Protocol):
    """Frozen ABI exposed to every extension's ``register(api)``.

    This Protocol is RUNTIME-CHECKABLE so smoke tests can assert the
    real ``ouroboros.extension_loader.PluginAPIImpl`` structurally
    matches the frozen surface.
    """

    # --- registration ---

    def register_tool(
        self,
        name: str,
        handler: Callable[..., str] | Callable[..., Awaitable[str]],
        *,
        description: str,
        schema: Dict[str, Any],
        timeout_sec: int = 60,
    ) -> None:
        """Register a tool. The runtime namespaces it to
        ``ext_<len>_<token>_<name>``; attempting to register a collision
        with a built-in tool name or another extension's tool raises
        ``ExtensionRegistrationError``. ``name`` must be alphanumeric
        plus underscores and at most 24 characters so the provider-facing
        name remains within the strictest tool-name limit.

        v5.7.0+: handlers may be synchronous or ``async def``. Async
        handlers are executed by the registry on a helper thread with a
        fresh event loop and ``asyncio.wait_for(timeout_sec)``. They are
        not run on the main server event loop, so authors should not rely
        on loop-local state captured at register time."""
        ...

    def register_route(
        self,
        path: str,
        handler: Callable[..., Any],
        *,
        methods: Sequence[str] = ("GET",),
    ) -> None:
        """Register an HTTP route. The final mount point is
        ``/api/extensions/<skill>/<path>``; ``path`` must not start
        with ``/`` and must not contain ``..`` segments. ``methods``
        must be a non-empty subset of ``VALID_EXTENSION_ROUTE_METHODS``."""
        ...

    def register_ws_handler(
        self,
        message_type: str,
        handler: Callable[..., Awaitable[Any]] | Callable[..., Any],
    ) -> None:
        """Register a WebSocket message handler. ``message_type`` is
        stored under the same provider-safe extension namespace on the
        dispatcher; handlers receive ``(payload_dict)`` and may be async.
        ``message_type`` follows the same alphanumeric/underscore and
        24-character limit as tool names."""
        ...

    def register_ui_tab(
        self,
        tab_id: str,
        title: str,
        *,
        icon: str = "extension",
        render: Dict[str, Any] | None = None,
    ) -> None:
        """Register a Widgets-page UI declaration.

        The runtime stores the declaration in
        ``ouroboros.extension_loader._ui_tabs`` keyed by
        ``"<skill>:<tab_id>"``. The browser hosts these declarations
        on the top-level Widgets page. ``render`` is a declarative
        browser-hosted schema. Supported host-owned shapes are:
        ``{"kind": "iframe", "route": "..."}`` and ``{"kind": "declarative",
        "schema_version": 1, "components": [...]}`` for generic
        forms/actions/markdown/json/table/media render blocks.
        v5.7.0 adds a reviewed sandboxed module exception:
        ``{"kind": "module", "entry": "widget.js"}``. The host serves
        the declared file via ``GET /api/extensions/<skill>/module/<entry>``
        only when a live widget tab declared that exact entry, then
        mounts the JS inside an opaque ``<iframe srcdoc sandbox="allow-scripts">``
        with a parent-mediated fetch bridge restricted to the owning
        ``/api/extensions/<skill>/...`` route prefix. Same-origin dynamic
        modules in the SPA origin remain outside this contract because
        they could call privileged app APIs."""
        ...

    def send_ws_message(self, message_type: str, data: Dict[str, Any]) -> None:
        """Broadcast a namespaced extension event to connected browsers.

        ``message_type`` follows the same short-name rules as
        ``register_ws_handler`` and is emitted as
        ``ext_<len>_<token>_<message_type>``. Requires the manifest
        ``ws_handler`` permission because it uses the same WebSocket
        extension namespace as inbound handlers. The broadcast is
        best-effort; when no WebSocket loop is available the message is
        dropped.
        """
        ...

    def register_settings_section(
        self,
        section_id: str,
        title: str,
        *,
        schema: Dict[str, Any],
    ) -> None:
        """Register a host-rendered Settings sub-panel for this extension
        (v5.7.0+).

        The runtime stores the declaration keyed by
        ``"<skill>:<section_id>"``; the browser fetches the catalogue
        via ``GET /api/extensions/<skill>/settings_section`` and mounts
        it in the Settings UI under an "Extension Settings" group.
        ``schema`` uses a deliberately narrow declarative subset:
        ``form`` / ``action`` (configuration writes through reviewed
        extension routes) plus ``markdown`` / ``json`` (explanatory or
        diagnostic content). Rich widget-only components such as media,
        stream, kanban, map, or arbitrary JS are not part of Settings.
        Arbitrary extension-supplied JS is NEVER rendered into the SPA
        origin via this API.

        ``section_id`` follows the same alphanumeric/underscore + 24
        character rules as tool names. ``title`` is a human-readable
        section name displayed verbatim (escaped). Calling this twice
        for the same ``section_id`` raises ``ExtensionRegistrationError``.
        """
        ...

    def register_supervised_task(
        self,
        name: str,
        factory: Callable[[], Awaitable[None]],
        *,
        restart_policy: str = "on_failure",
        max_restarts: int = 5,
        backoff_seconds: float = 2.0,
    ) -> None:
        """Register a long-lived in-process asyncio task.

        The task lifecycle is bound to the owning skill's enabled state.
        Requires the manifest ``supervised_task`` permission.
        """
        ...

    def register_companion_process(
        self,
        name: str,
    ) -> None:
        """Register a host-supervised companion subprocess.

        Command/runtime/restart settings are read from the reviewed
        ``companion_processes`` manifest entry with the same ``name``.
        Requires the manifest ``companion_process`` permission.
        """
        ...

    def subscribe_event(
        self,
        topic: str,
        handler: Callable[[Dict[str, Any]], Awaitable[None] | None],
    ) -> str:
        """Subscribe to a manifest-declared host event topic.

        The runtime rejects topics not listed in ``subscribe_events`` and all
        subscriptions are removed when the skill unloads. Requires the manifest
        ``subscribe_event`` permission. Chat topics require owner grants;
        ``skill.lifecycle`` is available without an owner grant so reviewed
        skills can react to skill_exec completion/failure events.
        """
        ...

    def get_skill_token(self) -> SkillToken:
        """Return the opaque Host Service API token for this skill."""
        ...

    def on_unload(self, callback: Callable[[], Any]) -> None:
        """Register a best-effort cleanup callback.

        The extension loader invokes callbacks when the owning skill is
        disabled, reloaded, made stale, or otherwise unloaded. Callbacks
        should be fast and idempotent: close sockets, stop EventSource
        clients, signal worker threads, or terminate child processes
        owned by the extension. Exceptions are logged and do not prevent
        registry teardown.
        """
        ...

    # --- runtime access ---

    def log(
        self,
        level: str,
        message: str,
        **fields: Any,
    ) -> None:
        """Structured log. ``level`` one of ``debug``/``info``/``warning``/``error``."""
        ...

    def get_settings(self, keys: Sequence[str]) -> Dict[str, Any]:
        """Return a ``{key: value}`` mapping for the requested keys.

        Requires the manifest ``read_settings`` permission. Returned
        keys must be in the skill manifest's ``env_from_settings``
        allowlist. Forbidden / "core" keys (``FORBIDDEN_EXTENSION_SETTINGS``)
        are dropped silently UNLESS the owner has captured an explicit,
        content-hash-bound grant via the desktop launcher's native
        confirmation bridge (v5.2.2 dual-track grants). When such a
        grant is in place the loader passes the granted subset into
        ``PluginAPIImpl`` at construction time and ``get_settings``
        forwards those values to the in-process plugin. Missing keys
        omit from the result.
        """
        ...

    def get_state_dir(self) -> str:
        """Absolute path of the skill's private state directory
        (``~/Ouroboros/data/state/skills/<skill>/``).

        This is the **canonical** writable location for an extension's
        durable state. Extensions run IN-PROCESS and are not filesystem-
        sandboxed (Phase 4 does not wrap the interpreter in an OS-level
        jail), so a misbehaving plugin could technically ``open(...)``
        paths elsewhere. The Skill Review Checklist's
        ``path_confinement`` item is the authoritative enforcement;
        ``get_state_dir`` is where well-behaved extensions should put
        their durable state so operators can find it in the expected
        place and ``toggle_skill`` / clean-uninstall paths know where
        to look."""
        ...

    def skill_job_dir(self, job_id: str) -> pathlib.Path:
        """Return an isolated per-job directory under the skill state tree.

        The runtime sanitizes ``job_id`` and creates
        ``jobs/<sanitized-id>-<hash>/{assets,output,tmp}`` so generation
        skills can keep concurrent or repeated jobs from overwriting one
        another's assets.
        """
        ...

    def get_runtime_info(self) -> Dict[str, Any]:
        """Return a read-only snapshot of runtime context the extension
        may need (v5.7.0+).

        Shape (subject to additive evolution within schema v1)::

            {
                "runtime_mode": "light"|"advanced"|"pro",
                "app_version": "5.7.0",     # ouroboros/VERSION
                "data_dir": "/.../Ouroboros/data",
                "skill_dir": "/.../data/skills/<bucket>/<skill>",
                "state_dir": "/.../data/state/skills/<skill>",
                "server_port": 8765,        # 0 if launcher not in HTTP mode
            }

        All values are computed on demand from the loader / config /
        skill loader; calling this method does not mutate state.
        Extensions can use the snapshot to e.g. adapt UX to the runtime
        mode (light vs pro), embed the app version in chat messages,
        find their state dir without re-reading their manifest, or
        construct relative routes to other in-process /api/extensions
        surfaces."""
        ...


class ExtensionRegistrationError(Exception):
    """Raised by the extension loader when a registration call violates
    the namespace / permission / schema contract. Surfaces to the
    agent as a ``load_error`` on the owning skill so the operator can
    fix the plugin and re-review."""


__all__ = [
    "PluginAPI",
    "ExtensionRegistrationError",
    "FORBIDDEN_SKILL_SETTINGS",
    "FORBIDDEN_EXTENSION_SETTINGS",
    "PLUGIN_API_VERSION",
    "VALID_EXTENSION_PERMISSIONS",
    "VALID_EXTENSION_ROUTE_METHODS",
]

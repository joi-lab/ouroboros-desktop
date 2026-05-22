"""Descriptive HTTP + WebSocket Gateway Boundary contracts (v1).

TypedDicts document payloads, not runtime validation. Keep discriminating
``type`` keys required; mark genuinely optional fields with ``NotRequired``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:  # Python 3.11+
    from typing import Literal, NotRequired, TypedDict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - CI supports Python 3.10.
    from typing_extensions import Literal, NotRequired, TypedDict  # type: ignore[assignment]


class ChatInbound(TypedDict):
    """Inbound WS chat message. ``type`` and ``content`` are required."""

    type: Literal["chat"]
    content: str
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]


class TaskConstraintInbound(TypedDict, total=False):
    mode: str
    skill_name: str
    payload_root: str
    allow_enable: bool
    allow_review: bool
    extra_allowlist: list[str]


class CommandInbound(TypedDict):
    """Inbound WS command message."""

    type: Literal["command"]
    cmd: str


class ExtensionInbound(TypedDict, total=False):
    """Inbound extension-owned WS message.

    The concrete ``type`` value is provider-safe and namespaced as
    ``ext_<len>_<token>_<message>`` by ``extension_loader``.
    """

    type: str
    data: Any


class TransportMetadata(TypedDict, total=False):
    """Generic external transport provenance for bridge skills."""

    kind: str
    conversation_id: str
    sender_label: str


class ChatOutbound(TypedDict):
    """Outbound WS chat frame."""

    type: Literal["chat"]
    role: Literal["user", "assistant", "system"]
    content: str
    ts: str
    markdown: NotRequired[bool]
    is_progress: NotRequired[bool]
    task_id: NotRequired[str]
    lifecycle: NotRequired[Dict[str, Any]]
    source: NotRequired[str]
    sender_label: NotRequired[str]
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    transport: NotRequired[TransportMetadata]
    # Deprecated compatibility field: runtime emits ``transport`` instead.
    telegram_chat_id: NotRequired[int]
    # UI-only system annotation emitted by skill-repair visible commands.
    system_type: NotRequired[str]
    # Present on some transport re-broadcast paths.
    chat_id: NotRequired[int]


class PhotoOutbound(TypedDict):
    """Outbound WS photo frame."""

    type: Literal["photo"]
    role: Literal["user", "assistant"]
    image_base64: str
    mime: str
    ts: str
    caption: NotRequired[str]
    content: NotRequired[str]
    source: NotRequired[str]
    sender_label: NotRequired[str]
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    transport: NotRequired[TransportMetadata]
    chat_id: NotRequired[int]
    # Deprecated compatibility field: runtime emits ``transport`` instead.
    telegram_chat_id: NotRequired[int]


class TypingOutbound(TypedDict):
    """Outbound WS typing indicator."""

    type: Literal["typing"]
    action: str


class LogOutbound(TypedDict):
    """Outbound WS log event."""

    type: Literal["log"]
    data: Dict[str, Any]


class HeartbeatOutbound(TypedDict):
    """Outbound heartbeat emitted by ``server_runtime.ws_heartbeat_loop``."""

    type: Literal["heartbeat"]
    ts: NotRequired[str]


class ExtensionLifecycleOutbound(TypedDict):
    """Outbound extension lifecycle notification."""

    type: Literal["extension_lifecycle"]
    skill: NotRequired[str]
    action: NotRequired[str]
    status: NotRequired[str]
    reason: NotRequired[str]
    data: NotRequired[Dict[str, Any]]


class ErrorResponse(TypedDict):
    error: str


class StatusResponse(TypedDict):
    status: str


class HealthResponse(TypedDict):
    """Shape of ``GET /api/health``."""

    status: Literal["ok"]
    version: str
    runtime_version: str
    app_version: str


class EvolutionStateSnapshot(TypedDict):
    """Nested ``evolution_state`` block inside ``StateResponse``."""

    enabled: bool
    status: str
    detail: str
    cycle: int
    owner_chat_bound: bool
    last_task_at: str
    consecutive_failures: int
    budget_remaining_usd: float
    budget_reserve_usd: float
    pending_count: int
    running_count: int
    queued_task_id: str
    running_task_id: str


class StateResponse(TypedDict):
    """Shape of ``GET /api/state`` (happy path)."""

    uptime: int
    workers_alive: int
    workers_total: int
    pending_count: int
    running_count: int
    spent_usd: float
    budget_limit: float
    budget_pct: float
    branch: str
    sha: str
    evolution_enabled: bool
    bg_consciousness_enabled: bool
    evolution_cycle: int
    evolution_state: EvolutionStateSnapshot
    bg_consciousness_state: Dict[str, Any]
    spent_calls: int
    supervisor_ready: bool
    supervisor_error: Optional[str]
    runtime_mode: str
    skills_repo_configured: bool
    github_token_configured: bool


class SettingsNetworkMeta(TypedDict):
    """Network fields inside the ``GET /api/settings`` ``_meta`` block."""

    bind_host: str
    bind_port: int
    lan_ip: str
    reachability: Literal["loopback_only", "lan_reachable", "host_ip_unknown"]
    recommended_url: str
    warning: str


class SettingsMeta(SettingsNetworkMeta, total=False):
    """Complete ``GET /api/settings`` ``_meta`` block."""

    custom_secret_keys: list[str]
    setup_contract: Dict[str, Any]


class SettingsSaveResponse(TypedDict, total=False):
    status: str
    no_changes: bool
    restart_required: bool
    restart_keys: list[str]
    immediate_changed: bool
    next_task_changed: bool
    warnings: list[str]


class OwnerRuntimeModeResponse(TypedDict):
    ok: bool
    runtime_mode: str
    restart_required: bool


class OwnerAutoGrantResponse(TypedDict):
    ok: bool
    enabled: bool


class SkillGrantResponse(TypedDict, total=False):
    ok: bool
    skill: str
    granted_keys: list[str]
    granted_permissions: list[str]
    extension_action: str
    extension_reason: str
    load_error: str
    grants: Dict[str, Any]
    error: str


class GitLogResponse(TypedDict):
    commits: list[Dict[str, Any]]
    tags: list[str]
    branch: str
    sha: str


class EvolutionDataResponse(TypedDict):
    points: list[Dict[str, Any]]
    generated_at: str
    cached: bool


class UploadResponse(TypedDict):
    ok: bool
    filename: str
    display_name: str
    path: str
    size: int
    mime: str


class ExtensionsIndexResponse(TypedDict, total=False):
    extensions: list[Dict[str, Any]]
    skills: list[Dict[str, Any]]
    lifecycle: Dict[str, Any]
    error: str


class SkillLifecycleQueueResponse(TypedDict, total=False):
    active: Dict[str, Any]
    events: list[Dict[str, Any]]


class MarketplaceSearchResponse(TypedDict, total=False):
    items: list[Dict[str, Any]]
    results: list[Dict[str, Any]]
    installed: list[Dict[str, Any]]
    error: str


class MarketplaceInstalledResponse(TypedDict, total=False):
    installed: list[Dict[str, Any]]
    skills: list[Dict[str, Any]]
    error: str


class LocalModelStatusResponse(TypedDict, total=False):
    status: str
    running: bool
    ready: bool
    port: int
    message: str
    error: str


class McpStatusResponse(TypedDict, total=False):
    enabled: bool
    servers: list[Dict[str, Any]]
    tools: list[Dict[str, Any]]
    error: str


class ModelCatalogResponse(TypedDict, total=False):
    providers: list[Dict[str, Any]]
    models: list[Dict[str, Any]]
    error: str


class FileBrowserListResponse(TypedDict, total=False):
    root: str
    path: str
    entries: list[Dict[str, Any]]
    error: str


class ChatHistoryResponse(TypedDict, total=False):
    messages: list[Dict[str, Any]]
    has_more: bool
    next_before_ts: str
    error: str


class TaskCreateResponse(TypedDict, total=False):
    ok: bool
    task_id: str
    status: str
    error: str


class TaskListResponse(TypedDict, total=False):
    tasks: list[Dict[str, Any]]
    queue: Dict[str, Any]
    error: str


class TaskEvent(TypedDict, total=False):
    seq: int
    source: str
    line: int
    ts: str
    type: str
    task_id: str
    root: str
    data: Dict[str, Any]


class TaskCancelResponse(TypedDict, total=False):
    ok: bool
    task_id: str
    error: str


class LogTailResponse(TypedDict, total=False):
    name: str
    entries: list[Dict[str, Any]]
    error: str


# Human/test-visible contract index; routers own executable Route objects.
HTTP_ENDPOINTS: tuple[str, ...] = (
    "GET /api/health",
    "GET /api/state",
    "GET /api/settings",
    "POST /api/settings",
    "POST /api/owner/runtime-mode",
    "POST /api/owner/auto-grant",
    "GET /api/model-catalog",
    "POST /api/tasks",
    "GET /api/tasks",
    "GET /api/tasks/{task_id}",
    "GET /api/tasks/{task_id}/events",
    "POST /api/tasks/{task_id}/cancel",
    "POST /api/command",
    "POST /api/reset",
    "GET /api/git/log",
    "POST /api/git/rollback",
    "POST /api/git/promote",
    "GET /api/update/status",
    "POST /api/update/check",
    "POST /api/update/apply",
    "GET /api/cost-breakdown",
    "GET /api/evolution-data",
    "GET /api/chat/history",
    "GET /api/logs/{name}",
    "POST /api/chat/upload",
    "DELETE /api/chat/upload",
    "GET /api/local-model/status",
    "POST /api/local-model/start",
    "POST /api/local-model/stop",
    "POST /api/local-model/test",
    "POST /api/local-model/install-runtime",
    "GET /api/mcp/status",
    "POST /api/mcp/refresh",
    "POST /api/mcp/test",
    "GET /api/extensions",
    "GET /api/extensions/{skill}/manifest",
    "GET /api/extensions/{skill}/module/{entry}",
    "GET /api/extensions/{skill}/settings_section",
    "ANY /api/extensions/{skill}/{rest:path}",
    "POST /api/skills/{skill}/toggle",
    "GET /api/skills/lifecycle-queue",
    "POST /api/skills/{skill}/review",
    "POST /api/skills/{skill}/grants",
    "POST /api/skills/{skill}/reconcile",
    "GET /api/marketplace/clawhub/search",
    "GET /api/marketplace/clawhub/installed",
    "GET /api/marketplace/clawhub/info/{slug:path}",
    "GET /api/marketplace/clawhub/preview/{slug:path}",
    "POST /api/marketplace/clawhub/install",
    "POST /api/marketplace/clawhub/update/{name}",
    "POST /api/marketplace/clawhub/uninstall/{name}",
    "GET /api/marketplace/ouroboroshub/catalog",
    "GET /api/marketplace/ouroboroshub/installed",
    "GET /api/marketplace/ouroboroshub/preview/{slug:path}",
    "POST /api/marketplace/ouroboroshub/install",
    "POST /api/marketplace/ouroboroshub/update/{name}",
    "POST /api/marketplace/ouroboroshub/uninstall/{name}",
    "GET /api/onboarding",
    "GET /api/claude-code/status",
    "POST /api/claude-code/install",
    "GET /api/files/list",
    "GET /api/files/read",
    "GET /api/files/content",
    "GET /api/files/download",
    "POST /api/files/upload",
    "POST /api/files/mkdir",
    "POST /api/files/write",
    "POST /api/files/delete",
    "POST /api/files/transfer",
    "WS /ws",
)

WS_MESSAGE_TYPES: tuple[str, ...] = (
    "chat",
    "command",
    "photo",
    "typing",
    "log",
    "heartbeat",
    "extension_lifecycle",
)


__all__ = [
    "ChatInbound",
    "TaskConstraintInbound",
    "CommandInbound",
    "ExtensionInbound",
    "TransportMetadata",
    "ChatOutbound",
    "PhotoOutbound",
    "TypingOutbound",
    "LogOutbound",
    "HeartbeatOutbound",
    "ExtensionLifecycleOutbound",
    "ErrorResponse",
    "StatusResponse",
    "HealthResponse",
    "StateResponse",
    "EvolutionStateSnapshot",
    "SettingsNetworkMeta",
    "SettingsMeta",
    "SettingsSaveResponse",
    "OwnerRuntimeModeResponse",
    "OwnerAutoGrantResponse",
    "SkillGrantResponse",
    "GitLogResponse",
    "EvolutionDataResponse",
    "UploadResponse",
    "ExtensionsIndexResponse",
    "SkillLifecycleQueueResponse",
    "MarketplaceSearchResponse",
    "MarketplaceInstalledResponse",
    "LocalModelStatusResponse",
    "McpStatusResponse",
    "ModelCatalogResponse",
    "FileBrowserListResponse",
    "ChatHistoryResponse",
    "TaskCreateResponse",
    "TaskListResponse",
    "TaskEvent",
    "TaskCancelResponse",
    "LogTailResponse",
    "HTTP_ENDPOINTS",
    "WS_MESSAGE_TYPES",
]

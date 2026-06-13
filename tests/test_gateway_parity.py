from __future__ import annotations

import pathlib
import re
from typing import get_type_hints

from ouroboros.gateway.contracts import (
    ChatOutbound,
    HTTP_ENDPOINTS,
    PhotoOutbound,
    SkillDeleteResponse,
    SkillLifecycleQueueResponse,
    VideoOutbound,
    WS_MESSAGE_TYPES,
)
from ouroboros.gateway.router import collect_routes


def _js_typedef_fields(text: str, name: str) -> set[str]:
    match = re.search(rf"@typedef \{{Object\}} {name}\b(?P<body>.*?)\n \*/", text, re.S)
    assert match, f"api_types.js missing {name}"
    return set(re.findall(r"@property \{[^}]+\} ([A-Za-z_][A-Za-z0-9_]*)\b", match.group("body")))


def test_gateway_contract_endpoint_index_matches_router_and_types(tmp_path):
    tokens: set[str] = set()
    for route in collect_routes(data_dir=tmp_path):
        path = getattr(route, "path", "")
        if not path:
            continue
        methods = getattr(route, "methods", None)
        if methods is None:
            tokens.add(f"WS {path}")
            continue
        normalized = sorted(m for m in methods if m not in {"HEAD", "OPTIONS"})
        if set(normalized) == {"DELETE", "GET", "PATCH", "POST", "PUT"}:
            tokens.add(f"ANY {path}")
        else:
            for method in normalized:
                tokens.add(f"{method} {path}")
    contract_tokens = set(HTTP_ENDPOINTS)
    missing = contract_tokens - tokens
    extra = tokens - contract_tokens
    assert not missing, f"HTTP_ENDPOINTS includes routes not mounted by gateway.router: {sorted(missing)}"
    assert not extra, f"gateway.router mounts routes missing from HTTP_ENDPOINTS: {sorted(extra)}"
    text = (pathlib.Path(__file__).resolve().parent.parent / "web" / "modules" / "api_types.js").read_text(
        encoding="utf-8"
    )
    version = (pathlib.Path(__file__).resolve().parent.parent / "VERSION").read_text(encoding="utf-8").strip()
    assert f"GATEWAY_CONTRACT_VERSION = '{version}'" in text
    for name in (
        "StateResponse",
        "HealthResponse",
        "SettingsMeta",
        "ChatInbound",
        "ChatOutbound",
        "PhotoOutbound",
        "VideoOutbound",
        "UploadResponse",
        "TaskCreateResponse",
        "TaskEvent",
        "TaskListResponse",
        "TaskCancelResponse",
        "LogTailResponse",
        "SkillDeleteResponse",
    ):
        assert re.search(rf"@typedef \{{Object\}} {name}\b", text), f"api_types.js missing {name}"
    for cls in (ChatOutbound, PhotoOutbound, VideoOutbound):
        expected = set(get_type_hints(cls, include_extras=True))
        actual = _js_typedef_fields(text, cls.__name__)
        assert actual == expected, f"{cls.__name__} JSDoc fields drifted: missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
    for field in ("source", "line", "root"):
        assert re.search(rf"@property \{{[^}}]+=\}} {field}\b", text), f"TaskEvent missing {field}"
    for field in (
        "subagent_event",
        "subagent_task_id",
        "root_task_id",
        "parent_task_id",
        "delegation_role",
        "subagent_role",
        "task_event",
        "status",
        "result",
        "trace_summary",
        "error",
        "artifact_status",
    ):
        assert re.search(rf"@property \{{string=\}} {field}\b", text), f"ChatOutbound missing {field}"
    assert re.search(r"@property \{number=\} cost_usd\b", text), "ChatOutbound missing cost_usd"
    assert re.search(r"@property \{number=\} chat_id\b", text), "ChatOutbound missing chat_id"
    assert re.search(r"@property \{boolean=\} worker_saturation_warning\b", text), "ChatOutbound missing worker_saturation_warning"
    assert "setup_contract" in text
    assert re.search(r"@property \{string=\} error\b", text), "SkillDeleteResponse missing optional error"
    assert {"chat", "command", "photo", "video", "typing", "log", "heartbeat", "extension_lifecycle"} <= set(WS_MESSAGE_TYPES)


def test_skill_lifecycle_queue_contract_matches_runtime_shape():
    fields = set(SkillLifecycleQueueResponse.__annotations__)

    assert {"active", "events"} <= fields
    assert {"queue", "recent_events", "running"}.isdisjoint(fields)


def test_skill_delete_contract_matches_runtime_shape():
    fields = set(SkillDeleteResponse.__annotations__)

    assert {
        "ok",
        "skill",
        "source",
        "deleted_payload_root",
        "deleted_state",
        "extension_action",
        "extension_reason",
        "error",
    } <= fields

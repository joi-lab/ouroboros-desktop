"""Task-start tool visibility policy.

This module determines which tools are available at the start of a task
without an explicit ``enable_tools`` call.

Tool sets are imported from ``ouroboros.tool_capabilities`` (the single
source of truth).  This module adds the visibility-decision logic on top.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

import json
import logging

from ouroboros.tool_capabilities import CORE_TOOL_NAMES, META_TOOL_NAMES

log = logging.getLogger(__name__)
_INITIAL_EXTENSION_SCHEMA_BUDGET = 8_000


class ToolSchemaProvider(Protocol):
    """Minimal registry contract needed by the loop/discovery helpers."""

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        ...


def is_initial_task_tool(name: str) -> bool:
    """Return True if the tool should be loaded before any enable_tools call."""

    return str(name or "").strip() in CORE_TOOL_NAMES or str(name or "").strip() in META_TOOL_NAMES

def _initial_tool_names_for(registry: ToolSchemaProvider) -> set[str]:
    override = getattr(registry, "initial_tool_names", None)
    if callable(override):
        try:
            names = override()
            if names is not None:
                return {str(name).strip() for name in names if str(name).strip()}
        except Exception:
            log.debug("Registry initial_tool_names override failed", exc_info=True)
    return set(CORE_TOOL_NAMES) | set(META_TOOL_NAMES)


def is_initial_extension_tool(name: str) -> bool:
    """Live extension tool schemas are visible from round 1."""

    from ouroboros.extension_loader import parse_extension_surface_name
    return parse_extension_surface_name(name) is not None


def initial_tool_schemas(registry: ToolSchemaProvider) -> List[Dict[str, Any]]:
    """Return the schemas that should be present from round 1."""

    result = []
    initial_names = _initial_tool_names_for(registry)
    extension_bytes = 0
    for schema in registry.schemas():
        name = schema.get("function", {}).get("name", "")
        if name in initial_names:
            result.append(schema)
            continue
        if is_initial_extension_tool(name):
            encoded = len(json.dumps(schema, ensure_ascii=False, default=str))
            if extension_bytes + encoded > _INITIAL_EXTENSION_SCHEMA_BUDGET:
                log.warning(
                    "Skipping initial extension tool schema %s: extension schema budget exceeded",
                    name,
                )
                continue
            extension_bytes += encoded
            result.append(schema)
    return result


def list_non_core_tools(registry: ToolSchemaProvider) -> List[Dict[str, str]]:
    """Return name+description for tools that require explicit enable_tools."""

    result = []
    initial_names = _initial_tool_names_for(registry)
    for schema in registry.schemas():
        function = schema.get("function", {})
        name = function.get("name", "")
        if not name or name in initial_names:
            continue
        result.append({
            "name": name,
            "description": function.get("description", "No description"),
        })
    return result

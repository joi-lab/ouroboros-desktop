"""Frozen ABI contracts between runtime, tools, gateway, and skill layer.

This package is intentionally small and additive: Protocols/TypedDicts describe
existing surfaces, while behavior-changing code belongs outside contracts.
"""

from __future__ import annotations

from ouroboros.contracts.tool_context import ToolContextProtocol
from ouroboros.contracts.tool_abi import ToolEntryProtocol, GetToolsProtocol
from ouroboros.contracts.skill_manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    VALID_SKILL_TYPES,
    VALID_SKILL_RUNTIMES,
    VALID_SKILL_PERMISSIONS,
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from ouroboros.contracts.schema_versions import (
    SCHEMA_VERSION_KEY,
    with_schema_version,
    read_schema_version,
)
from ouroboros.contracts.plugin_api import (
    PluginAPI,
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
)

__all__ = [
    "ToolContextProtocol",
    "ToolEntryProtocol",
    "GetToolsProtocol",
    "SKILL_MANIFEST_SCHEMA_VERSION",
    "VALID_SKILL_TYPES",
    "VALID_SKILL_RUNTIMES",
    "VALID_SKILL_PERMISSIONS",
    "SkillManifest",
    "SkillManifestError",
    "parse_skill_manifest_text",
    "SCHEMA_VERSION_KEY",
    "with_schema_version",
    "read_schema_version",
    "PluginAPI",
    "ExtensionRegistrationError",
    "FORBIDDEN_EXTENSION_SETTINGS",
    "VALID_EXTENSION_PERMISSIONS",
]

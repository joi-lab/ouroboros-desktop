"""Unified ``SKILL.md`` / ``skill.json`` manifest (v1).

One manifest format describes all three kinds of external packages:

- ``type: instruction`` — pure markdown guide, no executable payload.
- ``type: script``      — markdown guide + one or more scripts invoked
                          through the upcoming ``skill_exec`` tool.
- ``type: extension``   — markdown guide + ``plugin.py``-style entry plus
                          optional routes / ws handlers and future UI-tab
                          declarations.

The parser intentionally works on either::

    ---
    name: weather
    type: script
    ...
    ---
    # body (human readable instructions)
    ...

(YAML frontmatter in ``SKILL.md``) **or** a standalone ``skill.json`` file.

The parser is intentionally tolerant for missing optional fields and
unknown extras, but it FAILS CLOSED on structural contract damage:
invalid JSON/YAML, malformed structured fields (for example ``ui_tab``),
or an unsupported ``schema_version`` all raise ``SkillManifestError``.
"""

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


SKILL_MANIFEST_SCHEMA_VERSION = 1

VALID_SKILL_TYPES = frozenset({"instruction", "script", "extension"})
VALID_SKILL_RUNTIMES = frozenset({
    "",
    "python",
    "python3",
    "node",
    "bash",
    # v5.7.0: extended runtime set. The actual binary is still resolved via
    # ``shutil.which`` at exec time and the skill subprocess fails closed if
    # the operator's host doesn't ship the runtime, but the manifest
    # validator no longer rejects these declarations as unknown.
    "deno",
    "ruby",
    "go",
})
VALID_SKILL_PERMISSIONS = frozenset(
    {
        "net",
        "fs",
        "subprocess",
        "widget",
        "ws_handler",
        # Phase 4 ``type: extension`` permissions — kept in sync with
        # ``ouroboros.contracts.plugin_api.VALID_EXTENSION_PERMISSIONS``
        # so ``SkillManifest.validate()`` does not warn "unknown
        # permission" on legitimate extension manifests that declare
        # these Phase-4 surfaces. The single frozen-set remains the
        # SSOT for both script-type and extension-type permissions.
        "route",
        "tool",
        "read_settings",
        "iframe_raw",
        "companion_process",
        "supervised_task",
        "subscribe_event",
        "inject_chat",
    }
)
_EVENT_TOPIC_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class SkillManifestError(ValueError):
    """Raised when a manifest is structurally broken (not just missing fields)."""


@dataclass
class SkillManifest:
    """Structural description of one skill package.

    Fields marked optional default to empty values so the evolutionary layer
    can render partial skills in the UI with a ``needs_review`` badge.
    """

    name: str
    description: str
    version: str
    type: str  # instruction | script | extension
    when_to_use: str = ""
    requires: List[str] = field(default_factory=list)
    os: str = "any"
    runtime: str = ""
    timeout_sec: int = 60
    env_from_settings: List[str] = field(default_factory=list)
    # script-typed manifests list their scripts; each item is a mapping with
    # at least ``name`` and optionally ``description``.
    scripts: List[Dict[str, str]] = field(default_factory=list)
    # extension-typed manifests point at a Python entry module.
    entry: str = ""
    permissions: List[str] = field(default_factory=list)
    subscribe_events: List[str] = field(default_factory=list)
    companion_processes: List[Dict[str, Any]] = field(default_factory=list)
    ui_tab: Optional[Dict[str, Any]] = None
    # Human-readable body from SKILL.md after the closing ``---`` line.
    body: str = ""
    # Anything we didn't understand, preserved for forward-compatibility.
    raw_extra: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SKILL_MANIFEST_SCHEMA_VERSION

    # --- Convenience --------------------------------------------------

    def is_instruction(self) -> bool:
        return self.type == "instruction"

    def is_script(self) -> bool:
        return self.type == "script"

    def is_extension(self) -> bool:
        return self.type == "extension"

    def validate(self) -> List[str]:
        """Return a list of non-blocking warnings for a parsed manifest.

        Blocking failures are raised by ``parse_skill_manifest_text``; this
        function describes *soft* issues useful to show in review output
        (unknown type, unknown runtime, permissions typo, etc.).
        """
        warnings: List[str] = []
        if self.type not in VALID_SKILL_TYPES:
            warnings.append(
                f"unknown type '{self.type}' (expected one of "
                f"{sorted(VALID_SKILL_TYPES)})"
            )
        if self.runtime not in VALID_SKILL_RUNTIMES:
            warnings.append(
                f"unknown runtime '{self.runtime}' (expected empty or one of "
                f"{sorted(r for r in VALID_SKILL_RUNTIMES if r)})"
            )
        for perm in self.permissions:
            if perm not in VALID_SKILL_PERMISSIONS:
                warnings.append(
                    f"unknown permission '{perm}' (expected one of "
                    f"{sorted(VALID_SKILL_PERMISSIONS)})"
                )
        for topic in self.subscribe_events:
            if not _EVENT_TOPIC_RE.match(topic):
                warnings.append(
                    f"invalid subscribe_events topic '{topic}' "
                    "(expected lower.dotted format)"
                )
        if self.is_extension() and not self.entry:
            warnings.append("type=extension requires non-empty 'entry'")
        if self.is_script() and not self.scripts:
            warnings.append("type=script requires at least one entry in 'scripts'")
        if self.timeout_sec <= 0:
            warnings.append("timeout_sec must be positive")
        return warnings


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    re.DOTALL,
)


def parse_skill_manifest_text(text: str) -> SkillManifest:
    """Parse a ``SKILL.md`` (frontmatter + body) or a ``skill.json`` document.

    Auto-detects which form the input is in:

    - Starts with ``{`` -> parsed as JSON.
    - Starts with ``---`` -> parsed as YAML-ish frontmatter, the trailing
      body becomes ``manifest.body``.
    - Otherwise treated as an instruction-only markdown file with no
      frontmatter; a best-effort ``name`` is derived from the first heading.

    Raises ``SkillManifestError`` only on structural damage. Missing optional
    fields become empty values; unknown fields are preserved in ``raw_extra``.
    """
    src = text.lstrip("\ufeff")  # strip BOM if any
    stripped = src.lstrip()

    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SkillManifestError(f"invalid skill.json: {exc}") from exc
        if not isinstance(data, dict):
            raise SkillManifestError("skill.json root must be a mapping")
        return _manifest_from_mapping(data, body="")

    match = _FRONTMATTER_RE.match(src)
    if match is not None:
        front, body = match.group(1), match.group(2) or ""
        try:
            import yaml  # type: ignore
        except ImportError as exc:
            raise SkillManifestError(
                "PyYAML is required to parse SKILL.md frontmatter"
            ) from exc
        try:
            data: Any = yaml.safe_load(front) or {}
        except yaml.YAMLError as exc:  # type: ignore[name-defined]
            raise SkillManifestError(f"invalid SKILL.md frontmatter: {exc}") from exc
        if not isinstance(data, dict):
            raise SkillManifestError("SKILL.md frontmatter must be a mapping")
        return _manifest_from_mapping(data, body=body.strip())
    # Fallback: body-only markdown, treat as instruction skill.
    # ``stripped.startswith("---")`` is NOT treated as a broken frontmatter
    # fence here — a markdown document that legitimately starts with a
    # thematic break (``---`` on its own line) is a valid instruction
    # skill body. Real frontmatter parse failures (malformed YAML, bad
    # mapping shape) are caught by the branch above which only runs when
    # the full frontmatter regex actually matches.
    name = _derive_name_from_body(src)
    return SkillManifest(
        name=name,
        description="",
        version="",
        type="instruction",
        body=src.strip(),
        schema_version=SKILL_MANIFEST_SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _manifest_from_mapping(data: Dict[str, Any], *, body: str) -> SkillManifest:
    known = {
        "name",
        "description",
        "version",
        "type",
        "when_to_use",
        "requires",
        "os",
        "runtime",
        "timeout_sec",
        "env_from_settings",
        "scripts",
        "entry",
        "permissions",
        "subscribe_events",
        "companion_processes",
        "ui_tab",
        "schema_version",
    }
    extras: Dict[str, Any] = {
        key: value for key, value in data.items() if key not in known
    }

    timeout_raw = data.get("timeout_sec", 60)
    try:
        timeout_sec = int(timeout_raw) if timeout_raw not in (None, "") else 60
    except (TypeError, ValueError):
        timeout_sec = 60

    scripts_raw = data.get("scripts", [])
    scripts: List[Dict[str, str]] = []
    if scripts_raw in (None, ""):
        scripts_raw = []
    if not isinstance(scripts_raw, list):
        raise SkillManifestError("'scripts' must be a list when provided")
    for item in scripts_raw:
        if isinstance(item, dict):
            scripts.append({str(k): str(v) for k, v in item.items()})
        elif isinstance(item, str):
            scripts.append({"name": item})
        else:
            raise SkillManifestError("each 'scripts' item must be a mapping or string")

    ui_tab = data.get("ui_tab")
    if ui_tab is not None and not isinstance(ui_tab, dict):
        raise SkillManifestError("'ui_tab' must be a mapping when provided")

    companion_raw = data.get("companion_processes", [])
    if companion_raw in (None, ""):
        companion_raw = []
    if not isinstance(companion_raw, list):
        raise SkillManifestError("'companion_processes' must be a list when provided")
    companion_processes: List[Dict[str, Any]] = []
    for item in companion_raw:
        if not isinstance(item, dict):
            raise SkillManifestError("each 'companion_processes' item must be a mapping")
        if not str(item.get("name") or "").strip():
            raise SkillManifestError("each 'companion_processes' item must include name")
        if not isinstance(item.get("command"), list) or not item.get("command"):
            raise SkillManifestError("each 'companion_processes' item must include a non-empty command list")
        runtime = str(item.get("runtime") or "").strip().lower()
        if not runtime:
            raise SkillManifestError("each 'companion_processes' item must include runtime")
        if runtime and runtime not in VALID_SKILL_RUNTIMES:
            raise SkillManifestError(
                f"companion_processes runtime '{runtime}' is not supported"
            )
        command0 = str((item.get("command") or [""])[0] or "").strip().lower()
        command = [str(part or "").strip() for part in (item.get("command") or [])]
        inline_flags = {"-c", "-m", "-e", "--eval", "eval"}
        if any(arg in inline_flags for arg in command[1:]):
            raise SkillManifestError("companion inline/eval commands are not allowed")
        for arg in command[1:]:
            arg_path = pathlib.PurePosixPath(arg)
            if arg_path.is_absolute() or ".." in arg_path.parts:
                raise SkillManifestError("companion command arguments must stay inside the reviewed skill tree")
        if runtime in {"python", "python3"} and command0 not in {"python", "python3"}:
            raise SkillManifestError("python companion runtime must use python/python3 command")
        if runtime in {"python", "python3"}:
            if len(command) < 2:
                raise SkillManifestError("python companion command must name a reviewed script")
            if pathlib.PurePosixPath(command[1]).is_absolute() or ".." in pathlib.PurePosixPath(command[1]).parts:
                raise SkillManifestError("python companion script must be a relative reviewed path")
        if runtime in {"node", "npm"} and command0 not in {"node", "npm"}:
            raise SkillManifestError("node companion runtime must use node/npm command")
        if runtime in {"bash", "deno", "ruby", "go"} and command0 != runtime:
            raise SkillManifestError(f"{runtime} companion runtime must use {runtime} command")
        if runtime in {"bash", "deno", "ruby", "go"} and len(command) > 1:
            script_path = pathlib.PurePosixPath(command[1])
            if script_path.is_absolute() or ".." in script_path.parts:
                raise SkillManifestError(f"{runtime} companion script must be a relative reviewed path")
        companion_processes.append(dict(item))

    schema_version = data.get("schema_version", SKILL_MANIFEST_SCHEMA_VERSION)
    try:
        schema_version_int = int(schema_version)
    except (TypeError, ValueError):
        raise SkillManifestError("'schema_version' must be an integer") from None
    if schema_version_int != SKILL_MANIFEST_SCHEMA_VERSION:
        raise SkillManifestError(
            f"unsupported schema_version {schema_version_int}; "
            f"expected {SKILL_MANIFEST_SCHEMA_VERSION}"
        )

    return SkillManifest(
        name=str(data.get("name") or "").strip(),
        description=str(data.get("description") or "").strip(),
        version=str(data.get("version") or "").strip(),
        type=str(data.get("type") or "instruction").strip().lower(),
        when_to_use=str(data.get("when_to_use") or "").strip(),
        requires=_string_list(data.get("requires")),
        os=str(data.get("os") or "any").strip().lower() or "any",
        runtime=str(data.get("runtime") or "").strip().lower(),
        timeout_sec=timeout_sec,
        env_from_settings=_string_list(data.get("env_from_settings")),
        scripts=scripts,
        entry=str(data.get("entry") or "").strip(),
        permissions=_string_list(data.get("permissions")),
        subscribe_events=_string_list(data.get("subscribe_events")),
        companion_processes=companion_processes,
        ui_tab=ui_tab,
        body=body,
        raw_extra=extras,
        schema_version=schema_version_int,
    )


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _derive_name_from_body(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip().lower().replace(" ", "_") or "unnamed"
    return "unnamed"


__all__ = [
    "SKILL_MANIFEST_SCHEMA_VERSION",
    "VALID_SKILL_TYPES",
    "VALID_SKILL_RUNTIMES",
    "VALID_SKILL_PERMISSIONS",
    "SkillManifest",
    "SkillManifestError",
    "parse_skill_manifest_text",
]

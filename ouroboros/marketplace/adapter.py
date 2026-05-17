"""OpenClaw -> Ouroboros frontmatter adapter (v4.50).

Reads a staged ClawHub skill (output of :mod:`ouroboros.marketplace.fetcher`)
and produces a translated ``SKILL.md`` that conforms to Ouroboros's
:class:`ouroboros.contracts.skill_manifest.SkillManifest` shape, while
preserving the original ``SKILL.md`` as ``SKILL.openclaw.md`` for
auditability + later review.

The adapter NEVER:

- Auto-forwards a setting key that overlaps with
  :data:`ouroboros.contracts.plugin_api.FORBIDDEN_SKILL_SETTINGS`.
  Core keys may be preserved in ``env_from_settings`` only as explicit
  per-skill grant requirements; runtime access still requires a fresh executable review
  review plus owner approval bound to the current content hash.
- Normalises ``metadata.openclaw.install`` specs into review-first
  isolated dependency installs where possible; global host mutations
  become manual guidance.
- Trusts a declared ``always: true`` flag — every adapted skill lands
  with ``enabled: false`` and must be opted into by the operator
  through the Skills UI / ``toggle_skill`` after a fresh executable review.

Returns an :class:`AdapterResult` whose ``ok`` field summarises whether
the staged tree can proceed to the install + review stage. ``blockers``
explains every reason for a rejection and ``warnings`` carries
non-blocking notes the UI should surface to the operator.
"""

from __future__ import annotations

import json
import logging
import pathlib
import shutil
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.contracts.plugin_api import FORBIDDEN_SKILL_SETTINGS
from ouroboros.contracts.skill_manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from ouroboros.marketplace.clawhub import (
    _coerce_str_list,
    _extract_metadata_openclaw as _extract_metadata_block,
)
from ouroboros.marketplace.install_specs import install_specs_hash, normalize_install_specs
from ouroboros.utils import atomic_write_json, utc_now_iso

log = logging.getLogger(__name__)


_ALLOWED_RUNTIME_BINS = frozenset({"python", "python3", "bash", "node"})
_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_MAX_SLUG_LEN = 64
ADAPTER_VERSION = "5.5.0"


@dataclass
class AdapterResult:
    """Outcome of :func:`adapt_openclaw_skill`."""

    ok: bool
    sanitized_name: str
    target_dirname: str
    manifest: Optional[SkillManifest] = None
    warnings: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    translated_frontmatter: Dict[str, Any] = field(default_factory=dict)
    original_frontmatter: Dict[str, Any] = field(default_factory=dict)
    original_body: str = ""
    is_plugin: bool = False


def sanitize_clawhub_slug(slug: str) -> str:
    """Convert a ClawHub slug into an on-disk directory basename.

    ``owner/skill`` becomes ``owner__skill`` (double underscore so it
    cannot collide with a native ``owner_skill`` skill). Non-allowed
    characters become a single underscore. Empty/pathological inputs
    fall back to ``"_clawhub_skill"``.
    """
    cleaned = (slug or "").strip()
    if not cleaned:
        return "_clawhub_skill"
    cleaned = cleaned.replace("/", "__").replace("\\", "__")
    cleaned = _NAME_SAFE_RE.sub("_", cleaned)
    cleaned = cleaned.strip("._")
    if not cleaned:
        return "_clawhub_skill"
    return cleaned[:_MAX_SLUG_LEN]


def _read_skill_md(staging_dir: pathlib.Path) -> tuple[str, str, Dict[str, Any]]:
    """Return ``(raw_text, body_after_frontmatter, parsed_frontmatter)``.

    Tries ``SKILL.md`` first then ``skill.json``. ``parsed_frontmatter``
    is whatever ``parse_skill_manifest_text`` recovered, including the
    extras under ``raw_extra``. Raises :class:`SkillManifestError` if
    neither manifest is present (which would normally have been caught
    by the fetcher already, but we re-check defensively).
    """
    skill_md = staging_dir / "SKILL.md"
    skill_json = staging_dir / "skill.json"
    if skill_md.is_file():
        text = skill_md.read_text(encoding="utf-8")
        manifest = parse_skill_manifest_text(text)
        return text, manifest.body, _manifest_frontmatter_dict(manifest)
    if skill_json.is_file():
        text = skill_json.read_text(encoding="utf-8")
        manifest = parse_skill_manifest_text(text)
        return text, "", _manifest_frontmatter_dict(manifest)
    raise SkillManifestError(
        "staged skill has neither SKILL.md nor skill.json after fetcher validation"
    )


def _manifest_frontmatter_dict(manifest: SkillManifest) -> Dict[str, Any]:
    """Reconstruct a frontmatter-shaped dict from a parsed manifest.

    Keeps the parser-extracted ``raw_extra`` intact so downstream code
    can introspect OpenClaw-specific keys (``metadata`` /
    ``allowed-tools`` / ``compatibility`` / ...).
    """
    front: Dict[str, Any] = {
        "name": manifest.name,
        "description": manifest.description,
        "version": manifest.version,
        "type": manifest.type,
        "when_to_use": manifest.when_to_use,
        "requires": list(manifest.requires),
        "os": manifest.os,
        "runtime": manifest.runtime,
        "timeout_sec": manifest.timeout_sec,
        "env_from_settings": list(manifest.env_from_settings),
        "scripts": [dict(s) for s in manifest.scripts],
        "entry": manifest.entry,
        "permissions": list(manifest.permissions),
        "ui_tab": dict(manifest.ui_tab) if manifest.ui_tab else None,
    }
    front.update(manifest.raw_extra or {})
    return front


# ---------------------------------------------------------------------------
# Field translators
# ---------------------------------------------------------------------------


def _json_safe(value: Any) -> Any:
    """Return a JSON-serializable copy for provenance snapshots."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _openclaw_compat_snapshot(
    front: Dict[str, Any],
    metadata_block: Dict[str, Any],
    warnings: List[str],
) -> Dict[str, Any]:
    """Preserve OpenClaw authoring metadata not native to Ouroboros."""

    requires = metadata_block.get("requires") if isinstance(metadata_block, dict) else {}
    if not isinstance(requires, dict):
        requires = {}
    command_fields = {
        key: front.get(key)
        for key in (
            "user-invocable",
            "disable-model-invocation",
            "command-dispatch",
            "command-tool",
            "command-arg-mode",
            "argument-hint",
            "arguments",
        )
        if key in front
    }
    if requires.get("config"):
        warnings.append(
            "OpenClaw metadata declares requires.config gates. Ouroboros "
            "preserves them in provenance but does not treat them as runtime "
            "permissions or auto-enable conditions."
        )
    if metadata_block.get("always") is True:
        warnings.append(
            "OpenClaw metadata declares always=true. Ouroboros ignores it: "
            "marketplace installs still require review and explicit enablement."
        )
    return {
        "adapter_version": ADAPTER_VERSION,
        "metadata_openclaw": _json_safe(metadata_block),
        "requires": {
            "bins": _coerce_str_list(requires.get("bins")),
            "anyBins": _coerce_str_list(requires.get("anyBins")),
            "env": _coerce_str_list(requires.get("env")),
            "config": _coerce_str_list(requires.get("config")),
        },
        "skill_key": str(metadata_block.get("skillKey") or "").strip(),
        "emoji": str(metadata_block.get("emoji") or "").strip(),
        "homepage": str(metadata_block.get("homepage") or front.get("homepage") or front.get("website") or "").strip(),
        "primary_env": str(metadata_block.get("primaryEnv") or "").strip(),
        "always": metadata_block.get("always") is True,
        "command_fields": _json_safe(command_fields),
    }


def _normalise_os(value: Any) -> str:
    """Map OpenClaw ``os: ['darwin','linux']`` to Ouroboros's single-string field."""
    items = _coerce_str_list(value)
    if not items:
        return "any"
    lowered = {x.lower() for x in items}
    aliases = {"macos": "darwin", "win32": "windows", "win": "windows"}
    normalised = {aliases.get(x, x) for x in lowered}
    if normalised >= {"darwin", "linux", "windows"}:
        return "any"
    if len(normalised) == 1:
        only = next(iter(normalised))
        return only if only in {"darwin", "linux", "windows"} else "any"
    # Mixed (e.g. darwin+linux). Use comma-joined sorted list for visibility,
    # with "any" as a safe fallback when validation later refuses it.
    return ",".join(sorted(normalised))


def _detect_runtime(
    metadata_block: Dict[str, Any],
    staging_dir: pathlib.Path,
    warnings: List[str],
) -> str:
    """Decide which runtime the skill scripts target.

    Checks (in order):

    1. ``metadata.openclaw.requires.bins`` declared by the publisher.
    2. Shebangs / file extensions inside ``staging_dir/scripts/`` if
       any script files exist.
    3. ``""`` (empty) when there is no scripts/ directory at all — the
       skill becomes ``type: instruction`` downstream.
    """
    requires = metadata_block.get("requires") or {}
    if not isinstance(requires, dict):
        requires = {}
    bins = _coerce_str_list(requires.get("bins") or requires.get("anyBins"))
    declared = [b.lower() for b in bins if b]
    declared_in_allowlist = [b for b in declared if b in _ALLOWED_RUNTIME_BINS]
    declared_outside = [b for b in declared if b not in _ALLOWED_RUNTIME_BINS]
    if declared_outside:
        warnings.append(
            "Skill declares CLI dependencies outside the allowed runtime "
            f"set ({sorted(_ALLOWED_RUNTIME_BINS)}): {declared_outside}. "
            "Scripts will not be executable; the skill will land as "
            "'type: instruction' (markdown-only)."
        )
    if declared_in_allowlist:
        # Prefer the most common case (python > node > bash) when multiple
        # runtimes are declared but only one is supported by skill_exec.
        for preferred in ("python3", "python", "node", "bash"):
            if preferred in declared_in_allowlist:
                return preferred
    scripts_dir = staging_dir / "scripts"
    if scripts_dir.is_dir():
        for path in scripts_dir.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if name.endswith(".py"):
                return "python3"
            if name.endswith(".js") or name.endswith(".mjs"):
                return "node"
            if name.endswith(".sh") or name.endswith(".bash"):
                return "bash"
    return ""


def _list_scripts_dir(staging_dir: pathlib.Path) -> List[Dict[str, str]]:
    """Return Ouroboros-shaped ``scripts`` entries for files under ``scripts/``."""
    scripts_dir = staging_dir / "scripts"
    out: List[Dict[str, str]] = []
    if not scripts_dir.is_dir():
        return out
    for path in sorted(scripts_dir.iterdir()):
        if not path.is_file() or path.name.startswith("."):
            continue
        out.append({"name": path.name, "description": ""})
    return out


def _translate_permissions(
    metadata_block: Dict[str, Any],
    front: Dict[str, Any],
    warnings: List[str],
) -> List[str]:
    """Approximate Ouroboros permissions from OpenClaw metadata.

    The mapping is deliberately conservative: any time we cannot prove
    a less-permissive set works, we widen the permission and warn so
    the reviewer knows to double-check. Specifically:

    - ``allowed-tools`` containing ``Bash(python:*)`` -> ``subprocess``.
    - ``allowed-tools`` containing ``Read``/``Write`` -> ``fs``.
    - ``metadata.openclaw.requires.bins`` non-empty -> ``subprocess``.
    - ``metadata.openclaw.requires.env`` non-empty AND (likely a
      network credential) -> ``net``.
    - Tokens we did not match (``Edit``, ``Glob``, ``Grep``, ``LS``,
      ``TodoWrite``, ``MultiEdit``, custom verbs, etc.) are recorded
      and surfaced as a single grouped warning so the reviewer knows
      the publisher declared capabilities the adapter could not map —
      those tokens are NEVER silently dropped without a trace.
    """
    perms: set[str] = set()
    unrecognised: List[str] = []
    allowed_tools_raw = front.get("allowed-tools") or front.get("allowed_tools") or ""
    if isinstance(allowed_tools_raw, str):
        tokens = [t.strip() for t in allowed_tools_raw.split() if t.strip()]
    elif isinstance(allowed_tools_raw, list):
        tokens = [str(t).strip() for t in allowed_tools_raw if str(t).strip()]
    else:
        tokens = []
    for token in tokens:
        upper = token.upper()
        matched = False
        if upper.startswith("BASH") or upper.startswith("SHELL"):
            perms.add("subprocess")
            matched = True
        if upper in ("READ", "WRITE", "READFILE", "WRITEFILE", "FS"):
            perms.add("fs")
            matched = True
        if upper.startswith("FETCH") or upper.startswith("HTTP") or upper.startswith("WEB"):
            perms.add("net")
            matched = True
        if not matched:
            unrecognised.append(token)
    if unrecognised:
        warnings.append(
            "OpenClaw 'allowed-tools' tokens not mapped to Ouroboros permissions: "
            f"{sorted(set(unrecognised))}. Reviewer must cross-check "
            "SKILL.openclaw.md to confirm the publisher's declared capabilities "
            "are still honoured by the translated manifest."
        )
    requires = metadata_block.get("requires") or {}
    if isinstance(requires, dict):
        if _coerce_str_list(requires.get("bins")) or _coerce_str_list(requires.get("anyBins")):
            perms.add("subprocess")
        if _coerce_str_list(requires.get("env")):
            # Most ClawHub skills use env vars for API tokens that imply
            # outbound HTTP. We err on the side of declaring ``net``.
            perms.add("net")
    if not perms:
        warnings.append(
            "Could not derive any specific permissions from the OpenClaw "
            "manifest; skill will be installed with an empty permissions "
            "list. Reviewer must verify scripts make no privileged calls."
        )
    return sorted(perms)


def _translate_env_from_settings(
    metadata_block: Dict[str, Any],
    blockers: List[str],
    warnings: Optional[List[str]] = None,
) -> List[str]:
    """Translate the publisher's declared env keys into Ouroboros allowlist.

    Both the adapter and the runtime denylist (``skill_exec._FORBIDDEN_ENV_FORWARD_KEYS``)
    use a CASE-INSENSITIVE comparison: a publisher could otherwise
    declare ``openrouter_api_key`` (lowercase) and slip past the
    sorted-set check, since ``FORBIDDEN_SKILL_SETTINGS`` is canonically
    UPPERCASE. We normalise to UPPER at the boundary and emit the
    canonical form so the runtime + reviewer + UI all agree on the
    allowlisted shape.
    """
    requires = metadata_block.get("requires") or {}
    keys = _coerce_str_list(requires.get("env") if isinstance(requires, dict) else None)
    forbidden_upper = {k.upper() for k in FORBIDDEN_SKILL_SETTINGS}
    blocked: List[str] = []
    out: List[str] = []
    for key in keys:
        canonical = key.strip().upper()
        if not canonical:
            continue
        if canonical in forbidden_upper:
            blocked.append(canonical)
            continue
        out.append(canonical)
    if blocked:
        if warnings is not None:
            warnings.append(
                "OpenClaw manifest requests core settings keys that require "
                f"explicit per-skill grants before execution: {sorted(set(blocked))}."
            )
        else:
            blockers.append(
                "OpenClaw manifest requests core settings keys that require "
                f"explicit per-skill grants before execution: {sorted(set(blocked))}."
            )
    return out + [key for key in sorted(set(blocked)) if key not in out]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_frontmatter(front: Dict[str, Any]) -> str:
    """Render translated frontmatter as YAML in a stable key order."""
    import yaml  # type: ignore

    order = (
        "name", "description", "version", "type", "runtime",
        "timeout_sec", "when_to_use", "permissions", "env_from_settings",
        "os", "requires", "entry", "scripts",
    )
    ordered = {
        key: front[key]
        for key in order
        if key in front and front[key] not in ("", [], None)
    }
    dumped = yaml.safe_dump(
        ordered,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return "---\n" + dumped + "\n---"


def _render_skill_md(translated_front: Dict[str, Any], body: str) -> str:
    body_clean = (body or "").strip()
    if body_clean:
        return f"{_render_frontmatter(translated_front)}\n\n{body_clean}\n"
    return f"{_render_frontmatter(translated_front)}\n"


def _append_manual_install_guidance(body: str, manual_specs: List[Dict[str, Any]]) -> str:
    if not manual_specs:
        return body
    lines = [
        "",
        "## Manual setup required",
        "",
        "Ouroboros refused to run these publisher-declared installers automatically",
        "because they cannot be confined to this skill's isolated dependency directory.",
        "Install the required tools manually only if you trust the upstream project:",
        "",
    ]
    for spec in manual_specs:
        label = spec.get("package") or spec.get("kind") or "dependency"
        reason = spec.get("reason") or "manual setup required"
        lines.append(f"- `{label}`: {reason}")
    return (body or "").rstrip() + "\n" + "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def adapt_openclaw_skill(
    staging_dir: pathlib.Path,
    *,
    slug: str,
    version: str = "",
    sha256: str = "",
    is_plugin: bool = False,
) -> AdapterResult:
    """Translate a staged ClawHub package into Ouroboros's manifest shape.

    On success the staged tree gains:

    - ``SKILL.openclaw.md`` — the original manifest, untouched. Carries
      the OpenClaw frontmatter the registry indexed.
    - ``SKILL.md`` — the translated manifest using Ouroboros's
      vocabulary (``type``, ``runtime``, ``permissions``, ``scripts``,
      ``env_from_settings``).

    The staged directory is otherwise unchanged. Caller is responsible
    for moving it into ``data/skills/clawhub/<sanitized_name>/``.

    On failure ``ok`` is ``False`` and ``blockers`` lists the reasons.
    The caller should call ``StagedSkill.cleanup`` and abort install.
    """
    sanitized = sanitize_clawhub_slug(slug)
    target_dirname = sanitized
    warnings: List[str] = []
    blockers: List[str] = []
    provenance: Dict[str, Any] = {
        "schema_version": 1,
        "source": "clawhub",
        "slug": slug,
        "sanitized_name": sanitized,
        "version": (version or "").strip(),
        "sha256": (sha256 or "").strip(),
        "is_plugin": bool(is_plugin),
        "adapter_version": ADAPTER_VERSION,
        "installed_at": utc_now_iso(),
    }

    if is_plugin:
        blockers.append(
            "Package is an OpenClaw Node/TypeScript plugin (openclaw.plugin.json "
            "present). Ouroboros does not run Node-host plugins; refusing "
            "to install. Ask the author for a Python port or expose via MCP."
        )
        return AdapterResult(
            ok=False,
            sanitized_name=sanitized,
            target_dirname=target_dirname,
            warnings=warnings,
            blockers=blockers,
            provenance=provenance,
            is_plugin=True,
        )

    try:
        original_text, body, original_front = _read_skill_md(staging_dir)
    except SkillManifestError as exc:
        blockers.append(f"Manifest unreadable: {exc}")
        return AdapterResult(
            ok=False,
            sanitized_name=sanitized,
            target_dirname=target_dirname,
            warnings=warnings,
            blockers=blockers,
            provenance=provenance,
        )

    metadata_block = _extract_metadata_block(original_front)
    openclaw_compat = _openclaw_compat_snapshot(original_front, metadata_block, warnings)

    raw_install_specs = metadata_block.get("install")
    auto_install_specs, manual_install_specs, install_warnings = normalize_install_specs(raw_install_specs)
    warnings.extend(install_warnings)
    if auto_install_specs:
        warnings.append(
            "Skill declares dependency install specs. Ouroboros will land the "
            "payload disabled, require a fresh executable review, then install these "
            "dependencies only inside the skill's .ouroboros_env directory."
        )
    if manual_install_specs:
        warnings.append(
            "Some install specs were converted to manual setup guidance because "
            "they cannot be isolated without mutating global host state."
        )

    env_keys = _translate_env_from_settings(metadata_block, blockers, warnings)

    runtime = _detect_runtime(metadata_block, staging_dir, warnings)
    scripts_entries = _list_scripts_dir(staging_dir)
    if runtime and not scripts_entries:
        warnings.append(
            f"Runtime '{runtime}' detected but no executable files found "
            "under scripts/; skill becomes 'type: instruction'."
        )
    if not runtime:
        skill_type = "instruction"
    elif scripts_entries:
        skill_type = "script"
    else:
        skill_type = "instruction"

    permissions = _translate_permissions(metadata_block, original_front, warnings)
    if skill_type != "script":
        # Strip subprocess permission for instruction-only skills — they
        # have no executable surface to invoke.
        permissions = [p for p in permissions if p != "subprocess"]

    name = sanitized
    description = str(original_front.get("description") or "").strip()
    when_to_use = str(original_front.get("when_to_use") or "").strip()
    if not when_to_use and description:
        # OpenClaw uses ``description`` as the activation trigger; mirror it
        # into Ouroboros's dedicated ``when_to_use`` so the agent's
        # context-builder sees the same trigger surface.
        when_to_use = description

    os_field = _normalise_os(metadata_block.get("os"))
    if os_field not in {"any", "darwin", "linux", "windows"}:
        warnings.append(
            f"OS restriction '{os_field}' could not be normalised to a single "
            "OS literal; falling back to 'any' so the skill is discoverable."
        )
        os_field = "any"

    homepage = str(original_front.get("homepage") or original_front.get("website") or "").strip()
    license_field = str(original_front.get("license") or "").strip()
    primary_env = str(metadata_block.get("primaryEnv") or "").strip()

    # Recorded in provenance only — NOT folded into the rendered
    # SKILL.md (avoids dragging untrusted publisher URLs through any
    # future markdown-to-HTML rendering path; the marketplace UI reads
    # these from the provenance record directly via /api/marketplace/clawhub/installed).
    provenance_extras: Dict[str, Any] = {"clawhub_slug": slug}
    if homepage:
        provenance_extras["homepage"] = homepage
    if license_field:
        provenance_extras["license"] = license_field
    if primary_env:
        provenance_extras["primary_env"] = primary_env
    if raw_install_specs not in (None, "", [], {}):
        provenance_extras["install_specs"] = {
            "schema_version": 1,
            "auto": auto_install_specs,
            "manual": manual_install_specs,
            "raw": _json_safe(raw_install_specs),
            "specs_hash": install_specs_hash(auto_install_specs),
        }
    requested_grants = [
        key for key in env_keys
        if key.upper() in {item.upper() for item in FORBIDDEN_SKILL_SETTINGS}
    ]
    if requested_grants:
        provenance_extras["requested_key_grants"] = requested_grants

    raw_timeout = original_front.get("timeout_sec")
    if raw_timeout in (None, ""):
        timeout_sec = 60
    else:
        try:
            timeout_sec = int(raw_timeout)
        except (TypeError, ValueError):
            warnings.append(
                f"Manifest timeout_sec={raw_timeout!r} is not integer-valued; "
                "defaulting to 60s. Reviewer should confirm the publisher's "
                "intent (some OpenClaw skills ship suffixes like '60s'."
            )
            timeout_sec = 60
        if timeout_sec <= 0:
            warnings.append(
                f"Manifest timeout_sec={raw_timeout!r} must be positive; defaulting to 60s."
            )
            timeout_sec = 60

    translated_front: Dict[str, Any] = {
        "name": name,
        "description": description or f"ClawHub skill {slug}",
        "version": str(original_front.get("version") or version or "").strip(),
        "type": skill_type,
        "runtime": runtime if skill_type == "script" else "",
        "timeout_sec": timeout_sec,
        "when_to_use": when_to_use,
        "permissions": permissions,
        "env_from_settings": env_keys,
        "os": os_field,
        "scripts": scripts_entries if skill_type == "script" else [],
        "schema_version": SKILL_MANIFEST_SCHEMA_VERSION,
    }

    rendered_body = _append_manual_install_guidance(body, manual_install_specs)
    rendered_skill_md = _render_skill_md(translated_front, rendered_body)
    try:
        new_manifest = parse_skill_manifest_text(rendered_skill_md)
    except SkillManifestError as exc:
        blockers.append(
            f"Adapter produced an unparseable Ouroboros manifest: {exc}. "
            "This is an internal bug; please report."
        )
        return AdapterResult(
            ok=False,
            sanitized_name=sanitized,
            target_dirname=target_dirname,
            warnings=warnings,
            blockers=blockers,
            provenance=provenance,
            translated_frontmatter=translated_front,
            original_frontmatter=original_front,
            original_body=body,
        )

    manifest_warnings = new_manifest.validate()
    for w in manifest_warnings:
        warnings.append(f"manifest validate: {w}")

    provenance["original_manifest_sha256"] = _sha256_of_text(original_text)
    provenance["translated_manifest_sha256"] = _sha256_of_text(rendered_skill_md)
    provenance["adapter_warnings"] = list(warnings)
    provenance["openclaw_compat"] = openclaw_compat
    provenance["original_frontmatter"] = _json_safe(original_front)
    provenance.update(provenance_extras)

    if blockers:
        return AdapterResult(
            ok=False,
            sanitized_name=sanitized,
            target_dirname=target_dirname,
            manifest=new_manifest,
            warnings=warnings,
            blockers=blockers,
            provenance=provenance,
            translated_frontmatter=translated_front,
            original_frontmatter=original_front,
            original_body=body,
        )

    # Persist both manifests inside the staging directory. The fetcher
    # already validated that the staging dir is otherwise safe to copy.
    skill_md_path = staging_dir / "SKILL.md"
    openclaw_path = staging_dir / "SKILL.openclaw.md"
    try:
        # Move original aside, write translated SKILL.md.
        if openclaw_path.exists():
            openclaw_path.unlink()
        if skill_md_path.exists():
            shutil.copy2(str(skill_md_path), str(openclaw_path))
        else:
            # Rare: skill.json was used instead — preserve as-is.
            (staging_dir / "skill.openclaw.json").write_text(original_text, encoding="utf-8")
        skill_md_path.write_text(rendered_skill_md, encoding="utf-8")
        # Drop a sidecar provenance marker so a quick directory listing
        # makes the source obvious to humans, without depending on the
        # durable state plane.
        atomic_write_json(staging_dir / ".clawhub.json", provenance, trailing_newline=True)
    except OSError as exc:
        blockers.append(f"Failed to persist translated manifest: {exc}")
        return AdapterResult(
            ok=False,
            sanitized_name=sanitized,
            target_dirname=target_dirname,
            manifest=new_manifest,
            warnings=warnings,
            blockers=blockers,
            provenance=provenance,
            translated_frontmatter=translated_front,
            original_frontmatter=original_front,
            original_body=body,
        )

    return AdapterResult(
        ok=True,
        sanitized_name=sanitized,
        target_dirname=target_dirname,
        manifest=new_manifest,
        warnings=warnings,
        blockers=blockers,
        provenance=provenance,
        translated_frontmatter=translated_front,
        original_frontmatter=original_front,
        original_body=body,
    )


def _sha256_of_text(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


__all__ = [
    "ADAPTER_VERSION",
    "AdapterResult",
    "adapt_openclaw_skill",
    "sanitize_clawhub_slug",
]

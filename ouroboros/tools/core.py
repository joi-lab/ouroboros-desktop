"""File/data tools plus code search and digest helpers."""

from __future__ import annotations

import ast
import fnmatch
import json
import logging
import os
import pathlib
import re
import uuid
from typing import Any, Dict, List, Tuple

from ouroboros.artifacts import artifact_store_path_block_reason, copy_file_to_task_artifacts
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.tool_access import (
    decide_tool_access,
    active_tool_profile,
    normalize_root,
    resolve_user_file_path,
    resolve_resource_path,
    resource_root_path,
    user_files_path_block_reason,
)
from ouroboros.tool_capabilities import LOCAL_READONLY_SUBAGENT_MODE
from ouroboros.utils import atomic_write_json, read_text, safe_relpath, utc_now_iso
from ouroboros.contracts.task_constraint import normalize_task_constraint, resolve_payload_path
from ouroboros.contracts.skill_payload_policy import (
    SKILL_PAYLOAD_ALL_BUCKETS,
    SKILL_OWNER_STATE_FILENAMES,
    SkillPayloadPathError,
    cross_skill_redirect_error,
    decide_payload_short_form,
    is_skill_control_plane_path as _policy_is_skill_control_plane_path,
    is_skill_owner_state_alias,
    is_skill_owner_state_target as _policy_is_skill_owner_state_target,
    resolve_skill_payload_target,
)

log = logging.getLogger(__name__)

_SKILL_OWNER_STATE_FILENAMES = SKILL_OWNER_STATE_FILENAMES

# Payload-local provenance sidecars are launcher/marketplace-owned, not
# skill-author-editable. Generic write/delete/upload paths must block them.
_SELF_AUTHORED_MARKER = ".self_authored.json"


def _render_line_slice(path: str, content: str, max_lines: int = 2000, start_line: int = 1) -> str:
    """Return a line-ranged file view with the shared read-tool header."""
    start_raw, max_raw = _coerce_line_window(start_line, max_lines)
    max_raw = max(1, max_raw)
    lines = content.splitlines(keepends=True)
    total = len(lines)
    start = max(1, min(start_raw, total + 1))
    end = min(start + max_raw - 1, total)
    result = "".join(lines[start - 1:end])
    header = f"# {path} — lines {start}\u2013{end} of {total}\n"
    return header + result


def _coerce_line_window(start_line: Any = 1, max_lines: Any = 2000) -> tuple[int, int]:
    try:
        start_raw = int(start_line)
    except (TypeError, ValueError):
        start_raw = 1
    try:
        max_raw = int(max_lines)
    except (TypeError, ValueError):
        max_raw = 2000
    return start_raw, max(1, max_raw)


def _is_cognitive_data_path(norm: str) -> bool:
    text = str(norm or "").replace("\\", "/").lstrip("./")
    return text.startswith("memory/") or text in _MEMORY_AT_DRIVE_MEMORY


def _skill_payload_parts(target: pathlib.Path, data_root: pathlib.Path) -> tuple[str, str, pathlib.Path] | None:
    """Return (bucket, skill, payload_root) for data/skills payload paths."""
    for candidate in (target, pathlib.Path(target).resolve(strict=False)):
        try:
            rel = candidate.relative_to(data_root)
        except (OSError, ValueError):
            continue
        parts = rel.parts
        if len(parts) < 3 or parts[0].lower() != "skills":
            continue
        bucket = parts[1]
        if bucket.lower() not in SKILL_PAYLOAD_ALL_BUCKETS:
            continue
        skill_name = parts[2]
        if not skill_name or skill_name in {".", ".."}:
            continue
        return bucket.lower(), skill_name, data_root / "skills" / bucket / skill_name
    return None


def _native_payload_without_seed(target: pathlib.Path, data_root: pathlib.Path) -> bool:
    payload = _skill_payload_parts(target, data_root)
    if payload is None:
        return False
    bucket, _skill_name, payload_root = payload
    return bucket == "native" and not (payload_root / ".seed-origin").is_file()


def _data_skill_path(path: str, drive_root: pathlib.Path) -> pathlib.Path | None:
    try:
        return resolve_skill_payload_target(pathlib.Path(drive_root), path).target_path
    except SkillPayloadPathError:
        return None


def _looks_like_serialized_tool_result(content: Any) -> bool:
    text = str(content or "").lstrip()
    if not (text.startswith("{'content'") or text.startswith('{"content"')):
        return False
    try:
        parsed = ast.literal_eval(text)
    except Exception:
        try:
            parsed = json.loads(text)
        except Exception:
            return False
    return isinstance(parsed, dict) and isinstance(parsed.get("content"), str)


def _is_skill_owner_state_target(target: pathlib.Path, data_root: pathlib.Path) -> bool:
    return _policy_is_skill_owner_state_target(target, data_root)


def is_skill_control_plane_path(target: pathlib.Path, data_root: pathlib.Path) -> bool:
    """Return True for skill owner/provenance files blocked from generic writes."""
    return _policy_is_skill_control_plane_path(target, data_root)


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> List[str]:
    target = (root / safe_relpath(rel)).resolve()
    if not target.exists():
        return [f"⚠️ Directory not found: {rel}"]
    if not target.is_dir():
        return [f"⚠️ Not a directory: {rel}"]
    items = []
    try:
        for entry in sorted(target.iterdir()):
            if len(items) >= max_entries:
                items.append(f"...(truncated at {max_entries})")
                break
            suffix = "/" if entry.is_dir() else ""
            items.append(str(entry.relative_to(root)) + suffix)
    except Exception as e:
        items.append(f"⚠️ Error listing: {e}")
    return items


def _list_user_files_dir(ctx: ToolContext, root: pathlib.Path, target: pathlib.Path, max_entries: int = 500) -> List[str]:
    if not target.exists():
        return [f"⚠️ Directory not found: {target}"]
    if not target.is_dir():
        return [f"⚠️ Not a directory: {target}"]
    items: List[str] = []
    hidden = 0
    try:
        for entry in sorted(target.iterdir()):
            if user_files_path_block_reason(ctx, entry):
                hidden += 1
                continue
            if len(items) >= max_entries:
                items.append(f"...(truncated at {max_entries})")
                break
            suffix = "/" if entry.is_dir() else ""
            items.append(str(entry.relative_to(root)) + suffix)
    except Exception as e:
        items.append(f"⚠️ Error listing: {e}")
    if hidden:
        items.append(f"⚠️ {hidden} hidden/control entr{'y' if hidden == 1 else 'ies'} omitted from user_files listing.")
    return items


_SUBAGENT_SECRET_FILE_NAMES = frozenset({
    ".env",
    ".netrc",
    "auth.json",
    "credentials",
    "credentials.json",
    "keys.json",
    "secret.json",
    "secrets.json",
    "settings.json",
    "settings.json.lock",
    "token.json",
    "tokens.json",
})


def _is_local_readonly_subagent(ctx: ToolContext) -> bool:
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    return bool(task_constraint and task_constraint.mode == LOCAL_READONLY_SUBAGENT_MODE)


def _is_subagent_secret_data_path(norm: str) -> bool:
    text = str(norm or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return False
    parts = [part.lower() for part in text.split("/") if part and part != "."]
    if not parts:
        return False
    if any(part in {"auth", "credentials", "secrets", "tokens"} for part in parts):
        return True
    name = parts[-1]
    normalized_names = {name, name.lstrip(".")}
    if name.lstrip(".") == "settings.tmp":
        normalized_names.add("settings.json")
    for protected_name in (_SUBAGENT_SECRET_FILE_NAMES | _SKILL_OWNER_STATE_FILENAMES):
        bare = name.lstrip(".")
        if bare.startswith(f"{protected_name}.tmp") or bare.startswith(f"{protected_name}.lock"):
            normalized_names.add(protected_name)
    if normalized_names & (_SUBAGENT_SECRET_FILE_NAMES | _SKILL_OWNER_STATE_FILENAMES):
        return True
    if name.startswith(".env") or name.endswith(".env") or ".env." in name:
        return True
    if name.endswith((".key", ".pem", ".p12", ".pfx")):
        return True
    return bool(re.search(r"(?:^|[._-])(api[_-]?key|credential|password|secret|token)(?:[._-]|$)", name))


def _is_subagent_secret_repo_path(norm: str) -> bool:
    text = str(norm or "").replace("\\", "/").strip()
    while text.startswith("./"):
        text = text[2:]
    parts = [part.lower() for part in text.split("/") if part and part != "."]
    if ".git" in parts or any(part in {"auth", "credentials", "secrets", "tokens"} for part in parts):
        return True
    if not parts:
        return False
    name = parts[-1]
    if name in _SUBAGENT_SECRET_FILE_NAMES or name == "settings.tmp":
        return True
    if name.startswith(".env") or name.endswith(".env") or ".env." in name:
        return True
    if name.endswith((".key", ".pem", ".p12", ".pfx")):
        return True
    if re.search(r"(?:^|[._-])(api[_-]?key|credential|password|secret|token)(?:[._-]|$)", name):
        suffix = pathlib.PurePosixPath(name).suffix.lower()
        return suffix in {"", ".json", ".env", ".key", ".pem", ".p12", ".pfx", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf"}
    return False


def _is_subagent_secret_repo_target(target: pathlib.Path, repo_root: pathlib.Path) -> bool:
    root = pathlib.Path(repo_root).resolve(strict=False)
    try:
        rel = str(pathlib.Path(target).resolve(strict=False).relative_to(root)).replace(os.sep, "/")
    except (OSError, ValueError):
        rel = str(target).replace(os.sep, "/")
    if _is_subagent_secret_repo_path(rel):
        return True
    secret_candidates = [
        root / ".git" / "credentials",
        root / ".git" / "config",
    ]
    try:
        secret_candidates.extend(
            candidate
            for candidate in root.iterdir()
            if candidate.is_file() and _is_subagent_secret_repo_path(candidate.name)
        )
    except OSError:
        pass
    return any(
        candidate.is_file()
        and target.exists()
        and target.samefile(candidate)
        for candidate in secret_candidates
    )


def _filter_subagent_secret_repo_listing(items: List[str], repo_root: pathlib.Path) -> List[str]:
    filtered: List[str] = []
    redacted = 0
    root = pathlib.Path(repo_root).resolve(strict=False)
    for item in items:
        marker = item.rstrip("/")
        if marker.startswith("⚠️") or marker.startswith("...("):
            filtered.append(item)
            continue
        if _is_subagent_secret_repo_path(marker) or _is_subagent_secret_repo_target(root / marker, root):
            redacted += 1
            continue
        filtered.append(item)
    if redacted:
        filtered.append(f"⚠️ {redacted} secret/control entr{'y' if redacted == 1 else 'ies'} hidden from local_readonly_subagent.")
    return filtered


def _filter_subagent_secret_listing(items: List[str], data_root: pathlib.Path) -> List[str]:
    filtered: List[str] = []
    redacted = 0
    root = pathlib.Path(data_root).resolve(strict=False)
    for item in items:
        marker = item.rstrip("/")
        if marker.startswith("⚠️") or marker.startswith("...("):
            filtered.append(item)
            continue
        target = root / marker
        try:
            resolved_rel = str(pathlib.Path(target).resolve(strict=False).relative_to(root)).replace(os.sep, "/")
        except (OSError, ValueError):
            resolved_rel = marker
        if (
            _is_subagent_secret_data_path(marker)
            or _is_subagent_secret_data_path(resolved_rel)
            or _is_skill_owner_state_target(target, root)
            or is_skill_owner_state_alias(target, root)
            or any(
                candidate.is_file()
                and _is_subagent_secret_data_path(candidate.name)
                and target.exists()
                and target.samefile(candidate)
                for candidate in root.iterdir()
            )
        ):
            redacted += 1
            continue
        filtered.append(item)
    if redacted:
        filtered.append(f"⚠️ {redacted} secret/control entr{'y' if redacted == 1 else 'ies'} hidden from local_readonly_subagent.")
    return filtered


_MEMORY_AT_DRIVE_MEMORY = frozenset({
    "identity.md", "scratchpad.md", "dialogue_summary.md",
    "dialogue_blocks.json", "registry.md", "deep_review.md",
    "WORLD.md",
})


def _repo_read(
    ctx: ToolContext,
    path: str,
    max_lines: int = 2000,
    start_line: int = 1,
    display_path: str | None = None,
) -> str:
    """Read a repo file; root-level memory names return a runtime_data read hint."""
    target = ctx.repo_path(path)
    if _is_local_readonly_subagent(ctx) and _is_subagent_secret_repo_target(target, active_repo_dir_for(ctx)):
        return "⚠️ REPO_READ_BLOCKED: local_readonly_subagent cannot read repo secret or control files."
    try:
        content = read_text(target)
    except FileNotFoundError:
        norm = path.strip().lstrip("./").replace("\\", "/")
        base = norm.rsplit("/", 1)[-1]
        if "/" not in norm and base in _MEMORY_AT_DRIVE_MEMORY:
            title = base.split('.')[0].title()
            return (
                f"⚠️ NOT_FOUND: '{path}' is not at the repo root.\n\n"
                f"This file lives at `data_root/memory/{base}`, not in the "
                f"git repo. Some memory artifacts are already summarized in "
                f"context as `## {title}`, but raw memory state must be read "
                f"from the data root. If you need the raw file, call "
                f"`read_file(root='runtime_data', path='memory/{base}')`."
            )
        raise
    return _render_line_slice(display_path or path, content, max_lines=max_lines, start_line=start_line)


def _repo_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    repo_root = active_repo_dir_for(ctx)
    target = ctx.repo_path(dir)
    if _is_local_readonly_subagent(ctx) and _is_subagent_secret_repo_target(target, repo_root):
        return json.dumps(
            ["⚠️ REPO_LIST_BLOCKED: local_readonly_subagent cannot list repo secret or control paths."],
            ensure_ascii=False,
            indent=2,
        )
    items = _list_dir(repo_root, dir, max_entries)
    if _is_local_readonly_subagent(ctx):
        items = _filter_subagent_secret_repo_listing(items, repo_root)
    return json.dumps(items, ensure_ascii=False, indent=2)


def _normalize_data_read_path(ctx: ToolContext, path: str) -> str:
    """Normalize paths that redundantly include the drive root."""

    norm = str(path).strip().replace("\\", "/")
    if norm.startswith("./"):
        norm = norm[2:]
    drive_str = str(ctx.drive_root).rstrip("/")
    drive_no_lead = drive_str.lstrip("/")
    if drive_no_lead and norm.lstrip("/").startswith(drive_no_lead):
        stripped = norm.lstrip("/")
        norm = stripped[len(drive_no_lead):].lstrip("/")
    elif norm.startswith(".tmp-data-") or norm.lstrip("/").startswith(".tmp-data-"):
        candidate = norm.lstrip("/")
        first_slash = candidate.find("/")
        if first_slash > 0:
            after = candidate[first_slash + 1:]
            if after.startswith("data/"):
                norm = after[len("data/"):]
            else:
                norm = after
    return norm


def _data_read(
    ctx: ToolContext,
    path: str,
    max_lines: int = 2000,
    start_line: int = 1,
    display_path: str | None = None,
) -> str:
    """Read a drive text file; duplicate drive_root prefixes are stripped."""
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    norm = _normalize_data_read_path(ctx, path)
    if _is_local_readonly_subagent(ctx) and _is_subagent_secret_data_path(norm):
        return "⚠️ DATA_READ_BLOCKED: local_readonly_subagent cannot read secret or owner-control data files."
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            target = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, norm)
        except ValueError as e:
            return f"⚠️ DATA_READ_BLOCKED: {e}"
    else:
        target = ctx.drive_path(norm)
    if _is_local_readonly_subagent(ctx):
        root = pathlib.Path(ctx.drive_root).resolve(strict=False)
        try:
            resolved_rel = str(pathlib.Path(target).resolve(strict=False).relative_to(root)).replace(os.sep, "/")
        except (OSError, ValueError):
            resolved_rel = norm
        if (
            _is_subagent_secret_data_path(resolved_rel)
            or _is_skill_owner_state_target(target, root)
            or is_skill_owner_state_alias(target, root)
            or any(
                candidate.is_file()
                and _is_subagent_secret_data_path(candidate.name)
                and pathlib.Path(target).exists()
                and pathlib.Path(target).samefile(candidate)
                for candidate in root.iterdir()
            )
        ):
            return "⚠️ DATA_READ_BLOCKED: local_readonly_subagent cannot read secret or owner-control data files."
    if (
        _is_skill_owner_state_target(target, pathlib.Path(ctx.drive_root))
        and target.name.lower() != "review.json"
    ):
        return "DATA_READ_BLOCKED: skill owner state is not readable through generic data tools."
    try:
        content = read_text(target)
        start_raw, max_raw = _coerce_line_window(start_line, max_lines)
        if _is_cognitive_data_path(norm) and start_raw == 1 and max_raw == 2000:
            if display_path is None:
                return content
            full_line_count = max(1, len(content.splitlines()))
            return _render_line_slice(display_path, content, max_lines=full_line_count, start_line=1)
        return _render_line_slice(display_path or norm, content, max_lines=max_raw, start_line=start_raw)
    except FileNotFoundError:
        if norm.replace("\\", "/").startswith("memory/"):
            explanation = (
                "Memory artifacts under memory/ are created lazily on first "
                "write. Treat this as an empty/absent state and proceed with "
                "initialization if that is the task."
            )
        else:
            explanation = (
                "This path does not exist yet. Treat it as an empty/absent "
                "state. Lazy-creation is not guaranteed for paths outside "
                "memory/; if this path was expected to exist, verify it was "
                "written correctly."
            )
        return (
            f"⚠️ DATA_NOT_YET_CREATED: {path}\n\n"
            f"{explanation} Use list_files with root=runtime_data to confirm what currently exists."
        )


def _data_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    norm_dir = _normalize_data_read_path(ctx, dir)
    if _is_local_readonly_subagent(ctx) and _is_subagent_secret_data_path(norm_dir):
        return json.dumps(
            ["⚠️ DATA_LIST_BLOCKED: local_readonly_subagent cannot list secret or owner-control data paths."],
            ensure_ascii=False,
            indent=2,
        )
    if _is_local_readonly_subagent(ctx):
        try:
            list_target = ctx.drive_path(norm_dir)
        except ValueError as e:
            return json.dumps([f"⚠️ DATA_LIST_BLOCKED: {e}"], ensure_ascii=False, indent=2)
        root = pathlib.Path(ctx.drive_root).resolve(strict=False)
        if _is_skill_owner_state_target(list_target, root) or is_skill_owner_state_alias(list_target, root):
            return json.dumps(
                ["⚠️ DATA_LIST_BLOCKED: local_readonly_subagent cannot list secret or owner-control data paths."],
                ensure_ascii=False,
                indent=2,
            )
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            root = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, dir)
        except ValueError as e:
            return json.dumps([f"⚠️ DATA_LIST_BLOCKED: {e}"], ensure_ascii=False, indent=2)
        items = _list_dir(root, ".", max_entries)
        return json.dumps(items, ensure_ascii=False, indent=2)
    items = _list_dir(ctx.drive_root, dir, max_entries)
    if _is_local_readonly_subagent(ctx):
        items = _filter_subagent_secret_listing(items, pathlib.Path(ctx.drive_root))
    return json.dumps(items, ensure_ascii=False, indent=2)


def _data_write(
    ctx: ToolContext,
    path: str,
    content: str,
    mode: str = "overwrite",
    bucket: str = "",
    skill_name: str = "",
    display_root: str = "runtime_data",
) -> str:
    # bucket+skill_name synthesize a payload-confined skill_repair constraint.
    short_form = decide_payload_short_form(
        bucket=bucket,
        skill_name=skill_name,
        path_text=path,
        repo_dir=pathlib.Path(ctx.repo_dir),
        drive_root=pathlib.Path(ctx.drive_root),
    )
    if short_form.error:
        return f"⚠️ DATA_WRITE_ERROR: {short_form.error}"
    synth = short_form.constraint
    existing_tc = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    redirect_err = cross_skill_redirect_error(existing_tc, synth)
    if redirect_err:
        return f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}"
    # Real skill_repair confinement wins over synthesized short-form context.
    if existing_tc and existing_tc.mode == "skill_repair":
        task_constraint = existing_tc
    else:
        task_constraint = synth or existing_tc
    write_path = _normalize_data_read_path(ctx, path)
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            p = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, path)
        except ValueError as e:
            return f"⚠️ DATA_WRITE_ERROR: {e}"
    else:
        explicit_skill_target = _data_skill_path(path, pathlib.Path(ctx.drive_root))
        p = explicit_skill_target if explicit_skill_target is not None else ctx.drive_path(write_path)
    # Defense-in-depth: settings.json is owner-only. Use inode-aware matching
    # for symlinks/hardlinks/case-insensitive APFS/NTFS, with a fallback for
    # not-yet-existing case variants.
    from ouroboros import config as _cfg
    target_path = pathlib.Path(p)
    settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
    data_root = pathlib.Path(_cfg.DATA_DIR).resolve(strict=False)
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        lexical_target = pathlib.Path(p).resolve(strict=False)
    else:
        lexical_target = pathlib.Path(ctx.drive_root).resolve(strict=False) / safe_relpath(write_path)
    suffix = pathlib.PurePosixPath(str(path or "")).suffix.lower()
    if suffix in {".py", ".md", ".json", ".sh"} and _looks_like_serialized_tool_result(content):
        return (
            "⚠️ DATA_WRITE_BLOCKED: content looks like a serialized tool result "
            "object (for example {'content': ...}) rather than file text. "
            "Extract the actual file body before calling write_file."
        )
    if _native_payload_without_seed(lexical_target, data_root) or _native_payload_without_seed(target_path, data_root):
        return (
            "⚠️ DATA_WRITE_BLOCKED: data/skills/native/<skill>/ is reserved "
            "for launcher-seeded skills that carry a .seed-origin marker. "
            "Write user- or agent-authored skill payloads under "
            "data/skills/external/<skill>/ instead."
        )
    skill_owner_state_path = (
        _is_skill_owner_state_target(lexical_target, data_root)
        or _is_skill_owner_state_target(target_path, data_root)
    )
    if not skill_owner_state_path:
        skill_owner_state_path = is_skill_owner_state_alias(target_path, data_root)
    if skill_owner_state_path:
        return (
            "⚠️ DATA_WRITE_BLOCKED: skill review, enablement, grants, and "
            "marketplace provenance are owner/review controlled state. Edit "
            "the skill payload under data/skills/ and use skill_review, the "
            "Skills UI toggle, or the desktop launcher grant flow."
        )
    # Block marketplace/launcher sidecars for every data_write path, not only heal mode.
    if is_skill_control_plane_path(lexical_target, data_root) or is_skill_control_plane_path(target_path, data_root):
        return (
            "⚠️ DATA_WRITE_BLOCKED: marketplace provenance and launcher "
            "seed markers (.clawhub.json, .ouroboroshub.json, "
            "SKILL.openclaw.md, .seed-origin) are owner/review controlled. "
            "Edit the payload's user-authored files instead and rerun skill_review."
        )
    matches = False
    try:
        if target_path.exists() and settings_path.exists():
            matches = target_path.samefile(settings_path)
    except OSError:
        matches = False
    if not matches:
        try:
            same_parent = target_path.parent.resolve() == settings_path.parent.resolve()
        except OSError:
            same_parent = False
        if same_parent and target_path.name.lower() == settings_path.name.lower():
            matches = True
    if matches:
        return (
            "⚠️ DATA_WRITE_BLOCKED: settings.json is the canonical owner-edited "
            "file. Tool-level writes must route through /api/settings (which "
            "applies key-by-key policy — OUROBOROS_RUNTIME_MODE is owner-only "
            "and dropped on POST; other keys flow through normally). To change "
            "owner-only values, stop the agent, edit ~/Ouroboros/data/settings.json "
            "directly, then restart."
        )
    marker_payload = _skill_payload_parts(lexical_target, data_root) or _skill_payload_parts(target_path, data_root)
    should_mark_self_authored = False
    marker_path: pathlib.Path | None = None
    if (
        mode == "overwrite"
        and not (task_constraint and task_constraint.mode == "skill_repair")
        and marker_payload is not None
        and marker_payload[0] == "external"
        and pathlib.PurePosixPath(str(path or "")).name.lower() in {"skill.md", "skill.json"}
        and not target_path.exists()
    ):
        marker_path = marker_payload[2] / _SELF_AUTHORED_MARKER
        should_mark_self_authored = not marker_path.exists()

    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        p.write_text(content, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    if should_mark_self_authored and marker_path is not None:
        from ouroboros.skill_loader import compute_content_hash

        marker_payload[2].mkdir(parents=True, exist_ok=True)
        try:
            initial_hash = compute_content_hash(marker_payload[2])
        except Exception:
            initial_hash = ""
        marker_payload_data = {
            "schema_version": 1,
            "origin": "self_authored",
            "created_at": utc_now_iso(),
            "chat_id": int(getattr(ctx, "current_chat_id", 0) or 0),
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            "created_by_tool": "data_write",
            "initial_content_hash": initial_hash,
        }
        atomic_write_json(marker_path, marker_payload_data, trailing_newline=True)
        state_marker = pathlib.Path(ctx.drive_root) / "state" / "skills" / marker_payload[1] / "self_authored.json"
        state_marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_marker, marker_payload_data, trailing_newline=True)
    result = f"OK: wrote {mode} {_root_display_path(display_root, write_path)} ({len(content)} chars)"
    if short_form.ignored_reason:
        result += f"\n⚠️ SKILL_SHORT_FORM_IGNORED: {short_form.ignored_reason}."
    return result


def _access_or_block(ctx: ToolContext, root: str, operation: str) -> tuple[str, str]:
    try:
        normalized = normalize_root(root)
    except ValueError as exc:
        return "", f"⚠️ TOOL_ARG_ERROR: {exc}"
    profile = active_tool_profile(ctx)
    decision = decide_tool_access(profile=profile, root=normalized, operation=operation)  # type: ignore[arg-type]
    if not decision.allow:
        return "", f"⚠️ TOOL_ACCESS_BLOCKED: {decision.reason}"
    return normalized, ""


def _local_readonly_resource_block(
    ctx: ToolContext,
    normalized: str,
    target: pathlib.Path,
    base: pathlib.Path,
    *,
    action: str,
) -> str:
    if not _is_local_readonly_subagent(ctx):
        return ""
    if normalized in {"active_workspace", "system_repo"}:
        if _is_subagent_secret_repo_target(target, pathlib.Path(base)):
            return f"⚠️ {action}_BLOCKED: local_readonly_subagent cannot access repo secret or control paths."
        return ""
    if normalized in {"runtime_data", "task_drive", "skill_payload", "artifact_store", "user_files"}:
        root = pathlib.Path(base).resolve(strict=False)
        try:
            rel = pathlib.Path(target).resolve(strict=False).relative_to(root).as_posix()
        except (OSError, ValueError):
            rel = str(target).replace(os.sep, "/")
        data_root = pathlib.Path(ctx.drive_root).resolve(strict=False)
        if (
            _is_subagent_secret_data_path(rel)
            or _is_skill_owner_state_target(target, data_root)
            or is_skill_owner_state_alias(target, data_root)
        ):
            return f"⚠️ {action}_BLOCKED: local_readonly_subagent cannot access secret or owner-control data files."
    return ""


def _root_display_path(root: str, path: str) -> str:
    rel = safe_relpath(str(path or "."))
    if rel.startswith("./"):
        rel = rel[2:]
    return f"{root}:{rel or '.'}"


def _join_write_results(results: List[str]) -> str:
    rendered = "\n".join(results) if results else "⚠️ TOOL_ARG_ERROR: files must contain {path, content} objects."
    if any(str(line).lstrip().startswith("⚠️") for result in results for line in str(result).splitlines()):
        return "⚠️ WRITE_FILE_BATCH_PARTIAL_FAILURE: one or more writes failed.\n" + rendered
    return rendered


def _read_file(
    ctx: ToolContext,
    path: str,
    root: str = "active_workspace",
    max_lines: int = 2000,
    start_line: int = 1,
    bucket: str = "",
    skill_name: str = "",
) -> str:
    normalized, block = _access_or_block(ctx, root, "read")
    if block:
        return block
    if normalized == "active_workspace":
        return _repo_read(
            ctx,
            path,
            max_lines=max_lines,
            start_line=start_line,
            display_path=_root_display_path(normalized, path),
        )
    if normalized == "runtime_data":
        return _data_read(
            ctx,
            path,
            max_lines=max_lines,
            start_line=start_line,
            display_path=_root_display_path(normalized, path),
        )
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    if normalized == "skill_payload" and not bucket and not skill_name and task_constraint and task_constraint.mode == "skill_repair":
        return _data_read(ctx, path, max_lines=max_lines, start_line=start_line, display_path=_root_display_path(normalized, path))
    try:
        base = resource_root_path(ctx, normalized, bucket=bucket, skill_name=skill_name)
        target = resolve_resource_path(ctx, root=normalized, path=path, bucket=bucket, skill_name=skill_name)
        block_msg = _local_readonly_resource_block(ctx, normalized, target, base, action="READ_FILE")
        if block_msg:
            return block_msg
        content = read_text(target)
        return _render_line_slice(_root_display_path(normalized, path), content, max_lines=max_lines, start_line=start_line)
    except FileNotFoundError:
        return f"⚠️ NOT_FOUND: {_root_display_path(normalized, path)}"
    except Exception as exc:
        return f"⚠️ READ_FILE_ERROR: {type(exc).__name__}: {exc}"


def _list_files(
    ctx: ToolContext,
    dir: str = ".",
    root: str = "active_workspace",
    max_entries: int = 500,
    bucket: str = "",
    skill_name: str = "",
) -> str:
    normalized, block = _access_or_block(ctx, root, "list")
    if block:
        return block
    if normalized == "active_workspace":
        return _repo_list(ctx, dir=dir, max_entries=max_entries)
    if normalized == "runtime_data":
        return _data_list(ctx, dir=dir, max_entries=max_entries)
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    if normalized == "skill_payload" and not bucket and not skill_name and task_constraint and task_constraint.mode == "skill_repair":
        return _data_list(ctx, dir=dir, max_entries=max_entries)
    try:
        base = resource_root_path(ctx, normalized, bucket=bucket, skill_name=skill_name)
        if normalized == "user_files":
            target = resolve_user_file_path(ctx, dir, allow_protected_descendants=True)
            items = _list_user_files_dir(ctx, base, target, max_entries)
            return json.dumps(items, ensure_ascii=False, indent=2)
        items = _list_dir(base, dir, max_entries)
        if _is_local_readonly_subagent(ctx):
            if normalized == "system_repo":
                items = _filter_subagent_secret_repo_listing(items, base)
            elif normalized in {"task_drive", "skill_payload", "artifact_store", "user_files"}:
                items = _filter_subagent_secret_listing(items, base)
        return json.dumps(items, ensure_ascii=False, indent=2)
    except Exception as exc:
        return json.dumps([f"⚠️ LIST_FILES_ERROR: {type(exc).__name__}: {exc}"], ensure_ascii=False, indent=2)


def _write_file(
    ctx: ToolContext,
    path: str = "",
    content: str = "",
    files: List[Dict[str, str]] | None = None,
    root: str = "active_workspace",
    mode: str = "overwrite",
    force: bool = False,
    bucket: str = "",
    skill_name: str = "",
) -> str:
    normalized, block = _access_or_block(ctx, root, "write")
    if block:
        return block
    if normalized == "system_repo":
        try:
            from ouroboros.tool_access import resource_root_path

            active_root = resource_root_path(ctx, "active_workspace")
            system_root = resource_root_path(ctx, "system_repo")
            if active_root.resolve(strict=False) != system_root.resolve(strict=False):
                return "⚠️ WRITE_FILE_BLOCKED: root=system_repo writes require the active workspace to be the system repo."
        except Exception as exc:
            return f"⚠️ WRITE_FILE_BLOCKED: could not validate system_repo root: {type(exc).__name__}: {exc}"
    if normalized in {"active_workspace", "system_repo"}:
        from ouroboros.tools.git import _repo_write

        return _repo_write(ctx, path=path, content=content, files=files or [], force=force, display_root=normalized)
    if normalized == "runtime_data":
        if files:
            results = []
            for item in files:
                if not isinstance(item, dict):
                    continue
                results.append(_data_write(
                    ctx,
                    str(item.get("path") or ""),
                    str(item.get("content") or ""),
                    mode=mode,
                    display_root=normalized,
                ))
            return _join_write_results(results)
        return _data_write(ctx, path=path, content=content, mode=mode, display_root=normalized)
    if normalized == "skill_payload":
        if files:
            results = []
            for item in files:
                rel = str(item.get("path") or "") if isinstance(item, dict) else ""
                body = str(item.get("content") or "") if isinstance(item, dict) else ""
                results.append(_data_write(
                    ctx,
                    rel,
                    body,
                    mode=mode,
                    bucket=bucket,
                    skill_name=skill_name,
                    display_root=normalized,
                ))
            return _join_write_results(results)
        return _data_write(ctx, path=path, content=content, mode=mode, bucket=bucket, skill_name=skill_name, display_root=normalized)
    try:
        if files:
            results = []
            for item in files:
                if not isinstance(item, dict):
                    continue
                rel_path = str(item.get("path") or "")
                target = resolve_resource_path(ctx, root=normalized, path=rel_path, bucket=bucket, skill_name=skill_name)
                if normalized == "artifact_store":
                    block_reason = artifact_store_path_block_reason(target)
                    if block_reason:
                        results.append(f"⚠️ WRITE_FILE_BLOCKED: artifact_store path blocked: {block_reason}")
                        continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(str(item.get("content") or ""), encoding="utf-8")
                result = f"OK: wrote {_root_display_path(normalized, rel_path)} ({len(str(item.get('content') or ''))} chars)"
                if normalized == "user_files":
                    record = copy_file_to_task_artifacts(ctx, target, kind="user_file")
                    if record:
                        result += f"\nARTIFACT_OUTPUTS: registered user file -> artifact_store:{record.get('name')}"
                results.append(result)
            return _join_write_results(results)
        target = resolve_resource_path(ctx, root=normalized, path=path, bucket=bucket, skill_name=skill_name)
        if normalized == "artifact_store":
            block_reason = artifact_store_path_block_reason(target)
            if block_reason:
                return f"⚠️ WRITE_FILE_BLOCKED: artifact_store path blocked: {block_reason}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with target.open("a", encoding="utf-8") as fh:
                fh.write(content)
        else:
            target.write_text(content, encoding="utf-8")
        result = f"OK: wrote {_root_display_path(normalized, path)} ({len(content)} chars)"
        if normalized == "user_files":
            record = copy_file_to_task_artifacts(ctx, target, kind="user_file")
            if record:
                result += f"\nARTIFACT_OUTPUTS: registered user file -> artifact_store:{record.get('name')}"
        return result
    except Exception as exc:
        return f"⚠️ WRITE_FILE_ERROR: {type(exc).__name__}: {exc}"


def _edit_text(
    ctx: ToolContext,
    path: str,
    old_str: str,
    new_str: str,
    root: str = "active_workspace",
    bucket: str = "",
    skill_name: str = "",
) -> str:
    normalized, block = _access_or_block(ctx, root, "edit")
    if block:
        return block
    if normalized == "system_repo":
        try:
            from ouroboros.tool_access import resource_root_path

            active_root = resource_root_path(ctx, "active_workspace")
            system_root = resource_root_path(ctx, "system_repo")
            if active_root.resolve(strict=False) != system_root.resolve(strict=False):
                return "⚠️ EDIT_TEXT_BLOCKED: root=system_repo edits require the active workspace to be the system repo."
        except Exception as exc:
            return f"⚠️ EDIT_TEXT_BLOCKED: could not validate system_repo root: {type(exc).__name__}: {exc}"
    if normalized in {"active_workspace", "system_repo"}:
        from ouroboros.tools.git import _str_replace_editor

        result = _str_replace_editor(ctx, path=path, old_str=old_str, new_str=new_str, display_root=normalized)
        short_form = decide_payload_short_form(
            bucket=bucket,
            skill_name=skill_name,
            path_text=path,
            repo_dir=pathlib.Path(ctx.repo_dir),
            drive_root=pathlib.Path(ctx.drive_root),
        )
        if short_form.ignored_reason:
            result += f"\n⚠️ SKILL_SHORT_FORM_IGNORED: {short_form.ignored_reason}."
        return result
    if normalized == "skill_payload":
        from ouroboros.tools.git import _str_replace_editor

        return _str_replace_editor(
            ctx,
            path=path,
            old_str=old_str,
            new_str=new_str,
            bucket=bucket,
            skill_name=skill_name,
            display_root=normalized,
        )
    try:
        target = resolve_resource_path(ctx, root=normalized, path=path, bucket=bucket, skill_name=skill_name)
        if normalized == "artifact_store":
            block_reason = artifact_store_path_block_reason(target)
            if block_reason:
                return f"⚠️ EDIT_TEXT_BLOCKED: artifact_store path blocked: {block_reason}"
        text = target.read_text(encoding="utf-8")
        count = text.count(old_str)
        if count != 1:
            return f"⚠️ EDIT_TEXT_ERROR: old_str matched {count} times; expected exactly 1."
        target.write_text(text.replace(old_str, new_str, 1), encoding="utf-8")
        result = f"OK: edited {_root_display_path(normalized, path)}"
        if normalized == "user_files":
            record = copy_file_to_task_artifacts(ctx, target, kind="user_file")
            if record:
                result += f"\nARTIFACT_OUTPUTS: registered user file -> artifact_store:{record.get('name')}"
        return result
    except FileNotFoundError:
        return f"⚠️ EDIT_TEXT_ERROR: file not found: {_root_display_path(normalized, path)}"
    except Exception as exc:
        return f"⚠️ EDIT_TEXT_ERROR: {type(exc).__name__}: {exc}"

_MAX_PHOTO_FILE_BYTES = 10 * 1024 * 1024  # 10 MB


def _detect_image_mime(data: bytes) -> str:
    """Detect image MIME type from magic bytes."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    return "application/octet-stream"


def _send_photo(ctx: ToolContext, file_path: str = "", image_base64: str = "",
                caption: str = "") -> str:
    """Queue an owner-chat image from a file or legacy base64 payload."""
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send photo."

    actual_b64 = ""
    mime = "image/png"

    if file_path:
        fp = pathlib.Path(file_path).expanduser().resolve()
        if not fp.exists():
            return f"⚠️ File not found: {file_path}"
        if fp.stat().st_size > _MAX_PHOTO_FILE_BYTES:
            return f"⚠️ File too large ({fp.stat().st_size} bytes). Max: {_MAX_PHOTO_FILE_BYTES} bytes."
        try:
            raw = fp.read_bytes()
            mime = _detect_image_mime(raw)
            actual_b64 = __import__("base64").b64encode(raw).decode()
        except Exception as e:
            return f"⚠️ Failed to read image file: {e}"
    elif image_base64:
        if image_base64 == "__last_screenshot__":
            if not ctx.browser_state.last_screenshot_b64:
                return "⚠️ No screenshot stored. Take one first with browse_page(output='screenshot')."
            actual_b64 = ctx.browser_state.last_screenshot_b64
        else:
            actual_b64 = image_base64
    else:
        return "⚠️ Provide either file_path or image_base64."

    if not actual_b64 or len(actual_b64) < 100:
        return "⚠️ Image data is empty or too short."

    ctx.pending_events.append({
        "type": "send_photo",
        "chat_id": ctx.current_chat_id,
        "image_base64": actual_b64,
        "mime": mime,
        "caption": caption or "",
    })
    return "OK: photo queued for delivery to owner."


_MAX_VIDEO_FILE_BYTES = 50 * 1024 * 1024  # 50 MB


def _detect_video_mime(file_path: str, data: bytes) -> str:
    """Detect video MIME type from path extension or magic bytes."""
    if len(data) >= 8 and data[4:8] == b'ftyp':
        return "video/mp4"
    if data[:4] == b'\x1a\x45\xdf\xa3':
        return "video/webm"
    mime, _ = __import__("mimetypes").guess_type(file_path)
    if mime and str(mime).lower().startswith("video/"):
        return mime
    return "video/mp4"


def _send_video(ctx: ToolContext, file_path: str = "", caption: str = "") -> str:
    """Queue an owner-chat video from a file."""
    chat_id = getattr(ctx, "current_chat_id", None)
    if chat_id is None or chat_id == "":
        return "⚠️ No active chat — cannot send video."
    if not file_path:
        return "⚠️ Provide a file_path."

    fp = pathlib.Path(file_path).expanduser().resolve()
    if not fp.exists():
        return f"⚠️ File not found: {file_path}"
    if fp.stat().st_size > _MAX_VIDEO_FILE_BYTES:
        return f"⚠️ File too large ({fp.stat().st_size} bytes). Max: {_MAX_VIDEO_FILE_BYTES} bytes."

    try:
        raw = fp.read_bytes()
        mime = _detect_video_mime(str(fp), raw)
        actual_b64 = __import__("base64").b64encode(raw).decode()
    except Exception as e:
        return f"⚠️ Failed to read video file: {e}"

    ctx.pending_events.append({
        "type": "send_video",
        "chat_id": chat_id,
        "video_base64": actual_b64,
        "mime": mime,
        "caption": caption or "",
    })
    return "OK: video queued for delivery to owner."

_SEARCH_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
    ".eggs", ".ruff_cache", "python-standalone", "assets",
})

_SEARCH_SKIP_GLOBS = frozenset({
    "*.pyc", "*.pyo", "*.so", "*.dylib", "*.dll", "*.exe",
    "*.bin", "*.o", "*.a", "*.tar", "*.gz", "*.zip",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp",
    "*.woff", "*.woff2", "*.ttf", "*.eot",
    "*.min.js", "*.min.css", "*.map",
    "*.db", "*.sqlite", "*.sqlite3",
    "*.lock",
})

_MAX_SEARCH_RESULTS = 200
_MAX_FILE_SIZE_BYTES = 1024 * 1024  # 1 MB — skip huge files


def _is_search_skippable(path: pathlib.Path) -> bool:
    """Return True for files excluded from search_code."""
    name = path.name
    for glob_pat in _SEARCH_SKIP_GLOBS:
        if fnmatch.fnmatch(name, glob_pat):
            return True
    try:
        if path.stat().st_size > _MAX_FILE_SIZE_BYTES:
            return True
    except OSError:
        return True
    return False


def _code_search(ctx: ToolContext, query: str, path: str = ".",
                 regex: bool = False, max_results: int = 200,
                 include: str = "", root: str = "active_workspace",
                 bucket: str = "", skill_name: str = "") -> str:
    """Search repo text with optional regex, path, glob, and result cap."""
    if not query:
        return "⚠️ SEARCH_ERROR: query is required."
    normalized, block = _access_or_block(ctx, root, "search")
    if block:
        return block

    max_results = min(max(1, max_results), _MAX_SEARCH_RESULTS)
    try:
        root_path = resource_root_path(ctx, normalized, bucket=bucket, skill_name=skill_name)
    except Exception as exc:
        return f"⚠️ SEARCH_ERROR: {type(exc).__name__}: {exc}"
    display_search_path = _root_display_path(normalized, path)
    try:
        search_root = (
            resolve_user_file_path(ctx, path, allow_protected_descendants=True)
            if normalized == "user_files"
            else (root_path / safe_relpath(path)).resolve()
        )
    except Exception as exc:
        return f"⚠️ SEARCH_ERROR: {type(exc).__name__}: {exc}"
    if not search_root.exists():
        return f"⚠️ SEARCH_ERROR: path not found: {display_search_path}"
    subagent_readonly = _is_local_readonly_subagent(ctx)
    if subagent_readonly:
        block_msg = _local_readonly_resource_block(ctx, normalized, search_root, root_path, action="SEARCH")
        if block_msg:
            return block_msg

    try:
        if regex:
            pattern = re.compile(query)
        else:
            pattern = re.compile(re.escape(query))
    except re.error as e:
        return f"⚠️ SEARCH_ERROR: invalid regex: {e}"

    matches: List[str] = []
    files_searched = 0
    truncated = False

    for dirpath, dirnames, filenames in os.walk(str(search_root)):
        # Prune skipped dirs in-place.
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SEARCH_SKIP_DIRS]
        if normalized == "user_files":
            dirnames[:] = [
                d for d in dirnames
                if not user_files_path_block_reason(ctx, pathlib.Path(dirpath) / d)
            ]
        if subagent_readonly:
            dirnames[:] = [
                d for d in dirnames
                if not _local_readonly_resource_block(ctx, normalized, pathlib.Path(dirpath) / d, root_path, action="SEARCH")
            ]

        for fname in sorted(filenames):
            fp = pathlib.Path(dirpath) / fname

            if include and not fnmatch.fnmatch(fname, include):
                continue

            if subagent_readonly and _local_readonly_resource_block(ctx, normalized, fp, root_path, action="SEARCH"):
                continue
            if normalized == "user_files" and user_files_path_block_reason(ctx, fp):
                continue

            if _is_search_skippable(fp):
                continue

            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            files_searched += 1
            rel = fp.relative_to(root_path).as_posix()

            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append(f"{_root_display_path(normalized, rel)}:{lineno}: {line.rstrip()}")
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return f"No matches found for {'regex' if regex else 'literal'} `{query}` in {display_search_path} ({files_searched} files searched)."

    header = f"Found {len(matches)} match{'es' if len(matches) != 1 else ''} in {display_search_path} ({files_searched} files searched)"
    if truncated:
        header += f" — truncated at {max_results} results"
    return header + "\n\n" + "\n".join(matches)

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
})


def _extract_python_symbols(file_path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Extract Python class/function names with AST."""
    try:
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        classes = []
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
        return list(dict.fromkeys(classes)), list(dict.fromkeys(functions))
    except Exception:
        log.warning(f"Failed to extract Python symbols from {file_path}", exc_info=True)
        return [], []


def _codebase_digest(ctx: ToolContext) -> str:
    """Generate a compact file/symbol digest for the codebase."""
    from ouroboros.code_intelligence import build_code_inventory, render_codebase_digest

    inventory = build_code_inventory(
        active_repo_dir_for(ctx),
        drive_root=pathlib.Path(ctx.drive_root),
        persist=not _is_local_readonly_subagent(ctx),
    )
    if _is_local_readonly_subagent(ctx):
        repo_root = active_repo_dir_for(ctx)
        inventory.files = [
            file for file in inventory.files
            if not _is_subagent_secret_repo_target(repo_root / file.path, repo_root)
        ]
    return render_codebase_digest(inventory)

def _forward_to_worker(ctx: ToolContext, task_id: str, message: str) -> str:
    """Forward a message to a running worker task's mailbox."""
    from ouroboros.owner_inject import write_owner_message
    from ouroboros.task_results import STATUS_RUNNING, validate_task_id
    from ouroboros.task_status import FINAL_STATUSES, load_effective_task_result

    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (forward_to_worker): {exc}"
    data = load_effective_task_result(pathlib.Path(ctx.drive_root), tid)
    status = str(data.get("status") or "").lower()
    if not data:
        return f"⚠️ TASK_NOT_FOUND: task {tid} is not registered."
    if status in FINAL_STATUSES:
        return f"⚠️ TASK_NOT_ACTIVE: task {tid} is already {status}."
    if status != STATUS_RUNNING:
        return f"⚠️ TASK_NOT_ACTIVE: task {tid} is {status or 'unknown'}, not running."
    current_task_id = str(getattr(ctx, "task_id", "") or "").strip()
    target_parent = str(data.get("parent_task_id") or "").strip()
    target_root = str(data.get("root_task_id") or "").strip()
    if not current_task_id:
        return "⚠️ TASK_FORBIDDEN: forward_to_worker requires an active task context."
    allowed = target_parent == current_task_id or target_root == current_task_id
    if not allowed:
        return f"⚠️ TASK_FORBIDDEN: task {tid} is not a child or descendant of the current task."
    child_drive = str(data.get("child_drive_root") or data.get("headless_child_drive_root") or data.get("drive_root") or "").strip()
    mailbox_drive = pathlib.Path(child_drive) if child_drive else pathlib.Path(ctx.drive_root)
    write_owner_message(mailbox_drive, message, task_id=tid, msg_id=uuid.uuid4().hex)
    return f"Message forwarded to task {tid}"

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("read_file", {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from a declared resource root. "
                "Default root=active_workspace (the user's workspace or the Ouroboros repo in self-modification tasks). "
                "Use max_lines (default 2000) and start_line (default 1) to read large files in chunks. "
                "The result header shows root:path and 'lines X\u2013Y of Z' so you know where and how much you read."
            ),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo", "runtime_data", "task_drive", "skill_payload", "artifact_store", "user_files"], "default": "active_workspace"},
                "max_lines": {"type": "integer", "default": 2000,
                              "description": "Maximum number of lines to return (default 2000)."},
                "start_line": {"type": "integer", "default": 1,
                               "description": "1-indexed line to start reading from (default 1 = beginning)."},
                "bucket": {"type": "string", "description": "Required only for root=skill_payload."},
                "skill_name": {"type": "string", "description": "Required only for root=skill_payload."},
            }, "required": ["path"]},
        }, _read_file),
        ToolEntry("list_files", {
            "name": "list_files",
            "description": "List files under a resource root directory.",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo", "runtime_data", "task_drive", "skill_payload", "artifact_store", "user_files"], "default": "active_workspace"},
                "max_entries": {"type": "integer", "default": 500},
                "bucket": {"type": "string", "description": "Required only for root=skill_payload."},
                "skill_name": {"type": "string", "description": "Required only for root=skill_payload."},
            }, "required": []},
        }, _list_files),
        ToolEntry("write_file", {
            "name": "write_file",
            "description": (
                "Write UTF-8 file(s) to a declared resource root. "
                "Default root=active_workspace. "
                "OK messages show root:path. "
                "Use mode='append' to write a large file in chunks across multiple calls "
                "(useful when the full content exceeds a single LLM output budget). "
                "For root=skill_payload, supply bucket and skill_name."
            ),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "files": {"type": "array", "items": {"type": "object", "properties": {
                    "path": {"type": "string"}, "content": {"type": "string"},
                }, "required": ["path", "content"]}},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo", "runtime_data", "task_drive", "skill_payload", "artifact_store", "user_files"], "default": "active_workspace"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                "force": {"type": "boolean", "default": False, "description": "Bypass shrink guard for intentional active_workspace full rewrites."},
                "bucket": {
                    "type": "string",
                    "enum": ["external", "clawhub", "ouroboroshub"],
                    "description": "Skill payload bucket. Required for root=skill_payload.",
                },
                "skill_name": {
                    "type": "string",
                    "description": "Skill slug. Required for root=skill_payload.",
                },
            }, "required": []},
        }, _write_file, is_code_tool=True),
        ToolEntry("edit_text", {
            "name": "edit_text",
            "description": (
                "Replace exactly one occurrence of old_str with new_str in a file. "
                "Default root=active_workspace. Result messages show root:path. "
                "For root=skill_payload, supply bucket and skill_name."
            ),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo", "runtime_data", "task_drive", "skill_payload", "artifact_store", "user_files"], "default": "active_workspace"},
                "bucket": {"type": "string", "enum": ["external", "clawhub", "ouroboroshub"]},
                "skill_name": {"type": "string"},
            }, "required": ["path", "old_str", "new_str"]},
        }, _edit_text, is_code_tool=True),
        ToolEntry("send_photo", {
            "name": "send_photo",
            "description": (
                "Send an image to the owner's chat. "
                "Preferred: use file_path to send a local file. "
                "Legacy: use image_base64 with raw base64 or __last_screenshot__. "
                "Use after browse_page(output='screenshot') or browser_action(action='screenshot')."
            ),
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "description": "Local file path to image (preferred)"},
                "image_base64": {"type": "string", "description": "Base64-encoded image data or __last_screenshot__"},
                "caption": {"type": "string", "description": "Optional caption for the photo"},
            }, "required": []},
        }, _send_photo),
        ToolEntry("send_video", {
            "name": "send_video",
            "description": "Send a video to the owner's chat (e.g. an anime animation). Requires a local file_path.",
            "parameters": {"type": "object", "properties": {
                "file_path": {"type": "string", "description": "Local file path to video (preferred)"},
                "caption": {"type": "string", "description": "Optional caption for the video"},
            }, "required": ["file_path"]},
        }, _send_video),
        ToolEntry("search_code", {
            "name": "search_code",
            "description": (
                "Search for a pattern in the repository code. "
                "Literal search by default; set regex=True for regular expressions. "
                "Scoped to path (default: entire active workspace). "
                "Skips binaries, caches, vendor dirs, and files >1MB. "
                "Returns up to max_results matches (default 200) with root:file:line context."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search pattern (literal or regex)"},
                "path": {"type": "string", "default": ".", "description": "Subdirectory to search (relative to repo root)"},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo", "runtime_data", "task_drive", "skill_payload", "artifact_store", "user_files"], "default": "active_workspace"},
                "bucket": {"type": "string", "description": "Required only for root=skill_payload."},
                "skill_name": {"type": "string", "description": "Required only for root=skill_payload."},
                "regex": {"type": "boolean", "default": False, "description": "Treat query as a regular expression"},
                "max_results": {"type": "integer", "default": 200, "description": "Maximum number of matches to return (max 200)"},
                "include": {"type": "string", "default": "", "description": "Filter by glob pattern (e.g. '*.py')"},
            }, "required": ["query"]},
        }, _code_search),
        ToolEntry("codebase_digest", {
            "name": "codebase_digest",
            "description": "Get a compact digest of the entire codebase: files, sizes, classes, functions. One call instead of many read_file calls.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _codebase_digest),
        ToolEntry("forward_to_worker", {
            "name": "forward_to_worker",
            "description": (
                "Forward a message to a running worker task's mailbox. "
                "Use when my human sends a message during your active conversation "
                "that is relevant to a specific running background task. "
                "The worker will see it as [Message from my human] on its next LLM round."
            ),
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string", "description": "ID of the running task to forward to"},
                "message": {"type": "string", "description": "Message text to forward"},
            }, "required": ["task_id", "message"]},
        }, _forward_to_worker),
    ]

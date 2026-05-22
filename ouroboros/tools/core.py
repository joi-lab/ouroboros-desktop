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

from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
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


_MEMORY_AT_DRIVE_MEMORY = frozenset({
    "identity.md", "scratchpad.md", "dialogue_summary.md",
    "dialogue_blocks.json", "registry.md", "deep_review.md",
    "WORLD.md",
})


def _repo_read(ctx: ToolContext, path: str, max_lines: int = 2000, start_line: int = 1) -> str:
    """Read a repo file; root-level memory names return a data_read hint."""
    try:
        content = read_text(ctx.repo_path(path))
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
                f"`data_read(path='memory/{base}')`."
            )
        raise
    return _render_line_slice(path, content, max_lines=max_lines, start_line=start_line)


def _repo_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(_list_dir(active_repo_dir_for(ctx), dir, max_entries), ensure_ascii=False, indent=2)


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


def _data_read(ctx: ToolContext, path: str, max_lines: int = 2000, start_line: int = 1) -> str:
    """Read a drive text file; duplicate drive_root prefixes are stripped."""
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    norm = _normalize_data_read_path(ctx, path)
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            target = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, norm)
        except ValueError as e:
            return f"⚠️ DATA_READ_BLOCKED: {e}"
    else:
        target = ctx.drive_path(norm)
    if (
        _is_skill_owner_state_target(target, pathlib.Path(ctx.drive_root))
        and target.name.lower() != "review.json"
    ):
        return "DATA_READ_BLOCKED: skill owner state is not readable through generic data tools."
    try:
        content = read_text(target)
        start_raw, max_raw = _coerce_line_window(start_line, max_lines)
        if _is_cognitive_data_path(norm) and start_raw == 1 and max_raw == 2000:
            return content
        return _render_line_slice(norm, content, max_lines=max_raw, start_line=start_raw)
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
            f"{explanation} Use data_list to confirm what currently exists."
        )


def _data_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    task_constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            root = resolve_payload_path(pathlib.Path(ctx.drive_root), task_constraint, dir)
        except ValueError as e:
            return json.dumps([f"⚠️ DATA_LIST_BLOCKED: {e}"], ensure_ascii=False, indent=2)
        return json.dumps(_list_dir(root, ".", max_entries), ensure_ascii=False, indent=2)
    return json.dumps(_list_dir(ctx.drive_root, dir, max_entries), ensure_ascii=False, indent=2)


def _data_write(
    ctx: ToolContext,
    path: str,
    content: str,
    mode: str = "overwrite",
    bucket: str = "",
    skill_name: str = "",
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
            "Extract the actual file body before calling data_write."
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
            "the skill payload under data/skills/ and use review_skill, the "
            "Skills UI toggle, or the desktop launcher grant flow."
        )
    # Block marketplace/launcher sidecars for every data_write path, not only heal mode.
    if is_skill_control_plane_path(lexical_target, data_root) or is_skill_control_plane_path(target_path, data_root):
        return (
            "⚠️ DATA_WRITE_BLOCKED: marketplace provenance and launcher "
            "seed markers (.clawhub.json, .ouroboroshub.json, "
            "SKILL.openclaw.md, .seed-origin) are owner/review controlled. "
            "Edit the payload's user-authored files instead and rerun review_skill."
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
    result = f"OK: wrote {mode} {path} ({len(content)} chars)"
    if short_form.ignored_reason:
        result += f"\n⚠️ SKILL_SHORT_FORM_IGNORED: {short_form.ignored_reason}."
    return result

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
    """Return True for files excluded from code_search."""
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
                 include: str = "") -> str:
    """Search repo text with optional regex, path, glob, and result cap."""
    if not query:
        return "⚠️ SEARCH_ERROR: query is required."

    max_results = min(max(1, max_results), _MAX_SEARCH_RESULTS)
    root = active_repo_dir_for(ctx)
    search_root = (root / safe_relpath(path)).resolve()
    if not search_root.exists():
        return f"⚠️ SEARCH_ERROR: path not found: {path}"

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

        for fname in sorted(filenames):
            fp = pathlib.Path(dirpath) / fname

            if include and not fnmatch.fnmatch(fname, include):
                continue

            if _is_search_skippable(fp):
                continue

            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue

            files_searched += 1
            rel = fp.relative_to(root).as_posix()

            for lineno, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append(f"{rel}:{lineno}: {line.rstrip()}")
                    if len(matches) >= max_results:
                        truncated = True
                        break
            if truncated:
                break
        if truncated:
            break

    if not matches:
        return f"No matches found for {'regex' if regex else 'literal'} `{query}` in {path} ({files_searched} files searched)."

    header = f"Found {len(matches)} match{'es' if len(matches) != 1 else ''} ({files_searched} files searched)"
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
    repo_dir = active_repo_dir_for(ctx)
    py_files: List[pathlib.Path] = []
    md_files: List[pathlib.Path] = []
    other_files: List[pathlib.Path] = []

    for dirpath, dirnames, filenames in os.walk(str(repo_dir)):
        # Skip excluded directories
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            p = pathlib.Path(dirpath) / fn
            if not p.is_file():
                continue
            if p.suffix == ".py":
                py_files.append(p)
            elif p.suffix == ".md":
                md_files.append(p)
            elif p.suffix in (".txt", ".cfg", ".toml", ".yml", ".yaml", ".json"):
                other_files.append(p)

    total_lines = 0
    total_functions = 0
    sections: List[str] = []

    for pf in py_files:
        try:
            lines = pf.read_text(encoding="utf-8").splitlines()
            line_count = len(lines)
            total_lines += line_count
            classes, functions = _extract_python_symbols(pf)
            total_functions += len(functions)
            rel = pf.relative_to(repo_dir).as_posix()
            parts = [f"\n== {rel} ({line_count} lines) =="]
            if classes:
                cl = ", ".join(classes[:10])
                if len(classes) > 10:
                    cl += f", ... ({len(classes)} total)"
                parts.append(f"  Classes: {cl}")
            if functions:
                fn = ", ".join(functions[:20])
                if len(functions) > 20:
                    fn += f", ... ({len(functions)} total)"
                parts.append(f"  Functions: {fn}")
            sections.append("\n".join(parts))
        except Exception:
            log.debug(f"Failed to process Python file {pf} in codebase_digest", exc_info=True)
            pass

    for mf in md_files:
        try:
            line_count = len(mf.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = mf.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(f"Failed to process markdown file {mf} in codebase_digest", exc_info=True)
            pass

    for of in other_files:
        try:
            line_count = len(of.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = of.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(f"Failed to process config file {of} in codebase_digest", exc_info=True)
            pass

    total_files = len(py_files) + len(md_files) + len(other_files)
    header = f"Codebase Digest ({total_files} files, {total_lines} lines, {total_functions} functions)"
    return header + "\n" + "\n".join(sections)

def _forward_to_worker(ctx: ToolContext, task_id: str, message: str) -> str:
    """Forward a message to a running worker task's mailbox."""
    from ouroboros.owner_inject import write_owner_message
    write_owner_message(ctx.drive_root, message, task_id=task_id, msg_id=uuid.uuid4().hex)
    return f"Message forwarded to task {task_id}"

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("repo_read", {
            "name": "repo_read",
            "description": (
                "Read a UTF-8 text file from the local repo (relative path). "
                "Use max_lines (default 2000) and start_line (default 1) to read large files in chunks. "
                "The result header shows 'lines X\u2013Y of Z' so you know whether you saw the full file."
            ),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "max_lines": {"type": "integer", "default": 2000,
                              "description": "Maximum number of lines to return (default 2000)."},
                "start_line": {"type": "integer", "default": 1,
                               "description": "1-indexed line to start reading from (default 1 = beginning)."},
            }, "required": ["path"]},
        }, _repo_read),
        ToolEntry("repo_list", {
            "name": "repo_list",
            "description": "List files under a repo directory (relative path).",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 500},
            }, "required": []},
        }, _repo_list),
        ToolEntry("data_read", {
            "name": "data_read",
            "description": (
                "Read a UTF-8 text file from the local data directory. "
                "Use max_lines (default 2000) and start_line (default 1) "
                "to read large data/skill files in chunks."
            ),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "max_lines": {"type": "integer", "default": 2000,
                              "description": "Maximum number of lines to return (default 2000)."},
                "start_line": {"type": "integer", "default": 1,
                               "description": "1-indexed line to start reading from (default 1)."},
            }, "required": ["path"]},
        }, _data_read),
        ToolEntry("data_list", {
            "name": "data_list",
            "description": "List files under a local data directory.",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 500},
            }, "required": []},
        }, _data_list),
        ToolEntry("data_write", {
            "name": "data_write",
            "description": (
                "Write a UTF-8 text file to the local data directory. "
                "Use mode='append' to write a large file in chunks across multiple calls "
                "(useful when the full content exceeds a single LLM output budget). "
                "Optional bucket+skill_name args let tasks write a short relative path under "
                "an existing data/skills/<bucket>/<skill_name>/ payload. Explicit data/repo "
                "paths keep their own address space and ignore stale short-form args."
            ),
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                "bucket": {
                    "type": "string",
                    "enum": ["external", "clawhub", "ouroboroshub"],
                    "description": "Skill payload bucket for short relative payload paths only. Pair with skill_name. Do not supply for explicit repo/data paths.",
                },
                "skill_name": {
                    "type": "string",
                    "description": "Skill slug for short relative payload paths only. Requires bucket.",
                },
            }, "required": ["path", "content"]},
        }, _data_write),
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
        ToolEntry("code_search", {
            "name": "code_search",
            "description": (
                "Search for a pattern in the repository code. "
                "Literal search by default; set regex=True for regular expressions. "
                "Scoped to path (default: entire repo). "
                "Skips binaries, caches, vendor dirs, and files >1MB. "
                "Returns up to max_results matches (default 200) with file:line: context."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search pattern (literal or regex)"},
                "path": {"type": "string", "default": ".", "description": "Subdirectory to search (relative to repo root)"},
                "regex": {"type": "boolean", "default": False, "description": "Treat query as a regular expression"},
                "max_results": {"type": "integer", "default": 200, "description": "Maximum number of matches to return (max 200)"},
                "include": {"type": "string", "default": "", "description": "Filter by glob pattern (e.g. '*.py')"},
            }, "required": ["query"]},
        }, _code_search),
        ToolEntry("codebase_digest", {
            "name": "codebase_digest",
            "description": "Get a compact digest of the entire codebase: files, sizes, classes, functions. One call instead of many repo_read calls.",
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

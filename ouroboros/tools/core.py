"""File tools: repo_read, repo_list, data_read, data_list, data_write, code_search, codebase_digest, summarize_dialogue."""

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

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import read_text, safe_relpath, utc_now_iso

log = logging.getLogger(__name__)


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


def _repo_read(ctx: ToolContext, path: str, max_lines: int = 2000, start_line: int = 1) -> str:
    """Read a file from the repo, optionally slicing to a line range."""
    content = read_text(ctx.repo_path(path))
    lines = content.splitlines(keepends=True)
    total = len(lines)
    start = max(1, min(start_line, total + 1))
    end = min(start + max_lines - 1, total)
    slice_lines = lines[start - 1:end]
    result = "".join(slice_lines)
    header = f"# {path} — lines {start}\u2013{end} of {total}\n"
    return header + result


def _repo_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(_list_dir(ctx.repo_dir, dir, max_entries), ensure_ascii=False, indent=2)


def _data_read(ctx: ToolContext, path: str) -> str:
    # Pattern Register #2 (Cold-start missing-artifact reads) — architectural fix.
    # Memory artifacts like memory/knowledge/*.md, memory/identity.md, and
    # memory/scratchpad.md are created lazily on first write. A cold-start
    # read raised a bare FileNotFoundError, which bubbled as an opaque tool
    # error and cost the agent several rounds rediscovering the empty state.
    # Return a clear sentinel instead so the agent can branch on it.
    # Non-FileNotFound I/O errors still raise naturally.
    target = ctx.drive_path(path)
    if not target.exists():
        return (
            f"⚠️ DATA_NOT_YET_CREATED: {path}\n\n"
            "This file does not exist yet. Memory artifacts under "
            "memory/knowledge/, memory/identity.md, memory/scratchpad.md, and "
            "similar paths are created lazily on first write. Treat this as "
            "an empty/absent state and proceed with initialization if that is "
            "the task. Use data_list to confirm what currently exists."
        )
    return read_text(target)


def _data_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(_list_dir(ctx.drive_root, dir, max_entries), ensure_ascii=False, indent=2)


def _data_write(ctx: ToolContext, path: str, content: str, mode: str = "overwrite") -> str:
    p = ctx.drive_path(path)
    # v5.1.2 elevation ratchet defense-in-depth: settings.json is owner-only.
    # The chokepoint in ``ouroboros.config.save_settings`` already refuses
    # disk-level elevation; blocking ``data_write`` here turns the whole
    # class of attempts into a clear tool-level error.
    #
    # The match is inode-aware (``Path.samefile``) so it handles symlinks,
    # hardlinks, and case-insensitive filesystems (macOS APFS / Windows NTFS
    # — ``os.path.normcase`` is a no-op on darwin, so a string-equality
    # compare against the resolved path would let ``data_write("Settings.json",
    # ...)`` bypass on darwin even though APFS routes both names to the same
    # inode). For not-yet-existing paths ``samefile`` is unavailable; we
    # fall back to a parent-resolve + case-insensitive name compare which
    # covers the same-directory case-variant attack.
    from ouroboros import config as _cfg
    target_path = pathlib.Path(p)
    settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
    grants_path = False
    try:
        rel_to_data = target_path.resolve(strict=False).relative_to(pathlib.Path(_cfg.DATA_DIR).resolve(strict=False))
        parts = rel_to_data.parts
        grants_path = (
            len(parts) == 4
            and parts[0].lower() == "state"
            and parts[1].lower() == "skills"
            and parts[3].lower() == "grants.json"
        )
    except (OSError, ValueError):
        grants_path = False
    if not grants_path:
        grants_root = pathlib.Path(_cfg.DATA_DIR) / "state" / "skills"
        if target_path.exists() and grants_root.is_dir():
            for grant_file in grants_root.glob("*/grants.json"):
                try:
                    if grant_file.exists() and target_path.samefile(grant_file):
                        grants_path = True
                        break
                except OSError:
                    continue
    if grants_path:
        return (
            "⚠️ DATA_WRITE_BLOCKED: skill grants are owner-only state. "
            "Use the desktop launcher confirmation flow from the Skills UI."
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
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        p.write_text(content, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    return f"OK: wrote {mode} {path} ({len(content)} chars)"


# ---------------------------------------------------------------------------
# Send photo to owner
# ---------------------------------------------------------------------------

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
    """Send an image to the owner's chat.

    Preferred: file_path — reads a local image file.
    Legacy:    image_base64 — accepts raw base64 string or __last_screenshot__.
    """
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


# ---------------------------------------------------------------------------
# Code search
# ---------------------------------------------------------------------------

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
    """Return True if the file should be skipped during code search."""
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
    """Search for a pattern in the repository.

    Literal search by default.  Set regex=True for regular expressions.
    ``path`` scopes the search to a subdirectory (relative to repo root).
    ``include`` filters by glob pattern (e.g. "*.py").
    ``max_results`` caps the number of returned matches (default/max 200).
    """
    if not query:
        return "⚠️ SEARCH_ERROR: query is required."

    max_results = min(max(1, max_results), _MAX_SEARCH_RESULTS)
    root = ctx.repo_dir
    search_root = (root / safe_relpath(path)).resolve()
    if not search_root.exists():
        return f"⚠️ SEARCH_ERROR: path not found: {path}"

    # Compile the pattern
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
        # Prune skipped directories in-place
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SEARCH_SKIP_DIRS]

        for fname in sorted(filenames):
            fp = pathlib.Path(dirpath) / fname

            # Apply include filter
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


# ---------------------------------------------------------------------------
# Codebase digest
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
})


def _extract_python_symbols(file_path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Extract class and function names from a Python file using AST."""
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
    """Generate a compact digest of the codebase: files, sizes, classes, functions."""
    repo_dir = ctx.repo_dir
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

    # Python files
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

    # Markdown files
    for mf in md_files:
        try:
            line_count = len(mf.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = mf.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            log.debug(f"Failed to process markdown file {mf} in codebase_digest", exc_info=True)
            pass

    # Other config files (just names + sizes)
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


# ---------------------------------------------------------------------------
# Summarize dialogue
# ---------------------------------------------------------------------------

def _summarize_dialogue(ctx: ToolContext, last_n: int = 200) -> str:
    """Summarize dialogue history into key moments, decisions, and user preferences."""
    from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL

    # Read last_n messages from chat.jsonl
    chat_path = ctx.drive_root / "logs" / "chat.jsonl"
    if not chat_path.exists():
        return "⚠️ chat.jsonl not found"

    try:
        entries = []
        with chat_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        log.debug("Failed to parse chat.jsonl line in summarize_dialogue", exc_info=True)
                        continue

        # Take last N entries
        entries = entries[-last_n:] if len(entries) > last_n else entries

        if not entries:
            return "⚠️ No chat entries found"

        # Format entries as text
        dialogue_text = []
        for entry in entries:
            ts = entry.get("ts", "")
            direction = entry.get("direction", "")
            role = "User" if direction == "in" else "Ouroboros"
            text = entry.get("text", "")
            dialogue_text.append(f"[{ts}] {role}: {text}")

        formatted_dialogue = "\n".join(dialogue_text)

        # Build summarization prompt
        prompt = f"""Summarize the following dialogue history between the user and Ouroboros.

Extract:
1. Key decisions made (technical, architectural, strategic)
2. User preferences and communication style
3. Important technical choices and their rationale
4. Recurring themes or patterns

For each key moment, include the timestamp.

Format as markdown with clear sections.

Dialogue history ({len(entries)} messages):

{formatted_dialogue}

Now write a comprehensive summary:"""

        # Call LLM
        llm = LLMClient()
        model = os.environ.get("OUROBOROS_MODEL_LIGHT", "") or DEFAULT_LIGHT_MODEL

        messages = [
            {"role": "user", "content": prompt}
        ]

        _use_local_light = os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1")
        response, usage = llm.chat(
            messages=messages,
            model=model,
            max_tokens=4096,
            use_local=_use_local_light,
        )

        # Track cost in budget system
        if usage:
            usage_event = {
                "type": "llm_usage",
                "ts": utc_now_iso(),
                "task_id": ctx.task_id if ctx.task_id else "",
                "usage": {
                    "prompt_tokens": usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "cost": usage.get("cost", 0),
                },
                "category": "summarize",
            }
            if ctx.event_queue is not None:
                try:
                    ctx.event_queue.put_nowait(usage_event)
                except Exception:
                    if hasattr(ctx, "pending_events"):
                        ctx.pending_events.append(usage_event)
            elif hasattr(ctx, "pending_events"):
                ctx.pending_events.append(usage_event)

        summary = response.get("content", "")
        if not summary:
            return "⚠️ LLM returned empty summary"

        # Write to memory/dialogue_summary.md
        summary_path = ctx.drive_root / "memory" / "dialogue_summary.md"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(summary, encoding="utf-8")

        cost = float(usage.get("cost", 0))
        return f"OK: Summarized {len(entries)} messages. Written to memory/dialogue_summary.md. Cost: ${cost:.4f}\n\n{summary[:500]}..."

    except Exception as e:
        log.warning("Failed to summarize dialogue", exc_info=True)
        return f"⚠️ Error: {repr(e)}"


# ---------------------------------------------------------------------------
# forward_to_worker — LLM-initiated message routing to worker tasks
# ---------------------------------------------------------------------------

def _forward_to_worker(ctx: ToolContext, task_id: str, message: str) -> str:
    """Forward a message to a running worker task's mailbox."""
    from ouroboros.owner_inject import write_owner_message
    write_owner_message(ctx.drive_root, message, task_id=task_id, msg_id=uuid.uuid4().hex)
    return f"Message forwarded to task {task_id}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

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
            "description": "Read a UTF-8 text file from the local data directory.",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
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
            "description": "Write a UTF-8 text file to the local data directory.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
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
        ToolEntry("summarize_dialogue", {
            "name": "summarize_dialogue",
            "description": "Summarize dialogue history into key moments, decisions, and user preferences. Writes to memory/dialogue_summary.md.",
            "parameters": {"type": "object", "properties": {
                "last_n": {"type": "integer", "description": "Number of recent messages to summarize (default 200)"},
            }, "required": []},
        }, _summarize_dialogue),
        ToolEntry("forward_to_worker", {
            "name": "forward_to_worker",
            "description": (
                "Forward a message to a running worker task's mailbox. "
                "Use when the owner sends a message during your active conversation "
                "that is relevant to a specific running background task. "
                "The worker will see it as [Owner message during task] on its next LLM round."
            ),
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string", "description": "ID of the running task to forward to"},
                "message": {"type": "string", "description": "Message text to forward"},
            }, "required": ["task_id", "message"]},
        }, _forward_to_worker),
    ]

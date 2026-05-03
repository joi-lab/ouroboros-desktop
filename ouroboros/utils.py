"""
Ouroboros — Shared utilities.

Single source for helper functions used across all modules.
Does not import anything from ouroboros.* (zero dependency level).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import os
import pathlib
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Realtime log sink (set by app.py to stream events to the UI)
# ---------------------------------------------------------------------------
_log_sink: Optional[Callable[[Dict[str, Any]], None]] = None


def set_log_sink(fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    global _log_sink
    _log_sink = fn


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    """Append a JSON object as a line to a JSONL file (concurrent-safe)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    data = (line + "\n").encode("utf-8")

    lock_timeout_sec = 2.0
    lock_stale_sec = 10.0
    lock_sleep_sec = 0.01
    write_retries = 3
    retry_sleep_base_sec = 0.01

    path_hash = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    lock_path = path.parent / f".append_jsonl_{path_hash}.lock"
    lock_fd = None
    lock_acquired = False
    _written = False

    try:
        start = time.time()
        while time.time() - start < lock_timeout_sec:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                lock_acquired = True
                break
            except FileExistsError:
                try:
                    stat = lock_path.stat()
                    if time.time() - stat.st_mtime > lock_stale_sec:
                        lock_path.unlink()
                        continue
                except Exception:
                    log.debug("Failed to read lock stat during lock acquisition retry", exc_info=True)
                    pass
                time.sleep(lock_sleep_sec)
            except Exception:
                log.debug("Failed to acquire file lock for jsonl append", exc_info=True)
                break

        for attempt in range(write_retries):
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                _written = True
                return
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))

        for attempt in range(write_retries):
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                _written = True
                return
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))
    except Exception:
        log.warning("append_jsonl: all write attempts failed for %s", path, exc_info=True)
    finally:
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                log.debug("Failed to close lock fd after jsonl append", exc_info=True)
                pass
        if lock_acquired:
            try:
                lock_path.unlink()
            except Exception:
                log.debug("Failed to unlink lock file after jsonl append", exc_info=True)
                pass
        if _written and _log_sink is not None:
            try:
                _log_sink(obj)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def safe_relpath(p: str) -> str:
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

def truncate_for_log(s: str, max_chars: int = 4000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2:]


def clip_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    half = max(200, max_chars // 2)
    return text[:half] + "\n...(truncated)...\n" + text[-half:]


def short(s: Any, n: int = 120) -> str:
    t = str(s or "")
    return t[:n] + "..." if len(t) > n else t


def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars/4 heuristic)."""
    return max(1, (len(str(text or "")) + 3) // 4)


def is_tool_success(result: str) -> bool:
    """Check whether a tool result indicates success (not an error).

    Shared by presence loop, consciousness, and task loop for outgoing
    reply capture.  Checks error prefixes and JSON {"ok": false} patterns.
    """
    _err_prefixes = ("\u26a0\ufe0f", "Error:", "[TIMEOUT", "Failed")
    if result.startswith(_err_prefixes):
        return False
    if result.startswith("{"):
        try:
            data = json.loads(result)
            if isinstance(data, dict) and data.get("ok") is False:
                return False
        except (json.JSONDecodeError, ValueError):
            pass
    return True


# ---------------------------------------------------------------------------
# Subprocess
# ---------------------------------------------------------------------------

def run_cmd(cmd: List[str], cwd: Optional[pathlib.Path] = None) -> str:
    # Force C locale for git/shell so output stays English-parseable and ambiguous
    # refs/paths produce predictable error messages. Without this, e.g. a German
    # system locale makes ``git checkout <branch>`` fail with German error text
    # when a same-named path exists, and the ambiguity-resolution heuristic flips.
    _env = os.environ.copy()
    _env["LC_ALL"] = "C"
    _env["LANG"] = "C"
    _env["LANGUAGE"] = "C"
    res = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, env=_env,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        )
    return res.stdout.strip()


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def get_git_info(repo_dir: pathlib.Path) -> tuple[str, str]:
    """Best-effort retrieval of (git_branch, git_sha)."""
    branch = ""
    sha = ""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            branch = r.stdout.strip()
    except Exception:
        log.debug("Failed to get git branch", exc_info=True)
        pass
    try:
        r = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=2,
        )
        if r.returncode == 0:
            sha = r.stdout.strip()
    except Exception:
        log.debug("Failed to get git SHA", exc_info=True)
        pass
    return branch, sha


# ---------------------------------------------------------------------------
# Sanitization helpers (for logging)
# ---------------------------------------------------------------------------

def sanitize_task_for_event(
    task: Dict[str, Any], drive_logs: pathlib.Path, threshold: int = 4000,
) -> Dict[str, Any]:
    """Sanitize task dict for event logging: truncate large text, strip base64 images, persist full text."""
    try:
        sanitized = task.copy()

        # Strip all keys ending with _base64 (images, etc.)
        keys_to_strip = [k for k in sanitized.keys() if k.endswith("_base64")]
        for key in keys_to_strip:
            value = sanitized.pop(key)
            # Record that it was present and its size
            sanitized[f"{key}_present"] = True
            if isinstance(value, str):
                sanitized[f"{key}_len"] = len(value)

        text = task.get("text")
        if not isinstance(text, str):
            return sanitized

        text_len = len(text)
        text_hash = sha256_text(text)
        sanitized["text_len"] = text_len
        sanitized["text_sha256"] = text_hash

        if text_len > threshold:
            sanitized["text"] = truncate_for_log(text, threshold)
            sanitized["text_truncated"] = True
            try:
                task_id = task.get("id")
                filename = f"task_{task_id}.txt" if task_id else f"task_{text_hash[:12]}.txt"
                full_path = drive_logs / "tasks" / filename
                write_text(full_path, text)
                sanitized["text_full_path"] = f"tasks/{filename}"
            except Exception:
                log.debug("Failed to persist full task text to Drive during sanitization", exc_info=True)
                pass
        else:
            sanitized["text_truncated"] = False

        return sanitized
    except Exception:
        return task


_SECRET_KEYS = frozenset([
    "token", "api_key", "apikey", "authorization", "secret", "password", "passwd", "passphrase",
])

# Patterns that indicate leaked secrets in tool output
import re as _re
_SECRET_PATTERNS = _re.compile(
    r'ghp_[A-Za-z0-9]{30,}'       # GitHub personal access token
    r'|sk-ant-[A-Za-z0-9\-]{30,}' # Anthropic API key
    r'|sk-or-[A-Za-z0-9\-]{30,}'  # OpenRouter API key
    r'|gsk_[A-Za-z0-9]{30,}'      # Groq API key
    r'|sk-[A-Za-z0-9]{40,}'       # OpenAI API key
    r'|\b[0-9]{8,}:[A-Za-z0-9_\-]{30,}\b'  # Telegram bot token (digits:alphanum)
)


def sanitize_tool_result_for_log(result: str) -> str:
    """Redact potential secrets from tool result before logging."""
    if not isinstance(result, str) or len(result) < 20:
        return result
    return _SECRET_PATTERNS.sub("***REDACTED***", result)


def sanitize_tool_args_for_log(
    fn_name: str, args: Dict[str, Any], threshold: int = 3000,
) -> Dict[str, Any]:
    """Sanitize tool arguments for logging: redact secrets, truncate large fields."""

    def _sanitize_value(key: str, value: Any, depth: int) -> Any:
        if depth > 3:
            return {"_depth_limit": True}
        if key.lower() in _SECRET_KEYS:
            return "*** REDACTED ***"
        if isinstance(value, str) and len(value) > threshold:
            return {
                key: truncate_for_log(value, threshold),
                f"{key}_len": len(value),
                f"{key}_sha256": sha256_text(value),
                f"{key}_truncated": True,
            }
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return {k: _sanitize_value(k, v, depth + 1) for k, v in value.items()}
        if isinstance(value, list):
            sanitized = [_sanitize_value(key, item, depth + 1) for item in value[:50]]
            if len(value) > 50:
                sanitized.append({"_truncated": f"... {len(value) - 50} more items"})
            return sanitized
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except (TypeError, ValueError):
            log.debug("Failed to JSON serialize value in sanitize_tool_args", exc_info=True)
            return {"_repr": repr(value)}

    try:
        return {k: _sanitize_value(k, v, 0) for k, v in args.items()}
    except Exception:
        log.debug("Failed to sanitize tool arguments for logging", exc_info=True)
        try:
            return json.loads(json.dumps(args, ensure_ascii=False, default=str))
        except Exception:
            log.debug("Tool argument sanitization failed completely", exc_info=True)
            return {"_error": "sanitization_failed"}


async def collect_evolution_metrics(repo_dir: str, data_dir: str | None = None) -> list[dict]:
    """Collect evolution metrics (LOC, prompt sizes, memory) for each git tag."""
    import asyncio
    import subprocess as sp

    # --- Parse journal files from data_dir for historical interpolation ---
    def _parse_journal(filepath: str, size_key: str) -> list[tuple[_dt.datetime, float]]:
        """Parse a JSONL journal file into sorted (datetime, size_kb) tuples."""
        entries: list[tuple[_dt.datetime, float]] = []
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        ts = _dt.datetime.fromisoformat(obj["ts"])
                        size_chars = obj.get(size_key, 0)
                        entries.append((ts, size_chars / 1024))
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except FileNotFoundError:
            pass
        entries.sort(key=lambda x: x[0])
        return entries

    identity_journal: list[tuple[_dt.datetime, float]] = []
    scratchpad_journal: list[tuple[_dt.datetime, float]] = []
    if data_dir:
        mem_path = os.path.join(data_dir, "memory")
        identity_journal = _parse_journal(
            os.path.join(mem_path, "identity_journal.jsonl"), "new_len"
        )
        scratchpad_journal = _parse_journal(
            os.path.join(mem_path, "scratchpad_journal.jsonl"), "content_len"
        )

    def _interpolate_from_journal(
        journal_entries: list[tuple[_dt.datetime, float]], tag_date: str,
    ) -> float:
        """Find the latest journal entry whose timestamp is <= tag_date."""
        if not journal_entries or not tag_date:
            return 0
        try:
            dt = _dt.datetime.fromisoformat(tag_date)
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        best = 0.0
        for entry_dt, size_kb in journal_entries:
            entry_dt_aware = entry_dt if entry_dt.tzinfo else entry_dt.replace(tzinfo=_dt.timezone.utc)
            if entry_dt_aware <= dt:
                best = size_kb
            else:
                break
        return round(best, 2)

    result = sp.run(
        ["git", "tag", "-l", "--sort=creatordate",
         "--format=%(refname:short)\t%(creatordate:iso-strict)"],
        cwd=repo_dir, capture_output=True, text=True
    )

    tags = []
    for line in result.stdout.strip().split(chr(10)):
        if not line.strip():
            continue
        parts = line.split(chr(9))
        tag = parts[0]
        date = parts[1] if len(parts) > 1 else ""
        tags.append((tag, date))

    cache_path: pathlib.Path | None = None
    cached_by_tag: dict[str, dict[str, Any]] = {}
    if data_dir:
        cache_path = pathlib.Path(data_dir) / "state" / "evolution_metrics_cache.json"
        try:
            cache_obj = json.loads(cache_path.read_text(encoding="utf-8"))
            if isinstance(cache_obj, dict) and cache_obj.get("schema") == 1 and isinstance(cache_obj.get("points"), dict):
                cached_by_tag = {
                    str(tag): point
                    for tag, point in cache_obj["points"].items()
                    if isinstance(point, dict)
                }
        except (OSError, json.JSONDecodeError):
            cached_by_tag = {}

    def _metrics_for_tag(tag: str, date: str) -> dict | None:
        ls_result = sp.run(
            ["git", "ls-tree", "-r", "--name-only", tag],
            cwd=repo_dir, capture_output=True, text=True
        )
        if ls_result.returncode != 0:
            return None

        files = ls_result.stdout.strip().split(chr(10))

        # Count Python LOC (all .py files)
        python_lines = 0
        for f in files:
            if f.endswith(".py"):
                show = sp.run(
                    ["git", "show", f"{tag}:{f}"],
                    cwd=repo_dir, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                )
                if show.returncode == 0 and show.stdout:
                    python_lines += len(show.stdout.splitlines())

        def get_file_size_kb(filepath: str) -> float:
            show = sp.run(
                ["git", "show", f"{tag}:{filepath}"],
                cwd=repo_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace",
            )
            if show.returncode == 0 and show.stdout:
                return round(len(show.stdout.encode("utf-8")) / 1024, 2)
            return 0

        bible_kb = get_file_size_kb("BIBLE.md")
        system_kb = get_file_size_kb("prompts/SYSTEM.md")

        # Identity and scratchpad from journal interpolation
        identity_kb = _interpolate_from_journal(identity_journal, date)
        scratchpad_kb = _interpolate_from_journal(scratchpad_journal, date)
        memory_kb = round(identity_kb + scratchpad_kb, 2)

        return {
            "tag": tag,
            "date": date,
            "code_lines": python_lines,
            "bible_kb": bible_kb,
            "system_kb": system_kb,
            "identity_kb": identity_kb,
            "scratchpad_kb": scratchpad_kb,
            "memory_kb": memory_kb,
        }

    cached_points: list[dict[str, Any]] = []
    missing_tags: list[tuple[str, str]] = []
    for tag, date in tags:
        cached = cached_by_tag.get(tag)
        if cached and cached.get("date") == date:
            cached_points.append(dict(cached))
        else:
            missing_tags.append((tag, date))

    loop = asyncio.get_running_loop()
    semaphore = asyncio.Semaphore(4)

    async def _bounded_metrics(tag: str, date: str) -> dict | None:
        async with semaphore:
            return await loop.run_in_executor(None, _metrics_for_tag, tag, date)

    results = await asyncio.gather(*[
        _bounded_metrics(tag, date)
        for tag, date in missing_tags
    ])

    new_points = [r for r in results if r is not None]
    points_by_tag = {point["tag"]: point for point in cached_points + new_points}
    points = [points_by_tag[tag] for tag, _date in tags if tag in points_by_tag]

    if cache_path and new_points:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({
                    "schema": 1,
                    "points": points_by_tag,
                    "updated_at": utc_now_iso(),
                }, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
        except OSError:
            log.warning("Failed to write evolution metrics cache: %s", cache_path, exc_info=True)

    # Override latest tag's memory with live file sizes (same formula as historical: identity + scratchpad)
    if data_dir and points:
        mem_dir = os.path.join(data_dir, "memory")
        if os.path.isdir(mem_dir):
            def _file_kb(path: str) -> float:
                try:
                    return os.path.getsize(path) / 1024
                except OSError:
                    return 0

            identity_kb = _file_kb(os.path.join(mem_dir, "identity.md"))
            scratchpad_kb = _file_kb(os.path.join(mem_dir, "scratchpad.md"))

            points[-1]["identity_kb"] = round(identity_kb, 2)
            points[-1]["scratchpad_kb"] = round(scratchpad_kb, 2)
            points[-1]["memory_kb"] = round(identity_kb + scratchpad_kb, 2)

    return points


# ---------------------------------------------------------------------------
# Review-artifact preview helper (DEVELOPMENT.md item 2(f) compliance)
# ---------------------------------------------------------------------------

def truncate_review_artifact(text: str | None, limit: int = 4000) -> str:
    """Return text or a capped prefix with an explicit OMISSION NOTE.

    Complies with DEVELOPMENT.md item 2(f): review outputs and cognitive
    artifacts must not be silently clipped with raw [:N] slicing.  Use this
    helper everywhere a review-output string needs a display-safe preview.
    An omission note including the original length is appended so nothing is
    silently lost.

    Accepts None (e.g. JSON null from a reviewer) and coerces it to "".
    """
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n⚠️ OMISSION NOTE: truncated at {limit} chars; original length {len(text)}"


def truncate_review_reason(text: str, limit: int = 120) -> str:
    """Compact preview of a single reviewer reason/finding string.

    Uses explicit omission note rather than silent clipping.
    """
    return truncate_review_artifact(text, limit=limit)

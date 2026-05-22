"""Shared low-level utilities with no ouroboros.* imports."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
import math
import os
import pathlib
import subprocess
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)

_log_sink: Optional[Callable[[Dict[str, Any]], None]] = None


def set_log_sink(fn: Optional[Callable[[Dict[str, Any]], None]]) -> None:
    global _log_sink
    _log_sink = fn

def utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()

def emit_log_event(
    event_queue: Any,
    payload: Dict[str, Any],
    *,
    blocking: bool = False,
    log_label: str = "live log",
) -> None:
    """Best-effort log_event publish; blocking preserves critical live logs."""
    if event_queue is None:
        return
    try:
        envelope = {"type": "log_event", "data": dict(payload)}
        if blocking:
            event_queue.put(envelope)
        else:
            event_queue.put_nowait(envelope)
    except Exception:
        log.debug("Failed to emit %s event", log_label, exc_info=True)

def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def atomic_write_json(
    path: pathlib.Path,
    payload: Dict[str, Any],
    *,
    trailing_newline: bool = False,
    fsync: bool = False,
) -> None:
    """Atomically persist a JSON object using a unique sibling temp file."""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = (
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    )
    tmp = path.with_name(tmp_name)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if trailing_newline:
        content += "\n"
    try:
        if fsync:
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            try:
                os.write(fd, content.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def read_json_dict(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Return a JSON object from ``path`` or ``None`` when absent/invalid."""
    path = pathlib.Path(path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Failed to parse JSON file %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> bool:
    """Append a JSON object as a line to a JSONL file (concurrent-safe).

    Returns ``True`` on successful write, ``False`` when all retries
    failed (which is also logged at WARNING). Important events
    (``task_done``, ``llm_round``, escalation messages) need that signal
    so the caller can fall back to an in-memory queue or stderr instead
    of pretending the write succeeded.
    """
    if not isinstance(path, pathlib.Path):
        raise TypeError(f"append_jsonl: path must be pathlib.Path, got {type(path).__name__}")
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
                return True
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))

        for attempt in range(write_retries):
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                _written = True
                return True
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
    if not _written:
        log.warning("append_jsonl: all write attempts failed for %s", path)
    return _written


def iter_jsonl_objects(
    path: pathlib.Path,
    max_entries: Optional[int] = None,
    tail_bytes: Optional[int] = None,
    dict_only: bool = True,
) -> Iterator[Any]:
    """Yield parseable JSONL entries; max_entries applies to raw tail lines."""
    path = pathlib.Path(path)
    if (max_entries is not None and max_entries <= 0) or (tail_bytes is not None and tail_bytes <= 0):
        return
    try:
        with path.open("rb") as handle:
            if tail_bytes is not None:
                file_size = path.stat().st_size
                if file_size > tail_bytes:
                    start = file_size - tail_bytes
                    handle.seek(start - 1)
                    if handle.read(1) != b"\n":
                        handle.readline()
            lines = deque(handle, maxlen=max_entries) if max_entries else handle
            for raw in lines:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not dict_only or isinstance(entry, dict):
                    yield entry
    except FileNotFoundError:
        return


def iter_llm_usage_events(
    path: pathlib.Path,
    *,
    max_entries: Optional[int] = None,
    tail_bytes: Optional[int] = None,
) -> Iterator[Dict[str, Any]]:
    for event in iter_jsonl_objects(path, max_entries=max_entries, tail_bytes=tail_bytes):
        if event.get("type") == "llm_usage":
            yield event


def llm_usage_cost(event: Dict[str, Any]) -> float:
    usage = event.get("usage")
    value = event.get("cost")
    if value is None and isinstance(usage, dict):
        value = usage.get("cost")
    try:
        cost = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return cost if math.isfinite(cost) else 0.0


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def safe_relpath(p: str) -> str:
    """Normalize relative paths and reject traversal/NUL/control-char payloads."""
    if not isinstance(p, str):
        raise ValueError("Path must be a string.")
    for ch in p:
        if ch == "\x00":
            raise ValueError("Path contains NUL byte.")
        if ord(ch) < 0x20 and ch not in ("\t", "\n", "\r"):
            raise ValueError(
                f"Path contains control character U+{ord(ch):04X}."
            )
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p

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
    """Return False for error-prefix results and JSON {"ok": false}."""
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

def run_cmd(cmd: List[str], cwd: Optional[pathlib.Path] = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        )
    return res.stdout.strip()

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

def sanitize_task_for_event(
    task: Dict[str, Any], drive_logs: pathlib.Path, threshold: int = 4000,
) -> Dict[str, Any]:
    """Sanitize task event logs while persisting full oversized text."""
    try:
        sanitized = task.copy()

        keys_to_strip = [k for k in sanitized.keys() if k.endswith("_base64")]
        for key in keys_to_strip:
            value = sanitized.pop(key)
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

import re as _re
_SECRET_PATTERNS = _re.compile(
    r'ghp_[A-Za-z0-9]{30,}'       # GitHub personal access token
    r'|gh[ousr]_[A-Za-z0-9]{30,}' # GitHub OAuth/user/server/refresh tokens
    r'|github_pat_[A-Za-z0-9_]{30,}'  # GitHub fine-grained personal access token
    r'|AKIA[0-9A-Z]{16}'          # AWS access key id
    r'|sk_live_[A-Za-z0-9]{24,}'  # Stripe live secret key
    r'|sk_test_[A-Za-z0-9]{24,}'  # Stripe test secret key
    r'|sk-ant-[A-Za-z0-9_\-]{30,}' # Anthropic API key
    r'|sk-or-[A-Za-z0-9\-]{30,}'  # OpenRouter API key
    r'|sk-proj-[A-Za-z0-9_\-]{30,}'  # OpenAI project key
    r'|sk-svcacct-[A-Za-z0-9_\-]{30,}'  # OpenAI service account key
    r'|sk-admin-[A-Za-z0-9_\-]{30,}'  # OpenAI admin key
    r'|gsk_[A-Za-z0-9]{30,}'      # Groq API key
    r'|sk-[A-Za-z0-9]{40,}'       # OpenAI API key
    r'|\b[0-9]{8,}:[A-Za-z0-9_\-]{30,}\b'  # Telegram bot token (digits:alphanum)
)
_SECRET_BEARER_RE = _re.compile(r'(?i)\bBearer\s+([A-Za-z0-9_\-./+=]{24,})')
_SECRET_URL_CREDENTIAL_RE = _re.compile(
    r'(?i)\b(?:postgres|postgresql|mysql|mariadb|mongodb(?:\+srv)?|redis)://[^/\s:@]+:[^/\s@]+@'
)
_SECRET_LITERAL_FIELDS_RE = _re.compile(
    r'(?im)(?:^|[\s,{])["\']?([A-Za-z_][A-Za-z0-9_-]*)["\']?\s*[:=]\s*["\']([^"\']+)["\']'
)
_SECRET_BRACKET_LITERAL_RE = _re.compile(
    r'(?im)\[\s*["\']([A-Za-z_][A-Za-z0-9_-]*)["\']\s*\]\s*[:=]\s*["\']([^"\']+)["\']'
)
_SECRET_UNQUOTED_ASSIGNMENT_RE = _re.compile(
    r'(?im)^([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*([A-Za-z0-9_\-./+=]{16,})\s*$'
)
_SECRET_FALLBACK_LITERAL_RE = _re.compile(
    r'(?i)(?:os\.getenv|os\.environ\.get|settings\.get)\(\s*[\'"]([^\'"]+)[\'"][^)]*,\s*[\'"]([^\'"]+)[\'"]'
    r'|api\.get_settings\([^)]*\)\.get\(\s*[\'"]([^\'"]+)[\'"][^)]*,\s*[\'"]([^\'"]+)[\'"]'
    r'|process\.env\.([A-Z0-9_]+)\s*(?:\|\||\?\?)\s*[\'"]([^\'"]+)[\'"]'
)
_SECRET_KEY_NAME_RE = _re.compile(
    r'(?i)^(?:'
    r'token|access_token|refresh_token|auth_token|secret|secret_key|password|passwd|passphrase|authorization|'
    r'api[_-]?key|database_url|db_url|ouroboros_network_password|aws_access_key_id|aws_secret_access_key|stripe_secret_key|'
    r'[a-z0-9_-]+(?:[_-](?:token|secret|password|passwd|passphrase|api[_-]?key))'
    r')$'
)


def _secret_key_name(key: str) -> bool:
    raw = str(key or "").strip()
    snake = raw.lower() if raw.upper() == raw else _re.sub(r"(?<!^)(?=[A-Z])", "_", raw).lower()
    normalized = _re.sub(r"[^a-z0-9]+", "_", snake).strip("_")
    return bool(_SECRET_KEY_NAME_RE.match(normalized))


def _secret_placeholder_value(value: str) -> bool:
    cleaned = str(value or "").strip().rstrip(",}]").strip().strip("'\"").strip()
    if not cleaned:
        return True
    lowered = cleaned.lower()
    if lowered in {"redacted", "***redacted***", "set_via_env", "set-in-settings", "changeme", "example"}:
        return True
    if lowered == "bearer":
        return True
    if lowered.startswith("bearer "):
        bearer_value = cleaned[7:].strip()
        if _secret_placeholder_value(bearer_value):
            return True
    if lowered in {"str", "string", "int", "float", "bool", "none", "null", "undefined"}:
        return True
    if lowered.startswith(("str ", "str|", "str |", "str)", "str):", "string ", "string|", "string |", "string)", "string):")):
        return True
    if lowered.startswith(("os.environ", "os.getenv", "process.env", "settings.", "api.get_settings")):
        for literal in _re.findall(r"['\"]([^'\"]*)['\"]", cleaned):
            if literal and not _secret_placeholder_value(literal) and not _secret_key_name(literal):
                return False
        return True
    if lowered.startswith(("f\"", "f'")) and "{" in cleaned:
        return True
    if "settings" in lowered and any(word in lowered for word in ("configure", "configured", "set", "enter", "provide")):
        return True
    if "+" in cleaned and any(part in lowered for part in ("token", "key", "secret", "settings", "env")):
        return True
    if cleaned.startswith(("<", "${", "{")) and (cleaned.endswith((">", "}")) or cleaned.count("{") == 1):
        return True
    if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+\(?[^)]*\)?$", cleaned):
        return True
    if _re.match(r"^[A-Za-z_][A-Za-z0-9_]*\([^)]*\)$", cleaned):
        return True
    if _re.match(r"^[a-z_][a-z0-9_]*$", cleaned) and cleaned in {
        "password",
        "token",
        "secret",
        "api_key",
        "auth_header",
        "access_token",
        "refresh_token",
    }:
        return True
    if cleaned.isupper() and "_" in cleaned and not any(ch.isdigit() for ch in cleaned) and _secret_key_name(cleaned):
        return True
    return False


def sanitize_tool_result_for_log(result: str) -> str:
    """Redact potential secrets from tool result before logging."""
    if not isinstance(result, str) or len(result) < 20:
        return result
    return _SECRET_PATTERNS.sub("***REDACTED***", result)


def contains_real_secret_value(text: str) -> tuple[bool, List[str]]:
    """Detect concrete secret values by format and simple literal assignments."""
    if not isinstance(text, str) or not text:
        return False, []
    matches = [
        *_SECRET_PATTERNS.findall(text),
        *_SECRET_BEARER_RE.findall(text),
        *_SECRET_URL_CREDENTIAL_RE.findall(text),
    ]
    matches.extend(
        literal
        for env_key, env_literal, api_key, api_literal, js_key, js_literal in _SECRET_FALLBACK_LITERAL_RE.findall(text)
        for key, literal in ((env_key, env_literal), (api_key, api_literal), (js_key, js_literal))
        if key and literal and _secret_key_name(key) and not _secret_placeholder_value(literal)
    )
    matches.extend(
        value.strip()
        for key, value in _SECRET_LITERAL_FIELDS_RE.findall(text)
        if _secret_key_name(key) and not _secret_placeholder_value(value)
    )
    matches.extend(
        value.strip()
        for key, value in _SECRET_BRACKET_LITERAL_RE.findall(text)
        if _secret_key_name(key) and not _secret_placeholder_value(value)
    )
    matches.extend(
        value.strip()
        for key, value in _SECRET_UNQUOTED_ASSIGNMENT_RE.findall(text)
        if _secret_key_name(key) and not _secret_placeholder_value(value)
    )
    return bool(matches), list(matches)


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
            return f"<TRUNCATED:{key}:{len(value)}ch:sha={sha256_text(value)[:12]}>"
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

    def _parse_journal(filepath: str, size_key: str) -> list[tuple[_dt.datetime, float]]:
        """Parse a JSONL journal into sorted (datetime, size_kb) tuples."""
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

    # Latest tag uses live identity+scratchpad sizes.
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

def truncate_review_artifact(text: str | None, limit: int = 4000) -> str:
    """Return a display-safe preview with explicit OMISSION NOTE, never silent clipping."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n⚠️ OMISSION NOTE: truncated at {limit} chars; original length {len(text)}"


def truncate_review_reason(text: str, limit: int = 120) -> str:
    """Compact reviewer reason preview with explicit omission note."""
    return truncate_review_artifact(text, limit=limit)

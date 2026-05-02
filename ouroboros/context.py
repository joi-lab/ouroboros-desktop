"""
Ouroboros context builder.

Assembles LLM context from prompts, memory, logs, and runtime state.
Extracted from agent.py to keep the agent thin and focused.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, estimate_tokens, get_git_info,
)
from ouroboros.memory import Memory

log = logging.getLogger(__name__)


def _maybe_prepend_resume_hint(env: Any, task: Dict[str, Any], text: str) -> str:
    """If ``OUROBOROS_RESUME_DETECTION=true`` and the user's text contains
    a continuation signal AND a recent checkpoint exists for this chat,
    prepend the checkpoint as a resume-hint block. Otherwise return text
    unchanged. Behaviour is dormant by default — flag must be explicitly
    enabled in settings.json or env."""
    flag_raw = (os.environ.get("OUROBOROS_RESUME_DETECTION", "") or "").strip().lower()
    if flag_raw not in ("true", "1", "yes", "on"):
        return text
    try:
        from ouroboros.task_checkpoint import (
            build_resume_hint_block,
            detect_resume_signal,
            latest_checkpoint_for_chat,
        )
    except Exception:
        return text
    if not detect_resume_signal(text):
        return text
    chat_id = task.get("chat_id")

    class _DriveOnly:
        def __init__(self, fn):
            self.drive_path = fn
    drive_path_fn = getattr(env, "drive_path", None)
    if drive_path_fn is None:
        return text
    cp = latest_checkpoint_for_chat(_DriveOnly(drive_path_fn), chat_id=chat_id)
    if cp is None:
        return text
    block = build_resume_hint_block(cp)
    if not block:
        return text
    return block + text


def build_user_content(task: Dict[str, Any], env: Any = None) -> Any:
    """Build user message content. Supports text + optional image.

    If ``env`` is provided AND ``OUROBOROS_RESUME_DETECTION=true`` AND the
    text matches a continuation signal AND a recent checkpoint exists for
    this chat, the checkpoint is prepended as a resume-hint. Otherwise no
    behaviour change.
    """
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")
    image_caption = task.get("image_caption", "")

    # Resume-hint injection (Phase 2; flag-gated, dormant by default).
    if env is not None:
        text = _maybe_prepend_resume_hint(env, task, text)

    if not image_b64:
        # Return fallback text if both text and image are empty
        if not text:
            return "(empty message)"
        return text

    # Multipart content with text + image
    parts = []
    # Combine caption and text for the text part
    combined_text = ""
    if image_caption:
        combined_text = image_caption
    if text and text != image_caption:
        combined_text = (combined_text + "\n" + text).strip() if combined_text else text

    # Always include a text part when there's an image
    if not combined_text:
        combined_text = "Analyze the screenshot"

    parts.append({"type": "text", "text": combined_text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def build_runtime_section(env: Any, task: Dict[str, Any]) -> str:
    """Build the runtime context section (utc_now, repo_dir, drive_root, git_head, git_branch, task info, budget info)."""
    # --- Git context ---
    try:
        git_branch, git_sha = get_git_info(env.repo_dir)
    except Exception:
        log.debug("Failed to get git info for context", exc_info=True)
        git_branch, git_sha = "unknown", "unknown"

    # --- Budget calculation ---
    budget_info = None
    try:
        state_json = safe_read(env.drive_path("state/state.json"), fallback="{}")
        state_data = json.loads(state_json)
        spent_usd = float(state_data.get("spent_usd", 0))
        total_usd = float(os.environ.get("TOTAL_BUDGET", "1"))
        remaining_usd = total_usd - spent_usd
        budget_info = {"total_usd": total_usd, "spent_usd": spent_usd, "remaining_usd": remaining_usd}
    except Exception:
        log.debug("Failed to calculate budget info for context", exc_info=True)
        pass

    # --- Runtime context JSON ---
    # P0 (perf audit, 2026-05-01): the wall-clock timestamp used to live
    # here. It changed on every prompt assembly call and was the single
    # biggest cause of LM Studio KV-cache misses (every round = different
    # prefix bytes = full-prompt reprocessing, ~110s/call). Removed.
    # Tasks that genuinely need wall-clock time can request it via tools
    # (run_shell ["date","-u","+%FT%TZ"] or a dedicated tool) — making it
    # explicit when the model is reasoning about time. The runtime context
    # is now stable across rounds for the duration of a task.
    _is_desktop = bool(os.environ.get("OUROBOROS_DESKTOP_MODE", ""))
    runtime_data = {
        "repo_dir": str(env.repo_dir),
        "drive_root": str(env.drive_root),
        "git_head": git_sha,
        "git_branch": git_branch,
        "task": {"id": task.get("id"), "type": task.get("type")},
        "runtime_env": {
            "is_desktop": _is_desktop,
            "platform": sys.platform,
        },
    }
    if budget_info:
        runtime_data["budget"] = budget_info
    runtime_ctx = json.dumps(runtime_data, ensure_ascii=False, indent=2)
    return "## Runtime context\n\n" + runtime_ctx


_SECTION_BUDGETS = {
    "scratchpad": 90_000,
    "identity": 80_000,
    "registry": 30_000,
}


# P3 + P4 (perf audit): per-process caches for file-backed prompt sections.
# When a file's mtime is unchanged from the previous prompt assembly, reuse
# the previous text block byte-for-byte. Foundation of LM Studio KV-cache
# reuse — even the smallest byte difference in the prompt prefix busts the
# cache and forces a full prompt-processing pass.
#
# Key on (str(path),) → (mtime_ns, cached_text). Process-scoped (not
# task-scoped) so background-consciousness assemblies and cross-task
# continuations also benefit.
#
# Cache is BEST-EFFORT — any failure (file missing, OS error) falls back to
# direct read. Master kill-switch: OUROBOROS_DISABLE_CONTEXT_CACHE.
_FILE_TEXT_CACHE: Dict[str, Tuple[int, str]] = {}
_SECTION_TEXT_CACHE: Dict[str, Tuple[Tuple[Tuple[str, int], ...], str]] = {}


def _context_cache_disabled() -> bool:
    return (os.environ.get("OUROBOROS_DISABLE_CONTEXT_CACHE", "") or "").strip().lower() in (
        "true", "1", "yes",
    )


def _read_with_cache(path: pathlib.Path) -> str:
    """Read a file via the per-process mtime cache. Falls back to direct
    read on any cache lookup error."""
    if _context_cache_disabled():
        try:
            return path.read_text(encoding="utf-8") if path.exists() else ""
        except Exception:
            return ""
    key = str(path)
    try:
        st = path.stat()
        mtime_ns = st.st_mtime_ns
    except Exception:
        return ""
    cached = _FILE_TEXT_CACHE.get(key)
    if cached and cached[0] == mtime_ns:
        return cached[1]
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        text = ""
    _FILE_TEXT_CACHE[key] = (mtime_ns, text)
    return text


def _section_cache_key(paths: List[pathlib.Path]) -> Tuple[Tuple[str, int], ...]:
    """Build a cache key from a list of paths and their mtimes. Missing
    files contribute (path, -1) so absence-vs-presence flips invalidate."""
    out: List[Tuple[str, int]] = []
    for p in paths:
        try:
            mt = p.stat().st_mtime_ns if p.exists() else -1
        except Exception:
            mt = -1
        out.append((str(p), mt))
    return tuple(out)


def _cached_section(
    cache_key: str,
    paths: List[pathlib.Path],
    builder,
) -> str:
    """Run ``builder()`` and cache its output keyed on the mtimes of
    ``paths``. Subsequent calls with unchanged mtimes return the cached
    text without re-running the builder.

    The same byte-for-byte text every round is what enables LM Studio's
    KV cache to actually hit.
    """
    if _context_cache_disabled():
        return builder()
    key = _section_cache_key(paths)
    cached = _SECTION_TEXT_CACHE.get(cache_key)
    if cached and cached[0] == key:
        return cached[1]
    try:
        text = builder()
    except Exception:
        log.debug("Section builder %s failed", cache_key, exc_info=True)
        text = ""
    _SECTION_TEXT_CACHE[cache_key] = (key, text)
    return text


def _warn_if_over_budget(name: str, content: str) -> None:
    budget = _SECTION_BUDGETS.get(name)
    if budget and len(content) > budget:
        log.warning("Context section '%s' exceeds budget: %d chars > %d", name, len(content), budget)


def _parse_budget_chars(raw: str) -> Optional[int]:
    token = str(raw or "").strip().lower()
    token = token.replace("chars", "").replace("char", "").strip()
    token = token.replace(",", "").replace("_", "")
    if token.endswith("k"):
        try:
            return int(float(token[:-1]) * 1000)
        except ValueError:
            return None
    if token.isdigit():
        return int(token)
    return None


def _parse_file_size_budgets(dev_text: str) -> List[Tuple[str, int]]:
    budgets: List[Tuple[str, int]] = []
    in_section = False
    for line in dev_text.splitlines():
        if line.startswith("### File Size Budgets"):
            in_section = True
            continue
        if in_section and line.startswith("### "):
            break
        if not in_section or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0].lower() in {"file", "path"} or set(cells[0]) == {"-"}:
            continue
        budget = _parse_budget_chars(cells[1])
        if budget:
            budgets.append((cells[0], budget))
    return budgets


def _iter_budget_paths(root: pathlib.Path, pattern: str) -> List[pathlib.Path]:
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    path = root / pattern
    return [path] if path.exists() and path.is_file() else []


def _append_file_size_budget_checks(env: Any, checks: List[str]) -> None:
    try:
        repo_root = env.repo_dir if not isinstance(env, dict) else pathlib.Path(env["repo_dir"])
        drive_root = env.drive_root if not isinstance(env, dict) else pathlib.Path(env["drive_root"])
        dev_text = read_text(repo_root / "docs" / "DEVELOPMENT.md")
        seen: set[str] = set()
        for relpath, budget in _parse_file_size_budgets(dev_text):
            root = drive_root if relpath.startswith("memory/") else repo_root
            for fpath in _iter_budget_paths(root, relpath):
                resolved = str(fpath.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                size = fpath.stat().st_size
                label = str(fpath.relative_to(root)).replace("\\", "/")
                if size > budget:
                    checks.append(
                        f"WARNING: FILE SIZE BUDGET EXCEEDED — {label} is {size:,} chars "
                        f"(budget {budget:,}). Consolidate it or revise the budget in DEVELOPMENT.md."
                    )
                elif size >= int(budget * 0.9):
                    checks.append(
                        f"WARNING: FILE SIZE NEAR BUDGET — {label} is {size:,} chars "
                        f"({int(size * 100 / budget)}% of {budget:,}). Consider consolidation."
                    )
    except Exception:
        log.debug("Failed to append file size budget checks", exc_info=True)


def build_memory_sections(memory: Memory, partition: str = "all") -> List[str]:
    """Build memory sections, optionally partitioned by volatility.

    partition:
      - "all": scratchpad, identity, dialogue, registry (legacy/default)
      - "stable": identity only
      - "volatile": scratchpad and dialogue only (registry digest is injected separately)
    """
    sections = []

    include_stable = partition in {"all", "stable"}
    include_volatile = partition in {"all", "volatile"}

    if include_volatile:
        scratchpad_raw = memory.load_scratchpad()
        _warn_if_over_budget("scratchpad", scratchpad_raw)
        sections.append("## Scratchpad\n\n" + scratchpad_raw)

    if include_stable:
        identity_raw = memory.load_identity()
        _warn_if_over_budget("identity", identity_raw)
        sections.append("## Identity\n\n" + identity_raw)

    try:
        from ouroboros.consolidator import migrate_dialogue_summary_to_blocks
        migrate_dialogue_summary_to_blocks(
            memory.drive_root / "memory" / "dialogue_summary.md",
            memory.drive_root / "memory" / "dialogue_blocks.json",
        )
    except Exception:
        pass

    if include_volatile:
        dialogue_blocks = memory.load_dialogue_blocks()
        if dialogue_blocks:
            blocks_md = memory.format_blocks_as_markdown(dialogue_blocks)
            if blocks_md.strip():
                sections.append("## Dialogue History\n\n" + blocks_md)
        else:
            summary_path = memory.drive_root / "memory" / "dialogue_summary.md"
            if summary_path.exists():
                summary_text = read_text(summary_path)
                if summary_text.strip():
                    sections.append("## Dialogue Summary\n\n" + summary_text)

    if partition == "all":
        registry_path = memory.drive_root / "memory" / "registry.md"
        if registry_path.exists():
            registry_text = read_text(registry_path)
            if registry_text.strip():
                _warn_if_over_budget("registry", registry_text)
                sections.append("## Memory Registry\n\n" + registry_text)

    return sections


def _format_recent_reflections(entries: List[Dict[str, Any]], limit: int = 10) -> str:
    """Format recent execution reflections for dynamic context."""
    if not entries:
        return ""

    blocks: List[str] = []
    for entry in entries[-limit:]:
        ts_full = str(entry.get("ts", ""))
        ts = ts_full[:16] if len(ts_full) >= 16 else ts_full
        header_bits = [bit for bit in [
            ts,
            str(entry.get("task_type", "")).strip(),
            str(entry.get("task_id", "")).strip(),
        ] if bit]
        header = " | ".join(header_bits) or "unknown reflection"

        lines = [f"### {header}"]

        goal = str(entry.get("goal", "")).strip()
        if goal:
            lines.append(f"- Goal: {goal}")

        markers = [str(m).strip() for m in (entry.get("key_markers") or []) if str(m).strip()]
        if markers:
            lines.append(f"- Markers: {', '.join(markers)}")

        rounds = entry.get("rounds")
        if rounds not in (None, ""):
            lines.append(f"- Rounds: {rounds}")

        cost_usd = entry.get("cost_usd")
        if cost_usd not in (None, ""):
            lines.append(f"- Cost: ${cost_usd}")

        reflection = str(entry.get("reflection", "")).strip()
        if reflection:
            lines.append("")
            lines.append(reflection)

        blocks.append("\n".join(lines).strip())

    return "\n\n".join(blocks)


def build_recent_sections(memory: Memory, env: Any, task_id: str = "") -> List[str]:
    """Build recent dialogue and process-memory sections."""
    sections = []

    chat_summary = memory.summarize_chat(memory.read_jsonl_tail("chat.jsonl", 1000))
    if chat_summary:
        sections.append("## Recent chat\n\n" + chat_summary)

    progress_entries = memory.read_jsonl_tail("progress.jsonl", 200)
    if task_id:
        progress_entries = [e for e in progress_entries if str(e.get("task_id", "")).strip() == task_id]
    progress_summary = memory.summarize_progress(progress_entries, limit=50)
    if progress_summary:
        sections.append("## Recent progress\n\n" + progress_summary)

    tools_entries = memory.read_jsonl_tail("tools.jsonl", 200)
    if task_id:
        tools_entries = [e for e in tools_entries if str(e.get("task_id", "")).strip() == task_id]
    tools_summary = memory.summarize_tools(tools_entries)
    if tools_summary:
        sections.append("## Recent tools\n\n" + tools_summary)

    events_entries = memory.read_jsonl_tail("events.jsonl", 200)
    if task_id:
        events_entries = [e for e in events_entries if str(e.get("task_id", "")).strip() == task_id]
    events_summary = memory.summarize_events(events_entries)
    if events_summary:
        sections.append("## Recent events\n\n" + events_summary)

    supervisor_summary = memory.summarize_supervisor(memory.read_jsonl_tail("supervisor.jsonl", 200))
    if supervisor_summary:
        sections.append("## Supervisor\n\n" + supervisor_summary)

    reflections_entries = memory.read_jsonl_tail("task_reflections.jsonl", 20)
    reflections_text = _format_recent_reflections(reflections_entries, limit=10)
    if reflections_text:
        sections.append("## Execution reflections\n\n" + reflections_text)

    return sections


def _collect_log_analysis_checks(env: Any, checks: List[str]) -> None:
    import hashlib
    import time as _time

    try:
        from ouroboros.consciousness import BackgroundConsciousness
        consciousness_md = safe_read(env.repo_path("prompts/CONSCIOUSNESS.md"))
        if consciousness_md:
            whitelist = BackgroundConsciousness._BG_TOOL_WHITELIST
            scan_text = re.sub(r'```.*?```', '', consciousness_md, flags=re.DOTALL)
            tool_prefixes = (
                "schedule_", "update_", "knowledge_", "browse_", "analyze_",
                "web_", "send_", "repo_", "data_", "chat_", "list_", "get_",
                "wait_", "set_", "memory_",
            )
            prompt_tool_refs = {
                match.group(1)
                for match in re.finditer(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b', scan_text)
                if match.group(1) in whitelist or any(match.group(1).startswith(prefix) for prefix in tool_prefixes)
            }
            phantom = prompt_tool_refs - whitelist
            if phantom:
                checks.append(f"WARNING: PROMPT-RUNTIME DRIFT — CONSCIOUSNESS.md references tools not in BG whitelist: {', '.join(sorted(phantom))}")
            else:
                checks.append("OK: prompt-runtime sync (no phantom tools)")
    except Exception:
        pass

    try:
        msg_hash_to_tasks: Dict[str, set] = {}
        for log_path, type_field, type_value in (
            (env.drive_path("logs/events.jsonl"), "type", "owner_message_injected"),
            (env.drive_path("logs/supervisor.jsonl"), "event_type", "owner_message_injected"),
        ):
            if not log_path.exists():
                continue
            file_size = log_path.stat().st_size
            with log_path.open("r", encoding="utf-8") as f:
                if file_size > 256_000:
                    f.seek(file_size - 256_000)
                    f.readline()
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    if ev.get(type_field) != type_value:
                        continue
                    text = ev.get("text", "")
                    if not text and "event_repr" in ev:
                        event_repr = str(ev.get("event_repr", ""))
                        text = event_repr[:200] + f" [...{len(event_repr) - 200} chars omitted]" if len(event_repr) > 200 else event_repr
                    if not text:
                        continue
                    task_ids = msg_hash_to_tasks.setdefault(hashlib.md5(text.encode()).hexdigest()[:12], set())
                    task_ids.add(ev.get("task_id") or "unknown")
        dupes = {h: tids for h, tids in msg_hash_to_tasks.items() if len(tids) > 1}
        if dupes:
            checks.append(f"CRITICAL: DUPLICATE PROCESSING — {len(dupes)} message(s) appeared in multiple tasks: {', '.join(str(sorted(tids)) for tids in dupes.values())}")
        else:
            checks.append("OK: no duplicate message processing detected")
    except Exception:
        pass

    try:
        hit_rate = _compute_cache_hit_rate(env)
        if hit_rate is not None:
            if hit_rate < 0.30:
                checks.append(f"WARNING: LOW CACHE HIT RATE — {hit_rate:.0%} cached. Context structure may be degrading prompt caching efficiency.")
            elif hit_rate >= 0.50:
                checks.append(f"OK: cache hit rate ({hit_rate:.0%})")
            else:
                checks.append(f"INFO: cache hit rate moderate ({hit_rate:.0%})")
    except Exception:
        pass

    try:
        events_path = env.drive_path("logs/events.jsonl")
        if events_path.exists():
            llm_error_models: Counter = Counter()
            local_overflow_models: Counter = Counter()
            file_size = events_path.stat().st_size
            with events_path.open("r", encoding="utf-8") as f:
                if file_size > 256_000:
                    f.seek(file_size - 256_000)
                    f.readline()
                for raw in f:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    evt_type = str(ev.get("type") or "")
                    model = str(ev.get("model") or "unknown")
                    if evt_type in {"llm_api_error", "review_model_error", "consciousness_llm_error", "provider_incomplete_response"}:
                        llm_error_models[model] += 1
                    elif evt_type == "local_context_overflow":
                        local_overflow_models[model] += 1
            if llm_error_models:
                top = ", ".join(f"{model} x{count}" for model, count in llm_error_models.most_common(3))
                checks.append(f"WARNING: PROVIDER/ROUTING ERRORS — {sum(llm_error_models.values())} recent failures ({top}). Reliability or failover may need attention.")
            else:
                checks.append("OK: no recent provider/routing errors")
            if local_overflow_models:
                top = ", ".join(f"{model} x{count}" for model, count in local_overflow_models.most_common(3))
                checks.append(f"WARNING: LOCAL CONTEXT OVERFLOW — {sum(local_overflow_models.values())} recent overflow event(s) ({top}). Local context may need more compaction or a larger window.")
            else:
                checks.append("OK: no recent local context overflows")
    except Exception:
        pass

    try:
        rescue_dir = env.drive_path("archive/rescue")
        if rescue_dir.exists():
            recent = []
            now = _time.time()
            for entry in sorted(rescue_dir.iterdir(), reverse=True):
                if not entry.is_dir():
                    continue
                age_sec = now - entry.stat().st_mtime
                if age_sec < 7200:
                    file_count = sum(1 for item in entry.rglob("*") if item.is_file())
                    age_str = f"{int(age_sec // 60)}m ago" if age_sec < 3600 else f"{age_sec / 3600:.1f}h ago"
                    recent.append(f"{entry.name} ({age_str}, {file_count} files)")
                if len(recent) >= 3:
                    break
            if recent:
                checks.append(
                    f"WARNING: RESCUE SNAPSHOT AVAILABLE — {', '.join(recent)}. "
                    "Uncommitted changes were saved before last restart. "
                    "Use data_read to inspect archive/rescue/<dirname>/rescue_meta.json "
                    "and changes.diff to decide if recovery is needed."
                )
    except Exception:
        pass


def build_health_invariants(env: Any) -> str:
    """Build health invariants section for LLM-first self-detection.

    Checks crash_report.json for CRASH ROLLBACK, plus version sync,
    budget drift, memory health, prompt drift, log analysis, and file budgets.
    """
    import time as _time

    checks: List[str] = []

    # --- version sync ---
    try:
        from ouroboros.tools.release_sync import (
            _normalize_pep440,
            _shields_escape,
            extract_architecture_header_version,
            extract_readme_badge_version,
            is_release_version,
        )
        ver_file = read_text(env.repo_path("VERSION")).strip()
        desync_parts = []
        pyproject = read_text(env.repo_path("pyproject.toml"))
        pyproject_ver = ""
        for line in pyproject.splitlines():
            if line.strip().startswith("version"):
                pyproject_ver = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
        if is_release_version(ver_file) and pyproject_ver and _normalize_pep440(ver_file) != pyproject_ver:
            desync_parts.append(f"pyproject.toml={pyproject_ver}")
        try:
            readme = read_text(env.repo_path("README.md"))
            badge_ver = extract_readme_badge_version(readme)
            readme_ver = badge_ver
            if not readme_ver:
                rm = re.search(r'\*\*Version:\*\*\s*([^\s]+)', readme)
                readme_ver = str(rm.group(1) or "").strip() if rm else ""
            badge_token_ok = True
            if badge_ver and is_release_version(ver_file):
                badge_token_ok = f"version-{_shields_escape(ver_file)}-green" in readme
            if readme_ver and readme_ver != ver_file:
                desync_parts.append(f"README={readme_ver}")
            elif readme_ver and not badge_token_ok:
                desync_parts.append("README badge URL token")
        except Exception:
            pass
        try:
            arch = read_text(env.repo_path("docs/ARCHITECTURE.md"))
            arch_ver = extract_architecture_header_version(arch)
            if arch_ver and arch_ver != ver_file:
                desync_parts.append(f"ARCHITECTURE.md={arch_ver}")
        except Exception:
            pass
        if desync_parts:
            checks.append(f"CRITICAL: VERSION DESYNC — VERSION={ver_file}, {', '.join(desync_parts)}")
        elif ver_file:
            checks.append(f"OK: version sync ({ver_file})")
    except Exception:
        pass

    # --- budget drift ---
    try:
        state_data = json.loads(read_text(env.drive_path("state/state.json")))
        if state_data.get("budget_drift_alert"):
            checks.append(f"WARNING: BUDGET DRIFT {state_data.get('budget_drift_pct', 0):.1f}% — tracked=${state_data.get('spent_usd', 0):.2f} vs OpenRouter=${state_data.get('openrouter_total_usd', 0):.2f}")
        else:
            checks.append("OK: budget drift within tolerance")
    except Exception:
        pass

    # --- high-cost tasks ---
    try:
        from supervisor.state import per_task_cost_summary
        costly = [t for t in per_task_cost_summary(5) if t["cost"] > 5.0]
        for t in costly:
            checks.append(f"WARNING: HIGH-COST TASK — task_id={t['task_id']} cost=${t['cost']:.2f} rounds={t['rounds']}")
        if not costly:
            checks.append("OK: no high-cost tasks (>$5)")
    except Exception:
        pass

    # --- identity freshness & size ---
    try:
        identity_path = env.drive_path("memory/identity.md")
        if identity_path.exists():
            age_hours = (_time.time() - identity_path.stat().st_mtime) / 3600
            if age_hours > 8:
                checks.append(f"WARNING: STALE IDENTITY — identity.md last updated {age_hours:.0f}h ago")
            else:
                checks.append("OK: identity.md recent")
    except Exception:
        pass
    try:
        identity_content = read_text(env.drive_path("memory/identity.md"))
        if len(identity_content.strip()) < 200:
            checks.append(f"WARNING: THIN IDENTITY — identity.md is only {len(identity_content)} chars. Cognitive decay signal.")
    except Exception:
        pass

    # --- scratchpad ---
    try:
        sp_len = len(read_text(env.drive_path("memory/scratchpad.md")).strip())
        if sp_len < 50:
            checks.append("WARNING: EMPTY SCRATCHPAD — scratchpad is nearly empty. Memory loss signal.")
        elif sp_len > 50000:
            checks.append(f"WARNING: BLOATED SCRATCHPAD — {sp_len} chars. Extract durable insights to knowledge base.")
        else:
            checks.append(f"OK: scratchpad size ({sp_len} chars)")
    except Exception:
        pass

    # Crash rollback — inlined so inspect.getsource sees the literal strings
    try:
        crash_report = env.drive_path("state/crash_report.json")
        if crash_report.exists():
            crash_data = json.loads(crash_report.read_text(encoding="utf-8"))
            checks.append(
                f"CRITICAL: RECENT CRASH ROLLBACK — rolled back from "
                f"{crash_data.get('rolled_back_from', '?')[:12]} to tag "
                f"{crash_data.get('tag', '?')} at {crash_data.get('ts', '?')}"
            )
    except Exception:
        pass

    _collect_log_analysis_checks(env, checks)
    try:
        _append_file_size_budget_checks(env, checks)
    except Exception:
        pass
    if not checks:
        return ""
    return "## Health Invariants\n\n" + "\n".join(f"- {check}" for check in checks)


def _compute_cache_hit_rate(env: Any) -> Optional[float]:
    """Compute prompt cache hit rate from recent llm_round events."""
    events_path = env.drive_path("logs/events.jsonl")
    if not events_path.exists():
        return None
    total_prompt = total_cached = count = 0
    try:
        file_size = events_path.stat().st_size
        with events_path.open("r", encoding="utf-8") as f:
            if file_size > 256_000:
                f.seek(file_size - 256_000)
                f.readline()
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                    if ev.get("type") != "llm_round":
                        continue
                    usage = ev.get("usage", ev)
                    pt = int(usage.get("prompt_tokens", 0))
                    if pt > 0:
                        total_prompt += pt
                        total_cached += int(usage.get("cached_tokens", 0))
                        count += 1
                except (json.JSONDecodeError, ValueError, TypeError):
                    continue
    except Exception:
        return None
    if count < 5 or total_prompt == 0:
        return None
    return total_cached / total_prompt


def _build_registry_digest(env: Any) -> str:
    """Build a compact one-line-per-source digest from memory/registry.md.

    Returns a markdown table capped at 3000 chars, or empty string if
    the registry doesn't exist.
    """
    reg_path = env.drive_path("memory/registry.md")
    if not reg_path.exists():
        return ""
    try:
        text = reg_path.read_text(encoding="utf-8")
    except Exception:
        return ""

    rows: list = []
    current_id = ""
    fields: dict = {}
    for line in text.split("\n"):
        if line.startswith("### "):
            if current_id:
                rows.append(_registry_row(current_id, fields))
            current_id = line[4:].strip()
            fields = {}
        elif current_id and line.startswith("- **"):
            # Parse "- **Key:** value"
            m = re.match(r'^- \*\*(\w+):\*\*\s*(.*)', line)
            if m:
                fields[m.group(1).lower()] = m.group(2).strip()
    if current_id:
        rows.append(_registry_row(current_id, fields))

    if not rows:
        return ""

    header = "| source | path | updated | gaps |\n|---|---|---|---|"
    table = header + "\n" + "\n".join(rows)
    if len(table) > 3000:
        table = table[:2950] + "\n| ... | (truncated) | | |"
    return "## Memory Registry (what I know / don't know)\n\n" + table


def _registry_row(source_id: str, fields: dict) -> str:
    path = fields.get("path", "?")
    updated = fields.get("updated", "?")
    gaps = fields.get("gaps", "—")
    # Keep gaps short
    if len(gaps) > 60:
        gaps = gaps[:57] + f"... [{len(gaps) - 57} chars omitted]"
    return f"| {source_id} | {path} | {updated} | {gaps} |"


def build_llm_messages(
    env: Any,
    memory: Memory,
    task: Dict[str, Any],
    review_context_builder: Optional[Any] = None,
    soft_cap_tokens: int = 200_000,
    prompt_mode: str = "dense",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Build the full LLM message context for a task.

    ``prompt_mode``:
      ``"dense"`` (default): include every source — full project docs,
      knowledge base index, patterns, deep review, recent JSONL tails. Use
      with cloud models or local models with generous context windows.

      ``"medium"``: drop the three heaviest sources only — README.md,
      KB index dump, deep review. Keep DEVELOPMENT.md, CHECKLISTS.md,
      patterns, backlog, review status, recent JSONL tails. Intended as
      an escalation knob when ``"sparse"`` leaves the agent underfed but
      ``"dense"`` overflows the loaded context window.

      ``"sparse"``: keep only the constitutional core and live state — drops
      DEVELOPMENT/README/CHECKLISTS, KB index dump, patterns, deep review,
      backlog, review status, and recent JSONL tails. The agent can still
      reach those via ``knowledge_*`` / ``data_read`` tools when needed.
      Use with small-context local models (LM Studio loaded at 32k, etc.)
      or as fallback after a context-overflow error.

    Returns (messages, cap_info) tuple.
    """
    _mode = str(prompt_mode or "dense").lower()
    if _mode not in ("dense", "medium", "sparse"):
        _mode = "dense"
    sparse = (_mode == "sparse")
    medium = (_mode == "medium")
    task_type = str(task.get("type") or "user")
    base_prompt = safe_read(
        env.repo_path("prompts/SYSTEM.md"),
        fallback="You are Ouroboros. Your base prompt could not be loaded."
    )
    bible_md = safe_read(env.repo_path("BIBLE.md"))
    # Sparse drops ARCHITECTURE.md too — at 172k chars it's the single
    # heaviest source. Without dropping it, block-0 truncation eats into
    # blocks 1+2, which contain identity / scratchpad / runtime_context
    # — the constitutional core. Dropping ARCH from sparse keeps the core
    # intact and lets the truncation budget cover the dynamic block.
    # The agent can repo_read("docs/ARCHITECTURE.md") on demand if needed.
    arch_md = "" if sparse else safe_read(env.repo_path("docs/ARCHITECTURE.md"))
    # medium drops only the three heaviest sources (README, KB, deep_review).
    # Keep DEVELOPMENT and CHECKLISTS — they shape day-to-day judgment.
    dev_guide_md = "" if sparse else safe_read(env.repo_path("docs/DEVELOPMENT.md"))
    readme_md = "" if (sparse or medium) else safe_read(env.repo_path("README.md"))
    checklists_md = "" if sparse else safe_read(env.repo_path("docs/CHECKLISTS.md"))
    state_json = safe_read(env.drive_path("state/state.json"), fallback="{}")

    memory.ensure_files()

    static_text = (
        base_prompt + "\n\n"
        + "## BIBLE.md\n\n" + bible_md
    )
    if arch_md.strip():
        static_text += "\n\n## ARCHITECTURE.md\n\n" + arch_md
    if dev_guide_md.strip():
        static_text += "\n\n## DEVELOPMENT.md\n\n" + dev_guide_md
    if readme_md.strip():
        static_text += "\n\n## README.md\n\n" + readme_md
    if checklists_md.strip():
        static_text += "\n\n## CHECKLISTS.md\n\n" + checklists_md
    if sparse:
        static_text += (
            "\n\n[Sparse prompt mode active — ARCHITECTURE.md, DEVELOPMENT.md, "
            "README.md, CHECKLISTS.md, KB index, patterns, deep review, backlog, "
            "and recent JSONL tails are omitted from this round. Use repo_read "
            "for ARCHITECTURE, knowledge_list / knowledge_read for KB topics, "
            "and data_read for live state files. The runtime_context block in "
            "the dynamic section above gives you the current repo_dir and "
            "drive_root paths.]"
        )
    elif medium:
        static_text += (
            "\n\n[Medium prompt mode active — README.md, KB index, and deep review "
            "are omitted from this round (DEVELOPMENT.md, CHECKLISTS.md, patterns "
            "remain). Use knowledge_list, knowledge_read, and data_read to reach "
            "the omitted material on demand.]"
        )

    semi_stable_parts = []
    semi_stable_parts.extend(build_memory_sections(memory, partition="stable"))

    if not sparse:
        # P3 + P4 (perf audit): cache the KB / patterns / deep_review block
        # text on file mtime so semi-stable contents are byte-identical
        # round-to-round, enabling LM Studio KV-cache hits.
        kb_index_path = env.drive_path("memory/knowledge/index-full.md")
        patterns_path = env.drive_path("memory/knowledge/patterns.md")
        deep_review_path = env.drive_path("memory/deep_review.md")

        def _build_kb_block() -> str:
            if not kb_index_path.exists():
                return ""
            kb_index = _read_with_cache(kb_index_path)
            return ("## Knowledge base\n\n" + kb_index) if kb_index.strip() else ""

        def _build_patterns_block() -> str:
            if not patterns_path.exists():
                return ""
            patterns_text = _read_with_cache(patterns_path)
            if not patterns_text.strip():
                return ""
            return "## Known error patterns (Pattern Register)\n\n" + patterns_text

        def _build_deep_review_block() -> str:
            if not deep_review_path.exists():
                return ""
            dr_text = _read_with_cache(deep_review_path)
            if not dr_text.strip():
                return ""
            if len(dr_text) > 8000:
                dr_text = dr_text[:8000] + "\n\n[... truncated — full report in memory/deep_review.md]"
            return "## Last Deep Self-Review\n\n" + dr_text

        # medium drops KB index + deep_review (the two heaviest); patterns stays.
        if not medium:
            kb_block = _cached_section("kb_index", [kb_index_path], _build_kb_block)
            if kb_block:
                semi_stable_parts.append(kb_block)
        patterns_block = _cached_section("patterns", [patterns_path], _build_patterns_block)
        if patterns_block:
            semi_stable_parts.append(patterns_block)
        if not medium:
            deep_review_block = _cached_section("deep_review", [deep_review_path], _build_deep_review_block)
            if deep_review_block:
                semi_stable_parts.append(deep_review_block)

    semi_stable_text = "\n\n".join(semi_stable_parts)

    health_section = build_health_invariants(env)
    dynamic_parts = []
    if health_section:
        dynamic_parts.append(health_section)
    dynamic_parts.extend(build_memory_sections(memory, partition="volatile"))

    registry_digest = _build_registry_digest(env)
    if registry_digest:
        dynamic_parts.append(registry_digest)
    dynamic_parts.extend([
        "## Drive state\n\n" + state_json,
        build_runtime_section(env, task),
    ])

    if not sparse:
        try:
            from ouroboros.improvement_backlog import format_backlog_digest

            backlog_digest = format_backlog_digest(env.drive_root)
            if backlog_digest:
                dynamic_parts.append(backlog_digest)
        except Exception:
            log.debug("Failed to build improvement backlog digest", exc_info=True)

        review_section = ""
        if review_context_builder is not None:
            try:
                review_section = str(review_context_builder() or "").strip()
            except Exception:
                log.debug("Failed to build review continuity section", exc_info=True)
        if review_section:
            dynamic_parts.append(review_section)
        else:
            try:
                from ouroboros.review_state import load_state, format_status_section
                advisory_state = load_state(pathlib.Path(env.drive_root))
                if advisory_state.runs or advisory_state.last_commit_attempt:
                    advisory_section = format_status_section(
                        advisory_state,
                        repo_dir=pathlib.Path(env.repo_dir),
                    )
                    if advisory_section:
                        dynamic_parts.append(advisory_section)
            except Exception:
                log.debug("Failed to build advisory review status section", exc_info=True)

        dynamic_parts.extend(build_recent_sections(memory, env, task_id=task.get("id", "")))

    dynamic_text = "\n\n".join(dynamic_parts)

    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": semi_stable_text,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ],
        },
        {"role": "user", "content": build_user_content(task, env=env)},
    ]

    messages, cap_info = apply_message_token_soft_cap(messages, soft_cap_tokens)
    cap_info["prompt_mode"] = _mode
    return messages, cap_info


def apply_message_token_soft_cap(
    messages: List[Dict[str, Any]],
    soft_cap_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Estimate context size without silently trimming cognitive artifacts."""
    def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
        content = msg.get("content", "")
        if isinstance(content, list):
            total = sum(estimate_tokens(str(b.get("text", "")))
                        for b in content if isinstance(b, dict) and b.get("type") == "text")
            return total + 6
        return estimate_tokens(str(content)) + 6

    estimated = sum(_estimate_message_tokens(m) for m in messages)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }
    if soft_cap_tokens > 0 and estimated > soft_cap_tokens:
        info["trimmed_sections"].append("disabled_no_silent_truncation")
    return messages, info


from ouroboros.context_compaction import (
    _COMPACTION_PROTECTED_TOOLS,
    compact_tool_history,
    compact_tool_history_llm,
)


def safe_read(path: pathlib.Path, fallback: str = "") -> str:
    """Read a file, returning fallback if it doesn't exist or errors."""
    try:
        if path.exists():
            return read_text(path)
    except Exception:
        log.debug(f"Failed to read file {path} in safe_read", exc_info=True)
        pass
    return fallback

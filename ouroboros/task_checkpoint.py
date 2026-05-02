"""Auto-snapshot for tasks interrupted by escalation (Resume Phase 1).

When the runtime cuts a task short — wall-clock budget exhausted, hard
ceiling reached, or dedup/gate-retry escalation fired — the agent loses
its in-task working state. The next prompt starts cold: he doesn't know
which files he already read, what he learned, or where he was in the
design. The previously-documented failure mode is the agent re-reading
the same 8 files before realising he already mapped them.

This module writes a structured **markdown checkpoint** to
``memory/task_checkpoints/<task_id>.md`` at escalation time. Pure data
extraction from ``ToolContext`` state — no async, no event-log reads,
no chat plumbing. The checkpoint is dormant by default: nothing reads
it yet (Phase 2 wires resume-detection behind a flag). Phase 1 only
*writes* the snapshot so the data exists when Phase 2 lands.

Information captured (best-effort, missing fields render as ``"(not
available)"``):

  * original task text (from ``ctx.current_task_text`` if present)
  * elapsed wall-clock time and round count
  * distinct tool calls fired (from ``_dedup_counts`` keys)
  * gate failures by (tool, block_reason) (from ``_gate_failure_counts``)
  * dedup pings by tool (from ``_dedup_ping_counts``)
  * compat profile name + ctx_window if present
  * escalation reason

The file is written atomically (tmp + rename) and is safe to read
concurrently — Phase 2's resume-detection grabs the most recent one
matching the new task's chat_id.
"""
from __future__ import annotations

import logging
import os
import pathlib
import tempfile
import time
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

CHECKPOINT_DIR_RELATIVE = "memory/task_checkpoints"


def _safe_get(ctx: Any, attr: str, default: Any = None) -> Any:
    try:
        return getattr(ctx, attr, default)
    except Exception:
        return default


def _format_dedup_summary(counts: Dict[str, int]) -> List[str]:
    """Produce human-readable summary lines for dedup_counts.

    The signature format is ``"<tool>:<sha1[:16]>"`` so we can extract
    the tool name but not the args. Group by tool and surface call
    distribution: ``repo_read: 8 distinct call(s), 12 total``.
    """
    if not counts:
        return ["(no tool calls recorded)"]
    by_tool: Dict[str, List[int]] = {}
    for sig, count in counts.items():
        tool = sig.split(":", 1)[0] if ":" in sig else sig
        by_tool.setdefault(tool, []).append(int(count))
    lines = []
    for tool in sorted(by_tool):
        sigs = by_tool[tool]
        distinct = len(sigs)
        total = sum(sigs)
        lines.append(f"- **{tool}**: {distinct} distinct call(s), {total} total")
    return lines


def _format_gate_failures(counts: Dict[Any, int]) -> List[str]:
    if not counts:
        return ["(none)"]
    lines = []
    for key, count in counts.items():
        if isinstance(key, tuple) and len(key) == 2:
            tool, reason = key
        else:
            tool, reason = str(key), "unknown"
        lines.append(f"- `{tool}` × {count} (block_reason: `{reason}`)")
    return lines


def _format_ping_counts(counts: Dict[str, int]) -> List[str]:
    if not counts:
        return ["(none)"]
    return [f"- `{tool}`: {count} ping(s)" for tool, count in sorted(counts.items())]


def _extract_message_text(content: Any) -> str:
    """Messages can be plain strings or list-of-blocks (multipart). Stringify
    safely so the checkpoint stays human-readable."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("content") or ""
                if t:
                    out.append(str(t))
        return "\n".join(out)
    return str(content or "")


def _extract_task_prompt_and_last_thought(
    messages: Optional[List[Dict[str, Any]]],
) -> tuple:
    """From the conversation messages list, return (user_prompt, last_thought).

    user_prompt: the most recent user-role message content (the actual task).
    last_thought: the most recent non-empty assistant content. Helps the
    Phase 2 resume-detection surface "where Ouro was when interrupted".
    """
    if not messages:
        return "(not available)", "(not available)"
    user_prompt = "(not available)"
    last_thought = "(not available)"
    for msg in messages:
        role = (msg or {}).get("role")
        if role == "user":
            text = _extract_message_text(msg.get("content"))
            if text and text.strip():
                user_prompt = text  # latest user message wins
    for msg in reversed(messages):
        if (msg or {}).get("role") == "assistant":
            text = _extract_message_text(msg.get("content"))
            if text and text.strip():
                last_thought = text
                break
    return user_prompt, last_thought


def write_task_checkpoint(
    ctx: Any,
    *,
    task_id: str,
    reason: str,
    started_ts: float,
    rounds: int,
    extra_note: str = "",
) -> Optional[pathlib.Path]:
    """Write a checkpoint for an interrupted task.

    ``ctx`` is the active ``ToolContext`` (read-only here). ToolContext
    carries ``drive_path()`` so we resolve the checkpoint dir directly
    from it; no separate ``env`` parameter is needed.

    ``reason`` is one of ``wall_budget`` / ``wall_budget_hard_ceiling`` /
    ``gate_retry`` / ``dedup_threshold``.

    Returns the path written on success, or ``None`` if checkpointing
    failed for any reason (best-effort — never raises).
    """
    if not task_id:
        log.debug("task_checkpoint skipped: empty task_id")
        return None
    try:
        drive_path_fn = getattr(ctx, "drive_path", None)
        if drive_path_fn is None:
            log.debug("task_checkpoint skipped: ctx has no drive_path()")
            return None
        out_dir = pathlib.Path(drive_path_fn(CHECKPOINT_DIR_RELATIVE))
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{task_id}.md"

        # Gather state (best-effort)
        elapsed = max(0.0, time.time() - float(started_ts or time.time()))
        elapsed_min = elapsed / 60.0
        dedup_counts = _safe_get(ctx, "_dedup_counts", {}) or {}
        gate_counts = _safe_get(ctx, "_gate_failure_counts", {}) or {}
        ping_counts = _safe_get(ctx, "_dedup_ping_counts", {}) or {}
        extensions = _safe_get(ctx, "_wall_budget_extensions", 0) or 0
        chat_id = _safe_get(ctx, "current_chat_id", None)
        messages = _safe_get(ctx, "messages", None)
        task_text, last_thought = _extract_task_prompt_and_last_thought(messages)

        profile = _safe_get(ctx, "compat_profile", None)
        profile_name = getattr(profile, "name", None) if profile else None
        ctx_window = getattr(profile, "ctx_window", None) if profile else None

        distinct_calls = len(dedup_counts)
        total_calls = sum(int(v) for v in dedup_counts.values())
        total_pings = sum(int(v) for v in ping_counts.values())

        lines: List[str] = [
            f"# Task checkpoint — {task_id}",
            "",
            f"- **Stopped at:** {utc_now_iso()}",
            f"- **Reason:** `{reason}`",
            f"- **Elapsed:** {elapsed_min:.1f} min ({int(elapsed)} sec)",
            f"- **Rounds:** {rounds}",
            f"- **Wall-clock extensions used:** {extensions}",
            f"- **Profile:** `{profile_name or '(unknown)'}`",
        ]
        if ctx_window:
            lines.append(f"- **Ctx window:** {ctx_window}")
        if chat_id is not None:
            lines.append(f"- **Chat:** `{chat_id}`")
        lines.extend([
            "",
            "## Original task prompt",
            "",
            "```",
            str(task_text)[:4000],  # cap to keep checkpoints small
            "```",
            "",
            "## Last thought before stopping",
            "",
            "```",
            str(last_thought)[:2000],
            "```",
            "",
            "## Tool activity (this task)",
            "",
            f"- Distinct tool-call signatures: **{distinct_calls}**",
            f"- Total tool calls fired: **{total_calls}**",
            f"- Dedup pings emitted: **{total_pings}**",
            "",
            "### Per-tool distribution",
            "",
        ])
        lines.extend(_format_dedup_summary(dedup_counts))
        lines.extend([
            "",
            "## Gate failures",
            "",
        ])
        lines.extend(_format_gate_failures(gate_counts))
        lines.extend([
            "",
            "## Dedup pings",
            "",
        ])
        lines.extend(_format_ping_counts(ping_counts))
        if extra_note:
            lines.extend(["", "## Note", "", str(extra_note)])
        lines.extend([
            "",
            "---",
            "",
            "*This checkpoint is written automatically by the Resume layer "
            "(Phase 1). It exists so a future task can pick up where this "
            "one stopped without re-doing the work captured above. Phase 2 "
            "wires resume-detection to load it on a continuation prompt.*",
            "",
        ])

        body = "\n".join(lines)

        # Atomic write: tmp file + rename to avoid partial reads.
        with tempfile.NamedTemporaryFile(
            "w", dir=str(out_dir), prefix=f".{task_id}.", suffix=".md.tmp",
            delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(body)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_path = pathlib.Path(tmp.name)
        os.replace(tmp_path, out_path)
        log.info("task_checkpoint written: %s (reason=%s)", out_path, reason)
        return out_path
    except Exception:
        log.debug("write_task_checkpoint failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Phase 2 — resume detection
# ---------------------------------------------------------------------------

# Words/phrases in the user's prompt that signal "this is a continuation of
# the prior interrupted task." Case-insensitive substring match. Conservative:
# only triggers on intent-clear phrases so we don't false-positive on prompts
# that happen to contain a continuation word.
_RESUME_SIGNALS = (
    "continue where you left off",
    "pick up where you left off",
    "resume the previous task",
    "resume previous task",
    "continue the previous task",
    "continue previous task",
    "please continue",
    "/resume",
)


def detect_resume_signal(user_text: str) -> bool:
    """Return True if the user's text looks like a continuation request."""
    if not user_text:
        return False
    needle = str(user_text).lower()
    return any(sig in needle for sig in _RESUME_SIGNALS)


def build_resume_hint_block(checkpoint_path: pathlib.Path, *, max_chars: int = 6000) -> str:
    """Read the checkpoint file and produce a compact prepend block for the
    new task's user message. Returns an empty string on failure."""
    try:
        body = checkpoint_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if len(body) > max_chars:
        body = body[:max_chars] + "\n\n[... checkpoint truncated for length]"
    header = (
        "## Resume context (auto-loaded from previous interrupted task)\n\n"
        "*The task below was interrupted by escalation. Below is the "
        "auto-checkpoint from that task. Use it to skip work you already "
        "did — do **not** re-read files listed below unless the new prompt "
        "demands fresh state. Continue from the 'Last thought' if relevant.*\n\n"
        "<checkpoint>\n"
    )
    footer = "\n</checkpoint>\n\n## Current task\n\n"
    return header + body + footer


def latest_checkpoint_for_chat(
    ctx_or_env: Any, *, chat_id: Optional[int], max_age_hours: int = 24,
) -> Optional[pathlib.Path]:
    """Return the most recent checkpoint file matching ``chat_id``,
    if any was written within ``max_age_hours`` hours. Used by Phase 2
    resume-detection to surface continuation context.

    Accepts either a ToolContext (with ``drive_path``) or any object
    exposing ``drive_path(rel)``.
    """
    try:
        drive_path_fn = getattr(ctx_or_env, "drive_path", None)
        if drive_path_fn is None:
            return None
        out_dir = pathlib.Path(drive_path_fn(CHECKPOINT_DIR_RELATIVE))
        if not out_dir.exists():
            return None
        candidates = sorted(
            out_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            return None
        cutoff = time.time() - (max_age_hours * 3600)
        for cand in candidates:
            try:
                if cand.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            if chat_id is None:
                return cand
            try:
                # Match the "Chat: `<id>`" line we emit
                head = cand.read_text(encoding="utf-8", errors="replace")[:2000]
                if f"`{chat_id}`" in head:
                    return cand
            except OSError:
                continue
        return None
    except Exception:
        log.debug("latest_checkpoint_for_chat failed", exc_info=True)
        return None

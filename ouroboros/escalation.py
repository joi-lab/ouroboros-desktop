"""User-facing escalation layer (Phase 8 of the local-LLM compat work).

Phases 3 and 4 of the compat layer detect stuck loops and short-circuit
them with synthetic tool results — but those results go into the agent's
own transcript, not the operator's chat. Result: the agent stops its
internal loop but the operator has no signal it's stuck. Documented
case: Ouroboros spent 1.5 hours alternating between
``advisory_pre_review`` and ``repo_commit`` with fingerprint-mismatch
failures, with Phase 4 firing GATE_RETRY_EXHAUSTED twice — the loop
shrunk from 5 hours to 1.5, but the human still had no live indicator.

This module turns those internal signals into user-facing
``send_message`` events pushed straight onto the chat queue. The UI
surfaces them as real chat messages (``is_escalation: True``) rather
than progress spinners.

Three triggers:

1. **gate_retry_exhausted** — fired by ``loop_tool_execution.py`` when
   Phase 4's gate cap is hit. One escalation message per
   (tool, block_reason) pair.
2. **dedup_threshold** — fired when Phase 3's DEDUP_BLOCKED has been
   triggered ``profile.dedup_ping_threshold`` times for the same tool.
3. **wall_clock_budget** — fired by ``loop.py::run_llm_loop`` when the
   per-task wall-clock budget (``profile.wall_budget_sec``) is
   exceeded. Also signals the loop to break.

All three honor the ``escalation_enabled`` profile flag — cloud agents
keep the legacy "no escalation" behavior. Local profiles default
True. Master kill-switch: ``OUROBOROS_ESCALATION_DISABLED=true``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from ouroboros.task_checkpoint import write_task_checkpoint
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


def _checkpoint_once(
    ctx: Any, *, reason: str, started_ts: Optional[float], rounds: int,
) -> None:
    """Write the auto-snapshot at most once per task. Tracks via a flag
    on ctx so multiple escalations from the same task don't overwrite the
    first (most-informative) checkpoint."""
    if getattr(ctx, "_task_checkpoint_written", False):
        return
    task_id = str(getattr(ctx, "task_id", "") or "")
    if not task_id:
        return
    try:
        write_task_checkpoint(
            ctx,
            task_id=task_id,
            reason=reason,
            started_ts=started_ts or time.time(),
            rounds=rounds,
        )
        ctx._task_checkpoint_written = True
    except Exception:
        log.debug("auto-checkpoint failed", exc_info=True)


def _is_enabled(ctx: Any) -> bool:
    """Master switch. False unless the active compat profile opts in AND
    the operator hasn't disabled escalation via env."""
    if (os.environ.get("OUROBOROS_ESCALATION_DISABLED", "") or "").strip().lower() in (
        "true", "1", "yes",
    ):
        return False
    profile = getattr(ctx, "compat_profile", None)
    if profile is None:
        return False
    return bool(getattr(profile, "escalation_enabled", False))


def _push(ctx: Any, kind: str, headline: str, detail: str) -> bool:
    """Push a user-facing send_message event onto the chat queue.

    Returns True if the event was successfully enqueued (best-effort —
    swallows queue / chat-id errors so an escalation failure can't
    cascade into a worker crash).
    """
    if not _is_enabled(ctx):
        return False
    eq = getattr(ctx, "event_queue", None)
    chat_id = getattr(ctx, "current_chat_id", None)
    if eq is None or chat_id is None:
        return False
    payload = {
        "type": "send_message",
        "chat_id": chat_id,
        "text": f"🚨 **{headline}**\n\n{detail}",
        "format": "markdown",
        "is_escalation": True,
        "escalation_kind": kind,
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "ts": utc_now_iso(),
    }
    try:
        eq.put_nowait(payload)
        return True
    except Exception:
        log.debug("Failed to push escalation %r", kind, exc_info=True)
        return False


def emit_gate_retry(
    ctx: Any,
    *,
    tool_name: str,
    block_reason: str,
    count: int,
    cap: int,
) -> bool:
    """Phase 4 hit ``cap`` failures with the same block_reason. Tell the user
    immediately — don't wait for them to notice the trace went silent."""
    _checkpoint_once(ctx, reason="gate_retry", started_ts=None, rounds=0)
    headline = f"Stuck on `{tool_name}` — gate retry cap hit"
    detail = (
        f"I've called `{tool_name}` {count} times this task and each call "
        f"failed with block_reason `{block_reason}` (cap={cap}).\n\n"
        f"I'm stopping rather than retrying. Likely paths forward:\n"
        f"- Investigate the underlying issue (whatever `{block_reason}` signals).\n"
        f"- Pass `skip_advisory_pre_review=True` if it's an advisory issue.\n"
        f"- Direct me to a different tool or a different angle.\n\n"
        f"What would you like me to do?"
    )
    return _push(ctx, "gate_retry", headline, detail)


def maybe_emit_dedup(ctx: Any, *, tool_name: str) -> bool:
    """Phase 3 fired DEDUP_BLOCKED — increment per-tool dedup counter on
    the context and emit an escalation when the profile threshold is hit
    (only once per task per tool — further dedups stay silent until the
    counter naturally resets at the next task)."""
    if not _is_enabled(ctx):
        return False
    profile = getattr(ctx, "compat_profile", None)
    threshold = int(getattr(profile, "dedup_ping_threshold", 0) or 0) if profile else 0
    if threshold <= 0:
        return False

    counts = getattr(ctx, "_dedup_ping_counts", None)
    if counts is None:
        counts = {}
        try:
            ctx._dedup_ping_counts = counts
        except Exception:
            return False
    counts[tool_name] = counts.get(tool_name, 0) + 1
    if counts[tool_name] != threshold + 1:
        # Either still under threshold or already escalated for this tool
        # this task. Silent.
        return False

    _checkpoint_once(ctx, reason="dedup_threshold", started_ts=None, rounds=0)
    headline = f"Repeating same call to `{tool_name}`"
    detail = (
        f"I've been blocked from calling `{tool_name}` with identical "
        f"arguments {counts[tool_name]} times in this task. The dedup "
        f"guard says I'm in a loop.\n\n"
        f"Can you redirect me — different arguments, a different tool, "
        f"or a different angle?"
    )
    return _push(ctx, "dedup", headline, detail)


def _assess_productivity(ctx: Any, rounds: int) -> dict:
    """Derive productivity signals from state Phase 3 + Phase 8 already track.

    Returns a dict with:
      distinct_calls    — number of distinct (tool, args) signatures fired
      total_calls       — sum of all calls across signatures
      total_pings       — total dedup pings emitted (signals tight loops)
      avg_calls_per_sig — repetition rate
      rounds            — for round-rate calculations
    """
    dedup_counts = getattr(ctx, "_dedup_counts", None) or {}
    ping_counts = getattr(ctx, "_dedup_ping_counts", None) or {}
    distinct = len(dedup_counts)
    total = sum(dedup_counts.values())
    pings = sum(ping_counts.values())
    avg = (total / distinct) if distinct else 0.0
    return {
        "distinct_calls": distinct,
        "total_calls": total,
        "total_pings": pings,
        "avg_calls_per_sig": avg,
        "rounds": rounds,
    }


def _is_productive(signals: dict, base_budget_sec: int) -> tuple:
    """Heuristic productivity classifier. Returns (is_productive, reason).

    Three rejection signals:
      1. dedup pings (≥2): strong evidence of a tight repetition loop.
      2. low diversity: < (1 distinct call per 5 min budget) suggests
         the agent isn't exploring. 30 min budget → expect ≥ 6 distinct.
      3. high repetition: avg > 2.5 calls per signature means same
         actions are firing over and over.

    Conservative AND: a single failure → not productive. False negatives
    (productive task interrupted) are recoverable; false positives (drift
    loop extended) waste compute. Validated against real session traces
    in scripts/simulate_wall_clock_productivity.py.
    """
    if signals["total_pings"] >= 2:
        return False, "dedup_pings"
    expected_distinct = max(3, base_budget_sec // 300)
    if signals["distinct_calls"] < expected_distinct:
        return False, f"low_diversity ({signals['distinct_calls']}/{expected_distinct})"
    if signals["avg_calls_per_sig"] > 2.5:
        return False, f"high_repetition (avg={signals['avg_calls_per_sig']:.1f})"
    return True, "exploration"


def check_wall_clock(
    ctx: Any,
    *,
    task_started_ts: float,
    rounds: int,
) -> bool:
    """Check whether the per-task wall-clock budget has been exceeded.

    Returns True iff an escalation was emitted (caller should also
    break out of the round loop). Returns False when budget is unset,
    not yet exceeded, escalation disabled, OR the budget was extended
    because the task is productively progressing.

    Productivity-aware extension (2026-05-02):
      The budget is no longer a binary timer. When elapsed exceeds the
      base budget, the function inspects Phase 3+8 state for evidence
      of progress (tool diversity, dedup pings, repetition rate). If
      productive, the effective budget is extended by 50% of the base
      budget (up to ``OUROBOROS_WALL_BUDGET_MAX_EXTENSIONS`` times,
      default 2). If unproductive, escalation fires immediately. A hard
      ceiling at base * (1 + 0.5*max_ext) fires regardless of
      productivity to bound pathological breaches.

    Side effects:
      - ``_wall_budget_extensions``  : extension counter on ctx
      - ``_wall_budget_breached``    : marks one-shot escalation per task
    """
    if not _is_enabled(ctx):
        return False
    profile = getattr(ctx, "compat_profile", None)
    if profile is None:
        return False

    env_raw = (os.environ.get("OUROBOROS_WALL_BUDGET_SEC", "") or "").strip()
    budget = 0
    if env_raw:
        try:
            budget = max(0, int(env_raw))
        except (TypeError, ValueError):
            budget = 0
    if budget <= 0:
        budget = int(getattr(profile, "wall_budget_sec", 0) or 0)
    if budget <= 0:
        return False

    if getattr(ctx, "_wall_budget_breached", False):
        return False

    # Extension config (env override > defaults). Setting max_ext=0
    # restores legacy single-budget behavior without touching profile.
    max_ext_raw = (os.environ.get("OUROBOROS_WALL_BUDGET_MAX_EXTENSIONS", "") or "").strip()
    try:
        max_extensions = max(0, int(max_ext_raw)) if max_ext_raw else 2
    except (TypeError, ValueError):
        max_extensions = 2

    extensions = int(getattr(ctx, "_wall_budget_extensions", 0) or 0)
    extension_factor = 0.5

    elapsed = time.time() - task_started_ts
    hard_ceiling = int(budget * (1 + extension_factor * max_extensions))
    effective = int(budget * (1 + extension_factor * extensions))

    # Hard ceiling first — pathological breaches must fire even if the
    # agent looks productive. Otherwise a runaway productive-looking
    # task could run forever.
    if max_extensions > 0 and elapsed >= hard_ceiling:
        try:
            ctx._wall_budget_breached = True
        except Exception:
            pass
        _checkpoint_once(
            ctx, reason="wall_budget_hard_ceiling",
            started_ts=task_started_ts, rounds=rounds,
        )
        headline = f"Task wall-clock hard ceiling reached — {int(elapsed / 60)} min"
        detail = (
            f"This task has been running for ~{int(elapsed / 60)} minutes "
            f"({int(elapsed)} sec) across {rounds} rounds, with "
            f"{extensions} budget extension(s) already granted. The hard "
            f"ceiling for this profile is {int(hard_ceiling / 60)} minutes.\n\n"
            f"Stopping unconditionally to prevent runaway. What would you "
            f"like me to do — continue from a clean state, narrow the scope, "
            f"or hand off?"
        )
        return _push(ctx, "wall_budget_hard_ceiling", headline, detail)

    if elapsed < effective:
        return False

    # At the effective budget — assess productivity and decide.
    signals = _assess_productivity(ctx, rounds)
    productive, reason = _is_productive(signals, budget)

    if productive and extensions < max_extensions:
        # Extend instead of escalate. Log so the operator sees the decision.
        try:
            ctx._wall_budget_extensions = extensions + 1
        except Exception:
            pass
        log.info(
            "wall-clock budget extended (#%d of %d): elapsed=%.0fs effective_was=%ds "
            "signals=distinct=%d/pings=%d/avg=%.2f reason=%s",
            extensions + 1, max_extensions, elapsed, effective,
            signals["distinct_calls"], signals["total_pings"],
            signals["avg_calls_per_sig"], reason,
        )
        return False

    # Not productive (or already at max extensions) — fire escalation.
    try:
        ctx._wall_budget_breached = True
    except Exception:
        pass
    _checkpoint_once(
        ctx, reason="wall_budget",
        started_ts=task_started_ts, rounds=rounds,
    )

    if productive:
        # Reached max extensions while still productive — soft hand-off.
        headline = (
            f"Task wall-clock budget exhausted (max extensions reached) — "
            f"{int(elapsed / 60)} min"
        )
        detail = (
            f"This task has been running for ~{int(elapsed / 60)} minutes "
            f"({int(elapsed)} sec) across {rounds} rounds, with {extensions} "
            f"budget extension(s) granted because progress was steady. The "
            f"final extended budget for this profile is {int(effective / 60)} "
            f"minutes.\n\n"
            f"I'm still progressing but I've hit the maximum extension count. "
            f"Stopping for your input — would you like me to continue with a "
            f"fresh budget, narrow the scope, or hand off?"
        )
    else:
        headline = (
            f"Task wall-clock budget exhausted — {int(elapsed / 60)} min "
            f"(progress signals weak: {reason})"
        )
        detail = (
            f"This task has been running for ~{int(elapsed / 60)} minutes "
            f"({int(elapsed)} sec) across {rounds} rounds. Budget for this "
            f"profile is {int(budget / 60)} minutes (no extension granted "
            f"because the productivity check failed: {reason}; "
            f"distinct_calls={signals['distinct_calls']}, "
            f"dedup_pings={signals['total_pings']}, "
            f"avg_calls_per_sig={signals['avg_calls_per_sig']:.1f}).\n\n"
            f"I haven't reached a clean stopping point. Stopping for your "
            f"input. What would you like me to do — continue, narrow the "
            f"scope, or hand off?"
        )
    return _push(ctx, "wall_budget", headline, detail)

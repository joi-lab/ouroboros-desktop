"""Parallel review orchestration for the pre-commit pipeline.

Extracted from git.py (P7 Minimalism) so the commit path can share one
review orchestration implementation without duplication.

Public API:
  run_parallel_review(ctx, commit_message, *, goal, scope, review_rebuttal)
      -> (review_err, scope_result, triad_block_reason, triad_advisory)
  aggregate_review_verdict(review_err, scope_result, triad_block_reason, triad_advisory,
                           ctx, commit_message, commit_start, repo_dir)
      -> (blocked, combined_msg, block_reason, findings, scope_advisory_items)
  The caller must apply scope_advisory_items to ctx._review_advisory on both the
  blocked and non-blocked paths so advisory findings remain visible regardless of
  whether the commit was blocked.
"""
from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import logging
from typing import Optional

from ouroboros.utils import run_cmd
from ouroboros.tools.review_helpers import format_review_history_entry
from ouroboros.tools.scope_review import run_scope_review, _get_scope_model

log = logging.getLogger(__name__)


# ── Scope-history helpers ────────────────────────────────────────────────────

def _scope_history_entry(scope_result) -> dict:
    """Build a compact scope-history entry from a ScopeReviewResult.

    Preserves the epistemic ``status`` field (e.g. ``parse_failure``,
    ``budget_exceeded``, ``empty``) so that the retry / history path in
    ``_build_scope_history_section`` can distinguish a genuine clean PASS
    from a dropped-findings failure — the core requirement of the
    observability / epistemic-integrity fix (v4.32.0).
    """
    parts = []
    if scope_result.critical_findings:
        parts.append(
            "Critical: " + "; ".join(
                (
                    f"{f['item']} ({f.get('obligation_id')})"
                    if f.get("obligation_id") else f["item"]
                )
                for f in scope_result.critical_findings
            )
        )
    if scope_result.advisory_findings:
        parts.append(
            "Advisory: " + "; ".join(
                (
                    f"{f['item']} ({f.get('obligation_id')})"
                    if f.get("obligation_id") else f["item"]
                )
                for f in scope_result.advisory_findings
            )
        )
    status = getattr(scope_result, "status", None) or "responded"
    # Build summary: for non-responded statuses, lead with the status signal
    # so empty finding lists are not misread as clean PASS on retry.
    if not parts and status not in ("responded",):
        summary = f"({status})"
    else:
        summary = " | ".join(parts) if parts else "(no findings)"
    return {
        "blocked": scope_result.blocked,
        "status": status,
        "summary": summary,
        "critical_findings": scope_result.critical_findings or [],
        "advisory_findings": scope_result.advisory_findings or [],
    }


def _format_scope_advisory_msg(scope_result) -> str:
    """Format advisory scope findings as a readable message (advisory enforcement path)."""
    parts = []
    if scope_result.critical_findings:
        parts.append("Scope advisory findings (enforcement=advisory):\n" +
                     "\n".join(f"  • {f['item']}: {f.get('reason', '')}"
                                for f in scope_result.critical_findings))
    if scope_result.advisory_findings:
        parts.append("Scope advisory notes:\n" +
                     "\n".join(f"  • {f['item']}: {f.get('reason', '')}"
                                for f in scope_result.advisory_findings))
    return "---\n" + "\n".join(parts) if parts else ""
# ── Core parallel orchestration ──────────────────────────────────────────────

def run_parallel_review(ctx, commit_message, *, goal="", scope="", review_rebuttal=""):
    """Run triad review and scope review concurrently.

    Returns (review_err, scope_result, triad_block_reason, triad_advisory).
    Both reviewers always run regardless of each other's outcome.
    Scope review history is keyed to the staged diff hash so findings from a
    prior blocked attempt on a different diff are not shown to the reviewer.
    """
    from ouroboros.tools.review import _run_unified_review

    # Reset forensic fields at the start of each parallel review attempt
    # so stale values from a previous attempt are never persisted on early exit.
    ctx._last_scope_model = ""
    ctx._last_triad_raw_results = []
    ctx._last_scope_raw_result = {}

    try:
        diff_bytes = run_cmd(["git", "diff", "--cached"], cwd=ctx.repo_dir).encode()
    except Exception:
        diff_bytes = b""
    snapshot_key = hashlib.sha256(diff_bytes).hexdigest()[:16]
    _stored = getattr(ctx, '_scope_review_history', None) or {}
    _scope_history = _stored.get(snapshot_key, []) if isinstance(_stored, dict) else []
    _history_snapshot = list(getattr(ctx, '_review_history', []))

    def _run_triad():
        return _run_unified_review(ctx, commit_message, review_rebuttal=review_rebuttal,
                                   goal=goal, scope=scope)

    def _run_scope():
        try:
            ctx._last_scope_model = _get_scope_model()  # forensic: actual resolved model
            return run_scope_review(
                ctx, commit_message, goal=goal, scope=scope,
                review_rebuttal=review_rebuttal,
                review_history=_history_snapshot,
                scope_review_history=_scope_history,
            )
        except Exception as e:
            log.warning("Scope review raised unexpected exception: %s", e)
            from ouroboros.tools.scope_review import ScopeReviewResult
            return ScopeReviewResult(blocked=True, block_message=(
                f"⚠️ SCOPE_REVIEW_BLOCKED: Scope review failed — {e}\nFix the issue and retry."
            ))

    # Snapshot advisory state before launching threads to avoid race with scope thread
    _advisory_snapshot_before = list(getattr(ctx, '_review_advisory', []))
    with _cf.ThreadPoolExecutor(max_workers=2) as pool:
        triad_fut = pool.submit(_run_triad)
        scope_fut = pool.submit(_run_scope)
        try:
            review_err = triad_fut.result()
        except Exception as e:
            log.warning("Triad review raised unexpected exception: %s", e)
            review_err = (
                f"⚠️ REVIEW_BLOCKED: Triad review crashed — {e}\nFix the issue and retry."
            )
            # Reset per-run triad state so stale fields from a prior attempt don't bleed through
            ctx._last_review_block_reason = 'infra_failure'
            ctx._last_review_critical_findings = []
        triad_block_reason = getattr(ctx, '_last_review_block_reason', 'critical_findings')
        # Use post-triad advisory (set by _run_unified_review) minus pre-launch items
        triad_advisory_post = list(getattr(ctx, '_review_advisory', []))
        triad_advisory = [a for a in triad_advisory_post if a not in _advisory_snapshot_before]
        try:
            scope_result = scope_fut.result()
        except Exception as e:
            log.warning("Scope future raised unexpected exception: %s", e)
            from ouroboros.tools.scope_review import ScopeReviewResult
            scope_result = ScopeReviewResult(blocked=True, block_message=(
                f"⚠️ SCOPE_REVIEW_BLOCKED: Scope review future crashed — {e}\nFix the issue and retry."
            ))

    if scope_result is not None:
        updated = _scope_history + [_scope_history_entry(scope_result)]
        # Preserve existing history for other snapshot keys; update only the current key
        existing = getattr(ctx, '_scope_review_history', None) or {}
        if not isinstance(existing, dict):
            existing = {}
        existing[snapshot_key] = updated
        ctx._scope_review_history = existing
        # Store canonical scope actor record for durable persistence in CommitAttemptRecord
        ctx._last_scope_raw_result = {
            "model_id": getattr(scope_result, "model_id", "") or getattr(ctx, "_last_scope_model", ""),
            "status": getattr(scope_result, "status", "responded"),
            "raw_text": getattr(scope_result, "raw_text", ""),
            "prompt_chars": getattr(scope_result, "prompt_chars", 0),
            "tokens_in": getattr(scope_result, "tokens_in", 0),
            "tokens_out": getattr(scope_result, "tokens_out", 0),
            "cost_usd": getattr(scope_result, "cost_usd", 0.0),
            # parsed_items: same field name as triad actor records; scope has one reviewer
            # so this holds all structured findings from the scope model (or [] on skip/error)
            "parsed_items": list(
                (scope_result.critical_findings or []) + (scope_result.advisory_findings or [])
            ),
            "critical_findings": list(scope_result.critical_findings or []),
            "advisory_findings": list(scope_result.advisory_findings or []),
        }
    else:
        ctx._last_scope_raw_result = {}

    return review_err, scope_result, triad_block_reason, triad_advisory


def aggregate_review_verdict(review_err, scope_result, triad_block_reason, triad_advisory,
                              ctx, commit_message, commit_start, repo_dir):
    """Aggregate triad + scope results.

    Returns (blocked, combined_msg, block_reason, findings, scope_advisory_items).
    - (False, None, '', [], items) when both reviewers passed — caller should surface scope_advisory_items.
    - (True, msg, reason, findings, items) when blocked.

    scope_advisory_items is a list of structured advisory entries for ctx._review_advisory,
    so non-blocking scope findings stay visible on the main thread.
    """
    _combined_blocked = False
    _combined_messages = []
    _combined_findings = []
    _scope_advisory_items = []

    # Build scope advisory items for ctx surfacing (regardless of blocked/not)
    if scope_result is not None:
        for f in (scope_result.critical_findings or []):
            item = {
                "severity": "critical",
                "tag": "scope",
                "item": str(f.get("item", "") or ""),
                "reason": str(f.get("reason", "") or ""),
                "verdict": "FAIL",
            }
            if f.get("obligation_id"):
                item["obligation_id"] = str(f.get("obligation_id"))
            _scope_advisory_items.append(item)
        for f in (scope_result.advisory_findings or []):
            item = {
                "severity": "advisory",
                "tag": "scope",
                "item": str(f.get("item", "") or ""),
                "reason": str(f.get("reason", "") or ""),
                "verdict": "FAIL",
            }
            if f.get("obligation_id"):
                item["obligation_id"] = str(f.get("obligation_id"))
            _scope_advisory_items.append(item)

    if review_err:
        _combined_blocked = True
        _combined_messages.append(review_err)
        _combined_findings.extend(getattr(ctx, '_last_review_critical_findings', []))
    if scope_result is not None:
        if scope_result.blocked:
            _combined_blocked = True
            _combined_messages.append(scope_result.block_message)
            # Only add to durable blocking findings when scope actually blocked
            _combined_findings.extend(scope_result.critical_findings or [])
        elif scope_result.advisory_findings or scope_result.critical_findings:
            _advisory_msg = _format_scope_advisory_msg(scope_result)
            if _advisory_msg and _combined_blocked:
                _combined_messages.append(_advisory_msg)

    if not _combined_blocked:
        return False, None, '', _combined_findings, _scope_advisory_items

    if review_err and (scope_result is None or not scope_result.blocked):
        block_reason = triad_block_reason
    elif scope_result is not None and scope_result.blocked and not review_err:
        block_reason = "scope_blocked"
    else:
        block_reason = triad_block_reason

    if len(_combined_messages) > 1:
        combined_msg = "\n\n".join(_combined_messages)
        if review_err and scope_result is not None and scope_result.blocked:
            combined_msg += "\n\n---\n⚠️ Note: Both triad review AND scope review found issues (shown above)."
    else:
        combined_msg = _combined_messages[0]

    if triad_advisory and not review_err:
        adv_text = "\n".join(
            f"  ⚠️ Advisory: {format_review_history_entry(a)}"
            for a in triad_advisory
        )
        combined_msg += f"\n\n---\nTriad advisory findings:\n{adv_text}"

    return True, combined_msg, block_reason, _combined_findings, _scope_advisory_items

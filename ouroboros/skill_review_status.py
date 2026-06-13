"""Skill review verdict aggregation.

This module is deliberately tiny so both ``skill_review`` and
``skill_loader`` can share the same live status calculation without an
import cycle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

STATUS_CLEAN = "clean"
STATUS_WARNINGS = "warnings"
STATUS_BLOCKERS = "blockers"
STATUS_PENDING = "pending"

_LEGACY_STATUS_ALIASES = {
    "pass": STATUS_CLEAN,
    "advisory_pass": STATUS_WARNINGS,
    "advisory": STATUS_WARNINGS,
    "fail": STATUS_BLOCKERS,
    "pending": STATUS_PENDING,
    "pending_phase4": STATUS_PENDING,
}

VALID_SKILL_REVIEW_STATUSES = frozenset({
    STATUS_CLEAN,
    STATUS_WARNINGS,
    STATUS_BLOCKERS,
    STATUS_PENDING,
    *_LEGACY_STATUS_ALIASES.keys(),
})


HARD_CRITICAL_ITEMS = frozenset({
    "manifest_schema",
    "skill_preflight",
    "permissions_honesty",
    "no_repo_mutation",
    "path_confinement",
    "env_allowlist",
    "inject_chat_minimization",
    "event_subscription_minimization",
    "host_token_handling",
})

SEVERITY_DRIVEN_ITEMS = frozenset({
    "bug_hunting",
    "companion_process_safety",
    "extension_namespace_discipline",
    "widget_module_safety",
})

# Backward-compatible export name for older imports.  New aggregation logic
# distinguishes hard trust-boundary blockers from severity-driven items below.
CRITICAL_ITEMS = HARD_CRITICAL_ITEMS | SEVERITY_DRIVEN_ITEMS


def _severity_blocks(finding: Dict[str, Any]) -> bool:
    severity = str(finding.get("severity") or "").strip().lower()
    if severity in {"advisory", "warning", "warn"}:
        return False
    # Missing/unknown severity stays conservative for legacy persisted findings.
    return True


def aggregate_skill_review_status(
    findings: List[Dict[str, Any]],
    skill_type: str,
    *,
    is_module_widget: bool = False,
    enforcement: Optional[str] = None,
) -> str:
    """Collapse per-reviewer findings into an enforcement-independent verdict."""
    has_critical_fail = False
    has_warning_fail = False
    is_extension = skill_type == "extension"
    for finding in findings:
        verdict = finding.get("verdict") == "FAIL"
        if not verdict:
            continue
        item = finding.get("item")
        item_is_critical = item in HARD_CRITICAL_ITEMS
        if item in SEVERITY_DRIVEN_ITEMS:
            if item in {"extension_namespace_discipline", "widget_module_safety"} and not is_extension:
                item_is_critical = False
            else:
                item_is_critical = _severity_blocks(finding)
        if item_is_critical:
            has_critical_fail = True
        else:
            has_warning_fail = True
    if has_critical_fail:
        return STATUS_BLOCKERS
    if has_warning_fail:
        return STATUS_WARNINGS
    return STATUS_CLEAN


def normalize_skill_review_status(status: str) -> str:
    """Return the canonical skill review status for current code."""
    raw_status = str(status or "").strip().lower()
    return _LEGACY_STATUS_ALIASES.get(raw_status, raw_status if raw_status in {
        STATUS_CLEAN,
        STATUS_WARNINGS,
        STATUS_BLOCKERS,
        STATUS_PENDING,
    } else STATUS_PENDING)


def skill_review_gate(status: str, *, stale: bool = False, enforcement: Optional[str] = None) -> Dict[str, Any]:
    """Structured, agent-facing explanation of whether a review is executable."""
    raw_status = normalize_skill_review_status(status)
    if enforcement is None:
        try:
            from ouroboros.config import get_review_enforcement
            enforcement = get_review_enforcement()
        except Exception:
            enforcement = "blocking"
    enforcement = str(enforcement or "blocking").lower()
    if raw_status == STATUS_PENDING:
        executable = False
        reason = "review_pending"
        summary = "Review is pending or did not produce an executable verdict."
    elif stale:
        executable = False
        reason = "review_stale"
        summary = "Review is stale for the current skill content; re-run skill_review."
    elif raw_status == STATUS_CLEAN:
        executable = True
        reason = "ready"
        summary = "Review is executable: verdict clean."
    elif raw_status == STATUS_WARNINGS:
        executable = True
        reason = "warnings_do_not_block_execution"
        summary = "Review is executable: warning findings are advisory and do not block execution."
    elif raw_status == STATUS_BLOCKERS:
        if enforcement == "advisory":
            executable = True
            reason = "blockers_allowed_by_advisory_enforcement"
            summary = "Review is executable because advisory enforcement allows blocker findings by operator choice."
        else:
            executable = False
            reason = "blocker_findings_under_blocking_enforcement"
            summary = "Review is blocked: blocker findings must be fixed or review enforcement must be advisory."
    else:
        executable = False
        reason = "review_missing_or_unknown"
        summary = "Review status is missing or unknown; run skill_review."
    return {
        "status": raw_status or STATUS_PENDING,
        "stale": bool(stale),
        "executable_review": bool(executable),
        "blocking_reason": reason,
        "review_enforcement": enforcement,
        "summary": summary,
    }

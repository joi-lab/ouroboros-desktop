"""Typed task/loop outcome helpers.

Lifecycle status remains backward-compatible (`completed`, `failed`, ...).
Semantic result status lives beside it so provider/tool/artifact failures do
not masquerade as successful final text.
"""

from __future__ import annotations

import json
import pathlib
from hashlib import sha256
from typing import Any, Dict, List

from ouroboros.headless import (
    ARTIFACT_STATUS_FAILED,
    ARTIFACT_STATUS_FINALIZING,
    ARTIFACT_STATUS_PENDING,
    ARTIFACT_STATUS_READY,
)
from ouroboros.task_results import validate_task_id
from ouroboros.utils import atomic_write_json, utc_now_iso


RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"
RESULT_INFRA_FAILED = "infra_failed"
RESULT_PARTIAL = "partial"

REASON_FINAL_MESSAGE = "final_message"
REASON_EMPTY_FINAL_TEXT = "empty_final_text"
REASON_PROVIDER_FAILURE = "provider_failure"
REASON_ARTIFACT_FAILED = "artifact_failed"
REASON_ARTIFACT_PENDING = "artifact_pending"
REASON_TASK_EXCEPTION = "task_exception"
REASON_DEEP_SELF_REVIEW_UNAVAILABLE = "deep_self_review_unavailable"
REASON_DEEP_SELF_REVIEW_ERROR = "deep_self_review_error"
REASON_TOOL_FAILURE = "tool_failure"

_BLOCKING_TOOL_STATUSES = frozenset({
    "artifact_output_error",
    "blocked",
    "claude_code_error",
    "cwd_blocked",
    "data_blocked",
    "edit_text_blocked",
    "elevation_blocked",
    "error",
    "git_via_shell_blocked",
    "heal_mode_blocked",
    "install_error",
    "light_mode_blocked",
    "non_zero_exit",
    "protected_blocked",
    "run_script_blocked",
    "safety_violation",
    "shell_error",
    "skill_payload_blocked",
    "skill_payload_control_blocked",
    "skill_state_blocked",
    "timeout",
    "unavailable",
    "violation",
    "workspace_blocked",
    "write_file_blocked",
    "root_required_user_files",
})
_RECOVERY_TOOL_NAMES = frozenset({
    "claude_code_edit",
    "edit_text",
    "run_command",
    "run_script",
    "start_service",
    "stop_service",
    "write_file",
})

# Tools/roots whose successful use means the turn produced reviewable work.
# Root-aware write tools: these take a `root` arg, so the scratch-exclusion rule
# applies directly. claude_code_edit uses `cwd` (not `root`) and resolves its own
# work dir, so it is NOT root-checked here; its deliverables surface via the
# artifact_registered flag (declared outputs), and workspace/headless claude_code_edit
# is review-eligible anyway because such tasks are not direct chat.
_ROOT_WRITE_TOOLS = frozenset({"write_file", "edit_text"})
_EFFECT_COMMIT_TOOLS = frozenset({"commit_reviewed", "vcs_commit_reviewed"})
# Exclusion model: only pure scratch is exempt. Every other root is a real surface
# (deliverable, workspace, repo, skill payload, or a light-mode skill write via
# runtime_data). Excluding by scratch — not enumerating "deliverable" roots —
# keeps the immune gate complete as roots evolve and errs toward reviewing work.
_SCRATCH_ROOTS = frozenset({"task_drive"})
_OK_TOOL_STATUSES = frozenset({"", "ok", "ok_autocorrected"})
# Process/service tools that produce a registered deliverable when given outputs=[...].
_EFFECT_PROCESS_TOOLS = frozenset({"run_command", "run_script", "start_service"})
# Substantial coding tool (cwd-based, no root arg): any successful run is real
# work. Over-counting a rare scratch edit is the safe direction for an immune
# gate; under-counting a real repo/deliverable edit (no outputs=[...]) is not.
_EFFECT_CODING_TOOLS = frozenset({"claude_code_edit"})


def turn_has_reviewable_effects(llm_trace: Dict[str, Any]) -> bool:
    """True if the turn produced real reviewable work, from a structured trace read.

    Reviewable effects are a successful repo commit; a successful write_file/
    edit_text to any non-scratch root; any successful claude_code_edit (a
    substantial coding tool that uses cwd, not root); a successful
    run_command/run_script/start_service that declared deliverable outputs; or any
    successful tool that registered a canonical artifact (artifact_registered — a
    stopped service's outputs or a user_files write). Pure scratch (root=task_drive)
    write_file/edit_text does NOT count. Cognitive-memory updates go through
    update_identity/update_scratchpad/knowledge_write (not write tools) and are
    intentionally not effects; a light-mode generic cognitive write is
    advisory-redirected and never succeeds here. This is a P3 deterministic immune
    signal over observable runtime facts, never message-content inspection.
    """
    for call in llm_trace.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("is_error"):
            continue
        if str(call.get("status") or "ok") not in _OK_TOOL_STATUSES:
            continue
        tool = str(call.get("tool") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        if tool in _EFFECT_COMMIT_TOOLS or tool in _EFFECT_CODING_TOOLS:
            return True
        if tool in _ROOT_WRITE_TOOLS and str(args.get("root") or "active_workspace") not in _SCRATCH_ROOTS:
            return True
        if tool in _EFFECT_PROCESS_TOOLS:
            outputs = args.get("outputs")
            if isinstance(outputs, list) and any(str(item or "").strip() for item in outputs):
                return True
        # Structured flag set from the full (untruncated) tool result at capture time;
        # covers stopped-service outputs and user_files writes regardless of preview length.
        if call.get("artifact_registered"):
            return True
    return False


def _user_file_basenames(args: Dict[str, Any]) -> set[str]:
    """Lowercased file basenames declared in a write call's ``path`` and ``files[]``."""
    candidates = [args.get("path")]
    candidates.extend(
        (entry or {}).get("path") for entry in (args.get("files") or []) if isinstance(entry, dict)
    )
    return {
        pathlib.PurePath(str(candidate or "")).name.lower()
        for candidate in candidates
        if str(candidate or "").strip()
    }


def _unresolved_tool_errors(llm_trace: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls = [item for item in (llm_trace.get("tool_calls") or []) if isinstance(item, dict)]
    unresolved: List[Dict[str, Any]] = []
    for idx, item in enumerate(calls):
        if not item.get("is_error"):
            continue
        tool = str(item.get("tool") or "unknown")
        status = str(item.get("status") or "error")
        # COGNITIVE_TOOL_REQUIRED is an advisory redirect, not a task failure: the
        # agent is told to use update_identity/update_scratchpad/knowledge_write, but
        # a self-initiated cognitive write through the wrong tool must never fail the
        # task (that was the original "Привет fails" regression). Skip it entirely.
        if status == "cognitive_tool_required":
            continue
        if status not in _BLOCKING_TOOL_STATUSES and tool not in _RECOVERY_TOOL_NAMES:
            continue
        # ROOT_REQUIRED_USER_FILES is a real user deliverable. It is recovered ONLY
        # when every blocked file name (path or files[]) is later written via
        # root=user_files. This branch is terminal: it never falls through to the
        # generic same-target/artifact_registered recovery, which could otherwise
        # clear it through a non-user_files write (e.g. a run_command output).
        if status == "root_required_user_files":
            blocked_args = item.get("args") if isinstance(item.get("args"), dict) else {}
            blocked_names = _user_file_basenames(blocked_args)
            recovered_names: set[str] = set()
            for later in calls[idx + 1:]:
                if not (isinstance(later, dict) and not later.get("is_error")):
                    continue
                later_args = later.get("args") if isinstance(later.get("args"), dict) else {}
                if (
                    str(later.get("tool") or "") in _ROOT_WRITE_TOOLS
                    and str(later_args.get("root") or "") == "user_files"
                    and str(later.get("status") or "ok") in _OK_TOOL_STATUSES
                ):
                    recovered_names |= _user_file_basenames(later_args)
            if not (blocked_names and blocked_names <= recovered_names):
                unresolved.append({
                    "tool": tool,
                    "status": status,
                    "exit_code": item.get("exit_code"),
                    "signal": item.get("signal"),
                    "result": str(item.get("result") or "")[:500],
                })
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        target_parts = []
        target_paths = set()
        for key in ("root", "path", "cwd", "cmd", "script", "name", "outputs"):
            if key not in args:
                continue
            value = args.get(key)
            target_parts.append((key, value))
            if key in {"path", "cwd"} and value:
                target_paths.add(str(value))
            if key == "outputs" and isinstance(value, list):
                target_paths.update(str(part) for part in value if str(part or "").strip())
        target_key = json.dumps(target_parts, sort_keys=True, default=str)
        recovered = False
        for later in calls[idx + 1:]:
            if later.get("is_error"):
                continue
            later_tool = str(later.get("tool") or "")
            later_status = str(later.get("status") or "ok")
            later_result = str(later.get("result") or "")
            if later_status not in {"", "ok", "ok_autocorrected"}:
                continue
            later_args = later.get("args") if isinstance(later.get("args"), dict) else {}
            later_parts = []
            later_paths = set()
            for key in ("root", "path", "cwd", "cmd", "script", "name", "outputs"):
                if key not in later_args:
                    continue
                value = later_args.get(key)
                later_parts.append((key, value))
                if key in {"path", "cwd"} and value:
                    later_paths.add(str(value))
                if key == "outputs" and isinstance(value, list):
                    later_paths.update(str(part) for part in value if str(part or "").strip())
            same_target = later_tool == tool and target_key == json.dumps(later_parts, sort_keys=True, default=str)
            same_path = bool(target_paths and later_paths and target_paths.intersection(later_paths))
            artifact_registered = "ARTIFACT_OUTPUTS" in later_result or "registered output" in later_result
            if status == "artifact_output_error":
                recovered = artifact_registered and (same_path or not target_paths)
            else:
                recovered = same_target or (artifact_registered and same_path)
            if recovered:
                break
        if recovered:
            continue
        unresolved.append({
            "tool": tool,
            "status": status,
            "exit_code": item.get("exit_code"),
            "signal": item.get("signal"),
            "result": str(item.get("result") or "")[:500],
        })
    return unresolved


def derive_loop_outcome(final_text: str, usage: Dict[str, Any], llm_trace: Dict[str, Any]) -> Dict[str, Any]:
    """Return a typed LoopOutcome-compatible dict."""

    usage_status = str(usage.get("result_status") or "").strip()
    usage_reason = str(usage.get("reason_code") or "").strip()
    text = str(final_text or "")
    failure: Dict[str, Any] | None = None
    result_status = RESULT_SUCCEEDED
    reason_code = REASON_FINAL_MESSAGE
    tool_errors = _unresolved_tool_errors(llm_trace)
    verification_failures: List[Dict[str, Any]] = []
    for event in llm_trace.get("verification_events") or []:
        if not isinstance(event, dict):
            continue
        for service in event.get("services") or []:
            if not isinstance(service, dict):
                continue
            artifact_text = str(service.get("artifact_outputs") or "")
            if bool(service.get("artifact_output_failed")) or artifact_text.startswith("⚠️ ARTIFACT_OUTPUT_ERROR"):
                verification_failures.append({
                    "kind": str(event.get("kind") or "runtime_event"),
                    "service": service.get("name"),
                    "status": "artifact_output_error",
                    "reason": artifact_text[:500],
                })

    if usage_status == RESULT_INFRA_FAILED:
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_PROVIDER_FAILURE
        failure = {"kind": "provider", "reason_code": reason_code}
    elif usage_status == RESULT_FAILED:
        result_status = RESULT_FAILED
        reason_code = usage_reason or REASON_EMPTY_FINAL_TEXT
        failure = {"kind": "agent", "reason_code": reason_code}
    elif not text.strip():
        result_status = RESULT_FAILED
        reason_code = REASON_EMPTY_FINAL_TEXT
        failure = {"kind": "agent", "reason_code": reason_code}
    elif text.lstrip().startswith("⚠️ Failed to get a response") or text.lstrip().startswith("⚠️ All models are down"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_PROVIDER_FAILURE
        failure = {"kind": "provider", "reason_code": reason_code}
    elif text.lstrip().startswith("⚠️ Error during processing:"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_TASK_EXCEPTION
        failure = {"kind": "runtime", "reason_code": reason_code}
    elif text.lstrip().startswith("❌ Deep self-review unavailable:"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_DEEP_SELF_REVIEW_UNAVAILABLE
        failure = {"kind": "runtime", "reason_code": reason_code}
    elif text.lstrip().startswith("⚠️ Deep self-review error:") or text.lstrip().startswith("❌ Deep self-review failed:"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_DEEP_SELF_REVIEW_ERROR
        failure = {"kind": "runtime", "reason_code": reason_code}
    elif verification_failures:
        result_status = RESULT_FAILED
        reason_code = usage_reason or REASON_TOOL_FAILURE
        failure = {
            "kind": "verification",
            "reason_code": reason_code,
            "verification_failures": verification_failures[:20],
        }
    elif tool_errors:
        result_status = RESULT_FAILED
        reason_code = usage_reason or REASON_TOOL_FAILURE
        failure = {
            "kind": "tool",
            "reason_code": reason_code,
            "tool_errors": tool_errors[:20],
        }

    review_decision = llm_trace.get("review_decision") if isinstance(llm_trace.get("review_decision"), dict) else {}
    return {
        "schema_version": 2,
        "result_status": result_status,
        "review_eligibility": str(review_decision.get("eligibility") or "not_eligible"),
        "review_trigger": str(review_decision.get("trigger") or "not_evaluated"),
        "finish_reason": reason_code,
        "reason_code": reason_code,
        "final_text": text,
        "failure": failure,
        "usage": {
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_rounds": int(usage.get("rounds") or 0),
        },
        "trace_refs": collect_trace_refs(usage, llm_trace),
    }


def collect_trace_refs(usage: Dict[str, Any], llm_trace: Dict[str, Any]) -> Dict[str, Any]:
    refs: Dict[str, Any] = {}
    execution_id = str(usage.get("execution_id") or "").strip()
    if execution_id:
        refs["execution_id"] = execution_id
    llm_refs = []
    for item in usage.get("llm_call_refs") or []:
        if not isinstance(item, dict):
            continue
        llm_refs.append({
            "llm_call_id": item.get("llm_call_id"),
            "execution_id": item.get("execution_id"),
            "round_id": item.get("round_id"),
            "round": item.get("round"),
            "request_ref": item.get("request_ref"),
            "response_ref": item.get("response_ref"),
            "model": item.get("model"),
            "resolved_model": item.get("resolved_model"),
            "provider": item.get("provider"),
        })
    if llm_refs:
        refs["llm_call_refs"] = llm_refs
    tool_refs = []
    for item in llm_trace.get("tool_calls") or []:
        if isinstance(item, dict) and item.get("trace_ref"):
            trace = item.get("trace_ref") if isinstance(item.get("trace_ref"), dict) else {}
            tool_refs.append({
                "call_id": trace.get("call_id"),
                "manifest_ref": trace.get("manifest_ref"),
                "redacted_projection_ref": trace.get("redacted_projection_ref"),
                "redaction": trace.get("redaction"),
            })
    if tool_refs:
        refs["tool_call_refs"] = tool_refs
    return refs


def artifact_bundle_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return v2 ArtifactBundle while preserving old artifact fields."""

    artifacts = list(result.get("artifacts") or []) if isinstance(result.get("artifacts"), list) else []
    old_status = str(result.get("artifact_status") or "").strip()
    if old_status in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING, ARTIFACT_STATUS_READY, ARTIFACT_STATUS_FAILED}:
        status = old_status
    elif artifacts:
        status = ARTIFACT_STATUS_READY
    else:
        status = "not_applicable"
    records: List[Dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        explicit_status = str(item.get("status") or "").strip()
        if explicit_status:
            artifact_status = explicit_status
        elif path and pathlib.Path(path).exists():
            artifact_status = ARTIFACT_STATUS_READY
        elif status in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
            artifact_status = status
        else:
            artifact_status = ARTIFACT_STATUS_READY
        record = {
            "kind": str(item.get("kind") or ""),
            "name": str(item.get("name") or pathlib.Path(path).name),
            "path": path,
            "size": int(item.get("size") or 0),
            "sha256": str(item.get("sha256") or ""),
            "status": artifact_status,
            "errors": list(item.get("errors") or []) if isinstance(item.get("errors"), list) else [],
        }
        records.append(record)
    errors = []
    if result.get("artifact_error"):
        errors.append(str(result.get("artifact_error")))
    return {
        "schema_version": 1,
        "status": status,
        "artifacts": records,
        "errors": errors,
    }


def refresh_verification_ledger_artifacts(
    ledger: Dict[str, Any] | None,
    artifact_bundle: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Return ``ledger`` with artifact status synchronized after finalization."""

    if not isinstance(ledger, dict):
        return ledger
    entries = [
        item for item in (ledger.get("entries") or [])
        if not (isinstance(item, dict) and item.get("kind") == "artifact_bundle")
    ]
    artifact_status = str((artifact_bundle or {}).get("status") or "")
    if artifact_status in {ARTIFACT_STATUS_FAILED, ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
        entries.append({
            "kind": "artifact_bundle",
            "status": artifact_status,
            "errors": (artifact_bundle or {}).get("errors") or [],
        })
    updated = dict(ledger)
    updated["entries"] = entries
    updated["summary"] = {
        "entry_count": len(entries),
        "has_failures": any(str(item.get("status") or "").lower() not in {"", "ok", RESULT_SUCCEEDED} for item in entries if isinstance(item, dict)),
    }
    return updated


def build_verification_ledger(
    *,
    task: Dict[str, Any],
    loop_outcome: Dict[str, Any],
    llm_trace: Dict[str, Any],
    artifact_bundle: Dict[str, Any],
    review_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a task-scoped verification ledger from authoritative runtime facts."""

    entries: List[Dict[str, Any]] = []
    if loop_outcome.get("result_status") != RESULT_SUCCEEDED:
        entries.append({
            "kind": "loop_outcome",
            "status": loop_outcome.get("result_status"),
            "reason_code": loop_outcome.get("reason_code"),
        })

    for idx, call in enumerate(llm_trace.get("tool_calls") or [], start=1):
        if not isinstance(call, dict):
            continue
        status = str(call.get("status") or ("error" if call.get("is_error") else "ok"))
        if call.get("is_error") or status not in {"ok", ""}:
            entries.append({
                "kind": "tool_call",
                "index": idx,
                "tool": call.get("tool"),
                "status": status,
                "exit_code": call.get("exit_code"),
                "signal": call.get("signal"),
                "trace_ref": call.get("trace_ref"),
            })

    for event in llm_trace.get("verification_events") or []:
        if isinstance(event, dict):
            entries.append({"kind": "runtime_event", **event})

    for run in llm_trace.get("review_runs") or []:
        if isinstance(run, dict):
            failed = run.get("aggregate_signal") in {"FAIL", "DEGRADED"} or bool(run.get("degraded"))
            entries.append({
                "kind": "task_acceptance_review",
                "status": "failed" if failed else "ok",
                "aggregate_signal": run.get("aggregate_signal"),
                "degraded": run.get("degraded"),
                "finding_count": len(run.get("parsed_findings") or []),
            })

    artifact_status = str(artifact_bundle.get("status") or "")
    if artifact_status in {ARTIFACT_STATUS_FAILED, ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
        entries.append({
            "kind": "artifact_bundle",
            "status": artifact_status,
            "errors": artifact_bundle.get("errors") or [],
        })

    review = review_evidence or {}
    for key in ("critical_findings", "advisory_findings", "open_obligations"):
        items = review.get(key)
        if isinstance(items, list) and items:
            status = "failed" if key in {"critical_findings", "open_obligations"} else "partial"
            entries.append({
                "kind": "review",
                "category": key,
                "status": status,
                "count": len(items),
                "items": items[:10],
                "omitted": max(0, len(items) - 10),
            })

    return {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "task_id": str(task.get("id") or task.get("task_id") or ""),
        "entries": entries,
        "summary": {
            "entry_count": len(entries),
            "has_failures": any(str(item.get("status") or "").lower() not in {"", "ok", RESULT_SUCCEEDED} for item in entries),
        },
    }


def maybe_write_verification_artifact(
    drive_root: pathlib.Path,
    task_id: str,
    ledger: Dict[str, Any],
    *,
    threshold_chars: int = 12_000,
) -> Dict[str, Any]:
    """Inline small ledgers; write large ledgers as task artifacts."""

    raw = json.dumps(ledger, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw) <= threshold_chars:
        return {"inline": ledger, "artifact": None}
    safe_task = validate_task_id(task_id)
    artifact_dir = pathlib.Path(drive_root) / "task_results" / "artifacts" / safe_task
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "verification_ledger.json"
    atomic_write_json(path, ledger, trailing_newline=True)
    data = path.read_bytes()
    return {
        "inline": {
            "schema_version": 1,
            "created_at": ledger.get("created_at"),
            "task_id": ledger.get("task_id"),
            "summary": ledger.get("summary") or {},
            "omitted_to_artifact": True,
        },
        "artifact": {
            "kind": "verification_ledger",
            "name": "verification_ledger.json",
            "path": str(path),
            "size": len(data),
            "sha256": sha256(data).hexdigest(),
            "status": ARTIFACT_STATUS_READY,
            "errors": [],
        },
    }

"""Effective task status helpers shared by tools and gateways."""

from __future__ import annotations

import json
import pathlib
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List

from ouroboros.headless import (
    ARTIFACT_STATUS_FAILED,
    ARTIFACT_STATUS_FINALIZING,
    ARTIFACT_STATUS_PENDING,
    ARTIFACT_STATUS_READY,
)
from ouroboros.task_results import (
    STATUS_CANCEL_REQUESTED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_REQUESTED,
    STATUS_RUNNING,
    STATUS_SCHEDULED,
    list_task_results,
    load_task_result,
    validate_task_id,
)
from ouroboros.utils import iter_jsonl_objects, read_json_dict


FINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_REJECTED_DUPLICATE,
    # Cancel-intent latch: the parent should treat the child as terminal as soon
    # as cancellation is requested so the handoff-reminder loop stops; the
    # supervisor finalizes it to STATUS_CANCELLED shortly after.
    STATUS_CANCEL_REQUESTED,
})
NONTERMINAL_STATUSES: frozenset[str] = frozenset({
    STATUS_REQUESTED,
    STATUS_SCHEDULED,
    STATUS_RUNNING,
})
ARTIFACT_TERMINAL_STATUSES: frozenset[str] = frozenset({
    ARTIFACT_STATUS_READY,
    ARTIFACT_STATUS_FAILED,
})
ARTIFACT_NONTERMINAL_STATUSES: frozenset[str] = frozenset({
    ARTIFACT_STATUS_PENDING,
    ARTIFACT_STATUS_FINALIZING,
})
HANDOFF_SNIPPET_CHARS = 240
_TERMINAL_FAILURE_RESULT_STATUSES = frozenset({"failed", "infra_failed", "cancelled"})
_ORPHAN_RUNNING_GRACE_SECONDS = 30.0
_ARTIFACT_LIFECYCLE_FIELDS: frozenset[str] = frozenset({
    "artifact_status",
    "artifact_error",
    "artifact_bundle",
    "artifact_finalized_at",
})


def _fail_nonterminal_artifact_bundle(bundle: Dict[str, Any], message: str) -> Dict[str, Any]:
    updated = dict(bundle or {})
    updated["status"] = ARTIFACT_STATUS_FAILED
    errors = list(updated.get("errors") or []) if isinstance(updated.get("errors"), list) else []
    if message not in errors:
        errors.append(message)
    updated["errors"] = errors
    artifacts = updated.get("artifacts")
    if isinstance(artifacts, list):
        patched_artifacts = []
        for artifact in artifacts:
            if isinstance(artifact, dict):
                item = dict(artifact)
                if str(item.get("status") or "").strip().lower() in ARTIFACT_NONTERMINAL_STATUSES:
                    item["status"] = ARTIFACT_STATUS_FAILED
                    item_errors = list(item.get("errors") or []) if isinstance(item.get("errors"), list) else []
                    if message not in item_errors:
                        item_errors.append(message)
                    item["errors"] = item_errors
                patched_artifacts.append(item)
            else:
                patched_artifacts.append(artifact)
        updated["artifacts"] = patched_artifacts
    return updated


def _child_drive_candidates(result: Dict[str, Any]) -> List[pathlib.Path]:
    paths: List[pathlib.Path] = []
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    for source in (result, metadata):
        for key in ("child_drive_root", "headless_child_drive_root", "drive_root"):
            text = str(source.get(key) or "").strip()
            if not text:
                continue
            path = pathlib.Path(text)
            if path not in paths:
                paths.append(path)
    return paths


def _load_queue_snapshot(drive_root: pathlib.Path) -> Dict[str, Any]:
    path = pathlib.Path(drive_root) / "state" / "queue_snapshot.json"
    if not path.exists():
        return {"_snapshot_missing": True}
    data = read_json_dict(path)
    if not isinstance(data, dict):
        return {"_snapshot_invalid": True}
    return data


def _queue_task_status(snapshot: Dict[str, Any], task_id: str) -> tuple[str, Dict[str, Any]]:
    if snapshot.get("_snapshot_missing") or snapshot.get("_snapshot_invalid"):
        return "unknown", {}
    for row in snapshot.get("running") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or row.get("task_id") or "") == task_id:
            task = row.get("task") if isinstance(row.get("task"), dict) else {}
            return STATUS_RUNNING, task
    for row in snapshot.get("pending") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("id") or row.get("task_id") or "") == task_id:
            task = row.get("task") if isinstance(row.get("task"), dict) else {}
            return STATUS_SCHEDULED, task
    return "", {}


def _is_stale_orphan_running_task(drive_root: pathlib.Path, task_id: str, result: Dict[str, Any]) -> bool:
    status = str(result.get("status") or "").lower()
    if status != STATUS_RUNNING:
        return False
    result_status = str(result.get("result_status") or "").strip().lower()
    if result_status:
        return False
    heartbeat = 0.0
    try:
        parsed = datetime.fromisoformat(str(result.get("ts") or result.get("started_at") or result.get("created_at") or "").strip().replace("Z", "+00:00"))
        heartbeat = float((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())
    except Exception:
        pass
    if heartbeat and time.time() - heartbeat < _ORPHAN_RUNNING_GRACE_SECONDS:
        return False
    latest_task_event = heartbeat
    latest_worker_boot = 0.0
    for event in iter_jsonl_objects(pathlib.Path(drive_root) / "logs" / "events.jsonl", tail_bytes=2_000_000):
        try:
            parsed = datetime.fromisoformat(str(event.get("ts") or "").strip().replace("Z", "+00:00"))
            ev_ts = float((parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp())
        except Exception:
            ev_ts = 0.0
        event_task_id = str(event.get("task_id") or "")
        if not event_task_id and isinstance(event.get("task"), dict):
            event_task_id = str((event.get("task") or {}).get("id") or "")
        if event_task_id == task_id:
            latest_task_event = max(latest_task_event, ev_ts)
        if str(event.get("type") or "") == "worker_boot":
            latest_worker_boot = max(latest_worker_boot, ev_ts)
    return bool(latest_worker_boot and latest_worker_boot > latest_task_event)


def _normalize_workspace_artifact_status(result: Dict[str, Any]) -> Dict[str, Any]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if not (str(result.get("workspace_root") or "").strip() or str(metadata.get("workspace_root") or "").strip()):
        return result
    status = str(result.get("status") or "").lower()
    if status not in FINAL_STATUSES:
        return result
    artifact_status = str(result.get("artifact_status") or "").lower()
    if artifact_status in ARTIFACT_TERMINAL_STATUSES:
        return result
    normalized = dict(result)
    normalized.setdefault("child_status", status)
    normalized["status"] = STATUS_RUNNING
    normalized["artifact_status"] = ARTIFACT_STATUS_FINALIZING
    return normalized


def _parent_workspace_artifact_lifecycle_fields(result: Dict[str, Any]) -> frozenset[str]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    if not (str(result.get("workspace_root") or "").strip() or str(metadata.get("workspace_root") or "").strip()):
        return frozenset()
    artifact_status = str(result.get("artifact_status") or "").strip().lower()
    if artifact_status in ARTIFACT_TERMINAL_STATUSES or artifact_status in ARTIFACT_NONTERMINAL_STATUSES:
        return _ARTIFACT_LIFECYCLE_FIELDS
    return frozenset()


def _merge_queue_status(current_status: str, queue_status: str) -> str:
    current = str(current_status or "").lower()
    queued = str(queue_status or "").lower()
    if not queued or current in FINAL_STATUSES:
        return current
    if current == STATUS_RUNNING and queued == STATUS_SCHEDULED:
        return current
    return queued


def load_effective_task_result(drive_root: pathlib.Path, task_id: str) -> Dict[str, Any]:
    try:
        tid = validate_task_id(task_id)
    except ValueError:
        return {}
    return effective_task_result(drive_root, load_task_result(drive_root, tid) or {})


def effective_task_result(drive_root: pathlib.Path, result: Dict[str, Any], *, _seen: frozenset[str] = frozenset()) -> Dict[str, Any]:
    """Merge parent result, child-drive result, and active queue state."""

    if not result:
        return {}
    task_id = str(result.get("task_id") or result.get("id") or "").strip()
    if not task_id:
        return dict(result)
    retry_id = str(result.get("superseded_by") or result.get("retry_task_id") or "").strip()
    if retry_id and retry_id != task_id and retry_id not in _seen:
        retry_result = load_task_result(drive_root, retry_id) or {}
        if retry_result:
            effective_retry = effective_task_result(
                pathlib.Path(drive_root),
                retry_result,
                _seen=frozenset(set(_seen) | {task_id}),
            )
            if effective_retry:
                merged_retry = dict(effective_retry)
                lineage = list(merged_retry.get("retry_lineage") or [])
                lineage.insert(0, {
                    "task_id": task_id,
                    "status": result.get("status"),
                    "result_status": result.get("result_status"),
                    "reason_code": result.get("reason_code"),
                    "retry_task_id": retry_id,
                })
                merged_retry["retry_lineage"] = lineage
                merged_retry.setdefault("original_task_id", task_id)
                merged_retry.setdefault("supersedes_task_id", task_id)
                return merged_retry

    merged = dict(result)
    child_result: Dict[str, Any] = {}
    child_text = ""
    for child_drive in _child_drive_candidates(result):
        child_result = load_task_result(child_drive, task_id) or {}
        if child_result:
            child_text = str(child_drive)
            break

    if child_result:
        parent_status = str(result.get("status") or "").lower()
        child_status = str(child_result.get("status") or "").lower()
        copied_child_status = str(result.get("child_status") or "").lower()
        metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
        result_is_workspace = bool(str(result.get("workspace_root") or "").strip() or str(metadata.get("workspace_root") or "").strip())
        copied_child_terminal = (
            result_is_workspace
            and copied_child_status in FINAL_STATUSES
            and parent_status == copied_child_status
        )
        preserve_parent_terminal = (
            (parent_status in {STATUS_FAILED, STATUS_CANCELLED, STATUS_REJECTED_DUPLICATE} and not copied_child_terminal)
            or (parent_status in FINAL_STATUSES and child_status not in FINAL_STATUSES)
        )
        preserve_parent_retry = (
            child_status not in FINAL_STATUSES
            and parent_status not in {STATUS_REQUESTED, STATUS_SCHEDULED, STATUS_RUNNING}
        )
        parent_authoritative_fields = (
            {"status", "result", "error", "ts"}
            if preserve_parent_terminal or preserve_parent_retry
            else set()
        )
        parent_authoritative_fields = parent_authoritative_fields | _parent_workspace_artifact_lifecycle_fields(result)
        for key, value in child_result.items():
            if key in {"task_id", "parent_task_id", "root_task_id", "session_id", "actor_id", "delegation_role"}:
                continue
            if key in parent_authoritative_fields:
                continue
            if key == "artifacts":
                continue
            merged[key] = value
        merged.setdefault("child_drive_root", child_text)
        merged.setdefault("headless_child_drive_root", child_text)
        metadata = merged.get("metadata") if isinstance(merged.get("metadata"), dict) else {}
        merged_is_workspace = bool(str(merged.get("workspace_root") or "").strip() or str(metadata.get("workspace_root") or "").strip())
        if merged_is_workspace and child_status in FINAL_STATUSES and (parent_status not in {STATUS_FAILED, STATUS_CANCELLED, STATUS_REJECTED_DUPLICATE} or copied_child_terminal):
            merged = _normalize_workspace_artifact_status(merged)

    merged = _normalize_workspace_artifact_status(merged)

    parent_status = str(merged.get("status") or "").lower()
    if parent_status not in FINAL_STATUSES:
        queue_status, queue_task = _queue_task_status(_load_queue_snapshot(pathlib.Path(drive_root)), task_id)
        if queue_status and queue_status != "unknown":
            merged["status"] = _merge_queue_status(parent_status, queue_status)
            for key in (
                "parent_task_id",
                "root_task_id",
                "session_id",
                "actor_id",
                "delegation_role",
                "role",
                "memory_mode",
                "drive_root",
                "child_drive_root",
                "budget_drive_root",
                "task_constraint",
            ):
                if not merged.get(key) and queue_task.get(key):
                    merged[key] = queue_task.get(key)
        else:
            result_status = str(merged.get("result_status") or "").strip().lower()
            if queue_status == "unknown":
                merged["queue_reconciliation_warning"] = "queue snapshot missing or invalid"
            elif result_status in _TERMINAL_FAILURE_RESULT_STATUSES:
                merged["status"] = STATUS_CANCELLED if result_status == "cancelled" else STATUS_FAILED
                merged["status_reconciled_from"] = parent_status
                artifact_status = str(merged.get("artifact_status") or "").strip().lower()
                if artifact_status in ARTIFACT_NONTERMINAL_STATUSES:
                    merged["artifact_status"] = ARTIFACT_STATUS_FAILED
                    bundle = dict(merged.get("artifact_bundle") or {}) if isinstance(merged.get("artifact_bundle"), dict) else {}
                    merged["artifact_bundle"] = _fail_nonterminal_artifact_bundle(
                        bundle,
                        "task ended before artifact finalization",
                    )
            elif _is_stale_orphan_running_task(pathlib.Path(drive_root), task_id, merged):
                merged["status"] = STATUS_FAILED
                merged["result_status"] = "infra_failed"
                merged["reason_code"] = "orphaned_running_after_worker_restart"
                merged["status_reconciled_from"] = parent_status
                merged["result"] = (
                    str(merged.get("result") or "Task was interrupted before a terminal result was recorded.")
                    + "\n\n⚠️ TASK_ORPHAN_RECONCILED: queue is empty and worker restarted after this task; "
                    "marking the stale running task as infra_failed."
                )
                artifact_status = str(merged.get("artifact_status") or "").strip().lower()
                if artifact_status in ARTIFACT_NONTERMINAL_STATUSES:
                    merged["artifact_status"] = ARTIFACT_STATUS_FAILED
                    bundle = dict(merged.get("artifact_bundle") or {}) if isinstance(merged.get("artifact_bundle"), dict) else {}
                    merged["artifact_bundle"] = _fail_nonterminal_artifact_bundle(
                        bundle,
                        "task interrupted before artifact finalization",
                    )
    try:
        from ouroboros.artifacts import (
            collect_task_artifact_records,
            copy_file_to_task_artifacts,
            merge_artifact_records,
        )
        from ouroboros.outcomes import artifact_bundle_from_result

        if child_result:
            parent_artifacts = [item for item in (result.get("artifacts") or []) if isinstance(item, dict)]
            child_artifacts_for_merge = [item for item in (child_result.get("artifacts") or []) if isinstance(item, dict)]
            if parent_artifacts or child_artifacts_for_merge:
                merged["artifacts"] = merge_artifact_records(parent_artifacts, child_artifacts_for_merge)

        rebased_child_artifacts: List[Dict[str, Any]] = []
        if child_text:
            parent_artifact_ctx = SimpleNamespace(drive_root=pathlib.Path(drive_root), task_id=task_id)
            child_artifacts = merge_artifact_records(
                [item for item in (child_result.get("artifacts") or []) if isinstance(item, dict)],
                collect_task_artifact_records(pathlib.Path(child_text), task_id),
            )
            for child_artifact in child_artifacts:
                source_text = str(child_artifact.get("path") or "").strip()
                if not source_text:
                    continue
                source = pathlib.Path(source_text).expanduser().resolve(strict=False)
                if not source.is_file():
                    continue
                copied = copy_file_to_task_artifacts(
                    parent_artifact_ctx,
                    source,
                    kind=str(child_artifact.get("kind") or "child_artifact"),
                )
                if copied:
                    rebased_child_artifacts.append(copied)

        collected_artifacts = collect_task_artifact_records(drive_root, task_id)
        if collected_artifacts or rebased_child_artifacts:
            existing_artifacts = [item for item in (merged.get("artifacts") or []) if isinstance(item, dict)]
            rebased_names = {
                str(item.get("name") or pathlib.Path(str(item.get("path") or "")).name)
                for item in rebased_child_artifacts
                if isinstance(item, dict)
            }
            if rebased_names:
                existing_artifacts = [
                    item
                    for item in existing_artifacts
                    if str(item.get("name") or pathlib.Path(str(item.get("path") or "")).name) not in rebased_names
                ]
                collected_artifacts = collect_task_artifact_records(drive_root, task_id)
            merged["artifacts"] = merge_artifact_records(existing_artifacts, rebased_child_artifacts, collected_artifacts)
            merged["artifact_bundle"] = artifact_bundle_from_result(merged)
            if not merged.get("artifact_status"):
                merged["artifact_status"] = merged["artifact_bundle"].get("status")
    except Exception:
        pass
    return merged


def wait_for_effective_tasks(
    drive_root: pathlib.Path,
    task_ids: Iterable[str],
    *,
    timeout_sec: float,
    mode: str = "all_terminal",
    poll_interval_sec: float = 0.5,
) -> Dict[str, Any]:
    ids = []
    for item in task_ids:
        try:
            tid = validate_task_id(item)
        except ValueError:
            tid = str(item or "").strip()
        if tid and tid not in ids:
            ids.append(tid)
    start = time.monotonic()
    deadline = start + max(0.0, float(timeout_sec or 0))
    results: Dict[str, Dict[str, Any]] = {}
    timed_out = False
    while True:
        results = {tid: load_effective_task_result(pathlib.Path(drive_root), tid) for tid in ids}
        terminal = {tid: str(data.get("status") or "").strip().lower() in FINAL_STATUSES for tid, data in results.items()}
        if mode == "any_terminal" and any(terminal.values()):
            break
        if mode != "any_terminal" and all(terminal.values()):
            break
        if time.monotonic() >= deadline:
            timed_out = True
            break
        time.sleep(max(0.05, min(2.0, float(poll_interval_sec or 0.5))))
    return {
        "mode": mode,
        "timeout_sec": float(timeout_sec or 0),
        "elapsed_sec": max(0.0, time.monotonic() - start),
        "timed_out": timed_out,
        "all_terminal": all(str(data.get("status") or "").strip().lower() in FINAL_STATUSES for data in results.values()) if ids else True,
        "tasks": results,
    }


def find_child_tasks(
    drive_root: pathlib.Path,
    *,
    parent_task_id: str = "",
    root_task_id: str = "",
    exclude_task_id: str = "",
) -> List[Dict[str, Any]]:
    parent = str(parent_task_id or "").strip()
    root = str(root_task_id or "").strip()
    excluded = str(exclude_task_id or "").strip()
    rows: Dict[str, Dict[str, Any]] = {}
    for row in (effective_task_result(pathlib.Path(drive_root), item) for item in list_task_results(pathlib.Path(drive_root))):
        tid = str(row.get("task_id") or "")
        if not tid or tid == excluded:
            continue
        if str(row.get("delegation_role") or "") != "subagent":
            continue
        if parent and str(row.get("parent_task_id") or "") == parent:
            rows[tid] = row
        elif root and str(row.get("root_task_id") or "") == root:
            rows[tid] = row

    snapshot = _load_queue_snapshot(pathlib.Path(drive_root))
    for group, status in (("pending", STATUS_SCHEDULED), ("running", STATUS_RUNNING)):
        for item in snapshot.get(group) or []:
            if not isinstance(item, dict):
                continue
            task = item.get("task") if isinstance(item.get("task"), dict) else {}
            tid = str(item.get("id") or task.get("id") or "")
            if not tid or tid == excluded:
                continue
            if str(task.get("delegation_role") or "") != "subagent":
                continue
            if parent and str(task.get("parent_task_id") or "") == parent:
                row = dict(task)
            elif root and str(task.get("root_task_id") or "") == root:
                row = dict(task)
            else:
                continue
            row.setdefault("task_id", tid)
            row["status"] = status
            existing = rows.get(tid, {})
            if not existing:
                rows[tid] = row
                continue
            combined = dict(existing)
            for key, value in row.items():
                if key == "status":
                    combined["status"] = _merge_queue_status(str(existing.get("status") or ""), str(value or ""))
                elif not combined.get(key) and value:
                    combined[key] = value
            rows[tid] = combined
    return sorted(rows.values(), key=lambda item: (str(item.get("ts") or ""), str(item.get("task_id") or "")))


def _handoff_snippet(value: Any) -> Dict[str, Any]:
    text = str(value or "")
    stripped = text.strip()
    if not stripped:
        return {"available": False, "chars": 0, "preview": ""}
    preview = stripped.replace("\n", " ")
    if len(preview) > HANDOFF_SNIPPET_CHARS:
        preview = preview[: HANDOFF_SNIPPET_CHARS - 3] + "..."
    return {"available": True, "chars": len(text), "preview": preview}


def format_handoff_message(children: List[Dict[str, Any]]) -> str:
    payload = []
    for child in children:
        result_info = _handoff_snippet(child.get("result"))
        trace_info = _handoff_snippet(child.get("trace_summary"))
        payload.append({
            "task_id": str(child.get("task_id") or child.get("id") or ""),
            "status": str(child.get("status") or ""),
            "role": str(child.get("role") or ""),
            "description": str(child.get("description") or child.get("objective") or ""),
            "cost_usd": child.get("cost_usd", 0),
            "artifact_status": str(child.get("artifact_status") or ""),
            "result_available": result_info["available"],
            "result_chars": result_info["chars"],
            "result_preview": result_info["preview"],
            "trace_available": trace_info["available"],
            "trace_chars": trace_info["chars"],
            "trace_preview": trace_info["preview"],
            "full_output": "Use get_task_result, wait_task, or wait_tasks for the full untruncated child output.",
        })
    return (
        "[SUBAGENT_HANDOFF_STATUS]\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n[/SUBAGENT_HANDOFF_STATUS]"
    )

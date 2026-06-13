"""Helpers for durable task result/status files."""

from __future__ import annotations

import logging
import pathlib
import re
from typing import Any, Dict, List, Optional

from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)

STATUS_REQUESTED = "requested"
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_REJECTED_DUPLICATE = "rejected_duplicate"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"
# Intent latch: the agent/owner asked to cancel, but the supervisor has not yet
# torn the task down. Ranks above running so a late running/scheduled mirror
# cannot resurrect it, but below the truly-terminal statuses so the eventual
# STATUS_CANCELLED write still lands.
STATUS_CANCEL_REQUESTED = "cancel_requested"

# Monotonic lifecycle ordering. A write that would move a task *backwards* past
# the cancel-intent latch or a terminal status is ignored, so a stale
# scheduled/running mirror can never clobber a cancel/terminal outcome
# (the "ghost subagent" class). Unknown statuses are unranked and never block.
_TRULY_TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_REJECTED_DUPLICATE,
})
_STATUS_RANK = {
    STATUS_REQUESTED: 0,
    STATUS_SCHEDULED: 1,
    STATUS_RUNNING: 2,
    STATUS_INTERRUPTED: 2,
    STATUS_CANCEL_REQUESTED: 3,
    STATUS_COMPLETED: 4,
    STATUS_FAILED: 4,
    STATUS_CANCELLED: 4,
    STATUS_REJECTED_DUPLICATE: 4,
}
# Regressions are only blocked once a task reaches the cancel-intent latch or a
# terminal state; normal forward progress (requested->scheduled->running) and
# unknown statuses are always allowed.
_REGRESSION_GUARD_FLOOR = _STATUS_RANK[STATUS_CANCEL_REQUESTED]

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def _is_status_regression(existing_status: str, new_status: str) -> bool:
    """Return True when writing *new_status* over *existing_status* would
    regress or corrupt a task that has already reached cancel-intent or a
    terminal state.

    Rules:
      - Unknown statuses never block (forward-compatible).
      - Truly-terminal is sticky: once completed/failed/cancelled/rejected, only
        a same-status rewrite is allowed (result/trace enrichment). Switching to
        a *different* terminal status (e.g. cancelled -> completed) is blocked.
      - cancel-intent (cancel_requested) blocks regress to running/scheduled but
        still allows the supervisor's eventual terminal write (rank 3 -> 4).
    """
    existing = str(existing_status or "")
    new = str(new_status or "")
    # Sticky terminal FIRST — independent of whether the new status is ranked, so
    # a typo/unknown/future status can never overwrite a terminal one. Only an
    # identical-status rewrite (result/trace enrichment) is allowed.
    if existing in _TRULY_TERMINAL_STATUSES:
        return new != existing
    if existing == STATUS_CANCEL_REQUESTED:
        # Once cancellation is requested, never let a late success/duplicate (or
        # an unknown/unranked status) mask it: a worker finishing right after the
        # cancel latch must not flip the task to "completed". Allow only the real
        # teardown outcomes (cancelled/failed) or a same-status rewrite.
        return new not in (STATUS_CANCEL_REQUESTED, STATUS_CANCELLED, STATUS_FAILED)
    existing_rank = _STATUS_RANK.get(existing)
    new_rank = _STATUS_RANK.get(new)
    if existing_rank is None or new_rank is None:
        return False
    if existing_rank >= _REGRESSION_GUARD_FLOOR:
        return new_rank < existing_rank
    return False


def validate_task_id(task_id: Any) -> str:
    text = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(text):
        raise ValueError("task_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    return text


def task_results_dir(drive_root: Any) -> pathlib.Path:
    path = pathlib.Path(drive_root) / "task_results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_result_path(drive_root: Any, task_id: str) -> pathlib.Path:
    return task_results_dir(drive_root) / f"{validate_task_id(task_id)}.json"


def load_task_result(drive_root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        path = task_result_path(drive_root, task_id)
    except ValueError:
        return None
    return read_json_dict(path)


def list_task_results(
    drive_root: Any,
    *,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    wanted = {str(item) for item in list(statuses or []) if str(item).strip()}
    results: List[Dict[str, Any]] = []
    for path in sorted(task_results_dir(drive_root).glob("*.json")):
        data = read_json_dict(path)
        if data is None:
            continue
        if wanted and str(data.get("status") or "") not in wanted:
            continue
        results.append(data)
    return results


def write_task_result(
    results_drive_root: Any,
    task_id: str,
    status: str,
    **fields: Any,
) -> Dict[str, Any]:
    path = task_result_path(results_drive_root, task_id)
    existing = load_task_result(results_drive_root, task_id) or {}

    # Monotonic lifecycle: never let a stale scheduled/running mirror overwrite a
    # cancel-intent latch or a terminal outcome. This is the structural guard
    # against "ghost" tasks that keep reporting scheduled/running after they were
    # cancelled or finished.
    if existing and _is_status_regression(existing.get("status"), status):
        # Surface the blocked transition: when debugging a "stuck" task this is
        # the only signal that a stale/late write was intentionally dropped.
        log.debug("Blocked status regression %s -> %s for task %s",
                  existing.get("status"), status, task_id)
        return existing

    now = utc_now_iso()
    ts = str(fields.pop("ts", "") or existing.get("ts") or now)
    payload = {
        **existing,
        **fields,
        "task_id": task_id,
        "status": status,
        "ts": ts,
        "updated_at": now,
    }

    atomic_write_json(path, payload)
    return payload

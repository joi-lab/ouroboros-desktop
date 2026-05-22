"""Helpers for durable task result/status files."""

from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, List, Optional

from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

STATUS_REQUESTED = "requested"
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_REJECTED_DUPLICATE = "rejected_duplicate"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


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
    drive_root: Any,
    task_id: str,
    status: str,
    **fields: Any,
) -> Dict[str, Any]:
    path = task_result_path(drive_root, task_id)
    existing = load_task_result(drive_root, task_id) or {}

    ts = str(fields.pop("ts", "") or existing.get("ts") or utc_now_iso())
    payload = {
        **existing,
        **fields,
        "task_id": task_id,
        "status": status,
        "ts": ts,
    }

    atomic_write_json(path, payload)
    return payload

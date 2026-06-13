"""Read-only access to recent task summaries for LLM-first context recovery."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.task_status import effective_task_result


_MAX_TASKS = 20
_PREVIEW_CHARS = 800


def _coerce_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 5
    return max(1, min(_MAX_TASKS, limit))


def _read_json(path: pathlib.Path) -> tuple[Dict[str, Any] | None, str]:
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, f"expected JSON object, got {type(data).__name__}"
    return data, ""


def _preview(text: Any) -> str:
    value = str(text or "")
    if len(value) <= _PREVIEW_CHARS:
        return value
    return value[:_PREVIEW_CHARS] + f"\n... (truncated preview from {len(value)} chars)"


def _task_record(
    path: pathlib.Path,
    *,
    drive_root: pathlib.Path,
    include_results: bool,
    include_traces: bool,
) -> tuple[Dict[str, Any] | None, Dict[str, str] | None]:
    data, error = _read_json(path)
    if data is None:
        return None, {"path": str(path), "error": error}
    data = effective_task_result(drive_root, data)
    result = str(data.get("result") or "")
    record: Dict[str, Any] = {
        "task_id": str(data.get("task_id") or path.stem),
        "ts": str(data.get("ts") or ""),
        "status": str(data.get("status") or ""),
        "description": str(data.get("description") or ""),
        "cost_usd": data.get("cost_usd", 0),
        "total_rounds": data.get("total_rounds"),
        "result_preview": _preview(result),
    }
    if include_results:
        record["result"] = result
    if include_traces:
        record["trace_summary"] = str(data.get("trace_summary") or "")
    return record, None


def _running_tasks(drive_root: pathlib.Path) -> List[Dict[str, Any]]:
    snapshot, _error = _read_json(drive_root / "state" / "queue_snapshot.json")
    snapshot = snapshot or {}
    running = snapshot.get("running")
    if not isinstance(running, list):
        return []
    rows: List[Dict[str, Any]] = []
    for item in running:
        if not isinstance(item, dict):
            continue
        rows.append({
            "task_id": str(item.get("id") or item.get("task_id") or ""),
            "status": "running",
            "description": str(item.get("text") or item.get("description") or ""),
            "ts": str(item.get("ts") or snapshot.get("ts") or ""),
        })
    return rows


def _handle_recent_tasks(
    ctx: ToolContext,
    limit: int = 5,
    include_results: bool = False,
    include_traces: bool = False,
    **_kwargs: Any,
) -> str:
    """Return recent completed task summaries from the current drive."""
    drive_root = pathlib.Path(ctx.drive_root)
    task_dir = drive_root / "task_results"
    task_limit = _coerce_limit(limit)
    tasks: List[Dict[str, Any]] = []
    unreadable_tasks: List[Dict[str, str]] = []
    if task_dir.is_dir():
        files = sorted(
            (p for p in task_dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for path in files[:task_limit]:
            record, error = _task_record(
                path,
                drive_root=drive_root,
                include_results=bool(include_results),
                include_traces=bool(include_traces),
            )
            if record is not None:
                tasks.append(record)
            elif error is not None:
                unreadable_tasks.append(error)
    return json.dumps({
        "running": _running_tasks(drive_root),
        "tasks": tasks,
        "unreadable_tasks": unreadable_tasks,
    }, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("recent_tasks", {
            "name": "recent_tasks",
            "description": (
                "Read recent task results from this drive. Use when prior work, "
                "continuations, retries, or incomplete current context may matter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of recent completed tasks to return (1-20).",
                        "default": 5,
                    },
                    "include_results": {
                        "type": "boolean",
                        "description": "Include full result text instead of only result_preview.",
                        "default": False,
                    },
                    "include_traces": {
                        "type": "boolean",
                        "description": "Include each task's trace_summary.",
                        "default": False,
                    },
                },
                "required": [],
            },
        }, _handle_recent_tasks),
    ]

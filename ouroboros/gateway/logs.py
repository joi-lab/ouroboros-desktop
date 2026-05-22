"""Read-only runtime log endpoints for headless clients."""

from __future__ import annotations

import pathlib
import json
from typing import Any, Dict, List

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import coerce_int, json_error, request_drive_root
from ouroboros.task_results import load_task_result


_ALLOWED_LOGS = {
    "chat": "chat.jsonl",
    "progress": "progress.jsonl",
    "events": "events.jsonl",
    "tools": "tools.jsonl",
    "supervisor": "supervisor.jsonl",
}


async def api_logs_tail(request: Request) -> JSONResponse:
    name = str(request.path_params.get("name") or "").strip().lower()
    filename = _ALLOWED_LOGS.get(name)
    if not filename:
        return json_error(f"unknown log {name!r}", 404, allowed=sorted(_ALLOWED_LOGS))
    limit = max(1, min(coerce_int(request.query_params.get("limit"), 100), 2000))
    task_id = str(request.query_params.get("task_id") or "").strip()
    drive_root = request_drive_root(request)
    roots = [drive_root]
    if task_id:
        result = load_task_result(drive_root, task_id) or {}
        child = str(result.get("child_drive_root") or result.get("headless_child_drive_root") or "").strip()
        if child:
            roots.append(pathlib.Path(child))
    rows: List[Dict[str, Any]] = []
    for root in roots:
        path = pathlib.Path(root) / "logs" / filename
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line_no, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if task_id and str(entry.get("task_id") or "") != task_id:
                continue
            item = dict(entry)
            item.setdefault("_source_root", str(root))
            item.setdefault("_line", line_no)
            rows.append(item)
    rows.sort(key=lambda item: (str(item.get("ts") or ""), str(item.get("_source_root") or ""), int(item.get("_line") or 0)))
    return JSONResponse({"name": name, "entries": rows[-limit:]})


__all__ = ["api_logs_tail"]

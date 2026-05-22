"""Headless task gateway endpoints."""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
import time
import uuid
from typing import Any, Dict, Iterable, List, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from ouroboros.gateway._helpers import coerce_int, json_error, json_exception, request_drive_root, request_json_or, request_repo_dir
from ouroboros.headless import ARTIFACTS_DIR, HEADLESS_TASKS_DIR, prepare_task_drive
from ouroboros.task_results import STATUS_SCHEDULED, list_task_results, load_task_result, validate_task_id, write_task_result
from ouroboros.utils import iter_jsonl_objects, utc_now_iso


_FINAL_STATUSES = {"completed", "failed", "cancelled", "rejected_duplicate"}
_LOG_SOURCES = (
    ("progress", ("logs", "progress.jsonl")),
    ("chat", ("logs", "chat.jsonl")),
    ("events", ("logs", "events.jsonl")),
    ("tools", ("logs", "tools.jsonl")),
    ("supervisor", ("logs", "supervisor.jsonl")),
)


async def api_tasks_create(request: Request) -> JSONResponse:
    """POST /api/tasks — enqueue a managed headless task."""

    body = await request_json_or(request, {})
    if not isinstance(body, dict):
        return json_error("request body must be a JSON object", 400)
    description = str(body.get("description") or body.get("text") or body.get("prompt") or "").strip()
    if not description:
        return json_error("description is required", 400)

    ready_error = _supervisor_ready_error(request)
    if ready_error:
        return ready_error

    drive_root = request_drive_root(request)
    repo_dir = request_repo_dir(request)
    try:
        task_id = validate_task_id(body.get("task_id") or uuid.uuid4().hex[:8])
    except ValueError as exc:
        return json_error(str(exc), 400)
    if load_task_result(drive_root, task_id):
        return json_error(f"task_id already exists: {task_id}", 409)
    if (drive_root / HEADLESS_TASKS_DIR / task_id).exists() or (drive_root / ARTIFACTS_DIR / task_id).exists():
        return json_error(f"task_id already has headless state: {task_id}", 409)
    try:
        workspace_root = _resolve_workspace_root(
            body.get("workspace_root"),
            system_repo_dir=repo_dir,
            drive_root=drive_root,
        )
    except ValueError as exc:
        return json_error(str(exc), 400)
    workspace_mode = str(body.get("workspace_mode") or ("external" if workspace_root else "")).strip()
    memory_mode = str(body.get("memory_mode") or ("forked" if workspace_root else "shared")).strip().lower()
    if workspace_root and memory_mode not in {"forked", "empty", "shared"}:
        return json_error("memory_mode must be one of forked, empty, shared", 400)
    if workspace_root and memory_mode == "shared":
        return json_error("memory_mode=shared is not allowed for external workspaces; use forked or empty", 400)
    task_type = str(body.get("type") or "task")
    if workspace_root and task_type != "task":
        return json_error("external workspace tasks must use type='task'", 400)
    try:
        chat_id = int(body.get("chat_id") if body.get("chat_id") is not None else 0)
        depth = int(body.get("depth") or 0)
    except (TypeError, ValueError):
        return json_error("chat_id and depth must be integers", 400)

    child_drive = prepare_task_drive(drive_root, task_id, memory_mode) if workspace_root else None
    metadata = dict(body.get("metadata") or {}) if isinstance(body.get("metadata"), dict) else {}
    metadata.setdefault("session_id", str(body.get("session_id") or uuid.uuid4().hex))
    metadata.setdefault("actor_id", str(body.get("actor_id") or "cli"))
    metadata.setdefault("delegation_role", str(body.get("delegation_role") or "root"))
    parent_task_id = str(body.get("parent_task_id") or "") or None
    explicit_root = str(body.get("root_task_id") or "").strip()
    parent_result = load_task_result(drive_root, parent_task_id) if parent_task_id else {}
    root_task_id = explicit_root or str(parent_result.get("root_task_id") or "") or parent_task_id or task_id
    metadata.setdefault("task_id", task_id)
    metadata.setdefault("parent_task_id", parent_task_id or "")
    metadata.setdefault("root_task_id", root_task_id)
    if workspace_root:
        metadata["workspace_root"] = str(workspace_root)

    task_text = _compose_task_text(
        description,
        workspace_root=workspace_root,
        workspace_mode=workspace_mode,
        memory_mode=memory_mode,
        attachments=body.get("attachments"),
    )
    task = {
        "id": task_id,
        "type": task_type,
        "chat_id": chat_id,
        "text": task_text,
        "description": description,
        "context": str(body.get("context") or ""),
        "depth": depth,
        "parent_task_id": parent_task_id,
        "root_task_id": root_task_id,
        "session_id": metadata["session_id"],
        "actor_id": metadata["actor_id"],
        "delegation_role": metadata["delegation_role"],
        "workspace_root": str(workspace_root) if workspace_root else "",
        "workspace_mode": workspace_mode,
        "memory_mode": memory_mode,
        "metadata": metadata,
        "attachments": _normalize_attachments(body.get("attachments")),
    }
    if child_drive is not None:
        task["drive_root"] = str(child_drive)
        task["budget_drive_root"] = str(drive_root)
        metadata["budget_drive_root"] = str(drive_root)
    write_task_result(
        drive_root,
        task_id,
        STATUS_SCHEDULED,
        parent_task_id=task.get("parent_task_id"),
        root_task_id=task.get("root_task_id"),
        session_id=task.get("session_id"),
        actor_id=task.get("actor_id"),
        delegation_role=task.get("delegation_role"),
        description=description,
        context=task.get("context"),
        workspace_root=task.get("workspace_root"),
        workspace_mode=workspace_mode,
        memory_mode=memory_mode,
        child_drive_root=str(child_drive or ""),
        budget_drive_root=str(drive_root) if child_drive is not None else "",
        result="Task accepted and scheduled.",
    )
    try:
        from supervisor.queue import enqueue_task, persist_queue_snapshot

        enqueue_task(task)
        persist_queue_snapshot(reason="api_task_create")
    except Exception as exc:
        write_task_result(
            drive_root,
            task_id,
            "failed",
            parent_task_id=task.get("parent_task_id"),
            root_task_id=task.get("root_task_id"),
            session_id=task.get("session_id"),
            actor_id=task.get("actor_id"),
            delegation_role=task.get("delegation_role"),
            description=description,
            context=task.get("context"),
            workspace_root=task.get("workspace_root"),
            workspace_mode=workspace_mode,
            memory_mode=memory_mode,
            child_drive_root=str(child_drive or ""),
            budget_drive_root=str(drive_root) if child_drive is not None else "",
            result=f"Failed to enqueue task: {exc}",
        )
        return json_exception(exc, 503)
    return JSONResponse({"ok": True, "task_id": task_id, "status": STATUS_SCHEDULED})


async def api_tasks_list(request: Request) -> JSONResponse:
    statuses = [
        item.strip()
        for item in str(request.query_params.get("status") or "").split(",")
        if item.strip()
    ]
    limit = max(1, min(coerce_int(request.query_params.get("limit"), 50), 500))
    drive_root = request_drive_root(request)
    wanted = {status.lower() for status in statuses}
    rows = [_effective_task_result(drive_root, row) for row in list_task_results(drive_root)]
    if wanted:
        rows = [row for row in rows if str(row.get("status") or "").lower() in wanted]
    rows.sort(key=lambda item: str(item.get("ts") or ""), reverse=True)
    return JSONResponse({"tasks": rows[:limit], "queue": _queue_snapshot(drive_root)})


async def api_task_get(request: Request) -> JSONResponse:
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        return json_error(str(exc), 400)
    drive_root = request_drive_root(request)
    data = _effective_task_result(drive_root, load_task_result(drive_root, task_id) or {})
    if not data:
        return json_error("task not found", 404)
    return JSONResponse(data)


async def api_task_cancel(request: Request) -> JSONResponse:
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        return json_error(str(exc), 400)
    try:
        from supervisor.queue import cancel_task_by_id

        ok = cancel_task_by_id(task_id)
    except Exception as exc:
        return json_exception(exc, 503)
    if not ok:
        return json_error("task not found or not active", 404, task_id=task_id)
    return JSONResponse({"ok": True, "task_id": task_id})


async def api_task_events(request: Request) -> StreamingResponse:
    try:
        task_id = validate_task_id(request.path_params.get("task_id"))
    except ValueError as exc:
        message = str(exc)
        async def _bad_id():
            yield _sse({"type": "error", "error": message, "seq": 1}, event_id=1)
        return StreamingResponse(_bad_id(), media_type="text/event-stream", status_code=400)
    cursor = max(0, coerce_int(request.query_params.get("cursor"), 0))
    wait_sec = max(0, min(coerce_int(request.query_params.get("wait"), 30), 120))
    drive_root = request_drive_root(request)
    if not load_task_result(drive_root, task_id):
        async def _missing():
            yield _sse({"type": "error", "error": "task not found", "task_id": task_id, "seq": 1}, event_id=1)
        return StreamingResponse(_missing(), media_type="text/event-stream", status_code=404)

    async def _stream():
        nonlocal cursor
        deadline = time.time() + wait_sec
        while True:
            events = [evt for evt in iter_task_events(drive_root, task_id) if int(evt.get("seq") or 0) > cursor]
            emitted_final = False
            for event in events:
                cursor = int(event.get("seq") or cursor)
                if str(event.get("type") or "") == "task_result":
                    data = event.get("data") if isinstance(event.get("data"), dict) else {}
                    emitted_final = str(data.get("status") or "").lower() in _FINAL_STATUSES
                yield _sse(event, event_id=cursor)
            if _is_task_final(drive_root, task_id):
                if not emitted_final:
                    result = _effective_task_result(drive_root, load_task_result(drive_root, task_id) or {})
                    if result:
                        final_event = {
                            "source": "task_result",
                            "line": 0,
                            "ts": str(result.get("ts") or ""),
                            "type": "task_result",
                            "task_id": task_id,
                            "data": result,
                            "seq": cursor + 1,
                        }
                        cursor = int(final_event["seq"])
                        yield _sse(final_event, event_id=cursor)
                break
            if time.time() >= deadline:
                yield ": heartbeat\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(_stream(), media_type="text/event-stream")


def iter_task_events(drive_root: pathlib.Path, task_id: str) -> List[Dict[str, Any]]:
    """Return synthesized replayable events for a task from existing logs."""

    rows: List[Dict[str, Any]] = []
    roots = [pathlib.Path(drive_root)]
    result = _effective_task_result(drive_root, load_task_result(drive_root, task_id) or {})
    child = str(result.get("child_drive_root") or result.get("headless_child_drive_root") or "").strip()
    if child:
        roots.append(pathlib.Path(child))
    for root in roots:
        for source, parts in _LOG_SOURCES:
            path = root.joinpath(*parts)
            for line_no, entry in enumerate(iter_jsonl_objects(path), 1):
                if str(entry.get("task_id") or "") != task_id:
                    continue
                rows.append(_event_from_log_entry(source, line_no, entry, root))
    if result:
        rows.append({
            "source": "task_result",
            "line": 0,
            "ts": str(result.get("ts") or ""),
            "type": "task_result",
            "task_id": task_id,
            "data": result,
        })
    rows.sort(key=lambda item: (str(item.get("ts") or ""), str(item.get("source") or ""), int(item.get("line") or 0)))
    for idx, row in enumerate(rows, 1):
        row["seq"] = idx
    return rows


def _event_from_log_entry(source: str, line_no: int, entry: Dict[str, Any], root: pathlib.Path) -> Dict[str, Any]:
    event_type = str(entry.get("type") or source)
    if source == "progress":
        event_type = "progress"
    elif source == "chat":
        event_type = "message"
    elif source == "tools":
        event_type = "tool_call"
    return {
        "source": source,
        "line": line_no,
        "ts": str(entry.get("ts") or ""),
        "type": event_type,
        "task_id": str(entry.get("task_id") or ""),
        "root": str(root),
        "data": entry,
    }


def _effective_task_result(drive_root: pathlib.Path, result: Dict[str, Any]) -> Dict[str, Any]:
    if not result:
        return {}
    task_id = str(result.get("task_id") or result.get("id") or "").strip()
    child_text = str(result.get("child_drive_root") or result.get("headless_child_drive_root") or "").strip()
    if not task_id or not child_text:
        return result
    child_result = load_task_result(pathlib.Path(child_text), task_id) or {}
    if not child_result:
        return result
    merged = dict(result)
    parent_status = str(result.get("status") or "").lower()
    child_status = str(child_result.get("status") or "").lower()
    preserve_parent_terminal = (
        parent_status in {"failed", "cancelled", "rejected_duplicate"}
        or (parent_status in _FINAL_STATUSES and child_status not in _FINAL_STATUSES)
    )
    parent_terminal_fields = {"status", "result", "error", "ts"} if preserve_parent_terminal else set()
    for key, value in child_result.items():
        if key in {"task_id", "parent_task_id", "root_task_id", "session_id", "actor_id", "delegation_role"}:
            continue
        if key in parent_terminal_fields:
            continue
        merged[key] = value
    merged.setdefault("child_drive_root", child_text)
    return merged


def _sse(event: Dict[str, Any], *, event_id: int) -> str:
    payload = json.dumps(event, ensure_ascii=False)
    return f"id: {event_id}\nevent: task_event\ndata: {payload}\n\n"


def _is_task_final(drive_root: pathlib.Path, task_id: str) -> bool:
    result = _effective_task_result(drive_root, load_task_result(drive_root, task_id) or {})
    return str(result.get("status") or "").lower() in _FINAL_STATUSES


def _resolve_workspace_root(
    value: Any,
    *,
    system_repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Optional[pathlib.Path]:
    text = str(value or "").strip()
    if not text:
        return None
    root = pathlib.Path(text).expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"workspace_root is not a directory: {text}")
    system_repo = pathlib.Path(system_repo_dir).resolve(strict=False)
    drive = pathlib.Path(drive_root).resolve(strict=False)
    for protected_root, label in ((system_repo, "Ouroboros system repo"), (drive, "Ouroboros data drive")):
        overlaps = False
        try:
            root.relative_to(protected_root)
            overlaps = True
        except ValueError:
            try:
                protected_root.relative_to(root)
                overlaps = True
            except ValueError:
                pass
        if overlaps:
            raise ValueError(f"workspace_root must not overlap the {label}")
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        res = None
    git_root_text = (res.stdout or "").strip() if res is not None and res.returncode == 0 else ""
    git_root = pathlib.Path(git_root_text).resolve(strict=False) if git_root_text else None
    if git_root is None:
        raise ValueError("workspace_root must be a git worktree root")
    if git_root != root:
        raise ValueError(f"workspace_root must be the git worktree root: {git_root}")
    return root


def _normalize_attachments(value: Any) -> List[Dict[str, str]]:
    if not value:
        return []
    if not isinstance(value, list):
        return []
    out: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            path = str(item.get("path") or "").strip()
            label = str(item.get("label") or pathlib.Path(path).name).strip()
        else:
            path = str(item or "").strip()
            label = pathlib.Path(path).name
        if path:
            out.append({"path": path, "label": label})
    return out


def _compose_task_text(
    description: str,
    *,
    workspace_root: Optional[pathlib.Path],
    workspace_mode: str,
    memory_mode: str,
    attachments: Any,
) -> str:
    parts = [description]
    if workspace_root is not None:
        parts.append(
            "\n\n[HEADLESS_WORKSPACE]\n"
            f"workspace_root: {workspace_root}\n"
            f"workspace_mode: {workspace_mode or 'external'}\n"
            f"memory_mode: {memory_mode}\n"
            "Use repo_read, repo_write, repo_list, code_search, and run_shell against this active workspace.\n"
            "Do not commit in the workspace. Leave changes as a patch artifact for the caller.\n"
            "[END_HEADLESS_WORKSPACE]"
        )
    normalized = _normalize_attachments(attachments)
    if normalized:
        rendered = "\n".join(f"- {item['label']}: {item['path']}" for item in normalized)
        parts.append(f"\n\n[ATTACHMENTS]\n{rendered}\n[END_ATTACHMENTS]")
    return "".join(parts)


def _queue_snapshot(drive_root: pathlib.Path) -> Dict[str, Any]:
    path = pathlib.Path(drive_root) / "state" / "queue_snapshot.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _supervisor_ready_error(request: Request) -> Optional[JSONResponse]:
    state = getattr(request.app, "state", None)
    ready_event = getattr(state, "supervisor_ready_event", None) if state is not None else None
    if ready_event is not None and not ready_event.is_set():
        return json_error("supervisor is still starting", 503)
    try:
        from supervisor.workers import WORKERS

        if ready_event is not None and not WORKERS:
            return json_error("supervisor has no running workers", 503)
    except Exception:
        pass
    return None


__all__ = [
    "api_task_cancel",
    "api_task_events",
    "api_task_get",
    "api_tasks_create",
    "api_tasks_list",
    "iter_task_events",
]

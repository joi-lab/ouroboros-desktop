"""Control, update, and evolution HTTP endpoints."""

from __future__ import annotations

import asyncio
import pathlib
import time
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import get_version
from ouroboros.gateway._helpers import json_error, json_exception, request_drive_root, request_json_or, request_repo_dir
from ouroboros.gateway.ws import broadcast_ws_sync
from ouroboros.utils import utc_now_iso

_RECENT_VISIBLE_COMMANDS: Dict[str, float] = {}
_VISIBLE_COMMAND_DEDUPE_SEC = 5.0
_evo_cache: Dict[str, Any] = {}
_evo_task: asyncio.Task | None = None


def _request_restart(request: Request) -> None:
    callback = getattr(getattr(request.app, "state", None), "request_restart", None)
    if callable(callback):
        callback()


def _runtime_branch_defaults(request: Request) -> tuple[str, str]:
    callback = getattr(getattr(request.app, "state", None), "runtime_branch_defaults", None)
    if callable(callback):
        return callback()
    return "ouroboros", "ouroboros-stable"


def _managed_update_payload(*, fetch: bool, include_tags: bool) -> dict[str, Any]:
    from supervisor.git_ops import compute_managed_update_status, git_capture

    status = compute_managed_update_status(fetch=fetch)
    latest_version = ""
    target_ref = status.get("target_ref") or ""
    if target_ref and status.get("latest_sha"):
        rc, version_text, _ = git_capture(["git", "show", f"{target_ref}:VERSION"])
        if rc == 0:
            latest_version = version_text.strip()
    official_tags = []
    if include_tags:
        from supervisor.git_ops import list_official_update_tags

        official_tags = list_official_update_tags()
    return {
        "current_version": get_version(),
        "latest_version": latest_version,
        "official_tags": official_tags,
        **status,
    }


async def api_reset(request: Request) -> JSONResponse:
    """Reset all runtime data (state, memory, logs, settings) but keep repo."""
    import shutil

    data_dir = request_drive_root(request)
    try:
        deleted = []
        for subdir in ("state", "memory", "logs", "archive", "locks", "task_results", "uploads"):
            target = data_dir / subdir
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                deleted.append(subdir)
        settings_file = data_dir / "settings.json"
        if settings_file.exists():
            settings_file.unlink()
            deleted.append("settings.json")
        _request_restart(request)
        return JSONResponse({"status": "ok", "deleted": deleted, "restarting": True})
    except Exception as exc:
        return json_exception(exc)


async def api_command(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        if cmd:
            from supervisor.message_bus import get_bridge, log_chat

            bridge = get_bridge()
            visible_text = str(body.get("visible_text") or "").strip()
            task_constraint = body.get("task_constraint") if isinstance(body.get("task_constraint"), dict) else None
            visible_task_id = str(body.get("visible_task_id") or "").strip()
            if visible_task_id:
                now = time.monotonic()
                expired = [
                    key for key, ts in _RECENT_VISIBLE_COMMANDS.items()
                    if now - ts > _VISIBLE_COMMAND_DEDUPE_SEC
                ]
                for key in expired:
                    _RECENT_VISIBLE_COMMANDS.pop(key, None)
                if visible_task_id in _RECENT_VISIBLE_COMMANDS:
                    return JSONResponse({"ok": True, "deduped": True, "task_id": visible_task_id})
            send_kwargs: dict[str, Any] = {"broadcast": False, "suppress_chat_log": bool(visible_text)}
            if task_constraint:
                send_kwargs["task_constraint"] = task_constraint
            bridge.ui_send(cmd, **send_kwargs)
            if visible_task_id:
                _RECENT_VISIBLE_COMMANDS[visible_task_id] = time.monotonic()
            if visible_text:
                task_id = visible_task_id or "skill_repair"
                ts = utc_now_iso()
                payload = {
                    "type": "chat",
                    "role": "system",
                    "content": visible_text,
                    "ts": ts,
                    "source": "skill_repair",
                    "system_type": "skill_repair",
                    "task_id": task_id,
                }
                broadcast_ws_sync(payload)
                log_chat(
                    "system",
                    0,
                    0,
                    visible_text,
                    ts=ts,
                    source="skill_repair",
                    task_id=task_id,
                )
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return json_exception(exc, 400)


async def api_git_log(_request: Request) -> JSONResponse:
    """Return recent commits, tags, and current branch/sha."""
    try:
        from supervisor.git_ops import git_capture, list_commits, list_versions

        commits = list_commits(max_count=30)
        tags = list_versions(max_count=20)
        rc, branch, _ = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        rc2, sha, _ = git_capture(["git", "rev-parse", "--short", "HEAD"])
        return JSONResponse({
            "commits": commits,
            "tags": tags,
            "branch": branch.strip() if rc == 0 else "unknown",
            "sha": sha.strip() if rc2 == 0 else "",
        })
    except Exception as exc:
        return json_exception(exc)


async def api_git_rollback(request: Request) -> JSONResponse:
    """Roll back to a specific commit or tag, then restart."""
    try:
        body = await request.json()
        target = body.get("target", "").strip()
        if not target:
            return json_error("missing target", 400)
        from supervisor.git_ops import rollback_to_version

        ok, msg = rollback_to_version(target, reason="ui_rollback")
        if not ok:
            return json_error(msg, 400)
        _request_restart(request)
        return JSONResponse({"status": "ok", "message": msg})
    except Exception as exc:
        return json_exception(exc)


async def api_git_promote(request: Request) -> JSONResponse:
    """Promote the current dev branch to the runtime's stable branch."""
    try:
        import subprocess as sp

        branch_dev, branch_stable = _runtime_branch_defaults(request)
        sp.run(
            ["git", "branch", "-f", branch_stable, branch_dev],
            cwd=str(request_repo_dir(request)),
            check=True,
            capture_output=True,
        )
        return JSONResponse({"status": "ok", "message": f"{branch_stable} updated to match {branch_dev}"})
    except Exception as exc:
        return json_exception(exc)


async def api_update_status(_request: Request) -> JSONResponse:
    """Return passive managed-update status without fetching."""
    try:
        return JSONResponse(_managed_update_payload(fetch=False, include_tags=False))
    except Exception as exc:
        return json_exception(exc)


async def api_update_check(_request: Request) -> JSONResponse:
    """Fetch the managed remote and return fresh update status."""
    try:
        return JSONResponse(_managed_update_payload(fetch=True, include_tags=True))
    except Exception as exc:
        return json_exception(exc)


async def api_update_apply(request: Request) -> JSONResponse:
    """Prepare a managed update and restart so safe_restart applies it."""
    body = await request_json_or(request, {}, exceptions=(Exception,))
    try:
        strategy = str(body.get("strategy") or "replace")
        from supervisor.git_ops import BRANCH_DEV, _clear_update_intent, checkout_and_reset, prepare_managed_update

        ok, payload = prepare_managed_update(strategy)
        if not ok:
            return JSONResponse(payload, status_code=409)
        try:
            checkout_ok, checkout_msg = checkout_and_reset(
                BRANCH_DEV,
                reason="ui_update_apply",
                unsynced_policy="ignore",
            )
        except Exception as checkout_exc:
            _clear_update_intent()
            return JSONResponse(
                {"error": f"Prepared update but checkout failed: {checkout_exc}", **payload},
                status_code=409,
            )
        if not checkout_ok:
            _clear_update_intent()
            return JSONResponse(
                {"error": f"Prepared update but checkout failed: {checkout_msg}", **payload},
                status_code=409,
            )
        _request_restart(request)
        return JSONResponse({"status": "ok", "restarting": True, **payload})
    except Exception as exc:
        return json_exception(exc)


async def api_evolution_data(request: Request) -> JSONResponse:
    """Collect evolution metrics for each git tag."""
    from ouroboros.utils import collect_evolution_metrics

    global _evo_task
    now = time.time()
    force_refresh = str(request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
    if not force_refresh and _evo_cache.get("ts") and now - _evo_cache["ts"] < 60:
        return JSONResponse({
            "points": _evo_cache["points"],
            "generated_at": _evo_cache.get("generated_at", ""),
            "cached": True,
        })
    if _evo_task is None or _evo_task.done():
        _evo_task = asyncio.create_task(
            collect_evolution_metrics(
                str(request_repo_dir(request)),
                data_dir=str(request_drive_root(request)),
            )
        )
    data_points = await _evo_task
    _evo_cache["ts"] = time.time()
    _evo_cache["points"] = data_points
    _evo_cache["generated_at"] = utc_now_iso()
    return JSONResponse({
        "points": data_points,
        "generated_at": _evo_cache["generated_at"],
        "cached": False,
    })


__all__ = [
    "api_command",
    "api_evolution_data",
    "api_git_log",
    "api_git_promote",
    "api_git_rollback",
    "api_reset",
    "api_update_apply",
    "api_update_check",
    "api_update_status",
]

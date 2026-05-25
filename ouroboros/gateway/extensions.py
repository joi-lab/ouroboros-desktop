"""HTTP endpoints for extension catalogue, manifests, modules, and dispatch."""

from __future__ import annotations

import asyncio
import inspect
import logging
import pathlib
import shutil
from datetime import datetime, timezone
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.extension_loader import list_routes, snapshot
from ouroboros.gateway._helpers import (
    coerce_bool,
    json_error,
    json_exception,
    request_drive_root as _request_drive_root,
    request_json_or,
    request_repo_dir as _request_repo_dir,
)
from ouroboros.skill_lifecycle_queue import (
    LifecycleJobOptions,
    queue_snapshot,
    run_blocking_preserving_cancellation,
    run_lifecycle_job,
)
from ouroboros.skill_loader import (
    discover_skills,
    find_skill,
    grant_status_for_skill,
    requested_core_setting_keys,
    requested_skill_permissions,
    review_status_allows_execution,
    save_skill_grants,
    skill_review_gate,
    _sanitize_skill_name,
)
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)


def _review_fields(loaded: Any, *, stale: bool | None = None, gate: dict[str, Any] | None = None) -> dict[str, Any]:
    stale = loaded.review.is_stale_for(loaded.content_hash) if stale is None else stale
    gate = skill_review_gate(loaded.review.status, stale=stale) if gate is None else gate
    return {
        "review_status": loaded.review.status,
        "review_stale": stale,
        "review_gate": gate,
        "executable_review": gate["executable_review"],
    }


def _broadcast_extension_lifecycle(request: Request, skill: str, action: Any, reason: Any = "") -> None:
    if not action:
        return
    try:
        broadcaster = getattr(request.app.state, "broadcast_ws_sync", None)
    except Exception:
        broadcaster = None
    if not callable(broadcaster):
        return
    broadcaster({
        "type": "extension_lifecycle",
        "skill": str(skill or ""),
        "action": str(action or ""),
        "reason": str(reason or ""),
    })


def _owner_grant_audit(drive_root: pathlib.Path, request: Request, payload: Dict[str, Any]) -> None:
    try:
        client = getattr(request, "client", None)
        append_jsonl(
            pathlib.Path(drive_root) / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "owner_api_action",
                "action": "skill_grant",
                "client_host": str(getattr(client, "host", "") or ""),
                "skill": str(payload.get("skill") or ""),
                "granted_key_count": int(payload.get("granted_key_count") or 0),
                "granted_permission_count": int(payload.get("granted_permission_count") or 0),
                "extension_action": str(payload.get("extension_action") or ""),
                "extension_reason": str(payload.get("extension_reason") or ""),
            },
        )
    except Exception:
        log.debug("Failed to write owner grant audit event", exc_info=True)


def _grant_items_from_body(body: Dict[str, Any]) -> list[str]:
    raw = body.get("items")
    if raw is None:
        raw = body.get("keys")
    if raw is None:
        raw = body.get("granted_keys")
    if raw is None:
        return []
    out: list[str] = []
    values = raw if isinstance(raw, list) else [raw]
    for item in values:
        if isinstance(item, dict):
            value = item.get("value") or item.get("key") or item.get("permission") or item.get("name")
        else:
            value = item
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    return out


async def api_extensions_index(request: Request) -> JSONResponse:
    """Return discovered extensions plus live loader snapshot.

    The synchronous body runs in a worker thread and reuses discovered skills
    to avoid repeated filesystem walks during Widgets/Skills refresh.
    """
    try:
        import asyncio

        from ouroboros.config import get_skills_repo_path
        from ouroboros.skill_review_runner import reconcile_stale_review_jobs

        drive_root = _request_drive_root(request)
        repo_path = get_skills_repo_path()
        await asyncio.to_thread(reconcile_stale_review_jobs, drive_root)
        payload = await asyncio.to_thread(_build_extensions_index, drive_root, repo_path)
        return JSONResponse(payload)
    except Exception as exc:
        log.exception("api_extensions_index failure")
        return json_exception(exc)


def _build_extensions_index(drive_root, repo_path):
    """Threaded, request-scope-free body for ``GET /api/extensions``."""
    from ouroboros.extension_loader import extension_name_prefix, runtime_state_for_loaded_skill

    live_snapshot = snapshot()
    # Scan data plane plus optional external checkout; bootstrap copies native refs.
    skills = discover_skills(drive_root, repo_path=repo_path)
    runtime_states = {
        s.name: runtime_state_for_loaded_skill(s, drive_root)
        for s in skills
        if s.manifest.is_extension()
    }

    def _live_tool_count(skill_name: str) -> int:
        prefix = extension_name_prefix(skill_name)
        return sum(1 for name in live_snapshot.get("tools", []) if str(name).startswith(prefix))

    def _live_route_count(skill_name: str) -> int:
        prefix = f"/api/extensions/{skill_name}/"
        return sum(1 for name in live_snapshot.get("routes", []) if str(name).startswith(prefix))

    def _live_ws_count(skill_name: str) -> int:
        prefix = extension_name_prefix(skill_name)
        return sum(1 for name in live_snapshot.get("ws_handlers", []) if str(name).startswith(prefix))

    def _pending_ui_tabs(skill_name: str) -> list[str]:
        prefix = f"{skill_name}:"
        return [
            str(name)
            for name in live_snapshot.get("ui_tabs_pending", [])
            if str(name).startswith(prefix)
        ]

    # Inline ClawHub provenance so Installed UI avoids a second round-trip.
    try:
        from ouroboros.marketplace.provenance import read_provenance
    except Exception:  # pragma: no cover — defensive
        read_provenance = lambda *_a, **_kw: None  # type: ignore[assignment]
    marketplace_enabled = True

    catalog = []

    def _path_installed_at(skill_dir: pathlib.Path) -> str:
        candidates = [skill_dir / "SKILL.md", skill_dir / "plugin.py", skill_dir]
        stamps: list[float] = []
        for candidate in candidates:
            try:
                if candidate.exists():
                    stamps.append(candidate.stat().st_mtime)
            except OSError:
                continue
        if not stamps:
            return ""
        return datetime.fromtimestamp(min(stamps), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    for s in skills:
        payload_root = ""
        try:
            rel_skill_dir = s.skill_dir.resolve().relative_to(drive_root.resolve())
            if rel_skill_dir.parts[:1] == ("skills",):
                payload_root = rel_skill_dir.as_posix()
        except Exception:
            payload_root = ""
        entry = {
            "name": s.name,
            "type": s.manifest.type,
            "version": s.manifest.version,
            "description": s.manifest.description,
            "enabled": s.enabled,
            **_review_fields(s),
            "permissions": list(s.manifest.permissions or []),
            "load_error": runtime_states.get(s.name, {}).get("load_error", s.load_error),
            "desired_live": runtime_states.get(s.name, {}).get("desired_live", False),
            "live_loaded": runtime_states.get(s.name, {}).get("live_loaded", False),
            "live_reason": runtime_states.get(s.name, {}).get("reason", "not_extension"),
            "dispatch_live": bool(
                _live_tool_count(s.name)
                or _live_route_count(s.name)
                or _live_ws_count(s.name)
            ),
            "ui_tabs_pending": _pending_ui_tabs(s.name),
            "review_findings": list(s.review.findings or []),
            "grants": grant_status_for_skill(drive_root, s),
            "is_self_authored": bool(getattr(s, "is_self_authored", False)),
            # Keep source explicit so marketplace skills are not mislabeled native.
            "source": s.source,
            "payload_root": payload_root,
            "installed_at": _path_installed_at(s.skill_dir),
        }
        if s.source == "clawhub":
            try:
                prov = read_provenance(drive_root, s.name) or {}
            except Exception:  # pragma: no cover
                prov = {}
            if prov:
                if prov.get("installed_at"):
                    entry["installed_at"] = str(prov.get("installed_at") or "")
                entry["provenance"] = {
                    "slug": prov.get("slug", ""),
                    "version": prov.get("version", ""),
                    "sha256": prov.get("sha256", ""),
                    "adapter_version": prov.get("adapter_version", ""),
                    "openclaw_compat": dict(prov.get("openclaw_compat") or {}),
                    "installed_at": prov.get("installed_at", ""),
                    "updated_at": prov.get("updated_at", ""),
                }
                if marketplace_enabled:
                    entry["provenance"].update({
                        "homepage": prov.get("homepage", ""),
                        "license": prov.get("license", ""),
                        "primary_env": prov.get("primary_env", ""),
                        "adapter_warnings": list(prov.get("adapter_warnings") or []),
                        "original_manifest_sha256": prov.get("original_manifest_sha256", ""),
                        "translated_manifest_sha256": prov.get("translated_manifest_sha256", ""),
                        "registry_url": prov.get("registry_url", ""),
                    })
        catalog.append(entry)
    return {"skills": catalog, "live": live_snapshot}


async def api_extension_manifest(request: Request) -> JSONResponse:
    """GET /api/extensions/<skill>/manifest — raw manifest metadata."""
    from ouroboros.config import get_skills_repo_path
    from ouroboros.extension_loader import runtime_state_for_skill_name

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return json_error("missing skill name", 400)
    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    loaded = await asyncio.to_thread(find_skill, drive_root, skill_name, repo_path=repo_path)
    if loaded is None:
        return json_error("skill not found", 404)
    runtime_state = await asyncio.to_thread(
        runtime_state_for_skill_name,
        skill_name,
        drive_root,
        repo_path=repo_path,
    )
    load_error = runtime_state.get("load_error")
    if not isinstance(load_error, str) or not load_error.strip():
        load_error = loaded.load_error
    return JSONResponse(
        {
            "name": loaded.name,
            "manifest": {
                "name": loaded.manifest.name,
                "description": loaded.manifest.description,
                "version": loaded.manifest.version,
                "type": loaded.manifest.type,
                "entry": loaded.manifest.entry,
                "permissions": list(loaded.manifest.permissions or []),
                "env_from_settings": list(loaded.manifest.env_from_settings or []),
                "ui_tab": loaded.manifest.ui_tab,
            },
            "enabled": loaded.enabled,
            **_review_fields(loaded),
            "content_hash": loaded.content_hash,
            "load_error": load_error,
        }
    )


async def api_extension_module(request: Request) -> Response:
    """Serve reviewed widget module JS only for live registered tab entries."""
    from ouroboros.config import get_skills_repo_path
    from ouroboros.extension_loader import runtime_state_for_skill_name

    skill_name = str(request.path_params.get("skill") or "").strip()
    entry = str(request.path_params.get("entry") or "").strip()
    if not skill_name or not entry:
        return json_error("missing skill/module entry", 400)
    if "/" in entry or "\\" in entry or ".." in entry or entry.startswith("."):
        return json_error("invalid module entry", 400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    state = await asyncio.to_thread(
        runtime_state_for_skill_name,
        skill_name,
        drive_root,
        repo_path=repo_path,
    )
    if not state.get("desired_live"):
        return json_error(f"extension {skill_name!r} not live: {state.get('reason')}", 409, state=state)
    loaded = await asyncio.to_thread(find_skill, drive_root, skill_name, repo_path=repo_path)
    if loaded is None:
        return json_error("skill not found", 404)
    # Authorize against live PluginAPI tab registrations, not only manifest ui_tab.
    live = snapshot()
    module_declared = any(
        str(tab.get("skill") or "") == skill_name
        and str((tab.get("render") or {}).get("kind") or "") == "module"
        and str((tab.get("render") or {}).get("entry") or "") == entry
        for tab in live.get("ui_tabs", [])
    )
    if not module_declared:
        return json_error("module entry is not declared by a live widget tab", 404)
    target = (loaded.skill_dir / entry).resolve()
    try:
        target.relative_to(loaded.skill_dir.resolve())
    except ValueError:
        return json_error("module entry escapes skill directory", 400)
    if not target.is_file():
        return json_error("module entry file not found", 404)
    try:
        text = await asyncio.to_thread(target.read_text, encoding="utf-8")
    except UnicodeDecodeError:
        return json_error("module entry is not UTF-8 text", 400)
    return Response(
        text,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def api_extension_settings_section(request: Request) -> JSONResponse:
    """Return declarative Settings sections registered by one extension."""
    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return json_error("missing skill name", 400)
    live = snapshot()
    sections = [
        item
        for item in live.get("settings_sections", [])
        if str(item.get("skill") or "") == skill_name
    ]
    return JSONResponse({"skill": skill_name, "sections": sections})


async def api_extension_dispatch(request: Request) -> Response:
    """Dispatch an extension route after reconciling live loader state."""
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros.extension_loader import reconcile_extension, runtime_state_for_skill_name

    skill = str(request.path_params.get("skill") or "").strip()
    rest = str(request.path_params.get("rest") or "").strip()
    mount = f"/api/extensions/{skill}/{rest}"
    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    spec = list_routes().get(mount)
    if spec is None and skill:
        state = await asyncio.to_thread(
            runtime_state_for_skill_name,
            skill,
            drive_root,
            repo_path=repo_path,
        )
        if state.get("desired_live"):
            state = await asyncio.to_thread(
                reconcile_extension,
                skill,
                drive_root,
                load_settings,
                repo_path=repo_path,
            )
            spec = list_routes().get(mount)
            if spec is None and state.get("action") == "extension_load_error":
                return json_error(f"extension {skill!r} failed to go live", 409, state=state)
        elif state.get("reason") != "missing":
            return json_error(f"extension {skill!r} not live: {state.get('reason')}", 409, state=state)
    if spec is None:
        return json_error(f"no extension route registered for {mount!r}", 404)
    state = await asyncio.to_thread(
        runtime_state_for_skill_name,
        str(spec.get("skill") or skill),
        drive_root,
        repo_path=repo_path,
    )
    if not state.get("desired_live") or not state.get("live_loaded"):
        state = await asyncio.to_thread(
            reconcile_extension,
            skill,
            drive_root,
            load_settings,
            repo_path=repo_path,
        )
        spec = list_routes().get(mount)
        if state.get("action") == "extension_load_error":
            return json_error(f"extension {skill!r} failed to go live", 409, state=state)
    if not state.get("desired_live") or not state.get("live_loaded"):
        return json_error(f"extension {skill!r} not live: {state.get('reason')}", 409, state=state)
    if spec is None:
        return json_error(f"no extension route registered for {mount!r}", 404)
    method = request.method.upper()
    allowed = {m.upper() for m in spec.get("methods", ("GET",))}
    if "GET" in allowed:
        allowed.add("HEAD")
    if method not in allowed:
        return json_error(f"method {method} not allowed; allowed={sorted(allowed)}", 405)
    handler = spec.get("handler")
    if not callable(handler):
        return json_error("registered handler is not callable")
    try:
        if inspect.iscoroutinefunction(handler):
            result = await handler(request)
        else:
            result = await asyncio.to_thread(handler, request)
        if inspect.iscoroutine(result):
            result = await result
    except Exception as exc:
        log.exception("extension dispatch failure: %s", mount)
        return json_error(f"{type(exc).__name__}: {exc}")
    if isinstance(result, Response):
        return result
    return JSONResponse(result if result is not None else {})


async def api_skill_toggle(request: Request) -> JSONResponse:
    """Toggle a skill from the UI and run extension load/unload reconciliation."""
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros.skill_loader import find_skill, grant_status_for_skill, save_enabled
    from ouroboros import extension_loader

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return json_error("missing skill name", 400)
    body = await request_json_or(request, {}, exceptions=(Exception,))
    bool_sentinel = object()
    enabled = coerce_bool(body.get("enabled"), default=bool_sentinel)
    if enabled is bool_sentinel:
        return json_error("'enabled' must be a boolean", 400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()

    initial = await asyncio.to_thread(find_skill, drive_root, skill_name, repo_path=repo_path)
    if initial is None:
        return json_error("skill not found", 404)
    def _run_toggle_sync() -> dict[str, Any]:
        loaded = find_skill(drive_root, skill_name, repo_path=repo_path)
        if loaded is None:
            return {"error": "skill not found", "status_code": 404}
        collision_load_error = loaded.load_error.lower().startswith("skill name collision:")
        if enabled and loaded.load_error:
            return {"error": f"cannot enable: {loaded.load_error}", "status_code": 400}
        if enabled:
            stale = loaded.review.is_stale_for(loaded.content_hash)
            grants = grant_status_for_skill(drive_root, loaded)
            gate = skill_review_gate(loaded.review.status, stale=stale)
            if not gate["executable_review"]:
                return {
                    "error": "cannot enable until review status is a fresh executable review",
                    "status_code": 409,
                    **_review_fields(loaded, stale=stale, gate=gate),
                    "grants": grants,
                }
            if not grants.get("all_granted", True):
                return {
                    "error": "cannot enable until requested key and permission grants are approved",
                    "status_code": 409,
                    **_review_fields(loaded, stale=stale, gate=gate),
                    "grants": grants,
                }
            # Mirror toggle_skill's isolated-dependency enable guard for the UI.
            try:
                from ouroboros.marketplace.install_specs import install_specs_hash
                from ouroboros.marketplace.isolated_deps import read_deps_state
                from ouroboros.skill_dependencies import auto_install_specs_for_skill

                auto_specs = auto_install_specs_for_skill(drive_root, loaded)
                if auto_specs:
                    deps_state = read_deps_state(drive_root, loaded.name, loaded.skill_dir)
                    deps_status = str(deps_state.get("status") or "pending")
                    expected_hash = install_specs_hash(auto_specs)
                    actual_hash = str(deps_state.get("specs_hash") or "")
                    if deps_status != "installed":
                        return {
                            "error": "cannot enable until isolated dependencies are installed",
                            "status_code": 409,
                            "deps_status": deps_status,
                            "deps_error": deps_state.get("error", ""),
                            **_review_fields(loaded, stale=stale, gate=gate),
                            "grants": grants,
                        }
                    if actual_hash != expected_hash:
                        return {
                            "error": "cannot enable until isolated dependency fingerprint is refreshed",
                            "status_code": 409,
                            "deps_status": "stale",
                            **_review_fields(loaded, stale=stale, gate=gate),
                            "grants": grants,
                        }
            except Exception:
                log.debug("api_skill_toggle deps probe failed", exc_info=True)
        if not enabled and collision_load_error:
            action = None
            if loaded.name in extension_loader.snapshot()["extensions"]:
                extension_loader.unload_extension(loaded.name)
                action = "extension_unloaded"
            return {
                "error": (
                    "cannot persist disable because this skill's sanitized "
                    "name collides with another skill directory; rename one "
                    "of the directories first"
                ),
                "status_code": 400,
                "extension_action": action,
                "extension_reason": "name_collision",
            }
        save_enabled(drive_root, loaded.name, enabled)
        action = None
        live_reason = "not_extension"
        if loaded.manifest.is_extension() or loaded.name in extension_loader.snapshot()["extensions"]:
            state = extension_loader.reconcile_extension(
                loaded.name,
                drive_root,
                load_settings,
                repo_path=repo_path,
                retry_load_error=True,
            )
            action = state.get("action")
            live_reason = str(state.get("reason") or "")
        return {
            "skill": loaded.name,
            "source": loaded.source,
            **_review_fields(loaded),
            "grants": grant_status_for_skill(drive_root, loaded),
            "action": action,
            "live_reason": live_reason,
        }

    async def _run_toggle() -> dict[str, Any]:
        return await run_blocking_preserving_cancellation(
            _run_toggle_sync,
            log_label="skill toggle lifecycle operation",
        )

    queued = await run_lifecycle_job(
        kind="enable" if enabled else "disable",
        target=initial.name,
        source=initial.source,
        message=("Enabling" if enabled else "Disabling") + f" {initial.name}",
        runner=_run_toggle,
        options=LifecycleJobOptions(
            drive_root=drive_root,
            result_message=lambda item: (
                item.get("error", "")
                or (("Enabled" if enabled else "Disabled") + f" {item.get('skill', initial.name)}")
            ),
            result_error=lambda item: item.get("error", ""),
        ),
    )
    if queued.get("error"):
        return JSONResponse(queued, status_code=int(queued.get("status_code") or 400))
    _broadcast_extension_lifecycle(
        request,
        str(queued.get("skill") or initial.name),
        queued.get("action"),
        queued.get("live_reason"),
    )
    return JSONResponse(
        {
            "skill": queued.get("skill", initial.name),
            "enabled": enabled,
            "review_status": queued.get("review_status"),
            "review_stale": queued.get("review_stale"),
            "review_gate": queued.get("review_gate"),
            "executable_review": queued.get("executable_review"),
            "grants": queued.get("grants", {}),
            "extension_action": queued.get("action"),
            "extension_reason": queued.get("live_reason"),
        }
    )


class _ApiReviewCtx:
    """Minimal ToolContext-compatible carrier for HTTP-triggered review."""

    def __init__(self, drive_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        self.drive_root = drive_root
        self.repo_dir = repo_dir
        self.task_id = "api_skill_review"
        self.current_chat_id = 0
        self.pending_events: list = []
        self.emit_progress_fn = None
        self.event_queue = None  # _emit_usage_event falls back to pending_events
        self.messages: list = []


async def api_skill_review(request: Request) -> JSONResponse:
    """Queue tri-model skill review from the UI without blocking the event loop."""
    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return json_error("missing skill name", 400)

    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    ctx = _ApiReviewCtx(drive_root, repo_dir)
    from ouroboros.skill_review_runner import run_skill_review_lifecycle
    from ouroboros.skill_review import review_skill as _review_skill_impl

    payload = await run_skill_review_lifecycle(
        ctx,
        skill_name,
        source="skills",
        review_impl=_review_skill_impl,
    )
    return JSONResponse(payload)


async def api_skill_lifecycle_queue(request: Request) -> JSONResponse:
    """GET /api/skills/lifecycle-queue — recent mutating skill operations."""

    try:
        from ouroboros.skill_review_runner import reconcile_stale_review_jobs

        await asyncio.to_thread(reconcile_stale_review_jobs, _request_drive_root(request))
    except Exception:
        log.debug("stale review job reconciliation failed", exc_info=True)
    return JSONResponse(queue_snapshot())


async def api_skill_grants(request: Request) -> JSONResponse:
    """Owner grant path for reviewed skill settings keys and host permissions."""
    from ouroboros import extension_loader
    from ouroboros.config import get_skills_repo_path, load_settings

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return json_error("missing skill name", 400)
    body = await request_json_or(request, {}, exceptions=(Exception,))
    if not isinstance(body, dict):
        return json_error("request body must be a JSON object", 400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()

    def _save_grants_sync() -> dict[str, Any]:
        loaded = find_skill(drive_root, skill_name, repo_path=repo_path)
        if loaded is None:
            return {"error": "skill not found", "status_code": 404}
        if not (loaded.manifest.is_script() or loaded.manifest.is_extension()):
            return {
                "error": "key and permission grants are supported for script and extension skills",
                "status_code": 400,
            }
        stale = loaded.review.is_stale_for(loaded.content_hash)
        gate = skill_review_gate(loaded.review.status, stale=stale)
        if not review_status_allows_execution(loaded.review.status) or stale:
            return {
                "error": "key and permission grants require a fresh executable review",
                "status_code": 409,
                **_review_fields(loaded, stale=stale, gate=gate),
                "grants": grant_status_for_skill(drive_root, loaded),
            }
        allowed_keys = requested_core_setting_keys(list(loaded.manifest.env_from_settings or []))
        allowed_permissions = requested_skill_permissions(
            list(getattr(loaded.manifest, "permissions", []) or []),
            list(getattr(loaded.manifest, "subscribe_events", []) or []),
        )
        permission_map = {permission.lower(): permission for permission in allowed_permissions}
        requested_raw = _grant_items_from_body(body)
        requested_keys: list[str] = []
        requested_permissions: list[str] = []
        rejected: list[str] = []
        for item in requested_raw:
            key = item.upper()
            permission = permission_map.get(item.lower())
            if key in allowed_keys:
                if key not in requested_keys:
                    requested_keys.append(key)
            elif permission:
                if permission not in requested_permissions:
                    requested_permissions.append(permission)
            else:
                rejected.append(item)
        if not requested_raw or rejected or (not requested_keys and not requested_permissions):
            return {
                "error": (
                    "grant items must be requested by the current manifest; "
                    f"allowed keys={allowed_keys}, permissions={allowed_permissions}"
                ),
                "status_code": 400,
                "allowed_keys": allowed_keys,
                "allowed_permissions": allowed_permissions,
                "rejected_items": rejected,
            }
        save_skill_grants(
            drive_root,
            loaded.name,
            requested_keys,
            content_hash=loaded.content_hash,
            requested_keys=allowed_keys,
            granted_permissions=requested_permissions,
            requested_permissions=allowed_permissions,
        )
        extension_action = None
        extension_reason = None
        extension_load_error = None
        if loaded.manifest.is_extension():
            try:
                state = extension_loader.reconcile_extension(
                    loaded.name,
                    drive_root,
                    load_settings,
                    repo_path=repo_path,
                    retry_load_error=True,
                )
                extension_action = state.get("action")
                extension_reason = state.get("reason")
                extension_load_error = state.get("load_error")
            except Exception as exc:
                log.warning(
                    "Skill grant saved but extension reconcile failed for %s: %s",
                    loaded.name,
                    exc,
                    exc_info=True,
                )
                extension_reason = "reconcile_call_failed"
                extension_load_error = str(exc)
        refreshed = find_skill(drive_root, loaded.name, repo_path=repo_path) or loaded
        return {
            "ok": True,
            "skill": loaded.name,
            "granted_keys": requested_keys,
            "granted_permissions": requested_permissions,
            "extension_action": extension_action,
            "extension_reason": extension_reason,
            "load_error": extension_load_error,
            "grants": grant_status_for_skill(drive_root, refreshed),
        }

    result = await asyncio.to_thread(_save_grants_sync)
    if result.get("error"):
        return JSONResponse(result, status_code=int(result.get("status_code") or 400))
    _owner_grant_audit(
        drive_root,
        request,
        {
            "skill": result.get("skill"),
            "granted_key_count": len(result.get("granted_keys") or []),
            "granted_permission_count": len(result.get("granted_permissions") or []),
            "extension_action": result.get("extension_action"),
            "extension_reason": result.get("extension_reason"),
        },
    )
    _broadcast_extension_lifecycle(
        request,
        str(result.get("skill") or skill_name),
        result.get("extension_action"),
        result.get("extension_reason"),
    )
    return JSONResponse(result)


async def api_skill_reconcile(request: Request) -> JSONResponse:
    """Re-run the extension load gate after launcher-owned grants change."""
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros import extension_loader

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return json_error("missing skill name", 400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    state = await asyncio.to_thread(
        extension_loader.reconcile_extension,
        skill_name,
        drive_root,
        load_settings,
        repo_path=repo_path,
        retry_load_error=True,
    )
    _broadcast_extension_lifecycle(
        request,
        skill_name,
        state.get("action"),
        state.get("reason"),
    )
    return JSONResponse(
        {
            "skill": skill_name,
            "extension_action": state.get("action"),
            "extension_reason": state.get("reason"),
            "live_loaded": bool(state.get("live_loaded")),
            "load_error": state.get("load_error"),
        }
    )


async def api_skill_delete(request: Request) -> JSONResponse:
    """Delete a local data-plane skill payload and its durable state."""
    from ouroboros.config import get_skills_repo_path
    from ouroboros import extension_loader

    skill_name = _sanitize_skill_name(str(request.path_params.get("skill") or "").strip())
    if not skill_name or skill_name == "_unnamed":
        return json_error("missing skill name", 400)
    body = await request_json_or(request, {}, exceptions=(Exception,))

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()

    def _run_delete_sync() -> dict[str, Any]:
        requested_root = str(body.get("payload_root") or f"skills/external/{skill_name}").strip()
        root_parts = pathlib.PurePosixPath(requested_root).parts
        if len(root_parts) != 3 or root_parts[:2] != ("skills", "external"):
            return {"error": "local skill delete requires payload_root=skills/external/<name>", "status_code": 403}

        drive_root_path = pathlib.Path(drive_root).absolute()
        skills_root = drive_root_path / "skills"
        external_root = skills_root / "external"
        payload_dir = external_root / root_parts[2]
        if skills_root.is_symlink() or external_root.is_symlink() or payload_dir.is_symlink():
            return {"error": "local skill delete refuses symlinked data/skills/external payloads", "status_code": 403}

        skills = discover_skills(drive_root, repo_path=repo_path)
        loaded = next((item for item in skills if pathlib.Path(item.skill_dir).absolute() == payload_dir), None)
        if loaded is None:
            return {"error": f"skill {skill_name!r} not found at {requested_root}", "status_code": 404}
        if loaded.name != skill_name or loaded.source not in {"self_authored", "external"}:
            return {"error": "local skill delete is limited to self-authored/external skills", "status_code": 403}
        if any(item.name == skill_name and pathlib.Path(item.skill_dir).absolute() != payload_dir for item in skills):
            return {
                "error": (
                    "refusing to delete a local skill while another skill uses the same sanitized name; "
                    "rename one of the colliding skills first"
                ),
                "status_code": 409,
            }

        state_root = (drive_root_path / "state" / "skills").absolute()
        state_dir = state_root / loaded.name
        if state_root.is_symlink() or state_dir.is_symlink():
            return {"error": f"refusing to delete unsafe state path for {loaded.name!r}", "status_code": 500}
        try:
            state_dir.relative_to(state_root)
        except ValueError:
            return {"error": f"refusing to delete unsafe state path for {loaded.name!r}", "status_code": 500}

        extension_loader.unload_extension(loaded.name)
        shutil.rmtree(payload_dir)
        deleted_state = state_dir.exists()
        if deleted_state:
            shutil.rmtree(state_dir)
        if payload_dir.exists() or state_dir.exists():
            return {"error": f"failed to fully delete local skill {loaded.name!r}", "status_code": 500}
        return {
            "ok": True,
            "skill": loaded.name,
            "source": loaded.source,
            "deleted_payload_root": f"skills/external/{root_parts[2]}",
            "deleted_state": deleted_state,
            "extension_action": "extension_unloaded",
            "extension_reason": "deleted",
        }

    queued = await run_lifecycle_job(
        kind="delete",
        target=skill_name,
        source="external",
        message=f"Deleting {skill_name}",
        runner=lambda: run_blocking_preserving_cancellation(
            _run_delete_sync,
            log_label="local skill delete lifecycle operation",
        ),
        options=LifecycleJobOptions(
            drive_root=drive_root,
            result_message=lambda item: item.get("error", "") or f"Deleted {item.get('skill', skill_name)}",
            result_error=lambda item: item.get("error", ""),
        ),
    )
    if queued.get("error"):
        return JSONResponse(queued, status_code=int(queued.get("status_code") or 400))
    _broadcast_extension_lifecycle(
        request,
        str(queued.get("skill") or skill_name),
        queued.get("extension_action"),
        queued.get("extension_reason"),
    )
    return JSONResponse(queued)


__all__ = [
    "api_extensions_index",
    "api_extension_manifest",
    "api_extension_module",
    "api_extension_settings_section",
    "api_extension_dispatch",
    "api_skill_delete",
    "api_skill_toggle",
    "api_skill_review",
    "api_skill_grants",
    "api_skill_reconcile",
]

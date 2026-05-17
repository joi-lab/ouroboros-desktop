"""HTTP surface for the ClawHub marketplace (v4.50).

Endpoints:

- ``GET  /api/marketplace/clawhub/search?q=&official=&limit=&cursor=``
- ``GET  /api/marketplace/clawhub/info/{slug}``
- ``GET  /api/marketplace/clawhub/installed`` — local catalog snapshot
- ``POST /api/marketplace/clawhub/install``     ``{slug, version?, auto_review?, overwrite?}``
- ``POST /api/marketplace/clawhub/update/{name}``   ``{version?}``
- ``POST /api/marketplace/clawhub/uninstall/{name}``
- ``GET  /api/marketplace/clawhub/preview/{slug}`` — lightweight registry preview

Every mutating endpoint defers the heavy work to ``asyncio.to_thread``
so the Starlette event loop stays responsive while the registry HTTP
+ stage + adapter + skill_review pipeline runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.marketplace.clawhub import (
    ClawHubClientError,
    ClawHubClientHostBlocked,
    info as _registry_info,
    search as _registry_search,
)
from ouroboros.marketplace.install import (
    _run_skill_review,
    install_skill,
    uninstall_skill,
    update_skill,
)
from ouroboros.marketplace.provenance import read_provenance
from ouroboros.marketplace import ouroboroshub
from ouroboros.skill_lifecycle_queue import (
    JobProgressTarget,
    LifecycleJobOptions,
    run_blocking_preserving_cancellation,
    run_lifecycle_job,
)

log = logging.getLogger(__name__)


def _reconcile_deps_after_review(drive_root: pathlib.Path, skill_name: str) -> tuple[str, str]:
    from ouroboros.skill_review_runner import _reconcile_deps_after_pass_review

    return _reconcile_deps_after_pass_review(drive_root, skill_name)


def _review_status_allows_skill_runtime(status: str) -> bool:
    from ouroboros.skill_loader import review_status_allows_execution

    return review_status_allows_execution(status)


from ouroboros.gateway._helpers import (
    coerce_bool as _coerce_bool,
    coerce_int as _coerce_int,
    request_drive_root as _request_drive_root,
    request_repo_dir as _request_repo_dir,
)


def _client_error_response(exc: Exception, *, default_status: int = 502) -> JSONResponse:
    """Map a registry-client exception to a JSON error response."""
    if isinstance(exc, ClawHubClientHostBlocked):
        status = 400
    elif isinstance(exc, ClawHubClientError):
        status = default_status
    else:
        status = 500
    log.warning("marketplace error: %s", exc, exc_info=True)
    return JSONResponse({"error": str(exc), "code": exc.__class__.__name__}, status_code=status)


# ---------------------------------------------------------------------------
# Search / info / preview
# ---------------------------------------------------------------------------


async def api_marketplace_search(request: Request) -> JSONResponse:
    qp = request.query_params
    query = qp.get("q") or qp.get("query") or ""
    sort = qp.get("sort") or "registry"
    limit = _coerce_int(qp.get("limit"), 25)
    include_plugins = _coerce_bool(qp.get("include_plugins"), False)
    official_only = _coerce_bool(qp.get("official") or qp.get("only_official"), False)
    cursor = qp.get("cursor") or None
    is_text_search = bool(str(query or "").strip())
    effective_cursor = None if is_text_search else cursor
    registry_official_only = False if is_text_search else official_only
    try:
        page = await asyncio.to_thread(
            _registry_search,
            query,
            limit=limit,
            sort=sort,
            cursor=effective_cursor,
            official_only=registry_official_only,
            include_metadata=True,
            timeout_sec=15 if is_text_search else 5,
        )
    except Exception as exc:
        return _client_error_response(exc)
    results = list(page.get("results") or [])
    if not include_plugins:
        results = [r for r in results if not r.is_plugin]
    if is_text_search and official_only:
        results = [r for r in results if bool((r.badges or {}).get("official"))]
    return JSONResponse(
        {
            "query": query,
            "sort": sort,
            "limit": limit,
            "offset": 0,
            "cursor": effective_cursor,
            "next_cursor": page.get("next_cursor") or "",
            "official": official_only,
            "registry_path": page.get("path") or "packages",
            "registry_attempts": page.get("attempts") or [],
            "registry_warnings": page.get("warnings") or [],
            "registry_empty": not bool(results),
            "count": len(results),
            "results": [r.to_dict() for r in results],
        }
    )


async def api_marketplace_info(request: Request) -> JSONResponse:
    slug = (request.path_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    try:
        summary = await asyncio.to_thread(_registry_info, slug)
    except Exception as exc:
        return _client_error_response(exc)
    return JSONResponse(summary.to_dict())


def _preview_pipeline(slug: str, version: Optional[str]) -> Dict[str, Any]:
    """Return registry details for the lightweight ClawHub preview route."""
    summary = _registry_info(slug)
    return {
        "slug": slug,
        "version": version or summary.latest_version,
        "summary": summary.to_dict(),
        "adapter": {
            "ok": not summary.is_plugin,
            "warnings": [],
            "blockers": (
                ["OpenClaw Node/TypeScript plugins are not installable in Ouroboros."]
                if summary.is_plugin else []
            ),
        },
        "staging": {"is_plugin": summary.is_plugin},
    }


async def api_marketplace_preview(request: Request) -> JSONResponse:
    slug = (request.path_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    version = (request.query_params.get("version") or "").strip() or None
    try:
        payload = await asyncio.to_thread(_preview_pipeline, slug, version)
    except ClawHubClientError as exc:
        return _client_error_response(exc)
    except Exception as exc:
        log.exception("marketplace preview failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Install / update / uninstall
# ---------------------------------------------------------------------------


def _serialize_install_result(result: Any) -> Dict[str, Any]:
    """Project an :class:`InstallResult` into a JSON-friendly dict."""
    payload: Dict[str, Any] = {
        "ok": bool(result.ok),
        "sanitized_name": result.sanitized_name,
        "error": result.error,
    }
    if result.target_dir is not None:
        payload["target_dir"] = str(result.target_dir)
    if result.summary is not None:
        payload["summary"] = result.summary.to_dict()
    if result.archive is not None:
        payload["archive"] = {
            "sha256": result.archive.sha256,
            "size_bytes": len(result.archive.content),
            "version": result.archive.version,
        }
    if result.adapter is not None:
        payload["adapter"] = {
            "ok": result.adapter.ok,
            "warnings": result.adapter.warnings,
            "blockers": result.adapter.blockers,
            "sanitized_name": result.adapter.sanitized_name,
            "is_plugin": result.adapter.is_plugin,
        }
    payload["review_status"] = result.review_status
    payload["review_findings"] = result.review_findings
    payload["review_error"] = result.review_error
    payload["deps_status"] = getattr(result, "deps_status", "")
    payload["deps_error"] = getattr(result, "deps_error", "")
    payload["deps_fingerprint"] = getattr(result, "deps_fingerprint", {})
    payload["provenance"] = result.provenance
    return payload


async def api_marketplace_install(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    version = str(body.get("version") or "").strip() or None
    auto_review = _coerce_bool(body.get("auto_review"), True)
    overwrite = _coerce_bool(body.get("overwrite"), False)

    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    install_progress = JobProgressTarget()
    async def _run_install() -> Any:
        return await run_blocking_preserving_cancellation(
            install_skill,
            drive_root,
            repo_dir,
            slug=slug,
            version=version,
            auto_review=auto_review,
            overwrite=overwrite,
            progress_callback=install_progress.set,
            log_label="ClawHub install lifecycle operation",
        )

    try:
        result = await run_lifecycle_job(
            kind="install",
            target=slug,
            source="clawhub",
            message=f"Installing {slug}",
            runner=_run_install,
            options=LifecycleJobOptions(
                progress_target=install_progress,
                result_message=lambda item: (
                    f"Installed as {item.sanitized_name}"
                    if getattr(item, "ok", False)
                    else getattr(item, "error", "install failed")
                ),
                result_error=lambda item: (
                    getattr(item, "error", "install failed")
                    if not getattr(item, "ok", False)
                    else (getattr(item, "deps_error", "") if getattr(item, "deps_status", "") == "failed" else "")
                ),
            ),
        )
    except Exception as exc:
        log.exception("marketplace install failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    payload = _serialize_install_result(result)
    return JSONResponse(payload, status_code=200 if result.ok else 400)


async def api_marketplace_update(request: Request) -> JSONResponse:
    sanitized = (request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(sanitized)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    version = str(body.get("version") or "").strip() or None
    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    update_progress = JobProgressTarget()
    async def _run_update() -> Any:
        return await run_blocking_preserving_cancellation(
            update_skill,
            drive_root,
            repo_dir,
            sanitized_name=sanitized,
            version=version,
            progress_callback=update_progress.set,
            log_label="ClawHub update lifecycle operation",
        )

    try:
        result = await run_lifecycle_job(
            kind="update",
            target=sanitized,
            source="clawhub",
            message=f"Updating {sanitized}",
            runner=_run_update,
            options=LifecycleJobOptions(
                progress_target=update_progress,
                result_message=lambda item: (
                    f"Updated {item.sanitized_name}"
                    if getattr(item, "ok", False)
                    else getattr(item, "error", "update failed")
                ),
                result_error=lambda item: (
                    getattr(item, "error", "update failed")
                    if not getattr(item, "ok", False)
                    else (getattr(item, "deps_error", "") if getattr(item, "deps_status", "") == "failed" else "")
                ),
            ),
        )
    except Exception as exc:
        log.exception("marketplace update failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(_serialize_install_result(result), status_code=200 if result.ok else 400)


def _validate_path_param_name(name: str) -> Optional[str]:
    """Reject names that look like path traversal at the HTTP boundary.

    Cycle 2 critics found that Starlette's ``{name}`` matcher (default
    regex ``[^/]+``) accepts ``%2e%2e`` (``..``) which then flows into
    ``shutil.rmtree(parent / "..")`` and wipes the data plane. We
    refuse the request at the HTTP layer before ANY downstream code
    runs — the install module re-validates as defence-in-depth.
    Returns an error string (caller turns into 400) or ``None`` on OK.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return "missing name"
    if cleaned in {".", ".."}:
        return f"invalid name: {cleaned!r}"
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        return f"name must not contain path separators: {cleaned!r}"
    return None


def _installed_skill_payload(skill: Any, drive_root: pathlib.Path, *, provenance: Dict[str, Any] | None = None) -> Dict[str, Any]:
    from ouroboros.skill_loader import grant_status_for_skill, skill_review_gate

    try:
        rel_skill_dir = skill.skill_dir.resolve().relative_to(drive_root.resolve())
        payload_root = rel_skill_dir.as_posix() if rel_skill_dir.parts[:1] == ("skills",) else ""
    except Exception:
        payload_root = ""
    stale = skill.review.is_stale_for(skill.content_hash)
    gate = skill_review_gate(skill.review.status, stale=stale)
    payload = {
        "name": skill.name,
        "type": skill.manifest.type,
        "version": skill.manifest.version,
        "review_status": skill.review.status,
        "review_stale": stale,
        "review_gate": gate,
        "executable_review": gate["executable_review"],
        "review_findings": list(skill.review.findings or []),
        "enabled": skill.enabled,
        "load_error": skill.load_error,
        "grants": grant_status_for_skill(drive_root, skill),
        "payload_root": payload_root,
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return payload


def _installed_skills_for_source(drive_root: pathlib.Path, source: str) -> list[Dict[str, Any]]:
    from ouroboros.config import get_skills_repo_path
    from ouroboros.skill_loader import discover_skills

    out: list[Dict[str, Any]] = []
    for skill in discover_skills(drive_root, repo_path=get_skills_repo_path()):
        if skill.source != source:
            continue
        provenance = read_provenance(drive_root, skill.name) or {} if source == "clawhub" else None
        out.append(_installed_skill_payload(skill, drive_root, provenance=provenance))
    return out


async def api_marketplace_uninstall(request: Request) -> JSONResponse:
    sanitized = (request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(sanitized)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    drive_root = _request_drive_root(request)
    async def _run_uninstall() -> Any:
        return await run_blocking_preserving_cancellation(
            uninstall_skill,
            drive_root,
            sanitized_name=sanitized,
            log_label="ClawHub uninstall lifecycle operation",
        )

    try:
        result = await run_lifecycle_job(
            kind="uninstall",
            target=sanitized,
            source="clawhub",
            message=f"Uninstalling {sanitized}",
            runner=_run_uninstall,
            options=LifecycleJobOptions(
                result_message=lambda item: (
                    f"Uninstalled {item.sanitized_name}"
                    if getattr(item, "ok", False)
                    else getattr(item, "error", "uninstall failed")
                ),
                result_error=lambda item: "" if getattr(item, "ok", False) else getattr(item, "error", "uninstall failed"),
            ),
        )
    except Exception as exc:
        log.exception("marketplace uninstall failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(
        {
            "ok": result.ok,
            "sanitized_name": result.sanitized_name,
            "error": result.error,
        },
        status_code=200 if result.ok else 400,
    )


async def api_marketplace_installed(request: Request) -> JSONResponse:
    """List ClawHub-installed skills + provenance for the UI."""
    drive_root = _request_drive_root(request)
    out = _installed_skills_for_source(drive_root, "clawhub")
    return JSONResponse({"count": len(out), "skills": out})


# ---------------------------------------------------------------------------
# OuroborosHub static official-skill catalog
# ---------------------------------------------------------------------------


async def api_ouroboroshub_catalog(request: Request) -> JSONResponse:
    query = str(request.query_params.get("q") or request.query_params.get("query") or "").strip()
    try:
        results = await asyncio.to_thread(ouroboroshub.search, query)
    except Exception as exc:
        log.warning("OuroborosHub catalog failed: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"query": query, "count": len(results), "results": [item.to_dict() for item in results]})


async def api_ouroboroshub_preview(request: Request) -> JSONResponse:
    slug = str(request.path_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    try:
        summary = await asyncio.to_thread(ouroboroshub.info, slug)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"summary": summary.to_dict(), "files": summary.files})


async def api_ouroboroshub_install(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    overwrite = _coerce_bool(body.get("overwrite"), False)
    auto_review = _coerce_bool(body.get("auto_review"), True)
    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    install_progress = JobProgressTarget()

    async def _run_install() -> Dict[str, Any]:
        install_progress.set("Downloading from OuroborosHub…")
        result = await run_blocking_preserving_cancellation(
            ouroboroshub.install,
            slug,
            overwrite=overwrite,
            log_label="OuroborosHub install lifecycle operation",
        )
        payload: Dict[str, Any] = {
            "ok": result.ok,
            "sanitized_name": result.sanitized_name,
            "error": result.error,
            "provenance": result.provenance,
            "summary": result.summary.to_dict() if result.summary else None,
        }
        if result.target_dir is not None:
            payload["target_dir"] = str(result.target_dir)
        if result.ok and auto_review:
            install_progress.set("Running tri-model review…")
            status, findings, error = await run_blocking_preserving_cancellation(
                _run_skill_review,
                drive_root,
                repo_dir,
                result.sanitized_name,
                log_label="OuroborosHub install review lifecycle operation",
            )
            payload.update({"review_status": status, "review_findings": findings, "review_error": error})
            if _review_status_allows_skill_runtime(status) and not error:
                install_progress.set("Installing dependencies…")
                deps_status, deps_error = await run_blocking_preserving_cancellation(
                    _reconcile_deps_after_review,
                    drive_root,
                    result.sanitized_name,
                    log_label="OuroborosHub install dependency lifecycle operation",
                )
                payload.update({"deps_status": deps_status, "deps_error": deps_error})
                if deps_status == "failed":
                    payload["ok"] = False
                    payload["error"] = deps_error
        return payload

    payload = await run_lifecycle_job(
        kind="install",
        target=slug,
        source="ouroboroshub",
        message=f"Installing {slug}",
        runner=_run_install,
        options=LifecycleJobOptions(
            progress_target=install_progress,
            result_message=lambda item: (
                f"Installed as {item.get('sanitized_name')}"
                if item.get("ok")
                else item.get("error", "install failed")
            ),
            result_error=lambda item: "" if item.get("ok") else item.get("error", "install failed"),
        ),
    )
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)


async def api_ouroboroshub_update(request: Request) -> JSONResponse:
    name = str(request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(name)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    update_progress = JobProgressTarget()

    async def _run_update() -> Dict[str, Any]:
        was_live = False
        try:
            from ouroboros.extension_loader import is_extension_live, unload_extension

            was_live = bool(is_extension_live(name, drive_root))
            update_progress.set("Unloading existing extension…")
            await run_blocking_preserving_cancellation(
                unload_extension,
                name,
                log_label="OuroborosHub update extension unload lifecycle operation",
            )
        except Exception:
            log.debug("OuroborosHub pre-update extension unload failed for %s", name, exc_info=True)
        update_progress.set("Downloading from OuroborosHub…")
        result = await run_blocking_preserving_cancellation(
            ouroboroshub.install,
            name,
            overwrite=True,
            log_label="OuroborosHub update lifecycle operation",
        )
        payload: Dict[str, Any] = {
            "ok": result.ok,
            "sanitized_name": result.sanitized_name,
            "error": result.error,
            "provenance": result.provenance,
            "summary": result.summary.to_dict() if result.summary else None,
        }
        if result.target_dir is not None:
            payload["target_dir"] = str(result.target_dir)
        if result.ok:
            update_progress.set("Running tri-model review…")
            status, findings, error = await run_blocking_preserving_cancellation(
                _run_skill_review,
                drive_root,
                repo_dir,
                result.sanitized_name,
                log_label="OuroborosHub update review lifecycle operation",
            )
            payload.update({"review_status": status, "review_findings": findings, "review_error": error})
            deps_status = "not_required"
            deps_error = ""
            if _review_status_allows_skill_runtime(status) and not error:
                update_progress.set("Installing dependencies…")
                deps_status, deps_error = await run_blocking_preserving_cancellation(
                    _reconcile_deps_after_review,
                    drive_root,
                    result.sanitized_name,
                    log_label="OuroborosHub update dependency lifecycle operation",
                )
                payload.update({"deps_status": deps_status, "deps_error": deps_error})
                if deps_status == "failed":
                    payload["ok"] = False
                    payload["error"] = deps_error
            if was_live and _review_status_allows_skill_runtime(status) and not error and deps_status != "failed":
                try:
                    from ouroboros.config import load_settings
                    from ouroboros.extension_loader import reconcile_extension

                    update_progress.set("Reloading extension…")
                    live_state = await run_blocking_preserving_cancellation(
                        reconcile_extension,
                        result.sanitized_name,
                        drive_root,
                        load_settings,
                        log_label="OuroborosHub update extension reload lifecycle operation",
                    )
                    payload.update({
                        "extension_action": live_state.get("action"),
                        "extension_reason": live_state.get("reason"),
                    })
                except Exception:
                    log.debug("OuroborosHub post-update reconcile failed for %s", name, exc_info=True)
        elif was_live:
            try:
                from ouroboros.config import load_settings
                from ouroboros.extension_loader import reconcile_extension
                await run_blocking_preserving_cancellation(
                    reconcile_extension,
                    name,
                    drive_root,
                    load_settings,
                    log_label="OuroborosHub failed-update extension restore lifecycle operation",
                )
            except Exception:
                log.debug("OuroborosHub failed-update re-reconcile failed for %s", name, exc_info=True)
        return payload

    payload = await run_lifecycle_job(
        kind="update",
        target=name,
        source="ouroboroshub",
        message=f"Updating {name}",
        runner=_run_update,
        options=LifecycleJobOptions(
            progress_target=update_progress,
            result_message=lambda item: (
                f"Updated {item.get('sanitized_name')}"
                if item.get("ok")
                else item.get("error", "update failed")
            ),
            result_error=lambda item: "" if item.get("ok") else item.get("error", "update failed"),
        ),
    )
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)

async def api_ouroboroshub_installed(request: Request) -> JSONResponse:
    drive_root = _request_drive_root(request)
    out = _installed_skills_for_source(drive_root, "ouroboroshub")
    return JSONResponse({"count": len(out), "skills": out})


async def api_ouroboroshub_uninstall(request: Request) -> JSONResponse:
    sanitized = str(request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(sanitized)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    async def _run_uninstall() -> Dict[str, Any]:
        result = await run_blocking_preserving_cancellation(
            ouroboroshub.uninstall,
            sanitized,
            log_label="OuroborosHub uninstall lifecycle operation",
        )
        return {"ok": result.ok, "sanitized_name": result.sanitized_name, "error": result.error}

    payload = await run_lifecycle_job(
        kind="uninstall",
        target=sanitized,
        source="ouroboroshub",
        message=f"Uninstalling {sanitized}",
        runner=_run_uninstall,
        options=LifecycleJobOptions(
            result_message=lambda item: (
                f"Uninstalled {item.get('sanitized_name')}"
                if item.get("ok")
                else item.get("error", "uninstall failed")
            ),
            result_error=lambda item: "" if item.get("ok") else item.get("error", "uninstall failed"),
        ),
    )
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)


__all__ = [
    "api_marketplace_search",
    "api_marketplace_info",
    "api_marketplace_preview",
    "api_marketplace_install",
    "api_marketplace_update",
    "api_marketplace_uninstall",
    "api_marketplace_installed",
    "api_ouroboroshub_catalog",
    "api_ouroboroshub_preview",
    "api_ouroboroshub_install",
    "api_ouroboroshub_update",
    "api_ouroboroshub_installed",
    "api_ouroboroshub_uninstall",
]

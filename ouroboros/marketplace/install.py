"""Install, update, and uninstall ClawHub skills in the data plane.

The synchronous pipeline keeps registry lookup, download, staging, adaptation,
atomic landing, review, dependency install, and provenance as separate helpers
so routes can move work across ``asyncio.to_thread`` boundaries.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ouroboros.marketplace.adapter import AdapterResult, adapt_openclaw_skill
from ouroboros.marketplace.clawhub import (
    ClawHubArchive,
    ClawHubClientError,
    ClawHubSkillSummary,
    download as _registry_download,
    info as _registry_info,
)
from ouroboros.marketplace.fetcher import (
    FetchError,
    StagedSkill,
    land_staged_tree,
    stage as _stage_archive,
)
from ouroboros.marketplace.isolated_deps import DEPS_STATE_FILENAME, install_isolated_dependencies
from ouroboros.marketplace.provenance import (
    delete_provenance,
    read_provenance,
    write_provenance,
)
from ouroboros.skill_review_status import skill_review_gate

log = logging.getLogger(__name__)


@dataclass
class InstallResult:
    """Outcome of ``install_skill``."""

    ok: bool
    sanitized_name: str
    target_dir: Optional[pathlib.Path] = None
    summary: Optional[ClawHubSkillSummary] = None
    archive: Optional[ClawHubArchive] = None
    staged: Optional[StagedSkill] = None
    adapter: Optional[AdapterResult] = None
    review_status: str = ""
    review_findings: List[Dict[str, Any]] = field(default_factory=list)
    review_error: str = ""
    deps_status: str = ""
    deps_error: str = ""
    deps_fingerprint: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UninstallResult:
    ok: bool
    sanitized_name: str
    error: str = ""


def _clawhub_skills_root(drive_root: pathlib.Path) -> pathlib.Path:
    """Return the ClawHub skills bucket, creating the canonical layout."""
    try:
        from ouroboros.config import ensure_data_skills_dir
        ensure_data_skills_dir(pathlib.Path(drive_root))
    except ImportError:
        pass
    target = pathlib.Path(drive_root) / "skills" / "clawhub"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _land_staged_into_data_plane(
    staged: StagedSkill,
    target_dir: pathlib.Path,
    *,
    overwrite: bool,
) -> None:
    """Atomically land staged content, preserving the old tree until success."""
    target_dir = pathlib.Path(target_dir)
    if target_dir.exists():
        if not overwrite:
            raise RuntimeError(
                f"Target {target_dir} already exists — use overwrite=True to replace"
            )
    land_staged_tree(staged.staging_dir, target_dir, replacement_suffix=f"replaced-{staged.sha256[:8]}")


class _MarketplaceReviewCtx:
    """Minimal ToolContext-compatible carrier for headless auto-review."""

    def __init__(self, drive_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        self.drive_root: pathlib.Path = pathlib.Path(drive_root)
        self.repo_dir: pathlib.Path = pathlib.Path(repo_dir)
        self.task_id: Any = "marketplace_install"
        self.current_chat_id: Any = 0
        self.pending_events: List[Any] = []
        self.emit_progress_fn = lambda _msg: None
        self.event_queue = None  # _emit_usage_event tolerates None
        self.messages: List[Any] = []

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / rel).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / rel).resolve()

    def drive_logs(self) -> pathlib.Path:
        target = self.drive_root / "logs"
        target.mkdir(parents=True, exist_ok=True)
        return target


def _run_skill_review(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    skill_name: str,
) -> tuple[str, List[Dict[str, Any]], str]:
    """Run ``review_skill``; missing review code leaves install pending."""
    try:
        from ouroboros.skill_review import review_skill as _review_skill_impl
    except ImportError as exc:
        return "pending", [], f"review pipeline unavailable: {exc}"

    try:
        outcome = _review_skill_impl(
            _MarketplaceReviewCtx(drive_root, repo_dir), skill_name
        )
    except Exception as exc:
        log.exception("review_skill raised during marketplace install")
        return "pending", [], f"review_skill raised: {type(exc).__name__}: {exc}"
    return (
        str(outcome.status or "pending"),
        list(outcome.findings or []),
        str(outcome.error or ""),
    )


def install_skill(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    slug: str,
    version: Optional[str] = None,
    auto_review: bool = True,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> InstallResult:
    """Install one skill; ``overwrite=True`` is required for replacement.

    ``progress_callback`` receives worker-thread stage labels for the UI and
    must stay cheap/non-throwing.
    """

    def _progress(stage: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(stage)
            except Exception:
                log.debug("install_skill progress callback raised", exc_info=True)
    fail = lambda error, sanitized_name="", **kwargs: InstallResult(ok=False, sanitized_name=sanitized_name, error=error, **kwargs)

    _progress("Resolving registry…")

    cleaned_slug = (slug or "").strip()
    if not cleaned_slug:
        return fail("slug must be non-empty")

    requested_version = (version or "").strip()

    try:
        summary = _registry_info(cleaned_slug)
    except ClawHubClientError as exc:
        return fail(f"Registry lookup failed: {exc}")

    if summary.is_plugin:
        return fail(
            "Package is an OpenClaw Node/TypeScript plugin and cannot be installed via the Ouroboros marketplace. Skills only.",
            summary=summary,
        )

    target_version = requested_version or summary.latest_version
    if not target_version:
        return fail("Registry returned no version metadata; cannot resolve install target.", summary=summary)

    _progress(f"Downloading v{target_version}…")
    try:
        archive = _registry_download(cleaned_slug, version=target_version)
    except ClawHubClientError as exc:
        return fail(f"Download failed: {exc}", summary=summary)

    try:
        # archive.sha256 is a local recomputation check, not a MITM anchor.
        # Stage under the target bucket so the final move is same-FS atomic.
        staging_root = _clawhub_skills_root(drive_root) / ".staging"
        staged = _stage_archive(
            archive.content,
            slug=cleaned_slug,
            version=target_version,
            expected_sha256=archive.sha256,
            staging_root=staging_root,
        )
    except FetchError as exc:
        return fail(f"Archive validation failed: {exc}", summary=summary, archive=archive)

    _progress("Adapting manifest…")
    try:
        adapter_result = adapt_openclaw_skill(
            staged.staging_dir,
            slug=cleaned_slug,
            version=target_version,
            sha256=archive.sha256,
            is_plugin=staged.has_plugin_manifest,
        )
    except Exception as exc:
        staged.cleanup()
        log.exception("adapter raised during install")
        return fail(f"Adapter raised: {type(exc).__name__}: {exc}", summary=summary, archive=archive, staged=staged)

    if not adapter_result.ok:
        staged.cleanup()
        return fail(
            "Adapter rejected the package: " + "; ".join(adapter_result.blockers),
            sanitized_name=adapter_result.sanitized_name,
            summary=summary,
            archive=archive,
            staged=staged,
            adapter=adapter_result,
        )

    _progress("Landing into data plane…")
    target_root = _clawhub_skills_root(drive_root)
    target_dir = target_root / adapter_result.target_dirname
    try:
        _land_staged_into_data_plane(staged, target_dir, overwrite=overwrite)
    except Exception as exc:
        staged.cleanup()
        log.exception("Failed to land staged skill into data plane")
        return fail(
            f"Could not land skill into data plane: {exc}",
            sanitized_name=adapter_result.sanitized_name,
            summary=summary,
            archive=archive,
            staged=staged,
            adapter=adapter_result,
        )
    # Do not repoint staged.staging_dir to target_dir: cleanup() rmtrees it.
    # Persist provenance before review so reviewers can cross-check origin.
    from ouroboros.config import get_clawhub_registry_url
    provenance = dict(adapter_result.provenance)
    provenance.update({
        "registry_url": get_clawhub_registry_url(),
        "version": target_version,
        "homepage": summary.homepage,
        "license": summary.license,
        "primary_env": summary.primary_env,
    })
    try:
        write_provenance(drive_root, adapter_result.sanitized_name, provenance)
    except Exception:
        log.warning("Failed to persist provenance for %s", adapter_result.sanitized_name, exc_info=True)

    # Seed grants.json for core settings so the owner-grant bridge has one file.
    try:
        from ouroboros.skill_loader import (
            find_skill,
            requested_core_setting_keys,
            save_skill_grants,
        )
        installed_skill = find_skill(drive_root, adapter_result.sanitized_name)
        if installed_skill is not None:
            requested = requested_core_setting_keys(
                list(installed_skill.manifest.env_from_settings or [])
            )
            if requested:
                save_skill_grants(
                    drive_root,
                    installed_skill.name,
                    granted_keys=[],
                    content_hash=installed_skill.content_hash,
                    requested_keys=requested,
                )
    except Exception:
        log.debug("requires.config -> grants.json bootstrap failed", exc_info=True)

    review_status = "pending"
    review_findings: List[Dict[str, Any]] = []
    review_error = ""
    deps_status = "not_required"
    deps_error = ""
    deps_fingerprint: Dict[str, Any] = {}
    if auto_review:
        _progress("Running security review…")
        review_status, review_findings, review_error = _run_skill_review(
            drive_root, repo_dir, adapter_result.sanitized_name
        )
    auto_specs = list((provenance.get("install_specs") or {}).get("auto") or [])
    if auto_specs:
        deps_status = "pending_review"
        if skill_review_gate(review_status)["executable_review"] and not review_error:
            _progress("Installing dependencies…")
            try:
                deps_fingerprint = install_isolated_dependencies(
                    drive_root,
                    adapter_result.sanitized_name,
                    target_dir,
                    auto_specs,
                )
                deps_status = "installed"
                provenance["dependency_fingerprint"] = deps_fingerprint
                write_provenance(drive_root, adapter_result.sanitized_name, provenance)
            except Exception as exc:
                log.exception("isolated dependency install failed for %s", adapter_result.sanitized_name)
                deps_status = "failed"
                deps_error = f"{type(exc).__name__}: {exc}"
    _progress("Done")

    return InstallResult(
        ok=deps_status != "failed",
        sanitized_name=adapter_result.sanitized_name,
        target_dir=target_dir,
        summary=summary,
        archive=archive,
        staged=staged,
        adapter=adapter_result,
        review_status=review_status,
        review_findings=review_findings,
        review_error=review_error,
        deps_status=deps_status,
        deps_error=deps_error,
        deps_fingerprint=deps_fingerprint,
        error=deps_error if deps_status == "failed" else "",
        provenance=provenance,
    )


def uninstall_skill(
    drive_root: pathlib.Path,
    *,
    sanitized_name: str,
) -> UninstallResult:
    """Remove a ClawHub skill payload/provenance while keeping durable state.

    Path traversal is blocked by sanitize round-trip, root containment, and a
    required ``.clawhub.json`` sidecar proving marketplace ownership.
    """
    from ouroboros.skill_loader import _sanitize_skill_name

    cleaned = (sanitized_name or "").strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or _sanitize_skill_name(cleaned) != cleaned
    ):
        return UninstallResult(
            False,
            sanitized_name,
            "invalid sanitized_name — must round-trip through _sanitize_skill_name and contain no path separators",
        )

    root = _clawhub_skills_root(drive_root).resolve()
    target = (root / cleaned).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return UninstallResult(False, sanitized_name, f"target escapes clawhub root: {target}")
    if target == root:
        return UninstallResult(False, sanitized_name, "refusing to delete the clawhub bucket root")

    if not target.is_dir():
        return UninstallResult(False, sanitized_name, f"Not found: {target}")

    # Do not remove folders the marketplace pipeline did not install.
    if not (target / ".clawhub.json").is_file():
        return UninstallResult(
            False,
            sanitized_name,
            f"refusing to remove {cleaned!r}: no .clawhub.json sidecar (not a marketplace-installed skill)",
        )

    # Unload in-process extensions before deleting their source tree.
    try:
        from ouroboros.extension_loader import unload_extension
        unload_extension(cleaned)
    except Exception:  # pragma: no cover — defensive
        log.debug("extension unload pre-uninstall failed for %s", cleaned, exc_info=True)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return UninstallResult(False, sanitized_name, f"Failed to remove {target}: {exc}")
    try:
        from ouroboros.skill_loader import skill_state_dir
        (skill_state_dir(drive_root, cleaned) / DEPS_STATE_FILENAME).unlink(missing_ok=True)
    except Exception:
        log.debug("failed to clear deps state for %s", cleaned, exc_info=True)
    delete_provenance(drive_root, cleaned)
    return UninstallResult(True, cleaned)


def update_skill(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    sanitized_name: str,
    version: Optional[str] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> InstallResult:
    """Reinstall by resolving the original slug from persisted provenance."""
    record = read_provenance(drive_root, sanitized_name)

    def _progress(stage: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(stage)
            except Exception:
                log.debug("update_skill progress callback raised", exc_info=True)

    if not record:
        return InstallResult(
            False,
            sanitized_name,
            error=f"No clawhub.json provenance for {sanitized_name!r} — this skill was not installed via the marketplace.",
        )
    slug = str(record.get("slug") or "").strip()
    if not slug:
        return InstallResult(False, sanitized_name, error="provenance is missing slug")
    # Preserve live extension state across unload/swap when possible.
    was_live = False
    try:
        from ouroboros.extension_loader import is_extension_live, unload_extension
        was_live = bool(is_extension_live(sanitized_name, drive_root))
        _progress("Unloading existing extension…")
        unload_extension(sanitized_name)
    except Exception:  # pragma: no cover — defensive
        log.debug("pre-update unload failed for %s", sanitized_name, exc_info=True)
    result = install_skill(
        drive_root,
        repo_dir,
        slug=slug,
        version=version,
        auto_review=True,
        overwrite=True,
        progress_callback=progress_callback,
    )
    if was_live and (
        not getattr(result, "ok", False)
        or skill_review_gate(getattr(result, "review_status", ""))["executable_review"]
    ):
        try:
            from ouroboros.extension_loader import reconcile_extension
            from ouroboros.config import load_settings
            _progress("Reloading extension…")
            reconcile_extension(sanitized_name, drive_root, load_settings)
        except Exception:  # pragma: no cover — defensive
            log.debug("post-update reconcile failed for %s", sanitized_name, exc_info=True)
    return result


__all__ = [
    "InstallResult",
    "UninstallResult",
    "install_skill",
    "uninstall_skill",
    "update_skill",
]

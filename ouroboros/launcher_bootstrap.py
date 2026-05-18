"""Managed git bootstrap helpers for the desktop launcher."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


BUNDLE_REPO_NAME = "repo.bundle"
BUNDLE_MANIFEST_NAME = "repo_bundle_manifest.json"
MANAGED_REPO_META_NAME = "ouroboros-managed.json"
BOOTSTRAP_PIN_MARKER_NAME = "ouroboros-bootstrap-pending"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MANAGED_REMOTE_NAME = "managed"
DEFAULT_MANAGED_LOCAL_BRANCH = "ouroboros"
DEFAULT_MANAGED_LOCAL_STABLE_BRANCH = "ouroboros-stable"
DEFAULT_MANAGED_REMOTE_STABLE_BRANCH = "ouroboros-stable"


@dataclass(frozen=True)
class BootstrapContext:
    bundle_dir: pathlib.Path
    repo_dir: pathlib.Path
    data_dir: pathlib.Path
    settings_path: pathlib.Path
    embedded_python: str
    app_version: str
    hidden_run: Callable[..., Any]
    save_settings: Callable[[dict], None]
    log: Any


def check_git(is_windows: bool) -> bool:
    if shutil.which("git") is not None:
        return True
    if is_windows:
        for candidate in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
        ):
            if os.path.isfile(candidate):
                git_dir = os.path.dirname(candidate)
                os.environ["PATH"] = git_dir + ";" + os.environ.get("PATH", "")
                return True
    return False


def _bundle_repo_path(context: BootstrapContext) -> pathlib.Path:
    return context.bundle_dir / BUNDLE_REPO_NAME


def _bundle_manifest_path(context: BootstrapContext) -> pathlib.Path:
    return context.bundle_dir / BUNDLE_MANIFEST_NAME


def _managed_meta_path(repo_dir: pathlib.Path) -> pathlib.Path:
    return repo_dir / ".git" / MANAGED_REPO_META_NAME


def _bootstrap_pin_marker_path(repo_dir: pathlib.Path) -> pathlib.Path:
    return repo_dir / ".git" / BOOTSTRAP_PIN_MARKER_NAME


def _read_json_file(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _normalize_bundle_manifest(raw: dict[str, Any], *, app_version: str) -> dict[str, Any]:
    manifest = dict(raw)
    return {
        "schema_version": int(manifest.get("schema_version") or MANIFEST_SCHEMA_VERSION),
        "bundle_file": str(manifest.get("bundle_file") or BUNDLE_REPO_NAME),
        "app_version": str(manifest.get("app_version") or app_version),
        "source_sha": str(manifest.get("source_sha") or ""),
        "release_tag": str(manifest.get("release_tag") or ""),
        "bundle_sha256": str(manifest.get("bundle_sha256") or ""),
        "source_branch": str(manifest.get("source_branch") or ""),
        "managed_remote_name": str(manifest.get("managed_remote_name") or DEFAULT_MANAGED_REMOTE_NAME),
        "managed_remote_url": str(manifest.get("managed_remote_url") or ""),
        "managed_remote_branch": str(manifest.get("managed_remote_branch") or manifest.get("source_branch") or ""),
        "managed_local_branch": str(manifest.get("managed_local_branch") or DEFAULT_MANAGED_LOCAL_BRANCH),
        "managed_local_stable_branch": str(
            manifest.get("managed_local_stable_branch") or DEFAULT_MANAGED_LOCAL_STABLE_BRANCH
        ),
        "managed_remote_stable_branch": str(
            manifest.get("managed_remote_stable_branch") or DEFAULT_MANAGED_REMOTE_STABLE_BRANCH
        ),
    }


def load_bundle_manifest(context: BootstrapContext) -> dict[str, Any]:
    manifest_path = _bundle_manifest_path(context)
    if not manifest_path.is_file():
        raise RuntimeError(
            f"Embedded managed repo manifest is missing: {manifest_path}. "
            "Rebuild the app bundle with scripts/build_repo_bundle.py."
        )
    manifest = _normalize_bundle_manifest(_read_json_file(manifest_path), app_version=context.app_version)
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported managed repo manifest schema {manifest['schema_version']} "
            f"(expected {MANIFEST_SCHEMA_VERSION})."
        )
    if not manifest["source_sha"]:
        raise RuntimeError("Managed repo manifest is missing source_sha.")
    if not manifest["bundle_sha256"]:
        raise RuntimeError("Managed repo manifest is missing bundle_sha256.")
    if not manifest["managed_remote_branch"]:
        raise RuntimeError("Managed repo manifest is missing managed_remote_branch.")
    if manifest["app_version"] != context.app_version:
        raise RuntimeError(
            f"Managed repo manifest app_version {manifest['app_version']!r} does not "
            f"match launcher app version {context.app_version!r}."
        )
    expected_tag = f"v{manifest['app_version']}"
    if manifest["release_tag"] and manifest["release_tag"] != expected_tag:
        raise RuntimeError(
            f"Managed repo manifest release_tag {manifest['release_tag']!r} does not "
            f"match app_version {manifest['app_version']!r}."
        )
    _assert_bundle_integrity(context, manifest)
    return manifest


def load_repo_manifest(repo_dir: pathlib.Path) -> dict[str, Any]:
    meta_path = _managed_meta_path(repo_dir)
    if not meta_path.is_file():
        return {}
    return _read_json_file(meta_path)


def _write_repo_manifest(repo_dir: pathlib.Path, manifest: dict[str, Any]) -> None:
    meta_path = _managed_meta_path(repo_dir)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _mark_bootstrap_pin_pending(repo_dir: pathlib.Path) -> None:
    marker = _bootstrap_pin_marker_path(repo_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pending\n", encoding="utf-8")


def _repo_manifest_matches(repo_dir: pathlib.Path, bundle_manifest: dict[str, Any]) -> bool:
    installed = load_repo_manifest(repo_dir)
    if not installed:
        return False
    tracked_keys = (
        "schema_version",
        "app_version",
        "source_sha",
        "release_tag",
        "bundle_sha256",
        "managed_remote_name",
        "managed_remote_url",
        "managed_remote_branch",
        "managed_local_branch",
        "managed_local_stable_branch",
        "managed_remote_stable_branch",
    )
    return all(str(installed.get(key) or "") == str(bundle_manifest.get(key) or "") for key in tracked_keys)


def _run_git(context: BootstrapContext, args: list[str], *, cwd: pathlib.Path, check: bool = True) -> Any:
    return context.hidden_run(
        args,
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _remote_url(context: BootstrapContext, repo_dir: pathlib.Path, remote_name: str) -> str:
    result = _run_git(context, ["git", "remote", "get-url", remote_name], cwd=repo_dir, check=False)
    return str(getattr(result, "stdout", "") or "").strip()


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_bundle_integrity(context: BootstrapContext, manifest: dict[str, Any]) -> None:
    bundle_path = context.bundle_dir / manifest["bundle_file"]
    if not bundle_path.is_file():
        raise RuntimeError(
            f"Embedded managed repo bundle is missing: {bundle_path}. "
            "Rebuild the app bundle with scripts/build_repo_bundle.py."
        )
    actual_sha = _sha256_file(bundle_path)
    expected_sha = str(manifest.get("bundle_sha256") or "").strip()
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(
            f"Embedded managed repo bundle hash mismatch for {bundle_path}: "
            f"expected {expected_sha}, got {actual_sha}."
        )


def _archive_existing_repo(context: BootstrapContext, reason: str) -> pathlib.Path | None:
    if not context.repo_dir.exists():
        return None
    archive_root = context.data_dir / "archive" / "managed_repo"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_root / f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{reason}"
    shutil.move(str(context.repo_dir), str(archive_dir))
    context.log.info("Archived existing repo to %s (%s)", archive_dir, reason)
    return archive_dir


def _remove_if_exists(path: pathlib.Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _configure_managed_clone(context: BootstrapContext, repo_dir: pathlib.Path, manifest: dict[str, Any]) -> None:
    source_sha = str(manifest.get("source_sha") or "").strip()
    local_branch = manifest["managed_local_branch"]
    local_stable_branch = manifest["managed_local_stable_branch"]
    remote_name = manifest["managed_remote_name"]
    remote_url = manifest["managed_remote_url"]

    source_sha_check = _run_git(
        context,
        ["git", "rev-parse", "--verify", source_sha],
        cwd=repo_dir,
        check=False,
    )
    if getattr(source_sha_check, "returncode", 1) != 0:
        raise RuntimeError(
            f"Embedded managed repo bundle does not contain manifest source_sha {source_sha}."
        )
    _run_git(context, ["git", "checkout", "-B", local_branch, source_sha], cwd=repo_dir)
    head = _run_git(context, ["git", "rev-parse", "HEAD"], cwd=repo_dir)
    head_sha = str(getattr(head, "stdout", "") or "").strip()
    if head_sha != source_sha:
        raise RuntimeError(
            f"Managed repo bootstrap checked out {head_sha or '(unknown)'} but manifest "
            f"requires {source_sha}."
        )
    if local_stable_branch and local_stable_branch != local_branch:
        if head_sha:
            _run_git(context, ["git", "branch", "-f", local_stable_branch, head_sha], cwd=repo_dir)

    origin = _run_git(context, ["git", "remote"], cwd=repo_dir, check=False)
    existing_remotes = {
        line.strip() for line in str(getattr(origin, "stdout", "") or "").splitlines() if line.strip()
    }
    if "origin" in existing_remotes:
        _run_git(context, ["git", "remote", "remove", "origin"], cwd=repo_dir, check=False)
    if remote_name in existing_remotes:
        _run_git(context, ["git", "remote", "remove", remote_name], cwd=repo_dir, check=False)
    if remote_url:
        _run_git(context, ["git", "remote", "add", remote_name, remote_url], cwd=repo_dir)

    _run_git(context, ["git", "config", "user.name", "Ouroboros"], cwd=repo_dir, check=False)
    _run_git(context, ["git", "config", "user.email", "ouroboros@local.mac"], cwd=repo_dir, check=False)
    _write_repo_manifest(repo_dir, manifest)
    _mark_bootstrap_pin_pending(repo_dir)


def _ensure_managed_remote(context: BootstrapContext, repo_dir: pathlib.Path, manifest: dict[str, Any]) -> None:
    remote_name = manifest["managed_remote_name"]
    remote_url = manifest["managed_remote_url"]

    remotes = _run_git(context, ["git", "remote"], cwd=repo_dir, check=False)
    existing_remotes = {
        line.strip() for line in str(getattr(remotes, "stdout", "") or "").splitlines() if line.strip()
    }
    if remote_url:
        if remote_name in existing_remotes:
            _run_git(context, ["git", "remote", "set-url", remote_name, remote_url], cwd=repo_dir)
        else:
            _run_git(context, ["git", "remote", "add", remote_name, remote_url], cwd=repo_dir)

    _run_git(context, ["git", "config", "user.name", "Ouroboros"], cwd=repo_dir, check=False)
    _run_git(context, ["git", "config", "user.email", "ouroboros@local.mac"], cwd=repo_dir, check=False)
    _write_repo_manifest(repo_dir, manifest)


def _clone_repo_from_bundle(context: BootstrapContext, manifest: dict[str, Any]) -> pathlib.Path:
    bundle_path = context.bundle_dir / manifest["bundle_file"]
    if not bundle_path.is_file():
        raise RuntimeError(
            f"Embedded managed repo bundle is missing: {bundle_path}. "
            "Rebuild the app bundle with scripts/build_repo_bundle.py."
        )

    temp_repo = context.repo_dir.parent / f".repo-bootstrap-{uuid.uuid4().hex[:8]}"
    _remove_if_exists(temp_repo)
    try:
        _run_git(context, ["git", "clone", str(bundle_path), str(temp_repo)], cwd=context.bundle_dir)
        _configure_managed_clone(context, temp_repo, manifest)
        return temp_repo
    except Exception:
        _remove_if_exists(temp_repo)
        raise


def _install_managed_repo(context: BootstrapContext, manifest: dict[str, Any], *, reason: str) -> str:
    preserved_origin_url = _remote_url(context, context.repo_dir, "origin") if (context.repo_dir / ".git").exists() else ""
    archived_repo = _archive_existing_repo(context, reason)
    temp_repo = _clone_repo_from_bundle(context, manifest)
    try:
        shutil.move(str(temp_repo), str(context.repo_dir))
        if preserved_origin_url:
            _run_git(context, ["git", "remote", "add", "origin", preserved_origin_url], cwd=context.repo_dir, check=False)
    except Exception:
        _remove_if_exists(temp_repo)
        if archived_repo is not None and not context.repo_dir.exists():
            shutil.move(str(archived_repo), str(context.repo_dir))
        raise
    return "replaced" if archived_repo is not None else "created"


def ensure_managed_repo(context: BootstrapContext) -> str:
    """Ensure REPO_DIR is a managed git clone backed by the embedded bundle."""
    manifest = load_bundle_manifest(context)
    if not context.repo_dir.exists():
        return _install_managed_repo(context, manifest, reason="missing")
    if not (context.repo_dir / ".git").exists():
        return _install_managed_repo(context, manifest, reason="legacy-no-git")
    if not _repo_manifest_matches(context.repo_dir, manifest):
        _ensure_managed_remote(context, context.repo_dir, manifest)
        context.log.info(
            "Updated managed repo metadata for embedded bundle without replacing local checkout."
        )
        return "metadata-updated"

    _ensure_managed_remote(context, context.repo_dir, manifest)
    return "unchanged"


def sync_existing_repo_from_bundle(context: BootstrapContext) -> None:
    """Reconcile the managed repo against the embedded bundle metadata."""
    outcome = ensure_managed_repo(context)
    context.log.info("Managed repo sync outcome: %s", outcome)


def install_deps(context: BootstrapContext) -> None:
    """Install/update Python deps inside the embedded interpreter."""
    try:
        requirements = context.repo_dir / "requirements.txt"
        if requirements.exists():
            context.hidden_run(
                [context.embedded_python, "-m", "pip", "install", "-r", str(requirements)],
                timeout=240,
                capture_output=True,
            )
    except Exception as exc:
        context.log.warning("Dependency install/update failed: %s", exc)


_CLAUDE_SDK_BASELINE = "claude-agent-sdk>=0.1.60"
_CLAUDE_SDK_MIN_VERSION = "0.1.60"


def _version_tuple(v: str) -> tuple:
    """Parse a PEP 440-ish version string into a comparable tuple.

    Strips any post/pre/dev suffix after the first non-numeric component.
    ``"0.1.60" -> (0, 1, 60)``, ``"0.1.60.post1" -> (0, 1, 60)``.
    Returns ``(0,)`` on parse failure (treat as "very old, needs upgrade").
    """
    if not v:
        return (0,)
    parts: list[int] = []
    for p in v.split("."):
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def verify_claude_runtime(context: BootstrapContext) -> bool:
    """Ensure the Claude runtime baseline is present in the app-managed interpreter.

    Checks that ``claude-agent-sdk`` is importable, its installed version meets
    ``_CLAUDE_SDK_MIN_VERSION``, and its bundled CLI binary exists. If any
    check fails, attempts a repair install. Returns True on success.

    Version check prevents a silent gap where an older installed SDK
    (e.g. 0.1.50 on an upgraded install) still imports and has the CLI
    binary present, but pre-dates Opus 4.7 adaptive thinking support.
    """
    import sys as _sys
    cli_name = "claude.exe" if _sys.platform == "win32" else "claude"
    try:
        result = context.hidden_run(
            [context.embedded_python, "-c",
             "import claude_agent_sdk; "
             "import importlib.metadata as _m; "
             "from pathlib import Path; "
             f"cli = Path(claude_agent_sdk.__file__).parent / '_bundled' / '{cli_name}'; "
             "ver = _m.version('claude-agent-sdk'); "
             "print('ok|' + ver if cli.exists() else 'no_cli|' + ver)"],
            capture_output=True, text=True, timeout=30,
        )
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout.startswith("ok|"):
            installed = stdout.split("|", 1)[1]
            if _version_tuple(installed) >= _version_tuple(_CLAUDE_SDK_MIN_VERSION):
                context.log.info(
                    "Claude runtime verified: SDK %s >= %s, bundled CLI present.",
                    installed, _CLAUDE_SDK_MIN_VERSION,
                )
                return True
            context.log.warning(
                "Claude runtime SDK %s is below baseline %s — repairing.",
                installed, _CLAUDE_SDK_MIN_VERSION,
            )
        else:
            context.log.warning("Claude runtime check: %s (exit %d)", stdout, result.returncode)
    except Exception as exc:
        context.log.warning("Claude runtime probe failed: %s", exc)

    context.log.info("Repairing Claude runtime baseline...")
    try:
        repair = context.hidden_run(
            [context.embedded_python, "-m", "pip", "install", "--upgrade", _CLAUDE_SDK_BASELINE],
            timeout=120,
            capture_output=True,
        )
        if repair.returncode != 0:
            context.log.warning("Claude runtime repair pip returned exit %d", repair.returncode)
            return False
        context.log.info("Claude runtime repair install complete.")
        return True
    except Exception as exc:
        context.log.warning("Claude runtime repair failed: %s", exc)
        return False


_SEED_COMPLETE_MARKER = ".bootstrap-seed-complete"


def _read_skill_manifest_version(skill_dir: pathlib.Path) -> str:
    """Best-effort scan of ``SKILL.md``/``skill.json`` for the version string.

    Used by the per-skill version-aware re-seed pass: when the launcher
    ships a newer reference version of a native skill (for example
    weather 0.1.0 ``type: script`` -> 0.2.0 ``type: extension``), the
    bootstrap detects the version mismatch and replaces the data-plane
    copy in place. The user's durable enable / review state under
    ``data/state/skills/<name>/`` survives because we never touch that
    plane during a re-seed.

    Cycle 1 GPT-critic (Findings 1–3): defers to
    :func:`ouroboros.contracts.skill_manifest.parse_skill_manifest_text`
    so the version we see here is exactly the version
    ``SkillManifest.version`` will report. The hand-rolled line scanner
    that lived here previously had three concrete edge-case bugs:
    inline YAML comments leaked into the version string, single-line
    JSON manifests returned ``""``, and ``version:`` lines that
    appeared in the body BEFORE the ``---`` frontmatter delimiter
    were accepted as the version. The shared parser handles all three
    cases correctly.

    Returns ``""`` if the manifest cannot be parsed (e.g., the file is
    missing or malformed). The resync pass treats empty-string as "do
    not upgrade", so a malformed seed manifest just disables the
    upgrade path for that skill until the operator fixes it.
    """
    for candidate in ("SKILL.md", "skill.json"):
        path = skill_dir / candidate
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
            manifest = parse_skill_manifest_text(text)
        except Exception:
            return ""
        return str(manifest.version or "").strip()
    return ""


def _record_skill_upgrade_migration(
    drive_root: pathlib.Path,
    skill_name: str,
    old_version: str,
    new_version: str,
    log_obj: Any,
) -> None:
    """Persist a migration record so the Skills UI can surface a banner.

    A native skill being replaced under the operator's feet (because a
    new launcher version bumped the seed manifest) silently invalidates
    the review state and any saved patterns / agent prompts that
    referenced the old type. We write a JSON record at
    ``data/state/migrations.json`` — the SPA reads it on mount via
    ``/api/migrations``, displays a one-shot banner on the Skills tab,
    and persists dismissal via ``/api/migrations/<key>/dismiss``.

    The format is intentionally append-only: ``{key: record}`` where
    ``key`` is ``"<new_version>_<skill_name>_upgrade"`` so a future
    upgrade of the same skill at a still-newer version writes a fresh
    record instead of mutating the old one.
    """
    state_dir = drive_root / "state"
    target = state_dir / "migrations.json"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        existing: dict[str, Any] = {}
        if target.is_file():
            try:
                existing = json.loads(target.read_text(encoding="utf-8")) or {}
                if not isinstance(existing, dict):
                    existing = {}
            except Exception:
                existing = {}
        # Use the NEW version in the key so each upgrade gets its own
        # record. Operators can dismiss old upgrades while still seeing
        # the next one when a future bump fires.
        key = f"v{new_version}_{skill_name}_upgrade"
        from ouroboros.utils import utc_now_iso
        existing[key] = {
            "kind": "native_skill_upgrade",
            "skill": skill_name,
            "old_version": old_version,
            "new_version": new_version,
            "applied_at": utc_now_iso(),
            "dismissed": False,
            "summary": (
                f"Native skill ``{skill_name}`` was upgraded from "
                f"{old_version} to {new_version} on launch. Re-review "
                f"may be required before the new version becomes "
                f"executable. Old skill_exec / extension call shapes "
                f"may need to be updated in saved patterns."
            ),
        }
        target.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception as exc:  # pragma: no cover - defensive
        log_obj.warning(
            "Failed to write migration record for %s: %s", skill_name, exc,
        )


def _reseed_native_skill_in_place(
    seed_skill: pathlib.Path,
    target_skill: pathlib.Path,
    log_obj: Any,
    *,
    drive_root: pathlib.Path | None = None,
    skill_name: str | None = None,
    old_version: str = "",
    new_version: str = "",
) -> bool:
    """Replace ``target_skill`` with ``seed_skill`` while preserving durable state.

    The durable state plane lives at ``data/state/skills/<name>/``,
    which is OUTSIDE this skill directory, so a recursive replace
    here does not touch ``enabled.json`` / ``review.json``. We DO
    delete the old skill files (including any user mods) — native
    skills are launcher-owned and the ``.seed-origin`` marker is the
    explicit signal of that ownership.

    When ``drive_root`` and version metadata are passed, also writes
    a migration record so the Skills UI can surface a banner about
    the upgrade. This closes the gap where the operator's
    `skill_exec(skill="weather", script="fetch.py")` invocations
    silently broke after the v5 weather skill type-flip; now they
    see an explicit "weather upgraded — re-review required" notice
    on the Skills page on first launch after the upgrade.
    """
    try:
        if target_skill.exists():
            shutil.rmtree(target_skill)
        shutil.copytree(seed_skill, target_skill)
        # Preserve the seed-origin contract on the freshly-replaced copy.
        (target_skill / ".seed-origin").write_text(
            f"seeded_from={seed_skill.parent.name}\nupgrade=true\n",
            encoding="utf-8",
        )
        if drive_root is not None and skill_name and old_version and new_version:
            _record_skill_upgrade_migration(
                drive_root, skill_name, old_version, new_version, log_obj,
            )
        return True
    except OSError as exc:
        log_obj.warning(
            "Failed to upgrade native skill in place %s -> %s: %s",
            seed_skill, target_skill, exc,
        )
        return False


def _per_skill_version_resync(
    seed_dir: pathlib.Path,
    native_root: pathlib.Path,
    log_obj: Any,
    *,
    drive_root: pathlib.Path | None = None,
) -> int:
    """Re-seed any native skill whose manifest version drifted from the bundled seed.

    Runs AFTER the first-time bootstrap (so ``.bootstrap-seed-complete``
    already exists). The pass ONLY upgrades skills that:

    - exist in both the seed and the target native bucket;
    - carry a ``.seed-origin`` marker (i.e. were originally seeded —
      not a user-dropped folder);
    - have a parseable manifest ``version`` on both sides AND the seed
      version differs from the installed version.

    Skills the user deleted from ``native/`` are left absent — the
    operator's deletion intent is sticky. Skills the user added by
    hand (no ``.seed-origin``) are never touched. New seed skills not
    yet present locally are NOT auto-landed during resync — that
    upgrade path is reserved for a fresh ``.bootstrap-seed-complete``
    cycle (delete the marker to receive newly-shipped reference
    skills). This protects the resurrection invariant
    (``test_bootstrap_marker_prevents_resurrection_after_user_deletion``).

    Returns the number of skills that were reseeded.
    """
    if not seed_dir.is_dir() or not native_root.is_dir():
        return 0
    upgraded = 0
    for entry in sorted(seed_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not any((entry / candidate).is_file() for candidate in ("SKILL.md", "skill.json")):
            continue
        target = native_root / entry.name
        if not target.exists():
            # User deleted (or never had) this seed skill — respect
            # their absence intent. The first-time bootstrap is the
            # only path that auto-lands a seed skill into the data
            # plane; resync exclusively upgrades.
            continue
        if not (target / ".seed-origin").is_file():
            # User-managed skill in native/ — never touch.
            continue
        seed_version = _read_skill_manifest_version(entry)
        target_version = _read_skill_manifest_version(target)
        if not seed_version or not target_version:
            continue
        if seed_version == target_version:
            continue
        log_obj.info(
            "Native skill %s version drift (seed=%s, installed=%s) — re-seeding",
            entry.name, seed_version, target_version,
        )
        if _reseed_native_skill_in_place(
            entry, target, log_obj,
            drive_root=drive_root,
            skill_name=entry.name,
            old_version=target_version,
            new_version=seed_version,
        ):
            upgraded += 1
    return upgraded


def _seed_skills_into(seed_dir: pathlib.Path, target_root: pathlib.Path, log_obj: Any) -> int:
    """Copy seed skills under ``seed_dir`` into ``target_root/native/`` once.

    Pure-fs helper extracted so source-mode startup paths (where there is
    no full ``BootstrapContext``) can also seed ``data/skills/native/``
    on first launch. Returns the number of skill packages copied.

    The "exactly once" guarantee is anchored to a ``.bootstrap-seed-complete``
    marker file written into ``data/skills/native/`` after the first
    successful seed. If the operator later deletes every native skill the
    directory becomes empty BUT the marker stays, so a subsequent launch
    correctly reads "bootstrap already happened" and does NOT resurrect
    the deleted skills (would otherwise violate the docstring promise +
    BIBLE.md P0 agency).
    """
    if not seed_dir.is_dir():
        return 0
    native_root = target_root / "native"
    try:
        # Use the canonical layout helper so a future change to bucket
        # names happens in one place. ``ensure_data_skills_dir`` takes
        # the parent of ``target_root`` (since ``target_root`` already
        # ends in ``skills``) — fall back to manual mkdir if the
        # helper is unavailable for any reason.
        try:
            from ouroboros.config import ensure_data_skills_dir
            ensure_data_skills_dir(target_root.parent)
        except Exception:
            target_root.mkdir(parents=True, exist_ok=True)
            for bucket in ("native", "clawhub", "external"):
                (target_root / bucket).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log_obj.warning("Skills data root setup failed: %s", exc)
        return 0

    marker_path = native_root / _SEED_COMPLETE_MARKER
    if marker_path.is_file():
        # Bootstrap already ran — even if every seeded skill has since
        # been deleted, the operator's intent stands.
        return 0

    # If there are existing entries but no marker (in-place upgrade or
    # user-managed payload in ``native/``), treat that as "already
    # seeded by a different mechanism" and just write the marker
    # without re-copying — this is a safety property: never clobber
    # user-managed content in ``native/``.
    try:
        existing = [p for p in native_root.iterdir() if not p.name.startswith(".")]
    except OSError:
        existing = []
    if existing:
        try:
            marker_path.write_text(
                "Bootstrap inferred from pre-existing native/ contents.\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log_obj.warning("Failed to write %s: %s", marker_path, exc)
        return 0

    copied = 0
    for entry in sorted(seed_dir.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        if not any((entry / candidate).is_file() for candidate in ("SKILL.md", "skill.json")):
            continue
        dest = native_root / entry.name
        if dest.exists():
            continue
        try:
            shutil.copytree(entry, dest)
            # v4.50 fix (Ouroboros review O3): drop a per-skill seed
            # marker so ``_classify_skill_source`` can distinguish a
            # launcher-seeded skill from one a user manually dropped
            # into ``data/skills/native/``.
            (dest / ".seed-origin").write_text(
                f"seeded_from={seed_dir.name}\n", encoding="utf-8",
            )
            copied += 1
        except OSError as exc:
            log_obj.warning("Failed to copy seed skill %s -> %s: %s", entry, dest, exc)

    # Always write the marker after a bootstrap pass — even when 0
    # skills landed (seed_dir was empty, or every entry already
    # existed). The point is: "bootstrap has been attempted; do not
    # try again on subsequent launches".
    try:
        marker_path.write_text(
            f"Bootstrap-seed completed; copied {copied} skill(s) from {seed_dir}.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log_obj.warning("Failed to write %s: %s", marker_path, exc)

    if copied:
        log_obj.info(
            "Bootstrapped %d native skill(s) from seed %s into %s",
            copied, seed_dir, native_root,
        )
    return copied


def ensure_data_skills_seeded() -> int:
    """Source-mode entry point: seed ``data/skills/native/`` if empty
    AND reconcile native skills against the bundled seed when their
    manifest version changes.

    Two passes:

    1. ``_seed_skills_into`` — first-time copy of ``repo/skills/*`` into
       ``data/skills/native/*`` plus the durable
       ``.bootstrap-seed-complete`` marker. Idempotent: returns 0 when
       the marker already exists.
    2. ``_per_skill_version_resync`` — runs every launch (cheap text
       comparison of YAML frontmatter / JSON ``version`` field). When
       a launcher-shipped seed bumps a native skill's version, we
       replace the data-plane copy IN PLACE while preserving every
       file under ``data/state/skills/<name>/`` (enabled / review
       state). User-managed skills (no ``.seed-origin`` marker) are
       never touched.

    Returns the total number of native skill packages copied or
    upgraded across both passes. Best-effort and never raises.
    """
    import logging as _logging
    from ouroboros.config import DATA_DIR, REPO_DIR

    log_obj = _logging.getLogger(__name__)
    seed_dir = pathlib.Path(REPO_DIR) / "skills"
    target_root = pathlib.Path(DATA_DIR) / "skills"
    copied = _seed_skills_into(seed_dir, target_root, log_obj)
    drive_root = pathlib.Path(DATA_DIR)
    try:
        upgraded = _per_skill_version_resync(
            seed_dir, target_root / "native", log_obj,
            drive_root=drive_root,
        )
    except Exception:  # pragma: no cover - defensive
        log_obj.warning("Native skill version-resync raised", exc_info=True)
        upgraded = 0
    try:
        cleanup_orphaned_seed_markers(seed_dir, target_root / "native", log_obj)
    except Exception:  # pragma: no cover - defensive
        log_obj.warning("Orphaned seed-marker cleanup raised", exc_info=True)
    return copied + upgraded


def cleanup_orphaned_seed_markers(
    seed_dir: pathlib.Path,
    native_root: pathlib.Path,
    log_obj,
) -> None:
    """Strip ``.seed-origin`` markers from native skills whose seed has
    been removed from ``repo/skills/``.

    v5.7.0: ``video_gen`` was removed from the bundled seed in favour
    of the OuroborosHub-published copy, but existing user installs
    keep the on-disk ``data/skills/native/video_gen/`` directory plus
    its launcher-written ``.seed-origin`` marker. Without this helper
    those installs would still be classified as ``source: native``
    forever, even though the launcher no longer ships a seed for
    them. Removing the marker re-classifies them as ``source: external``
    (user-managed), which is honest about the runtime ownership.

    Idempotent: only strips a marker when the matching seed dir is
    absent. Never deletes payload files; the user keeps their copy."""
    if not native_root.is_dir():
        return
    for entry in native_root.iterdir():
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        marker = entry / ".seed-origin"
        if not marker.is_file():
            continue
        if (seed_dir / entry.name).is_dir():
            continue
        try:
            marker.unlink()
            log_obj.info(
                "Native skill %r seed has been removed from repo/skills/; "
                "re-classifying installed copy as external (user-managed).",
                entry.name,
            )
        except OSError:  # pragma: no cover - defensive
            log_obj.warning(
                "Failed to strip orphaned .seed-origin from %s",
                entry, exc_info=True,
            )


def bootstrap_native_skills(context: BootstrapContext) -> None:
    """One-time copy of ``repo/skills/*`` into ``data/skills/native/*``.

    v4.50 moved skill packages out of the git-tracked ``repo/`` tree into
    the data plane (``data/skills/native/``) so the runtime location is
    user-mutable without dirtying the managed repo. ``repo/skills/`` now
    serves as the launcher-shipped seed; this function copies that seed
    into the data plane exactly once — when the destination is empty.

    Idempotent and best-effort: any error is logged and swallowed so a
    transient FS failure does not block startup. Subsequent launches
    leave the data-plane copy alone, even if the user modified or
    deleted some seed-derived skills.
    """
    _seed_skills_into(
        context.repo_dir / "skills",
        context.data_dir / "skills",
        context.log,
    )


def bootstrap_repo(context: BootstrapContext) -> None:
    """Ensure the launcher-managed git repo exists and matches the embedded bundle."""
    context.data_dir.mkdir(parents=True, exist_ok=True)
    outcome = ensure_managed_repo(context)
    context.log.info("Bootstrapping managed repository to %s (outcome=%s)", context.repo_dir, outcome)

    try:
        memory_dir = context.data_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        world_path = memory_dir / "WORLD.md"
        if not world_path.exists():
            env = os.environ.copy()
            env["PYTHONPATH"] = str(context.repo_dir)
            context.hidden_run(
                [
                    context.embedded_python,
                    "-c",
                    f"import sys; sys.path.insert(0, '{context.repo_dir}'); "
                    f"from ouroboros.world_profiler import generate_world_profile; "
                    f"generate_world_profile('{world_path}')",
                ],
                env=env,
                timeout=30,
                capture_output=True,
            )
    except Exception as exc:
        context.log.warning("World profile generation failed: %s", exc)

    bootstrap_native_skills(context)
    if outcome != "unchanged":
        install_deps(context)
    verify_claude_runtime(context)
    context.log.info("Bootstrap complete.")

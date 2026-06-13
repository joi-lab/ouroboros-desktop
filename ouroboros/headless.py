"""Headless task helpers for CLI/workspace runs.

The gateway owns task transport; this module owns the small amount of local
filesystem state needed for isolated external runs and patch artifacts.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Sequence, Tuple

from ouroboros.task_results import load_task_result, validate_task_id, write_task_result
from ouroboros.utils import atomic_write_json, utc_now_iso


HEADLESS_TASKS_DIR = pathlib.Path("state") / "headless_tasks"
ARTIFACTS_DIR = pathlib.Path("task_results") / "artifacts"
TASK_DRIVES_DIR = pathlib.Path("task_drives")
ARTIFACT_STATUS_PENDING = "pending"
ARTIFACT_STATUS_FINALIZING = "finalizing"
ARTIFACT_STATUS_READY = "ready"
ARTIFACT_STATUS_FAILED = "failed"

_FINAL_STATUSES = {"completed", "failed", "cancelled", "rejected_duplicate"}
_ARTIFACT_LIFECYCLE_FIELDS = {
    "artifact_status",
    "artifact_error",
    "artifact_bundle",
    "artifact_finalized_at",
}
DEFAULT_HEADLESS_TASK_RETENTION_DAYS = 7
_PATCH_EXCLUDE_RULES_VERSION = 1
_TOP_LEVEL_EXCLUDE_DIRS = {".ouroboros", ".venv", "venv", "env"}
_ANY_SEGMENT_EXCLUDE_DIRS = {
    ".cache",
    ".mypy_cache",
    ".npm",
    ".pnpm-store",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".yarn",
    "__pycache__",
    "node_modules",
}
_SENSITIVE_EXAMPLE_SUFFIXES = (".example", ".sample", ".template", ".dist")
_SENSITIVE_KEY_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
_SENSITIVE_FILENAMES = {"credentials.json", "secrets.json", "token.json"}


def task_state_dir(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return pathlib.Path(drive_root) / HEADLESS_TASKS_DIR / validate_task_id(task_id)


def task_artifacts_dir(drive_root: pathlib.Path, task_id: str, *, create: bool = True) -> pathlib.Path:
    path = pathlib.Path(drive_root) / ARTIFACTS_DIR / validate_task_id(task_id)
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def write_workspace_preflight_artifact(
    parent_drive_root: pathlib.Path,
    task_id: str,
    preflight: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist the full workspace preflight report as a task artifact."""

    artifact_dir = task_artifacts_dir(parent_drive_root, task_id)
    path = artifact_dir / "workspace_preflight.json"
    atomic_write_json(path, preflight, trailing_newline=True)
    raw = path.read_bytes() if path.exists() else b""
    return {
        "kind": "workspace_preflight",
        "name": "workspace_preflight.json",
        "path": str(path),
        "size": len(raw),
        "sha256": sha256(raw).hexdigest() if raw else "",
        "workspace_root": str(preflight.get("workspace_root") or ""),
    }


def prepare_task_drive(parent_drive_root: pathlib.Path, task_id: str, memory_mode: str) -> Optional[pathlib.Path]:
    """Create an isolated child drive for external runs.

    ``forked`` copies stable identity/world/registry/knowledge context. ``empty``
    starts with a blank data root that ``Memory.ensure_files`` will initialize.
    Any other value keeps the parent drive shared and returns ``None``.
    """

    mode = str(memory_mode or "shared").strip().lower()
    if mode not in {"forked", "empty"}:
        return None

    task_id = validate_task_id(task_id)
    parent = pathlib.Path(parent_drive_root)
    child = task_state_dir(parent, task_id) / "data"
    child.mkdir(parents=True, exist_ok=True)
    for rel in ("memory", "logs", "state", "task_results"):
        (child / rel).mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        child / "state" / "state.json",
        {
            "schema_version": 1,
            "headless_task_id": str(task_id),
            "memory_mode": mode,
            "created_at": utc_now_iso(),
        },
        trailing_newline=True,
    )
    if mode == "forked":
        _copy_stable_memory(parent, child)
    return child


def headless_task_retention_days() -> int:
    raw = os.environ.get("OUROBOROS_HEADLESS_TASK_RETENTION_DAYS", "")
    if not str(raw or "").strip():
        try:
            from ouroboros.config import load_settings

            raw = load_settings().get("OUROBOROS_HEADLESS_TASK_RETENTION_DAYS", "")
        except Exception:
            raw = ""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        days = DEFAULT_HEADLESS_TASK_RETENTION_DAYS
    return max(1, min(365, days))


def _timestamp_from_result(result: Dict[str, Any], fallback: float) -> float:
    for key in ("artifact_finalized_at", "completed_at", "finished_at", "ts"):
        raw = str(result.get(key) or "").strip()
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return float(parsed.timestamp())
        except ValueError:
            continue
    return fallback


def prune_headless_task_drives(
    parent_drive_root: pathlib.Path,
    *,
    retention_days: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Best-effort startup prune for copied-back terminal child drives."""

    parent = pathlib.Path(parent_drive_root)
    base = parent / HEADLESS_TASKS_DIR
    days = headless_task_retention_days() if retention_days is None else max(1, int(retention_days))
    cutoff = float(now if now is not None else time.time()) - days * 86400
    report: Dict[str, Any] = {"retention_days": days, "scanned": 0, "pruned": [], "skipped": [], "errors": []}
    if not base.is_dir():
        return report
    for task_dir in sorted(base.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        report["scanned"] += 1
        try:
            validate_task_id(task_id)
            dir_mtime = task_dir.stat().st_mtime
            try:
                from ouroboros.task_status import load_effective_task_result

                result = load_effective_task_result(parent, task_id) or {}
            except Exception:
                result = load_task_result(parent, task_id) or {}
            status = str(result.get("status") or "").lower()
            if status not in _FINAL_STATUSES:
                report["skipped"].append({"task_id": task_id, "reason": "parent_not_terminal", "status": status})
                continue
            artifact_status = str(result.get("artifact_status") or "").lower()
            if artifact_status and artifact_status not in {ARTIFACT_STATUS_READY, ARTIFACT_STATUS_FAILED}:
                report["skipped"].append({"task_id": task_id, "reason": "artifacts_not_terminal", "artifact_status": artifact_status})
                continue
            retention_ts = _timestamp_from_result(result, dir_mtime)
            if retention_ts > cutoff:
                report["skipped"].append({"task_id": task_id, "reason": "younger_than_retention"})
                continue
            expected_child = str((task_dir / "data").resolve(strict=False))
            known_child = str(
                result.get("child_drive_root")
                or result.get("headless_child_drive_root")
                or result.get("drive_root")
                or ""
            ).strip()
            if known_child and str(pathlib.Path(known_child).resolve(strict=False)) != expected_child:
                report["skipped"].append({"task_id": task_id, "reason": "child_drive_mismatch"})
                continue
            shutil.rmtree(task_dir)
            report["pruned"].append({"task_id": task_id, "path": str(task_dir)})
        except Exception as exc:
            report["errors"].append({"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"})
    return report


def prune_task_drives(
    parent_drive_root: pathlib.Path,
    *,
    retention_days: Optional[int] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Best-effort startup prune for direct-task scratch drives."""

    parent = pathlib.Path(parent_drive_root)
    base = parent / TASK_DRIVES_DIR
    days = headless_task_retention_days() if retention_days is None else max(1, int(retention_days))
    cutoff = float(now if now is not None else time.time()) - days * 86400
    report: Dict[str, Any] = {"retention_days": days, "scanned": 0, "pruned": [], "skipped": [], "errors": []}
    if not base.is_dir():
        return report
    for task_dir in sorted(base.iterdir()):
        if not task_dir.is_dir():
            continue
        task_id = task_dir.name
        report["scanned"] += 1
        try:
            validate_task_id(task_id)
            dir_mtime = task_dir.stat().st_mtime
            try:
                from ouroboros.task_status import load_effective_task_result

                result = load_effective_task_result(parent, task_id) or {}
            except Exception:
                result = load_task_result(parent, task_id) or {}
            status = str(result.get("status") or "").lower()
            if status not in _FINAL_STATUSES:
                report["skipped"].append({"task_id": task_id, "reason": "task_not_terminal", "status": status})
                continue
            if _timestamp_from_result(result, dir_mtime) > cutoff:
                report["skipped"].append({"task_id": task_id, "reason": "younger_than_retention"})
                continue
            shutil.rmtree(task_dir)
            report["pruned"].append({"task_id": task_id, "path": str(task_dir)})
        except Exception as exc:
            report["errors"].append({"task_id": task_id, "error": f"{type(exc).__name__}: {exc}"})
    return report


def copy_child_task_result(parent_drive_root: pathlib.Path, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Copy a child-drive task result back to the parent data root."""

    task_id = str(task.get("id") or "")
    child_drive = _child_drive_from_task(task)
    if not task_id or child_drive is None:
        return None
    child_result = load_task_result(child_drive, task_id)
    if not isinstance(child_result, dict):
        return None
    workspace_task = _workspace_root_from_task(task) is not None
    child_status = str(child_result.get("status") or "completed")
    existing = load_task_result(parent_drive_root, task_id) if workspace_task and child_status in _FINAL_STATUSES else {}
    existing_artifact_status = str((existing or {}).get("artifact_status") or "").strip().lower()
    preserve_parent_artifacts = existing_artifact_status in {
        ARTIFACT_STATUS_PENDING,
        ARTIFACT_STATUS_FINALIZING,
        ARTIFACT_STATUS_READY,
        ARTIFACT_STATUS_FAILED,
    }
    payload = {
        key: value
        for key, value in child_result.items()
        if key not in {"task_id", "status"}
    }
    if isinstance(payload.get("artifacts"), list):
        payload["artifacts"] = _copy_child_artifacts_to_parent(
            parent_drive_root,
            task_id,
            child_drive,
            [item for item in payload.get("artifacts") or [] if isinstance(item, dict)],
        )
        try:
            from ouroboros.outcomes import artifact_bundle_from_result

            payload["artifact_bundle"] = artifact_bundle_from_result(payload)
        except Exception:
            payload.pop("artifact_bundle", None)
    if preserve_parent_artifacts:
        payload["artifacts"] = _merge_artifacts(
            list((existing or {}).get("artifacts") or []),
            list(payload.get("artifacts") or []),
        )
        for key in _ARTIFACT_LIFECYCLE_FIELDS:
            if key in (existing or {}):
                payload[key] = (existing or {}).get(key)
    payload.setdefault("headless_child_drive_root", str(child_drive))
    if workspace_task and child_status in _FINAL_STATUSES:
        if not preserve_parent_artifacts and existing_artifact_status not in {ARTIFACT_STATUS_READY, ARTIFACT_STATUS_FAILED}:
            payload["artifact_status"] = ARTIFACT_STATUS_FINALIZING
        payload["child_status"] = child_status
    return write_task_result(
        parent_drive_root,
        task_id,
        child_status,
        **payload,
    )


def _copy_child_artifacts_to_parent(
    parent_drive_root: pathlib.Path,
    task_id: str,
    child_drive: pathlib.Path,
    artifacts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Rebase child-drive artifact files into the parent task artifact store."""

    parent_dir = task_artifacts_dir(parent_drive_root, task_id)
    rebased: List[Dict[str, Any]] = []
    for artifact in artifacts:
        item = dict(artifact)
        raw_path = str(item.get("path") or "").strip()
        if not raw_path:
            rebased.append(item)
            continue
        src = pathlib.Path(raw_path)
        if not src.is_absolute():
            src = (child_drive / raw_path).resolve(strict=False)
        try:
            src.resolve(strict=False).relative_to(parent_dir.resolve(strict=False))
            rebased.append(item)
            continue
        except ValueError:
            pass
        if not src.is_file():
            rebased.append(item)
            continue
        dest = parent_dir / src.name
        if dest.exists() and dest.resolve(strict=False) != src.resolve(strict=False):
            dest = parent_dir / f"{src.stem}_{sha256(str(src).encode('utf-8')).hexdigest()[:8]}{src.suffix}"
        shutil.copy2(src, dest)
        data = dest.read_bytes()
        item["path"] = str(dest)
        item["name"] = str(item.get("name") or dest.name)
        item["size"] = len(data)
        item["sha256"] = sha256(data).hexdigest()
        rebased.append(item)
    return rebased


def finalize_task_artifacts(parent_drive_root: pathlib.Path, task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Write patch/memory-export artifacts for a completed headless task."""

    artifacts: List[Dict[str, Any]] = []
    task_id = str(task.get("id") or "")
    if not task_id:
        return artifacts

    artifact_dir = task_artifacts_dir(parent_drive_root, task_id)
    workspace_root = _workspace_root_from_task(task)
    existing = load_task_result(parent_drive_root, task_id) or {}
    status = str(existing.get("status") or "completed")
    artifact_status = ARTIFACT_STATUS_READY
    artifact_error = ""
    if workspace_root is not None:
        write_task_result(
            parent_drive_root,
            task_id,
            status,
            artifact_status=ARTIFACT_STATUS_FINALIZING,
        )
        try:
            patch_artifacts, manifest = write_workspace_patch_artifacts(
                workspace_root,
                artifact_dir,
                task=task,
            )
            artifacts.extend(patch_artifacts)
            if manifest.get("status") == ARTIFACT_STATUS_FAILED:
                artifact_status = ARTIFACT_STATUS_FAILED
                artifact_error = "; ".join(str(err.get("message") or err) for err in manifest.get("errors") or [])[:1000]
        except Exception as exc:
            artifact_status = ARTIFACT_STATUS_FAILED
            artifact_error = f"{type(exc).__name__}: {exc}"
            manifest_path = artifact_dir / "workspace_patch.json"
            manifest = _empty_patch_manifest(
                workspace_root,
                status=ARTIFACT_STATUS_FAILED,
                errors=[{"type": "exception", "message": artifact_error}],
            )
            atomic_write_json(
                manifest_path,
                manifest,
                trailing_newline=True,
            )
            artifacts.append({
                "kind": "workspace_patch_manifest",
                "name": "workspace_patch.json",
                "path": str(manifest_path),
                "size": manifest_path.stat().st_size if manifest_path.exists() else 0,
                "workspace_root": str(workspace_root),
            })

    child_drive = _child_drive_from_task(task)
    if child_drive is not None:
        try:
            export_path = artifact_dir / "memory_export.json"
            atomic_write_json(export_path, build_memory_export(child_drive, task), trailing_newline=True)
            artifacts.append({
                "kind": "memory_export",
                "name": "memory_export.json",
                "path": str(export_path),
                "size": export_path.stat().st_size if export_path.exists() else 0,
                "memory_mode": str(task.get("memory_mode") or ""),
            })
        except Exception as exc:
            if workspace_root is not None:
                artifact_status = ARTIFACT_STATUS_FAILED
            message = f"{type(exc).__name__}: {exc}"
            artifact_error = f"{artifact_error}; {message}" if artifact_error else message

    if artifacts or workspace_root is not None:
        existing = load_task_result(parent_drive_root, task_id) or {}
        drop_kinds = {"workspace_patch"} if workspace_root is not None and artifact_status == ARTIFACT_STATUS_FAILED else set()
        merged = _merge_artifacts(list(existing.get("artifacts") or []), artifacts, drop_kinds=drop_kinds)
        fields: Dict[str, Any] = {
            "artifacts": merged,
            "artifact_status": artifact_status if workspace_root is not None else str(existing.get("artifact_status") or ""),
            "artifact_finalized_at": utc_now_iso(),
        }
        if artifact_error:
            fields["artifact_error"] = artifact_error
        provisional = {
            **existing,
            **fields,
            "artifacts": merged,
            "artifact_status": fields.get("artifact_status", existing.get("artifact_status")),
        }
        try:
            from ouroboros.outcomes import artifact_bundle_from_result, refresh_verification_ledger_artifacts

            artifact_bundle = artifact_bundle_from_result(provisional)
            fields["artifact_bundle"] = artifact_bundle
            refreshed_ledger = refresh_verification_ledger_artifacts(
                existing.get("verification_ledger"),
                artifact_bundle,
            )
            if refreshed_ledger is not None:
                fields["verification_ledger"] = refreshed_ledger
        except Exception:
            pass
        write_task_result(
            parent_drive_root,
            task_id,
            str(existing.get("status") or status or "completed"),
            **fields,
        )
    return artifacts


def build_workspace_patch(workspace_root: pathlib.Path) -> str:
    """Return a git patch for tracked changes plus untracked files."""

    with tempfile.TemporaryDirectory() as tmp:
        artifacts, manifest = write_workspace_patch_artifacts(
            pathlib.Path(workspace_root),
            pathlib.Path(tmp),
            task={},
        )
        if manifest.get("status") == ARTIFACT_STATUS_FAILED:
            return ""
        for artifact in artifacts:
            if artifact.get("kind") == "workspace_patch":
                path = pathlib.Path(str(artifact.get("path") or ""))
                return path.read_text(encoding="utf-8") if path.is_file() else ""
    return ""


def write_workspace_patch_artifacts(
    workspace_root: pathlib.Path,
    artifact_dir: pathlib.Path,
    *,
    task: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Stream workspace patch and manifest artifacts into ``artifact_dir``."""

    root = pathlib.Path(workspace_root).resolve(strict=False)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifact_dir / "workspace.patch"
    manifest_path = artifact_dir / "workspace_patch.json"
    errors: List[Dict[str, Any]] = []
    diagnostics: List[Dict[str, Any]] = []
    excluded: List[Dict[str, str]] = []
    sensitive: List[Dict[str, str]] = []
    included_untracked: List[str] = []
    changed_tracked = _git_path_list(
        ["git", "diff", "--name-only", "-z", "--no-ext-diff", "--no-color", "HEAD", "--"],
        root,
        errors,
    )
    diffstat = _git_stdout(
        ["git", "diff", "--stat", "--no-ext-diff", "--no-color", "HEAD", "--"],
        root,
        allow_rc={0},
        errors=errors,
    )
    untracked = _untracked_files(root, errors)
    for rel in untracked:
        sensitive_reason = _sensitive_untracked_reason(rel)
        if sensitive_reason:
            sensitive.append({"path": rel, "reason": sensitive_reason})
            continue
        reason = _patch_exclude_reason(rel)
        if reason:
            excluded.append({"path": rel, "reason": reason})
            continue
        included_untracked.append(rel)
    if sensitive:
        errors.append({
            "type": "sensitive_untracked_files",
            "message": "untracked sensitive-looking files are not included in workspace patch",
            "paths": [item["path"] for item in sensitive],
        })

    hasher = sha256()
    total_size = 0
    with patch_path.open("wb") as fh:
        if not errors:
            total_size += _append_git_output(
                ["git", "diff", "--binary", "--no-ext-diff", "--no-color", "HEAD", "--"],
                root,
                fh,
                hasher,
                allow_rc={0},
                errors=errors,
                diagnostics=diagnostics,
            )
            for rel in included_untracked:
                if total_size:
                    total_size += _write_patch_separator(fh, hasher)
                total_size += _append_git_output(
                    ["git", "diff", "--no-index", "--binary", "--no-ext-diff", "--no-color", "--", os.devnull, rel],
                    root,
                    fh,
                    hasher,
                    allow_rc={0, 1},
                    errors=errors,
                    diagnostics=diagnostics,
                )
    if errors:
        try:
            patch_path.unlink()
        except OSError:
            pass
        total_size = 0
        digest = ""
    else:
        digest = hasher.hexdigest()

    head_error: Dict[str, Any] | None = None
    expected_head = _preflight_head_from_task(task)
    head_errors: List[Dict[str, Any]] = []
    current_head = _git_stdout(["git", "rev-parse", "HEAD"], root, allow_rc={0}, errors=head_errors).strip()
    if expected_head and not current_head:
        errors.extend(head_errors)
        head_error = {
            "type": "workspace_head_unverified",
            "message": "workspace HEAD could not be verified at artifact finalization",
            "expected_head": expected_head,
            "current_head": "",
        }
        errors.append(head_error)
    elif expected_head and current_head != expected_head:
        head_error = {
            "type": "workspace_head_changed",
            "message": "workspace HEAD changed during task execution; patch artifact is invalid",
            "expected_head": expected_head,
            "current_head": current_head,
        }
        errors.append(head_error)
    if head_error:
        try:
            patch_path.unlink()
        except OSError:
            pass
        total_size = 0
        digest = ""

    status = ARTIFACT_STATUS_FAILED if errors else ARTIFACT_STATUS_READY
    manifest = {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "status": status,
        "workspace_root": str(root),
        "patch_name": "workspace.patch",
        "manifest_name": "workspace_patch.json",
        "patch_size": total_size,
        "sha256": digest,
        "diffstat": diffstat,
        "counts": {
            "tracked_changed": len(changed_tracked),
            "untracked_included": len(included_untracked),
            "untracked_excluded": len(excluded),
            "sensitive_blocked": len(sensitive),
        },
        "tracked_changed": changed_tracked,
        "untracked_included": included_untracked,
        "untracked_excluded": excluded,
        "sensitive_blocked": sensitive,
        "exclude_rules_version": _PATCH_EXCLUDE_RULES_VERSION,
        "diagnostics": diagnostics,
        "errors": errors,
    }
    atomic_write_json(manifest_path, manifest, trailing_newline=True)
    artifacts = [
        {
            "kind": "workspace_patch_manifest",
            "name": "workspace_patch.json",
            "path": str(manifest_path),
            "size": manifest_path.stat().st_size if manifest_path.exists() else 0,
            "workspace_root": str(root),
        }
    ]
    if status == ARTIFACT_STATUS_READY:
        artifacts.insert(0, {
            "kind": "workspace_patch",
            "name": "workspace.patch",
            "path": str(patch_path),
            "size": total_size,
            "sha256": digest,
            "workspace_root": str(root),
        })
    return artifacts, manifest


def build_memory_export(child_drive_root: pathlib.Path, task: Dict[str, Any]) -> Dict[str, Any]:
    """Create an explicit export artifact without merging it into parent memory."""

    root = pathlib.Path(child_drive_root)
    memory_root = root / "memory"
    files: Dict[str, str] = {}
    if memory_root.is_dir():
        for path in sorted(memory_root.rglob("*")):
            if not path.is_file() or path.name.startswith("."):
                continue
            try:
                rel = str(path.relative_to(memory_root)).replace(os.sep, "/")
                files[rel] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "task_id": str(task.get("id") or ""),
        "memory_mode": str(task.get("memory_mode") or ""),
        "child_drive_root": str(root),
        "files": files,
    }


def _copy_stable_memory(parent: pathlib.Path, child: pathlib.Path) -> None:
    parent_memory = parent / "memory"
    child_memory = child / "memory"
    for rel in ("identity.md", "WORLD.md", "registry.md"):
        src = parent_memory / rel
        if src.is_file():
            dst = child_memory / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    src_knowledge = parent_memory / "knowledge"
    dst_knowledge = child_memory / "knowledge"
    if src_knowledge.is_dir():
        shutil.copytree(src_knowledge, dst_knowledge, dirs_exist_ok=True)


def _child_drive_from_task(task: Dict[str, Any]) -> Optional[pathlib.Path]:
    text = str(task.get("drive_root") or task.get("child_drive_root") or "").strip()
    return pathlib.Path(text) if text else None


def _workspace_root_from_task(task: Dict[str, Any]) -> Optional[pathlib.Path]:
    text = str(task.get("workspace_root") or "").strip()
    if not text:
        meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        text = str(meta.get("workspace_root") or "").strip()
    return pathlib.Path(text) if text else None


def _git_stdout(
    cmd: Sequence[str],
    cwd: pathlib.Path,
    *,
    allow_rc: Iterable[int] = (0,),
    errors: Optional[List[Dict[str, Any]]] = None,
) -> str:
    try:
        result = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        if errors is not None:
            errors.append({"type": "git_timeout", "command": list(cmd), "message": "git command timed out"})
        return ""
    except Exception as exc:
        if errors is not None:
            errors.append({"type": "git_exception", "command": list(cmd), "message": f"{type(exc).__name__}: {exc}"})
        return ""
    if result.returncode not in set(allow_rc):
        if errors is not None:
            errors.append({
                "type": "git_error",
                "command": list(cmd),
                "returncode": result.returncode,
                "stderr": (result.stderr or "")[-2000:],
            })
        return ""
    return result.stdout or ""


def _git_lines(cmd: Sequence[str], root: pathlib.Path, errors: List[Dict[str, Any]]) -> List[str]:
    return [line.strip() for line in _git_stdout(cmd, root, errors=errors).splitlines() if line.strip()]


def _untracked_files(root: pathlib.Path, errors: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    return _git_path_list(["git", "ls-files", "-z", "--others", "--exclude-standard"], root, errors)


def _git_path_list(cmd: Sequence[str], root: pathlib.Path, errors: Optional[List[Dict[str, Any]]] = None) -> List[str]:
    output = _git_bytes(cmd, root, errors=errors)
    if not output:
        return []
    return [part.decode("utf-8", errors="replace") for part in output.split(b"\0") if part]


def _git_bytes(
    cmd: Sequence[str],
    cwd: pathlib.Path,
    *,
    allow_rc: Iterable[int] = (0,),
    errors: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    try:
        result = subprocess.run(
            list(cmd),
            cwd=str(cwd),
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        if errors is not None:
            errors.append({"type": "git_timeout", "command": list(cmd), "message": "git command timed out"})
        return b""
    except Exception as exc:
        if errors is not None:
            errors.append({"type": "git_exception", "command": list(cmd), "message": f"{type(exc).__name__}: {exc}"})
        return b""
    if result.returncode not in set(allow_rc):
        if errors is not None:
            errors.append({
                "type": "git_error",
                "command": list(cmd),
                "returncode": result.returncode,
                "stderr": (result.stderr or b"").decode("utf-8", errors="replace")[-2000:],
            })
        return b""
    return result.stdout or b""


def _append_git_output(
    cmd: Sequence[str],
    cwd: pathlib.Path,
    fh: BinaryIO,
    hasher: Any,
    *,
    allow_rc: set[int],
    errors: List[Dict[str, Any]],
    diagnostics: List[Dict[str, Any]],
) -> int:
    written_box = {"value": 0}
    read_errors: List[str] = []
    try:
        with tempfile.TemporaryFile() as stderr_fh:
            proc = subprocess.Popen(
                list(cmd),
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=stderr_fh,
            )
            assert proc.stdout is not None

            def _reader() -> None:
                try:
                    while True:
                        chunk = proc.stdout.read(1024 * 128)
                        if not chunk:
                            break
                        fh.write(chunk)
                        hasher.update(chunk)
                        written_box["value"] += len(chunk)
                except Exception as exc:
                    read_errors.append(f"{type(exc).__name__}: {exc}")

            reader = threading.Thread(target=_reader, name="workspace-patch-git-stdout", daemon=True)
            reader.start()
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                reader.join(timeout=5)
                if reader.is_alive():
                    errors.append({"type": "git_timeout", "command": list(cmd), "message": "git stdout reader timed out"})
                errors.append({"type": "git_timeout", "command": list(cmd), "message": "git command timed out"})
                return int(written_box["value"])
            reader.join(timeout=5)
            if reader.is_alive():
                errors.append({"type": "git_timeout", "command": list(cmd), "message": "git stdout reader timed out"})
            for read_error in read_errors:
                errors.append({"type": "git_exception", "command": list(cmd), "message": read_error})
            stderr_fh.seek(0)
            stderr = stderr_fh.read() or b""
    except subprocess.TimeoutExpired:
        try:
            proc.kill()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        errors.append({"type": "git_timeout", "command": list(cmd), "message": "git command timed out"})
        return int(written_box["value"])
    except Exception as exc:
        errors.append({"type": "git_exception", "command": list(cmd), "message": f"{type(exc).__name__}: {exc}"})
        return int(written_box["value"])
    if proc.returncode not in allow_rc:
        errors.append({
            "type": "git_error",
            "command": list(cmd),
            "returncode": proc.returncode,
            "stderr": stderr.decode("utf-8", errors="replace")[-2000:],
        })
    written = int(written_box["value"])
    diagnostics.append({"command": list(cmd), "returncode": proc.returncode, "bytes": written})
    return written


def _write_patch_separator(fh: BinaryIO, hasher: Any) -> int:
    data = b"\n"
    fh.write(data)
    hasher.update(data)
    return len(data)


def _patch_exclude_reason(rel: str) -> str:
    parts = pathlib.PurePosixPath(str(rel).replace("\\", "/")).parts
    if not parts:
        return ""
    if parts[0] in _TOP_LEVEL_EXCLUDE_DIRS:
        return f"top-level env/cache directory: {parts[0]}"
    for part in parts:
        if part in _ANY_SEGMENT_EXCLUDE_DIRS:
            return f"env/cache directory segment: {part}"
    return ""


def _sensitive_untracked_reason(rel: str) -> str:
    name = pathlib.PurePosixPath(str(rel).replace("\\", "/")).name
    lower = name.lower()
    if lower.startswith(".env") and not lower.endswith(_SENSITIVE_EXAMPLE_SUFFIXES):
        return "dotenv secret"
    if lower in _SENSITIVE_KEY_NAMES or lower in _SENSITIVE_FILENAMES:
        return "credential filename"
    if lower.endswith((".pem", ".key", ".p12", ".pfx")):
        return "private key or certificate"
    return ""


def _preflight_head_from_task(task: Dict[str, Any]) -> str:
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    preflight = meta.get("workspace_preflight") if isinstance(meta.get("workspace_preflight"), dict) else {}
    git = preflight.get("git") if isinstance(preflight.get("git"), dict) else {}
    return str(git.get("head") or "")


def _empty_patch_manifest(
    workspace_root: pathlib.Path,
    *,
    status: str,
    errors: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "status": status,
        "workspace_root": str(workspace_root),
        "patch_name": "workspace.patch",
        "manifest_name": "workspace_patch.json",
        "patch_size": 0,
        "sha256": "",
        "diffstat": "",
        "counts": {
            "tracked_changed": 0,
            "untracked_included": 0,
            "untracked_excluded": 0,
            "sensitive_blocked": 0,
        },
        "tracked_changed": [],
        "untracked_included": [],
        "untracked_excluded": [],
        "sensitive_blocked": [],
        "exclude_rules_version": _PATCH_EXCLUDE_RULES_VERSION,
        "diagnostics": [],
        "errors": errors,
    }


def _merge_artifacts(
    existing: List[Dict[str, Any]],
    new_items: List[Dict[str, Any]],
    *,
    drop_kinds: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    drop = drop_kinds or set()
    key_for = lambda item: (
        str(item.get("kind") or ""),
        str(item.get("name") or pathlib.Path(str(item.get("path") or "")).name),
    )
    keys = {key_for(item) for item in new_items if isinstance(item, dict)}
    for item in existing:
        if not isinstance(item, dict):
            continue
        key = key_for(item)
        if key[0] not in drop and key not in keys:
            merged.append(item)
    merged.extend(new_items)
    return merged


__all__ = [
    "ARTIFACT_STATUS_FAILED",
    "ARTIFACT_STATUS_FINALIZING",
    "ARTIFACT_STATUS_PENDING",
    "ARTIFACT_STATUS_READY",
    "build_memory_export",
    "build_workspace_patch",
    "copy_child_task_result",
    "finalize_task_artifacts",
    "prepare_task_drive",
    "prune_headless_task_drives",
    "prune_task_drives",
    "task_artifacts_dir",
    "task_state_dir",
    "write_workspace_patch_artifacts",
    "write_workspace_preflight_artifact",
]

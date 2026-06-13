"""Task-scoped artifact helpers shared by tools and outcome finalization."""

from __future__ import annotations

import pathlib
import shutil
from hashlib import sha256
from typing import Any, Dict, Iterable, List, Union

from ouroboros.utils import atomic_write_json, read_json_dict
from ouroboros.headless import ARTIFACT_STATUS_READY, task_artifacts_dir
from ouroboros.task_results import validate_task_id

_ARTIFACT_MANIFEST = ".artifact_manifest.json"


def artifact_store_path_block_reason(path: pathlib.Path) -> str:
    """Return a block reason for task-artifact control/provenance paths."""

    try:
        parts = pathlib.Path(path).parts
    except TypeError:
        parts = (str(path),)
    for part in parts:
        if part.startswith("."):
            return "artifact_store hidden/control metadata paths are reserved"
    return ""


def task_artifact_dir_path(drive_root: Union[pathlib.Path, str], task_id: str, *, create: bool = False) -> pathlib.Path:
    """Return the task artifact directory without creating it unless requested."""

    return task_artifacts_dir(pathlib.Path(drive_root), validate_task_id(task_id), create=create)


def task_id_for_artifacts(ctx: Any) -> str:
    """Return a stable task id for artifact storage."""

    for value in (
        getattr(ctx, "task_id", None),
        (getattr(ctx, "task_metadata", {}) or {}).get("task_id")
        if isinstance(getattr(ctx, "task_metadata", {}), dict)
        else "",
        (getattr(ctx, "task_metadata", {}) or {}).get("id")
        if isinstance(getattr(ctx, "task_metadata", {}), dict)
        else "",
    ):
        try:
            return validate_task_id(value)
        except ValueError:
            continue
    return "interactive"


def artifact_record(path: pathlib.Path, *, kind: str = "task_artifact", source_path: str = "") -> Dict[str, Any]:
    raw = pathlib.Path(path).read_bytes()
    record: Dict[str, Any] = {
        "kind": kind,
        "name": pathlib.Path(path).name,
        "path": str(path),
        "size": len(raw),
        "sha256": sha256(raw).hexdigest(),
        "status": ARTIFACT_STATUS_READY,
        "errors": [],
    }
    if source_path:
        record["source_path"] = source_path
    return record


def copy_file_to_task_artifacts(ctx: Any, source_path: Union[pathlib.Path, str], *, kind: str = "user_file") -> Dict[str, Any] | None:
    """Copy a generated file into this task's canonical artifact store."""

    source = pathlib.Path(source_path).expanduser().resolve(strict=False)
    if not source.is_file():
        return None
    task_id = task_id_for_artifacts(ctx)
    artifact_dir = task_artifact_dir_path(pathlib.Path(getattr(ctx, "drive_root")), task_id, create=True)
    data = read_json_dict(artifact_dir / _ARTIFACT_MANIFEST) or {}
    manifest = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    manifest = {str(key): dict(value) for key, value in manifest.items() if isinstance(value, dict)}
    dest = artifact_dir / source.name
    reused_existing_source = False
    for existing in manifest.values():
        existing_source = str(existing.get("source_path") or "")
        existing_path = str(existing.get("path") or "")
        if existing_source == str(source) and existing_path:
            candidate = pathlib.Path(existing_path).resolve(strict=False)
            if candidate.parent == artifact_dir.resolve(strict=False):
                dest = candidate
                reused_existing_source = True
                break
    if dest.exists() and dest.resolve(strict=False) != source.resolve(strict=False) and not reused_existing_source:
        suffix = source.suffix
        stem = source.name[: -len(suffix)] if suffix else source.name
        digest = sha256(str(source.resolve(strict=False)).encode("utf-8", errors="replace")).hexdigest()[:8]
        dest = artifact_dir / f"{stem}.{digest}{suffix}"
    if dest.resolve(strict=False) != source.resolve(strict=False):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    record = artifact_record(dest, kind=kind, source_path=str(source))
    manifest[pathlib.Path(str(record.get("path") or record.get("name") or "")).name] = dict(record)
    atomic_write_json(artifact_dir / _ARTIFACT_MANIFEST, {"schema_version": 1, "artifacts": manifest}, trailing_newline=True)
    return record


def collect_task_artifact_records(drive_root: Union[pathlib.Path, str], task_id: str) -> List[Dict[str, Any]]:
    """Return records for files already present in the task artifact store."""

    try:
        artifact_dir = task_artifact_dir_path(pathlib.Path(drive_root), validate_task_id(task_id), create=False)
    except ValueError:
        return []
    records: List[Dict[str, Any]] = []
    if not artifact_dir.exists():
        return records
    data = read_json_dict(artifact_dir / _ARTIFACT_MANIFEST) or {}
    raw_manifest = data.get("artifacts") if isinstance(data.get("artifacts"), dict) else {}
    manifest = {str(key): dict(value) for key, value in raw_manifest.items() if isinstance(value, dict)}
    artifact_root = artifact_dir.resolve(strict=False)
    for path in sorted(p for p in artifact_dir.rglob("*") if p.is_file() and not p.is_symlink()):
        if path.name == _ARTIFACT_MANIFEST:
            continue
        try:
            path.resolve(strict=False).relative_to(artifact_root)
            record = artifact_record(path)
            manifest_record = manifest.get(path.name)
            if manifest_record:
                record.update({
                    key: value
                    for key, value in manifest_record.items()
                    if key not in {"path", "size", "sha256", "status", "errors"} and value
                })
            records.append(record)
        except OSError:
            continue
    return records


def merge_artifact_records(*groups: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            key = str(item.get("path") or item.get("name") or "")
            if not key:
                continue
            if key not in merged:
                order.append(key)
                merged[key] = dict(item)
                continue
            existing = merged[key]
            fresh = dict(item)
            merged[key] = {**existing, **fresh}
            if existing.get("kind") and fresh.get("kind") == "task_artifact" and existing.get("kind") != "task_artifact":
                merged[key]["kind"] = existing["kind"]
            for meta_key in ("kind", "source_path", "name"):
                if existing.get(meta_key) and not fresh.get(meta_key):
                    merged[key][meta_key] = existing[meta_key]
            if existing.get("name") and fresh.get("kind") == "task_artifact":
                merged[key]["name"] = existing["name"]
    return [merged[key] for key in order]

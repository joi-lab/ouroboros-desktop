"""Headless task helpers for CLI/workspace runs.

The gateway owns task transport; this module owns the small amount of local
filesystem state needed for isolated external runs and patch artifacts.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Optional

from ouroboros.task_results import load_task_result, validate_task_id, write_task_result
from ouroboros.utils import atomic_write_json, utc_now_iso


HEADLESS_TASKS_DIR = pathlib.Path("state") / "headless_tasks"
ARTIFACTS_DIR = pathlib.Path("task_results") / "artifacts"


def task_state_dir(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return pathlib.Path(drive_root) / HEADLESS_TASKS_DIR / validate_task_id(task_id)


def task_artifacts_dir(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    path = pathlib.Path(drive_root) / ARTIFACTS_DIR / validate_task_id(task_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


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


def copy_child_task_result(parent_drive_root: pathlib.Path, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Copy a child-drive task result back to the parent data root."""

    task_id = str(task.get("id") or "")
    child_drive = _child_drive_from_task(task)
    if not task_id or child_drive is None:
        return None
    child_result = load_task_result(child_drive, task_id)
    if not isinstance(child_result, dict):
        return None
    payload = {
        key: value
        for key, value in child_result.items()
        if key not in {"task_id", "status"}
    }
    payload.setdefault("headless_child_drive_root", str(child_drive))
    return write_task_result(
        parent_drive_root,
        task_id,
        str(child_result.get("status") or "completed"),
        **payload,
    )


def finalize_task_artifacts(parent_drive_root: pathlib.Path, task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Write patch/memory-export artifacts for a completed headless task."""

    artifacts: List[Dict[str, Any]] = []
    task_id = str(task.get("id") or "")
    if not task_id:
        return artifacts

    artifact_dir = task_artifacts_dir(parent_drive_root, task_id)
    workspace_root = _workspace_root_from_task(task)
    if workspace_root is not None:
        patch_text = build_workspace_patch(workspace_root)
        patch_path = artifact_dir / "workspace.patch"
        patch_path.write_text(patch_text, encoding="utf-8")
        artifacts.append({
            "kind": "workspace_patch",
            "path": str(patch_path),
            "size": len(patch_text.encode("utf-8")),
            "workspace_root": str(workspace_root),
        })

    child_drive = _child_drive_from_task(task)
    if child_drive is not None:
        export_path = artifact_dir / "memory_export.json"
        atomic_write_json(export_path, build_memory_export(child_drive, task), trailing_newline=True)
        artifacts.append({
            "kind": "memory_export",
            "path": str(export_path),
            "size": export_path.stat().st_size if export_path.exists() else 0,
            "memory_mode": str(task.get("memory_mode") or ""),
        })

    if artifacts:
        existing = load_task_result(parent_drive_root, task_id) or {}
        merged = list(existing.get("artifacts") or [])
        merged.extend(artifacts)
        write_task_result(
            parent_drive_root,
            task_id,
            str(existing.get("status") or "completed"),
            artifacts=merged,
        )
    return artifacts


def build_workspace_patch(workspace_root: pathlib.Path) -> str:
    """Return a git patch for tracked changes plus untracked files."""

    root = pathlib.Path(workspace_root)
    if not root.exists() or not root.is_dir():
        return ""
    tracked = _git_stdout(["git", "diff", "--binary", "HEAD", "--"], root)
    untracked_parts = []
    for rel in _untracked_files(root):
        diff = _git_stdout(["git", "diff", "--no-index", "--binary", "--", os.devnull, rel], root, allow_rc={0, 1})
        if diff.strip():
            untracked_parts.append(diff)
    parts = [part for part in [tracked, *untracked_parts] if part.strip()]
    return "\n".join(parts)


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
    cmd: List[str],
    cwd: pathlib.Path,
    *,
    allow_rc: Iterable[int] = (0,),
) -> str:
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return ""
    if result.returncode not in set(allow_rc):
        return ""
    return result.stdout or ""


def _untracked_files(root: pathlib.Path) -> List[str]:
    output = _git_stdout(["git", "ls-files", "--others", "--exclude-standard"], root)
    return [line.strip() for line in output.splitlines() if line.strip()]


__all__ = [
    "build_memory_export",
    "build_workspace_patch",
    "copy_child_task_result",
    "finalize_task_artifacts",
    "prepare_task_drive",
    "task_artifacts_dir",
    "task_state_dir",
]

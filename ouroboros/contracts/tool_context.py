"""Minimal runtime-checkable ToolContext ABI for tools and extensions."""

from __future__ import annotations

import pathlib
from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ToolContextProtocol(Protocol):
    """Minimum ToolContext fields; extend only with a deliberate contract bump."""

    # Filesystem roots.
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    workspace_root: pathlib.Path | None
    workspace_mode: str
    budget_drive_root: str
    # Per-project facts scope: when set, knowledge reads/writes target the
    # per-project store (projects/<id>/knowledge); empty = canonical memory.
    project_id: str

    # Runtime drains pending_events; emit_progress_fn is best-effort.
    pending_events: list
    emit_progress_fn: Callable[[str], Any]

    # May be None outside a running task.
    current_chat_id: Any
    task_id: Any
    task_metadata: dict
    task_contract: dict

    # Boundary-checked path helpers.
    def repo_path(self, rel: str) -> pathlib.Path: ...
    def active_repo_dir(self) -> pathlib.Path: ...
    def is_workspace_mode(self) -> bool: ...
    def drive_path(self, rel: str) -> pathlib.Path: ...
    def drive_logs(self) -> pathlib.Path: ...


__all__ = ["ToolContextProtocol"]

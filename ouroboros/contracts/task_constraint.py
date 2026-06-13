"""Structured per-task execution constraints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


_LOCAL_READONLY_SUBAGENT_MODE = "local_readonly_subagent"


@dataclass(frozen=True)
class TaskConstraint:
    mode: str = "normal"
    skill_name: str = ""
    payload_root: str = ""
    allow_enable: bool = True
    allow_review: bool = True
    extra_allowlist: tuple[str, ...] = ()


def normalize_task_constraint(value: Any) -> Optional[TaskConstraint]:
    if isinstance(value, TaskConstraint):
        if value.mode == _LOCAL_READONLY_SUBAGENT_MODE:
            return TaskConstraint(mode=_LOCAL_READONLY_SUBAGENT_MODE, allow_enable=False, allow_review=False)
        return value
    if not isinstance(value, Mapping):
        return None
    extra = value.get("extra_allowlist") or ()
    if not isinstance(extra, (list, tuple)):
        extra = ()
    mode = str(value.get("mode") or "normal").strip() or "normal"
    if mode == _LOCAL_READONLY_SUBAGENT_MODE:
        return TaskConstraint(mode=_LOCAL_READONLY_SUBAGENT_MODE, allow_enable=False, allow_review=False)
    return TaskConstraint(
        mode=mode,
        skill_name=str(value.get("skill_name") or "").strip(),
        payload_root=str(value.get("payload_root") or "").strip().replace("\\", "/").strip("/"),
        allow_enable=_coerce_bool(value.get("allow_enable", True), default=True),
        allow_review=_coerce_bool(value.get("allow_review", True), default=True),
        extra_allowlist=tuple(str(item) for item in extra if str(item).strip()),
    )


def _coerce_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return default
    return default


def resolve_payload_path(drive_root: Path, constraint: TaskConstraint, path_text: str) -> Path:
    from ouroboros.contracts.skill_payload_policy import resolve_constrained_payload_path

    return resolve_constrained_payload_path(drive_root, constraint, path_text)

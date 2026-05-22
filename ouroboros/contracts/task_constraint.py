"""Structured per-task execution constraints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


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
        return value
    if not isinstance(value, Mapping):
        return None
    extra = value.get("extra_allowlist") or ()
    if not isinstance(extra, (list, tuple)):
        extra = ()
    return TaskConstraint(
        mode=str(value.get("mode") or "normal").strip() or "normal",
        skill_name=str(value.get("skill_name") or "").strip(),
        payload_root=str(value.get("payload_root") or "").strip().replace("\\", "/").strip("/"),
        allow_enable=bool(value.get("allow_enable", True)),
        allow_review=bool(value.get("allow_review", True)),
        extra_allowlist=tuple(str(item) for item in extra if str(item).strip()),
    )


def resolve_payload_path(drive_root: Path, constraint: TaskConstraint, path_text: str) -> Path:
    from ouroboros.contracts.skill_payload_policy import resolve_constrained_payload_path

    return resolve_constrained_payload_path(drive_root, constraint, path_text)

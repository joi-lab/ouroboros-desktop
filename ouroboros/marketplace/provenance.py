"""Atomic durable provenance records for ClawHub-installed skills.

Records live beside skill review/enablement state and preserve install-time
fields across updates for operator cross-checks against registry metadata.
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, Optional

from ouroboros.skill_loader import skill_state_dir
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)


_SCHEMA_VERSION = 1
PROVENANCE_FILENAME = "clawhub.json"


def write_provenance(
    drive_root: pathlib.Path,
    skill_name: str,
    record: Dict[str, Any],
) -> pathlib.Path:
    """Persist a provenance record and return its path on disk."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    payload = dict(record or {})
    payload.setdefault("schema_version", _SCHEMA_VERSION)
    payload.setdefault("source", "clawhub")
    now_iso = utc_now_iso()
    payload.setdefault("installed_at", now_iso)
    payload["updated_at"] = now_iso
    atomic_write_json(target, payload, trailing_newline=True)
    return target


def read_provenance(
    drive_root: pathlib.Path,
    skill_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the persisted provenance for ``skill_name`` or ``None``."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    if not target.is_file():
        return None
    return read_json_dict(target)


def delete_provenance(drive_root: pathlib.Path, skill_name: str) -> None:
    """Remove the provenance file (idempotent)."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    try:
        if target.is_file():
            target.unlink()
    except OSError:
        log.warning("Failed to delete provenance file %s", target, exc_info=True)


__all__ = [
    "PROVENANCE_FILENAME",
    "delete_provenance",
    "read_provenance",
    "write_provenance",
]

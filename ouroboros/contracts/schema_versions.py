"""Opt-in schema-version helpers for future durable state payloads.

Existing files are not retrofitted; missing/invalid versions read as legacy 0.
The ``_schema_version`` key avoids the advisory_review.json ``state_version``.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


SCHEMA_VERSION_KEY = "_schema_version"


def with_schema_version(payload: Mapping[str, Any], version: int) -> Dict[str, Any]:
    """Return a shallow copy with SCHEMA_VERSION_KEY set; never mutate input."""
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"with_schema_version expects a mapping, got {type(payload).__name__}"
        )
    out: Dict[str, Any] = dict(payload)
    out[SCHEMA_VERSION_KEY] = int(version)
    return out


def read_schema_version(payload: Any, default: int = 0) -> int:
    """Return declared schema version or default when missing/invalid."""
    if not isinstance(payload, Mapping):
        return int(default)
    raw = payload.get(SCHEMA_VERSION_KEY, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "SCHEMA_VERSION_KEY",
    "with_schema_version",
    "read_schema_version",
]

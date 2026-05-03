"""
Credentials sidecar — gitignored JSON files under ``<DATA_DIR>/`` for
external-service credentials that should NOT live in ``settings.json``.

Why a sidecar
=============

``settings.json`` is what users paste into bug reports, attach to support
requests, and what some skills read for diagnostics. JWTs and API tokens
in there become leaks waiting to happen.

The sidecar pattern:
    - JSON file at ``<DATA_DIR>/<name>-credentials.json``
    - explicitly gitignored
    - read by helper functions in this module on demand
    - JWT/token only ever touches process memory during the request that
      uses it; SHA256 fingerprint is what stays cached for safety
      validation

Schema
======

Each sidecar is free-form JSON, but skills using it should document
their own shape. Common conventions:

.. code:: json

    {
        "_schema": "openbotcity-credentials/1",
        "service": "openbotcity",
        "bot_id": "cca9af58-407f-46f7-8b49-e50f066a2cbe",
        "slug": "pixelcanvas",
        "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6...",
        "jwt_iat": 1777613699,
        "jwt_exp": 1809149699,
        "verification_code": "OBC-J96V-P5ZJ",
        "metadata": { ... arbitrary skill-specific extras ... }
    }
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def sidecar_path(name: str, drive_root: Optional[pathlib.Path] = None) -> pathlib.Path:
    """Resolve the sidecar path for a named service.

    Lookup order:
      1. Per-service env var ``<UPPER_NAME>_CREDENTIALS_PATH`` (allows
         deployments to vault-mount credentials elsewhere).
      2. ``<drive_root>/<name>-credentials.json``.
      3. ``$OUROBOROS_DATA_DIR/<name>-credentials.json`` if drive_root is
         omitted.

    The path is returned even when the file doesn't exist; callers use
    ``load_sidecar`` for None-on-missing semantics.
    """
    safe = "".join(c for c in name.lower() if c.isalnum() or c in "-_")
    env_var = f"{safe.upper().replace('-', '_')}_CREDENTIALS_PATH"
    explicit = os.environ.get(env_var, "").strip()
    if explicit:
        return pathlib.Path(explicit)
    if drive_root is None:
        env_root = os.environ.get("OUROBOROS_DATA_DIR", "").strip()
        drive_root = pathlib.Path(env_root) if env_root else pathlib.Path.cwd()
    return pathlib.Path(drive_root) / f"{safe}-credentials.json"


def load_sidecar(name: str, drive_root: Optional[pathlib.Path] = None) -> Optional[Dict[str, Any]]:
    """Read a credentials sidecar. Returns ``None`` when the file is
    absent or malformed — never raises, so callers can treat absence
    as "feature disabled" without try/except boilerplate.

    The mtime is recorded in the returned dict under ``_sidecar_mtime``
    so consumers can detect rotation without rereading the JSON.
    """
    path = sidecar_path(name, drive_root)
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("credentials sidecar %s unreadable: %s", path, exc)
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("credentials sidecar %s malformed JSON: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("credentials sidecar %s root is not a JSON object", path)
        return None
    try:
        data["_sidecar_mtime"] = path.stat().st_mtime
    except OSError:
        pass
    data["_sidecar_path"] = str(path)
    return data


def credentials_present(name: str, drive_root: Optional[pathlib.Path] = None) -> bool:
    """Cheap check — does the sidecar exist and parse as a JSON object?"""
    return load_sidecar(name, drive_root) is not None


def fingerprint_secret(secret: str) -> str:
    """Stable short hex digest (first 16 hex chars of SHA256) of a secret.

    Used by the safety carve-out to pin a JWT/token without holding the
    secret in long-lived memory beyond the brief request window.
    Different secrets always produce different digests; same secret
    always produces the same digest. NOT a cryptographic guarantee
    against preimage attacks — purely a comparison aid.
    """
    h = hashlib.sha256()
    h.update(str(secret or "").encode("utf-8"))
    return h.hexdigest()[:16]


def redact_secret(value: str, *, keep: int = 6) -> str:
    """Return a redacted display string for a secret. Useful when echoing
    sidecar contents in logs or error messages."""
    if not value:
        return ""
    s = str(value)
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}…{s[-keep:]} (len={len(s)})"

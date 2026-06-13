"""Private forensic execution ledger.

The JSONL logs stay UI/API-friendly and compact. Full decision-affecting
payloads live here as local private gzip blobs plus small call manifests that
point to those blobs.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import pathlib
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

from ouroboros.utils import atomic_write_json, utc_now_iso


OBSERVABILITY_DIR = "observability"
SCHEMA_VERSION = 1
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIR_MODE = 0o700

_NON_SECRET_KEY_NAMES = frozenset({
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "token_estimate",
    "token_count",
    "total_tokens",
    "reasoning_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    "prompt_token_details",
    "completion_token_details",
    "input_tokens",
    "output_tokens",
})
_SECRET_KEY_EXACT = frozenset({
    "authorization",
    "auth_token",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
    "password",
    "passwd",
    "passphrase",
    "token",
    "secret",
    "apikey",
    "credential",
    "credentials",
    "private_key",
    "private_key_pem",
    "stripe_secret_key",
    "secret_key",
    "client_secret",
    "api_key",
})
_SECRET_KEY_SUFFIXES = (
    "_api_key",
    "_token",
    "_secret",
    "_password",
    "_passwd",
    "_passphrase",
    "_authorization",
    "_access_token",
    "_refresh_token",
    "_credential",
    "_credentials",
    "_private_key",
    "_private_key_pem",
    "_secret_key",
    "_secret_access_key",
    "_client_secret",
)
_TOKEN_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    ("bearer_token", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_\-./+=]{16,}")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("openrouter_key", re.compile(r"\bsk-or-[A-Za-z0-9\-]{20,}\b")),
    ("openai_project_key", re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}\b")),
    ("groq_key", re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b")),
    ("stripe_key", re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{20,}\b")),
    ("telegram_bot_token", re.compile(r"\b[0-9]{8,}:[A-Za-z0-9_\-]{20,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    (
        "url_credentials",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@"),
    ),
)
_SECRET_LITERAL_RE = re.compile(
    r"""(?im)(?P<prefix>(?:^|[\s,{])["']?[A-Za-z_][A-Za-z0-9_-]*(?:token|secret|password|passwd|passphrase|api[_-]?key|authorization|credential)[A-Za-z0-9_-]*["']?\s*[:=]\s*["']?)(?P<value>[^"'\s,}]{12,})(?P<suffix>["']?)"""
)
_SECRET_LITERAL_KEY_RE = re.compile(r"""["']?(?P<key>[A-Za-z_][A-Za-z0-9_-]*)["']?\s*[:=]\s*["']?$""")


def _normalize_key_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _is_secret_key_name(name: str) -> bool:
    normalized = _normalize_key_name(name)
    if not normalized or normalized in _NON_SECRET_KEY_NAMES:
        return False
    if normalized in _SECRET_KEY_EXACT or normalized.endswith(_SECRET_KEY_SUFFIXES):
        return True
    parts = set(normalized.split("_"))
    if "token" in parts or "password" in parts or "passwd" in parts or "passphrase" in parts:
        return True
    if "secret" in parts and parts & {"key", "token", "access", "credential", "credentials"}:
        return True
    if "private" in parts and "key" in parts:
        return True
    if "credential" in parts or "credentials" in parts:
        return True
    return False


@dataclass
class RedactionRecord:
    """One redaction fact for a projection, never the original secret."""

    path: str
    rule: str


@dataclass
class RedactionResult:
    """Redacted value plus a manifest of the redaction rules that fired."""

    value: Any
    records: List[RedactionRecord] = field(default_factory=list)

    def manifest(self) -> Dict[str, Any]:
        return {
            "redacted": bool(self.records),
            "count": len(self.records),
            "rules": [
                {"path": item.path, "rule": item.rule}
                for item in self.records
            ],
        }


def new_execution_id() -> str:
    return f"exec_{uuid.uuid4().hex}"


def new_call_id(prefix: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_]+", "_", str(prefix or "call")).strip("_").lower()
    safe = safe or "call"
    return f"{safe}_{uuid.uuid4().hex}"


def _observability_root(drive_root: pathlib.Path) -> pathlib.Path:
    base = pathlib.Path(drive_root)
    if not base.is_absolute():
        raise ValueError("observability drive_root must be an absolute path")
    root = base / OBSERVABILITY_DIR
    root.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(root)
    return root


def posix_private_modes_supported() -> bool:
    """Return true when chmod-style private modes are meaningful to assert."""

    return os.name == "posix"


def _chmod_private_dir(path: pathlib.Path) -> None:
    try:
        os.chmod(path, _PRIVATE_DIR_MODE)
    except OSError:
        pass


def _chmod_private(path: pathlib.Path) -> None:
    try:
        os.chmod(path, _PRIVATE_FILE_MODE)
    except OSError:
        pass


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")


def write_blob(drive_root: pathlib.Path, payload: Any, *, kind: str = "json") -> Dict[str, Any]:
    """Persist a full private payload as a content-addressed gzip blob."""

    raw = _json_bytes(payload) if kind == "json" else str(payload).encode("utf-8", errors="replace")
    digest = hashlib.sha256(raw).hexdigest()
    path = _observability_root(pathlib.Path(drive_root)) / "blobs" / f"{digest}.{kind}.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent)
    if not path.exists():
        tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            with gzip.open(tmp, "wb") as fh:
                fh.write(raw)
            _chmod_private(tmp)
            os.replace(tmp, path)
            _chmod_private(path)
        except Exception:
            if path.exists():
                # Concurrent reviewers can legitimately publish the same
                # content-addressed blob. On Windows the losing os.replace may
                # raise while the winning blob is already durable.
                try:
                    tmp.unlink()
                except OSError:
                    pass
                _chmod_private(path)
            else:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise
    else:
        _chmod_private(path)
    return {
        "sha256": digest,
        "path": str(path),
        "kind": kind,
        "encoding": "gzip",
        "size": len(raw),
        "compressed_size": path.stat().st_size if path.exists() else 0,
    }


def write_call_manifest(
    drive_root: pathlib.Path,
    *,
    task_id: str,
    call_id: str,
    manifest: Dict[str, Any],
) -> Dict[str, Any]:
    """Write the small per-call manifest with refs into the private ledger."""

    safe_task = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(task_id or "unknown")).strip("_") or "unknown"
    safe_call = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(call_id or new_call_id("call"))).strip("_")
    path = _observability_root(pathlib.Path(drive_root)) / "calls" / safe_task / f"{safe_call}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_private_dir(path.parent.parent)
    _chmod_private_dir(path.parent)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now_iso(),
        "task_id": str(task_id or ""),
        "call_id": safe_call,
        **dict(manifest or {}),
    }
    atomic_write_json(path, payload, trailing_newline=True)
    _chmod_private(path)
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        digest = hashlib.sha256(_json_bytes(payload)).hexdigest()
    return {
        "path": str(path),
        "call_id": safe_call,
        "sha256": digest,
    }


def _redact_text(text: str, records: List[RedactionRecord], path: str) -> str:
    out = text
    for rule, pattern in _TOKEN_PATTERNS:
        if rule == "url_credentials":
            def _url_repl(match: re.Match[str]) -> str:
                records.append(RedactionRecord(path=path, rule=rule))
                return f"{match.group(1)}***REDACTED***:***REDACTED***@"

            out = pattern.sub(_url_repl, out)
            continue
        def _repl(match: re.Match[str], _rule: str = rule) -> str:
            records.append(RedactionRecord(path=path, rule=_rule))
            return "***REDACTED***"

        out = pattern.sub(_repl, out)
    def _literal_repl(match: re.Match[str]) -> str:
        prefix = match.group("prefix")
        key_match = _SECRET_LITERAL_KEY_RE.search(prefix)
        if key_match and not _is_secret_key_name(key_match.group("key")):
            return match.group(0)
        records.append(RedactionRecord(path=path, rule="secret_literal_assignment"))
        return f"{prefix}***REDACTED***{match.group('suffix')}"

    out = _SECRET_LITERAL_RE.sub(_literal_repl, out)
    return out


def _redact_any(value: Any, records: List[RedactionRecord], path: str) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            item_path = f"{path}.{key_text}" if path else key_text
            if _is_secret_key_name(key_text):
                if item not in (None, "", False):
                    records.append(RedactionRecord(path=item_path, rule="secret_key_name"))
                out[key_text] = "***REDACTED***" if item not in (None, "", False) else item
            else:
                out[key_text] = _redact_any(item, records, item_path)
        return out
    if isinstance(value, list):
        return [_redact_any(item, records, f"{path}[{idx}]") for idx, item in enumerate(value)]
    if isinstance(value, tuple):
        return [_redact_any(item, records, f"{path}[{idx}]") for idx, item in enumerate(value)]
    if isinstance(value, str):
        return _redact_text(value, records, path)
    return value


def redact_projection(value: Any) -> RedactionResult:
    records: List[RedactionRecord] = []
    return RedactionResult(_redact_any(value, records, "$"), records)


def persist_call(
    drive_root: pathlib.Path,
    *,
    task_id: str,
    call_id: str,
    call_type: str,
    payload: Dict[str, Any],
    manifest: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Persist a full payload and return refs plus a redacted projection."""

    full_ref = write_blob(drive_root, payload, kind="json")
    redacted = redact_projection(payload)
    projection_ref = write_blob(drive_root, redacted.value, kind="json")
    manifest_ref = write_call_manifest(
        drive_root,
        task_id=task_id,
        call_id=call_id,
        manifest={
            "call_type": call_type,
            "full_payload_ref": full_ref,
            "redacted_projection_ref": projection_ref,
            "redaction": redacted.manifest(),
            **dict(manifest or {}),
        },
    )
    return {
        "call_id": call_id,
        "redacted_projection_ref": projection_ref,
        "manifest_ref": manifest_ref,
        "redaction": redacted.manifest(),
    }


def prune_observability_blobs(
    drive_root: pathlib.Path,
    retention_days: int | None = None,
    *,
    now: float | None = None,
) -> Dict[str, Any]:
    """Best-effort observability retention audit.

    Forensic call manifests and CAS blobs are durable replay evidence. This
    function intentionally does not delete them; it returns counts for startup
    housekeeping telemetry while preserving the "keep compressed" contract.
    """

    enabled = retention_days is not None
    if retention_days is None:
        raw = os.environ.get("OUROBOROS_OBSERVABILITY_RETENTION_DAYS", "").strip()
        if not raw:
            return {
                "enabled": False,
                "preserved_indefinitely": True,
                "manifest_count": 0,
                "blob_count": 0,
                "deleted_manifests": 0,
                "deleted_blobs": 0,
                "errors": [],
            }
        try:
            retention_days = int(raw)
            enabled = True
        except ValueError:
            return {
                "enabled": False,
                "preserved_indefinitely": True,
                "manifest_count": 0,
                "blob_count": 0,
                "deleted_manifests": 0,
                "deleted_blobs": 0,
                "errors": [f"invalid retention days: {raw!r}"],
            }
    retention_days = max(1, min(int(retention_days), 365))
    root = pathlib.Path(drive_root) / OBSERVABILITY_DIR
    calls_root = root / "calls"
    blobs_root = root / "blobs"
    report = {
        "enabled": enabled,
        "preserved_indefinitely": True,
        "retention_days": retention_days,
        "manifest_count": 0,
        "blob_count": 0,
        "deleted_manifests": 0,
        "deleted_blobs": 0,
        "errors": [],
    }
    if not root.exists():
        return report

    for manifest_path in list(calls_root.glob("*/*.json")) if calls_root.exists() else []:
        try:
            manifest_path.stat()
            report["manifest_count"] += 1
        except Exception as exc:
            report["errors"].append(f"{manifest_path}: {type(exc).__name__}: {exc}")

    if blobs_root.exists():
        for blob_path in list(blobs_root.glob("*.gz")):
            try:
                blob_path.stat()
                report["blob_count"] += 1
            except Exception as exc:
                report["errors"].append(f"{blob_path}: {type(exc).__name__}: {exc}")

    return report

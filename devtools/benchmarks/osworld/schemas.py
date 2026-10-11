"""Validation helpers for OSWorld logs-only bundles."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


REQUIRED_BUNDLE_FILES = (
    "SUMMARY.json",
    "sample_manifest.json",
)


def load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def validate_bundle(root: Path) -> dict[str, Any]:
    base = Path(root).expanduser().resolve(strict=False)
    missing = [name for name in REQUIRED_BUNDLE_FILES if not (base / name).is_file()]
    if missing:
        raise ValueError(f"OSWorld logs-only bundle is missing: {', '.join(missing)}")
    summary = load_json(base / "SUMMARY.json")
    sample_manifest = load_json(base / "sample_manifest.json")
    root_trace_manifest = base / "trace_manifest.json"
    if root_trace_manifest.is_file():
        trace_manifest: dict[str, Any] = load_json(root_trace_manifest)
    else:
        nested = sorted(path.relative_to(base).as_posix() for path in base.glob("**/traces/trace_manifest.json"))
        if not nested:
            raise ValueError("OSWorld logs-only bundle has no trace_manifest.json files")
        trace_manifest = {"trace_manifest_paths": nested}
    return {
        "root": str(base),
        "summary": summary,
        "sample_manifest": sample_manifest,
        "trace_manifest": trace_manifest,
    }


def read_traj_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        if isinstance(item, dict):
            rows.append(item)
    return rows

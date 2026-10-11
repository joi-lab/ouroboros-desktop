#!/usr/bin/env python3
"""Normalize an OSWorld logs-only bundle into an inspectable index."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.run_roots import ensure_file_output_outside_repo, repo_root_from_devtools
from devtools.benchmarks.osworld.schemas import read_traj_jsonl, validate_bundle


def normalize_bundle(root: Path) -> dict[str, Any]:
    bundle = validate_bundle(root)
    base = Path(bundle["root"])
    traj_paths = sorted(base.glob("**/traj.jsonl"))
    traces = []
    for path in traj_paths:
        rows = read_traj_jsonl(path)
        traces.append(
            {
                "path": str(path.relative_to(base)),
                "events": len(rows),
                "first_type": str(rows[0].get("type") or rows[0].get("event") or "") if rows else "",
                "last_type": str(rows[-1].get("type") or rows[-1].get("event") or "") if rows else "",
            }
        )
    return {
        "bundle_root": str(base),
        "summary": bundle["summary"],
        "sample_manifest": bundle["sample_manifest"],
        "trace_manifest": bundle["trace_manifest"],
        "traj_count": len(traces),
        "traces": traces,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_root")
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    normalized = normalize_bundle(Path(args.bundle_root))
    text = json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        out = ensure_file_output_outside_repo(Path(args.output), repo_root_from_devtools())
        out.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Minimal SWE-bench prediction helper backed by ``ouroboros run``.

Input is a JSONL file whose rows include ``instance_id``, ``workspace_root``,
and an instruction field (``problem_statement`` or ``prompt``). Output is a
SWE-bench-compatible predictions JSONL.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL instances")
    parser.add_argument("--output", required=True, help="predictions JSONL")
    parser.add_argument("--model-name", default="ouroboros-cli")
    parser.add_argument(
        "--workspaces-root",
        default="",
        help="optional directory containing per-instance or repo-name local checkouts",
    )
    args = parser.parse_args()

    rows = []
    for raw in Path(args.input).read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item: Any = json.loads(raw)
        if not isinstance(item, dict):
            continue
        instance_id = str(item.get("instance_id") or "")
        workspace = str(item.get("workspace_root") or "").strip()
        if not workspace and args.workspaces_root:
            root = Path(args.workspaces_root).expanduser()
            repo = str(item.get("repo") or "").strip()
            candidates = [root / instance_id]
            if repo:
                candidates.extend([root / repo.replace("/", "__"), root / repo.split("/")[-1]])
            for candidate in candidates:
                if candidate.is_dir():
                    workspace = str(candidate)
                    break
        prompt = str(item.get("problem_statement") or item.get("prompt") or "")
        if not instance_id or not workspace or not prompt:
            raise ValueError("each row must include instance_id, workspace_root or --workspaces-root, and problem_statement/prompt")
        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        if not workspace_path.is_dir():
            raise ValueError(f"workspace_root is not a directory for {instance_id}: {workspace}")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace_path, capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            raise ValueError(f"workspace_root is not a git checkout for {instance_id}: {workspace_path}")
        base_commit = str(item.get("base_commit") or "").strip()
        if base_commit and head.stdout.strip() != base_commit:
            raise ValueError(f"workspace HEAD for {instance_id} is {head.stdout.strip()}, expected base_commit {base_commit}")
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode != 0 or status.stdout.strip():
            raise ValueError(f"workspace must be clean before SWE-bench run for {instance_id}")
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ouroboros.cli",
                "run",
                "--workspace",
                str(workspace_path),
                "--memory-mode",
                "empty",
                "--patch",
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=7200,
        )
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            if len(details) > 4000:
                details = details[:4000] + "\n...[truncated]"
            raise RuntimeError(details or f"ouroboros run exited {result.returncode}")
        rows.append({
            "instance_id": instance_id,
            "model_name_or_path": args.model_name,
            "model_patch": result.stdout,
        })
    Path(args.output).write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

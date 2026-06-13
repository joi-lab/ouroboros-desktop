#!/usr/bin/env python3
"""Minimal SWE-bench prediction helper backed by ``ouroboros run``.

Input is a JSONL file whose rows include ``instance_id``, ``workspace_root``,
and an instruction field (``problem_statement`` or ``prompt``). Output is a
SWE-bench-compatible predictions JSONL.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL instances")
    parser.add_argument("--output", required=True, help="predictions JSONL")
    parser.add_argument("--model-name", default="ouroboros-cli")
    parser.add_argument("--cli", default="", help="optional Ouroboros CLI command prefix, e.g. 'ouroboros'")
    parser.add_argument("--timeout", type=int, default=7200, help="per-instance Ouroboros CLI timeout seconds")
    parser.add_argument("--continue-on-error", action="store_true", help="continue after failed instances and write errors JSONL")
    parser.add_argument("--errors-output", default="", help="errors JSONL path; defaults to <output>.errors.jsonl when continuing")
    parser.add_argument("--logs-dir", default="", help="optional directory for per-instance stdout/stderr logs")
    parser.add_argument(
        "--workspaces-root",
        default="",
        help="optional directory containing per-instance or repo-name local checkouts",
    )
    args = parser.parse_args()

    rows = []
    errors = []
    def record_error(row: dict) -> bool:
        if not args.continue_on_error:
            raise RuntimeError(str(row.get("error") or row))
        errors.append(row)
        return True

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
            record_error({
                "instance_id": instance_id,
                "error": "each row must include instance_id, workspace_root or --workspaces-root, and problem_statement/prompt",
                "reason_code": "invalid_instance",
            })
            continue
        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        if not workspace_path.is_dir():
            record_error({
                "instance_id": instance_id,
                "error": f"workspace_root is not a directory for {instance_id}: {workspace}",
                "reason_code": "invalid_workspace",
            })
            continue
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace_path, capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            record_error({
                "instance_id": instance_id,
                "error": f"workspace_root is not a git checkout for {instance_id}: {workspace_path}",
                "reason_code": "not_git_checkout",
            })
            continue
        base_commit = str(item.get("base_commit") or "").strip()
        if base_commit and head.stdout.strip() != base_commit:
            record_error({
                "instance_id": instance_id,
                "error": f"workspace HEAD for {instance_id} is {head.stdout.strip()}, expected base_commit {base_commit}",
                "reason_code": "wrong_base_commit",
            })
            continue
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if status.returncode != 0 or status.stdout.strip():
            record_error({
                "instance_id": instance_id,
                "error": f"workspace must be clean before SWE-bench run for {instance_id}",
                "reason_code": "dirty_workspace",
            })
            continue
        cli_prefix = shlex.split(args.cli) if args.cli else [sys.executable, "-m", "ouroboros.cli"]
        if args.logs_dir:
            result_json_path = Path(args.logs_dir).expanduser() / instance_id / "task_result.json"
        else:
            result_json_path = Path(tempfile.gettempdir()) / f"ouroboros_swebench_{instance_id}.task_result.json"
        result_json_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            *cli_prefix,
            "run",
            "--workspace",
            str(workspace_path),
            "--memory-mode",
            "empty",
            "--timeout",
            str(int(args.timeout)),
            "--patch",
            "--result-json-out",
            str(result_json_path),
            prompt,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=int(args.timeout) + 60,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            if args.logs_dir:
                log_dir = Path(args.logs_dir).expanduser() / instance_id
                log_dir.mkdir(parents=True, exist_ok=True)
                (log_dir / "ouroboros.stdout").write_text(stdout, encoding="utf-8")
                (log_dir / "ouroboros.stderr").write_text(stderr, encoding="utf-8")
            error_row = {
                "instance_id": instance_id,
                "returncode": 124,
                "error": f"ouroboros run timed out after {int(args.timeout)}s",
                "timeout": True,
            }
            if not args.continue_on_error:
                raise RuntimeError(error_row["error"]) from exc
            errors.append(error_row)
            continue
        task_result: dict[str, Any] = {}
        if result_json_path.exists():
            try:
                loaded = json.loads(result_json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    task_result = loaded
            except Exception:
                task_result = {}
        if args.logs_dir:
            log_dir = Path(args.logs_dir).expanduser() / instance_id
            log_dir.mkdir(parents=True, exist_ok=True)
            (log_dir / "ouroboros.stdout").write_text(result.stdout, encoding="utf-8")
            (log_dir / "ouroboros.stderr").write_text(result.stderr, encoding="utf-8")
            (log_dir / "ouroboros-agent-result.json").write_text(json.dumps({
                "instance_id": instance_id,
                "returncode": result.returncode,
                "stdout_chars": len(result.stdout or ""),
                "stderr_chars": len(result.stderr or ""),
                "patch_empty": not bool((result.stdout or "").strip()),
                "timeout_sec": int(args.timeout),
                "result_status": task_result.get("result_status"),
                "reason_code": task_result.get("reason_code"),
                "artifact_bundle": task_result.get("artifact_bundle"),
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            if len(details) > 4000:
                details = details[:4000] + "\n...[truncated]"
            error_row = {
                "instance_id": instance_id,
                "returncode": result.returncode,
                "error": details or f"ouroboros run exited {result.returncode}",
                "result_status": task_result.get("result_status"),
                "reason_code": task_result.get("reason_code"),
                "artifact_bundle": task_result.get("artifact_bundle"),
                "trace_refs": task_result.get("trace_refs"),
            }
            if not args.continue_on_error:
                raise RuntimeError(error_row["error"])
            errors.append(error_row)
            continue
        if not (result.stdout or "").strip():
            error_row = {
                "instance_id": instance_id,
                "returncode": 0,
                "error": "ouroboros run produced no patch",
                "result_status": task_result.get("result_status"),
                "reason_code": task_result.get("reason_code") or "no_patch",
                "artifact_bundle": task_result.get("artifact_bundle"),
                "trace_refs": task_result.get("trace_refs"),
            }
            if not args.continue_on_error:
                raise RuntimeError(error_row["error"])
            errors.append(error_row)
            continue
        rows.append({
            "instance_id": instance_id,
            "model_name_or_path": args.model_name,
            "model_patch": result.stdout,
        })
    Path(args.output).write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
    if errors:
        error_path = Path(args.errors_output).expanduser() if args.errors_output else Path(str(args.output) + ".errors.jsonl")
        error_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in errors) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

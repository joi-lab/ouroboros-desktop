#!/usr/bin/env python3
"""Capture SWE-bench Pro prediction patches from prepared task repositories."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import admit_benchmark_run, finalize_run_manifest
from devtools.benchmarks.common.result_index import task_result_row, write_result_index
from devtools.benchmarks.common.run_roots import (
    default_settings_path,
    assert_file_output_outside_repo,
    assert_outside_repo,
    run_root,
    safe_benchmark_id,
)


CAPTURE = Path(__file__).resolve().parent / "capture_patch.sh"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _capture_patch(repo_dir: Path, base_commit: str, out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["bash", str(CAPTURE), str(repo_dir), base_commit, str(out_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"capture_patch.sh failed for {repo_dir}: {proc.stderr or proc.stdout}")
    patch = out_path.read_text(encoding="utf-8", errors="replace")
    if not patch.strip():
        raise RuntimeError(f"capture_patch.sh produced an empty patch for {repo_dir}")
    return patch


def _collect_attestations(paths: list[str]) -> dict[str, Any]:
    """Aggregate per-task runtime attestations for the scoring-time provenance backstop.

    Each file is a record written by the run that produced the patches (the E1v2
    entrypoint's ``seed_attestation.json``, or any launcher's manifest ``extra``). The
    LINE OF DESCENT is verified where the commits physically exist — inside the evolving
    volume — because an evolved commit is unknown to the host seed checkout and an equality
    or host-side ancestry test would flag legitimate evolution as corruption (owner Q11).
    Here we aggregate and cross-check what IS checkable off-box: one seed stamp for the whole
    run, every record's own verdict, and any use of the evolved-volume override.
    """
    records: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw).expanduser()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - a missing/broken record is recorded, never guessed
            records.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
            continue
        records.append({"path": str(path), **loaded} if isinstance(loaded, dict) else {"path": str(path)})
    seeds = sorted({str(item.get("seed") or item.get("seed_stamp") or "") for item in records} - {""})
    return {
        "records": records,
        "count": len(records),
        "seed_stamps": seeds,
        "single_seed": len(seeds) <= 1,
        "lineage_broken": [str(item.get("path")) for item in records if item.get("reason")],
        "overridden": [str(item.get("path")) for item in records if item.get("overridden")],
    }


def _append_not_attempted_rows(ledger_rows: list[dict[str, Any]], input_rows: list[dict[str, Any]]) -> None:
    for item in input_rows[len(ledger_rows):]:
        ledger_rows.append(
            task_result_row(
                benchmark="swe_bench_pro",
                instance_id=str(item.get("instance_id") or ""),
                status="not_attempted",
                reason_code="aborted_after_prior_error",
                official_eval_status="not_run",
                error="not attempted because fail-fast stopped after an earlier error",
            )
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL rows with instance_id, repo_dir/workspace_root, base_commit")
    parser.add_argument("--output", required=True, help="prediction JSONL")
    parser.add_argument("--patch-dir", default="", help="optional directory for captured .diff files")
    parser.add_argument("--model-name", default="ouroboros-pro")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--errors-output", default="")
    parser.add_argument("--ledger-output", default="", help="denominator-preserving JSONL result ledger")
    parser.add_argument("--manifest-output", default="", help="run manifest JSON")
    parser.add_argument("--settings-path", default="")
    parser.add_argument("--isolated-data-root", default="", help="isolated Ouroboros data root used for this run")
    parser.add_argument(
        "--allow-dirty-seed",
        action="store_true",
        help="record and proceed with an unclean/unidentifiable seed checkout instead of refusing "
             "(the refusal is the default: a dirty seed makes the run unreproducible)",
    )
    parser.add_argument(
        "--attestation",
        action="append",
        default=[],
        help="runtime attestation JSON produced by the run that generated these patches; repeatable",
    )
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    settings_path = Path(args.settings_path).expanduser() if args.settings_path else default_settings_path()
    output = assert_file_output_outside_repo(Path(args.output), REPO_ROOT)
    errors_output = (
        assert_file_output_outside_repo(Path(args.errors_output), REPO_ROOT)
        if args.errors_output
        else Path(str(output) + ".errors.jsonl")
    )
    ledger_output = (
        assert_file_output_outside_repo(Path(args.ledger_output), REPO_ROOT)
        if args.ledger_output
        else Path(str(output) + ".ledger.jsonl")
    )
    manifest_output = (
        assert_file_output_outside_repo(Path(args.manifest_output), REPO_ROOT)
        if args.manifest_output
        else Path(str(output) + ".run_manifest.json")
    )
    patch_dir = Path(args.patch_dir).expanduser() if args.patch_dir else run_root("swe_bench_pro") / "patches"
    assert_outside_repo(patch_dir, REPO_ROOT)  # `_capture_patch` creates it when it writes
    predictions: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    ledger_rows: list[dict[str, Any]] = []
    # Build the manifest ONCE, BEFORE any capture work: it carries the seed-provenance gate,
    # so an unreproducible seed stops the run here instead of after the artifacts exist.
    # `admit_benchmark_run` persists the complete gate/refusal payload BEFORE enforcement raises,
    # and the retained dict is augmented below and finalized with a typed outcome on every exit.
    manifest = admit_benchmark_run(
        manifest_output,
        benchmark="swe_bench_pro",
        run_root=output.parent,
        repo_dir=REPO_ROOT,
        # DECLARED input only (see `extra.input`): `_rows` reads and parses `--input`, so the
        # ids are not knowable until the run is admitted. Amended below with what it held.
        requested_task_ids=[],
        require_clean=not args.allow_dirty_seed,
        output_paths={
            "predictions": str(output),
            "errors": str(errors_output),
            "ledger": str(ledger_output),
            "patch_dir": str(patch_dir),
        },
        dataset="ScaleAI/SWE-bench_Pro",
        timeout_sec=120,
        isolated_data_root=str(args.isolated_data_root or ""),
        settings_path=settings_path,
        extra={
            "model_name_or_path": args.model_name,
            "input": str(input_path),
            # DECLARED paths only. `_collect_attestations` READS each of them, and evaluating it
            # here meant it ran inside the admission call's own argument list — Python evaluates
            # arguments before entering the callee, so it executed while nothing was on disk yet.
            # Same evaluation-order trap `runtime_attestation` was moved out of; the aggregate is
            # attached to the retained manifest inside the admitted run below.
            "runtime_attestation_paths": [str(path) for path in (args.attestation or [])],
        },
    )

    with finalize_run_manifest(manifest_output, manifest, outcome="completed") as final:
        input_rows = _rows(input_path)
        manifest["requested_task_ids"] = [str(item.get("instance_id") or "") for item in input_rows]
        manifest["requested_count"] = len(input_rows)
        manifest["extra"]["runtime_attestations"] = _collect_attestations(list(args.attestation or []))
        first_error: Exception | None = None
        for item in input_rows:
            instance_id = str(item.get("instance_id") or "").strip()
            patch_path = ""
            try:
                safe_instance_id = safe_benchmark_id(instance_id)
                repo_dir = Path(str(item.get("repo_dir") or item.get("workspace_root") or "")).expanduser()
                base_commit = str(item.get("base_commit") or "").strip()
                if not instance_id or not repo_dir.is_dir() or not base_commit:
                    raise RuntimeError("each row must include instance_id, repo_dir/workspace_root, and base_commit")
                patch_out = patch_dir / f"{safe_instance_id}.diff"
                patch_path = str(patch_out)
                patch = _capture_patch(repo_dir, base_commit, patch_out)
            except Exception as exc:
                reason = "capture_failed"
                if isinstance(exc, ValueError):
                    reason = "invalid_instance_id"
                elif "empty patch" in str(exc):
                    reason = "empty_patch"
                elif "repo_dir" in str(exc) or "base_commit" in str(exc):
                    reason = "invalid_instance"
                errors.append({"instance_id": instance_id, "error": str(exc), "reason_code": reason})
                ledger_rows.append(
                    task_result_row(
                        benchmark="swe_bench_pro",
                        instance_id=instance_id,
                        status="empty_patch" if reason == "empty_patch" else "failed",
                        reason_code=reason,
                        output_paths={"patch": patch_path} if patch_path else {},
                        error=str(exc),
                    )
                )
                if not args.continue_on_error:
                    first_error = exc
                    break
                continue
            predictions.append(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": args.model_name,
                    "model_patch": patch,
                }
            )
            ledger_rows.append(
                task_result_row(
                    benchmark="swe_bench_pro",
                    instance_id=instance_id,
                    status="completed",
                    reason_code="patch_generated",
                    prediction_written=True,
                    official_eval_status="pending",
                    output_paths={"prediction_jsonl": str(output), "patch": patch_path},
                    details={"patch_bytes": len(patch.encode("utf-8", errors="replace"))},
                )
            )
        if first_error is not None and not args.continue_on_error:
            _append_not_attempted_rows(ledger_rows, input_rows)
        output.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in predictions) + ("\n" if predictions else ""),
            encoding="utf-8",
        )
        if errors:
            errors_output.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in errors) + "\n",
                encoding="utf-8",
            )
        write_result_index(ledger_output, ledger_rows)
        manifest["extra"].update({"prediction_count": len(predictions), "error_count": len(errors)})
        if first_error is not None:
            # A named terminal outcome BEFORE the re-raise (the shared seam adds the typed
            # exception), so the run's own record never stays at `started`.
            final.update({"outcome": "stopped_instance_error", "exit_code": 1})
            raise first_error
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

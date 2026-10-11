#!/usr/bin/env python3
"""Run official SWE-bench Pro eval, then print a diagnostic summary.

This wrapper does not replace Pro scoring. The official ``swe_bench_pro_eval.py``
output remains the source of truth. The post-run table only helps inspect
per-instance output files.

  python pro/grade_pro.py --predictions runs/pro_smoke/predictions.jsonl --workers 4
"""
from __future__ import annotations
import argparse, ast, csv, json, pathlib, subprocess, sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.run_roots import (
    ensure_outside_repo,
    latest_run_root,
    repo_root_from_devtools,
    run_root,
)

PRO = pathlib.Path(__file__).resolve().parent
CSV_DEFAULT = PRO / "task_order_pro_70.csv"


def _default_run_root() -> pathlib.Path:
    """Resolve grading defaults to the most recent existing run, not a fresh
    empty timestamped dir. Computing ``run_root(...)`` at import time made every
    default point at a brand-new dir that never held the predictions to grade.
    """
    return latest_run_root("swe_bench_pro") or run_root("swe_bench_pro")


def load_predictions(path: pathlib.Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def colleague_verdicts(csv_path: pathlib.Path) -> dict:
    with csv_path.open() as f:
        return {r["instance_id"]: r["verdict"] for r in csv.DictReader(f)}


def raw_sample_index(raw_sample: pathlib.Path) -> dict:
    """Return raw official sample rows by instance id for diagnostic inspection."""
    idx = {}
    for l in raw_sample.read_text(errors="replace").splitlines():
        if not l.strip():
            continue
        try:
            d = json.loads(l)
        except Exception:
            continue
        idx[d.get("instance_id", "")] = d
    return idx


def as_set(v) -> set:
    if isinstance(v, str):
        try:
            return set(json.loads(v))
        except Exception:
            try:
                return set(ast.literal_eval(v))
            except Exception:
                return set()
    return set(v or [])


def instance_verdict(
    output_path: pathlib.Path,
    raw_row: dict | None,
) -> tuple[str, str, str]:
    """Classify one instance as ``pass`` / ``fail`` / ``ungraded`` plus a typed reason.

    Third state, not a third opinion: an instance the official evaluator never scored is NOT
    a model failure, and folding it into the FAIL bucket is what made earlier headlines
    unfalsifiable. The returned verdict feeds the reported denominator; it never re-scores
    anything (the official per-instance output stays the authority).
    Returns ``(verdict, reason, tests_column)``.
    """
    if raw_row is None:
        return "ungraded", "instance_not_in_dataset", "-"
    if not output_path.is_file():
        return "ungraded", "no_official_output", "-"
    try:
        out = json.loads(output_path.read_text())
        # The SHAPE of the parsed output is part of parsing: an unexpected-but-valid JSON body
        # (tests as a dict, rows without `name`) must land in the same `ungraded` bucket, never
        # produce a headline through a raised KeyError/TypeError.
        tests = out.get("tests") or []
        passed = {t["name"] for t in tests if t.get("status") == "PASSED"}
    except Exception as exc:  # noqa: BLE001 - an unparseable output is ungraded, never a FAIL
        return "ungraded", f"output_unparseable:{type(exc).__name__}", "-"
    need = as_set(raw_row.get("FAIL_TO_PASS") or raw_row.get("fail_to_pass")) | as_set(
        raw_row.get("PASS_TO_PASS") or raw_row.get("pass_to_pass")
    )
    column = f"{len(passed)}/{len(need - passed)}/{len(need)}"
    if not need:
        return "ungraded", "no_required_tests", column
    return ("pass" if need <= passed else "fail"), "", column


def main() -> int:
    default_run_root = _default_run_root()
    ap = argparse.ArgumentParser()
    ap.add_argument("--predictions", default=str(default_run_root / "predictions.jsonl"))
    ap.add_argument("--out-dir", default=str(default_run_root / "pro_eval"))
    ap.add_argument("--eval-repo", default=str(default_run_root / "SWE-bench_Pro-os"), help="external checkout of scaleapi/SWE-bench_Pro-os")
    ap.add_argument("--prefix", default="ours")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--platform", default="linux/amd64")
    ap.add_argument("--skip-run", action="store_true", help="aggregate an existing out-dir without running official eval")
    ap.add_argument("--csv", default="", help="optional CSV with instance_id,verdict columns for baseline comparison")
    args = ap.parse_args()

    eval_repo = pathlib.Path(args.eval_repo).expanduser().resolve()
    raw_sample = eval_repo / "helper_code" / "sweap_eval_full_v2.jsonl"
    if not eval_repo.is_dir():
        print(f"error: eval harness clone is missing: {eval_repo}\n  git clone --depth 1 https://github.com/scaleapi/SWE-bench_Pro-os.git {eval_repo}", file=sys.stderr)
        return 2
    if not raw_sample.is_file():
        print(f"error: raw_sample is missing: {raw_sample}", file=sys.stderr); return 2

    preds = load_predictions(pathlib.Path(args.predictions).expanduser())
    preds = [p for p in preds if (p.get("model_patch") or "").strip()]
    out_dir = ensure_outside_repo(pathlib.Path(args.out_dir), repo_root_from_devtools())
    patches = [{"instance_id": p["instance_id"], "patch": p["model_patch"], "prefix": args.prefix} for p in preds]
    patches_path = out_dir / "patches.json"
    patches_path.write_text(json.dumps(patches), encoding="utf-8")
    print(f"[swe-pro] wrote {len(patches)} patches to {patches_path}", file=sys.stderr)

    if not args.skip_run:
        cmd = [sys.executable, "swe_bench_pro_eval.py",
               "--raw_sample_path", str(raw_sample),
               "--patch_path", str(patches_path),
               "--output_dir", str(out_dir),
               "--scripts_dir", "run_scripts",
               "--num_workers", str(args.workers),
               "--dockerhub_username", "jefzda",
               "--use_local_docker", "--docker_platform", args.platform]
        print("[swe-pro] running official eval:\n  " + " ".join(cmd), file=sys.stderr)
        r = subprocess.run(cmd, cwd=str(eval_repo))
        if r.returncode != 0:
            print(f"[swe-pro] official eval returned {r.returncode}; summarizing available output", file=sys.stderr)

    rs = raw_sample_index(raw_sample)
    csv_path = pathlib.Path(args.csv).expanduser() if args.csv else CSV_DEFAULT
    if csv_path.is_file():
        verd = colleague_verdicts(csv_path)
    else:
        verd = {}
        hint = " (pass --csv <path> to enable baseline comparison)" if args.csv else ""
        print(f"[swe-pro] note: baseline CSV not found at {csv_path}; baseline column blank{hint}", file=sys.stderr)
    print("\n[diagnostic] Non-leaderboard summary derived from official per-instance outputs.")
    print(f"{'instance':52} {'verdict':10} {'reason':26} {'baseline':10} {'tests P/missing/total'}")
    n_res = 0
    n_ungraded = 0
    verdict_rows: list[dict] = []
    for p in patches:
        iid = p["instance_id"]
        od = out_dir / iid / f"{args.prefix}_output.json"
        if not od.is_file():
            od2 = list((out_dir).glob(f"**/{args.prefix}_output.json"))
            od = next((x for x in od2 if iid in str(x)), od)
        verdict, reason, ntp = instance_verdict(od, rs.get(iid))
        n_res += int(verdict == "pass")
        n_ungraded += int(verdict == "ungraded")
        verdict_rows.append({"instance_id": iid, "verdict": verdict, "reason": reason,
                             "tests": ntp, "official_output": str(od)})
        print(f"{iid[:52]:52} {verdict:10} {reason[:26]:26} {verd.get(iid,'?'):10} {ntp}")
    # B1: the RAW, unadjusted headline is the SOLE reported metric, and its FORMULA IS
    # UNCHANGED (resolved over submitted instances). Any contamination / benchmark-defect
    # analysis (e.g. the `interface`-field false-negatives on some instances) lives ONLY in
    # the separate diagnostic CONTAMINATION_AUDIT.md and never adjusts this number — gold is
    # never shown to the solver and nothing is re-scored.
    total = len(patches)
    pct = (100.0 * n_res / total) if total else 0.0
    graded = total - n_ungraded
    print(f"\n[headline] RAW Pass@1: {n_res}/{total} = {pct:.1f}%  (official Pro eval output remains source of truth)")
    print(f"[headline] ungraded={n_ungraded}/{total} (instances the official evaluator never scored; "
          "they stay in the headline denominator above)")
    if n_ungraded:
        graded_pct = (100.0 * n_res / graded) if graded else 0.0
        print(f"[diagnostic] pass/(total-ungraded) = {n_res}/{graded} = {graded_pct:.1f}% "
              "— DIAGNOSTIC ONLY, NOT leaderboard-valid (it shrinks the denominator).")
    print("[headline] Contamination/benchmark-defect notes are diagnostic only — see "
          "devtools/benchmarks/swe_bench_pro/CONTAMINATION_AUDIT.md (the raw headline above is NOT adjusted by them).")
    (out_dir / "grade_summary.json").write_text(
        json.dumps(
            {
                "schema": "ouroboros.swe_bench_pro.grade_summary.v1",
                "submitted": total,
                "pass": n_res,
                "fail": total - n_res - n_ungraded,
                "ungraded": n_ungraded,
                "headline_raw_pass_at_1_pct": round(pct, 2),
                "diagnostic_pass_over_graded_pct": round((100.0 * n_res / graded), 2) if graded else 0.0,
                "diagnostic_not_leaderboard_valid": True,
                "verdicts": verdict_rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

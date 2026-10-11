#!/usr/bin/env python3
"""Build predictions_<N>_<M>.jsonl from consolidated E1v2 patch outputs.

The consolidated run root is $SWEBENCH_RUNS/pro_e1_full/ (default OBOCACHE/swebench-runs). Older DIRS-list build_predictions_*.py scripts are obsolete (folders merged).

  python3 pro/build_predictions.py --start 1 --end 60 [--out runs.jsonl]
  SWEBENCH_RUNS=/path python3 pro/build_predictions.py --start 1 --end 60
"""
import argparse, csv, json, os, pathlib, sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

from devtools.benchmarks.common.run_roots import ensure_file_output_outside_repo

ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
CSV = ROOT / "task_order_pro_70.csv"
RUNS_ROOT = pathlib.Path(os.environ.get("SWEBENCH_RUNS", "/Volumes/OBOCACHE/swebench-runs"))
FULL = RUNS_ROOT / "pro_e1_full"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=60)
    ap.add_argument("--out", default=None)
    ap.add_argument("--model-name", default="ouroboros-e1-pro-sonnet-4.5",
                    help="model_name_or_path written into each prediction row (leaderboard-shaped).")
    a = ap.parse_args()
    if not FULL.is_dir():
        print(f"[build] MISSING {FULL} - is OBOCACHE mounted? (mount | grep OBOCACHE)")
        return 2
    rows = sorted(csv.DictReader(CSV.open()), key=lambda r: int(r["idx"]))
    sel = [r for r in rows if a.start <= int(r["idx"]) <= a.end]
    # Both the explicit --out and the default $SWEBENCH_RUNS path must satisfy the
    # devtools outside-repo output contract (SWEBENCH_RUNS could point inside the repo).
    out = ensure_file_output_outside_repo(
        pathlib.Path(a.out) if a.out else FULL / f"predictions_{a.start}_{a.end}.jsonl",
        REPO_ROOT,
    )
    n_ok = 0
    with out.open("w", encoding="utf-8") as f:
        for r in sel:
            p = FULL / r["instance_id"] / "patch.diff"
            patch = p.read_text(errors="replace") if (p.is_file() and p.stat().st_size > 0) else ""
            if patch.strip(): n_ok += 1
            else: print(f"  empty: idx{r['idx']} {r['instance_id'][:46]}")
            f.write(json.dumps({"instance_id": r["instance_id"],
                                "model_name_or_path": a.model_name,
                                "model_patch": patch}) + "\n")
    print(f"[build] {len(sel)} predictions ({n_ok} non-empty) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

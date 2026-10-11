#!/usr/bin/env python3
"""Build E1v2-vs-E0 learning-curve data (optionally PNG if matplotlib exists)."""

from __future__ import annotations

import argparse
import csv
import json
import pathlib
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

from devtools.benchmarks.common.run_roots import ensure_file_output_outside_repo


ROOT = pathlib.Path(__file__).resolve().parent.parent
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
CSV_DEFAULT = ROOT / "task_order_pro_70.csv"


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "pass", "resolved", "ok"}


def load_e0(csv_path: pathlib.Path) -> list[dict[str, Any]]:
    rows = sorted(csv.DictReader(csv_path.open(encoding="utf-8")), key=lambda row: int(row["idx"]))
    return [{
        "idx": int(row["idx"]),
        "instance_id": row["instance_id"],
        "e0_resolved": _truthy(row.get("verdict")),
    } for row in rows]


def load_e1v2_results(path: pathlib.Path) -> dict[str, bool]:
    if not path.exists():
        return {}
    results: dict[str, bool] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        obj = json.loads(line)
        instance_id = str(obj.get("instance_id") or obj.get("task_id") or "")
        if not instance_id:
            continue
        results[instance_id] = _truthy(obj.get("resolved") if "resolved" in obj else obj.get("status"))
    return results


def curve_rows(e0_rows: list[dict[str, Any]], e1v2: dict[str, bool], window: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pos, row in enumerate(e0_rows, start=1):
        start = max(0, pos - window)
        span = e0_rows[start:pos]
        e0_rate = sum(1 for item in span if item["e0_resolved"]) / len(span)
        e1_span = [e1v2.get(item["instance_id"], False) for item in span]
        e1_rate = sum(1 for item in e1_span if item) / len(e1_span)
        out.append({
            "idx": row["idx"],
            "instance_id": row["instance_id"],
            "e0_window_rate": round(e0_rate, 4),
            "e1v2_window_rate": round(e1_rate, 4),
            "window": len(span),
        })
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-order", default=str(CSV_DEFAULT))
    parser.add_argument("--e1v2-results", required=True, help="JSONL with instance_id + resolved/status")
    parser.add_argument("--window", type=int, default=10)
    parser.add_argument("--out-json", default="")
    parser.add_argument("--out-png", default="")
    args = parser.parse_args(argv)

    rows = curve_rows(load_e0(pathlib.Path(args.task_order)), load_e1v2_results(pathlib.Path(args.e1v2_results)), window=max(1, args.window))
    if args.out_json:
        ensure_file_output_outside_repo(pathlib.Path(args.out_json), REPO_ROOT).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    if args.out_png:
        try:
            import matplotlib.pyplot as plt  # type: ignore

            xs = [r["idx"] for r in rows]
            plt.plot(xs, [r["e0_window_rate"] for r in rows], label="E0")
            plt.plot(xs, [r["e1v2_window_rate"] for r in rows], label="E1v2")
            plt.xlabel("Task index")
            plt.ylabel("Sliding resolved rate")
            plt.legend()
            plt.tight_layout()
            plt.savefig(ensure_file_output_outside_repo(pathlib.Path(args.out_png), REPO_ROOT))
        except Exception as exc:  # pragma: no cover - optional plotting dependency
            raise SystemExit(f"plot failed: {type(exc).__name__}: {exc}") from exc
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

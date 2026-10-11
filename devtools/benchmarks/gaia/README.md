# GAIA Adapter

Maintained adapter for running Ouroboros against GAIA through the official
`inspect_evals/gaia` task and scorer.

Generated runs go under `bench_runs/gaia/` (or `OUROBOROS_BENCH_RUNS_ROOT`), never
inside `repo/` or live runtime `data/`.

```bash
python devtools/benchmarks/gaia/run_gaia.py --dry-run
python devtools/benchmarks/gaia/score_gaia.py --run-dir bench_runs/gaia/<run>
```

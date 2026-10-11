# SWE-bench Devtools

These helpers generate official SWE-bench prediction JSONL files for a set of
already-prepared local task checkouts.

They do not download datasets, reset repositories, or score benchmark results.
Evaluation remains the official SWE-bench harness:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Verified \
  --predictions_path /path/to/predictions.jsonl \
  --max_workers 1 \
  --run_id ouroboros
```

Prediction rows produced by `swebench_predictions.py` contain only:

```json
{"instance_id": "...", "model_name_or_path": "...", "model_patch": "..."}
```

Supported preset aliases in `presets.py`:

- `full` -> `princeton-nlp/SWE-bench`
- `lite` -> `princeton-nlp/SWE-bench_Lite`
- `verified` -> `princeton-nlp/SWE-bench_Verified`

Input rows must provide `instance_id`, a clean git `workspace_root` or
`--workspaces-root`, and `problem_statement` or `prompt`. If `base_commit` is
present, the helper refuses to run unless the checkout HEAD matches it.

The helper also writes sidecar artifacts next to the predictions path unless
explicit paths are supplied:

- `<predictions>.ledger.jsonl` records every requested instance, including
  invalid input, dirty workspaces, timeouts, non-zero Ouroboros exits, and empty
  patches.
- `<predictions>.errors.jsonl` contains failure rows for operator debugging.
- `<predictions>.run_manifest.json` records provenance and official eval command
  shape.

The official predictions JSONL intentionally omits failed/empty-patch rows
because the SWE-bench harness expects only prediction records. The ledger is the
denominator-preserving Ouroboros audit artifact.

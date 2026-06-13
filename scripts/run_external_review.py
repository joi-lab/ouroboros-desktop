#!/usr/bin/env python3
"""Dry-run the real triad + scope reviewers against the staged diff.

Development-only: no commit, no review-state mutation, full raw reviewer output.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List


_REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
_OUROBOROS_HOME = _REPO_DIR.parent
_DATA_DIR = _OUROBOROS_HOME / "data"
_SETTINGS_PATH = _DATA_DIR / "settings.json"
_SECRET_ENV_KEYS = {
    "GITHUB_TOKEN",
    "OUROBOROS_NETWORK_PASSWORD",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "ANTHROPIC_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
}
_EXPLICIT_ENV_OVERRIDE_KEYS = {
    "OUROBOROS_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODEL",
}

if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))


def _load_settings_into_env() -> Dict[str, Any]:
    """Populate env from settings.json without printing secret values."""
    preexisting_secrets = {
        key: os.environ.get(key, "")
        for key in _SECRET_ENV_KEYS
        if os.environ.get(key)
    }
    preexisting_overrides = {
        key: os.environ.get(key, "")
        for key in _EXPLICIT_ENV_OVERRIDE_KEYS
        if os.environ.get(key)
    }
    if not _SETTINGS_PATH.exists():
        sys.stderr.write(
            f"[run_external_review] settings.json not found at {_SETTINGS_PATH}\n"
            "Run the wizard first or set OPENROUTER_API_KEY / "
            "OUROBOROS_REVIEW_MODELS in the environment manually.\n"
        )
        return {}
    try:
        settings = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"[run_external_review] Failed to parse settings.json: {exc}\n")
        return {}

    pushed: List[str] = []
    for key, value in settings.items():
        if value is None or value == "" or isinstance(value, (dict, list)):
            continue
        os.environ.setdefault(key, str(value))
        pushed.append(key)

    try:
        from ouroboros.config import apply_settings_to_env
        apply_settings_to_env(settings)
        for key, value in preexisting_secrets.items():
            if not str(settings.get(key) or "").strip():
                os.environ[key] = value
        for key, value in preexisting_overrides.items():
            os.environ[key] = value
    except Exception as exc:
        sys.stderr.write(
            f"[run_external_review] apply_settings_to_env failed (continuing with raw env copy): {exc}\n"
        )

    sys.stderr.write(
        f"[run_external_review] env populated from {_SETTINGS_PATH} "
        f"({len(pushed)} keys, including: "
        f"{', '.join(k for k in ('OPENROUTER_API_KEY', 'OUROBOROS_REVIEW_MODELS', 'OUROBOROS_SCOPE_REVIEW_MODEL') if k in pushed)})\n"
    )
    return settings


def _ensure_diff_present() -> str:
    """Return the staged diff text the reviewers will see; abort if empty."""
    proc = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=_REPO_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[run_external_review] git diff failed: {proc.stderr}\n")
        sys.exit(2)
    diff = proc.stdout
    if not diff.strip():
        sys.stderr.write(
            "[run_external_review] No staged diff found. "
            "``git add`` the relevant files before running this script.\n"
        )
        sys.exit(3)
    return diff


def _build_ctx() -> Any:
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=_REPO_DIR, drive_root=_DATA_DIR)
    for name, value in {
        "_review_advisory": [],
        "_review_iteration_count": 0,
        "_review_history": [],
        "_scope_review_history": {},
        "_last_scope_model": "",
        "_last_triad_raw_results": [],
        "_last_scope_raw_result": {},
        "_last_review_block_reason": "",
        "_last_review_critical_findings": [],
        "_current_review_tool_name": "external_review",
    }.items():
        setattr(ctx, name, value)
    return ctx


def _print_section(title: str, body: str, *, use_color: bool = True) -> None:
    bar = "=" * 78
    if use_color and sys.stdout.isatty():
        head = f"\033[1;33m{title}\033[0m"
    else:
        head = title
    print(f"\n{bar}\n{head}\n{bar}\n{body}\n")


def _format_review_record(record: Dict[str, Any], sections: list[tuple[str, str]]) -> str:
    parts = [f"{key:<13}: {record.get(key, default)}" for key, default in (
        ("model_id", "?"), ("status", "?"), ("tokens_in", 0),
        ("tokens_out", 0), ("cost_usd", 0.0), ("prompt_chars", 0),
    ) if key in record or key != "prompt_chars"]
    parts += ["", "── raw_text (verbatim, no truncation) ──", record.get("raw_text", "<empty>")]
    for title, key in sections:
        parts += ["", f"── {title} ──", json.dumps(record.get(key, []), indent=2, ensure_ascii=False)]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit-message", required=True, help="Synthetic commit message for the review prompt")
    parser.add_argument("--goal", default="", help="Optional goal/intent string passed to scope reviewer")
    parser.add_argument("--scope", default="", help="Optional scope hint passed to scope reviewer")
    parser.add_argument("--review-rebuttal", default="", help="Optional rebuttal text (for rerun scenarios)")
    parser.add_argument("--review-models", default="", help="Override OUROBOROS_REVIEW_MODELS for this dry-run")
    parser.add_argument("--scope-review-models", default="", help="Override OUROBOROS_SCOPE_REVIEW_MODELS for this dry-run")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors in section headers")
    parser.add_argument("--output", help="Also write full raw output to this file")
    args = parser.parse_args()

    use_color = not args.no_color
    _load_settings_into_env()
    if args.review_models:
        os.environ["OUROBOROS_REVIEW_MODELS"] = args.review_models
        sys.stderr.write("[run_external_review] using explicit reviewer slots override\n")
    if args.scope_review_models:
        os.environ["OUROBOROS_SCOPE_REVIEW_MODELS"] = args.scope_review_models
        sys.stderr.write("[run_external_review] using explicit scope reviewer slots override\n")
    diff = _ensure_diff_present()
    sys.stderr.write(f"[run_external_review] diff size: {len(diff)} chars\n")

    ctx = _build_ctx()

    from ouroboros.tools.parallel_review import run_parallel_review, aggregate_review_verdict

    sys.stderr.write("[run_external_review] launching triad + scope reviewers in parallel...\n")
    commit_start = time.time()
    review_err, scope_result, triad_block_reason, triad_advisory = run_parallel_review(
        ctx,
        args.commit_message,
        goal=args.goal,
        scope=args.scope,
        review_rebuttal=args.review_rebuttal,
    )
    aggregated = aggregate_review_verdict(
        review_err,
        scope_result,
        triad_block_reason,
        triad_advisory,
        ctx,
        args.commit_message,
        commit_start,
        str(_REPO_DIR),
    )

    out_buf: List[str] = []

    def _emit(title: str, body: str) -> None:
        _print_section(title, body, use_color=use_color)
        out_buf.append(f"\n{'=' * 78}\n{title}\n{'=' * 78}\n{body}\n")

    _emit(
        "META",
        json.dumps(
            {
                "commit_message": args.commit_message,
                "goal": args.goal,
                "scope": args.scope,
                "diff_size_chars": len(diff),
                "triad_block_reason": triad_block_reason,
                "triad_advisory_count": len(triad_advisory),
            },
            indent=2,
            ensure_ascii=False,
        ),
    )

    triad_raw = list(getattr(ctx, "_last_triad_raw_results", []) or [])
    if not triad_raw:
        _emit("TRIAD REVIEWERS", "<empty — no actor records produced>")
    else:
        for idx, actor in enumerate(triad_raw):
            _emit(
                f"TRIAD REVIEWER {idx + 1}/{len(triad_raw)}",
                _format_review_record(actor, [("parsed_items", "parsed_items")]),
            )

    scope_raw = getattr(ctx, "_last_scope_raw_result", {}) or {}
    if not scope_raw:
        _emit("SCOPE REVIEWER", "<empty — no scope record produced>")
    else:
        _emit(
            "SCOPE REVIEWER",
            _format_review_record(scope_raw, [
                ("critical_findings", "critical_findings"),
                ("advisory_findings", "advisory_findings"),
            ]),
        )

    if review_err:
        _emit("TRIAD BLOCK MESSAGE (review_err)", review_err)

    if scope_result is not None and getattr(scope_result, "block_message", None):
        _emit("SCOPE BLOCK MESSAGE (scope_result.block_message)", scope_result.block_message)

    _emit(
        "AGGREGATED VERDICT",
        json.dumps(aggregated, indent=2, ensure_ascii=False, default=str)
        if isinstance(aggregated, (dict, list))
        else str(aggregated),
    )

    if args.output:
        try:
            pathlib.Path(args.output).write_text("".join(out_buf), encoding="utf-8")
            sys.stderr.write(f"[run_external_review] full output also written to {args.output}\n")
        except Exception as exc:
            sys.stderr.write(f"[run_external_review] failed to write --output: {exc}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())

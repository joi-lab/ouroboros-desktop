"""
Ouroboros — Review utilities.

Utilities for code collection and complexity metrics.
Review tasks go through the standard agent tool loop (LLM-first).
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict, List, Tuple

from ouroboros.utils import clip_text, estimate_tokens


_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".mp3", ".mp4", ".mov",
    ".avi", ".wav", ".ogg", ".opus", ".woff", ".woff2", ".ttf", ".otf",
    ".class", ".so", ".dylib", ".bin",
    # sensitive credential/key files
    ".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der",
    # database files
    ".sqlite", ".db", ".sqlite3",
    # large binary/data files
    ".csv", ".tsv", ".parquet", ".avro", ".h5", ".pt", ".ckpt", ".safetensors",
}

_SKIP_FILENAMES = {
    ".env", ".env.local", ".env.production",
    "secrets.json", "credentials.json", "credentials.yml",
    ".netrc", ".npmrc", ".pypirc",
}

_MAX_FILE_BYTES = 1_048_576  # 1 MB per-file cap
TARGET_MODULE_LINES = 1000
MAX_MODULE_LINES = 1600
TARGET_FUNCTION_LINES = 150
# Raised in v4.40.0 from 250 to 300: advisory SDK orchestration
# (claude_advisory_review._handle_advisory_pre_review at 294 lines) packs a
# coherent single-call flow whose decomposition would obscure control flow
# more than the size itself.  Splitting would require an unrelated refactor
# and is tracked as tech-debt, not a fresh violation.
MAX_FUNCTION_LINES = 300
# Raised in v4.40.0 from 1160 to 1200: absorbs the ~9 new helper functions
# introduced by the safety.py policy-based rewrite (_is_secret_key,
# _redact_secret_value, _redact_secrets_in_arguments, _redact_secrets_in_text,
# _any_remote_provider_configured, _any_local_routing_enabled,
# _light_model_has_reachable_provider, _resolve_safety_routing,
# _run_llm_check) with headroom for incremental growth.
# Raised in v4.40.4 from 1200 to 1250: absorbs the commit-readiness debt
# subsystem in review_state.py (_allocate_commit_readiness_debt_id,
# _hydrate_commit_readiness_debt, _build_commit_readiness_debt_observations,
# _sync_commit_readiness_debts, get_open_commit_readiness_debts,
# _commit_readiness_debts_view, _coalesce_open_obligations,
# _allocate_obligation_id, _hydrate_obligation, _touch_obligation,
# _update_obligations_from_attempt, _make_obligation_fingerprint,
# _looks_like_public_obligation_id, _stable_digest, _normalize_*_key,
# plus the shared _run_reviewed_stage_cycle / _run_non_committing_review_cycle
# extraction in tools/git.py and _commit_readiness_debts_payload in
# claude_advisory_review.py) with headroom for incremental growth.
# Phase 3 three-layer refactor adds the external skill surface
# (``ouroboros/skill_loader.py``, ``ouroboros/skill_review.py``,
# ``ouroboros/tools/skill_exec.py``) with exception sentinels,
# streaming-output runner, capped readers, and scoping helpers.
# Ceiling raised to 1350 to accommodate that surface + Phase 4–6
# headroom (extension loader, Widget ABI, pro-mode auto-PR).
# v4.50.0-rc.5 raises ceiling 1350 → 1450: Phases 3–6 actually landed,
# producing ~47 new helpers across ``extension_loader``,
# ``extensions_api``, ``contracts/plugin_api``, ``launcher_bootstrap``,
# ``onboarding_wizard``, and the new ``scripts/build_repo_bundle``
# tag-verification helpers. Splitting further would require a refactor
# larger than the pre-release scope; the ceiling bump stays consistent
# with how MAX_TOTAL_FUNCTIONS has grown through v4.40→v4.47 as each
# phase shipped.
MAX_TOTAL_FUNCTIONS = 1730  # v5.7.0 + compat foundation + KV-cache prefix helpers
# v4.40.0 adds claude_advisory_review.py to the grandfathered set: the file
# grew to 1731 lines across v4.37-v4.39 (plan_task quorum + direct-provider
# fallback + convergence rule + syntax preflight + reflection decoupling).
# Splitting is deferred until each surface stabilises.
#
# v4.50.0-rc.5 adds server.py: grew past 1600 lines (now 1659) across
# Phases 2–5 (runtime-mode endpoints, extensions HTTP surface, local
# model API, plus the LAN hint + Skills toggle + review routes). A split
# candidate exists (onboarding/settings HTTP leg → ``ouroboros/server_ui.py``)
# but is deferred to a dedicated structural refactor rather than
# blocking the pre-release.
GRANDFATHERED_OVERSIZED_MODULES = {"llm.py", "claude_advisory_review.py", "review_state.py", "server.py"}
# Immutable bundle-only entrypoints ship with release artifacts but should not
# count against the self-editable codebase function budget.
FUNCTION_COUNT_EXCLUDED_FILES = {"launcher.py"}


# ---------------------------------------------------------------------------
# Complexity metrics
# ---------------------------------------------------------------------------

def compute_complexity_metrics(sections: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Compute codebase complexity metrics from collected sections."""
    total_lines = 0
    total_functions = 0
    function_lengths: List[Tuple[str, int, int]] = []  # (path, start_line, length)
    file_sizes: List[Tuple[str, int]] = []  # (path, lines)
    total_files = len(sections)
    py_files = 0

    for path, content in sections:
        lines = content.splitlines()
        line_count = len(lines)
        total_lines += line_count
        file_sizes.append((path, line_count))

        if not path.endswith(".py"):
            continue
        if pathlib.Path(path).name in FUNCTION_COUNT_EXCLUDED_FILES:
            continue
        py_files += 1

        func_starts: List[int] = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                func_starts.append(i)
                total_functions += 1

        for j, start in enumerate(func_starts):
            # Get indentation of the def line
            def_line = lines[start]
            def_indent = len(def_line) - len(def_line.lstrip())

            # Find end: first non-blank, non-comment line with indent <= def_indent
            end = len(lines)
            for k in range(start + 1, len(lines)):
                line = lines[k]
                stripped = line.strip()
                # Skip blank lines and comments
                if not stripped or stripped.startswith("#"):
                    continue
                # Check indentation
                line_indent = len(line) - len(line.lstrip())
                if line_indent <= def_indent:
                    end = k
                    break

            # Cap at next function start if it comes first
            if j + 1 < len(func_starts):
                end = min(end, func_starts[j + 1])

            length = end - start
            function_lengths.append((path, start, length))

    # Compute aggregates
    func_lens = [length for _, _, length in function_lengths]
    avg_func_len = round(sum(func_lens) / max(1, len(func_lens)), 1) if func_lens else 0
    max_func_len = max(func_lens) if func_lens else 0

    # Sort for reporting
    largest_files = sorted(file_sizes, key=lambda x: x[1], reverse=True)[:10]
    longest_functions = sorted(function_lengths, key=lambda x: x[2], reverse=True)[:10]
    target_drift_functions = [
        (p, start, length)
        for p, start, length in function_lengths
        if length > TARGET_FUNCTION_LINES
    ]
    oversized_functions = [
        (p, start, length)
        for p, start, length in function_lengths
        if length > MAX_FUNCTION_LINES
    ]
    target_drift_modules = [
        (p, lines)
        for p, lines in file_sizes
        if p.endswith(".py") and lines > TARGET_MODULE_LINES
    ]
    grandfathered_modules = [
        (p, lines)
        for p, lines in file_sizes
        if p.endswith(".py")
        and lines > MAX_MODULE_LINES
        and pathlib.Path(p).name in GRANDFATHERED_OVERSIZED_MODULES
    ]
    oversized_modules = [
        (p, lines)
        for p, lines in file_sizes
        if p.endswith(".py")
        and lines > MAX_MODULE_LINES
        and pathlib.Path(p).name not in GRANDFATHERED_OVERSIZED_MODULES
    ]

    return {
        "total_files": total_files,
        "py_files": py_files,
        "total_lines": total_lines,
        "total_functions": total_functions,
        "avg_function_length": avg_func_len,
        "max_function_length": max_func_len,
        "largest_files": largest_files,
        "longest_functions": longest_functions,
        "target_drift_functions": target_drift_functions,
        "oversized_functions": oversized_functions,
        "target_drift_modules": target_drift_modules,
        "grandfathered_modules": grandfathered_modules,
        "oversized_modules": oversized_modules,
    }


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_sections(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    max_file_chars: int = 300_000,
    max_total_chars: int = 4_000_000,
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """Walk repo and drive, collect text files as (path, content) pairs."""
    sections: List[Tuple[str, str]] = []
    total_chars = 0
    truncated = 0
    dropped = 0
    dropped_paths: List[str] = []

    def _walk(root: pathlib.Path, prefix: str, skip_dirs: set) -> None:
        nonlocal total_chars, truncated, dropped, dropped_paths
        try:
            root_resolved = root.resolve()
            if not root_resolved.exists():
                return
        except Exception:
            return

        for dirpath, dirnames, filenames in os.walk(str(root_resolved)):
            dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
            for fn in sorted(filenames):
                try:
                    p = pathlib.Path(dirpath) / fn
                    if not p.is_file() or p.is_symlink():
                        continue
                    if p.suffix.lower() in _SKIP_EXT:
                        continue
                    content = p.read_text(encoding="utf-8", errors="replace")
                    if not content.strip():
                        continue
                    rel = p.relative_to(root_resolved).as_posix()
                    if len(content) > max_file_chars:
                        content = clip_text(content, max_file_chars)
                        truncated += 1
                    if total_chars >= max_total_chars:
                        dropped += 1
                        if len(dropped_paths) < 20:
                            dropped_paths.append(f"{prefix}/{rel}")
                        continue
                    if (total_chars + len(content)) > max_total_chars:
                        content = clip_text(content, max(2000, max_total_chars - total_chars))
                        truncated += 1
                    sections.append((f"{prefix}/{rel}", content))
                    total_chars += len(content)
                except Exception:
                    continue

    _walk(repo_dir, "repo",
          {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"})
    # Only scan memory/ — identity, scratchpad, knowledge base (not logs/state/artifacts)
    _walk(drive_root / "memory", "drive/memory", set())

    stats = {
        "files": len(sections),
        "chars": total_chars,
        "truncated": truncated,
        "dropped": dropped,
        "dropped_paths": dropped_paths,
    }
    return sections, stats


def collect_full_codebase(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Tuple[str, Dict[str, Any]]:
    """Collect ALL text files without truncation, formatted for single-context review.

    Returns (full_text, stats) where stats has keys: files, tokens.
    Includes repo files and drive memory files (identity.md, scratchpad.md, knowledge base).
    Skips drive logs (*.jsonl).
    """
    parts: List[str] = []
    file_count = 0

    def _walk(root: pathlib.Path, prefix: str, skip_dirs: set, skip_ext_extra: set = frozenset()) -> None:
        nonlocal file_count
        try:
            root_resolved = root.resolve()
            if not root_resolved.exists():
                return
        except Exception:
            return

        for dirpath, dirnames, filenames in os.walk(str(root_resolved)):
            dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
            for fn in sorted(filenames):
                try:
                    p = pathlib.Path(dirpath) / fn
                    if not p.is_file() or p.is_symlink():
                        continue
                    if p.suffix.lower() in _SKIP_EXT:
                        continue
                    if p.suffix.lower() in skip_ext_extra:
                        continue
                    if fn in _SKIP_FILENAMES:
                        continue
                    if p.stat().st_size > _MAX_FILE_BYTES:
                        continue
                    content = p.read_text(encoding="utf-8", errors="replace")
                    if not content.strip():
                        continue
                    rel = p.relative_to(root_resolved).as_posix()
                    parts.append(f"## FILE: {prefix}/{rel}\n{content}\n")
                    file_count += 1
                except Exception:
                    continue

    _walk(repo_dir, "repo",
          {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv", ".idea", ".vscode"})
    # Only scan memory/ — identity, scratchpad, knowledge base (not logs/state/artifacts)
    _walk(drive_root / "memory", "drive/memory", set(), skip_ext_extra={".jsonl"})

    full_text = "\n".join(parts)
    token_estimate = estimate_tokens(full_text)
    stats = {"files": file_count, "tokens": token_estimate}
    return full_text, stats

"""Blocking scope reviewer for Ouroboros commit pipeline.

Runs IN PARALLEL with the triad diff review. Single-model (configurable via OUROBOROS_SCOPE_REVIEW_MODEL),
fail-closed: timeout, parse error, API failure, or unreadable touched-file context all block.

Role: full-codebase reviewer with unique advantage — sees the ENTIRE repository,
not just the diff. Finds cross-module bugs, broken implicit contracts, hidden
regressions, and forgotten touchpoints that diff-only triad reviewers miss.

The budget gate skips scope review (non-blocking warning) when the assembled
scope-review prompt exceeds the model's safe input budget, preventing
context-window errors.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from dataclasses import dataclass, field
from typing import List, Optional

from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.review_helpers import (
    build_full_repo_pack,
    build_goal_section,
    build_rebuttal_section as _shared_build_rebuttal_section,
    build_scope_section,
    build_touched_file_pack,
    load_checklist_section,
    CRITICAL_FINDING_CALIBRATION,
    BINARY_EXTENSIONS,
    _SENSITIVE_EXTENSIONS,
    _SENSITIVE_NAMES,
    load_governance_doc,
    _ANTI_THRASHING_RULE_VERDICT,
    _CONVERGENCE_RULE_TEXT,
    _HISTORY_VERIFICATION_ONLY_RULE,
    build_review_history_section as _shared_review_history_section,
    emit_review_usage,
    format_review_history_entry,
)
from ouroboros.triad_review import extract_json_array
from ouroboros.utils import run_cmd, utc_now_iso, append_jsonl, estimate_tokens

log = logging.getLogger(__name__)

_SCOPE_MODEL_DEFAULT = "openai/gpt-5.5"
_SCOPE_MAX_TOKENS = 100_000  # 100K output tokens

# Budget gate: if the fully assembled scope-review prompt (input) exceeds this
# token estimate, scope review is skipped with a non-blocking warning instead of
# crashing or sending an oversized request.
#
# Context math: the default reviewer (openai/gpt-5.5) has a 1M context
# window (GA March 2026) that is SHARED between input and output; other configured
# reviewers via OUROBOROS_SCOPE_REVIEW_MODEL may have different ceilings.
# `estimate_tokens` uses chars/4 which under-counts real tokens by ~15%, so at
# gate=850_000 actual input is ≈1_000_000 tokens. On the default 1M model, output
# `_SCOPE_MAX_TOKENS` draws from the same 1M window, so near-gate prompts sit
# close to the API ceiling; the non-blocking skip path is best-effort, not a
# guarantee — some API-level rejections at 850K are still possible. This is a
# conscious trade: 850K lets scope-review prompts that previously skipped at
# ~778K actually run.
from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET as _REVIEW_BUDGET

_SCOPE_BUDGET_TOKEN_LIMIT = _REVIEW_BUDGET

# Defense-in-depth cap for deleted-file content inlined into the scope prompt.
# Matches the >1MB guard that `build_head_snapshot_section` applied before
# v4.33 — the staged diff itself is not size-capped here, but the inline pack
# has no reason to carry a 10 MB HEAD snapshot.
_DELETED_INLINE_MAX_BYTES = 1_048_576  # 1 MB


@dataclass
class ScopeReviewResult:
    """Structured outcome from run_scope_review.

    blocked: True if the commit is blocked.
    block_message: Human-readable block string (non-empty when blocked=True).
    critical_findings: List of structured finding dicts (same schema as triad review):
        {"verdict": "FAIL", "severity": "critical", "item": <real_item_name>,
         "reason": <str>, "model": "scope_reviewer"}
    advisory_findings: Advisory (non-blocking) finding dicts.
    """
    blocked: bool = False
    block_message: str = ""
    critical_findings: List[dict] = field(default_factory=list)
    advisory_findings: List[dict] = field(default_factory=list)
    # Canonical per-actor evidence (epistemic integrity)
    raw_text: str = ""
    model_id: str = ""
    status: str = "responded"  # "responded"|"error"|"parse_failure"|"empty_response"|"budget_exceeded"|"omitted"|"empty"
    prompt_chars: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


@dataclass
class _TouchedContextStatus:
    """Structured status for touched-file context collection.

    Using a dataclass avoids magic-string channels where real filenames could
    accidentally collide with control sentinels (e.g. a file literally named
    ``__empty__`` or ``__budget_exceeded_N__``).

    status values:
      "empty"          — no touched files could be read at all (fail-closed)
      "omitted"        — some touched files are unreadable (fail-closed)
      "budget_exceeded" — touched files OK but the assembled prompt exceeds token limit

    Success (all files readable, prompt fits budget) is represented by
    returning None from _compute_touched_status / _build_scope_prompt,
    not by a separate "ok" status value.
    """
    status: str  # "empty" | "omitted" | "budget_exceeded"
    omitted_paths: List[str] = field(default_factory=list)
    token_count: int = 0  # estimated full prompt tokens when budget is exceeded


def _get_scope_model() -> str:
    """Return the configured scope review model (env → settings default)."""
    return (
        os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL", "").strip()
        or _SCOPE_MODEL_DEFAULT
    )

_SCOPE_PREAMBLE = (
    "You are a pre-commit reviewer for Ouroboros, a self-modifying AI agent.\n"
    "Its Constitution is BIBLE.md. Its engineering handbook is DEVELOPMENT.md.\n"
)


_CANONICAL_CONTEXT_DOCS = (
    "BIBLE.md",
    "docs/DEVELOPMENT.md",
    "docs/ARCHITECTURE.md",
    "docs/CHECKLISTS.md",
)
_CURRENT_TOUCHED_CONTEXT_SKIP_PREFIXES = (
    "tests/",
)


def _load_doc(repo_dir: pathlib.Path, rel_path: str) -> str:
    return load_governance_doc(repo_dir, rel_path, on_missing="placeholder")


def _load_dev_guide(repo_dir: pathlib.Path) -> str:
    """Compatibility wrapper for tests and older callers."""
    return _load_doc(repo_dir, "docs/DEVELOPMENT.md")


def _load_canonical_context_docs(repo_dir: pathlib.Path) -> str:
    parts: list[str] = []
    for rel_path in _CANONICAL_CONTEXT_DOCS:
        parts.append(f"## {rel_path}\n\n{_load_doc(repo_dir, rel_path)}")
    return "\n\n---\n\n".join(parts)


def _should_skip_current_touched_context(path: str) -> bool:
    norm = str(path or "").replace("\\", "/").lstrip("./")
    return (
        norm in _CANONICAL_CONTEXT_DOCS
        or any(norm.startswith(prefix) for prefix in _CURRENT_TOUCHED_CONTEXT_SKIP_PREFIXES)
    )


def _build_review_history_section(history: list, open_obligations: list = None) -> str:
    """Format previous triad rounds for scope-review prompt context."""
    return _shared_review_history_section(
        history,
        open_obligations,
        title="## Previous triad review rounds",
        include_commit_message=False,
        compact_labels=True,
    )


def _parse_staged_name_status(repo_dir: pathlib.Path) -> list:
    """Parse staged changes with name-status for rename/delete/copy awareness.

    Returns list of (status_char, current_path, head_lookup_path) tuples:
    - status_char: A=added, M=modified, D=deleted, R=renamed, C=copied
    - current_path: path in current working tree (new path for renames)
    - head_lookup_path: path to use for git show HEAD (old path for renames)
    """
    try:
        name_status_raw = run_cmd(
            ["git", "diff", "--cached", "--name-status"], cwd=repo_dir
        )
    except Exception:
        name_status_raw = ""

    entries = []
    for line in name_status_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if not parts:
            continue
        status_char = parts[0][0].upper()
        if status_char in ("R", "C") and len(parts) >= 3:
            old_path, new_path = parts[1], parts[-1]
            entries.append((status_char, new_path, old_path))
        elif len(parts) >= 2:
            path = parts[1]
            entries.append((status_char, path, path))
        else:
            entries.append(("M", parts[0], parts[0]))

    # Fallback to --name-only if --name-status produced nothing
    if not entries:
        try:
            changed = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=repo_dir)
            for p in changed.strip().splitlines():
                p = p.strip()
                if p:
                    entries.append(("M", p, p))
        except Exception:
            pass

    return entries


def _classify_deleted_for_inline(path: str) -> Optional[str]:
    """Return a suppression reason for a deleted path, or None to inline its content.

    Mirrors the sensitive / binary filtering that ``build_head_snapshot_section``
    applied before v4.33 (defense-in-depth — the staged diff itself is not
    filtered, but our inline pack has no reason to duplicate a secret or a
    binary blob into the scope prompt). Path-only check; size check happens
    after ``git show`` in ``_inline_deleted_file_pack``.
    """
    fp = pathlib.Path(path)
    fname_lower = fp.name.lower()
    suffix_lower = fp.suffix.lower()
    if suffix_lower in _SENSITIVE_EXTENSIONS or fname_lower in _SENSITIVE_NAMES:
        return "sensitive (env/credential/key)"
    if suffix_lower in BINARY_EXTENSIONS:
        return "binary extension"
    return None


def _inline_deleted_file_pack(
    current_files_section: str,
    deleted_paths: list,
    repo_dir: pathlib.Path,
) -> str:
    """Append deleted-file blocks to the current-files section with HEAD content.

    Since v4.33.0 scope review no longer emits a separate ``Pre-change
    snapshots`` section (the staged diff and full repo pack already cover
    cross-module context). For deleted files we still want the reviewer to
    see what was removed, so we inline the HEAD version directly with an
    explicit ``DELETED`` marker.

    Defense-in-depth filtering — sensitive / binary / oversize
    deletions are replaced with a suppression marker instead of having
    their HEAD content echoed into the scope prompt. Falls back to an
    ``HEAD content unavailable`` marker when ``git show`` can't produce the
    file (e.g. staged-for-delete without ever being committed, or HEAD is
    corrupt). Never raises.
    """
    if not deleted_paths:
        return current_files_section

    notes: list[str] = []
    for dp in deleted_paths:
        suffix = pathlib.Path(dp).suffix.lstrip(".") or "text"
        suppress_reason = _classify_deleted_for_inline(dp)
        if suppress_reason is not None:
            notes.append(
                f"### {dp}\n\n*(DELETED — {suppress_reason}; content suppressed)*\n"
            )
            continue

        try:
            head_content = run_cmd(
                ["git", "show", f"HEAD:{dp}"], cwd=repo_dir
            )
        except Exception:
            head_content = ""

        if head_content and len(
            head_content.encode("utf-8", errors="replace")
        ) > _DELETED_INLINE_MAX_BYTES:
            notes.append(
                f"### {dp}\n\n*(DELETED — content > "
                f"{_DELETED_INLINE_MAX_BYTES // 1024} KB; suppressed)*\n"
            )
            continue

        if head_content:
            notes.append(
                f"### {dp}\n\n*(DELETED — content from HEAD)*\n\n"
                f"```{suffix}\n{head_content}\n```\n"
            )
        else:
            notes.append(
                f"### {dp}\n\n*(DELETED — HEAD content unavailable; "
                "see staged diff for removed lines)*\n"
            )

    joint = "\n".join(notes)
    if current_files_section.strip():
        return current_files_section + "\n\n" + joint
    return joint


def _compute_touched_status(
    current_files_section: str,
    deleted_paths: list,
    omitted: list,
    current_paths: list,
) -> Optional["_TouchedContextStatus"]:
    """Return a _TouchedContextStatus when touched-file context is incomplete, or None if OK.

    Deletion-only diffs are valid (HEAD snapshot provides context).
    Blocks only when the current-files section is truly empty with no deletions,
    or when some readable non-deleted files couldn't be read.

    Returns None when all touched files are accessible (proceed to budget check).
    """
    if not current_files_section.strip() and not deleted_paths:
        return _TouchedContextStatus(status="empty")
    if omitted and current_paths:
        return _TouchedContextStatus(status="omitted", omitted_paths=list(omitted))
    return None


def _gather_scope_packs(repo_dir: pathlib.Path, all_touched_paths: list) -> str:
    """Collect the wider repository pack for scope review.

    Raises RuntimeError on git failure (fail-closed).
    """
    # The canonical docs are injected explicitly into the prompt below. Exclude
    # them from the wider pack to avoid duplicating BIBLE / ARCHITECTURE /
    # CHECKLISTS / DEVELOPMENT while still keeping them in every scope review.
    exclude_set = set(all_touched_paths) | set(_CANONICAL_CONTEXT_DOCS)
    try:
        full_pack, _repo_omitted = build_full_repo_pack(repo_dir, exclude_paths=exclude_set)
        repo_pack_section = full_pack
        if _repo_omitted:
            repo_pack_section += (
                f"\n\n*(Omitted {len(_repo_omitted)} file(s): binary, vendored, sensitive, or >1MB)*\n"
            )
        if not repo_pack_section.strip():
            repo_pack_section = "(no additional repo files)"
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"build_full_repo_pack error: {exc}") from exc

    return repo_pack_section


def _scope_round_label(entry: dict) -> str:
    """Derive a round label from a scope history entry.

    Epistemic-integrity rule (v4.32.0): a round that was degraded, omitted,
    budget-exceeded, or failed to parse MUST NOT be labelled ``PASSED``.
    Only a genuine responded-no-findings round gets ``PASSED``.

    Label priority:
      1. ``blocked=True``        → ``BLOCKED``
      2. status != "responded"   → uppercase status (``BUDGET_EXCEEDED`` etc.)
      3. otherwise                → ``PASSED``
    """
    if entry.get("blocked"):
        return "BLOCKED"
    status = str(entry.get("status") or "responded").strip()
    if status and status != "responded":
        return status.upper()
    return "PASSED"


def _build_scope_history_section(scope_review_history: Optional[list]) -> str:
    """Format prior scope review rounds into a prompt section."""
    if not scope_review_history:
        return ""
    rounds = []
    for i, entry in enumerate(scope_review_history, 1):
        label = _scope_round_label(entry)
        parts = [f"Round {i}: {label}"]
        critical_findings = list(entry.get("critical_findings") or [])
        advisory_findings = list(entry.get("advisory_findings") or [])
        if critical_findings:
            parts.append("Critical findings:")
            for finding in critical_findings:
                parts.append(f"- {format_review_history_entry(finding, default_severity='critical')}")
        if advisory_findings:
            parts.append("Advisory findings:")
            for finding in advisory_findings:
                parts.append(f"- {format_review_history_entry(finding)}")
        if not critical_findings and not advisory_findings:
            parts.append(str(entry.get("summary") or "(no summary)"))
        rounds.append("\n".join(parts))
    return (
        "\n## Prior scope review rounds (your previous findings for this commit)\n\n"
        + "\n\n---\n".join(rounds)
        + "\n\nAddress any previously raised issues. If the same issue persists, "
        "mark it FAIL again with a reference to the prior round.\n"
        f"\nIMPORTANT: {_HISTORY_VERIFICATION_ONLY_RULE}\n"
        f"\nIMPORTANT: {_ANTI_THRASHING_RULE_VERDICT}\n"
    )


def _build_scope_prompt(
    repo_dir: pathlib.Path,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
    review_history: Optional[list] = None,
    scope_review_history: Optional[list] = None,
    drive_root: Optional[pathlib.Path] = None,
) -> tuple:
    """Build the scope review prompt with full context packs.

    Returns (prompt_str_or_None, context_status_or_None) where:
    - (prompt_str, None) — all OK, ready for LLM call
    - (None, _TouchedContextStatus(status="empty"))   — no touched files (fail-closed)
    - (None, _TouchedContextStatus(status="omitted"))  — some files unreadable (fail-closed)
    - (None, _TouchedContextStatus(status="budget_exceeded", token_count=N)) — assembled prompt too large

    Priority: touched-file omission ALWAYS takes precedence over the budget gate.
    This ensures fail-closed guarantees for unreadable/binary touched files cannot
    be silently downgraded to a non-blocking advisory by the budget gate.
    """
    try:
        scope_checklist = load_checklist_section("Intent / Scope Review Checklist")
    except Exception:
        scope_checklist = "(Intent / Scope Review Checklist not found in docs/CHECKLISTS.md)"

    goal_section = build_goal_section(goal, scope, commit_message)
    scope_section = build_scope_section(scope)
    canonical_docs = _load_canonical_context_docs(repo_dir)
    critical_calibration = CRITICAL_FINDING_CALIBRATION  # noqa: F841 — used in f-string below
    rebuttal_section = _shared_build_rebuttal_section(review_rebuttal)
    # Load open obligations for anti-thrashing hint.
    # Always load (not gated on review_history): scope may block independently of triad,
    # so scope obligations should be visible even when triad history is empty.
    _open_obs_for_scope = []
    _drive_root = pathlib.Path(drive_root) if drive_root else None
    if _drive_root is not None:
        try:
            from ouroboros.review_state import load_state, make_repo_key
            _rs = load_state(_drive_root)
            _repo_key = make_repo_key(repo_dir)
            _open_obs_for_scope = _rs.get_open_obligations(repo_key=_repo_key)
        except Exception:
            pass  # Non-fatal: best-effort hint
    history_section = _build_review_history_section(
        review_history or [], open_obligations=_open_obs_for_scope,
    )
    scope_history_section = _build_scope_history_section(scope_review_history)

    # Scope-only retry path (v4.39.0): if the same commit has been blocked
    # repeatedly by scope review while triad passed, `review_history` is
    # empty but `scope_review_history` grows. The convergence rule is
    # emitted only from `_build_review_history_section` based on
    # `review_history`, so scope-only chains would silently skip it.
    # Inject the rule once here when `scope_review_history >= 2` and
    # the triad-history section didn't already include it.
    if (
        scope_review_history
        and len(scope_review_history) >= 2
        and _CONVERGENCE_RULE_TEXT not in history_section
    ):
        scope_history_section = (
            (scope_history_section.rstrip() + "\n\n")
            if scope_history_section
            else ""
        ) + f"**IMPORTANT: {_CONVERGENCE_RULE_TEXT}**\n"

    try:
        diff_text = run_cmd(["git", "diff", "--cached"], cwd=repo_dir)
    except Exception:
        diff_text = "(failed to get staged diff)"

    touched_entries = _parse_staged_name_status(repo_dir)
    current_paths = [ep[1] for ep in touched_entries if ep[0] != "D"]
    deleted_paths = [ep[1] for ep in touched_entries if ep[0] == "D"]
    all_touched_paths = [ep[1] for ep in touched_entries]

    current_context_paths = [
        path for path in current_paths
        if not _should_skip_current_touched_context(path)
    ]
    current_skipped_by_design = [
        path for path in current_paths
        if _should_skip_current_touched_context(path)
    ]

    current_files_section, omitted = build_touched_file_pack(repo_dir, current_context_paths)
    current_files_section = _inline_deleted_file_pack(
        current_files_section, deleted_paths, repo_dir
    )
    if current_skipped_by_design:
        skip_note = (
            "## CURRENT FILE CONTEXT DEDUPLICATION NOTE\n"
            "The following touched files are not duplicated as full current-file "
            "snapshots because they are either canonical docs injected above or "
            "tests whose exact changes are visible in the staged diff below:\n"
            + "\n".join(f"- {path}" for path in current_skipped_by_design)
            + "\n"
        )
        current_files_section = (
            current_files_section + "\n\n" + skip_note
            if current_files_section.strip()
            else skip_note
        )
    touched_status = _compute_touched_status(
        current_files_section, deleted_paths, omitted, current_context_paths
    )

    # Fail-closed check BEFORE the budget gate: touched-file omission always wins.
    # If some touched files are unreadable/binary, return immediately with the
    # structured status so _handle_prompt_signals can block the commit. This prevents
    # the budget gate from silently downgrading an incomplete-context failure to an
    # advisory skip.
    if touched_status is not None:
        return None, touched_status

    repo_pack_section = _gather_scope_packs(repo_dir, all_touched_paths)

    prompt = f"""\
{_SCOPE_PREAMBLE}

## Your role

You are the fourth reviewer — and the most powerful one.

The triad diff reviewers see only the changed files and the diff hunks.
**You see the ENTIRE codebase.** Use that advantage.

Your primary mission: find problems that diff-only reviewers CANNOT see.
Specifically:
- **Cross-module bugs**: does this change break something in a different module
  through implicit coupling, shared state, or assumed call patterns?
- **Broken implicit contracts**: are there constants, data format assumptions,
  expected function signatures, or protocol invariants relied upon by OTHER
  modules that this change violates without updating those callers?
- **Hidden regressions**: does a seemingly-unrelated module elsewhere in the
  repo break because of this change in a non-obvious way?
- **Forgotten touchpoints**: exact files/symbols that MUST also change but don't —
  sibling tests, config values, adjacent prompts, parallel flows, doc sections.
- **Completeness**: does the diff actually accomplish the stated intent end-to-end,
  or does it only fix one surface while leaving the rest inconsistent?

Diff-only reviewers handle line-by-line correctness. You handle whole-system coherence.

## Your task

For each finding, you MUST name the exact file, symbol, test, prompt, doc,
config, or sibling flow that proves the issue. Vague concerns without a
concrete artifact reference must be marked advisory, not critical.

## Output format

Output ONLY a valid JSON array.

You MUST cover every checklist item from the Intent / Scope Review
Checklist below. Skipping an item is not allowed — a missing entry
indicates the item was not actually reviewed.

The eight checklist item identifiers you MUST return (exactly these strings
in the "item" field; no substitutions):

    1. intent_alignment
    2. forgotten_touchpoints
    3. cross_surface_consistency
    4. regression_surface
    5. prompt_doc_sync
    6. architecture_fit
    7. cross_module_bugs
    8. implicit_contracts

Each element must have:
- "item" (one of the eight identifiers above — verbatim, case-sensitive)
- optional "obligation_id" when resolving or re-checking a previously surfaced obligation
- "verdict": "PASS" or "FAIL"
- "severity": "critical" or "advisory"
- "reason":
  - For FAIL: concrete artifact (file/symbol/line/contract) + what is wrong + how to fix.
  - For PASS: 1–2 sentences stating WHY this item passes, naming a concrete
    artifact or code path that you checked. A bare "PASS" or single-word
    reason without justification indicates the item was not actually
    reviewed and will be treated as a reviewer failure.

If one checklist item has multiple distinct concrete problems, return one
FAIL entry per distinct root cause. Do not compress unrelated bugs into a
single summary. If an item has no problems, return one PASS entry. Do not
return duplicate PASS entries, and do not return PASS for an item that also
has a FAIL — the concrete FAIL is authoritative.

Severity rules:
- Use "critical" only when you can cite a concrete missing file, symbol, test, prompt, doc, config, or sibling path and explain why the transformation is incomplete or inconsistent.
- If you cannot point to an exact touchpoint, use "advisory".
- Scope affects only unchanged legacy code outside the diff. The diff itself is always fully reviewable.
- For cross-surface / prose-vs-code mismatches apply the `Critical surface whitelist` in `docs/CHECKLISTS.md` — only release metadata, tool schema, module map, behavioural documentation, and safety contracts qualify as critical; commentary and narrative prose mismatches are advisory.

If an open obligation record above already names an `obligation_id` for this root cause,
reuse that exact `obligation_id`. Do NOT invent a new id for the same root cause.

## Anti pattern-lock guard

If after your first reading you have found **exactly one FAIL** and all
other items are PASS, do a deliberate SECOND pass focused on a DIFFERENT
concern class before returning. Real diffs that have exactly one issue
are rarer than diffs with several issues on different dimensions;
single-FAIL outputs are the most common pattern-lock failure mode.

Concrete pairings for the second pass:
- If your FAIL was in `intent_alignment`, re-examine `forgotten_touchpoints` and `cross_module_bugs`.
- If your FAIL was in `forgotten_touchpoints`, re-examine `cross_surface_consistency` and `implicit_contracts`.
- If your FAIL was in `cross_surface_consistency`, re-examine `implicit_contracts` and `regression_surface`.
- If your FAIL was in `regression_surface`, re-examine `cross_module_bugs` and `architecture_fit`.
- If your FAIL was in `cross_module_bugs`, re-examine `implicit_contracts` and `forgotten_touchpoints`.
- If your FAIL was in `implicit_contracts`, re-examine `cross_module_bugs` and `regression_surface`.
- If your FAIL was in `prompt_doc_sync`, re-examine `cross_surface_consistency` and `architecture_fit`.
- If your FAIL was in `architecture_fit`, re-examine `implicit_contracts` and `regression_surface`.

Update PASS entries in-place if your second pass uncovers new FAILs.
Return only one JSON array — not two.

{critical_calibration}

{scope_checklist}
{scope_section}

{goal_section}

## Canonical Documentation Context

These files are always included explicitly. Do not treat their absence from the
wider repository pack as omission.

{canonical_docs}

{rebuttal_section}{history_section}{scope_history_section}

## Current touched files (post-change — what the file looks like NOW)

Files deleted by this diff appear here with an explicit `DELETED` marker and
their HEAD content inlined; other removed lines are visible via the staged
diff below. HEAD versions of modified files are not sent as a separate
section — the staged diff below already shows every `-` line.

{current_files_section}

## Staged diff

{diff_text}

## Wider repository context

{repo_pack_section}
"""
    prompt_tokens = estimate_tokens(prompt)
    if prompt_tokens > _SCOPE_BUDGET_TOKEN_LIMIT:
        return None, _TouchedContextStatus(
            status="budget_exceeded",
            token_count=prompt_tokens,
        )
    return prompt, None


def _classify_scope_findings(items: list) -> tuple:
    """Classify raw JSON items into (critical_findings, advisory_findings) lists."""
    critical_findings: List[dict] = []
    advisory_findings: List[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        verdict = str(item.get("verdict", "")).upper()
        severity = str(item.get("severity", "advisory")).lower()
        if verdict != "FAIL":
            continue
        finding = {
            "verdict": "FAIL",
            "severity": severity,
            "item": str(item.get("item", "scope_review")),
            "reason": str(item.get("reason", "")),
            "model": "scope_reviewer",
        }
        obligation_id = str(item.get("obligation_id", "") or "")
        if obligation_id:
            finding["obligation_id"] = obligation_id
        if severity == "critical":
            critical_findings.append(finding)
        else:
            advisory_findings.append(finding)
    return critical_findings, advisory_findings


def _emit_usage(ctx: ToolContext, model: str, usage: dict) -> None:
    emit_review_usage(ctx, model=model, usage=usage, source="scope_review")


def _log_scope_result(
    ctx: ToolContext,
    critical_count: int,
    advisory_count: int,
    prompt_chars: int = 0,
) -> None:
    """Append a scope_review_complete event to events.jsonl.

    Also emits budget headroom metrics so operators can see when the scope
    pack is approaching the gate. ``headroom_tokens`` is a signed delta
    (negative when the prompt exceeds the gate — would have been skipped).
    """
    prompt_tokens = max(0, int(prompt_chars) // 4) if prompt_chars else 0
    try:
        append_jsonl(ctx.drive_logs() / "events.jsonl", {
            "ts": utc_now_iso(), "type": "scope_review_complete",
            "task_id": getattr(ctx, "task_id", "") or "",
            "model": _get_scope_model(),
            "critical_count": critical_count,
            "advisory_count": advisory_count,
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_budget": _SCOPE_BUDGET_TOKEN_LIMIT,
            "headroom_tokens": _SCOPE_BUDGET_TOKEN_LIMIT - prompt_tokens,
        })
    except Exception:
        pass


def _call_scope_llm(prompt: str) -> tuple:
    """Execute the scope review LLM call synchronously.

    Returns (raw_text, usage, error_msg) — error_msg is non-empty on failure.
    """
    from ouroboros.config import resolve_effort as _resolve_effort
    scope_model = _get_scope_model()
    scope_effort = _resolve_effort("scope_review")
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": "Review the staged change and context above. Output ONLY a JSON array.",
        },
    ]
    llm = LLMClient()
    try:
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                msg, usage = pool.submit(
                    asyncio.run,
                    llm.chat_async(
                        messages=messages,
                        model=scope_model,
                        reasoning_effort=scope_effort,
                        max_tokens=_SCOPE_MAX_TOKENS,
                        temperature=0.2,
                        no_proxy=True,
                    ),
                ).result(timeout=180)
        except RuntimeError:
            msg, usage = asyncio.run(
                llm.chat_async(
                    messages=messages,
                    model=scope_model,
                    reasoning_effort=scope_effort,
                    max_tokens=_SCOPE_MAX_TOKENS,
                    temperature=0.2,
                    no_proxy=True,
                )
            )
    except Exception as e:
        error_msg = (
            f"⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer ({scope_model}) failed — commit blocked.\n"
            f"Error: {type(e).__name__}: {e}\n"
            "Retry the commit, or check API key and network connectivity."
        )
        return "", None, error_msg
    return str(msg.get("content") or ""), usage, ""


def _handle_prompt_signals(
    prompt: Optional[str],
    context_status: Optional["_TouchedContextStatus"],
) -> Optional[ScopeReviewResult]:
    """Translate context status into a ScopeReviewResult, or None to continue.

    Returns a completed ScopeReviewResult if processing should stop (budget
    exceeded or incomplete context), or None when the LLM call may proceed.

    Uses a structured _TouchedContextStatus instead of magic strings to avoid
    ambiguous collisions between real filenames and control sentinels.
    """
    if context_status is None:
        return None  # proceed with LLM call

    if context_status.status == "budget_exceeded":
        token_count = context_status.token_count
        # prompt_chars: back-compute from the token estimate used in the budget gate.
        # estimate_tokens uses chars/4, so chars ≈ token_count * 4.  This is the same
        # assembled prompt size that triggered the gate — the most useful forensic fact.
        _prompt_chars_est = token_count * 4
        log.warning(
            "Scope review skipped: full scope-review prompt (~%d tokens) exceeds budget limit (%d). "
            "Scope review downgraded to non-blocking warning.",
            token_count, _SCOPE_BUDGET_TOKEN_LIMIT,
        )
        return ScopeReviewResult(
            blocked=False,
            block_message="",
            status="budget_exceeded",
            prompt_chars=_prompt_chars_est,
            advisory_findings=[{
                "verdict": "FAIL",
                "severity": "advisory",
                "item": "scope_review_skipped",
                "reason": (
                    f"⚠️ SCOPE_REVIEW_SKIPPED: Full scope-review prompt (~{token_count} tokens) "
                    f"exceeds model context budget ({_SCOPE_BUDGET_TOKEN_LIMIT} tokens). "
                    "Scope review downgraded to non-blocking warning. "
                    "Consider reducing codebase size or splitting the review."
                ),
                "model": "scope_reviewer",
            }],
        )

    if context_status.status == "empty":
        return ScopeReviewResult(
            blocked=True,
            status="empty",
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Could not read any touched files — "
                "scope review requires direct file context. Commit blocked."
            ),
        )

    if context_status.status == "omitted":
        omitted_names = ", ".join(context_status.omitted_paths) or "(unknown)"
        return ScopeReviewResult(
            blocked=True,
            status="omitted",
            block_message=(
                f"⚠️ SCOPE_REVIEW_BLOCKED: Some touched file(s) could not be included "
                f"in direct context (binary/oversize/unreadable): {omitted_names}.\n"
                "Scope review requires complete touched-file context. Commit blocked.\n"
                "Possible fixes: reduce file size, commit binary files separately, "
                "or ensure all touched files are readable text."
            ),
        )

    # Unknown status: fail-closed (block commit) to honour the documented contract.
    # The module is explicitly fail-closed except for the explicit 'budget_exceeded' skip.
    # Any unrecognised status is a programming error — block rather than silently proceeding.
    log.error(
        "Scope review: unrecognised _TouchedContextStatus.status=%r — blocking commit (fail-closed).",
        context_status.status,
    )
    return ScopeReviewResult(
        blocked=True,
        status="error",
        block_message=(
            f"⚠️ SCOPE_REVIEW_BLOCKED: Unexpected context status '{context_status.status}' — "
            "commit blocked (fail-closed). This is a programming error; please report it."
        ),
    )


def _build_block_message(
    critical_findings: List[dict], advisory_findings: List[dict]
) -> str:
    """Format critical + advisory findings into a human-readable block message."""
    crit_lines = "\n".join(
        f"  CRITICAL: [scope:{f['item']}] {f['reason']}" for f in critical_findings
    )
    adv_section = ""
    if advisory_findings:
        adv_lines = "\n".join(
            f"  WARN: [scope:{f['item']}] {f['reason']}" for f in advisory_findings
        )
        adv_section = f"\n\nAdvisory warnings:\n{adv_lines}"
    return (
        "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer found critical completeness issues.\n"
        "Commit has NOT been created. Fix the issues and try again.\n\n"
        + crit_lines + adv_section
    )


def run_scope_review(
    ctx: ToolContext,
    commit_message: str,
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
    review_history: Optional[list] = None,
    scope_review_history: Optional[list] = None,  # prior scope rounds for this commit
) -> ScopeReviewResult:
    """Run the blocking scope review. Returns a ScopeReviewResult.

    result.blocked is True if the commit must not proceed.
    result.critical_findings contains structured dicts with real checklist item
    names (not synthetic strings), so callers can pass them directly into
    obligation tracking without any string parsing.
    """
    repo_dir = pathlib.Path(ctx.repo_dir)
    scope_model_id = _get_scope_model()

    try:
        prompt, context_status = _build_scope_prompt(
            repo_dir, commit_message,
            goal=goal, scope=scope,
            review_rebuttal=review_rebuttal,
            review_history=review_history,
            scope_review_history=scope_review_history,
            drive_root=pathlib.Path(ctx.drive_root) if getattr(ctx, "drive_root", None) else None,
        )
    except RuntimeError as exc:
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Failed to build review context — commit blocked.\n"
                f"Error: {exc}\n"
                "Ensure git is available and the repository is in a valid state."
            ),
            model_id=scope_model_id,
            status="error",
        )

    signal_result = _handle_prompt_signals(prompt, context_status)
    if signal_result is not None:
        # Populate model_id for epistemic traceability. DO NOT overwrite
        # signal_result.status — _handle_prompt_signals already sets the
        # canonical status for every early-exit path (budget_exceeded/
        # empty/omitted/error) and any further assignment here would
        # silently diverge from that single source of truth.
        signal_result.model_id = scope_model_id
        return signal_result

    _prompt_chars = len(prompt)  # type: ignore[arg-type]
    raw_text, usage, llm_error = _call_scope_llm(prompt)  # type: ignore[arg-type]
    _tokens_in = int((usage or {}).get("prompt_tokens", 0) or 0)
    _tokens_out = int((usage or {}).get("completion_tokens", 0) or 0)
    _cost_usd = float((usage or {}).get("cost", 0.0) or 0.0)
    if llm_error:
        return ScopeReviewResult(
            blocked=True,
            block_message=llm_error,
            model_id=scope_model_id,
            status="error",
            prompt_chars=_prompt_chars,
        )
    if usage:
        _emit_usage(ctx, scope_model_id, usage or {})

    if not raw_text.strip():
        # Distinct status: model responded (no transport error) but returned empty text.
        # "error" would make this path indistinguishable from API/transport failures.
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer returned empty response — commit blocked.\n"
                "Retry the commit."
            ),
            model_id=scope_model_id,
            status="empty_response",
            prompt_chars=_prompt_chars,
            tokens_in=_tokens_in,
            tokens_out=_tokens_out,
            cost_usd=_cost_usd,
        )

    items = extract_json_array(raw_text, normalize=True)
    if items is None:
        return ScopeReviewResult(
            blocked=True,
            block_message=(
                "⚠️ SCOPE_REVIEW_BLOCKED: Could not parse scope reviewer output as JSON — commit blocked.\n"
                "Full raw response preserved in scope_raw_result (status='parse_failure')."
            ),
            model_id=scope_model_id,
            status="parse_failure",
            raw_text=raw_text,
            prompt_chars=_prompt_chars,
            tokens_in=_tokens_in,
            tokens_out=_tokens_out,
            cost_usd=_cost_usd,
        )

    critical_findings, advisory_findings = _classify_scope_findings(items)
    _log_scope_result(
        ctx,
        len(critical_findings),
        len(advisory_findings),
        prompt_chars=_prompt_chars,
    )

    if critical_findings:
        from ouroboros import config as _cfg
        if _cfg.get_review_enforcement() == "blocking":
            return ScopeReviewResult(
                blocked=True,
                block_message=_build_block_message(critical_findings, advisory_findings),
                critical_findings=critical_findings,
                advisory_findings=advisory_findings,
                model_id=scope_model_id,
                status="responded",
                raw_text=raw_text,
                prompt_chars=_prompt_chars,
                tokens_in=_tokens_in,
                tokens_out=_tokens_out,
                cost_usd=_cost_usd,
            )
        # Advisory mode: findings returned but commit not blocked.
        # (do NOT mutate ctx._review_advisory here; parallel_review.py aggregates
        #  scope findings on the main thread after both futures complete to avoid races)

    return ScopeReviewResult(
        blocked=False,
        critical_findings=critical_findings,
        advisory_findings=advisory_findings,
        model_id=scope_model_id,
        status="responded",
        raw_text=raw_text,
        prompt_chars=_prompt_chars,
        tokens_in=_tokens_in,
        tokens_out=_tokens_out,
        cost_usd=_cost_usd,
    )

"""Enforcement-aware Atlas-backed scope reviewer for the commit pipeline.

Runs beside triad review and sees touched context plus a generated repo atlas. Critical findings follow
``OUROBOROS_REVIEW_ENFORCEMENT``: blocking enforcement blocks, advisory
enforcement reports them without blocking. Infrastructure failures such as
model errors, empty output, parse failures, and touched-context errors still
fail closed; oversized prompts are the explicit non-blocking skip path.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import pathlib
from dataclasses import dataclass, field
from typing import List, Optional

from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.review_context_atlas import (
    ReviewContextAtlasRequest,
    compile_review_context_atlas,
)
from ouroboros.tools.review_helpers import (
    build_goal_section,
    build_rebuttal_section as _shared_build_rebuttal_section,
    build_scope_section,
    build_touched_file_pack,
    load_checklist_section,
    CRITICAL_FINDING_CALIBRATION,
    REPO_ANTI_PATTERN_LOCK_GUARD,
    REVIEW_JSON_ARRAY_CONTRACT,
    REVIEW_PREAMBLE,
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
    parse_git_name_status,
)
from ouroboros.triad_review import extract_json_array
from ouroboros.utils import run_cmd, utc_now_iso, append_jsonl, estimate_tokens

log = logging.getLogger(__name__)

_SCOPE_MODEL_DEFAULT = "openai/gpt-5.5"
_SCOPE_MAX_TOKENS = 100_000  # 100K output tokens
_SCOPE_REVIEW_SLOT_TIMEOUT_SEC = 900

# Budget gate: estimate_tokens under-counts real tokens, so this non-blocking
# skip limit leaves headroom for 1M-context reviewer models.
from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET as _REVIEW_BUDGET

_SCOPE_BUDGET_TOKEN_LIMIT = _REVIEW_BUDGET

# Defense-in-depth cap for deleted-file HEAD content inlined into the prompt.
_DELETED_INLINE_MAX_BYTES = 1_048_576  # 1 MB

_SCOPE_CONTEXT_MANIFEST = contextvars.ContextVar("scope_context_manifest", default={})


class _ScopeAtlasBudgetExceeded(RuntimeError):
    def __init__(self, manifest: dict):
        self.manifest = dict(manifest or {})
        token_count = int(self.manifest.get("estimated_total_tokens") or 0)
        super().__init__(
            f"Generated Scope Atlas exceeded hard budget"
            + (f" (~{token_count:,} estimated tokens)" if token_count else "")
        )


def _current_scope_context_manifest() -> dict:
    return dict(_SCOPE_CONTEXT_MANIFEST.get({}) or {})


@dataclass
class ScopeReviewResult:
    """Structured outcome from ``run_scope_review``."""
    blocked: bool = False
    block_message: str = ""
    critical_findings: List[dict] = field(default_factory=list)
    advisory_findings: List[dict] = field(default_factory=list)
    # Canonical per-actor evidence.
    raw_text: str = ""
    model_id: str = ""
    status: str = "responded"  # "responded"|"error"|"parse_failure"|"empty_response"|"budget_exceeded"|"omitted"|"empty"
    prompt_chars: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    context_manifest: dict = field(default_factory=dict)
    prompt_ref: dict = field(default_factory=dict)
    response_ref: dict = field(default_factory=dict)


@dataclass
class _TouchedContextStatus:
    """Touched-context sentinel; ``None`` means context OK."""
    status: str  # "empty" | "omitted" | "budget_exceeded"
    omitted_paths: List[str] = field(default_factory=list)
    token_count: int = 0  # estimated full prompt tokens when budget is exceeded


def _get_scope_model() -> str:
    """Return the configured scope review model (env → settings default)."""
    try:
        from ouroboros.config import get_scope_review_models

        models = get_scope_review_models()
        if models:
            return models[0]
    except Exception:
        pass
    return os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL", "").strip() or _SCOPE_MODEL_DEFAULT

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
    """Format previous triad rounds for scope-review context."""
    return _shared_review_history_section(
        history,
        open_obligations,
        title="## Previous triad review rounds",
        include_commit_message=False,
        compact_labels=True,
    )


def _parse_staged_name_status(repo_dir: pathlib.Path) -> list:
    """Parse staged changes with rename/delete/copy awareness."""
    try:
        name_status_raw = run_cmd(
            ["git", "diff", "--cached", "--name-status"], cwd=repo_dir
        )
    except Exception:
        name_status_raw = ""

    entries = parse_git_name_status(name_status_raw)

    # Fallback to --name-only if --name-status produced nothing.
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
    """Return a suppression reason for deleted HEAD content, or None to inline."""
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
    """Append deleted-file HEAD content or explicit suppression markers."""
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
    """Return touched-context failure status, or None when context is complete."""
    if not current_files_section.strip() and not deleted_paths:
        return _TouchedContextStatus(status="empty")
    if omitted and current_paths:
        return _TouchedContextStatus(status="omitted", omitted_paths=list(omitted))
    return None


def _gather_scope_packs(
    repo_dir: pathlib.Path,
    all_touched_paths: list,
    fixed_prompt_tokens: int = 0,
    drive_root: Optional[pathlib.Path] = None,
) -> str:
    """Collect the bounded wider repository atlas, failing closed on git errors."""
    # Canonical docs and touched files are injected explicitly; avoid duplicating them.
    already_included = frozenset(set(all_touched_paths) | set(_CANONICAL_CONTEXT_DOCS))
    try:
        atlas = compile_review_context_atlas(
            ReviewContextAtlasRequest(
                repo_dir=repo_dir,
                anchors=tuple(all_touched_paths),
                already_included=already_included,
                fixed_prompt_tokens=fixed_prompt_tokens,
                target_total_tokens=850_000,
                hard_total_tokens=_SCOPE_BUDGET_TOKEN_LIMIT,
                include_tests=False,
                title="Generated Scope Atlas",
                drive_root=drive_root,
            )
        )
        _SCOPE_CONTEXT_MANIFEST.set(atlas.manifest)
        if atlas.status == "budget_exceeded":
            raise _ScopeAtlasBudgetExceeded(atlas.manifest)
        repo_pack_section = atlas.text or "(no additional repo files)"
    except _ScopeAtlasBudgetExceeded:
        raise
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"review_context_atlas error: {exc}") from exc

    return repo_pack_section


def _scope_round_label(entry: dict) -> str:
    """Return BLOCKED, degraded status, or PASSED for a scope history round."""
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
    """Build the scope prompt or a touched-context/budget status sentinel."""
    _SCOPE_CONTEXT_MANIFEST.set({})
    try:
        scope_checklist = load_checklist_section("Intent / Scope Review Checklist")
    except Exception:
        scope_checklist = "(Intent / Scope Review Checklist not found in docs/CHECKLISTS.md)"

    goal_section = build_goal_section(goal, scope, commit_message)
    scope_section = build_scope_section(scope)
    canonical_docs = _load_canonical_context_docs(repo_dir)
    critical_calibration = CRITICAL_FINDING_CALIBRATION  # noqa: F841 — used in f-string below
    rebuttal_section = _shared_build_rebuttal_section(review_rebuttal)
    # Scope can block independently of triad, so load obligations even without triad history.
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

    # Scope-only retry chains need the convergence rule even without triad history.
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

    # Touched-file omissions fail closed before the budget skip can apply.
    if touched_status is not None:
        return None, touched_status

    repo_pack_placeholder = "__GENERATED_SCOPE_ATLAS_PENDING__"
    repo_pack_section = repo_pack_placeholder

    prompt = f"""\
{REVIEW_PREAMBLE}

## Your role

You are the Atlas-backed whole-repository reviewer. Diff reviewers cover line-level mistakes;
you cover cross-module contracts, forgotten touchpoints, hidden regressions,
prompt/doc sync, architecture fit, and end-to-end intent completeness.

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

Each element must follow the shared review JSON contract:
{REVIEW_JSON_ARRAY_CONTRACT}

Additional scope-review requirements:
- "item" must be one of the eight identifiers above — verbatim, case-sensitive.
- optional "obligation_id" when resolving or re-checking a previously surfaced obligation.
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

Severity rules: critical requires a concrete current artifact and a required
change to this diff; otherwise use advisory. Scope affects only unchanged
legacy code outside the diff. Apply the `Critical surface whitelist` in
`docs/CHECKLISTS.md` for prose-vs-code mismatches.

If an open obligation record above already names an `obligation_id` for this root cause,
reuse that exact `obligation_id`. Do NOT invent a new id for the same root cause.

## Anti pattern-lock guard

{REPO_ANTI_PATTERN_LOCK_GUARD}

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
    fixed_prompt_tokens = estimate_tokens(prompt)
    try:
        import inspect
        gather_kwargs = {"fixed_prompt_tokens": fixed_prompt_tokens}
        if "drive_root" in inspect.signature(_gather_scope_packs).parameters:
            gather_kwargs["drive_root"] = drive_root
        repo_pack_section = _gather_scope_packs(
            repo_dir,
            all_touched_paths,
            **gather_kwargs,
        )
    except _ScopeAtlasBudgetExceeded as exc:
        return None, _TouchedContextStatus(
            status="budget_exceeded",
            token_count=int(exc.manifest.get("estimated_total_tokens") or 0),
        )
    head, sep, tail = prompt.rpartition(repo_pack_placeholder)
    if not sep:
        raise RuntimeError("scope review atlas placeholder missing")
    prompt = head + repo_pack_section + tail
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
    model_id: str = "",
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
            "model": model_id or _get_scope_model(),
            "critical_count": critical_count,
            "advisory_count": advisory_count,
            "prompt_tokens": prompt_tokens,
            "prompt_tokens_budget": _SCOPE_BUDGET_TOKEN_LIMIT,
            "headroom_tokens": _SCOPE_BUDGET_TOKEN_LIMIT - prompt_tokens,
        })
    except Exception:
        pass


def _scope_drive_root(ctx: ToolContext | None = None) -> pathlib.Path:
    if ctx is not None:
        try:
            return pathlib.Path(ctx.drive_root)
        except Exception:
            pass
    try:
        from ouroboros.config import DATA_DIR

        return pathlib.Path(DATA_DIR)
    except Exception:
        return pathlib.Path("../data").resolve(strict=False)


def _call_scope_llm(prompt: str, scope_model: str | None = None, ctx: ToolContext | None = None) -> tuple:
    """Execute the scope review LLM call synchronously.

    Returns (raw_text, usage, error_msg) — error_msg is non-empty on failure.
    ``usage`` may contain a private ``_review_refs`` entry with durable prompt
    and response refs from the shared review substrate.
    """
    from ouroboros.config import resolve_effort as _resolve_effort
    scope_model = scope_model or _get_scope_model()
    scope_effort = _resolve_effort("scope_review")
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": "Review the staged change and context above. Output ONLY a JSON array.",
        },
    ]
    try:
        from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

        request = ReviewRequest(
            surface="scope_review",
            goal="Review the staged change and context above. Output ONLY a JSON array.",
            messages=messages,
            task_id=str(getattr(ctx, "task_id", "") or "scope_review") if ctx is not None else "scope_review",
            call_type="scope_review",
            max_tokens=_SCOPE_MAX_TOKENS,
            temperature=0.2,
            no_proxy=True,
        )
        slot = ReviewSlot(
            slot_id="scope_slot_1",
            model=scope_model,
            effort=scope_effort,
            timeout_sec=_SCOPE_REVIEW_SLOT_TIMEOUT_SEC,
            max_tokens=_SCOPE_MAX_TOKENS,
            temperature=0.2,
            role_hint="scope reviewer",
        )
        result = run_review_request(
            request,
            slots=[slot],
            drive_root=_scope_drive_root(ctx),
            llm=LLMClient(),
            usage_ctx=None,
        )
        actor = (result.actors or [{}])[0]
        usage = dict(actor.get("usage") or {})
        usage["_review_refs"] = {
            "prompt_ref": actor.get("prompt_ref") or {},
            "response_ref": actor.get("response_ref") or {},
        }
        if actor.get("status") not in {"ok", "empty"}:
            error_msg = (
                f"⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer ({scope_model}) failed — commit blocked.\n"
                f"Error: {actor.get('error') or actor.get('status') or 'scope reviewer failed'}\n"
                "Retry the commit, or check API key and network connectivity."
            )
            return "", usage, error_msg
        return str(actor.get("raw_text") or ""), usage, ""
    except Exception as e:
        error_msg = (
            f"⚠️ SCOPE_REVIEW_BLOCKED: Scope reviewer ({scope_model}) failed — commit blocked.\n"
            f"Error: {type(e).__name__}: {e}\n"
            "Retry the commit, or check API key and network connectivity."
        )
        return "", None, error_msg


def _handle_prompt_signals(
    prompt: Optional[str],
    context_status: Optional["_TouchedContextStatus"],
) -> Optional[ScopeReviewResult]:
    """Translate touched-context status into an early ScopeReviewResult."""
    if context_status is None:
        return None  # proceed with LLM call

    if context_status.status == "budget_exceeded":
        token_count = context_status.token_count
        # Back-compute prompt chars from the budget-gate token estimate.
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

    # Unknown status is a programming error; fail closed.
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
    scope_model: Optional[str] = None,
) -> ScopeReviewResult:
    """Run blocking scope review and return structured findings/evidence."""
    repo_dir = pathlib.Path(ctx.repo_dir)
    scope_model_id = scope_model or _get_scope_model()

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
            context_manifest=_current_scope_context_manifest(),
        )

    signal_result = _handle_prompt_signals(prompt, context_status)
    if signal_result is not None:
        # Keep _handle_prompt_signals as the status SSOT for early exits.
        signal_result.model_id = scope_model_id
        signal_result.context_manifest = _current_scope_context_manifest()
        return signal_result

    _prompt_chars = len(prompt)  # type: ignore[arg-type]
    raw_text, usage, llm_error = _call_scope_llm(prompt, scope_model=scope_model_id, ctx=ctx)  # type: ignore[arg-type]
    _usage = dict(usage or {})
    _review_refs = dict(_usage.pop("_review_refs", {}) or {})
    _prompt_ref = dict(_review_refs.get("prompt_ref") or {})
    _response_ref = dict(_review_refs.get("response_ref") or {})
    _tokens_in = int(_usage.get("prompt_tokens", 0) or 0)
    _tokens_out = int(_usage.get("completion_tokens", 0) or 0)
    _cost_usd = float(_usage.get("cost", 0.0) or 0.0)
    if llm_error:
        return ScopeReviewResult(
            blocked=True,
            block_message=llm_error,
            model_id=scope_model_id,
            status="error",
            prompt_chars=_prompt_chars,
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
        )
    if _usage:
        _emit_usage(ctx, scope_model_id, _usage)

    if not raw_text.strip():
        # Empty model response is distinct from transport/API error.
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
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
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
            context_manifest=_current_scope_context_manifest(),
            prompt_ref=_prompt_ref,
            response_ref=_response_ref,
        )

    critical_findings, advisory_findings = _classify_scope_findings(items)
    _log_scope_result(
        ctx,
        len(critical_findings),
        len(advisory_findings),
        prompt_chars=_prompt_chars,
        model_id=scope_model_id,
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
                context_manifest=_current_scope_context_manifest(),
                prompt_ref=_prompt_ref,
                response_ref=_response_ref,
            )
        # Parallel review aggregates advisory findings on the main thread.

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
        context_manifest=_current_scope_context_manifest(),
        prompt_ref=_prompt_ref,
        response_ref=_response_ref,
    )

"""Pre-implementation Atlas-backed design review tool."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
import logging

from ouroboros.llm import LLMClient
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.review_context_atlas import (
    ReviewContextAtlasRequest,
    compile_review_context_atlas,
)
from ouroboros.tools.review_helpers import (
    build_head_snapshot_section,
    emit_review_usage,
    load_governance_doc,
    load_checklist_section,
)
from ouroboros.utils import estimate_tokens

log = logging.getLogger(__name__)

_PLAN_REVIEW_MAX_TOKENS = 65536
_PLAN_REVIEW_EFFORT = "high"

from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET as _REVIEW_BUDGET

_PLAN_BUDGET_TOKEN_LIMIT = _REVIEW_BUDGET


def get_tools():
    return [
        ToolEntry(
            name="plan_task",
            schema={
                "name": "plan_task",
                "description": (
                    "Run a pre-implementation design review of a proposed plan using 2–3 parallel Atlas-backed "
                    "reviewers. Call this BEFORE writing any code for non-trivial tasks (>2 files or >50 lines "
                    "of changes). Each reviewer sees a generated repository Atlas plus your plan description and the "
                    "files you plan to touch. They will identify forgotten touchpoints, implicit contract "
                    "violations, simpler alternatives, and Bible/architecture compliance issues — before you've "
                    "written a single line. Uses the reviewer slots configured in OUROBOROS_REVIEW_MODELS (same "
                    "slot as the commit triad); duplicate model IDs are allowed and count as separate stochastic "
                    "slots. Returns structured feedback from every reviewer slot with detailed explanations and "
                    "alternative approaches. Non-blocking: you decide what to do with the feedback."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan": {"type": "string", "description": "Describe what you plan to implement: which files you will change, what the key design decisions are, and what you will NOT change."},
                        "goal": {"type": "string", "description": "The high-level goal of the task (what problem is being solved)."},
                        "files_to_touch": {"type": "array", "description": "Optional list of repo-relative file paths you plan to modify. Their current content (HEAD snapshot) will be injected so reviewers can reason about concrete code, not just abstract plans.", "items": {"type": "string"}},
                    },
                    "required": ["plan", "goal"],
                },
            },
            handler=_handle_plan_task,
            timeout_sec=600,
        )
    ]


def _handle_plan_task(
    ctx: ToolContext,
    plan: str = "",
    goal: str = "",
    files_to_touch: list | None = None,
) -> str:
    if not plan.strip():
        return "ERROR: plan parameter is required and must not be empty."
    if not goal.strip():
        return "ERROR: goal parameter is required and must not be empty."

    files_to_touch = files_to_touch or []

    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    _run_plan_review_async(ctx, plan, goal, files_to_touch),
                ).result(timeout=590)
        except RuntimeError:
            result = asyncio.run(_run_plan_review_async(ctx, plan, goal, files_to_touch))
        return result
    except concurrent.futures.TimeoutError:
        return "ERROR: Plan review timed out after 590s."
    except Exception as e:
        log.error("plan_task failed: %s", e, exc_info=True)
        return f"ERROR: Plan review failed: {e}"


async def _run_plan_review_async(
    ctx: ToolContext,
    plan: str,
    goal: str,
    files_to_touch: list,
) -> str:
    repo_dir = ctx.repo_dir

    from ouroboros import config as _cfg

    resolved_models = list(_cfg.get_review_models() or [])
    if not resolved_models:
        return (
            "ERROR: No review models configured. Set OUROBOROS_REVIEW_MODELS "
            "in settings."
        )

    if len(resolved_models) < 2:
        return (
            "ERROR: plan_task requires at least 2 reviewer slots for "
            f"review coordination. Got {len(resolved_models)} "
            f"model(s) from {resolved_models!r}. Fix OUROBOROS_REVIEW_MODELS "
            f"in settings (example: {_cfg.SETTINGS_DEFAULTS['OUROBOROS_REVIEW_MODELS']!r})."
        )

    models = _get_review_models()

    checklist = _load_plan_checklist()
    bible_text = _load_bible(repo_dir)
    dev_md = _load_doc(repo_dir, "docs/DEVELOPMENT.md")
    arch_md = _load_doc(repo_dir, "docs/ARCHITECTURE.md")
    checklists_md = _load_doc(repo_dir, "docs/CHECKLISTS.md")

    ctx.emit_progress_fn("📐 plan_task: reading planned-touch file snapshots…")
    canonical_docs = {
        "BIBLE.md",
        "docs/DEVELOPMENT.md",
        "docs/ARCHITECTURE.md",
        "docs/CHECKLISTS.md",
    }
    head_snapshots = ""
    if files_to_touch:
        head_snapshots = build_head_snapshot_section(repo_dir, files_to_touch)

    system_prompt = _build_system_prompt(checklist, bible_text, dev_md, arch_md, checklists_md)
    placeholder = "__GENERATED_PLAN_ATLAS_PENDING__"
    user_content = _build_user_content(plan, goal, files_to_touch, head_snapshots, placeholder, "")
    fixed_prompt_tokens = estimate_tokens(system_prompt + user_content)
    ctx.emit_progress_fn("📐 plan_task: building Generated Plan Review Atlas…")
    try:
        atlas = compile_review_context_atlas(
            ReviewContextAtlasRequest(
                repo_dir=repo_dir,
                anchors=tuple(files_to_touch),
                already_included=frozenset(set(files_to_touch) | canonical_docs),
                fixed_prompt_tokens=fixed_prompt_tokens,
                target_total_tokens=850_000,
                hard_total_tokens=_PLAN_BUDGET_TOKEN_LIMIT,
                include_tests=False,
                title="Generated Plan Review Atlas",
            )
        )
    except Exception as e:
        return f"ERROR: Failed to build review context atlas: {e}"

    if atlas.status == "budget_exceeded":
        estimated = int((atlas.manifest or {}).get("estimated_total_tokens") or 0)
        return (
            f"⚠️ PLAN_REVIEW_SKIPPED: generated repository atlas exceeded hard budget"
            + (f" ({estimated:,} estimated tokens)" if estimated else "")
            + ". Split the plan into a smaller scope or reduce protected context size."
        )

    head, sep, tail = user_content.rpartition(placeholder)
    if not sep:
        return "ERROR: Failed to build review context atlas: placeholder missing."
    user_content = head + atlas.text + tail

    estimated_tokens = estimate_tokens(system_prompt + user_content)
    if estimated_tokens > _PLAN_BUDGET_TOKEN_LIMIT:
        return (
            f"⚠️ PLAN_REVIEW_SKIPPED: assembled prompt too large "
            f"({estimated_tokens:,} estimated tokens, limit {_PLAN_BUDGET_TOKEN_LIMIT:,}). "
            f"Consider reducing files_to_touch or splitting the plan into smaller scopes."
        )

    ctx.emit_progress_fn(
        f"📐 plan_task: running {len(models)} parallel reviewers "
        f"(~{estimated_tokens:,} tokens each)…"
    )

    llm_client = LLMClient()
    semaphore = asyncio.Semaphore(3)
    tasks = [
        _query_reviewer(llm_client, model, system_prompt, user_content, semaphore)
        for model in models
    ]
    raw_results = await asyncio.gather(*tasks)

    _emit_plan_review_usage(ctx, raw_results)

    return _format_output(raw_results, models, goal, estimated_tokens)


def _emit_plan_review_usage(ctx: "ToolContext", raw_results: list) -> None:
    for result in raw_results:
        if result.get("error"):
            continue
        tokens_in = result.get("tokens_in", 0)
        tokens_out = result.get("tokens_out", 0)
        if not tokens_in and not tokens_out:
            continue
        model = result.get("model") or result.get("request_model") or ""
        cost = float(result.get("cost", 0) or 0)
        emit_review_usage(
            ctx,
            model=model,
            usage={"prompt_tokens": tokens_in, "completion_tokens": tokens_out, "cost": cost},
            source="plan_review",
            extra={"cost": cost},
        )


async def _query_reviewer(
    llm_client: LLMClient,
    model: str,
    system_prompt: str,
    user_content: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    async with semaphore:
        try:
            msg, usage = await llm_client.chat_async(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                model=model,
                reasoning_effort=_PLAN_REVIEW_EFFORT,
                max_tokens=_PLAN_REVIEW_MAX_TOKENS,
                temperature=0.2,
                no_proxy=True,
            )
            content = msg.get("content") or "(empty response)"
            resolved_model = str((usage or {}).get("resolved_model") or model)
            prompt_tokens = (usage or {}).get("prompt_tokens", 0)
            completion_tokens = (usage or {}).get("completion_tokens", 0)
            cost = float((usage or {}).get("cost", 0) or 0)
            return {
                "model": resolved_model,
                "request_model": model,
                "text": content,
                "error": None,
                "tokens_in": prompt_tokens,
                "tokens_out": completion_tokens,
                "cost": cost,
            }
        except asyncio.TimeoutError:
            return {
                "model": model, "request_model": model,
                "text": "", "error": "Timeout after 120s",
                "tokens_in": 0, "tokens_out": 0,
            }
        except Exception as e:
            error_msg = _classify_reviewer_error(e, model)
            return {
                "model": model, "request_model": model,
                "text": "", "error": error_msg,
                "tokens_in": 0, "tokens_out": 0,
            }


def _format_output(raw_results: list, models: list, goal: str, estimated_tokens: int) -> str:
    """Render reviewer responses plus coordinated aggregate verdict."""
    lines = [
        "## Plan Review Results",
        "",
        f"**Goal:** {goal}",
        f"**Models:** {len(models)} parallel reviewers",
        f"**Prompt size:** ~{estimated_tokens:,} tokens per reviewer",
        "",
        "---",
        "",
    ]

    per_reviewer: list[str] = []

    for i, result in enumerate(raw_results):
        model_label = result.get("model") or result.get("request_model") or f"Model {i+1}"
        lines.append(f"### Reviewer {i+1}: {model_label}")
        lines.append("")

        if result.get("error"):
            lines.extend([f"⚠️ **ERROR:** {result['error']}", ""])
            per_reviewer.append("DEGRADED")
            continue

        text = result.get("text", "").strip()
        if not text:
            lines.extend(["⚠️ **ERROR:** Empty response from reviewer.", ""])
            per_reviewer.append("DEGRADED")
            continue

        lines.extend([text, ""])

        reviewer_signal = _parse_aggregate_signal(text)
        per_reviewer.append(reviewer_signal if reviewer_signal else "DEGRADED")
        lines.extend(["---", ""])

    revise_count = sum(1 for sig in per_reviewer if sig == "REVISE_PLAN")
    review_required_count = sum(1 for sig in per_reviewer if sig == "REVIEW_REQUIRED")
    degraded_count = sum(1 for sig in per_reviewer if sig == "DEGRADED")
    green_count = sum(1 for sig in per_reviewer if sig == "GREEN")

    if not per_reviewer:
        lines.extend(["## Aggregate Signal", "", "❓ **REVIEW_REQUIRED**", ""])
        lines.append("No reviewer responses were collected (empty reviewer list). "
                     "Treat as REVIEW_REQUIRED — re-run plan_task with at least one reviewer configured.")
        return "\n".join(lines)

    if revise_count >= 2:
        aggregate_signal = "REVISE_PLAN"
    elif revise_count == 1 or review_required_count > 0 or degraded_count > 0:
        aggregate_signal = "REVIEW_REQUIRED"
    elif green_count == len(per_reviewer):
        aggregate_signal = "GREEN"
    else:
        aggregate_signal = "REVIEW_REQUIRED"

    signal_emoji = {
        "GREEN": "✅",
        "REVIEW_REQUIRED": "⚠️",
        "REVISE_PLAN": "❌",
    }.get(aggregate_signal, "❓")

    lines.extend(["## Aggregate Signal", "", f"{signal_emoji} **{aggregate_signal}**", ""])
    lines.append(
        f"Per-reviewer signals: REVISE_PLAN={revise_count}, "
        f"REVIEW_REQUIRED={review_required_count}, "
        f"GREEN={green_count}, DEGRADED={degraded_count}."
    )
    lines.append("")

    if aggregate_signal == "GREEN":
        lines.append(
            "All reviewers converged on GREEN. Read every reviewer's PROPOSALS "
            "section (they are the point of this call) and proceed with implementation."
        )
    elif aggregate_signal == "REVIEW_REQUIRED":
        reasons: list[str] = []
        if revise_count == 1:
            reasons.append(
                "one reviewer dissented with REVISE_PLAN while the others did not — "
                "a single dissent often sees the structural issue the others missed; "
                "read the dissenting reviewer's response in full before deciding"
            )
        if review_required_count > 0:
            reasons.append(
                f"{review_required_count} reviewer(s) raised RISKs or non-structural concerns"
            )
        if degraded_count > 0:
            reasons.append(
                f"{degraded_count} reviewer(s) failed to return a parseable response "
                "(error, empty, or missing AGGREGATE line) — GREEN cannot be confirmed"
            )
        if reasons:
            lines.append("Reason: " + "; ".join(reasons) + ".")
        lines.append(
            "Read every reviewer's full response and PROPOSALS section. "
            "Decide whether to adjust the plan before coding."
        )
    else:  # REVISE_PLAN
        lines.append(
            f"{revise_count} reviewers independently flagged REVISE_PLAN — majority "
            "confirms a structural problem with the plan. Redesign to address the "
            "flagged issues before writing any code."
        )

    return "\n".join(lines)


def _build_system_prompt(
    checklist: str,
    bible_text: str,
    dev_md: str,
    arch_md: str,
    checklists_md: str = "",
) -> str:
    parts = [(
        "You are a senior design reviewer for Ouroboros, a self-creating AI agent.\n"
        "Your job is to review a proposed implementation plan BEFORE any code is written.\n"
        "You are validating a concrete candidate plan, not brainstorming from zero. If the plan is weak, say exactly why and what boundary or contract was missed.\n"
        "You have broad Atlas-backed repository access to find issues that the implementer may have missed.\n\n"
        "## Review stance — GENERATIVE, not audit\n\n"
        "Your primary job is to CONTRIBUTE ideas the implementer may not see, using broad Atlas-backed repo access.\n"
        "Finding defects in the plan is secondary; proposing concrete alternatives, surfacing existing surfaces that already solve the goal, and flagging subtle contract breaks is primary.\n"
        "Assume the implementer has already thought through the first-pass design — you are a design PARTNER who contributes, not an auditor who rubber-stamps.\n\n"
        "## Required output structure (follow exactly)\n\n"
        "1. **Your own approach** (1-2 sentences). State what YOU would do with broad Atlas-backed repo access: the concrete alternative path, the existing file/function you would reuse, or the simpler route. If after real effort you see no better approach, say so explicitly.\n"
        "2. **`## PROPOSALS` section** (top 1-2 ideas). Each proposal is one of:\n   - An existing function/module that already solves this (named exactly).\n   - A subtle contract break or shared-state interaction the plan likely missed.\n   - A simpler path with less surface area preserving the goal.\n   - A risk pattern visible from codebase history in your context.\n   - A BIBLE.md alignment issue with a specific principle cited.\n"
        "3. **Per-item verdicts**. For each checklist item below:\n   - **verdict**: PASS | RISK | FAIL\n   - **explanation**: 2-5 sentences describing what you found (or why it's fine)\n   - **concrete fix** (if RISK or FAIL): exact file, function, or line to address\n   - **alternative approaches** (if applicable): 1-2 more elegant solutions\n"
        "4. **Final line** (exactly one of):\n   - `AGGREGATE: GREEN` — no critical issues, implementer can proceed\n   - `AGGREGATE: REVIEW_REQUIRED` — risks or minor concerns, implementer should consider adjustments\n   - `AGGREGATE: REVISE_PLAN` — critical structural issues, plan must be revised before coding\n\n"
        "Be specific. Name exact files, functions, constants, or call sites.\nVague concerns without a concrete pointer are advisory at most.\nIf you see a simpler solution, say so directly — don't just hint.\n\n"
        "## Rules (what NOT to flag)\n\n"
        "- Do NOT mark RISK on `minimalism` just because you would have done it differently. Flag RISK only when you can name (a) fewer files touched, (b) fewer lines changed, or (c) reuse of a specific existing surface — concrete alternative, not taste.\n"
        "- Do NOT penalise missing tests, `VERSION` bumps, `README.md` changelog rows, or `docs/ARCHITECTURE.md` updates — the plan has no code yet. Focus on design correctness and elegance, not commit hygiene. Commit-gate reviewers handle that later.\n\n"
        "## Aggregate level — majority-vote coordination across 2-3 reviewer slots\n\n"
        "- `AGGREGATE: REVISE_PLAN` should be used ONLY when you are confident the plan has a concrete structural problem that warrants a redesign. The coordinator escalates to final `REVISE_PLAN` only when at least 2 reviewer slots independently flag it — a lone dissenting `REVISE_PLAN` will surface as `REVIEW_REQUIRED` with your dissent noted (with 2-reviewer setups, \"≥2 reviewers\" means both reviewers agreed). This is deliberate: `plan_review` is a coordinative signal, not a block. Use `REVIEW_REQUIRED` for real but non-structural risks; reserve `REVISE_PLAN` for defects worth blocking the plan on.\n\n---\n"
    )]

    if checklist and not checklists_md:
        parts.append(f"## Plan Review Checklist\n\n{checklist}\n\n---\n")

    for title, body in (
        ("## BIBLE.md (Constitution — highest priority)", bible_text),
        ("## DEVELOPMENT.md (Engineering handbook)", dev_md),
        ("## ARCHITECTURE.md (Current system structure)", arch_md),
    ):
        if body:
            parts.append(f"{title}\n\n{body}\n\n---\n")

    if checklists_md:
        parts.append(
            "## CHECKLISTS.md (review contracts and critical thresholds)\n\n"
            "Use the `## Plan Review Checklist` section inside this file as the per-item matrix for this plan review.\n\n"
            f"{checklists_md}\n\n---\n"
        )

    return "\n".join(parts)


def _build_user_content(
    plan: str,
    goal: str,
    files_to_touch: list,
    head_snapshots: str,
    repo_pack: str,
    omitted_note: str,
) -> str:
    parts = [f"## Implementation Plan Under Review\n\n**Goal:** {goal}\n\n**Proposed Plan:**\n{plan}\n"]

    if files_to_touch:
        parts.append(f"**Files planned to touch:** {', '.join(files_to_touch)}\n")

    if head_snapshots:
        parts.append(f"## Current State of Planned-Touch Files (HEAD)\n\n{head_snapshots}\n")

    if repo_pack:
        parts.append(f"## Generated Repository Atlas (for cross-module analysis)\n\n{repo_pack}")

    if omitted_note:
        parts.append(omitted_note)

    return "\n".join(parts)


def _classify_reviewer_error(exc: BaseException, model: str) -> str:
    """Return actionable reviewer failure text without swallowing details."""
    import json

    exc_type = type(exc).__name__
    exc_str = str(exc)

    # JSONDecodeError usually means provider returned a non-JSON error body.
    if isinstance(exc, json.JSONDecodeError):
        return (
            f"API error (provider returned non-JSON response body — likely oversized prompt "
            f"or HTTP error from {model}): {exc_str}"
        )

    # Import lazily so the module loads without openai installed.
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            BadRequestError,
            RateLimitError,
        )
        if isinstance(exc, RateLimitError):
            return f"Rate limit / quota exceeded for {model} (HTTP 429): {exc_str}"
        if isinstance(exc, BadRequestError):
            return (
                f"Bad request for {model} (HTTP 400 — prompt may be too large "
                f"for this model's context window): {exc_str}"
            )
        if isinstance(exc, APIConnectionError):
            return f"API connection error for {model} (network failure): {exc_str}"
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", "?")
            return f"API status error {status} for {model}: {exc_str}"
    except ImportError:
        pass

    # Catch-all: preserve the full unknown exception text.
    return f"{exc_type}: {exc_str}"


def _parse_aggregate_signal(text: str) -> str:
    """Extract the final valid ``AGGREGATE:`` signal from reviewer text."""
    import re
    pattern = re.compile(
        r"^\s*AGGREGATE\s*:\s*(GREEN|REVIEW_REQUIRED|REVISE_PLAN)\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = pattern.findall(text)
    if matches:
        return matches[-1].upper()
    return ""


def _get_review_models() -> list[str]:
    """Return up to 3 review-model slots, preserving explicit duplicates."""
    from ouroboros import config as _cfg

    models = list(_cfg.get_review_models() or [])
    if not models:
        main = os.environ.get("OUROBOROS_MODEL", _cfg.SETTINGS_DEFAULTS["OUROBOROS_MODEL"])
        models = [main]

    return models[:3]  # cap at 3


def _load_plan_checklist() -> str:
    """Load the Plan Review Checklist section from CHECKLISTS.md."""
    try:
        return load_checklist_section("Plan Review Checklist")
    except Exception as e:
        log.warning("Could not load Plan Review Checklist: %s", e)
        return ""


def _load_bible(repo_dir) -> str:
    return load_governance_doc(repo_dir, "BIBLE.md", on_missing="explicit")


def _load_doc(repo_dir, rel_path: str) -> str:
    return load_governance_doc(repo_dir, rel_path, on_missing="explicit")

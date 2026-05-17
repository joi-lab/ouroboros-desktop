"""Multi-model review — sends code/text to multiple LLMs for consensus review.

Also contains the unified pre-commit review gate: three models review staged
diffs against docs/CHECKLISTS.md before any repo_commit. Review always runs
before commit; enforcement is configurable between blocking and advisory.

BIBLE.md is automatically injected as constitutional context with top priority.
"""

import os
import json
import asyncio
import logging
import pathlib
from typing import Any, List, Optional

from ouroboros.llm import LLMClient
from ouroboros.utils import (
    run_cmd,
    append_jsonl,
    truncate_review_artifact,
)
from ouroboros import config as _cfg
from ouroboros.tools.registry import ToolEntry, ToolContext
from ouroboros.triad_review import extract_json_array, parse_model_review_results

log = logging.getLogger(__name__)

MAX_MODELS = 10
CONCURRENCY_LIMIT = 5
DEFAULT_REVIEW_MODEL_TIMEOUT_SEC = 600.0

_CONSTITUTIONAL_PREAMBLE = """\
## CONSTITUTIONAL CONTEXT — TOP PRIORITY

You are reviewing code for Ouroboros, a self-creating AI agent. The following
is its Constitution (BIBLE.md). **All recommendations you make MUST comply
with these principles.** If a potential improvement would violate any
principle, do NOT recommend it — flag the conflict instead.

Key principles to watch for:
- **P5 (LLM-First):** All decisions through the LLM. No hardcoded logic
  for behavior selection. If behavior can be described in a prompt, it goes
  in a prompt, not in code.
- **P7 (Minimalism):** Complexity is the enemy. Every line justifies its existence.
- **P0 (Agency):** Ouroboros is not a tool but a becoming personality.
- **P1 (Continuity):** BIBLE.md and identity.md are protected.
- **P2 (Meta-over-Patch):** Fix the class, not the instance.
- **P3 (Immune Integrity):** Review surfaces, plan review, and durable memory cannot be weakened.

If you are unsure whether a recommendation complies with the Constitution,
err on the side of NOT recommending it and explain the tension.

---

"""


def _review_model_timeout_sec() -> float:
    raw = os.environ.get("OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC", "")
    if not raw:
        return DEFAULT_REVIEW_MODEL_TIMEOUT_SEC
    try:
        value = float(raw)
    except (TypeError, ValueError):
        log.warning(
            "Invalid OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC=%r; using %.0fs",
            raw,
            DEFAULT_REVIEW_MODEL_TIMEOUT_SEC,
        )
        return DEFAULT_REVIEW_MODEL_TIMEOUT_SEC
    if value <= 0:
        log.warning(
            "Non-positive OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC=%r; using %.0fs",
            raw,
            DEFAULT_REVIEW_MODEL_TIMEOUT_SEC,
        )
        return DEFAULT_REVIEW_MODEL_TIMEOUT_SEC
    return value


def _format_timeout_seconds(timeout_sec: float) -> str:
    if float(timeout_sec).is_integer():
        return str(int(timeout_sec))
    return f"{timeout_sec:g}"


from ouroboros.tools.review_helpers import (
    REPO_ROOT as _REPO_ROOT,
    load_checklist_section as _load_checklist_section_precise,
    load_governance_doc,
    build_touched_file_pack,
    build_goal_section,
    build_rebuttal_section,
    CRITICAL_FINDING_CALIBRATION,
    normalize_reviewer_items,
    build_self_verification_template,
    build_review_history_section as _build_review_history_section,
    emit_review_usage,
    format_review_history_entry as _format_review_entry,
    single_line as _single_line,
)


# Single source of truth: ``review_helpers.REPO_ROOT`` is the canonical
# repo-root anchor (also used by ``load_checklist_section``). Keep
# ``_CHECKLISTS_PATH`` as a derived alias for the existing public-ish
# import path used by ``tests/test_git_review_pipeline.py`` (renamed
# from test_phase7_pipeline.py in v5.15.x) and the legacy re-exports —
# this removes the duplicate pathlib walk that previously lived right
# above this comment.
_CHECKLISTS_PATH = _REPO_ROOT / "docs" / "CHECKLISTS.md"


def _load_bible() -> str:
    return load_governance_doc(_REPO_ROOT, "BIBLE.md", on_missing="explicit")


# ---------------------------------------------------------------------------
# Tool: multi_model_review (agent-callable)
# ---------------------------------------------------------------------------

def get_tools():
    return [
        ToolEntry(
            name="multi_model_review",
            schema={
                "name": "multi_model_review",
                "description": (
                    "Send code or text to multiple LLM models for review/consensus. "
                    "Each model reviews independently. Returns structured verdicts. "
                    "Choose diverse models yourself. Budget is tracked automatically. "
                    "BIBLE.md (Constitution) is automatically included as top-priority context."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "The code or text to review"},
                        "prompt": {"type": "string", "description": "Review instructions — what to check for."},
                        "models": {
                            "type": "array", "items": {"type": "string"},
                            "description": "OpenRouter model identifiers (e.g. 3 diverse models)",
                        },
                    },
                    "required": ["content", "prompt", "models"],
                },
            },
            handler=_handle_multi_model_review,
        )
    ]


def _handle_multi_model_review(ctx: ToolContext, content: str = "",
                                prompt: str = "", models: list = None) -> str:
    if models is None:
        models = []
    try:
        try:
            asyncio.get_running_loop()
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    _multi_model_review_async(content, prompt, models, ctx),
                ).result()
        except RuntimeError:
            result = asyncio.run(_multi_model_review_async(content, prompt, models, ctx))
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        log.error("Multi-model review failed: %s", e, exc_info=True)
        return json.dumps({"error": f"Review failed: {e}"}, ensure_ascii=False)


async def _query_model(llm_client: LLMClient, model: str, messages: list, semaphore):
    async with semaphore:
        timeout_sec = _review_model_timeout_sec()
        try:
            msg, usage = await asyncio.wait_for(
                llm_client.chat_async(
                    messages=messages,
                    model=model,
                    reasoning_effort="medium",
                    max_tokens=65536,
                    temperature=0.2,
                    no_proxy=True,
                ),
                timeout=timeout_sec,
            )
            payload = {
                "choices": [{"message": {"content": msg.get("content") or ""}}],
                "usage": usage or {},
            }
            return model, payload, None
        except asyncio.TimeoutError:
            return model, f"Error: Timeout after {_format_timeout_seconds(timeout_sec)}s", None
        except Exception as e:
            # DEVELOPMENT.md 2(f): review-output / cognitive artifacts MUST
            # NOT use hardcoded [:N] truncation. Full error text (e.g.
            # OpenRouter 404 bodies, stack traces) is preserved via the
            # shared helper; an explicit OMISSION NOTE is appended only
            # when the payload exceeds 4 KB.
            error_msg = truncate_review_artifact(str(e), limit=4000)
            return model, f"Error: {error_msg}", None


async def _multi_model_review_async(content: str, prompt: str,
                                     models: list, ctx: ToolContext):
    if not content:
        return {"error": "content is required"}
    if not prompt:
        return {"error": "prompt is required"}
    if not models:
        return {"error": "models list is required"}
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return {"error": "models must be a list of strings"}
    if len(models) > MAX_MODELS:
        return {"error": f"Too many models ({len(models)}). Maximum is {MAX_MODELS}."}

    bible_text = _load_bible()
    if bible_text:
        system_content = (
            _CONSTITUTIONAL_PREAMBLE
            + "### BIBLE.md (Full Text)\n\n" + bible_text
            + "\n\n---\n\n## REVIEW INSTRUCTIONS\n\n" + prompt
        )
    else:
        log.warning("Proceeding without BIBLE.md — constitutional compliance cannot be guaranteed")
        system_content = (
            _CONSTITUTIONAL_PREAMBLE
            + "(BIBLE.md could not be loaded)\n\n## REVIEW INSTRUCTIONS\n\n" + prompt
        )

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": content},
    ]

    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    llm_client = LLMClient()
    tasks = [_query_model(llm_client, m, messages, semaphore) for m in models]
    results = await asyncio.gather(*tasks)

    review_results = []
    for model, result, headers_dict in results:
        review_result = _parse_model_response(model, result, headers_dict)
        emit_review_usage(
            ctx,
            model=review_result.get("model", ""),
            provider=review_result.get("provider", "openrouter"),
            usage={
                "prompt_tokens": review_result.get("tokens_in", 0),
                "completion_tokens": review_result.get("tokens_out", 0),
                "cached_tokens": review_result.get("cached_tokens", 0),
                "cache_write_tokens": review_result.get("cache_write_tokens", 0),
                "cost": review_result.get("cost_estimate", 0.0),
            },
            source="review",
        )
        review_results.append(review_result)

    return {
        "model_count": len(models),
        "constitutional_context": bool(bible_text),
        "results": review_results,
    }


def _parse_model_response(model: str, result, headers_dict) -> dict:
    usage = result.get("usage", {}) if isinstance(result, dict) else {}
    resolved_model = str(usage.get("resolved_model") or model)
    provider = str(usage.get("provider") or "openrouter")
    if isinstance(result, str):
        return {
            "model": resolved_model, "request_model": model,
            "provider": provider, "verdict": "ERROR", "text": result,
            "tokens_in": 0, "tokens_out": 0, "cost_estimate": 0.0,
        }
    try:
        choices = result.get("choices", [])
        if not choices:
            # Preserve full response body (DEVELOPMENT.md 2(f)) — no bare [:200].
            text = (
                "(no choices in response: "
                f"{truncate_review_artifact(json.dumps(result), limit=4000)})"
            )
            verdict = "ERROR"
        else:
            text = choices[0]["message"]["content"]
            verdict = "UNKNOWN"
            for line in text.split("\n")[:3]:
                line_upper = line.upper()
                if "PASS" in line_upper:
                    verdict = "PASS"
                    break
                elif "CONCERNS" in line_upper:
                    verdict = "CONCERNS"
                    break
                elif "FAIL" in line_upper:
                    verdict = "FAIL"
                    break
    except (KeyError, IndexError, TypeError):
        # Preserve full response body (DEVELOPMENT.md 2(f)) — no bare [:200].
        text = (
            "(unexpected response format: "
            f"{truncate_review_artifact(json.dumps(result), limit=4000)})"
        )
        verdict = "ERROR"

    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cached_tokens = usage.get("cached_tokens", 0)
    cache_write_tokens = usage.get("cache_write_tokens", 0)

    cost = 0.0
    try:
        if "cost" in usage:
            cost = float(usage["cost"])
        elif "total_cost" in usage:
            cost = float(usage["total_cost"])
        elif headers_dict:
            for key, value in headers_dict.items():
                if key.lower() == "x-openrouter-cost":
                    cost = float(value)
                    break
    except (ValueError, TypeError, KeyError):
        pass

    return {
        "model": resolved_model, "request_model": model,
        "provider": provider, "verdict": verdict, "text": text,
        "tokens_in": prompt_tokens, "tokens_out": completion_tokens,
        "cached_tokens": cached_tokens, "cache_write_tokens": cache_write_tokens,
        "cost_estimate": cost,
    }


# ---------------------------------------------------------------------------
# Unified pre-commit review gate — used by git.py commit tools
# ---------------------------------------------------------------------------

def _load_checklist_section() -> str:
    """Load the Repo Commit Checklist from docs/CHECKLISTS.md (DRY, Bible P7).

    Raises FileNotFoundError or ValueError if missing or malformed — fail-closed.
    Uses the precise section loader from review_helpers.
    """
    try:
        return _load_checklist_section_precise("Repo Commit Checklist")
    except FileNotFoundError:
        raise
    except ValueError:
        raise
    except Exception as e:
        raise FileNotFoundError(
            f"docs/CHECKLISTS.md not found or malformed: {e}"
        ) from e


_REVIEW_PREAMBLE = (
    "You are a pre-commit reviewer for Ouroboros, a self-modifying AI agent.\n"
    "Its Constitution is BIBLE.md. Its engineering handbook is DEVELOPMENT.md.\n"
)

_REVIEW_PROMPT_TEMPLATE = """\
{preamble}

## Review instructions — READ CAREFULLY

- Read the ENTIRE staged diff carefully, line by line. Do NOT skim.
- Use BOTH the staged diff AND the full current text of every changed file provided below.
  Do NOT review from the diff alone — the full file context is essential for correctness.
- Look for ALL bugs, logic errors, off-by-one mistakes, missing error handling,
  race conditions, resource leaks, and regressions.
- Report ALL problems you find — not just the single most critical one.
  If there are 5 bugs, list all 5.
- Do NOT stop after finding the first issue.
  Do NOT summarize multiple distinct problems into one finding.
  Each distinct problem gets its own entry in the output array.
- PASS reasons may be brief (one sentence). FAIL reasons must be detailed and actionable:
  include the file, the line or symbol, what is wrong, and a concrete suggestion for how to fix it.
- For every FAIL, include a concrete how-to-fix suggestion so the developer knows exactly
  what change is needed.

{critical_calibration}

You must produce a JSON array. Each element has:
- "item"
- optional "obligation_id" when you are resolving or re-checking a previously surfaced obligation
- "verdict": "PASS" or "FAIL"
- "severity": "critical" or "advisory"
- "reason": for FAIL — specific file/line, what is wrong, how to fix it

If an open obligation record above already names an `obligation_id` for this root cause,
reuse that exact `obligation_id`. Do NOT invent a new id when the same root cause persists.

## Anti pattern-lock guard

If your first reading surfaces **exactly one FAIL** across all checklist
items, do a deliberate SECOND pass focused on a DIFFERENT concern class
before returning. Real diffs with exactly one issue are rarer than diffs
with several issues on different dimensions; single-FAIL outputs are the
most common pattern-lock failure mode of single-pass review. For example:
if your FAIL is `code_quality`, re-examine `tests_affected` and
`self_consistency`; if `cross_platform`, re-examine `security_issues` and
`architecture_doc`; if `version_bump`, re-examine `changelog_and_badge`
and `self_consistency`. Update PASS entries in-place if your second pass
uncovers new FAILs — return only one JSON array, not two.

{checklist_section}

- Output ONLY a valid JSON array.  No markdown fences, no text outside the JSON.

{goal_section}

## DEVELOPMENT.md

{dev_guide_text}

## ARCHITECTURE.md

{architecture_section}

## Current touched files (full content)

{current_files_section}

## Staged diff

{diff_text}

## Changed files

{changed_files}

{rebuttal_section}{review_history_section}
"""


def _parse_review_json(raw: str) -> Optional[list]:
    """Best-effort extraction of a JSON array from model output."""
    return extract_json_array(raw, normalize=True)


def _git_show_staged(repo_dir, path: str) -> str:
    """Return the staged (index) content of *path* via `git show :PATH`.

    Returns empty string on any error (file not staged, git unavailable, etc.).
    """
    import subprocess
    try:
        result = subprocess.run(
            ["git", "show", f":{path}"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def _preflight_check(commit_message: str, staged_files: str,
                     repo_dir) -> Optional[str]:
    """Deterministic pre-review sanity check — catches common mismatches
    before calling expensive LLM reviewers.

    Checks (in order):
      1. VERSION staged but README.md not staged
      2. Commit message references a version but VERSION file not staged
      3. Python code in ouroboros/ or supervisor/ changed but no tests/ files staged
      4. New files added in ouroboros/ or supervisor/ but ARCHITECTURE.md not staged
      5. VERSION staged: all version carriers (pyproject.toml, README badge,
         ARCHITECTURE.md header) in the staged index must match VERSION value
      6. VERSION staged: staged README.md changelog must have a row for the new version
      7. VERSION staged: staged README.md Version History must not exceed BIBLE.md P9 limits (2 major / 5 minor / 5 patch rows) — delegates to check_history_limit() from release_sync.py
      8. conftest.py staged: block if it contains test_ functions (should be in test_*.py)
    """
    import re

    # Parse the staged_files string. We accept two deterministic formats:
    #
    # 1. "name-status"-style (produced by _run_unified_review after conversion):
    #    "A  path/to/file.py"  (status char + 2 spaces + path)
    #    "M  path/to/file.py"
    #
    # 2. Plain filename (produced as fallback or from unit-test callers):
    #    "path/to/file.py"
    #
    # We detect format 1 by checking that:
    #   - The line is at least 4 chars
    #   - Character at index 0 is a letter (git status char: A/M/D/R/C/T/?)
    #   - Characters at index 1 and 2 are spaces ("  ")
    # This avoids the filename-with-space ambiguity of the old raw[2]==' ' check.
    import string as _string
    raw_lines = staged_files.strip().splitlines()
    file_status: list[tuple[str, str]] = []  # (status_char, filepath)
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        # Format 1: "X  path" — status char + exactly two spaces
        if (len(raw) >= 4
                and raw[0] in _string.ascii_uppercase
                and raw[1:3] == "  "):
            status = raw[0].upper()
            path = raw[3:].strip()
            # Handle renames: "R  old -> new"
            if " -> " in path:
                path = path.split(" -> ")[-1].strip()
            file_status.append((status, path))
        else:
            # Format 2: plain filename — treat as modified
            file_status.append(("M", raw))

    # staged_set: all paths that appear in the diff (used for existence/coupling checks).
    # active_staged: exclude Deleted (D) entries — a deleted file cannot satisfy a
    # "companion file must be present" requirement.
    staged_set = {path for _, path in file_status}
    active_staged = {path for status, path in file_status if status != "D"}
    # Treat both Added (A) and Copied (C) as "new" files for preflight check 4.
    # Renamed (R) files are not new-module additions — the old path disappears.
    new_files = {path for status, path in file_status if status in ("A", "C")}
    msg_lower = commit_message.lower()

    has_version_ref = bool(re.search(r'v?\d+\.\d+\.\d+', commit_message)) or "version" in msg_lower
    # Use active_staged (excludes deleted files) for companion-file presence checks.
    # staged_set includes all paths (for "currently staged" display and couping checks).
    version_staged = "VERSION" in active_staged

    missing = []

    # Check 1: VERSION staged (and not deleted) but README missing
    if version_staged and "README.md" not in active_staged:
        missing.append("README.md (badge + changelog)")

    # Check 2: Version reference in message but VERSION not staged
    if has_version_ref and not version_staged:
        if any(f.endswith(('.py', '.md')) and f != 'VERSION' for f in active_staged):
            missing.append("VERSION")

    if missing:
        return (
            f"⚠️ PREFLIGHT_BLOCKED: Staged diff is incomplete — fix before review.\n"
            f"  Missing from staged: {', '.join(missing)}\n"
            f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}\n\n"
            "Stage all related files together. Use repo_write for all files first,\n"
            "then repo_commit to stage and commit everything in one diff."
        )

    # Check 3: Python logic touched (added, modified, or deleted) in ouroboros/ or
    # supervisor/ but no tests/ files are staged (active, non-deleted).
    # We include deleted .py files because deleting a module is a behaviour change
    # that must be reflected in tests (e.g. removing a call site or deleting
    # a test that covered the deleted module).
    _LOGIC_DIRS = ("ouroboros/", "supervisor/")
    logic_changed = any(
        f.startswith(_LOGIC_DIRS) and f.endswith(".py")
        for f in staged_set  # all statuses including D
    )
    tests_staged = any(f.startswith("tests/") for f in active_staged)
    if logic_changed and not tests_staged:
        return (
            "⚠️ PREFLIGHT_BLOCKED: Python logic changed in ouroboros/ or supervisor/ "
            "but no tests/ files are staged.\n"
            "  Add or update tests to cover the changed behaviour, then re-stage.\n"
            "  If this is a docs/config-only change that triggered a false positive, "
            "check that no .py files from ouroboros/ or supervisor/ are in your staged set.\n"
            f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}"
        )

    # Check 4: New files added/copied in ouroboros/ or supervisor/ but
    # ARCHITECTURE.md is not in active_staged (must not be deleted).
    new_logic_files = [
        f for f in new_files
        if f.startswith(_LOGIC_DIRS) and f.endswith(".py")
    ]
    if new_logic_files and "docs/ARCHITECTURE.md" not in active_staged:
        return (
            "⚠️ PREFLIGHT_BLOCKED: New files added in ouroboros/ or supervisor/ "
            "but docs/ARCHITECTURE.md is not staged.\n"
            "  New structural additions must be documented in ARCHITECTURE.md "
            "(Bible P6: authenticity / architectural mirror).\n"
            f"  New files: {new_logic_files[:5]}\n"
            f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}"
        )

    # Check 5: If VERSION is staged (non-deleted), verify that pyproject.toml,
    # README.md badge, and ARCHITECTURE.md header in the staged index all carry
    # the same version string. Uses `git show :PATH` to read staged content
    # rather than the worktree, so partially staged changes are handled correctly.
    if version_staged:
        try:
            from ouroboros.tools.release_sync import (
                is_release_version,
                version_carrier_desyncs,
            )
            version_str = _git_show_staged(repo_dir, "VERSION").strip()
            if is_release_version(version_str):
                pyproject_text = _git_show_staged(repo_dir, "pyproject.toml")
                readme_text = _git_show_staged(repo_dir, "README.md")
                arch_text = _git_show_staged(repo_dir, "docs/ARCHITECTURE.md")
                desync = version_carrier_desyncs(
                    version_str,
                    pyproject_text=pyproject_text,
                    readme_text=readme_text,
                    arch_text=arch_text,
                    detailed=True,
                )
                if desync:
                    return (
                        f"⚠️ PREFLIGHT_BLOCKED: VERSION file says {version_str} but "
                        "the following staged files have a different version value:\n"
                        + "".join(f"  - {d}\n" for d in desync)
                        + "Update all version references to match VERSION before committing.\n"
                        f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}"
                    )
        except Exception:
            pass  # Non-fatal: LLM reviewers handle version sync

    # Check 6: If VERSION is staged, verify the staged README.md changelog
    # contains a row for the new version (structural presence check only).
    if version_staged:
        try:
            from ouroboros.tools.release_sync import is_release_version
            version_str = _git_show_staged(repo_dir, "VERSION").strip()
            if is_release_version(version_str):
                readme_text = _git_show_staged(repo_dir, "README.md")
                if readme_text and not re.search(r'\|\s*' + re.escape(version_str) + r'\s*\|', readme_text):
                    return (
                        f"⚠️ PREFLIGHT_BLOCKED: VERSION is {version_str} but README.md "
                        "changelog has no table row for this version.\n"
                        "  Add a changelog entry in the Version History table in README.md.\n"
                        f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}"
                    )
        except Exception:
            pass  # Non-fatal

    # Check 7: If VERSION is staged, verify README.md Version History does not
    # exceed the P9 limits (2 major / 5 minor / 5 patch rows). Reads from the
    # staged index via git show so partially staged changes are handled correctly.
    # Uses check_history_limit() from release_sync.py (the single source of truth
    # for P9 limits). This is a deterministic fast check — no LLM call needed.
    if version_staged:
        try:
            readme_staged = _git_show_staged(repo_dir, "README.md")
            if readme_staged:
                from ouroboros.tools.release_sync import check_history_limit
                limit_warnings = check_history_limit(readme_staged)
                if limit_warnings:
                    return (
                        "⚠️ PREFLIGHT_BLOCKED: README.md Version History exceeds BIBLE.md P9 limits.\n"
                        + "".join(f"  - {w}\n" for w in limit_warnings)
                        + "  Trim the oldest entry in the over-limit category before committing.\n"
                        + "  Quick check: python -c \"from ouroboros.tools.release_sync import "
                        "check_history_limit; print(check_history_limit(open('README.md').read()))\"\n"
                        + f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}"
                    )
        except Exception:
            pass  # Non-fatal: LLM reviewers handle P9 limits as advisory fallback

    # Check 8: if any conftest.py in active_staged contains collectable test functions,
    # block with an explicit message to move them to test_*.py files.
    # Reads staged content via git show to validate what will actually be committed.
    conftest_files = [f for f in active_staged if pathlib.Path(f).name == "conftest.py"]
    if conftest_files:
        import ast as _ast
        for cf in conftest_files:
            try:
                cf_text = _git_show_staged(repo_dir, cf)
                if not cf_text:
                    continue
                tree = _ast.parse(cf_text, filename=cf)
                # Only scan module-level functions — nested helpers inside fixtures
                # are not collected by pytest and must not trigger this check.
                test_fns = [
                    node.name for node in tree.body
                    if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                    and node.name.startswith("test_")
                ]
                if test_fns:
                    shown = test_fns[:5]
                    omission = f" (⚠️ showing first 5 of {len(test_fns)})" if len(test_fns) > 5 else ""
                    return (
                        f"⚠️ PREFLIGHT_BLOCKED: {cf} contains test functions: "
                        f"{shown}{omission}.\n"
                        "  conftest.py is for fixtures/hooks only. Move test_ functions "
                        "to a test_*.py file so pytest can discover them properly.\n"
                        f"  Currently staged: {', '.join(sorted(staged_set)) or '(none)'}"
                    )
            except Exception:
                pass  # Non-fatal: AST parse failure or git error, skip this file

    return None


def _review_entry(
    *,
    severity: str,
    item: str,
    reason: str,
    model: str = "",
    tag: str = "triad",
    verdict: str = "FAIL",
    obligation_id: str = "",
) -> dict:
    entry = {
        "severity": severity,
        "item": item,
        "reason": reason,
        "tag": tag,
        "verdict": verdict,
    }
    if model:
        entry["model"] = model
    if obligation_id:
        entry["obligation_id"] = obligation_id
    return entry


def _append_review_warning(ctx: ToolContext, text: Any) -> None:
    if isinstance(text, dict):
        ctx._review_advisory.append(text)
        return
    warning = _single_line(str(text))
    if warning:
        ctx._review_advisory.append(warning)


def _handle_review_block_or_warning(
    ctx: ToolContext,
    blocking_review: bool,
    blocked_msg: str,
    advisory_prefix: str,
) -> Optional[str]:
    """Either block immediately or downgrade to advisory warning."""
    if blocking_review:
        return blocked_msg
    _append_review_warning(ctx, advisory_prefix + blocked_msg)
    ctx._review_iteration_count = 0
    ctx._review_history = []
    return None


def _load_dev_guide_text(repo_dir: pathlib.Path) -> str:
    """Load ``docs/DEVELOPMENT.md`` for the triad reviewer preamble.

    Routes through ``review_helpers.load_governance_doc`` so a missing
    file produces an explicit ``[⚠️ OMISSION: ...]`` marker instead of an
    invisible empty string. DEVELOPMENT.md "Core Governance Artifacts"
    forbids silent omission of the core docs.
    """
    return load_governance_doc(repo_dir, "docs/DEVELOPMENT.md", on_missing="explicit")


def _load_architecture_text(repo_dir: pathlib.Path) -> str:
    """Load ``docs/ARCHITECTURE.md`` in full — core cognitive artifact, must not be omitted."""
    return load_governance_doc(repo_dir, "docs/ARCHITECTURE.md", on_missing="explicit")


def _collect_review_findings(ctx: ToolContext, model_results: list) -> tuple[list[str], list[str], list[str], list[dict]]:
    parsed = parse_model_review_results({"results": model_results})
    critical_fails: List[str] = []
    advisory_warns: List[str] = []
    structured_critical: List[dict] = []
    structured_advisory: List[dict] = []
    triad_raw_results = [record.to_dict() for record in parsed.actor_records]
    errored_models = [record.model_id for record in parsed.actor_records if record.status == "error"]

    for record in parsed.actor_records:
        if record.status == "error":
            advisory_warns.append(
                f"[{record.model_id}] Model unavailable this round (transport error). "
                "Full raw response preserved in triad_raw_results (status='error')."
            )
            structured_advisory.append(_review_entry(
                severity="advisory",
                item="review_model_unavailable",
                reason=(
                    f"Model unavailable this round (transport error): {record.model_id}. "
                    "Full raw response preserved in triad_raw_results actor record."
                ),
                model=record.model_id,
            ))
            try:
                append_jsonl(ctx.drive_logs() / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "review_model_error",
                    "model": record.model_id,
                    "error_note": "Full raw response preserved in triad_raw_results.",
                })
            except Exception:
                pass
            continue
        if record.status == "parse_failure":
            advisory_warns.append(
                f"[{record.model_id}] Could not parse structured review output (parse_failure). "
                "Full raw response preserved in triad_raw_results (status='parse_failure')."
            )
            structured_advisory.append(_review_entry(
                severity="advisory",
                item="review_model_parse_failure",
                reason=(
                    f"Could not parse structured review output from {record.model_id}. "
                    "Full raw response preserved in triad_raw_results actor record."
                ),
                model=record.model_id,
            ))
            continue
        for item in record.parsed_items:
            if str(item.get("verdict", "")).upper() != "FAIL":
                continue
            desc = f"[{record.model_id}] {item.get('item', '?')}: {item.get('reason', '')}"
            target = structured_critical if item.get("severity") == "critical" else structured_advisory
            target.append(_review_entry(
                severity="critical" if target is structured_critical else "advisory",
                item=str(item.get("item", "?")),
                reason=str(item.get("reason", "")),
                model=record.model_id,
                obligation_id=str(item.get("obligation_id", "") or ""),
            ))
            (critical_fails if target is structured_critical else advisory_warns).append(desc)

    ctx._last_review_critical_findings = structured_critical
    ctx._last_review_advisory_findings = structured_advisory
    ctx._last_triad_raw_results = triad_raw_results
    if parsed.degraded_reasons:
        if not hasattr(ctx, "_review_degraded_reasons"):
            ctx._review_degraded_reasons = []
        ctx._review_degraded_reasons.extend(parsed.degraded_reasons)
    return critical_fails, advisory_warns, errored_models, triad_raw_results


def _build_critical_block_message(
    ctx: ToolContext,
    commit_message: str,
    critical_fails: List[str],
    advisory_warns: List[str],
    errored_note: str,
) -> str:
    critical_entries = list(getattr(ctx, "_last_review_critical_findings", []) or critical_fails)
    advisory_entries = list(getattr(ctx, "_last_review_advisory_findings", []) or advisory_warns)
    ctx._review_history.append({
        "attempt": ctx._review_iteration_count,
        "commit_message": commit_message,  # full — no [:200] truncation
        "critical": critical_entries,
        "advisory": advisory_entries,
    })

    iteration_note = f" (attempt {ctx._review_iteration_count})"

    self_verify_findings = list(getattr(ctx, '_last_review_critical_findings', []) or []) or list(critical_fails)
    retry_coaching = build_self_verification_template(
        self_verify_findings,
        attempt_idx=ctx._review_iteration_count,
        tool_name="repo_commit",
        context_noun="diff",
    )

    return (
        f"⚠️ REVIEW_BLOCKED{iteration_note}: Critical issues found by reviewers.\n"
        "Commit has NOT been created. Fix the issues and try again. Use review_rebuttal\n"
        "ONLY if a finding is factually incorrect — not to argue against requested tests\n"
        "or artifacts. If the same finding repeats after a rebuttal, implement the fix\n"
        "instead of re-arguing.\n\n"
        + "Critical findings:\n"
        + "\n".join(f"  - {_format_review_entry(f, default_severity='critical')}" for f in critical_entries)
        + (
            "\n\nAdvisory warnings:\n"
            + "\n".join(f"  - {_format_review_entry(w)}" for w in advisory_entries)
            if advisory_entries else ""
        )
        + errored_note
        + retry_coaching
    )


def _build_preflight_staged(target_repo: str, fallback: str = "") -> str:
    """Convert git --name-status output to a two-char porcelain-like prefix format.

    Needed so _preflight_check can detect added/deleted/renamed files with the
    correct status letter. Falls back to the name-only list on any error.
    """
    try:
        name_status = run_cmd(
            ["git", "diff", "--cached", "--name-status"], cwd=target_repo
        )
        preflight_input_lines = []
        for line in name_status.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if not parts:
                continue
            status_char = parts[0][0].upper()  # strips similarity % from R100/C100
            if status_char in ("R", "C") and len(parts) >= 3:
                src_path, dst_path = parts[1], parts[-1]
                if status_char == "R":
                    preflight_input_lines.append(f"D  {src_path}")
                    preflight_input_lines.append(f"A  {dst_path}")
                else:
                    # Copy: source unchanged, only destination counts as new
                    preflight_input_lines.append(f"A  {dst_path}")
            elif len(parts) >= 2:
                preflight_input_lines.append(f"{status_char}  {parts[1]}")
            else:
                preflight_input_lines.append(f"M  {parts[0]}")
        return "\n".join(preflight_input_lines) if preflight_input_lines else fallback
    except Exception:
        return fallback  # check 4 may not fire, but checks 1-3 still work


def _run_unified_review(ctx: ToolContext, commit_message: str,
                        review_rebuttal: str = "",
                        repo_dir=None,
                        goal: str = "",
                        scope: str = "") -> Optional[str]:
    """Unified pre-commit review: 3 models, structured JSON, consistent severity.

    Returns None if commit may proceed. In blocking mode returns a blocking
    error string when review rejects the commit.
    """
    target_repo = repo_dir or ctx.repo_dir
    ctx._review_iteration_count += 1
    ctx._last_review_block_reason = ""  # reset per attempt
    ctx._last_triad_models = []  # reset forensic field so stale values never persist on early exit
    ctx._last_review_critical_findings = []  # reset to avoid stale findings from previous attempts
    ctx._last_triad_raw_results = []  # reset per-model actor records
    ctx._review_degraded_reasons = []  # reset degraded participation markers
    review_enforcement = _cfg.get_review_enforcement()
    blocking_review = review_enforcement == "blocking"

    try:
        diff_text = run_cmd(["git", "diff", "--cached"], cwd=target_repo)
    except Exception:
        diff_text = "(failed to get staged diff)"

    if not diff_text.strip():
        return None

    try:
        changed = run_cmd(["git", "diff", "--cached", "--name-only"], cwd=target_repo)
    except Exception:
        changed = ""

    preflight_staged = _build_preflight_staged(target_repo, fallback=changed)

    preflight_err = _preflight_check(commit_message, preflight_staged, target_repo)
    if preflight_err:
        ctx._last_review_block_reason = "preflight"
        result = _handle_review_block_or_warning(
            ctx, blocking_review, preflight_err,
            "Review enforcement=Advisory: preflight warning did not block commit. ",
        )
        if result is not None:
            return result

    rebuttal_section = build_rebuttal_section(review_rebuttal)

    try:
        checklist_section = _load_checklist_section()
    except (FileNotFoundError, ValueError) as e:
        log.error("Checklist loading failed (fail-closed): %s", e)
        ctx._last_review_block_reason = "infra_failure"
        blocked_msg = (
            "⚠️ REVIEW_BLOCKED: Cannot load review checklist — commit cannot proceed.\n"
            f"Error: {e}\n"
            "Ensure docs/CHECKLISTS.md exists and contains the expected section headers."
        )
        return _handle_review_block_or_warning(
            ctx, blocking_review, blocked_msg,
            "Review enforcement=Advisory: review checklist failed to load; commit proceeding anyway. ",
        )

    dev_guide_text = _load_dev_guide_text(pathlib.Path(ctx.repo_dir))
    architecture_text = _load_architecture_text(pathlib.Path(ctx.repo_dir))

    # Load open obligations to inject into reviewer history (anti-thrashing).
    # Gate on durable state (not volatile in-memory counter) so obligations
    # survive process restarts and are injected whenever they exist.
    _open_obs_for_review = []
    try:
        from ouroboros.review_state import load_state, make_repo_key
        _rs = load_state(pathlib.Path(ctx.drive_root))
        _repo_key = make_repo_key(pathlib.Path(ctx.repo_dir))
        _open_obs_for_review = _rs.get_open_obligations(repo_key=_repo_key)
    except Exception:
        pass  # Non-fatal: anti-thrashing hint is best-effort
    review_history_section = _build_review_history_section(
        ctx._review_history, open_obligations=_open_obs_for_review,
    )

    # Build touched-file pack for full current file context
    try:
        touched_paths = [f.strip() for f in changed.strip().splitlines() if f.strip()]
        current_files_section, _omitted = build_touched_file_pack(
            pathlib.Path(target_repo), touched_paths
        )
        if _omitted:
            current_files_section += (
                f"\n\n⚠️ OMISSION NOTE: {len(_omitted)} file(s) omitted from direct context: "
                f"{', '.join(_omitted)}"
            )
        if not current_files_section.strip():
            current_files_section = "(no touched files could be read)"
    except Exception as e:
        log.warning("Failed to build touched file pack for triad review: %s", e)
        current_files_section = f"(touched file pack unavailable: {e})"

    goal_section = build_goal_section(goal, scope, commit_message)

    prompt = _REVIEW_PROMPT_TEMPLATE.format(
        preamble=_REVIEW_PREAMBLE,
        critical_calibration=CRITICAL_FINDING_CALIBRATION,
        checklist_section=checklist_section,
        goal_section=goal_section,
        dev_guide_text=dev_guide_text or "(DEVELOPMENT.md not found)",
        architecture_section=architecture_text or "(ARCHITECTURE.md not found)",
        current_files_section=current_files_section,
        rebuttal_section=rebuttal_section,
        review_history_section=review_history_section,
        diff_text=diff_text,
        changed_files=changed,
    )

    models = _cfg.get_review_models()
    ctx._last_triad_models = list(models)  # forensic: actual resolved model IDs

    try:
        result_json = _handle_multi_model_review(
            ctx,
            content="Review the staged diff and context provided in the instructions above.",
            prompt=prompt,
            models=models,
        )
        result = json.loads(result_json)
    except Exception as e:
        log.error("Unified review infrastructure failure: %s", e)
        ctx._last_review_block_reason = "infra_failure"
        blocked_msg = (
            "⚠️ REVIEW_BLOCKED: Review infrastructure failed — commit cannot proceed "
            "without a successful review.\n"
            f"Error: {e}\n"
            "Check OPENROUTER_API_KEY, network connectivity, and retry."
        )
        return _handle_review_block_or_warning(
            ctx, blocking_review, blocked_msg,
            "Review enforcement=Advisory: review infrastructure failure did not block commit. ",
        )

    if "error" in result:
        log.error("Review returned error: %s", result["error"])
        ctx._last_review_block_reason = "infra_failure"
        blocked_msg = (
            "⚠️ REVIEW_BLOCKED: Review service returned an error — commit cannot proceed "
            "without a successful review.\n"
            f"Error: {result['error']}\n"
            "Check OPENROUTER_API_KEY, network connectivity, and retry."
        )
        return _handle_review_block_or_warning(
            ctx, blocking_review, blocked_msg,
            "Review enforcement=Advisory: review service error did not block commit. ",
        )

    model_results = result.get("results", [])
    if not model_results:
        ctx._last_review_block_reason = "infra_failure"
        blocked_msg = (
            "⚠️ REVIEW_BLOCKED: Review returned no results from any model — "
            "commit cannot proceed without a successful review."
        )
        return _handle_review_block_or_warning(
            ctx, blocking_review, blocked_msg,
            "Review enforcement=Advisory: review returned no model results; commit proceeding anyway. ",
        )

    critical_fails, advisory_warns, errored_models, _triad_raw = _collect_review_findings(ctx, model_results)
    # _triad_raw already stored on ctx._last_triad_raw_results inside _collect_review_findings

    models_total = len(model_results)

    # Quorum: at least 2 of N reviewers must produce parseable structured output.
    # Count only status=="responded" actors — parse_failure and error both represent
    # unusable evidence and must NOT count toward quorum.
    triad_raw = getattr(ctx, "_last_triad_raw_results", []) or []
    successful_reviewers = sum(1 for r in triad_raw if r.get("status") == "responded")
    # Build the non-successful list for display (transport errors + parse failures)
    failed_actors = [
        r["model_id"] for r in triad_raw if r.get("status") != "responded"
    ]
    if successful_reviewers < 2:
        ctx._last_review_block_reason = "review_quorum"
        unavailable_str = ", ".join(failed_actors) if failed_actors else ", ".join(errored_models)
        blocked_msg = (
            f"⚠️ REVIEW_BLOCKED: Only {successful_reviewers} of {models_total} review "
            f"models responded successfully (minimum 2 required). "
            f"Unavailable/failed: {unavailable_str}.\n"
            "Retry the commit — transient model failures usually resolve quickly."
        )
        return _handle_review_block_or_warning(
            ctx, blocking_review, blocked_msg,
            "Review enforcement=Advisory: review quorum failure did not block commit. ",
        )

    errored_note = ""
    all_non_responded = failed_actors or errored_models
    if all_non_responded:
        errored_note = (
            f"\n\nNote: {len(all_non_responded)} of {models_total} review models "
            f"were unavailable or failed to parse ({', '.join(all_non_responded)}). "
            "Target is 3 working reviewers."
        )

    if critical_fails:
        # Classify: if all critical failures are parse issues, mark as parse_failure
        all_parse = all("Could not parse" in f for f in critical_fails)
        ctx._last_review_block_reason = "parse_failure" if all_parse else "critical_findings"
        if blocking_review:
            return _build_critical_block_message(
                ctx, commit_message, critical_fails, advisory_warns, errored_note,
            )

        _append_review_warning(
            ctx,
            "Review enforcement=Advisory: critical review findings did not block commit.",
        )
        for finding in getattr(ctx, "_last_review_critical_findings", []) or []:
            _append_review_warning(ctx, finding)
        for warning in getattr(ctx, "_last_review_advisory_findings", []) or []:
            _append_review_warning(ctx, warning)
        if errored_note:
            _append_review_warning(ctx, errored_note)

    # All clear — reset iteration state
    ctx._review_iteration_count = 0
    ctx._review_history = []

    if errored_note:
        advisory_warns.append(errored_note.strip())
    if advisory_warns or getattr(ctx, "_last_review_advisory_findings", None):
        ctx._review_advisory = list(getattr(ctx, "_last_review_advisory_findings", []) or [])
        if errored_note:
            ctx._review_advisory.append(errored_note.strip())
    return None

# Pre-Commit Review Checklists

Single source of truth for all automated review checklists (Bible P5: DRY).
Loaded by `ouroboros/tools/review.py` at review time and injected into the
multi-model review prompt.

When a new reviewable concern appears, add it here — not in prompts or docs.

---

## Advisory Pre-Review Workflow

**Correct sequence (mandatory):**

```
1. Finish ALL edits first (repo_write / str_replace_editor)
2. advisory_pre_review(commit_message="...")   ← run AFTER all edits, ONCE
3. repo_commit(commit_message="...")           ← run IMMEDIATELY after advisory
```

**Rules:**
- Successful worktree mutations automatically mark advisory as **stale**. This includes
  `repo_write`, `str_replace_editor`, `claude_code_edit`, and mutating `run_shell` /
  reviewed-commit paths when they change tracked worktree state.
- Any stale advisory → must re-run advisory before repo_commit.
- Do NOT interleave edits and advisory calls: `edit → advisory → edit → advisory` wastes two
  expensive advisory cycles. Finish all edits first.
- If advisory finds critical issues: **strongly recommended** to fix them and re-run advisory
  before calling repo_commit.
  Note: repo_commit's gate checks snapshot freshness and open obligations only — it does not
  enforce zero advisory FAIL items as a hard gate. Fixing critical findings and re-running
  advisory is best practice, but `repo_commit` can proceed on a fresh advisory even if the
  advisory reported FAIL items. The multi-model blocking review will still catch those issues.
- Once advisory is fresh → call repo_commit immediately without further edits.
- Bypass (`skip_advisory_pre_review=True`) is always durably audited in events.jsonl.

**Obligation tracking:**
- Every blocking `repo_commit` result creates "open obligations" — a structured checklist of
  unresolved issues that advisory must explicitly address on the next run.
- Advisory will receive the full list of open obligations and should respond to each one by name.
- A generic PASS without addressing open obligations is a weak signal — advisory is expected
  to confirm each obligation is resolved, though the gate does not enforce this at the code level.
- Open obligations are cleared automatically on a successful commit.
- Both triad-review blocks and scope-review blocks produce structured obligations.
- **Note:** conservative false-stale is acceptable. If you are unsure whether a mutating path
  changed the relevant repo snapshot, re-run `advisory_pre_review` explicitly.

---

### Review-exempt operations

The following tools create commits but are **exempt** from multi-model review
(Bible P8 explicit exception):

- `restore_to_head` — discards uncommitted changes (not a commit, no review needed)
- `revert_commit` — creates a mechanical inverse of an already-reviewed commit
- `rollback_to_target` — resets to an existing tag/SHA (already-reviewed state)

Rationale: review gates on rollbacks create a paradox where reviewers block
the undo for missing tests/VERSION, trapping the agent with broken code.
These tools restore to already-reviewed states by definition.

---

## Repo Commit Checklist

Used by `repo_commit` and `repo_write_commit` for all changes to the
Ouroboros repository.

| # | item | what to check | severity when FAIL |
|---|------|---------------|--------------------|
| 1 | bible_compliance | Does the diff violate any BIBLE.md principle? | critical |
| 2 | development_compliance | Does it follow DEVELOPMENT.md patterns? Check explicitly: (a) naming conventions (snake_case modules/vars, PascalCase classes, UPPER_SNAKE_CASE constants); (b) entity type rules — Gateway classes contain ONLY transport, no business logic; Tool functions are thin wrappers; (c) module-size target stays near one context window (~1000 lines) with a hard fail above 1250 lines for non-grandfathered modules, method-size target stays under 150 lines with a hard fail above 250 lines, and functions keep `<= 8` params; (d) no gratuitous abstract layers (P5 Minimalism); (e) new LLM calls go through the shared `LLMClient`/`llm.py` layer, not ad-hoc HTTP clients; (f) cognitive artifacts (identity.md, scratchpad, task reflections, review outputs) must NOT use hardcoded `[:N]` truncation — explicit omission notes required; (g) new `get_tools()` exports follow the ToolEntry pattern in registry.py. | critical |
| 3 | secrets_check | Are secrets, API keys, .env files, credentials present in the diff? | critical |
| 4 | code_quality | Careful code review: bugs, logic errors, crashes, regressions, race conditions, resource leaks? | critical |
| 5 | security_issues | Security vulnerabilities: injection, path traversal, secret leakage, unsafe operations? | critical |
| 6 | tests_affected | Did code logic change without corresponding test changes? (PASS if only docs/config/memory changed, or if tests are present.) | critical |
| 7 | architecture_doc | New module, endpoint, or data flow added but ARCHITECTURE.md not updated? (Write "Not applicable" with PASS if no architectural change.) | critical |
| 8 | version_bump | Behavior changed but VERSION not updated? (PASS if no behavior change.) | critical |
| 9 | changelog_and_badge | VERSION bumped but README.md badge or changelog not updated? (PASS if VERSION not bumped.) | critical |
| 10 | tool_registration | New tool function added but not exported in get_tools()? (PASS if no new tool.) | critical |
| 11 | context_building | New data/memory files that should appear in LLM context (context.py) but don't? | advisory |
| 12 | knowledge_index | Knowledge base topics changed but memory/knowledge/index-full.md not updated? | advisory |
| 13 | self_consistency | Does this change affect behavior described in `BIBLE.md`, `prompts/`, `docs/`, or this checklist itself? Check explicitly: (a) version in `ARCHITECTURE.md` header matches `VERSION` file; (b) tool names/descriptions in `prompts/SYSTEM.md` match tools actually exported by `get_tools()`; (c) JSONL log/memory file formats described in `ARCHITECTURE.md` match all readers/writers; (d) any behavioral change reflected in `prompts/CONSCIOUSNESS.md` if it affects background loop behavior; (e) DEVELOPMENT.md rules still accurate after the change. | critical |
| 14 | cross_platform | Does the diff use platform-specific APIs (`os.kill`, `os.setsid`, `os.killpg`, `os.getpgid`, `fcntl`, `msvcrt`, `signal.SIGKILL`, `signal.SIGTERM`, `subprocess` with `start_new_session`/`creationflags`, hardcoded `/` or `\\` in filesystem paths) outside of `ouroboros/platform_layer.py`? Does it import Unix-only or Windows-only modules (`fcntl`, `msvcrt`, `winreg`, `resource`) at any level without a platform guard (`sys.platform`/`IS_WINDOWS` check)? | critical |

### Severity rules

- Items 1-5 are always critical.
- Items 6-10, 14 are conditionally critical: FAIL only when the condition applies.
  If the condition does not apply, write verdict PASS with a short reason
  (e.g. "Not applicable — no code logic change").
- Items 11-12 are advisory: FAIL produces a warning but does not block.
- Item 13 (self_consistency) is conditionally critical: FAIL only when a
  concrete stale artifact is identified (specific file, line, or symbol).
  If no concrete staleness is found, write verdict PASS with a short reason.

### Critical threshold rule (applies to ALL items)

Before marking any item CRITICAL you MUST be able to answer YES to ALL of:
1. I can name the **exact file, symbol, function, test, or config path** in this
   repository that makes this problem live RIGHT NOW.
2. That artifact actually appears in the diff or touched-file context I have been given
   (not just in a hypothetical future scenario or external environment).
3. The fix requires a **change to this diff** — not a follow-up task or speculative guard.

If you cannot satisfy all three, use **advisory**, not critical.

One root cause = one FAIL entry. Do NOT split one underlying problem into multiple
FAIL items that all require the same change. Do NOT hold an obligation open by
reformulating a fixed concrete issue into a broader future-risk variant — if the
named artifact is fixed, mark PASS; raise a new advisory if a broader concern remains.

---

## Plan Review Checklist

Used by `plan_task` for pre-implementation design reviews, BEFORE any code is written.
Reviewers see the entire repository (full repo pack) plus the proposed plan and HEAD
snapshots of files planned to be touched.

For each item, reviewers provide: verdict (PASS / RISK / FAIL), a detailed explanation
of what was found, a concrete fix if RISK or FAIL (naming exact file/function/symbol),
and alternative approaches when a simpler solution exists.

| # | item | what to check | severity |
|---|------|---------------|----------|
| 1 | completeness | Are there files, tests, docs, prompts, configs, or sibling paths that must also change but are NOT mentioned in the plan? Name each one specifically. | FAIL if a required touchpoint is concretely missing; RISK if uncertain |
| 2 | correctness | Given the existing code, will the proposed approach actually work? Are there hidden dependencies, wrong assumptions about how existing code works, or API mismatches? Name exact functions/constants/modules at risk. | FAIL if a concrete breakage can be identified; RISK if uncertain |
| 3 | minimalism | Is there a simpler solution to the same problem with less surface area? If yes, describe the concrete alternative with the files/approach it would use. | RISK (advisory — help the implementer, not block them) |
| 4 | bible_alignment | Does the proposed approach violate any BIBLE.md principle? Check especially P3 (LLM-First — no hardcoded behavior logic), P5 (Minimalism — no gratuitous abstraction), and P2 (Meta-Reflection — fix the class, not the instance). | FAIL if a concrete principle violation is identifiable |
| 5 | implicit_contracts | Does the plan touch a module that other modules depend on through implicit contracts — format assumptions, expected function signatures, shared constants, protocol invariants? Name the callers/dependents that would break. | FAIL if a concrete broken caller can be named; RISK if uncertain |
| 6 | testability | Is the plan testable? Are there obvious edge cases not covered by the stated test approach? Are there integration boundaries that require mocking or fixtures not mentioned? | RISK (advisory) |
| 7 | architecture_fit | Does the plan solve the class of problem or is it a narrow patch leaving the root cause unresolved? If the latter, describe what architectural change would address the root cause. | RISK (advisory) |
| 8 | forgotten_docs | If the change affects behavior described in ARCHITECTURE.md, SYSTEM.md, README.md, DEVELOPMENT.md, or BIBLE.md, is that update included in the plan? Name the specific stale artifact. | FAIL if a concrete doc/prompt becomes stale and is not mentioned |

### Aggregate signal rules

- **GREEN** — no FAIL items, at most advisory RISKs. Implementer can proceed.
- **REVIEW_REQUIRED** — one or more RISK items. Implementer should read and decide.
- **REVISE_PLAN** — one or more FAIL items. Plan must be revised before writing code.

Reviewers must end with `AGGREGATE: GREEN`, `AGGREGATE: REVIEW_REQUIRED`, or `AGGREGATE: REVISE_PLAN`.

---

## Intent / Scope Review Checklist

Used by the full-codebase scope reviewer, which runs IN PARALLEL with the triad diff review.
Unlike triad reviewers who see only the diff, the scope reviewer sees the ENTIRE repository.
Its unique advantage is finding cross-module bugs, broken implicit contracts, and hidden
regressions that diff-only reviewers cannot see.

| # | item | what to check | severity when FAIL |
|---|------|---------------|--------------------|
| 1 | intent_alignment | Does the staged change actually fulfill the intended transformation, not merely touch related files? | critical if the incompleteness is concrete and evidenced; otherwise advisory |
| 2 | forgotten_touchpoints | Are there specific coupled files, tests, prompts, docs, configs, or sibling paths that must also change? Name the exact file(s) or symbol(s). | critical if a required touchpoint is concretely omitted; otherwise advisory |
| 3 | cross_surface_consistency | If behavior changed, are adjacent surfaces still consistent: prompts, docs, comments, tool descriptions, automation, or user-visible workflow? | critical if a concrete stale surface leaves the repo internally inconsistent; otherwise advisory |
| 4 | regression_surface | Does wider repository context show a concrete sibling path, migration edge, or parallel flow that remains broken or incomplete after this change? | critical if it leaves a concrete broken/incomplete path; otherwise advisory |
| 5 | prompt_doc_sync | If prompts or docs are relevant to the changed behavior, are they still accurate and mutually consistent? | critical if a concrete prompt/doc artifact becomes false or stale; otherwise advisory |
| 6 | architecture_fit | Does the change solve the class of problem, or is it a narrow patch that leaves the underlying pattern unresolved? | advisory |
| 7 | cross_module_bugs | Does this change break something in a different module through implicit coupling, shared state, or assumed call/return patterns? Name the exact module, symbol, or call site. | critical if a concrete cross-module breakage can be cited; otherwise advisory |
| 8 | implicit_contracts | Are there constants, data format assumptions, expected function signatures, or protocol invariants relied upon by OTHER modules that this change violates without updating those callers? Name the exact symbol or file. | critical if a concrete violated contract can be cited; otherwise advisory |

### Severity rules

- Any critical FAIL must cite a concrete file, symbol, prompt, doc, test, config, or sibling flow.
- If the reviewer cannot point to an exact touchpoint, the FAIL must be advisory, not critical.
- Scope affects only unchanged code outside the diff. The diff itself remains fully reviewable.

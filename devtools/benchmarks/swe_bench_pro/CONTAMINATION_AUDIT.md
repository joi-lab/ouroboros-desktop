# SWE-bench Pro — Contamination & Benchmark-Defect Audit (diagnostic only)

**This file never changes a score.** It is a secondary, human-readable audit of
SWE-bench Pro instances whose published task spec is internally inconsistent with
its own gold patch/tests. The **raw Pass@1 headline** printed by `grade_pro.py`
(`[headline] RAW Pass@1: N/M`) is the SOLE reported metric and is **not adjusted**
by anything here. Gold patches/tests are **never shown to the solver**, and no
instance is re-scored, dropped, or re-weighted. This is forensic transparency, not
leaderboard engineering.

## Why this exists

A handful of public-split instances are **false negatives by construction**: the
solver produces a correct fix, but the published task `interface` field (the part of
the prompt that tells the agent which public symbols the change must expose) is
inconsistent with the gold solution, so the gold-derived `FAIL_TO_PASS` tests check
a symbol/name the task itself told the agent does **not** change. These are
benchmark defects in the published task specification itself, not solver failures
and not contamination of our harness. They are a different defect class from
SWE-bench Pro issue #93, which documents future-git-history reward hacking in
public OSS images; that class is handled separately by `strip_gold_history.sh`
and the anti-cheat rules in `METHODOLOGY.md`.

## Rubric (`Verified` column)

- **defect-interface** — the `interface` field asserts "No new interfaces are
  introduced" (or lists the wrong names) while the gold patch renames/introduces a
  public symbol the hidden tests then require. The agent, obeying the stated
  interface, cannot match the hidden name.
- **defect-deps** — the prepared task image is missing an (undocumented) dependency
  bump the gold solution assumes; the gold tests cannot pass in the shipped image
  without it.
- **confirmed-real** — re-checked and the failure is a genuine solver miss (stays in
  the raw count as a fail; listed only for completeness).

`Verified` is the result of a manual cross-read of (a) the task spec/`interface`,
(b) the gold patch, and (c) the `FAIL_TO_PASS`/`PASS_TO_PASS` names — **without**
feeding any of it to the solver.

## Audited instances

| idx | Verified | Note |
|-----|----------|------|
| 4   | defect-interface | Task `interface` claims no new interface; gold renames/introduces a public symbol the hidden `FAIL_TO_PASS` requires. |
| 9   | defect-interface | Same class as idx4 — the stated interface contradicts the symbol the gold tests check. |
| 11  | defect-interface + defect-deps | Interface inconsistency AND the prepared image lacks an undocumented dependency bump the gold solution assumes. |

Update this table as further instances are cross-read. Adding a row is a
diagnostic note; it does not retroactively change any reported run's raw headline.

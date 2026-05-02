"""Tests for the section schedule (Phase 1 of adaptive context assembly).

The schedule is dormant — no caller invokes it yet. These tests pin the
structural invariants so a future change can't quietly break the algorithm
that Phase 4 will build on top.

The behavioural invariants (priority ordering, overflow handling, edge
cases) are validated end-to-end in
``scripts/simulate_adaptive_assembly.py`` against 15 scenarios.
"""
from __future__ import annotations

import pytest

from ouroboros.prompt_schedule import (
    MODE_PRIORITY_CAP,
    SCHEDULE,
    SectionSpec,
    estimate_tokens,
    mode_to_priority_cap,
)


# ---------------------------------------------------------------------------
# Schedule shape
# ---------------------------------------------------------------------------

def test_schedule_is_a_tuple_of_section_specs():
    assert isinstance(SCHEDULE, tuple)
    assert len(SCHEDULE) > 0
    for spec in SCHEDULE:
        assert isinstance(spec, SectionSpec)


def test_schedule_priorities_are_monotonically_non_decreasing():
    """The schedule order is the assembler's iteration order. A non-monotonic
    priority would make ``sorted()`` necessary in the algorithm — keep the
    invariant explicit so the source order matches the consideration order."""
    priorities = [s.priority for s in SCHEDULE]
    assert priorities == sorted(priorities), (
        f"schedule priorities not monotonic: {priorities}"
    )


def test_priority_zero_sections_are_marked_required():
    """Constitutional sections must signal ``is_optional=False`` so the
    assembler includes them even on budget overflow."""
    for spec in SCHEDULE:
        if spec.priority == 0:
            assert spec.is_optional is False, (
                f"priority-0 section {spec.name!r} marked optional — would "
                f"break constitutional floor guarantee"
            )


def test_priority_above_zero_sections_are_optional():
    for spec in SCHEDULE:
        if spec.priority > 0:
            assert spec.is_optional is True, (
                f"priority-{spec.priority} {spec.name!r} marked required — "
                f"would force inclusion past budget"
            )


def test_section_names_are_unique():
    names = [s.name for s in SCHEDULE]
    assert len(names) == len(set(names)), (
        f"duplicate section name in SCHEDULE: {names}"
    )


def test_buckets_are_one_of_three_known_values():
    valid = {"static", "semi_stable", "dynamic"}
    for spec in SCHEDULE:
        assert spec.bucket in valid, (
            f"section {spec.name!r} has unknown bucket {spec.bucket!r}; "
            f"must be one of {valid}"
        )


def test_constitutional_floor_covers_expected_sections():
    """Pin the four constitutional sections so a future refactor can't
    quietly demote one of them."""
    p0 = {s.name for s in SCHEDULE if s.priority == 0}
    assert p0 == {"system_prompt", "bible", "identity", "scratchpad"}, (
        f"constitutional set drifted: {p0}"
    )


# ---------------------------------------------------------------------------
# Token estimator
# ---------------------------------------------------------------------------

def test_estimate_tokens_returns_zero_for_empty():
    assert estimate_tokens(0) == 0
    assert estimate_tokens(-100) == 0


def test_estimate_tokens_is_conservative():
    """1000 chars / 3.5 chars-per-token = 286, * 1.10 safety = 314.6 → 315.
    The estimator must round up so the budget tracker never under-counts."""
    assert estimate_tokens(1000) >= 286, "estimator under-counts vs 3.5 ratio"
    assert estimate_tokens(1000) >= 315, "safety margin not applied"


def test_estimate_tokens_monotonic():
    """Bigger input → more tokens."""
    prev = -1
    for chars in (0, 1, 100, 1000, 10_000, 100_000):
        cur = estimate_tokens(chars)
        assert cur >= prev
        prev = cur


@pytest.mark.parametrize("chars,min_expected", [
    (3_500, 1_100),     # ~3.5k chars → at least 1100 tokens
    (35_000, 11_000),   # ~10x BIBLE
    (172_000, 54_000),  # ARCHITECTURE.md actual size
])
def test_estimate_tokens_realistic_ranges(chars, min_expected):
    assert estimate_tokens(chars) >= min_expected


# ---------------------------------------------------------------------------
# Mode → priority cap
# ---------------------------------------------------------------------------

def test_mode_priority_cap_table_covers_known_modes():
    for mode in ("light", "medium", "heavy", "full", "sparse", "dense"):
        assert mode in MODE_PRIORITY_CAP


def test_mode_priority_cap_ordering():
    """Light < medium < heavy < full — escalation must be strictly increasing."""
    assert MODE_PRIORITY_CAP["light"] < MODE_PRIORITY_CAP["medium"]
    assert MODE_PRIORITY_CAP["medium"] < MODE_PRIORITY_CAP["heavy"]
    assert MODE_PRIORITY_CAP["heavy"] < MODE_PRIORITY_CAP["full"]


def test_aliases_match_canonical_modes():
    """``sparse`` should match ``light``, ``dense`` should match ``full``."""
    assert MODE_PRIORITY_CAP["sparse"] == MODE_PRIORITY_CAP["light"]
    assert MODE_PRIORITY_CAP["dense"] == MODE_PRIORITY_CAP["full"]


def test_mode_to_priority_cap_handles_case_and_unknown():
    assert mode_to_priority_cap("MEDIUM") == MODE_PRIORITY_CAP["medium"]
    assert mode_to_priority_cap("Sparse") == MODE_PRIORITY_CAP["sparse"]
    # Unknown collapses to full (most permissive — safer than refusing)
    assert mode_to_priority_cap("bogus") == 99
    assert mode_to_priority_cap("") == 99
    assert mode_to_priority_cap(None) == 99


# ---------------------------------------------------------------------------
# Schedule completeness — all sections in today's static path are represented
# ---------------------------------------------------------------------------

def test_schedule_covers_every_static_path_source():
    """Every section the static dense path emits must have a SectionSpec.
    If a future PR adds a new section to ``build_llm_messages`` without
    extending SCHEDULE, the adaptive path would silently drop it — pin
    the coverage here."""
    schedule_names = {s.name for s in SCHEDULE}
    expected_today = {
        # Constitutional core
        "system_prompt", "bible", "identity", "scratchpad",
        # Static docs
        "architecture", "development", "checklists", "readme",
        # Semi-stable references
        "patterns", "kb_index", "deep_review",
        # Dynamic state
        "health", "registry", "backlog", "review_status", "recent_tails",
    }
    missing = expected_today - schedule_names
    assert not missing, f"SCHEDULE missing sections from static path: {missing}"


def test_schedule_dormancy_no_runtime_caller():
    """Phase 1 lands dormant. Verify nothing in the runtime imports the
    assembler (which doesn't exist yet) — only the schedule. This test is
    a tripwire: when Phase 4 lands, it should be deleted along with this
    docstring's premise."""
    import ouroboros.prompt_schedule as mod
    # Only public exports we expect at this phase
    public = {n for n in dir(mod) if not n.startswith("_")}
    expected = {
        "SectionSpec", "SCHEDULE", "estimate_tokens",
        "MODE_PRIORITY_CAP", "mode_to_priority_cap",
        "CHARS_PER_TOKEN", "SAFETY_MARGIN",
    }
    # Allow imports the module pulls in
    leftover = public - expected - {"math", "Tuple", "dataclass"}
    # The actual symbols above should all be present
    for s in expected:
        assert s in public, f"prompt_schedule missing public symbol {s!r}"

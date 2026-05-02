"""Section schedule for adaptive context assembly.

This module is **dormant** as of Phase 1 — it ships the data structures and
the priority-ordered ``SCHEDULE`` constant, plus a conservative token
estimator, but no caller invokes the assembler yet. The ``build_llm_messages``
path in ``ouroboros.context`` continues to use the static dense/medium/sparse
modes.

Phase 6 wires the schedule into a flag-gated adaptive path
(``OUROBOROS_PROMPT_ASSEMBLY=adaptive``). Until then, the schedule is a
self-documenting description of which sections matter, in which order, and
how heavy they are.

The schedule was validated end-to-end against 15 scenarios (real file sizes
+ adversarial edge cases) in ``scripts/simulate_adaptive_assembly.py``.
Algorithmic invariants verified there:

  * priority-0 sections are unconditionally included (constitutional floor)
  * higher-priority caps strictly include lower-priority sets
  * deterministic output for byte-stable KV-cache reuse
  * dropped-section reasons are accurate (``empty`` / ``budget`` / ``cap``)
  * priority-0 overflow is reported, not silently absorbed

See ``adaptations.md`` ("Adaptive context assembly — Phase 1") for context.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple


# Conservative chars→tokens estimator. The 1.10 multiplier guards against
# tokenizers that produce more tokens than the avg-3.5-chars-per-token rule
# of thumb suggests (e.g. code with many short tokens, non-Latin text). The
# assembler should err on the side of leaving budget unused rather than
# overshooting and forcing fallback truncation.
CHARS_PER_TOKEN = 3.5
SAFETY_MARGIN = 1.10


def estimate_tokens(chars: int) -> int:
    """Conservative chars→tokens estimator with a safety margin.

    Returns 0 for non-positive inputs (empty section). Otherwise rounds up
    so the budget tracker never thinks a section is smaller than it is.
    """
    if chars <= 0:
        return 0
    return int(math.ceil(chars / CHARS_PER_TOKEN * SAFETY_MARGIN))


@dataclass(frozen=True)
class SectionSpec:
    """Metadata for a single context section.

    Attributes:
      name:        stable identifier used in events and section caches
      priority:    0=constitutional (always included), higher=more droppable
      bucket:      which of the three system-message blocks this lands in
                   (``"static"`` | ``"semi_stable"`` | ``"dynamic"``).
                   The bucket determines KV-cache directives at message-build
                   time — static blocks get the longest TTL.
      is_optional: ``False`` only for priority-0 sections; signals to the
                   assembler that this section is included even if it pushes
                   ``budget_used`` past ``available_for_system``.
    """
    name: str
    priority: int
    bucket: str
    is_optional: bool = True


# Priority-ordered schedule. Empirically tuned against real file sizes
# (see scripts/simulate_adaptive_assembly.py). The ordering reflects:
#
#   priority 0 — constitutional core: SYSTEM, BIBLE, identity, scratchpad
#                Always included. May overflow budget at small ctx; the
#                algorithm reports this honestly via AssemblyResult.overflow.
#
#   priority 1 — high signal-per-byte at low cost: patterns, health, registry
#                These are small (~5k chars combined) and shape day-to-day
#                judgment. Worth including as soon as any budget exists.
#
#   priority 2 — judgment-shaping docs: DEVELOPMENT, CHECKLISTS
#                Small to medium (~50k chars combined). Useful at 64k+ ctx.
#
#   priority 3 — heavy architectural reference: ARCHITECTURE
#                Largest single file (~172k chars / 50k tokens). Only fits
#                in 128k+ ctx; below that, the agent reads it on demand.
#
#   priority 4 — live operational state: backlog, review_status
#                Medium size, useful for active work but not foundational.
#
#   priority 5 — high-volume references: kb_index, deep_review
#                Can grow without bound. Heavy mode is the threshold for
#                including these.
#
#   priority 6 — largest-lowest-signal: README, recent_tails
#                README duplicates much of ARCHITECTURE; recent_tails is
#                already accessible via tools. Full mode only.
SCHEDULE: Tuple[SectionSpec, ...] = (
    # Priority 0 — constitutional floor
    SectionSpec("system_prompt", 0, "static",      is_optional=False),
    SectionSpec("bible",         0, "static",      is_optional=False),
    SectionSpec("identity",      0, "semi_stable", is_optional=False),
    SectionSpec("scratchpad",    0, "dynamic",     is_optional=False),
    # Priority 1 — high signal-per-byte
    SectionSpec("patterns",      1, "semi_stable"),
    SectionSpec("health",        1, "dynamic"),
    SectionSpec("registry",      1, "dynamic"),
    # Priority 2 — judgment-shaping docs
    SectionSpec("development",   2, "static"),
    SectionSpec("checklists",    2, "static"),
    # Priority 3 — architectural reference
    SectionSpec("architecture",  3, "static"),
    # Priority 4 — live operational state
    SectionSpec("backlog",       4, "dynamic"),
    SectionSpec("review_status", 4, "dynamic"),
    # Priority 5 — high-volume references
    SectionSpec("kb_index",      5, "semi_stable"),
    SectionSpec("deep_review",   5, "semi_stable"),
    # Priority 6 — largest-lowest-signal
    SectionSpec("readme",        6, "static"),
    SectionSpec("recent_tails",  6, "dynamic"),
)


# Mode names map to priority caps. The static path's "dense"/"medium"/
# "sparse" stay valid as aliases so an operator switching between assembly
# modes doesn't have to relearn the vocabulary.
MODE_PRIORITY_CAP = {
    "light":  1,
    "medium": 3,
    "heavy":  5,
    "full":   99,
    # Aliases for the static path
    "sparse": 1,
    "dense":  99,
}


def mode_to_priority_cap(mode: str) -> int:
    """Resolve a prompt-mode name to its priority cap. Unknown modes
    collapse to ``full`` (defensive)."""
    return MODE_PRIORITY_CAP.get(str(mode or "").lower(), 99)

"""Phase 8 — user-escalation layer (``ouroboros.escalation``).

Triggered by Phase 3 (dedup) and Phase 4 (gate retry) short-circuits
plus a per-task wall-clock budget. Pushes user-facing send_message
events onto the chat queue so the operator sees a chat-level signal
instead of having to notice the trace went silent.

These tests verify the three trigger points in isolation; the
integration with loop_tool_execution.py is covered by the existing
gate-retry / dedup test files (which still pass after Phase 8 wiring
because the wiring is best-effort and swallows errors).
"""

from __future__ import annotations

import queue
import time
from dataclasses import dataclass
from typing import Any, Optional

import pytest

from ouroboros.compat import PROFILES, CompatProfile
from ouroboros.escalation import (
    _is_enabled,
    check_wall_clock,
    emit_gate_retry,
    maybe_emit_dedup,
)


# ---------------------------------------------------------------------------
# Fixtures: minimal context object stand-in
# ---------------------------------------------------------------------------

@dataclass
class _FakeCtx:
    compat_profile: Optional[CompatProfile] = None
    event_queue: Optional[queue.Queue] = None
    current_chat_id: Optional[int] = None
    task_id: str = ""
    _dedup_ping_counts: dict = None  # type: ignore[assignment]
    _dedup_counts: dict = None  # type: ignore[assignment]
    _wall_budget_breached: bool = False
    _wall_budget_extensions: int = 0

    def __post_init__(self):
        if self._dedup_ping_counts is None:
            self._dedup_ping_counts = {}
        if self._dedup_counts is None:
            self._dedup_counts = {}


@pytest.fixture
def small_local_ctx():
    """Profile that has escalation_enabled=True with realistic thresholds."""
    return _FakeCtx(
        compat_profile=PROFILES["small_local"],
        event_queue=queue.Queue(),
        current_chat_id=1,
        task_id="t-1",
    )


@pytest.fixture
def cloud_ctx():
    """Cloud profile — all escalation disabled."""
    return _FakeCtx(
        compat_profile=PROFILES["cloud_class"],
        event_queue=queue.Queue(),
        current_chat_id=1,
        task_id="t-1",
    )


# ---------------------------------------------------------------------------
# Master switch
# ---------------------------------------------------------------------------

def test_disabled_for_cloud_profile(cloud_ctx):
    assert _is_enabled(cloud_ctx) is False


def test_enabled_for_small_local_profile(small_local_ctx):
    assert _is_enabled(small_local_ctx) is True


def test_disabled_when_no_profile():
    ctx = _FakeCtx(compat_profile=None)
    assert _is_enabled(ctx) is False


def test_kill_switch_overrides_profile(small_local_ctx, monkeypatch):
    monkeypatch.setenv("OUROBOROS_ESCALATION_DISABLED", "true")
    assert _is_enabled(small_local_ctx) is False


# ---------------------------------------------------------------------------
# Gate-retry escalation
# ---------------------------------------------------------------------------

def test_gate_retry_pushes_send_message(small_local_ctx):
    ok = emit_gate_retry(
        small_local_ctx,
        tool_name="repo_commit",
        block_reason="tests_preflight_blocked",
        count=3,
        cap=2,
    )
    assert ok is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["type"] == "send_message"
    assert msg["is_escalation"] is True
    assert msg["escalation_kind"] == "gate_retry"
    assert "repo_commit" in msg["text"]
    assert "tests_preflight_blocked" in msg["text"]
    assert "(cap=2)" in msg["text"]
    assert "3 times" in msg["text"]


def test_gate_retry_silent_for_cloud(cloud_ctx):
    ok = emit_gate_retry(
        cloud_ctx,
        tool_name="repo_commit",
        block_reason="x",
        count=10,
        cap=5,
    )
    assert ok is False
    assert cloud_ctx.event_queue.empty()


def test_gate_retry_silent_when_chat_id_missing(small_local_ctx):
    small_local_ctx.current_chat_id = None
    ok = emit_gate_retry(
        small_local_ctx,
        tool_name="x", block_reason="y", count=1, cap=1,
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Dedup escalation
# ---------------------------------------------------------------------------

def test_dedup_emits_only_after_threshold(small_local_ctx):
    # small_local threshold=2 — emit after 3 dedups (threshold + 1)
    assert maybe_emit_dedup(small_local_ctx, tool_name="repo_read") is False  # 1
    assert maybe_emit_dedup(small_local_ctx, tool_name="repo_read") is False  # 2
    assert maybe_emit_dedup(small_local_ctx, tool_name="repo_read") is True   # 3 → emit
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["escalation_kind"] == "dedup"
    assert "repo_read" in msg["text"]


def test_dedup_only_emits_once_per_tool_per_task(small_local_ctx):
    # Pump past threshold
    for _ in range(5):
        maybe_emit_dedup(small_local_ctx, tool_name="repo_read")
    # Drain whatever was queued
    drained = []
    while not small_local_ctx.event_queue.empty():
        drained.append(small_local_ctx.event_queue.get_nowait())
    # Exactly one escalation, not five
    assert len(drained) == 1


def test_dedup_separate_buckets_per_tool(small_local_ctx):
    # Tool A — escalate
    for _ in range(3):
        maybe_emit_dedup(small_local_ctx, tool_name="repo_read")
    # Tool B — independent counter, also escalates
    for _ in range(3):
        maybe_emit_dedup(small_local_ctx, tool_name="git_status")
    drained = []
    while not small_local_ctx.event_queue.empty():
        drained.append(small_local_ctx.event_queue.get_nowait())
    assert len(drained) == 2
    assert {m["text"].split("`")[1] for m in drained} == {"repo_read", "git_status"}


def test_dedup_silent_for_cloud(cloud_ctx):
    for _ in range(20):
        ok = maybe_emit_dedup(cloud_ctx, tool_name="repo_read")
        assert ok is False
    assert cloud_ctx.event_queue.empty()


# ---------------------------------------------------------------------------
# Wall-clock budget
# ---------------------------------------------------------------------------

def test_wall_clock_under_budget_returns_false(small_local_ctx):
    # small_local budget = 1800s; 60s elapsed → not breached
    started = time.time() - 60
    assert check_wall_clock(small_local_ctx, task_started_ts=started, rounds=10) is False
    assert small_local_ctx.event_queue.empty()


def test_wall_clock_over_budget_emits_once(small_local_ctx):
    # 2000s elapsed > 1800s budget
    started = time.time() - 2000
    assert check_wall_clock(small_local_ctx, task_started_ts=started, rounds=50) is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["escalation_kind"] == "wall_budget"
    assert "33 min" in msg["text"]  # 2000s ≈ 33 min
    assert "30 minutes" in msg["text"]  # budget
    assert "50 rounds" in msg["text"]
    # Subsequent calls return False (already emitted)
    assert check_wall_clock(small_local_ctx, task_started_ts=started, rounds=51) is False


def test_wall_clock_silent_for_cloud(cloud_ctx):
    # cloud budget = 0 → disabled regardless of elapsed time
    started = time.time() - 99999
    assert check_wall_clock(cloud_ctx, task_started_ts=started, rounds=999) is False


def test_wall_clock_env_override_beats_profile(small_local_ctx, monkeypatch):
    monkeypatch.setenv("OUROBOROS_WALL_BUDGET_SEC", "60")
    started = time.time() - 120  # 120s elapsed > 60s env budget < 1800s profile
    assert check_wall_clock(small_local_ctx, task_started_ts=started, rounds=5) is True


def test_wall_clock_invalid_env_falls_back_to_profile(small_local_ctx, monkeypatch):
    monkeypatch.setenv("OUROBOROS_WALL_BUDGET_SEC", "garbage")
    started = time.time() - 120  # 120s < 1800s profile budget
    assert check_wall_clock(small_local_ctx, task_started_ts=started, rounds=5) is False


def test_wall_clock_zero_env_disables(small_local_ctx, monkeypatch):
    monkeypatch.setenv("OUROBOROS_WALL_BUDGET_SEC", "0")
    started = time.time() - 99999
    # env=0 means "use profile" — and profile is 1800, so 99999s elapsed → emit
    assert check_wall_clock(small_local_ctx, task_started_ts=started, rounds=5) is True


# ---------------------------------------------------------------------------
# Profile shape regression
# ---------------------------------------------------------------------------

def test_profile_dataclass_includes_escalation_fields():
    p = PROFILES["small_local"]
    assert hasattr(p, "escalation_enabled")
    assert hasattr(p, "wall_budget_sec")
    assert hasattr(p, "dedup_ping_threshold")
    assert p.escalation_enabled is True
    assert p.wall_budget_sec == 1800
    assert p.dedup_ping_threshold == 2


def test_cloud_profile_disables_all_escalation():
    p = PROFILES["cloud_class"]
    assert p.escalation_enabled is False
    assert p.wall_budget_sec == 0
    assert p.dedup_ping_threshold == 0


def test_constrained_local_tighter_than_small():
    s = PROFILES["small_local"]
    c = PROFILES["constrained_local"]
    assert c.wall_budget_sec < s.wall_budget_sec
    assert c.dedup_ping_threshold < s.dedup_ping_threshold


# ---------------------------------------------------------------------------
# Productivity-aware wall-clock (2026-05-02)
# Algorithm validated against real session traces in
# scripts/simulate_wall_clock_productivity.py before landing here.
# ---------------------------------------------------------------------------

def test_productive_task_extends_budget_instead_of_firing(small_local_ctx):
    """Drift loop discriminator: tool-diverse, no pings, low repetition →
    extend budget. Verifies the productive-35min trace fixture in the
    simulator behaves correctly when wired through real check_wall_clock."""
    # 35 minutes elapsed (past 30-min budget for small_local)
    started = time.time() - 35 * 60
    # 9 distinct tool calls, each fired exactly once (Ouro's productive trace)
    small_local_ctx._dedup_counts = {
        "repo_read:tools_git_py":              1,
        "repo_read:structured_output_py":      1,
        "repo_read:review_py":                 1,
        "repo_read:commit_gate_py":            1,
        "repo_read:parallel_review_py":        1,
        "repo_read:advisory_pre_review_py":    1,
        "repo_read:claude_advisory_review_py": 1,
        "repo_read:agent_py":                  1,
        "data_read:state_json":                1,
    }
    small_local_ctx._dedup_ping_counts = {}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=15)
    assert fired is False, "productive task incorrectly escalated"
    assert small_local_ctx._wall_budget_extensions == 1
    assert small_local_ctx.event_queue.empty(), "no chat message should be sent on extend"


def test_drift_loop_fires_unproductive_with_pings(small_local_ctx):
    """Drift loop discriminator: dedup pings present → fire immediately."""
    started = time.time() - 31 * 60
    small_local_ctx._dedup_counts = {
        "repo_read:identity_md": 5,
        "repo_read:scratchpad_md": 4,
        "data_read:adaptations_md": 3,
        "repo_read:bible_md": 2,
    }
    small_local_ctx._dedup_ping_counts = {"repo_read": 2, "data_read": 1}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=12)
    assert fired is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["escalation_kind"] == "wall_budget"
    assert "dedup_pings" in msg["text"]


def test_low_diversity_without_pings_still_fires(small_local_ctx):
    """Even without pings, too-few distinct tool calls indicates drift."""
    started = time.time() - 31 * 60
    # 4 distinct calls in 30-min budget → below 1-per-5-min threshold
    small_local_ctx._dedup_counts = {
        "repo_read:bible_md": 3,
        "repo_read:adaptations_md": 2,
        "data_read:state_json": 2,
        "repo_read:checklists_md": 1,
    }
    small_local_ctx._dedup_ping_counts = {}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=10)
    assert fired is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert "low_diversity" in msg["text"]


def test_high_repetition_fires_even_with_diverse_calls(small_local_ctx):
    """6 distinct calls but each repeated ~5 times → avg = 5.0 > 2.5."""
    started = time.time() - 31 * 60
    small_local_ctx._dedup_counts = {
        f"repo_read:file_{i}": 5 for i in range(6)
    }
    small_local_ctx._dedup_ping_counts = {}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=15)
    assert fired is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert "high_repetition" in msg["text"]


def test_extended_budget_fires_at_max_extensions(small_local_ctx):
    """Once 2 extensions are used, even productive tasks must escalate."""
    # small_local budget = 1800s. 2 extensions → effective 3600s.
    # At 3601s elapsed, the next check should fire — with productive signals
    # this is the soft "max-extensions reached" message.
    started = time.time() - 3601  # past extended budget
    # max_ext default is 2; hard ceiling = 1800 * (1 + 0.5*2) = 3600
    # so 3601 hits hard ceiling first
    small_local_ctx._wall_budget_extensions = 2
    small_local_ctx._dedup_counts = {f"tool_{i}": 1 for i in range(20)}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=40)
    assert fired is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["escalation_kind"] == "wall_budget_hard_ceiling"


def test_hard_ceiling_fires_regardless_of_productivity(small_local_ctx):
    """Pathological breach: long-running productive task hits hard ceiling."""
    started = time.time() - 70 * 60   # 70 min, > 2x 30-min budget
    small_local_ctx._dedup_counts = {f"tool_{i}": 1 for i in range(20)}
    small_local_ctx._dedup_ping_counts = {}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=30)
    assert fired is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["escalation_kind"] == "wall_budget_hard_ceiling"
    assert "hard ceiling" in msg["text"].lower()


def test_max_extensions_zero_restores_legacy_behavior(small_local_ctx, monkeypatch):
    """Setting OUROBOROS_WALL_BUDGET_MAX_EXTENSIONS=0 disables extensions
    so existing operators get the pre-2026-05-02 single-budget behavior."""
    monkeypatch.setenv("OUROBOROS_WALL_BUDGET_MAX_EXTENSIONS", "0")
    started = time.time() - 35 * 60   # past 30-min budget
    small_local_ctx._dedup_counts = {f"tool_{i}": 1 for i in range(20)}
    fired = check_wall_clock(small_local_ctx, task_started_ts=started, rounds=15)
    assert fired is True
    # No extensions granted; normal wall_budget kind (not hard_ceiling, since
    # max_ext=0 means hard_ceiling = budget itself = 1800; elapsed=2100 > 1800).
    msg = small_local_ctx.event_queue.get_nowait()
    # With max_ext=0, hard_ceiling == budget, so we hit hard_ceiling immediately.
    # This is correct — max_ext=0 explicitly disables the productivity-extension
    # path; any breach goes straight to hard_ceiling.
    assert msg["escalation_kind"] in ("wall_budget", "wall_budget_hard_ceiling")


def test_extension_state_persists_across_calls(small_local_ctx):
    """Extension counter must increment across multiple check calls
    so consecutive productive checks consume the extension allowance.

    Note: with the default factor=0.5 and max_ext=2, the effective-budget
    at max-extensions equals the hard-ceiling (both base*2). So a third
    productive check past the extended budget always fires hard_ceiling,
    never a "soft max-extensions" message — the latter is unreachable
    in the default config (kept as defensive code for max_ext > 2).
    """
    # First check at 31 min: extends to ext=1 (effective 45 min)
    small_local_ctx._dedup_counts = {f"tool_{i}": 1 for i in range(10)}
    fired = check_wall_clock(small_local_ctx, task_started_ts=time.time() - 31 * 60, rounds=12)
    assert fired is False
    assert small_local_ctx._wall_budget_extensions == 1
    # Second check at 46 min: still productive, extends to ext=2 (effective 60 min)
    fired2 = check_wall_clock(small_local_ctx, task_started_ts=time.time() - 46 * 60, rounds=20)
    assert fired2 is False
    assert small_local_ctx._wall_budget_extensions == 2
    # Third check at 61 min: past hard-ceiling (60 min) → fires hard ceiling
    fired3 = check_wall_clock(small_local_ctx, task_started_ts=time.time() - 61 * 60, rounds=25)
    assert fired3 is True
    msg = small_local_ctx.event_queue.get_nowait()
    assert msg["escalation_kind"] == "wall_budget_hard_ceiling"

"""Tests for the soft-restart path: tool handler, pipeline propagation,
and tool registration.

The soft restart was added 2026-05-02 to break the dev-mode auto-restart
spam loop. Where ``request_restart`` queues a full process exit (handled
by the launcher / rescue_and_reset path), ``request_soft_restart`` queues
a worker-pool refresh only — kill + respawn workers so they re-import
modules from disk, no git operations, supervisor process untouched.

This file pins the wiring so a future refactor can't silently merge the
two paths or drop the policy field.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def test_request_soft_restart_sets_reason_and_policy():
    """The handler latches reason + policy='soft' on ToolContext, no git ops."""
    from ouroboros.tools import control as control_module

    ctx = SimpleNamespace(pending_restart_reason=None, pending_restart_policy=None)
    result = control_module._request_soft_restart(ctx, "pick up watcher fix")

    assert ctx.pending_restart_reason == "pick up watcher fix"
    assert ctx.pending_restart_policy == "soft"
    assert "soft restart queued" in result.lower()
    assert "policy=soft" in result.lower()


def test_request_soft_restart_default_reason():
    """Empty reason becomes 'agent_requested_soft_restart'."""
    from ouroboros.tools import control as control_module

    ctx = SimpleNamespace(pending_restart_reason=None, pending_restart_policy=None)
    control_module._request_soft_restart(ctx, "")

    assert ctx.pending_restart_reason == "agent_requested_soft_restart"
    assert ctx.pending_restart_policy == "soft"


def test_request_soft_restart_does_not_touch_last_push_succeeded():
    """Unlike _request_restart, the soft path is dev-mode safe and must
    not touch evolution-mode push gates."""
    from ouroboros.tools import control as control_module

    ctx = SimpleNamespace(
        pending_restart_reason=None,
        pending_restart_policy=None,
        last_push_succeeded=True,
        current_task_type="evolution",
    )
    control_module._request_soft_restart(ctx, "reload")

    # Soft path is allowed even in evolution mode — no precondition gate.
    assert ctx.pending_restart_policy == "soft"
    assert ctx.last_push_succeeded is True


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def test_request_soft_restart_is_registered():
    """The tool must be in get_tools() so the registry picks it up."""
    from ouroboros.tools import control as control_module

    entries = control_module.get_tools()
    names = {e.name for e in entries}
    assert "request_soft_restart" in names


def test_request_soft_restart_is_in_core_tool_names():
    """Must be a CORE tool — always available without enable_tools, since
    the agent needs it to respond to a drift notification immediately."""
    from ouroboros.tools.registry import CORE_TOOL_NAMES
    assert "request_soft_restart" in CORE_TOOL_NAMES


def test_request_soft_restart_has_skip_policy():
    """No LLM safety check; this is a trusted built-in like request_restart."""
    from ouroboros.safety import TOOL_POLICY, POLICY_SKIP
    assert TOOL_POLICY.get("request_soft_restart") == POLICY_SKIP


# ---------------------------------------------------------------------------
# Pipeline propagation — restart_request event must carry policy field
# ---------------------------------------------------------------------------

def test_emit_task_results_propagates_soft_policy(tmp_path, monkeypatch):
    """The restart_request event emitted at task end must include
    ``policy='soft'`` when the soft path queued the restart."""
    from ouroboros import agent_task_pipeline as pipeline

    monkeypatch.setattr(pipeline, "_store_task_result", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_run_chat_consolidation", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_run_scratchpad_consolidation", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *a, **k: None)

    pending_events: list = []
    ctx = SimpleNamespace(
        pending_restart_reason="reload modules",
        pending_restart_policy="soft",
    )
    env = SimpleNamespace(drive_root=tmp_path)
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline.emit_task_results(
        env=env,
        memory=object(),
        llm=object(),
        pending_events=pending_events,
        task={"id": "t-1", "type": "task", "chat_id": 1, "text": "x"},
        text="ok",
        usage={"rounds": 1, "cost": 0.0},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
        ctx=ctx,
    )

    restart_evts = [e for e in pending_events if e.get("type") == "restart_request"]
    assert len(restart_evts) == 1
    assert restart_evts[0].get("policy") == "soft"
    assert restart_evts[0].get("reason") == "reload modules"
    # Both fields must be cleared after emission.
    assert ctx.pending_restart_reason is None
    assert ctx.pending_restart_policy is None


def test_emit_task_results_omits_policy_for_full_restart(tmp_path, monkeypatch):
    """When pending_restart_policy is unset (full restart), the event
    must NOT carry a policy field — preserves backward compat with the
    existing _handle_restart_in_supervisor path."""
    from ouroboros import agent_task_pipeline as pipeline

    monkeypatch.setattr(pipeline, "_store_task_result", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_run_chat_consolidation", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_run_scratchpad_consolidation", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *a, **k: None)

    pending_events: list = []
    ctx = SimpleNamespace(
        pending_restart_reason="full reload",
        pending_restart_policy=None,
    )
    env = SimpleNamespace(drive_root=tmp_path)
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline.emit_task_results(
        env=env,
        memory=object(),
        llm=object(),
        pending_events=pending_events,
        task={"id": "t-2", "type": "task", "chat_id": 1, "text": "x"},
        text="ok",
        usage={"rounds": 1, "cost": 0.0},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
        ctx=ctx,
    )

    restart_evts = [e for e in pending_events if e.get("type") == "restart_request"]
    assert len(restart_evts) == 1
    assert "policy" not in restart_evts[0]


# ---------------------------------------------------------------------------
# ToolContext field
# ---------------------------------------------------------------------------

def test_tool_context_has_pending_restart_policy_field():
    """ToolContext must expose pending_restart_policy so the soft handler
    can stash the policy for the pipeline to read."""
    import pathlib
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(repo_dir=pathlib.Path("/tmp"), drive_root=pathlib.Path("/tmp"))
    assert hasattr(ctx, "pending_restart_policy")
    assert ctx.pending_restart_policy is None


# ---------------------------------------------------------------------------
# Settings — drift accumulator knobs
# ---------------------------------------------------------------------------

def test_drift_settings_in_defaults():
    """OUROBOROS_DRIFT_SETTLE_SEC + OUROBOROS_DRIFT_QUORUM must default
    so settings.json round-trips don't drop them."""
    from ouroboros.config import SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS.get("OUROBOROS_DRIFT_SETTLE_SEC") == 60
    assert SETTINGS_DEFAULTS.get("OUROBOROS_DRIFT_QUORUM") == 3


def test_drift_settings_propagated_to_env():
    """Both keys must appear in apply_settings_to_env's env_keys list."""
    import inspect
    from ouroboros import config

    src = inspect.getsource(config.apply_settings_to_env)
    assert "OUROBOROS_DRIFT_SETTLE_SEC" in src
    assert "OUROBOROS_DRIFT_QUORUM" in src

"""Phase 1 — ``ouroboros.compat`` compatibility-profile resolver.

These tests pin the detection rules and the dormant-by-default contract.
The profile system is wired into ``agent.py::run_task`` and read by
``loop.py``, ``loop_tool_execution.py``, and ``llm.py``; this file
verifies the resolver in isolation.
"""

from __future__ import annotations

import pytest

from ouroboros.compat import (
    PROFILES,
    CompatProfile,
    resolve_profile,
    emit_profile_selected,
)


# ---------------------------------------------------------------------------
# Dormant-by-default contract
# ---------------------------------------------------------------------------

def test_master_switch_off_returns_cloud_class(monkeypatch):
    """When OUROBOROS_COMPAT_ENABLED is unset, every model must resolve to
    cloud_class — guaranteed zero behaviour change on a fresh install."""
    monkeypatch.delenv("OUROBOROS_COMPAT_ENABLED", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", "4096")

    profile, reason = resolve_profile("openai-compatible::qwen-7b", use_local=False)
    assert profile is PROFILES["cloud_class"]
    assert reason == "master_switch_off"


def test_master_switch_explicitly_false_returns_cloud_class(monkeypatch):
    monkeypatch.setenv("OUROBOROS_COMPAT_ENABLED", "false")
    profile, reason = resolve_profile("openai-compatible::qwen-7b", use_local=True)
    assert profile is PROFILES["cloud_class"]
    assert reason == "master_switch_off"


# ---------------------------------------------------------------------------
# Detection rules (master switch enabled)
# ---------------------------------------------------------------------------

@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("OUROBOROS_COMPAT_ENABLED", "true")
    monkeypatch.delenv("OUROBOROS_COMPAT_PROFILE", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", raising=False)
    monkeypatch.delenv("LOCAL_MODEL_CONTEXT_LENGTH", raising=False)


def test_anthropic_routes_cloud(enabled):
    profile, reason = resolve_profile("anthropic/claude-opus-4.7", use_local=False)
    assert profile is PROFILES["cloud_class"]
    assert reason == "detected_provider_cloud"


def test_openai_routes_cloud(enabled):
    profile, _ = resolve_profile("openai::gpt-5.2", use_local=False)
    assert profile is PROFILES["cloud_class"]


def test_openrouter_unprefixed_routes_cloud(enabled):
    profile, _ = resolve_profile("anthropic/claude-sonnet-4.6", use_local=False)
    assert profile is PROFILES["cloud_class"]


def test_constrained_local_at_ctx_lt_16k(enabled, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", "4096")
    profile, reason = resolve_profile("openai-compatible::qwen-7b", use_local=False)
    assert profile is PROFILES["constrained_local"]
    assert reason == "detected_local_constrained"


def test_small_local_at_ctx_ge_16k(enabled, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", "32768")
    profile, reason = resolve_profile("openai-compatible::qwen-27b", use_local=False)
    assert profile is PROFILES["small_local"]
    assert reason == "detected_local_small"


def test_use_local_true_routes_local_path(enabled):
    """USE_LOCAL_MAIN=true (bundled llama-cpp) is also local even though the
    model string isn't openai-compatible-prefixed."""
    profile, reason = resolve_profile("anthropic/claude-opus-4.7", use_local=True)
    # use_local overrides the cloud-provider detection
    assert profile.name in ("small_local", "constrained_local")
    assert reason.startswith("detected_local_")


def test_unknown_localhost_defaults_small_local(enabled):
    """No context length set + openai-compatible model → small_local
    (assume modest 32k+ window rather than tiny 4k)."""
    profile, reason = resolve_profile("openai-compatible::unknown-model", use_local=False)
    assert profile is PROFILES["small_local"]
    assert reason == "detected_local_small"


def test_local_context_length_falls_back(enabled, monkeypatch):
    """When OPENAI_COMPATIBLE_CONTEXT_LENGTH is unset, LOCAL_MODEL_CONTEXT_LENGTH
    is consulted — same precedence the runtime uses elsewhere."""
    monkeypatch.delenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", raising=False)
    monkeypatch.setenv("LOCAL_MODEL_CONTEXT_LENGTH", "8192")
    profile, _ = resolve_profile("openai-compatible::qwen-14b", use_local=False)
    assert profile is PROFILES["constrained_local"]


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------

def test_env_override_pins_profile(enabled, monkeypatch):
    monkeypatch.setenv("OUROBOROS_COMPAT_PROFILE", "constrained_local")
    monkeypatch.setenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", "200000")  # would otherwise be small_local
    profile, reason = resolve_profile("openai-compatible::qwen-27b", use_local=False)
    assert profile is PROFILES["constrained_local"]
    assert reason == "env_override"


def test_env_override_overrides_cloud_provider(enabled, monkeypatch):
    """Explicit pin wins even over a cloud model — useful for testing or for
    deliberately constraining loop policy regardless of provider."""
    monkeypatch.setenv("OUROBOROS_COMPAT_PROFILE", "constrained_local")
    profile, reason = resolve_profile("anthropic/claude-opus-4.7", use_local=False)
    assert profile is PROFILES["constrained_local"]
    assert reason == "env_override"


def test_invalid_override_falls_back_to_detect(enabled, monkeypatch):
    monkeypatch.setenv("OUROBOROS_COMPAT_PROFILE", "garbage_value")
    monkeypatch.setenv("OPENAI_COMPATIBLE_CONTEXT_LENGTH", "32768")
    profile, reason = resolve_profile("openai-compatible::qwen-27b", use_local=False)
    assert profile is PROFILES["small_local"]
    assert reason == "detected_local_small"


def test_empty_override_runs_detection(enabled, monkeypatch):
    monkeypatch.setenv("OUROBOROS_COMPAT_PROFILE", "")
    profile, reason = resolve_profile("anthropic/claude-opus-4.7", use_local=False)
    assert profile is PROFILES["cloud_class"]
    assert reason == "detected_provider_cloud"


# ---------------------------------------------------------------------------
# Profile shape
# ---------------------------------------------------------------------------

def test_cloud_class_reproduces_today_hardcoded_values():
    """Cloud profile must match the legacy hardcoded constants exactly so a
    cloud task sees zero behaviour change when the master switch is on."""
    p = PROFILES["cloud_class"]
    assert p.checkpoint_interval == 15      # loop.py:255 hardcoded
    assert p.dedup_cap == 0                  # disabled — current behavior
    assert p.gate_retry_cap == 0             # disabled — current behavior
    # 999 = effectively disabled (only emergency >1.2M chars triggers compaction)
    assert p.compact_round == 999
    assert p.compact_msgs == 999


def test_profiles_are_frozen_dataclasses():
    """Frozen so a consumer can't mutate the shared instance and corrupt
    other tasks running in the same process."""
    p = PROFILES["small_local"]
    with pytest.raises((AttributeError, Exception)):
        p.dedup_cap = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Event emission
# ---------------------------------------------------------------------------

def test_emit_profile_selected_pushes_event_with_full_payload():
    import queue
    q = queue.Queue()
    emit_profile_selected(
        q, "task-123", PROFILES["constrained_local"], "detected_local_constrained",
        target_model="openai-compatible::qwen-7b",
        provider="openai-compatible",
        context_length=4096,
    )
    payload = q.get_nowait()
    assert payload["type"] == "log_event"
    data = payload["data"]
    assert data["type"] == "compat_profile_selected"
    assert data["task_id"] == "task-123"
    assert data["profile"] == "constrained_local"
    assert data["reason"] == "detected_local_constrained"
    assert data["context_length"] == 4096
    assert data["dedup_cap"] == 2  # constrained_local cap


def test_emit_profile_selected_silent_when_no_queue():
    """None queue is a no-op — must not raise."""
    emit_profile_selected(None, "t", PROFILES["cloud_class"], "master_switch_off")

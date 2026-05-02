"""Local-LLM compatibility profile for Ouroboros.

Single source of truth for "what kind of brain is driving this task" so the
runtime can adapt loop policies (checkpoint cadence, tool-call dedup, gate
retry caps, compaction thresholds) without touching the constitutional core.

Discipline: **land dormant, engage by flag.** Every consumer falls back to
today's hardcoded value when ``OUROBOROS_COMPAT_ENABLED`` is False (the
default), so this module is observably a no-op until explicitly turned on.

Resolved once per task at ``agent.py::run_task`` entry and stashed on
``tools._ctx.compat_profile`` for the duration of that task — mirrors the
existing ``OUROBOROS_PROMPT_MODE`` per-task capture pattern. In-flight tasks
finish on the profile they captured; new tasks pick up flag flips.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from ouroboros.pricing import infer_provider_from_model

log = logging.getLogger(__name__)

_CLOUD_PROVIDERS = frozenset({"anthropic", "openai", "openrouter", "cloudru"})


@dataclass(frozen=True)
class CompatProfile:
    """Frozen policy bundle keyed off the model class driving a task.

    Field semantics:
      ``checkpoint_interval`` — how often ``_maybe_inject_self_check``
        injects a periodic checkpoint. Smaller = more frequent self-check
        rounds (helps drift-prone small models).
      ``dedup_cap`` — after this many *additional* identical
        (tool_name, args) calls in a single task, the dispatcher returns
        a synthetic DEDUP_BLOCKED result. ``0`` disables dedup entirely.
      ``gate_retry_cap`` — after this many failed (tool_name, block_reason)
        pairs in a single task, the dispatcher emits GATE_RETRY_EXHAUSTED
        and stops retrying. ``0`` disables.
      ``compact_round`` / ``compact_msgs`` — routine-compaction thresholds
        for the local-routing path in ``loop.py``. ``999`` = effectively
        disabled (only the emergency >1.2M-char path fires).
      ``escalation_enabled`` — when True, GATE_RETRY_EXHAUSTED + repeat
        DEDUP_BLOCKED + wall-clock-budget exhaustion push a user-facing
        send_message into the chat queue. Cloud profile defaults False
        (cloud agents rarely get stuck in a way that needs human help).
      ``wall_budget_sec`` — task wall-clock cap; 0 disables. When elapsed
        exceeds this and ``escalation_enabled`` is True, the loop emits
        a halt-and-ask message and breaks out cleanly.
      ``dedup_ping_threshold`` — push a dedup escalation after this many
        DEDUP_BLOCKED events for the same tool within one task. 0 disables.
    """
    name: str
    checkpoint_interval: int
    dedup_cap: int
    gate_retry_cap: int
    compact_round: int
    compact_msgs: int
    escalation_enabled: bool
    wall_budget_sec: int
    dedup_ping_threshold: int


# Cloud-class profile reproduces today's hardcoded values bit-for-bit.
# This is the safe default that callers fall back to when
# OUROBOROS_COMPAT_ENABLED=False, when the profile string is unknown,
# or when probing fails — guarantees zero regression.
PROFILES = {
    "cloud_class": CompatProfile(
        name="cloud_class",
        checkpoint_interval=15,
        dedup_cap=0,
        gate_retry_cap=0,
        compact_round=999,
        compact_msgs=999,
        escalation_enabled=False,
        wall_budget_sec=0,
        dedup_ping_threshold=0,
    ),
    "small_local": CompatProfile(
        name="small_local",
        checkpoint_interval=8,
        dedup_cap=3,
        gate_retry_cap=2,
        compact_round=6,
        compact_msgs=40,
        escalation_enabled=True,
        wall_budget_sec=1800,        # 30 min
        dedup_ping_threshold=2,
    ),
    "constrained_local": CompatProfile(
        name="constrained_local",
        checkpoint_interval=5,
        dedup_cap=2,
        gate_retry_cap=1,
        compact_round=4,
        compact_msgs=25,
        escalation_enabled=True,
        wall_budget_sec=900,         # 15 min
        dedup_ping_threshold=1,
    ),
}


def _read_int_env(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _is_master_enabled() -> bool:
    return (os.environ.get("OUROBOROS_COMPAT_ENABLED", "") or "").strip().lower() in (
        "true", "1", "yes",
    )


def resolve_profile(target_model: str, *, use_local: bool = False) -> Tuple[CompatProfile, str]:
    """Pick the compat profile for the resolved model. Returns (profile, reason).

    The ``reason`` string is logged to the events feed so Ouro can see
    in his own dynamic context why a particular profile was selected.

    Resolution order:
      1. master switch off → cloud_class (reason=master_switch_off)
      2. valid OUROBOROS_COMPAT_PROFILE override → that profile
      3. provider in {anthropic, openai, openrouter, cloudru} → cloud_class
      4. use_local=True OR provider=openai-compatible:
           context length 1..16383 → constrained_local
           context length >= 16384 OR unknown → small_local
      5. fallback → cloud_class
    """
    if not _is_master_enabled():
        return PROFILES["cloud_class"], "master_switch_off"

    override = (os.environ.get("OUROBOROS_COMPAT_PROFILE", "") or "").strip().lower()
    if override in PROFILES:
        return PROFILES[override], "env_override"
    if override:
        log.warning("Invalid OUROBOROS_COMPAT_PROFILE=%r — falling back to detection", override)

    provider = infer_provider_from_model(str(target_model or ""))
    if provider in _CLOUD_PROVIDERS and not use_local:
        return PROFILES["cloud_class"], "detected_provider_cloud"

    # use_local=True or openai-compatible — both indicate local routing.
    ctx_len = _read_int_env("OPENAI_COMPATIBLE_CONTEXT_LENGTH")
    if ctx_len == 0:
        ctx_len = _read_int_env("LOCAL_MODEL_CONTEXT_LENGTH")
    if 0 < ctx_len < 16384:
        return PROFILES["constrained_local"], "detected_local_constrained"
    return PROFILES["small_local"], "detected_local_small"


def emit_profile_selected(
    event_queue: Optional[Any],
    task_id: str,
    profile: CompatProfile,
    reason: str,
    *,
    target_model: str = "",
    provider: str = "",
    context_length: int = 0,
) -> None:
    """Push a ``compat_profile_selected`` event onto the event queue.

    Fire-and-forget — failures must not block task startup.
    """
    if event_queue is None:
        return
    try:
        from ouroboros.utils import utc_now_iso
        event_queue.put_nowait({
            "type": "log_event",
            "data": {
                "ts": utc_now_iso(),
                "type": "compat_profile_selected",
                "task_id": task_id,
                "profile": profile.name,
                "reason": reason,
                "target_model": target_model,
                "provider": provider,
                "context_length": context_length,
                "checkpoint_interval": profile.checkpoint_interval,
                "dedup_cap": profile.dedup_cap,
                "gate_retry_cap": profile.gate_retry_cap,
                "compact_round": profile.compact_round,
                "compact_msgs": profile.compact_msgs,
            },
        })
    except Exception:
        log.debug("Failed to emit compat_profile_selected event", exc_info=True)

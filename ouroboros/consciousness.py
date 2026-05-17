"""
Ouroboros — Background Consciousness.

A persistent thinking loop that runs between tasks, giving the agent
continuous presence rather than purely reactive behavior.

The consciousness:
- Wakes periodically (interval decided by the LLM via set_next_wakeup)
- Loads scratchpad, identity, recent events
- Calls the LLM with a lightweight introspection prompt
- Has access to a subset of tools (memory, messaging, scheduling)
- Can message the user proactively
- Can schedule tasks for itself
- Pauses when a regular task is running
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from ouroboros.loop_tool_execution import StatefulToolExecutor, _truncate_tool_result
from ouroboros.utils import (
    append_jsonl,
    emit_log_event,
    read_text,
    sanitize_tool_args_for_log,
    sanitize_tool_result_for_log,
    truncate_for_log,
    utc_now_iso,
)
from ouroboros.config import get_light_model, resolve_effort
from ouroboros.llm import LLMClient
from ouroboros.memory import Memory
from ouroboros.context import (
    build_runtime_section, build_memory_sections,
    build_recent_sections, build_health_invariants,
    build_knowledge_sections, safe_read,
)

log = logging.getLogger(__name__)


class BackgroundConsciousness:
    """Persistent background thinking loop for Ouroboros."""

    def __init__(
        self,
        drive_root: pathlib.Path,
        repo_dir: pathlib.Path,
        event_queue: Any,
        owner_chat_id_fn: Callable[[], Optional[int]],
    ):
        self._drive_root = drive_root
        self._repo_dir = repo_dir
        self._event_queue = event_queue
        self._owner_chat_id_fn = owner_chat_id_fn

        self._max_bg_rounds = int(os.environ.get("OUROBOROS_BG_MAX_ROUNDS", "10"))
        self._wakeup_min = int(os.environ.get("OUROBOROS_BG_WAKEUP_MIN", "30"))
        self._wakeup_max = int(os.environ.get("OUROBOROS_BG_WAKEUP_MAX", "7200"))

        self._llm = LLMClient()
        self._registry = self._build_registry()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._next_wakeup_sec: float = 300.0
        self._observations: queue.Queue = queue.Queue(maxsize=100)
        self._deferred_events: list = []
        self._tool_executor = StatefulToolExecutor()

        # Budget tracking
        self._bg_spent_usd: float = 0.0
        self._bg_budget_pct: float = float(
            os.environ.get("OUROBOROS_BG_BUDGET_PCT", "10")
        )
        self._last_cycle_started_at: str = ""
        self._last_cycle_finished_at: str = ""
        self._last_idle_reason: str = "stopped"
        self._last_error: str = ""

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def _model(self) -> str:
        return get_light_model()

    def status_snapshot(self) -> Dict[str, Any]:
        return {
            "running": bool(self.is_running),
            "paused": bool(self._paused),
            "next_wakeup_sec": int(self._next_wakeup_sec),
            "last_cycle_started_at": self._last_cycle_started_at,
            "last_cycle_finished_at": self._last_cycle_finished_at,
            "last_idle_reason": self._last_idle_reason,
            "last_error": self._last_error,
        }

    def start(self) -> str:
        if self.is_running:
            return "Background consciousness is already running."
        self._running = True
        self._paused = False
        self._last_idle_reason = "starting"
        self._last_error = ""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return "Background consciousness started."

    def stop(self) -> str:
        if not self.is_running:
            return "Background consciousness is not running."
        self._running = False
        self._last_idle_reason = "stopping"
        self._stop_event.set()
        self._wakeup_event.set()  # Unblock sleep
        try:
            self._tool_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            log.debug("Failed to shutdown consciousness tool executor", exc_info=True)
        return "Background consciousness stopping."

    def pause(self) -> None:
        """Pause during task execution to avoid budget contention."""
        self._paused = True
        self._last_idle_reason = "paused_by_active_task"

    def resume(self) -> None:
        """Resume after task completes. Flush any deferred events first."""
        if self._deferred_events and self._event_queue is not None:
            for evt in self._deferred_events:
                self._event_queue.put(evt)
            self._deferred_events.clear()
        self._paused = False
        self._last_idle_reason = "waking"
        self._wakeup_event.set()

    def inject_observation(self, text: str) -> None:
        """Push an event the consciousness should notice."""
        try:
            self._observations.put_nowait(text)
        except queue.Full:
            pass

    def _emit_live_log(self, event_type: str, **fields: Any) -> None:
        emit_log_event(
            self._event_queue,
            {
                "type": event_type,
                "ts": utc_now_iso(),
                "task_id": "bg-consciousness",
                "task_type": "consciousness",
                **fields,
            },
            blocking=True,
            log_label="consciousness live",
        )

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _loop(self) -> None:
        """Daemon thread: sleep → wake → think → sleep."""
        while not self._stop_event.is_set():
            # Wait for next wakeup
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self._next_wakeup_sec)

            if self._stop_event.is_set():
                break

            # Skip if paused (task running)
            if self._paused:
                self._last_idle_reason = "paused_by_active_task"
                continue

            # Budget check
            if not self._check_budget():
                self._last_idle_reason = "budget_blocked"
                self._next_wakeup_sec = self._wakeup_max
                continue

            try:
                self._last_cycle_started_at = utc_now_iso()
                self._last_idle_reason = "thinking"
                self._last_error = ""
                cycle_completed = self._think()
                self._last_cycle_finished_at = utc_now_iso()
                # Only set 'sleeping' for normal completions.
                # Context overflow or LLM errors set their own distinct status inside _think().
                if cycle_completed and not self._stop_event.is_set() and not self._paused:
                    self._last_idle_reason = "sleeping"
            except Exception as e:
                self._last_cycle_finished_at = utc_now_iso()
                self._last_idle_reason = "error_backoff"
                self._last_error = repr(e)
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_error",
                    "error": repr(e),
                    "traceback": traceback.format_exc()[:1500],
                })
                self._next_wakeup_sec = min(
                    self._next_wakeup_sec * 2, self._wakeup_max
                )
        self._last_idle_reason = "stopped"

    def _check_budget(self) -> bool:
        """Check if background consciousness is within its budget allocation."""
        try:
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            if total_budget <= 0:
                return True
            max_bg = total_budget * (self._bg_budget_pct / 100.0)
            return self._bg_spent_usd < max_bg
        except Exception:
            log.warning("Failed to check background consciousness budget", exc_info=True)
            return True

    # -------------------------------------------------------------------
    # Think cycle
    # -------------------------------------------------------------------

    def _think(self) -> bool:
        """One thinking cycle: build context, call LLM, execute tools iteratively.

        Returns True if the cycle completed normally, False if it was skipped
        (e.g. context overflow).  _loop() uses this to set a distinct status
        instead of overwriting last_idle_reason with 'sleeping'.
        """
        try:
            context = self._build_context()
        except OverflowError as exc:
            # Context too large — skip this wakeup cycle entirely (P1: no silent truncation).
            log.warning("consciousness: wakeup cycle skipped: %s", exc)
            self._last_idle_reason = "context_overflow"
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_context_overflow",
                "error": str(exc),
            })
            return False
        model = self._model

        tools = self._tool_schemas()
        messages = [
            {"role": "system", "content": context},
            {"role": "user", "content": "Wake up. Think."},
        ]

        total_cost = 0.0
        final_content = ""
        round_idx = 0
        all_pending_events = []  # Accumulate events across all tool calls

        try:
            for round_idx in range(1, self._max_bg_rounds + 1):
                if self._paused:
                    break
                _use_local_light = os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1")
                self._emit_live_log(
                    "llm_round_started",
                    round=round_idx,
                    attempt=1,
                    model=model,
                    reasoning_effort="low",
                    use_local=bool(_use_local_light),
                )
                msg, usage = self._llm.chat(
                    messages=messages,
                    model=model,
                    tools=tools,
                    reasoning_effort=resolve_effort("consciousness"),
                    max_tokens=65536,
                    use_local=_use_local_light,
                )
                cost = float(usage.get("cost") or 0)
                total_cost += cost
                self._bg_spent_usd += cost

                # Global budget update happens via event queue → events.py _handle_llm_usage.
                # Do NOT call update_budget_from_usage directly here — that would double-count.

                # Budget check between rounds
                if not self._check_budget():
                    self._last_idle_reason = "budget_blocked"
                    append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                        "ts": utc_now_iso(),
                        "type": "bg_budget_exceeded_mid_cycle",
                        "round": round_idx,
                    })
                    break

                # Report usage to supervisor
                if self._event_queue is not None:
                    provider = "local" if _use_local_light else "openrouter"
                    model_name = f"{model} (local)" if _use_local_light else model
                    self._event_queue.put({
                        "type": "llm_usage",
                        "provider": provider,
                        "model": model_name,
                        "usage": usage,
                        "cost": cost,
                        "source": "consciousness",
                        "ts": utc_now_iso(),
                        "category": "consciousness",
                    })

                content = msg.get("content") or ""
                tool_calls = msg.get("tool_calls") or []
                self._emit_live_log(
                    "llm_round_finished",
                    round=round_idx,
                    attempt=1,
                    model=model,
                    reasoning_effort="low",
                    prompt_tokens=int(usage.get("prompt_tokens") or 0),
                    completion_tokens=int(usage.get("completion_tokens") or 0),
                    cached_tokens=int(usage.get("cached_tokens") or 0),
                    cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
                    cost_usd=cost,
                    response_kind="tool_calls" if tool_calls else "message",
                    tool_call_count=len(tool_calls),
                    has_text=bool(content.strip()),
                )

                self._emit_progress(content)

                if self._paused:
                    break

                # If we have content but no tool calls, we're done
                if content and not tool_calls:
                    final_content = content
                    break

                # If we have tool calls, execute them and continue loop
                if tool_calls:
                    messages.append(msg)
                    for tc in tool_calls:
                        result = self._execute_tool(tc, all_pending_events)
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id", ""),
                            "content": result,
                        })
                    continue

                # If neither content nor tool_calls, stop
                break

            # Forward or defer accumulated events
            if all_pending_events and self._event_queue is not None:
                if self._paused:
                    self._deferred_events.extend(all_pending_events)
                else:
                    for evt in all_pending_events:
                        self._event_queue.put(evt)

            # Log the thought with round count
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_thought",
                "thought_preview": (final_content or "")[:300],
                "cost_usd": total_cost,
                "rounds": round_idx,
                "model": model,
            })

        except Exception as e:
            self._emit_live_log("llm_round_error", round=round_idx, model=model, error=repr(e))
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_llm_error",
                "error": repr(e),
            })
            self._last_idle_reason = "llm_error"
            # Apply exponential backoff so persistent provider/tool failures don't
            # keep waking at the normal interval (mirrors _loop()'s error_backoff path).
            self._next_wakeup_sec = min(self._next_wakeup_sec * 2, self._wakeup_max)
            return False

        return True

    def _emit_progress(self, content: str) -> None:
        if not content or not content.strip():
            return
        chat_id = self._owner_chat_id_fn()
        entry = {
            "type": "send_message",
            "chat_id": chat_id,
            "text": f"💬 {content.strip()}",
            "format": "markdown",
            "ts": utc_now_iso(),
            "task_id": "bg-consciousness",
            "content": content.strip(),
            "is_progress": True,
        }
        persist_locally = self._event_queue is None or chat_id is None
        # 1. UI event queue (only if we have a chat_id)
        if self._event_queue is not None and chat_id is not None:
            try:
                if self._paused:
                    self._deferred_events.append(entry)
                else:
                    self._event_queue.put(entry)
            except Exception:
                log.warning("Failed to emit progress event", exc_info=True)
                persist_locally = False
        # 2. Persist directly only when the event will not go through supervisor
        if persist_locally:
            append_jsonl(self._drive_root / "logs" / "progress.jsonl", entry)

    # -------------------------------------------------------------------
    # Context building (lightweight)
    # -------------------------------------------------------------------

    def _load_bg_prompt(self) -> str:
        """Load consciousness system prompt from file."""
        prompt_path = self._repo_dir / "prompts" / "CONSCIOUSNESS.md"
        if prompt_path.exists():
            return read_text(prompt_path)
        return "You are Ouroboros in background consciousness mode. Think."

    def _build_context(self) -> str:
        from ouroboros.agent import Env
        env = Env(repo_dir=self._repo_dir, drive_root=self._drive_root)
        memory = Memory(drive_root=self._drive_root, repo_dir=self._repo_dir)
        bg_task = {"id": "bg-consciousness", "type": "consciousness"}

        parts = [self._load_bg_prompt()]

        # BIBLE.md — full
        bible_md = safe_read(env.repo_path("BIBLE.md"))
        if bible_md:
            parts.append("## BIBLE.md\n\n" + bible_md)

        # Section size warning threshold — defined early so all sections can use it.
        _BG_SECTION_WARN_CHARS = 200_000  # warn if a single section exceeds ~50K tokens

        # ARCHITECTURE.md — full (core cognitive artifact; must not be omitted)
        # Per docs/DEVELOPMENT.md Core Governance Artifacts invariant: log a warning
        # if the file is missing so the operator knows context is incomplete.
        architecture_md = safe_read(env.repo_path("docs/ARCHITECTURE.md"))
        if architecture_md:
            if len(architecture_md) > _BG_SECTION_WARN_CHARS:
                import logging as _logging
                _logging.getLogger(__name__).warning(
                    "consciousness: ARCHITECTURE.md is large (%d chars) — "
                    "consider restructuring if this consistently causes context overflow",
                    len(architecture_md),
                )
            parts.append("## ARCHITECTURE.md\n\n" + architecture_md)
        else:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "consciousness: docs/ARCHITECTURE.md not found or empty — "
                "background consciousness is operating without architectural context"
            )

        # Memory sections: scratchpad, identity, dialogue summary (full size)
        parts.extend(build_memory_sections(memory))

        parts.extend(
            build_knowledge_sections(
                env,
                warn_large=True,
                pattern_header="## Pattern Register",
            )
        )

        try:
            from ouroboros.improvement_backlog import format_backlog_digest

            backlog_digest = format_backlog_digest(self._drive_root, limit=8, max_chars=4000)
            if backlog_digest:
                parts.append(backlog_digest)
        except Exception:
            log.debug("Failed to include improvement backlog in consciousness context", exc_info=True)

        # Health invariants
        health_section = build_health_invariants(env)
        if health_section:
            parts.append(health_section)

        # Drive state — full content, no clip_text.
        state_json = safe_read(env.drive_path("state/state.json"), fallback="{}")
        if len(state_json) > _BG_SECTION_WARN_CHARS:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "consciousness: drive state JSON is large (%d chars)", len(state_json)
            )
        parts.append("## Drive state\n\n" + state_json)

        # Runtime section (same as main agent)
        parts.append(build_runtime_section(env, bg_task))

        # Recent sections — empty task_id so we get ALL tasks' progress/tools/events
        parts.extend(build_recent_sections(memory, env, task_id=""))

        # Recent observations (consciousness-specific)
        observations = []
        while not self._observations.empty():
            try:
                observations.append(self._observations.get_nowait())
            except queue.Empty:
                break
        if observations:
            parts.append("## Recent observations\n\n" + "\n".join(
                f"- {o}" for o in observations[-10:]))

        # BG-specific runtime info
        bg_info_lines = [
            f"BG budget spent: ${self._bg_spent_usd:.4f}",
            f"Current wakeup interval: {self._next_wakeup_sec}s",
            f"Current model: {self._model}",
        ]
        parts.append("## Background consciousness info\n\n" + "\n".join(bg_info_lines))

        # Overflow guard (P1: cognitive artifacts must not be silently dropped).
        # BIBLE P1: compaction only through explicit summarization preserving substance.
        # Hard limit: if context would exceed the safe budget, fail this wakeup cycle
        # fast with a clear error rather than truncating or omitting cognitive content.
        # Warn-only threshold allows detecting growth before it becomes a hard failure.
        _BG_TOTAL_WARN_CHARS = 600_000   # ~150K tokens — warn but proceed
        _BG_TOTAL_MAX_CHARS = 1_200_000  # ~300K tokens — fail fast (P1 compliance)
        full_text = "\n\n".join(parts)
        if len(full_text) > _BG_TOTAL_MAX_CHARS:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "consciousness: context too large (%d chars > %d limit) — "
                "skipping wakeup cycle; groom memory (knowledge, patterns, scratchpad) "
                "to reduce size",
                len(full_text), _BG_TOTAL_MAX_CHARS,
            )
            # Raise so _think() / _run() can catch and log without dropping artifacts.
            raise OverflowError(
                f"Background consciousness context too large ({len(full_text):,} chars). "
                "Groom memory to continue."
            )
        if len(full_text) > _BG_TOTAL_WARN_CHARS:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "consciousness: context is large (%d chars) — consider grooming memory",
                len(full_text),
            )
        return full_text

    # -------------------------------------------------------------------
    # Tool registry (separate instance for consciousness, not shared with agent)
    # -------------------------------------------------------------------

    _BG_TOOL_WHITELIST = frozenset({
        # Memory & identity
        "send_user_message", "schedule_task", "update_scratchpad",
        "update_identity", "set_next_wakeup",
        # Knowledge base
        "knowledge_read", "knowledge_write", "knowledge_list",
        # Read-only tools for awareness
        "web_search", "repo_read", "repo_list", "data_read", "data_list",
        "chat_history",
        # GitHub Issues
        "list_github_issues", "get_github_issue",
    })

    def _build_registry(self) -> "ToolRegistry":
        """Create a ToolRegistry scoped to consciousness-allowed tools."""
        from ouroboros.tools.registry import ToolRegistry, ToolContext, ToolEntry

        registry = ToolRegistry(repo_dir=self._repo_dir, drive_root=self._drive_root)

        # Register consciousness-specific tool (modifies self._next_wakeup_sec)
        def _set_next_wakeup(ctx: Any, seconds: int = 300) -> str:
            self._next_wakeup_sec = max(self._wakeup_min, min(self._wakeup_max, int(seconds)))
            return f"OK: next wakeup in {self._next_wakeup_sec}s"

        registry.register(ToolEntry("set_next_wakeup", {
            "name": "set_next_wakeup",
            "description": "Set how many seconds until your next thinking cycle. "
                           "Default 300. Range: 60-3600.",
            "parameters": {"type": "object", "properties": {
                "seconds": {"type": "integer",
                            "description": "Seconds until next wakeup (60-3600)"},
            }, "required": ["seconds"]},
        }, _set_next_wakeup))

        return registry

    def _tool_schemas(self) -> List[Dict[str, Any]]:
        """Return tool schemas filtered to the consciousness whitelist."""
        return [
            s for s in self._registry.schemas()
            if s.get("function", {}).get("name") in self._BG_TOOL_WHITELIST
        ]

    def _execute_tool(self, tc: Dict[str, Any], all_pending_events: List[Dict[str, Any]]) -> str:
        """Execute a consciousness tool call with timeout. Returns result string."""
        fn_name = tc.get("function", {}).get("name", "")
        if fn_name not in self._BG_TOOL_WHITELIST:
            return f"Tool {fn_name} not available in background mode."
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, ValueError):
            return "Failed to parse arguments."

        self._emit_live_log(
            "tool_call_started",
            tool=fn_name,
            args=sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {}),
            timeout_sec=self._registry.get_timeout(fn_name),
        )

        chat_id = self._owner_chat_id_fn()
        self._registry._ctx.current_chat_id = chat_id
        self._registry._ctx.pending_events = []

        timeout_sec = self._registry.get_timeout(fn_name)
        result = None
        error = None
        timed_out = False

        def _run_tool():
            nonlocal result, error
            try:
                result = self._registry.execute(fn_name, args)
            except Exception as e:
                error = e

        future = self._tool_executor.submit(_run_tool)
        try:
            future.result(timeout=timeout_sec)
        except (TimeoutError, concurrent.futures.TimeoutError):
            self._tool_executor.reset()
            timed_out = True
            result = f"[TIMEOUT after {timeout_sec}s]"
            self._emit_live_log(
                "tool_call_timeout",
                tool=fn_name,
                args=sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {}),
                timeout_sec=timeout_sec,
            )
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_tool_timeout",
                "tool": fn_name,
                "timeout_sec": timeout_sec,
            })

        if error is not None:
            self._emit_live_log(
                "tool_call_finished",
                tool=fn_name,
                args=sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {}),
                is_error=True,
                result_preview=repr(error),
            )
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_tool_error",
                "tool": fn_name,
                "error": repr(error),
            })
            result = f"Error: {repr(error)}"

        for evt in self._registry._ctx.pending_events:
            all_pending_events.append(evt)

        result_str = _truncate_tool_result(
            result,
            tool_name=fn_name,
            tool_args=args if isinstance(args, dict) else {},
        )

        args_for_log = sanitize_tool_args_for_log(fn_name, args)
        if error is None and result is not None and not timed_out:
            self._emit_live_log(
                "tool_call_finished",
                tool=fn_name,
                args=args_for_log,
                is_error=False,
                result_preview=sanitize_tool_result_for_log(truncate_for_log(result_str, 500)),
            )
        append_jsonl(self._drive_root / "logs" / "tools.jsonl", {
            "ts": utc_now_iso(),
            "tool": fn_name,
            "source": "consciousness",
            "args": args_for_log,
            "result_preview": sanitize_tool_result_for_log(truncate_for_log(result_str, 2000)),
        })

        return result_str

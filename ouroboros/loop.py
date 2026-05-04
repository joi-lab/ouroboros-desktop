"""
Ouroboros — LLM tool loop.

Core loop: send messages to LLM, execute tool calls, repeat until final response.
Extracted from agent.py to keep the agent thin.
"""

from __future__ import annotations

import json
import os
import queue
import pathlib
from typing import Any, Callable, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, normalize_reasoning_effort, add_usage
from ouroboros.tool_policy import initial_tool_schemas, list_non_core_tools
from ouroboros.tools.registry import ToolRegistry
from ouroboros.context import build_user_content
from ouroboros.context_compaction import compact_tool_history_llm
from ouroboros.utils import estimate_tokens

from ouroboros.loop_tool_execution import (
    StatefulToolExecutor,
    handle_tool_calls,
    _truncate_tool_result,
    _TOOL_RESULT_LIMITS,
    _DEFAULT_TOOL_RESULT_LIMIT,
)
from ouroboros.loop_llm_call import call_llm_with_retry, emit_llm_usage_event, estimate_cost

# Backward-compat alias for source-inspecting and monkeypatched tests
_call_llm_with_retry = call_llm_with_retry

log = logging.getLogger(__name__)


def _estimate_messages_chars(messages: List[Dict[str, Any]]) -> int:
    """Estimate the serialised size of a message list in characters.

    Counts all serialised fields that actually reach the provider:
    - message `content` (string or multipart list)
    - `tool_calls` (serialised to JSON)
    - `tool_call_id`
    This deliberately excludes the static system-prompt block (built once in
    context.py and amortised across rounds by prompt caching), focusing on
    the mutable per-round portion of the transcript which is what grows
    unboundedly during long tasks.
    """
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    # Serialise the whole block so non-text types (image_url, etc.)
                    # are counted — not just the "text" field.
                    try:
                        import json as _json2
                        total += len(_json2.dumps(block, ensure_ascii=False))
                    except (TypeError, ValueError):
                        total += len(str(block))
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            try:
                import json as _json
                total += len(_json.dumps(tool_calls, ensure_ascii=False))
            except (TypeError, ValueError):
                total += sum(len(str(tc)) for tc in tool_calls)
        tc_id = msg.get("tool_call_id")
        if tc_id:
            total += len(str(tc_id))
    return total


def _provider_failure_hint(accumulated_usage: Dict[str, Any]) -> str:
    detail = " ".join(str(accumulated_usage.get("_last_llm_error") or "").split()).strip()
    if not detail:
        return ""
    return f" Last provider error: {detail}"


def _handle_text_response(
    content: Optional[str],
    llm_trace: Dict[str, Any],
    accumulated_usage: Dict[str, Any],
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """Handle LLM response without tool calls (final response)."""
    if content and content.strip():
        llm_trace["reasoning_notes"].append(content.strip())
    return (content or ""), accumulated_usage, llm_trace


def _check_budget_limits(
    budget_remaining_usd: Optional[float],
    accumulated_usage: Dict[str, Any],
    round_idx: int,
    messages: List[Dict[str, Any]],
    llm: LLMClient,
    active_model: str,
    active_effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    event_queue: Optional[queue.Queue],
    llm_trace: Dict[str, Any],
    task_type: str = "task",
    use_local: bool = False,
) -> Optional[Tuple[str, Dict[str, Any], Dict[str, Any]]]:
    """
    Check budget limits and handle budget overrun.

    Returns:
        None if budget is OK (continue loop)
        (final_text, accumulated_usage, llm_trace) if budget exceeded (stop loop)
    """
    if budget_remaining_usd is None:
        return None

    task_cost = accumulated_usage.get("cost", 0)

    if budget_remaining_usd <= 0:
        finish_reason = f"🚫 Task rejected. Total budget exhausted. Please increase TOTAL_BUDGET in settings."
        return finish_reason, accumulated_usage, llm_trace

    budget_pct = task_cost / budget_remaining_usd if budget_remaining_usd > 0 else 1.0

    per_task_limit = float(os.environ.get("OUROBOROS_PER_TASK_COST_USD", "20.0") or 20.0)
    if task_cost >= per_task_limit and round_idx % 10 == 0:
        messages.append({
            "role": "user",
            "content": f"[COST NOTE] Task spent ${task_cost:.3f}, which is at or above the per-task soft threshold of ${per_task_limit:.2f}. Continue only if the expected value still justifies the cost.",
        })

    if budget_pct > 0.5:
        finish_reason = f"Task spent ${task_cost:.3f} (>50% of remaining ${budget_remaining_usd:.2f}). Budget exhausted."
        messages.append({"role": "user", "content": f"[BUDGET LIMIT] {finish_reason} Give your final response now."})
        try:
            final_msg, final_cost = _call_llm_with_retry(
                llm, messages, active_model, None, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                use_local=use_local,
            )
            if final_msg:
                return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
            return finish_reason, accumulated_usage, llm_trace
        except Exception:
            log.warning("Failed to get final response after budget limit", exc_info=True)
            return finish_reason, accumulated_usage, llm_trace
    elif budget_pct > 0.3 and round_idx % 10 == 0:
        messages.append({"role": "user", "content": f"[INFO] Task spent ${task_cost:.3f} of ${budget_remaining_usd:.2f}. Wrap up if possible."})

    return None


def _build_recent_tool_trace(messages: List[Dict[str, Any]], window: int = 15) -> str:
    """Build a factual trace of recent tool calls for the LLM to evaluate.

    Returns a compact trace string showing the last N tool calls with their
    names and argument summaries. The LLM uses this to assess whether
    it is making progress or repeating the same actions (P5 LLM-First).
    """
    all_calls: List[str] = []
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", "")
                if isinstance(args, dict):
                    args = json.dumps(args, sort_keys=True)
                args_str = str(args)
                summary = f"{name}({args_str[:80]})" if len(args_str) > 80 else f"{name}({args_str})"
                all_calls.append(summary)
    recent = all_calls[-window:] if all_calls else []
    if not recent:
        return ""
    return "Recent tool calls (oldest first):\n" + "\n".join(f"  {i+1}. {c}" for i, c in enumerate(recent))


def _emit_checkpoint_event(
    event_queue: Optional[queue.Queue],
    task_id: str,
    drive_logs: Optional[pathlib.Path],
    data: Dict[str, Any],
) -> None:
    """Emit a task_checkpoint event for observability.

    Routes via the supervisor event queue when available (which persists to
    events.jsonl). Falls back to direct append when queue is absent.
    """
    from ouroboros.loop_llm_call import _emit_live_log
    payload = {"type": "task_checkpoint", "task_id": task_id, **data}
    if event_queue is not None:
        _emit_live_log(event_queue, payload)
    elif drive_logs:
        try:
            from ouroboros.utils import append_jsonl, utc_now_iso
            append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), **payload})
        except Exception:
            pass


def _extract_plain_text_from_content(content: Any) -> str:
    """Extract plain text from either a string or a multipart content list.

    Used by seal_task_transcript to compute prefix token estimates and to
    flatten previously-sealed multipart tool messages back to plain strings.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content) if content is not None else ""


def _maybe_inject_self_check(
    round_idx: int,
    max_rounds: int,
    messages: List[Dict[str, Any]],
    accumulated_usage: Dict[str, Any],
    emit_progress: Callable[[str], None],
    *,
    event_queue: Optional[queue.Queue] = None,
    task_id: str = "",
    drive_logs: Optional[pathlib.Path] = None,
) -> bool:
    """Inject a periodic self-check user message every REMINDER_INTERVAL rounds.

    Contract: this is a plain `user` message inserted into the transcript carrying
    round/cost/context summary, the last-N tool-call trace, and a short directed
    self-check prompt. The next LLM round runs normally — tools remain enabled,
    reasoning effort is unchanged, and the message flows through normal compaction.
    There is no structured reflection format, no audit-only mode, and no anomaly
    classification: the model reads the message like any other user turn and
    decides whether to continue, narrow scope, or wrap up.

    Rationale for the minimalist design: the previous structured-reflection
    mechanism (Known/Blocker/Decision/Next four-field contract, tools disabled,
    effort=xhigh) produced 0 valid reflections and 37 task_checkpoint_anomaly
    records in production logs before this rewrite — the ceremony competed
    with the model's actual work without adding usable signal.

    Emits a single task_checkpoint event for observability.
    Returns True if a checkpoint was injected (caller uses this for logging).
    """
    REMINDER_INTERVAL = 15
    if round_idx <= 1 or round_idx % REMINDER_INTERVAL != 0 or round_idx >= max_rounds:
        return False

    ctx_tokens = sum(
        estimate_tokens(str(m.get("content", "")))
        if isinstance(m.get("content"), str)
        else sum(estimate_tokens(str(b.get("text", ""))) for b in m.get("content", []) if isinstance(b, dict))
        for m in messages
    )
    task_cost = accumulated_usage.get("cost", 0)
    checkpoint_num = round_idx // REMINDER_INTERVAL

    tool_trace = _build_recent_tool_trace(messages)

    reminder = (
        f"[CHECKPOINT {checkpoint_num} — round {round_idx}/{max_rounds}]\n"
        f"Context: ~{ctx_tokens} tokens | Cost so far: ${task_cost:.2f} | "
        f"Rounds remaining: {max_rounds - round_idx}\n"
    )
    if tool_trace:
        reminder += f"\n{tool_trace}\n"
    reminder += (
        "\nThis is a periodic self-check, not a command to stop. "
        "Glance at your recent tool-call trace above and briefly consider:\n"
        "- Are you still making progress toward the task, or repeating the same actions?\n"
        "- Is the current approach still the right one, or should you narrow scope / try a different angle?\n"
        "- If the task is effectively done, wrap up by replying with your final answer in plain text (no tool call). "
        "Otherwise continue with the most valuable next step.\n"
        "\nNo special format required — just think, then act."
    )

    # Defense-in-depth against consecutive same-role messages: if the previous
    # message is already a `user` turn (for example: an owner message drained
    # by `_drain_incoming_messages` in the same loop iteration), merge the
    # checkpoint reminder into that turn instead of appending a second user
    # message. Anthropic's Messages API rejects consecutive user messages
    # with a 400, and OpenRouter routes Anthropic models through that path —
    # a bare append could cause a checkpoint round to lose its LLM call.
    #
    # When the prior user message carries multipart content (e.g. image_url
    # blocks from the photo bridge, or cache_control-annotated blocks), we
    # append a new `{"type": "text", "text": ...}` block to the existing
    # list instead of flattening to a plain string — flattening would drop
    # image data and cache markers silently, breaking the implicit message
    # format contract with `ouroboros/context.py::build_user_content` and
    # `ouroboros/llm.py::_anthropic_blocks_from_content`.
    if messages and messages[-1].get("role") == "user":
        prior = messages[-1].get("content")
        if isinstance(prior, list):
            messages[-1] = {
                "role": "user",
                "content": list(prior) + [{"type": "text", "text": "\n\n---\n\n" + reminder}],
            }
        else:
            prior_text = prior if isinstance(prior, str) else str(prior or "")
            messages[-1] = {
                "role": "user",
                "content": (prior_text.rstrip() + "\n\n---\n\n" + reminder) if prior_text else reminder,
            }
    else:
        messages.append({"role": "user", "content": reminder})
    emit_progress(
        f"Checkpoint {checkpoint_num} at round {round_idx}: "
        f"~{ctx_tokens} tokens, ${task_cost:.2f} spent"
    )

    _emit_checkpoint_event(event_queue, task_id, drive_logs, {
        "checkpoint_number": checkpoint_num,
        "round": round_idx,
        "max_rounds": max_rounds,
        "context_tokens": ctx_tokens,
        "task_cost": task_cost,
    })

    return True


def seal_task_transcript(
    messages: List[Dict[str, Any]],
    keep_active: int = 5,
    min_prefix_tokens: int = 2048,
) -> None:
    """Seal one stable tool-result message with cache_control to improve prompt cache hits.

    Strategy:
    - First, revert any previously sealed tool message back to a plain string so
      compaction and later rounds always see normal content (not stale multipart blocks).
    - Then identify the last tool-result message that falls BEFORE the active recent
      window (last `keep_active` tool results). That message is the "seal boundary".
    - If the token estimate for content up to and including that message exceeds
      `min_prefix_tokens`, mark it with a multipart cache_control block.
    - Only one sealed boundary exists at a time. Non-Anthropic paths strip
      cache_control in llm.py before sending, so they are unaffected.

    Mutates `messages` in-place. Returns None.
    """
    # Step 1: revert any previously sealed tool messages to plain strings
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            # Was sealed — flatten back to plain text
            msg["content"] = _extract_plain_text_from_content(content)

    # Step 2: collect indices of all tool-result messages
    tool_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
    ]
    if len(tool_indices) <= keep_active:
        # Not enough tool rounds for a stable prefix yet
        return

    # The candidate to seal: last tool result before the active window
    seal_candidate_idx = tool_indices[-(keep_active + 1)]

    # Step 3: estimate prefix token count up to and including the candidate
    prefix_text_len = sum(
        len(_extract_plain_text_from_content(m.get("content", "")))
        for m in messages[: seal_candidate_idx + 1]
    )
    prefix_tokens = prefix_text_len // 4  # rough 4-chars-per-token estimate

    if prefix_tokens < min_prefix_tokens:
        return

    # Step 4: seal the candidate message
    candidate = messages[seal_candidate_idx]
    plain_text = str(candidate.get("content", ""))
    candidate["content"] = [
        {
            "type": "text",
            "text": plain_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _setup_dynamic_tools(tools_registry, tool_schemas, messages):
    """
    Wire tool-discovery handlers onto an existing tool_schemas list.

    Creates closures for list_available_tools / enable_tools, registers them
    as handler overrides, and injects a system message advertising non-core
    tools.  Mutates tool_schemas in-place (via list.append) when tools are
    enabled, so the caller's reference stays live.

    Returns (tool_schemas, enabled_extra_set).
    """
    enabled_extra: set = set()
    active_tool_names = {
        str(schema.get("function", {}).get("name") or "").strip()
        for schema in tool_schemas
        if str(schema.get("function", {}).get("name") or "").strip()
    }

    def _handle_list_tools(ctx=None, **kwargs):
        non_core = [
            t for t in list_non_core_tools(tools_registry)
            if t["name"] not in active_tool_names
        ]
        if not non_core:
            return "All tools are already in your active set."
        lines = [f"**{len(non_core)} additional tools available** (use `enable_tools` to activate):\n"]
        for t in non_core:
            lines.append(f"- **{t['name']}**: {t['description'][:120]}")
        return "\n".join(lines)

    def _handle_enable_tools(ctx=None, tools: str = "", **kwargs):
        names = [n.strip() for n in tools.split(",") if n.strip()]
        enabled, not_found = [], []
        for name in names:
            schema = tools_registry.get_schema_by_name(name)
            if schema and name not in active_tool_names:
                tool_schemas.append(schema)
                enabled_extra.add(name)
                active_tool_names.add(name)
                enabled.append(name)
            elif name in active_tool_names:
                enabled.append(f"{name} (already active)")
            else:
                not_found.append(name)
        parts = []
        if enabled:
            parts.append(f"✅ Enabled: {', '.join(enabled)}")
        if not_found:
            parts.append(f"❌ Not found: {', '.join(not_found)}")
        return "\n".join(parts) if parts else "No tools specified."

    tools_registry.override_handler("list_available_tools", _handle_list_tools)
    tools_registry.override_handler("enable_tools", _handle_enable_tools)

    non_core_count = len(list_non_core_tools(tools_registry))
    if non_core_count > 0:
        messages.append({
            "role": "system",
            "content": (
                f"Note: You have {len(tool_schemas)} core tools loaded. "
                f"There are {non_core_count} additional tools available "
                f"(use `list_available_tools` to see them, `enable_tools` to activate). "
                f"Core tools cover most tasks. Enable extras only when needed."
            ),
        })

    return tool_schemas, enabled_extra


def _drain_incoming_messages(
    messages: List[Dict[str, Any]],
    incoming_messages: queue.Queue,
    drive_root: Optional[pathlib.Path],
    task_id: str,
    event_queue: Optional[queue.Queue],
    _owner_msg_seen: set,
) -> None:
    """Inject owner messages received during task execution."""
    while not incoming_messages.empty():
        try:
            injected = incoming_messages.get_nowait()
            if isinstance(injected, dict):
                messages.append({"role": "user", "content": build_user_content(injected)})
            else:
                messages.append({"role": "user", "content": injected})
        except queue.Empty:
            break

    if drive_root is not None and task_id:
        from ouroboros.owner_inject import drain_owner_messages
        drive_msgs = drain_owner_messages(drive_root, task_id=task_id, seen_ids=_owner_msg_seen)
        for dmsg in drive_msgs:
            messages.append({
                "role": "user",
                "content": f"[Owner message during task]: {dmsg}",
            })
            if event_queue is not None:
                try:
                    event_queue.put_nowait({
                        "type": "owner_message_injected",
                        "task_id": task_id,
                        "text": dmsg,
                    })
                except Exception:
                    pass


def run_llm_loop(
    messages: List[Dict[str, Any]],
    tools: ToolRegistry,
    llm: LLMClient,
    drive_logs: pathlib.Path,
    emit_progress: Callable[[str], None],
    incoming_messages: queue.Queue,
    task_type: str = "",
    task_id: str = "",
    budget_remaining_usd: Optional[float] = None,
    event_queue: Optional[queue.Queue] = None,
    initial_effort: str = "medium",
    drive_root: Optional[pathlib.Path] = None,
) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
    """
    Core LLM-with-tools loop.

    Sends messages to LLM, executes tool calls, retries on errors.
    LLM controls model/effort via switch_model tool (LLM-first, Bible P5).

    Returns: (final_text, accumulated_usage, llm_trace)
    """
    active_model = llm.default_model()
    active_effort = initial_effort
    active_use_local = os.environ.get("USE_LOCAL_MAIN", "").lower() in ("true", "1")

    # Phase 6 auto-routing (2026-05-04): when the agent issues tool calls
    # with is_code_tool=True (repo_write, str_replace_editor, etc.), the
    # next round should run on MODEL_CODE rather than MAIN. Default-on
    # via OUROBOROS_AUTO_ROUTE_CODE; agent's switch_model overrides
    # remain authoritative. Honors BIBLE P5 (LLM-first) by reading a
    # signal the agent's tool calls already produce — agency over the
    # default, not removal of it.
    _auto_route_code = (
        os.environ.get("OUROBOROS_AUTO_ROUTE_CODE", "true").strip().lower()
        in ("true", "1", "yes")
    )
    _model_code = os.environ.get("OUROBOROS_MODEL_CODE", "").strip()
    _model_main = os.environ.get("OUROBOROS_MODEL", "").strip()
    _non_code_streak = 0  # rounds since the last code-tool call

    llm_trace: Dict[str, Any] = {"reasoning_notes": [], "tool_calls": []}
    accumulated_usage: Dict[str, Any] = {}
    max_retries = 3
    from ouroboros.tools import tool_discovery as _td
    _td.set_registry(tools)

    tool_schemas = initial_tool_schemas(tools)
    tool_schemas, _enabled_extra_tools = _setup_dynamic_tools(tools, tool_schemas, messages)

    tools._ctx.event_queue = event_queue
    tools._ctx.task_id = task_id
    tools._ctx.messages = messages
    stateful_executor = StatefulToolExecutor()
    _owner_msg_seen: set = set()
    try:
        MAX_ROUNDS = max(1, int(os.environ.get("OUROBOROS_MAX_ROUNDS", "200")))
    except (ValueError, TypeError):
        MAX_ROUNDS = 200
        log.warning("Invalid OUROBOROS_MAX_ROUNDS, defaulting to 200")
    round_idx = 0
    try:
        while True:
            round_idx += 1

            if round_idx > MAX_ROUNDS:
                finish_reason = f"⚠️ Task exceeded MAX_ROUNDS ({MAX_ROUNDS}). Consider decomposing into subtasks via schedule_task."
                messages.append({"role": "system", "content": f"[ROUND_LIMIT] {finish_reason}"})
                try:
                    final_msg, final_cost = call_llm_with_retry(
                        llm, messages, active_model, None, active_effort,
                        max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                        use_local=active_use_local,
                    )
                    if final_msg:
                        return (final_msg.get("content") or finish_reason), accumulated_usage, llm_trace
                    return finish_reason, accumulated_usage, llm_trace
                except Exception:
                    log.warning("Failed to get final response after round limit", exc_info=True)
                    return finish_reason, accumulated_usage, llm_trace

            ctx = tools._ctx
            if ctx.active_model_override:
                active_model = ctx.active_model_override
                ctx.active_model_override = None
            if getattr(ctx, "active_use_local_override", None) is not None:
                active_use_local = ctx.active_use_local_override
                ctx.active_use_local_override = None
            if ctx.active_effort_override:
                active_effort = normalize_reasoning_effort(ctx.active_effort_override, default=active_effort)
                ctx.active_effort_override = None

            _drain_incoming_messages(messages, incoming_messages, drive_root, task_id, event_queue, _owner_msg_seen)

            # Periodic self-check: inject a user message with round/cost/context
            # summary + recent tool-call trace + a short directed self-check prompt.
            # Ordering note: injection runs AFTER `_drain_incoming_messages` so
            # the checkpoint is always the LAST message before the LLM call.
            # If drain appended a user turn, the merge branch inside
            # `_maybe_inject_self_check` folds the checkpoint reminder into
            # that turn with a `\n\n---\n\n` separator — which both avoids
            # consecutive same-role messages (Anthropic rejects those with 400)
            # and keeps the self-check visible at the tail of the transcript.
            # Not a special round — tools and effort are unchanged, the message
            # flows through the next normal LLM turn. The only coupling with
            # the rest of the loop is a small compaction skip below: running
            # the light-model compactor on the same round we just appended a
            # fresh user message doubles LLM cost for a marginal benefit (the
            # checkpoint message is already inside `keep_recent=50` so
            # compaction would not summarize it anyway).
            _checkpoint_injected = _maybe_inject_self_check(
                round_idx, MAX_ROUNDS, messages, accumulated_usage, emit_progress,
                event_queue=event_queue, task_id=task_id, drive_logs=drive_logs,
            )

            _compaction_usage = None
            pending_compaction = getattr(tools._ctx, '_pending_compaction', None)
            # Compaction policy:
            #
            # Manual compaction (_pending_compaction) and the remote emergency path
            # run UNCONDITIONALLY — even on checkpoint rounds.  Only the routine
            # per-round compaction (local threshold / old "every round > 12" remote
            # path) is suppressed on checkpoint rounds to avoid paying twice for the
            # compaction LLM call on the same turn the self-check was injected.
            #
            # Remote mode (default): automatic semantic compaction is DISABLED for
            # routine rounds.  The previous policy called compact_tool_history_llm
            # every round after round_idx > 12, which meant that once tool-rounds
            # exceeded keep_recent=50 the compactor ran on EVERY round — destroying
            # raw tool outputs, breaking cache continuity, and hurting cache hit rate.
            # Remote models handle large contexts (~400k tokens); preserving exact
            # history is more valuable than saving tokens.
            #
            # Emergency threshold: if the total context grows beyond ~1.2M chars
            # (~300k tokens) we must compact regardless of mode to stay within
            # provider context limits.
            #
            # Local mode: compact at a lower threshold because local models typically
            # have small context windows (8k–32k tokens).

            # --- Manual compaction (always runs) ---
            if pending_compaction is not None:
                messages, _compaction_usage = compact_tool_history_llm(messages, keep_recent=pending_compaction)
                tools._ctx._pending_compaction = None

            # --- Emergency compaction (always runs, catches both remote and local) ---
            elif _estimate_messages_chars(messages) > 1_200_000:
                messages, _compaction_usage = compact_tool_history_llm(messages, keep_recent=50)

            # --- Routine compaction (suppressed on checkpoint rounds) ---
            elif not _checkpoint_injected:
                if active_use_local:
                    # Local models: compact aggressively to fit small context windows.
                    if round_idx > 6 and len(messages) > 40:
                        messages, _compaction_usage = compact_tool_history_llm(messages, keep_recent=20)
                # Remote: no routine compaction — emergency path above handles overflow.
            if tools._ctx.messages is not messages:
                tools._ctx.messages = messages
            if _compaction_usage:
                add_usage(accumulated_usage, _compaction_usage)
                _cm = os.environ.get("OUROBOROS_MODEL_LIGHT") or "anthropic/claude-sonnet-4.6"
                _cc = float(_compaction_usage.get("cost") or 0) or estimate_cost(
                    _cm, int(_compaction_usage.get("prompt_tokens") or 0),
                    int(_compaction_usage.get("completion_tokens") or 0),
                    int(_compaction_usage.get("cached_tokens") or 0))
                emit_llm_usage_event(event_queue, task_id, _cm, _compaction_usage, _cc, "compaction")

            # Seal one stable tool-result boundary for prompt caching (Anthropic-only path;
            # non-Anthropic providers strip cache_control in llm.py).
            seal_task_transcript(messages)

            msg, cost = call_llm_with_retry(
                llm, messages, active_model, tool_schemas, active_effort,
                max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                use_local=active_use_local,
            )

            if msg is None:
                fallback_model = os.environ.get("OUROBOROS_MODEL_FALLBACK", "").strip()
                if not fallback_model or fallback_model == active_model:
                    local_tag = " (local)" if active_use_local else ""
                    return (
                        f"⚠️ Failed to get a response from model {active_model}{local_tag} after {max_retries} attempts. "
                        f"No viable fallback model configured.{_provider_failure_hint(accumulated_usage)} "
                        f"If background consciousness is running, it will retry when the provider recovers."
                    ), accumulated_usage, llm_trace

                fallback_use_local = os.environ.get("USE_LOCAL_FALLBACK", "").lower() in ("true", "1")
                primary_tag = " (local)" if active_use_local else ""
                fallback_tag = " (local)" if fallback_use_local else ""
                emit_progress(f"⚡ Fallback: {active_model}{primary_tag} → {fallback_model}{fallback_tag} after empty response")
                msg, fallback_cost = call_llm_with_retry(
                    llm, messages, fallback_model, tool_schemas, active_effort,
                    max_retries, drive_logs, task_id, round_idx, event_queue, accumulated_usage, task_type,
                    use_local=fallback_use_local,
                )

                if msg is None:
                    return (
                        f"⚠️ All models are down. Primary ({active_model}{primary_tag}) and fallback ({fallback_model}{fallback_tag}) "
                        f"both returned no response. Stopping.{_provider_failure_hint(accumulated_usage)} "
                        f"Background consciousness will attempt recovery when the provider is back."
                    ), accumulated_usage, llm_trace

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls:
                return _handle_text_response(content, llm_trace, accumulated_usage)

            messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

            if content and content.strip():
                emit_progress(content.strip())
                llm_trace["reasoning_notes"].append(content.strip())

            error_count = handle_tool_calls(
                tool_calls, tools, drive_logs, task_id, stateful_executor,
                messages, llm_trace, emit_progress
            )

            # Phase 6 auto-routing: inspect the round's tool calls; if any
            # were is_code_tool=True, route the next round to MODEL_CODE.
            # After OUROBOROS_AUTO_ROUTE_CODE_NON_CODE_STREAK consecutive
            # non-code rounds (default 1), reset to MAIN.
            if _auto_route_code and _model_code and _model_code != _model_main:
                round_calls = llm_trace.get("tool_calls") or []
                round_calls = round_calls[-len(tool_calls):] if tool_calls else []
                _had_code_tool = any(
                    bool(tc.get("is_code_tool")) for tc in round_calls
                )
                if _had_code_tool:
                    _non_code_streak = 0
                    if active_model != _model_code:
                        tools._ctx.active_model_override = _model_code
                else:
                    _non_code_streak += 1
                    _streak_cap = int(os.environ.get(
                        "OUROBOROS_AUTO_ROUTE_CODE_NON_CODE_STREAK", "1"
                    ) or "1")
                    if _non_code_streak >= _streak_cap and active_model != _model_main:
                        tools._ctx.active_model_override = _model_main

            budget_result = _check_budget_limits(
                budget_remaining_usd, accumulated_usage, round_idx, messages,
                llm, active_model, active_effort, max_retries, drive_logs,
                task_id, event_queue, llm_trace, task_type, active_use_local
            )
            if budget_result is not None:
                return budget_result

    finally:
        if stateful_executor:
            try:
                from ouroboros.tools.browser import cleanup_browser
                stateful_executor.submit(cleanup_browser, tools._ctx).result(timeout=5)
            except Exception:
                log.debug("Browser cleanup on executor thread failed or timed out", exc_info=True)
            try:
                stateful_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                log.warning("Failed to shutdown stateful executor", exc_info=True)
        if drive_root is not None and task_id:
            try:
                from ouroboros.owner_inject import cleanup_task_mailbox
                cleanup_task_mailbox(drive_root, task_id)
            except Exception:
                log.debug("Failed to cleanup task mailbox", exc_info=True)

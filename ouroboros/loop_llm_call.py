"""
LLM call, retry, pricing, and usage-event logic for the main loop.

Handles model pricing estimation, cost tracking, per-call retry with backoff,
and real-time usage event emission.
Extracted from loop.py to keep the main loop orchestrator focused.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import time
from typing import Any, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, LocalContextTooLargeError, add_usage
from ouroboros.pricing import emit_llm_usage_event, estimate_cost, infer_model_category
from ouroboros.utils import utc_now_iso, append_jsonl

log = logging.getLogger(__name__)


def _is_context_overflow_error(text: str) -> bool:
    """Detect provider responses that signal a model context overflow.

    Covers the wordings used by LM Studio, OpenAI, OpenRouter, and generic
    OpenAI-compatible servers.
    """
    if not text:
        return False
    t = text.lower()
    markers = (
        "context_length_exceeded",
        "tokens to keep",
        "greater than the context length",
        "maximum context length",
        "exceeds context",
        "this model's maximum context",
    )
    return any(m in t for m in markers)


def _shrink_system_message_progressively(messages: List[Dict[str, Any]]) -> bool:
    """Shrink the system message in-place when a context overflow is detected.

    Order mirrors the sparse-mode philosophy from
    ``ouroboros.context.build_llm_messages``: drop the dynamic block first,
    then the semi-stable block, then halve the static block. Each call
    shrinks by one step. Returns ``True`` if shrinking happened, ``False``
    if the message is already at the constitutional minimum.
    """
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            blocks = [b for b in content if isinstance(b, dict)]
            if len(blocks) >= 3:
                msg["content"] = blocks[:2]
                return True
            if len(blocks) == 2:
                msg["content"] = [blocks[0]]
                return True
            if len(blocks) == 1 and isinstance(blocks[0].get("text"), str):
                t = blocks[0]["text"]
                if len(t) > 8000:
                    blocks[0]["text"] = t[: len(t) // 2] + "\n\n[Static context halved after overflow]"
                    return True
            return False
        if isinstance(content, str):
            if len(content) > 8000:
                msg["content"] = content[: len(content) // 2] + "\n\n[System message halved after overflow]"
                return True
            return False
    return False


def _emit_live_log(event_queue: Optional[queue.Queue], payload: Dict[str, Any]) -> None:
    if not event_queue:
        return
    try:
        event_queue.put_nowait({
            "type": "log_event",
            "data": {"ts": utc_now_iso(), **payload},
        })
    except Exception:
        log.debug("Failed to emit live LLM log event", exc_info=True)


def _short_error_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def call_llm_with_retry(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict[str, Any]]],
    effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str = "",
    use_local: bool = False,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Call LLM with retry logic, usage tracking, and event emission.

    Returns:
        (response_message, cost) on success
        (None, 0.0) on failure after max_retries
    """
    msg = None
    last_error: Optional[Exception] = None
    # Step C: shrink-retries are tracked separately from network-retries.
    # Each successful shrink earns a free retry that does not consume the
    # max_retries network-error budget — otherwise an oversize prompt
    # against a small-context local model exhausts retries before we've
    # shrunk enough to fit. Cap at 6 to prevent infinite loops.
    shrink_attempts = 0
    MAX_SHRINK_ATTEMPTS = 6

    attempt = 0
    while attempt < max_retries:
        try:
            _emit_live_log(event_queue, {
                "type": "llm_round_started",
                "task_id": task_id,
                "task_type": task_type,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": model,
                "reasoning_effort": effort,
                "use_local": bool(use_local),
            })
            kwargs = {"messages": messages, "model": model, "reasoning_effort": effort,
                      "use_local": use_local}
            if tools:
                kwargs["tools"] = tools
            resp_msg, usage = llm.chat(**kwargs)
            msg = resp_msg
            accumulated_usage.pop("_last_llm_error", None)

            cost = float(usage.get("cost") or 0)
            display_model = str(usage.get("resolved_model") or model)
            provider = "local" if use_local else str(usage.get("provider") or "openrouter")
            if use_local:
                cost = 0.0
                display_model = f"{model} (local)"
            elif cost == 0.0:
                cost = estimate_cost(
                    display_model,
                    int(usage.get("prompt_tokens") or 0),
                    int(usage.get("completion_tokens") or 0),
                    int(usage.get("cached_tokens") or 0),
                    int(usage.get("cache_write_tokens") or 0),
                )
            usage["cost"] = cost
            add_usage(accumulated_usage, usage)

            category = task_type if task_type in ("evolution", "consciousness", "review", "summarize") else "task"
            emit_llm_usage_event(
                event_queue,
                task_id,
                display_model,
                usage,
                cost,
                category,
                provider=provider,
                source="loop",
            )

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls and (not content or not content.strip()):
                finish_reason = msg.get("finish_reason") or msg.get("stop_reason")
                is_provider_glitch = finish_reason is None
                event_type = "provider_incomplete_response" if is_provider_glitch else "llm_empty_response"
                log_msg = (
                    "Provider returned incomplete response (finish_reason=null)"
                    if is_provider_glitch
                    else "LLM returned empty response (no content, no tool_calls)"
                )
                _emit_live_log(event_queue, {
                    "type": event_type,
                    "task_id": task_id,
                    "task_type": task_type,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "model": model,
                    "finish_reason": finish_reason,
                })
                log.warning("%s, attempt %d/%d", log_msg, attempt + 1, max_retries)

                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": event_type,
                    "task_id": task_id,
                    "round": round_idx, "attempt": attempt + 1,
                    "model": model,
                    "raw_content": repr(content)[:500] if content else None,
                    "raw_tool_calls": repr(tool_calls)[:500] if tool_calls else None,
                    "finish_reason": finish_reason,
                })
                accumulated_usage["_last_llm_error"] = _short_error_text(log_msg)

                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None, cost

            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1

            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = int(usage.get("cached_tokens") or 0)
            cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
            cache_hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens > 0 else 0.0
            _round_event = {
                "ts": utc_now_iso(), "type": "llm_round",
                "task_id": task_id,
                "round": round_idx, "model": display_model,
                "reasoning_effort": effort,
                "provider": provider,
                "source": "loop",
                "model_category": infer_model_category(display_model),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
                "cache_hit_rate": cache_hit_rate,
                "cost_usd": cost,
            }
            _emit_live_log(event_queue, {
                "type": "llm_round_finished",
                "task_id": task_id,
                "task_type": task_type,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": display_model,
                "reasoning_effort": effort,
                "prompt_tokens": _round_event["prompt_tokens"],
                "completion_tokens": _round_event["completion_tokens"],
                "cached_tokens": _round_event["cached_tokens"],
                "cache_write_tokens": _round_event["cache_write_tokens"],
                "cost_usd": cost,
                "response_kind": "tool_calls" if tool_calls else "message",
                "tool_call_count": len(tool_calls),
                "has_text": bool(content and str(content).strip()),
            })
            append_jsonl(drive_logs / "events.jsonl", _round_event)
            return msg, cost

        except Exception as e:
            last_error = e
            _emit_live_log(event_queue, {
                "type": "llm_round_error",
                "task_id": task_id,
                "task_type": task_type,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": model,
                "error": repr(e),
            })
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "llm_api_error",
                "task_id": task_id,
                "round": round_idx, "attempt": attempt + 1,
                "model": model, "error": repr(e),
            })
            accumulated_usage["_last_llm_error"] = _short_error_text(repr(e))
            if isinstance(e, LocalContextTooLargeError):
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "local_context_overflow",
                    "task_id": task_id,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "model": model,
                    "error": repr(e),
                })
                break

            # Step C: detect remote-provider context overflow, shrink the
            # system message in place, and latch sparse mode for the next
            # task. Don't burn a network-retry slot — the request was
            # rejected at validation, the smaller prompt is a different
            # request, and we may need several shrink rounds to fit a
            # small-context local model.
            if _is_context_overflow_error(repr(e)):
                shrunk = _shrink_system_message_progressively(messages)
                os.environ["OUROBOROS_PROMPT_MODE"] = "sparse"
                _emit_live_log(event_queue, {
                    "type": "llm_context_overflow_recovery",
                    "task_id": task_id,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "shrink_attempt": shrink_attempts + 1,
                    "model": model,
                    "shrunk": shrunk,
                })
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "remote_context_overflow",
                    "task_id": task_id,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "shrink_attempt": shrink_attempts + 1,
                    "model": model,
                    "shrunk": shrunk,
                    "prompt_mode_latched": "sparse",
                })
                if shrunk and shrink_attempts < MAX_SHRINK_ATTEMPTS:
                    shrink_attempts += 1
                    log.warning(
                        "Context overflow (network attempt %d/%d, shrink %d/%d) "
                        "— shrunk system message; retrying without consuming "
                        "a network-retry slot",
                        attempt + 1, max_retries, shrink_attempts, MAX_SHRINK_ATTEMPTS,
                    )
                    # Do NOT bump ``attempt`` — shrink retries are free.
                    continue
                # Already at constitutional minimum or shrink budget exhausted
                # — bail so the caller can fall back to the next model.
                break

            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt * 2, 30))
            attempt += 1

    return None, 0.0

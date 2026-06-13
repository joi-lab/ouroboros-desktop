"""
LLM call, retry, pricing, and usage-event logic for the main loop.

Handles model pricing estimation, cost tracking, per-call retry with backoff,
and real-time usage event emission.
Extracted from loop.py to keep the main loop orchestrator focused.
"""

from __future__ import annotations

import json
import pathlib
import queue
import time
from typing import Any, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, LocalContextTooLargeError, add_usage
from ouroboros.observability import new_call_id, new_execution_id, persist_call
from ouroboros.pricing import emit_llm_usage_event, estimate_cost, infer_model_category
from ouroboros.utils import append_jsonl, emit_log_event, sanitize_tool_result_for_log, utc_now_iso

log = logging.getLogger(__name__)

MAIN_LOOP_MAX_TOKENS = 65_536


def _emit_live_log(event_queue: Optional[queue.Queue], payload: Dict[str, Any]) -> None:
    """Thin wrapper around the SSOT helper — keeps the call-site signature stable."""
    emit_log_event(
        event_queue,
        {"ts": utc_now_iso(), **payload},
        log_label="LLM live",
    )


def _short_error_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _remember_llm_call(
    usage: Dict[str, Any],
    *,
    llm_call_id: str,
    execution_id: str,
    round_id: str,
    round_idx: int,
    attempt: int,
    model: str,
    display_model: str,
    provider: str,
    request_ref: Dict[str, Any],
    response_ref: Dict[str, Any],
) -> None:
    call_meta = {
        "llm_call_id": llm_call_id,
        "execution_id": execution_id,
        "round_id": round_id,
        "round": round_idx,
        "attempt": attempt,
        "model": model,
        "resolved_model": display_model,
        "provider": provider,
        "request_ref": request_ref.get("manifest_ref") if request_ref else None,
        "response_ref": response_ref.get("manifest_ref") if response_ref else None,
    }
    usage["_last_llm_call_meta"] = call_meta
    usage.setdefault("llm_call_refs", []).append(call_meta)


def _normalize_usage_cost(
    usage: Dict[str, Any],
    *,
    model: str,
    use_local: bool,
) -> tuple[float, str, str, bool]:
    provider_reported_cost = bool(usage.get("cost"))
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
            usage.get("prompt_cache_ttl"),
        )
    usage["cost"] = cost
    cost_estimated = bool(usage.get("cost_estimated")) or (bool(cost) and not provider_reported_cost)
    return cost, display_model, provider, cost_estimated


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
    drive_root = pathlib.Path(drive_logs).parent
    execution_id = str(accumulated_usage.setdefault("execution_id", new_execution_id()))
    round_id = f"{execution_id}:round:{round_idx}"

    for attempt in range(max_retries):
        llm_call_id = new_call_id("llm")
        request_ref: Dict[str, Any] = {}
        try:
            _emit_live_log(event_queue, {
                "type": "llm_round_started",
                "task_id": task_id,
                "task_type": task_type,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": model,
                "reasoning_effort": effort,
                "use_local": bool(use_local),
            })
            kwargs = {
                "messages": messages,
                "model": model,
                "reasoning_effort": effort,
                "max_tokens": MAIN_LOOP_MAX_TOKENS,
                "use_local": use_local,
            }
            if tools:
                kwargs["tools"] = tools
            try:
                request_ref = persist_call(
                    drive_root,
                    task_id=task_id,
                    call_id=f"{llm_call_id}_request",
                    call_type="llm_request",
                    payload={
                        "messages": messages,
                        "tools": tools or [],
                        "model": model,
                        "reasoning_effort": effort,
                        "max_tokens": MAIN_LOOP_MAX_TOKENS,
                        "use_local": bool(use_local),
                    },
                    manifest={
                        "execution_id": execution_id,
                        "round_id": round_id,
                        "llm_call_id": llm_call_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "reasoning_effort": effort,
                    },
                )
            except Exception:
                log.debug("Failed to persist LLM request observability payload", exc_info=True)
            resp_msg, usage = llm.chat(**kwargs)
            msg = resp_msg
            accumulated_usage.pop("_last_llm_error", None)

            cost, display_model, provider, cost_estimated = _normalize_usage_cost(
                usage,
                model=model,
                use_local=use_local,
            )
            add_usage(accumulated_usage, usage)
            response_ref: Dict[str, Any] = {}
            try:
                response_ref = persist_call(
                    drive_root,
                    task_id=task_id,
                    call_id=f"{llm_call_id}_response",
                    call_type="llm_response",
                    payload={
                        "message": msg,
                        "usage": usage,
                    },
                    manifest={
                        "execution_id": execution_id,
                        "round_id": round_id,
                        "llm_call_id": llm_call_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "resolved_model": display_model,
                        "provider": provider,
                    },
                )
            except Exception:
                log.debug("Failed to persist LLM response observability payload", exc_info=True)
            _remember_llm_call(
                accumulated_usage,
                llm_call_id=llm_call_id,
                execution_id=execution_id,
                round_id=round_id,
                round_idx=round_idx,
                attempt=attempt + 1,
                model=model,
                display_model=display_model,
                provider=provider,
                request_ref=request_ref,
                response_ref=response_ref,
            )

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
                cost_estimated=cost_estimated,
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
                    "execution_id": execution_id,
                    "round_id": round_id,
                    "llm_call_id": llm_call_id,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "model": model,
                    "finish_reason": finish_reason,
                })
                log.warning("%s, attempt %d/%d", log_msg, attempt + 1, max_retries)

                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": event_type,
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "round_id": round_id,
                    "llm_call_id": llm_call_id,
                    "round": round_idx, "attempt": attempt + 1,
                    "model": model,
                    "raw_content": repr(content)[:500] if content else None,
                    "raw_tool_calls": repr(tool_calls)[:500] if tool_calls else None,
                    "finish_reason": finish_reason,
                    "request_ref": request_ref.get("manifest_ref") if request_ref else None,
                    "response_ref": response_ref.get("manifest_ref") if response_ref else None,
                })
                accumulated_usage["_last_llm_error"] = _short_error_text(log_msg)
                accumulated_usage["result_status"] = "infra_failed" if is_provider_glitch else "failed"
                accumulated_usage["reason_code"] = event_type

                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return None, cost

            accumulated_usage.pop("result_status", None)
            accumulated_usage.pop("reason_code", None)
            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1

            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = int(usage.get("cached_tokens") or 0)
            cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
            prompt_cache_ttl = str(usage.get("prompt_cache_ttl") or "")
            cache_hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens > 0 else 0.0
            _round_event = {
                "ts": utc_now_iso(), "type": "llm_round",
                "task_id": task_id,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx, "model": display_model,
                "reasoning_effort": effort,
                "provider": provider,
                "source": "loop",
                "model_category": infer_model_category(display_model),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
                "prompt_cache_ttl": prompt_cache_ttl,
                "cache_hit_rate": cache_hit_rate,
                "cost_usd": cost,
                "request_ref": request_ref.get("manifest_ref") if request_ref else None,
                "response_ref": response_ref.get("manifest_ref") if response_ref else None,
            }
            _emit_live_log(event_queue, {
                "type": "llm_round_finished",
                "task_id": task_id,
                "task_type": task_type,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": display_model,
                "reasoning_effort": effort,
                "prompt_tokens": _round_event["prompt_tokens"],
                "completion_tokens": _round_event["completion_tokens"],
                "cached_tokens": _round_event["cached_tokens"],
                "cache_write_tokens": _round_event["cache_write_tokens"],
                "prompt_cache_ttl": _round_event["prompt_cache_ttl"],
                "cost_usd": cost,
                "response_kind": "tool_calls" if tool_calls else "message",
                "tool_call_count": len(tool_calls),
                "has_text": bool(content and str(content).strip()),
            })
            append_jsonl(drive_logs / "events.jsonl", _round_event)
            return msg, cost

        except Exception as e:
            last_error = e
            safe_error = sanitize_tool_result_for_log(repr(e))
            _emit_live_log(event_queue, {
                "type": "llm_round_error",
                "task_id": task_id,
                "task_type": task_type,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": model,
                "error": safe_error,
            })
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "llm_api_error",
                "task_id": task_id,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx, "attempt": attempt + 1,
                "model": model, "error": safe_error,
                "request_ref": request_ref.get("manifest_ref") if request_ref else None,
            })
            accumulated_usage["_last_llm_error"] = _short_error_text(safe_error)
            accumulated_usage["result_status"] = "infra_failed"
            accumulated_usage["reason_code"] = "llm_api_error"
            if isinstance(e, LocalContextTooLargeError):
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "local_context_overflow",
                    "task_id": task_id,
                    "execution_id": execution_id,
                    "round_id": round_id,
                    "llm_call_id": llm_call_id,
                    "round": round_idx,
                    "attempt": attempt + 1,
                    "model": model,
                    "error": safe_error,
                })
                break
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt * 2, 30))

    return None, 0.0

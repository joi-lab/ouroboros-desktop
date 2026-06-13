"""Web search tool — OpenAI Responses API with LLM-first overridable defaults."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from ouroboros.pricing import estimate_cost
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import sanitize_tool_result_for_log, utc_now_iso

log = logging.getLogger(__name__)

DEFAULT_SEARCH_MODEL = "gpt-5.2"
DEFAULT_SEARCH_CONTEXT_SIZE = "medium"
DEFAULT_REASONING_EFFORT = "high"

def _estimate_openai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost through the shared pricing table."""
    pricing_model = model if "/" in str(model or "") else f"openai/{model}"
    cost = estimate_cost(pricing_model, input_tokens, output_tokens)
    if cost:
        return cost
    return round(input_tokens * 2.0 / 1_000_000 + output_tokens * 10.0 / 1_000_000, 6)


def _obj_to_plain(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _obj_to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_obj_to_plain(item) for item in value]
    if hasattr(value, "model_dump"):
        try:
            return _obj_to_plain(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {
            str(k): _obj_to_plain(v)
            for k, v in vars(value).items()
            if not str(k).startswith("_")
        }
    return str(value)


def _extract_sources_from_response(resp_obj: Any) -> List[Dict[str, str]]:
    plain = _obj_to_plain(resp_obj)
    sources: List[Dict[str, str]] = []
    seen: set[str] = set()

    stack: List[Any] = [plain]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            ntype = str(node.get("type") or "").lower()
            if "url_citation" in ntype or ("url" in node and ("title" in node or "snippet" in node)):
                url = sanitize_tool_result_for_log(str(node.get("url") or node.get("uri") or "").strip())
                if url and url not in seen:
                    seen.add(url)
                    sources.append({
                        "url": url,
                        "title": sanitize_tool_result_for_log(str(node.get("title") or node.get("name") or "").strip()),
                        "snippet": sanitize_tool_result_for_log(str(node.get("snippet") or node.get("text") or node.get("description") or "").strip()),
                    })
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)

    return sources


def _resolve_openai_client_settings() -> tuple[str, str | None, str, str]:
    """Return credentials only for official OpenAI Responses web search."""
    official_key = (os.environ.get("OPENAI_API_KEY", "") or "").strip()
    legacy_base_url = (os.environ.get("OPENAI_BASE_URL", "") or "").strip()

    if official_key and not legacy_base_url:
        return official_key, None, "openai", "openai"
    return "", None, "openai", "openai"


def _web_search(
    ctx: ToolContext,
    query: str,
    model: str = "",
    search_context_size: str = "",
    reasoning_effort: str = "",
) -> str:
    api_key, base_url, provider, api_key_type = _resolve_openai_client_settings()
    if not api_key:
        return json.dumps({
            "error": (
                "web_search requires the official OpenAI Responses API. "
                "Set OPENAI_API_KEY and leave OPENAI_BASE_URL empty."
            ),
        })

    active_model = model or os.environ.get("OUROBOROS_WEBSEARCH_MODEL", DEFAULT_SEARCH_MODEL)
    active_context = search_context_size or DEFAULT_SEARCH_CONTEXT_SIZE
    active_effort = reasoning_effort or DEFAULT_REASONING_EFFORT

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url)

        # --- Streaming path: emit progress while the search runs ---
        stream = client.responses.create(
            model=active_model,
            tools=[{
                "type": "web_search",
                "search_context_size": active_context,
            }],
            reasoning={"effort": active_effort},
            tool_choice="auto",
            input=query,
            stream=True,
        )

        text_parts: list[str] = []
        usage: dict = {}
        sources: List[Dict[str, str]] = []
        progress_sent = False

        for event in stream:
            etype = getattr(event, "type", "")

            # Web search lifecycle — emit progress so the user sees activity
            if etype in (
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
            ) and not progress_sent:
                if hasattr(ctx, "emit_progress_fn") and ctx.emit_progress_fn:
                    try:
                        safe_query = sanitize_tool_result_for_log(str(query or ""))[:100]
                        ctx.emit_progress_fn(f"🔍 Searching: {safe_query}")
                    except Exception:
                        pass
                progress_sent = True

            # Accumulate text deltas
            elif etype == "response.output_text.delta":
                delta = getattr(event, "delta", "")
                if delta:
                    text_parts.append(delta)

            # Final event — extract usage for cost tracking
            elif etype == "response.completed":
                resp_obj = getattr(event, "response", None)
                if resp_obj:
                    u = getattr(resp_obj, "usage", None)
                    if u:
                        usage = u.model_dump() if hasattr(u, "model_dump") else {}
                    sources = _extract_sources_from_response(resp_obj)

        text = "".join(text_parts)

        # Track web search cost (estimate from tokens — OpenAI usage has no total_cost)
        if usage and hasattr(ctx, "pending_events"):
            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            cost = _estimate_openai_cost(active_model, input_tokens, output_tokens)
            metadata = getattr(ctx, "task_metadata", {})
            if not isinstance(metadata, dict):
                metadata = {}
            try:
                ctx.pending_events.append({
                    "type": "llm_usage",
                    "task_id": str(getattr(ctx, "task_id", "") or ""),
                    "root_task_id": str(metadata.get("root_task_id") or ""),
                    "parent_task_id": str(metadata.get("parent_task_id") or ""),
                    "delegation_role": str(metadata.get("delegation_role") or ""),
                    "provider": provider,
                    "model": active_model,
                    "api_key_type": api_key_type,
                    "model_category": "websearch",
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "usage": usage,
                    "cost": cost,
                    "source": "web_search",
                    "ts": utc_now_iso(),
                    "category": "task",
                })
            except Exception:
                log.debug("Failed to emit web_search cost event", exc_info=True)

        return json.dumps({"answer": text or "(no answer)", "sources": sources}, ensure_ascii=False, indent=2)
    except Exception as e:
        detail = sanitize_tool_result_for_log(str(e))[:500]
        return json.dumps(
            {"error": f"OpenAI web search failed ({type(e).__name__}): {detail}"},
            ensure_ascii=False,
        )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("web_search", {
            "name": "web_search",
            "description": (
                "Search the web via OpenAI Responses API. "
                f"Defaults: model={DEFAULT_SEARCH_MODEL}, search_context_size={DEFAULT_SEARCH_CONTEXT_SIZE}, "
                f"reasoning_effort={DEFAULT_REASONING_EFFORT}. "
                "Override any parameter per-call if needed (LLM-first: you decide)."
            ),
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "Search query"},
                "model": {"type": "string", "description": f"OpenAI model (default: {DEFAULT_SEARCH_MODEL})"},
                "search_context_size": {"type": "string", "enum": ["low", "medium", "high"],
                                        "description": f"How much context to fetch (default: {DEFAULT_SEARCH_CONTEXT_SIZE})"},
                "reasoning_effort": {"type": "string", "enum": ["low", "medium", "high"],
                                     "description": f"Reasoning effort (default: {DEFAULT_REASONING_EFFORT})"},
            }, "required": ["query"]},
        }, _web_search, timeout_sec=540),
    ]

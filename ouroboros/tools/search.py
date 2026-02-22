"""Web search tool — OpenAI Responses API with LLM-first overridable defaults."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

DEFAULT_SEARCH_MODEL = "gpt-5.2"
DEFAULT_SEARCH_CONTEXT_SIZE = "medium"
DEFAULT_REASONING_EFFORT = "high"

_OPENAI_PRICING = {
    "gpt-5.2": (1.75, 14.0),
    "gpt-4.1": (2.0, 8.0),
    "o3": (2.0, 8.0),
    "o4-mini": (1.10, 4.40),
}


def _estimate_openai_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost from token counts. Returns 0 if model pricing unknown."""
    pricing = _OPENAI_PRICING.get(model)
    if not pricing:
        for key, val in _OPENAI_PRICING.items():
            if key in model:
                pricing = val
                break
    if not pricing:
        pricing = (2.0, 10.0)
    input_price, output_price = pricing
    return round(input_tokens * input_price / 1_000_000 + output_tokens * output_price / 1_000_000, 6)


def _web_search(
    ctx: ToolContext,
    query: str,
    model: str = "",
    search_context_size: str = "",
    reasoning_effort: str = "",
) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return json.dumps({
            "error": "OPENAI_API_KEY not set. Configure it in Settings to enable web search."
        })

    active_model = model or os.environ.get("OUROBOROS_WEBSEARCH_MODEL", DEFAULT_SEARCH_MODEL)
    active_context = search_context_size or DEFAULT_SEARCH_CONTEXT_SIZE
    active_effort = reasoning_effort or DEFAULT_REASONING_EFFORT

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.responses.create(
            model=active_model,
            tools=[{
                "type": "web_search",
                "search_context_size": active_context,
            }],
            reasoning={"effort": active_effort},
            tool_choice="auto",
            input=query,
        )
        d = resp.model_dump()
        text = ""
        for item in d.get("output", []) or []:
            if item.get("type") == "message":
                for block in item.get("content", []) or []:
                    if block.get("type") in ("output_text", "text"):
                        text += block.get("text", "")

        # Track web search cost (estimate from tokens — OpenAI usage has no total_cost)
        usage = d.get("usage") or {}
        if usage and hasattr(ctx, "pending_events"):
            input_tokens = int(usage.get("input_tokens") or usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or usage.get("completion_tokens") or 0)
            cost = _estimate_openai_cost(active_model, input_tokens, output_tokens)
            try:
                ctx.pending_events.append({
                    "type": "llm_usage",
                    "provider": "openai_websearch",
                    "model": active_model,
                    "api_key_type": "openai",
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

        return json.dumps({"answer": text or "(no answer)"}, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": f"OpenAI web search failed: {repr(e)}"}, ensure_ascii=False)


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
        }, _web_search),
    ]

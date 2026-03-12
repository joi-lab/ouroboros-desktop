"""
Ouroboros — LLM client.

The only module that communicates with LLM APIs (OpenRouter + optional local).
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "google/gemini-3-flash-preview"


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


def fetch_openrouter_pricing() -> Dict[str, Tuple[float, float, float]]:
    """
    Fetch current pricing from OpenRouter API.

    Returns dict of {model_id: (input_per_1m, cached_per_1m, output_per_1m)}.
    Returns empty dict on failure.
    """
    import logging
    log = logging.getLogger("ouroboros.llm")

    try:
        import requests
    except ImportError:
        log.warning("requests not installed, cannot fetch pricing")
        return {}

    try:
        url = "https://openrouter.ai/api/v1/models"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()

        data = resp.json()
        models = data.get("data", [])

        # Prefixes we care about
        prefixes = ("anthropic/", "openai/", "google/", "meta-llama/", "x-ai/", "qwen/")

        pricing_dict = {}
        for model in models:
            model_id = model.get("id", "")
            if not model_id.startswith(prefixes):
                continue

            pricing = model.get("pricing", {})
            if not pricing or not pricing.get("prompt"):
                continue

            # OpenRouter pricing is in dollars per token (raw values)
            raw_prompt = float(pricing.get("prompt", 0))
            raw_completion = float(pricing.get("completion", 0))
            raw_cached_str = pricing.get("input_cache_read")
            raw_cached = float(raw_cached_str) if raw_cached_str else None

            # Convert to per-million tokens
            prompt_price = round(raw_prompt * 1_000_000, 4)
            completion_price = round(raw_completion * 1_000_000, 4)
            if raw_cached is not None:
                cached_price = round(raw_cached * 1_000_000, 4)
            else:
                cached_price = round(prompt_price * 0.1, 4)  # fallback: 10% of prompt

            # Sanity check: skip obviously wrong prices
            if prompt_price > 1000 or completion_price > 1000:
                log.warning(f"Skipping {model_id}: prices seem wrong (prompt={prompt_price}, completion={completion_price})")
                continue

            pricing_dict[model_id] = (prompt_price, cached_price, completion_price)

        log.info(f"Fetched pricing for {len(pricing_dict)} models from OpenRouter")
        return pricing_dict

    except (requests.RequestException, ValueError, KeyError) as e:
        log.warning(f"Failed to fetch OpenRouter pricing: {e}")
        return {}


class LLMClient:
    """LLM API wrapper. Routes calls to OpenRouter or a local llama-cpp-python server."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
    ):
        self._api_key_override = api_key
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url
        self._client = None
        self._client_api_key: Optional[str] = None
        self._local_client = None
        self._local_port: Optional[int] = None

    def _get_client(self):
        current_api_key = self._api_key_override
        if current_api_key is None:
            current_api_key = os.environ.get("OPENROUTER_API_KEY", "")

        if self._client is None or self._client_api_key != current_api_key:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=current_api_key,
                max_retries=0,
                default_headers={
                    "HTTP-Referer": "https://ouroboros.local/",
                    "X-Title": "Ouroboros",
                },
            )
            self._client_api_key = current_api_key
            self._api_key = current_api_key
        return self._client

    def _get_local_client(self):
        port = int(os.environ.get("LOCAL_MODEL_PORT", "8766"))
        if self._local_client is None or self._local_port != port:
            from openai import OpenAI
            self._local_client = OpenAI(
                base_url=f"http://127.0.0.1:{port}/v1",
                api_key="local",
                max_retries=0,
            )
            self._local_port = port
        return self._local_client

    @staticmethod
    def _strip_cache_control(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Strip cache_control from message content blocks (OpenRouter/Anthropic-only)."""
        import copy
        cleaned = copy.deepcopy(messages)
        for msg in cleaned:
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        return cleaned

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API as fallback."""
        try:
            import requests
            url = f"{self._base_url.rstrip('/')}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            log.debug("Failed to fetch generation cost from OpenRouter", exc_info=True)
            pass
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
        use_local: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost).

        When use_local=True, routes to the local llama-cpp-python server
        and strips OpenRouter-specific parameters (reasoning, provider, cache_control).
        """
        if use_local:
            return self._chat_local(messages, tools, max_tokens, tool_choice)

        return self._chat_openrouter(messages, model, tools, reasoning_effort, max_tokens, tool_choice)

    def _chat_local(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        max_tokens: int,
        tool_choice: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to the local llama-cpp-python server."""
        client = self._get_local_client()

        clean_messages = self._strip_cache_control(messages)
        # Flatten multipart content blocks to plain strings (local server doesn't support arrays)
        for msg in clean_messages:
            content = msg.get("content")
            if isinstance(content, list):
                msg["content"] = "\n\n".join(
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )

        clean_tools = None
        if tools:
            clean_tools = [
                {k: v for k, v in t.items() if k != "cache_control"}
                for t in tools
            ]

        # Cap max_tokens to fit within the model's context window
        local_max = min(max_tokens, 2048)
        ctx_len = 0
        try:
            from ouroboros.local_model import get_manager
            ctx_len = get_manager().get_context_length()
            if ctx_len > 0:
                local_max = min(max_tokens, max(256, ctx_len // 4))
        except Exception:
            pass

        if ctx_len > 0:
            self._truncate_messages_for_context(clean_messages, ctx_len, local_max)

        kwargs: Dict[str, Any] = {
            "model": "local-model",
            "messages": clean_messages,
            "max_tokens": local_max,
        }
        if clean_tools:
            kwargs["tools"] = clean_tools
            kwargs["tool_choice"] = tool_choice

        last_exc: Optional[Exception] = None
        for attempt in range(3):
            try:
                resp = client.chat.completions.create(**kwargs)
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                err = str(exc)
                if "context_length_exceeded" in err and attempt < 2:
                    log.warning("Context overflow (attempt %d), truncating: %s", attempt + 1, err)
                    self._shrink_messages_from_error(clean_messages, err)
                    continue
                log.warning("Local model request failed: %s", exc)
                raise
        if last_exc is not None:
            raise last_exc

        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        if not msg.get("tool_calls") and msg.get("content") and clean_tools:
            allowed_tool_names = {
                str(t.get("function", {}).get("name", "")).strip()
                for t in clean_tools
                if isinstance(t, dict)
            }
            msg = self._parse_tool_calls_from_content(msg, allowed_tool_names)

        usage["cost"] = 0.0
        return msg, usage

    @staticmethod
    def _parse_tool_calls_from_content(
        msg: Dict[str, Any],
        allowed_tool_names: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Parse <tool_call> XML tags from content into structured tool_calls.

        Works around llama-cpp-python not parsing Qwen/Hermes-style tool calls
        (https://github.com/abetlen/llama-cpp-python/issues/1784).
        """
        content = str(msg.get("content", "") or "")
        stripped = content.strip()
        if not stripped:
            return msg

        # Safety: only upgrade the response when it consists solely of
        # one or more <tool_call> blocks. If the model mixed prose with
        # examples or explanations, leave it as plain text.
        full_pattern = re.compile(
            r"^(?:\s*<tool_call>\s*\{.*?\}\s*</tool_call>\s*)+$",
            re.DOTALL,
        )
        if not full_pattern.fullmatch(stripped):
            return msg

        matches = re.findall(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", stripped, re.DOTALL)
        if not matches:
            return msg

        allowed = {name for name in (allowed_tool_names or set()) if name}
        tool_calls = []
        for i, raw in enumerate(matches):
            try:
                raw_stripped = raw.strip()
                try:
                    obj = json.loads(raw_stripped)
                except json.JSONDecodeError:
                    if raw_stripped.startswith("{{") and raw_stripped.endswith("}}"):
                        obj = json.loads(raw_stripped[1:-1])
                    else:
                        raise
                if not isinstance(obj, dict):
                    raise ValueError("tool_call payload must be an object")
                name = str(obj.get("name", "")).strip()
                args = obj.get("arguments", {})
                if not name:
                    raise ValueError("tool_call missing function name")
                if allowed and name not in allowed:
                    raise ValueError(f"unknown tool '{name}'")
                if not isinstance(args, dict):
                    raise ValueError("tool_call arguments must be an object")
                tool_calls.append({
                    "id": f"call_local_{i}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                })
            except (json.JSONDecodeError, ValueError) as exc:
                log.warning("Rejected local <tool_call> block: %s (%s)", raw[:200], exc)
                return msg

        if not tool_calls:
            return msg

        msg = dict(msg)
        msg["tool_calls"] = tool_calls
        msg["content"] = None
        log.info("Parsed %d local tool call(s) from text output", len(tool_calls))
        return msg

    @staticmethod
    def _truncate_messages_for_context(
        messages: List[Dict[str, Any]], ctx_len: int, max_tokens: int,
    ) -> None:
        """Hard-truncate message content so total fits within the context window.

        Uses a conservative 3-chars-per-token ratio to avoid underestimating.
        """
        available_tokens = ctx_len - max_tokens - 64
        if available_tokens < 256:
            available_tokens = 256
        target_chars = available_tokens * 3

        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if total_chars <= target_chars:
            return

        for msg in messages:
            if msg["role"] == "system" and isinstance(msg.get("content"), str):
                content = msg["content"]
                other_chars = total_chars - len(content)
                allowed = max(512, target_chars - other_chars)
                if len(content) > allowed:
                    msg["content"] = content[:allowed] + "\n\n[Context truncated to fit model window]"
                    log.info("Truncated system message from %d to %d chars for %d-token context",
                             len(content), allowed, ctx_len)
                return

    @staticmethod
    def _shrink_messages_from_error(
        messages: List[Dict[str, Any]], error_text: str,
    ) -> None:
        """Parse a context_length_exceeded error and shrink the largest message."""
        m = re.search(r"requested (\d+) tokens.*?(\d+) in the messages", error_text)
        if not m:
            for msg in messages:
                if msg["role"] == "system" and isinstance(msg.get("content"), str):
                    msg["content"] = msg["content"][:len(msg["content"]) // 2]
                    return
            return

        requested = int(m.group(1))
        msg_tokens = int(m.group(2))
        # Find max context from "maximum context length is N tokens"
        ctx_match = re.search(r"maximum context length is (\d+)", error_text)
        ctx_max = int(ctx_match.group(1)) if ctx_match else 16384
        comp_match = re.search(r"(\d+) in the completion", error_text)
        comp_tokens = int(comp_match.group(1)) if comp_match else 2048

        target_msg_tokens = ctx_max - comp_tokens - 64
        if target_msg_tokens < 256:
            target_msg_tokens = 256
        ratio = target_msg_tokens / max(msg_tokens, 1)
        if ratio >= 1.0:
            ratio = 0.5

        for msg in messages:
            if msg["role"] == "system" and isinstance(msg.get("content"), str):
                content = msg["content"]
                new_len = max(512, int(len(content) * ratio))
                if new_len < len(content):
                    msg["content"] = content[:new_len] + "\n\n[Context truncated to fit model window]"
                    log.info("Retry-truncated system message to %d chars (ratio=%.2f)", new_len, ratio)
                return

    def _chat_openrouter(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to OpenRouter."""
        client = self._get_client()
        effort = normalize_reasoning_effort(reasoning_effort)

        extra_body: Dict[str, Any] = {
            "reasoning": {"effort": effort, "exclude": True},
        }

        # Pin Anthropic models to Anthropic provider for prompt caching
        if model.startswith("anthropic/"):
            extra_body["provider"] = {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if tools:
            tools_with_cache = [t for t in tools]  # shallow copy
            if tools_with_cache:
                last_tool = {**tools_with_cache[-1]}  # copy last tool
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_with_cache[-1] = last_tool
            kwargs["tools"] = tools_with_cache
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (prompt_details_for_write.get("cache_write_tokens")
                              or prompt_details_for_write.get("cache_creation_tokens")
                              or prompt_details_for_write.get("cache_creation_input_tokens"))
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        if not usage.get("cost"):
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id)
                if cost is not None:
                    usage["cost"] = cost

        return msg, usage

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "anthropic/claude-sonnet-4.6",
        max_tokens: int = 2048,
        reasoning_effort: str = "none",
    ) -> Tuple[str, Dict[str, Any]]:
        """
        Send a vision query to an LLM. Lightweight — no tools, no loop.

        Args:
            prompt: Text instruction for the model
            images: List of image dicts. Each dict must have either:
                - {"url": "https://..."} — for URL images
                - {"base64": "<b64>", "mime": "image/png"} — for base64 images
            model: VLM-capable model ID
            max_tokens: Max response tokens
            reasoning_effort: Effort level

        Returns:
            (text_response, usage_dict)
        """
        # Build multipart content
        content: List[Dict[str, Any]] = [{"type": "text", "text": prompt}]
        for img in images:
            if "url" in img:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": img["url"]},
                })
            elif "base64" in img:
                mime = img.get("mime", "image/png")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['base64']}"},
                })
            else:
                log.warning("vision_query: skipping image with unknown format: %s", list(img.keys()))

        messages = [{"role": "user", "content": content}]
        response_msg, usage = self.chat(
            messages=messages,
            model=model,
            tools=None,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
        )
        text = response_msg.get("content") or ""
        return text, usage

    def default_model(self) -> str:
        """Return the single default model from env. LLM switches via tool if needed."""
        return os.environ.get("OUROBOROS_MODEL", "anthropic/claude-sonnet-4.6")

    def available_models(self) -> List[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = os.environ.get("OUROBOROS_MODEL", "anthropic/claude-sonnet-4.6")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models

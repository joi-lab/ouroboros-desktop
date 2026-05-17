"""
Ouroboros — LLM client.

The only module that communicates with LLM APIs (OpenRouter, direct providers, + optional local).
Contract: chat(), default_model(), available_models(), add_usage().
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import copy
from typing import Any, Dict, List, Optional, Set, Tuple

from ouroboros.provider_models import normalize_anthropic_model_id

log = logging.getLogger(__name__)

DEFAULT_LIGHT_MODEL = "anthropic/claude-sonnet-4.6"


class LocalContextTooLargeError(RuntimeError):
    """Raised when a local model cannot fit context without silent truncation."""


def _estimate_message_chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            total += sum(len(str(block.get("text", ""))) for block in content if isinstance(block, dict))
        else:
            total += len(str(content or ""))
    return total


def _split_markdown_sections(text: str) -> Tuple[str, List[Tuple[str, str]]]:
    lines = str(text or "").splitlines()
    preamble: List[str] = []
    sections: List[Tuple[str, str]] = []
    current_title: Optional[str] = None
    current_lines: List[str] = []

    for line in lines:
        if line.startswith("## "):
            if current_title is None:
                preamble = current_lines[:]
            else:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line[3:].strip()
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_title is None:
        return "\n".join(lines).strip(), []

    sections.append((current_title, "\n".join(current_lines).strip()))
    return "\n".join(preamble).strip(), sections


def _compact_markdown_sections(
    text: str,
    preserve_titles: Set[str],
    reason: str,
) -> str:
    preamble, sections = _split_markdown_sections(text)
    if not sections:
        return text

    parts: List[str] = []
    if preamble:
        parts.append(preamble)

    for title, section in sections:
        if title in preserve_titles:
            parts.append(section)
            continue
        omitted_chars = max(0, len(section))
        parts.append(
            f"## {title}\n\n"
            f"[Compacted for local-model context: omitted {omitted_chars} chars. {reason}]"
        )

    return "\n\n".join(p for p in parts if p).strip()


_LOCAL_COMPACTION_MODES = {
    "static": (
        {"BIBLE.md"},
        "Use a larger-context model or read the source file directly if this section becomes necessary.",
    ),
    "semi_stable": (
        {"Identity"},
        "Identity was preserved; non-core stable memory sections were compacted for local execution.",
    ),
    "dynamic": (
        {
            "Scratchpad",
            "Dialogue History",
            "Dialogue Summary",
            "Memory Registry (what I know / don't know)",
            "Drive state",
            "Runtime context",
            "Health Invariants",
        },
        "Working-memory and runtime sections were preserved; non-core recent/history sections were compacted for local execution.",
    ),
    "system": (
        {
            "BIBLE.md",
            "Scratchpad",
            "Identity",
            "Drive state",
            "Runtime context",
            "Health Invariants",
            "Recent observations",
            "Background consciousness info",
        },
        "Non-core sections were compacted for local execution.",
    ),
}


def _compact_local_text(text: str, mode: str) -> str:
    preserve_titles, reason = _LOCAL_COMPACTION_MODES[mode]
    return _compact_markdown_sections(text, preserve_titles=preserve_titles, reason=reason)


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


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

    # Per-process cache of OpenRouter model capabilities. Populated lazily on
    # the first request that needs it, then reused for the lifetime of the
    # process. A missing entry means "unknown" — callers treat that as broad
    # support and do NOT strip any parameters (zero-regression fallback when
    # the capabilities endpoint is unreachable or a model isn't listed).
    _SUPPORTED_PARAMS_CACHE: Dict[str, set] = {}
    _SUPPORTED_PARAMS_FETCHED: bool = False

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
        self._async_client = None
        self._async_client_api_key: Optional[str] = None
        self._local_client = None
        self._local_port: Optional[int] = None
        self._remote_clients: Dict[Tuple[str, str, str, Tuple[Tuple[str, str], ...]], Any] = {}
        self._async_remote_clients: Dict[Tuple[str, str, str, Tuple[Tuple[str, str], ...]], Any] = {}

    @classmethod
    def _fetch_openrouter_capabilities(cls) -> None:
        """Populate _SUPPORTED_PARAMS_CACHE via OpenRouter's /models endpoint.

        Runs at most once per process. On any failure the cache remains empty,
        which means every ``_get_supported_parameters`` lookup returns ``None``
        and callers fall back to the pre-v4.33 behavior of not stripping any
        kwargs.
        """
        cls._SUPPORTED_PARAMS_FETCHED = True
        try:
            import requests
            resp = requests.get(
                "https://openrouter.ai/api/v1/models",
                timeout=15,
            )
            if resp.status_code != 200:
                log.debug(
                    "OpenRouter /models returned %d; supported_parameters cache empty",
                    resp.status_code,
                )
                return
            for m in resp.json().get("data", []) or []:
                mid = m.get("id") or ""
                sp = m.get("supported_parameters")
                if mid and isinstance(sp, list) and sp:
                    cls._SUPPORTED_PARAMS_CACHE[mid] = set(sp)
        except Exception:
            log.debug("Failed to fetch OpenRouter model capabilities", exc_info=True)

    @classmethod
    def _get_supported_parameters(cls, model_id: str) -> Optional[set]:
        """Return the set of parameter names the given OpenRouter model accepts.

        Returns ``None`` when we don't know — caller should treat this as broad
        support (no parameter stripping). The first call triggers a one-shot
        fetch of every model's capabilities; subsequent calls use the in-memory
        cache populated by that fetch.
        """
        if not cls._SUPPORTED_PARAMS_FETCHED:
            cls._fetch_openrouter_capabilities()
        return cls._SUPPORTED_PARAMS_CACHE.get(model_id)

    @staticmethod
    def _parse_provider_model(model: str) -> Tuple[str, str]:
        model_name = str(model or "").strip()
        for prefix, provider in (
            ("openai::", "openai"),
            ("anthropic::", "anthropic"),
            ("cloudru::", "cloudru"),
            ("openai-compatible::", "openai-compatible"),
            ("openrouter::", "openrouter"),
        ):
            if model_name.startswith(prefix):
                return provider, model_name[len(prefix):].strip()
        return "openrouter", model_name

    @staticmethod
    def _qualified_model_name(provider: str, resolved_model: str) -> str:
        if provider == "openrouter":
            return resolved_model
        if provider == "openai":
            return f"openai/{resolved_model}"
        if provider == "anthropic":
            return f"anthropic/{resolved_model}"
        if provider == "cloudru":
            return f"cloudru/{resolved_model}"
        return f"openai-compatible/{resolved_model}"

    def _resolve_remote_target(self, model: str) -> Dict[str, Any]:
        provider, resolved_model = self._parse_provider_model(model)
        usage_model = self._qualified_model_name(provider, resolved_model)

        if provider == "openai":
            return {
                "provider": provider,
                "resolved_model": resolved_model,
                "usage_model": usage_model,
                "api_key": os.environ.get("OPENAI_API_KEY", ""),
                "base_url": "https://api.openai.com/v1",
                "default_headers": {},
                "supports_openrouter_extensions": False,
                "supports_generation_cost": False,
            }

        if provider == "anthropic":
            resolved_model = normalize_anthropic_model_id(resolved_model)
            return {
                "provider": provider,
                "resolved_model": resolved_model,
                "usage_model": self._qualified_model_name(provider, resolved_model),
                "api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
                "base_url": "https://api.anthropic.com/v1",
                "default_headers": {},
                "supports_openrouter_extensions": False,
                "supports_generation_cost": False,
            }

        if provider == "cloudru":
            return {
                "provider": provider,
                "resolved_model": resolved_model,
                "usage_model": usage_model,
                "api_key": os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY", ""),
                "base_url": (
                    os.environ.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL", "") or ""
                ).strip() or "https://foundation-models.api.cloud.ru/v1",
                "default_headers": {},
                "supports_openrouter_extensions": False,
                "supports_generation_cost": False,
            }

        if provider == "openai-compatible":
            compatible_key = (os.environ.get("OPENAI_COMPATIBLE_API_KEY", "") or "").strip()
            compatible_base_url = (os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
            legacy_base_url = (os.environ.get("OPENAI_BASE_URL", "") or "").strip()
            legacy_key = (os.environ.get("OPENAI_API_KEY", "") or "").strip()
            return {
                "provider": provider,
                "resolved_model": resolved_model,
                "usage_model": usage_model,
                "api_key": compatible_key or legacy_key,
                "base_url": compatible_base_url or legacy_base_url,
                "default_headers": {},
                "supports_openrouter_extensions": False,
                "supports_generation_cost": False,
            }

        current_api_key = self._api_key_override
        if current_api_key is None:
            current_api_key = os.environ.get("OPENROUTER_API_KEY", "")
        return {
            "provider": "openrouter",
            "resolved_model": resolved_model,
            "usage_model": usage_model,
            "api_key": current_api_key,
            "base_url": self._base_url,
            "default_headers": {
                "HTTP-Referer": "https://ouroboros.local/",
                "X-Title": "Ouroboros",
            },
            "supports_openrouter_extensions": True,
            "supports_generation_cost": True,
        }

    def _get_client(self):
        target = self._resolve_remote_target("openrouter::")
        return self._get_remote_client(target)

    def _get_remote_client(self, target: Dict[str, Any]):
        base_url = str(target.get("base_url") or "")
        api_key = str(target.get("api_key") or "")
        headers_dict = dict(target.get("default_headers") or {})
        headers = tuple(sorted((str(k), str(v)) for k, v in headers_dict.items()))
        cache_key = (str(target.get("provider") or ""), base_url, api_key, headers)

        client = self._remote_clients.get(cache_key)
        if client is None:
            from openai import OpenAI

            kwargs: Dict[str, Any] = {
                "api_key": api_key,
                "max_retries": 0,
            }
            if base_url:
                kwargs["base_url"] = base_url
            if headers_dict:
                kwargs["default_headers"] = headers_dict
            client = OpenAI(**kwargs)
            self._remote_clients[cache_key] = client
        return client

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

    def _get_async_remote_client(self, target: Dict[str, Any]):
        base_url = str(target.get("base_url") or "")
        api_key = str(target.get("api_key") or "")
        headers_dict = dict(target.get("default_headers") or {})
        headers = tuple(sorted((str(k), str(v)) for k, v in headers_dict.items()))
        cache_key = (str(target.get("provider") or ""), base_url, api_key, headers)

        client = self._async_remote_clients.get(cache_key)
        if client is None:
            from openai import AsyncOpenAI

            kwargs: Dict[str, Any] = {
                "api_key": api_key,
                "max_retries": 0,
            }
            if base_url:
                kwargs["base_url"] = base_url
            if headers_dict:
                kwargs["default_headers"] = headers_dict
            client = AsyncOpenAI(**kwargs)
            self._async_remote_clients[cache_key] = client
        return client

    @staticmethod
    def _no_proxy_timeout():
        import httpx

        return httpx.Timeout(connect=30.0, read=3600.0, write=3600.0, pool=30.0)

    @classmethod
    def _make_no_proxy_client(cls, target: Dict[str, Any]):
        import httpx
        from openai import OpenAI

        http_client = httpx.Client(
            trust_env=False,
            mounts={},
            timeout=cls._no_proxy_timeout(),
        )
        oa_client = OpenAI(
            api_key=str(target.get("api_key") or ""),
            base_url=str(target.get("base_url") or ""),
            default_headers=dict(target.get("default_headers") or {}),
            http_client=http_client,
            max_retries=0,
        )
        return oa_client, http_client

    @classmethod
    def _make_no_proxy_async_client(cls, target: Dict[str, Any]):
        import httpx
        from openai import AsyncOpenAI

        http_client = httpx.AsyncClient(
            trust_env=False,
            mounts={},
            timeout=cls._no_proxy_timeout(),
        )
        oa_client = AsyncOpenAI(
            api_key=str(target.get("api_key") or ""),
            base_url=str(target.get("base_url") or ""),
            default_headers=dict(target.get("default_headers") or {}),
            http_client=http_client,
            max_retries=0,
        )
        return oa_client, http_client

    @staticmethod
    def _strip_cache_control(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Strip cache_control from message content blocks (OpenRouter/Anthropic-only).

        For tool-role messages whose content is a list of blocks, also flattens
        the content back to a plain string, because OpenAI and compatible providers
        expect tool content as a string (not an array of blocks).
        """
        import copy
        cleaned = copy.deepcopy(messages)
        for msg in cleaned:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            if msg.get("role") == "tool":
                # Flatten back to plain string for providers that require it
                msg["content"] = "".join(
                    block.get("text", "") if isinstance(block, dict) else str(block)
                    for block in content
                )
            else:
                for block in content:
                    if isinstance(block, dict):
                        block.pop("cache_control", None)
        return cleaned

    def _fetch_generation_cost(
        self,
        generation_id: str,
        target: Optional[Dict[str, Any]] = None,
    ) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API as fallback."""
        active_target = target or self._resolve_remote_target("openrouter::")
        if not active_target.get("supports_generation_cost"):
            return None
        try:
            import requests
            base_url = str(active_target.get("base_url") or "").rstrip("/")
            api_key = str(active_target.get("api_key") or "")
            url = f"{base_url}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {api_key}"}, timeout=5)
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
        max_tokens: int = 65536,
        tool_choice: str = "auto",
        use_local: bool = False,
        temperature: Optional[float] = None,
        no_proxy: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Single LLM call. Returns: (response_message_dict, usage_dict with cost).

        When use_local=True, routes to the local llama-cpp-python server
        and strips OpenRouter-specific parameters (reasoning, provider, cache_control).

        When no_proxy=True, the underlying httpx transport is built with
        trust_env=False and an empty mounts map, bypassing OS-level and
        env-var proxy detection.  Use this in contexts where the process
        was forked from a multithreaded parent (e.g. macOS app-bundle
        workers) to avoid a SIGSEGV in SCDynamicStoreCopyProxiesWithOptions.
        """
        if use_local:
            return self._chat_local(messages, tools, max_tokens, tool_choice)

        target = self._resolve_remote_target(model)
        return self._chat_remote(
            target, messages, tools, reasoning_effort, max_tokens, tool_choice, temperature,
            no_proxy=no_proxy,
        )

    async def chat_async(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 65536,
        tool_choice: str = "auto",
        temperature: Optional[float] = None,
        no_proxy: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Async remote chat used by review/concurrent callers.

        When no_proxy=True, bypasses OS-level and env-var proxy detection via
        trust_env=False.  This is required in forked worker processes on macOS
        to avoid a SIGSEGV in SCDynamicStoreCopyProxiesWithOptions.

        Applies to both the Anthropic path (synchronous requests.Session run in
        a thread) and the OpenAI-compatible async path (httpx.AsyncClient with
        trust_env=False and empty mounts).
        """
        if tools:
            raise ValueError("chat_async does not support tool calls")
        target = self._resolve_remote_target(model)
        if target.get("provider") == "anthropic":
            return await asyncio.to_thread(
                self._chat_anthropic,
                target,
                messages,
                tools,
                reasoning_effort,
                max_tokens,
                tool_choice,
                temperature,
                no_proxy,
            )
        if no_proxy:
            _oa_client, _http_client = self._make_no_proxy_async_client(target)
            try:
                kwargs = self._build_remote_kwargs(
                    target, messages, reasoning_effort, max_tokens, tool_choice, temperature, tools
                )
                resp = await _oa_client.chat.completions.create(**kwargs)
                return self._normalize_remote_response(resp.model_dump(), target, skip_cost_fetch=True)
            finally:
                try:
                    await _http_client.aclose()
                except Exception:
                    pass
        client = self._get_async_remote_client(target)
        kwargs = self._build_remote_kwargs(
            target, messages, reasoning_effort, max_tokens, tool_choice, temperature, tools
        )
        resp = await client.chat.completions.create(**kwargs)
        return self._normalize_remote_response(resp.model_dump(), target)

    def _prepare_messages_for_local_context(
        self,
        messages: List[Dict[str, Any]],
        ctx_len: int,
        max_tokens: int,
    ) -> List[Dict[str, Any]]:
        available_tokens = max(256, ctx_len - max_tokens - 64)
        target_chars = available_tokens * 3
        total_chars = _estimate_message_chars(messages)
        if total_chars <= target_chars:
            return messages

        compacted = copy.deepcopy(messages)
        for msg in compacted:
            if msg.get("role") != "system":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for idx, block in enumerate(content):
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    block_text = str(block.get("text", ""))
                    if idx == 0:
                        block["text"] = _compact_local_text(block_text, "static")
                    elif idx == 1:
                        block["text"] = _compact_local_text(block_text, "semi_stable")
                    else:
                        block["text"] = _compact_local_text(block_text, "dynamic")
            elif isinstance(content, str):
                msg["content"] = _compact_local_text(content, "system")
            break

        compacted_chars = _estimate_message_chars(compacted)
        if compacted_chars <= target_chars:
            return compacted

        raise LocalContextTooLargeError(
            f"Local model context too large after safe compaction "
            f"({compacted_chars} chars > target {target_chars})."
        )

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
            clean_messages = self._prepare_messages_for_local_context(clean_messages, ctx_len, local_max)

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
                if "context_length_exceeded" in err:
                    raise LocalContextTooLargeError(err) from exc
                if attempt == 2:
                    log.warning("Local model request failed: %s", exc)
                    raise
                log.warning(
                    "Local model request failed (attempt %d/3): %s",
                    attempt + 1,
                    exc,
                )
                time.sleep(0.5 * (attempt + 1))
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
    def _strip_reasoning_wrappers(text: str):
        """Remove leading reasoning wrapper tags and return (cleaned_text, reasoning_text).

        Strips ``<think>...</think>`` and ``<reasoning>...</reasoning>`` blocks
        (Qwen3 style) from the **outer envelope** of *text* — i.e. from the
        portion that precedes the first ``<tool_call>`` block.  This avoids
        accidentally altering JSON payloads inside ``<tool_call>...</tool_call>``
        that may themselves contain literal ``<think>`` or ``<reasoning>`` text
        as argument values.

        Strategy:
        1. Split *text* at the first ``<tool_call>`` occurrence.
        2. Strip reasoning wrappers only from the prefix (part before the first
           ``<tool_call>``).
        3. Concatenate the stripped prefix with the unchanged tool-call section.

        Returns:
            (cleaned_text, reasoning_text) where:
              - cleaned_text is *text* with reasoning wrappers removed from the
                prefix only, surrounding whitespace stripped.
              - reasoning_text is the concatenated inner content of all reasoning
                blocks found in the prefix (stripped), or an empty string when
                no blocks were found.
        """
        # Split at first <tool_call> so we never touch JSON inside tool payloads.
        tool_call_start = re.search(r"<tool_call\b", text, re.IGNORECASE)
        if tool_call_start:
            prefix = text[: tool_call_start.start()]
            suffix = text[tool_call_start.start():]
        else:
            prefix = text
            suffix = ""

        reasoning_parts: list = []

        def _extract(tag: str, s: str) -> str:
            pattern = re.compile(
                r"<" + re.escape(tag) + r">(.*?)</" + re.escape(tag) + r">",
                re.DOTALL | re.IGNORECASE,
            )
            inner_texts = pattern.findall(s)
            reasoning_parts.extend(p.strip() for p in inner_texts if p.strip())
            return pattern.sub("", s)

        cleaned_prefix = _extract("think", prefix)
        cleaned_prefix = _extract("reasoning", cleaned_prefix)

        combined = (cleaned_prefix.strip() + ("\n" if cleaned_prefix.strip() and suffix else "") + suffix).strip()
        return combined, "\n\n".join(reasoning_parts)

    @staticmethod
    def _parse_tool_calls_from_content(
        msg: Dict[str, Any],
        allowed_tool_names: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """Parse <tool_call> XML tags from content into structured tool_calls.

        Works around llama-cpp-python not parsing Qwen/Hermes-style tool calls
        (https://github.com/abetlen/llama-cpp-python/issues/1784).

        Qwen3 models with ``enable_thinking=True`` (default) wrap their chain-of-
        thought in ``<think>...</think>`` before emitting the actual ``<tool_call>``
        block.  This method strips the reasoning wrapper first, then applies the
        strict full-match safety guard so responses that contain genuine prose
        alongside tool call text are still rejected.

        Contract: when tool calls are successfully parsed, ``msg["content"]`` is
        set to the extracted reasoning text (may be an empty string).  Callers in
        ``loop.py`` check ``content`` truthiness — non-empty reasoning text will
        surface as a progress/reasoning note in the UI, which is the intended
        behaviour for thinking models.
        """
        content = str(msg.get("content", "") or "")
        stripped_raw = content.strip()
        if not stripped_raw:
            return msg

        # Phase 1: strip known reasoning wrappers (<think>, <reasoning>).
        # Only these explicit tag pairs are removed; arbitrary prose is left.
        stripped, reasoning = LLMClient._strip_reasoning_wrappers(stripped_raw)
        if not stripped:
            # Content was only reasoning text — nothing actionable.
            return msg

        # Phase 2: Safety guard — only upgrade the response when the remaining
        # text consists solely of one or more <tool_call> blocks.  Mixed prose
        # (without a reasoning wrapper) is left as plain text.
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
        # Preserve reasoning text in content so loop.py can emit it as a
        # progress/reasoning note (P1 Continuity).  Empty string when no think
        # wrapper was present (original behaviour: content was None in that case,
        # but an empty string is equally falsy for callers that check truthiness).
        msg["content"] = reasoning or None
        log.info("Parsed %d local tool call(s) from text output", len(tool_calls))
        return msg

    @staticmethod
    def _stringify_anthropic_content(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _stringify_tool_description(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return "".join(str(part) for part in value if part is not None)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def _coalesce_anthropic_message(
        messages: List[Dict[str, Any]],
        role: str,
        content: List[Dict[str, Any]],
    ) -> None:
        if not content:
            return
        if messages and messages[-1].get("role") == role and isinstance(messages[-1].get("content"), list):
            messages[-1]["content"].extend(content)
            return
        messages.append({"role": role, "content": list(content)})

    @staticmethod
    def _anthropic_image_block(image_url: str) -> Optional[Dict[str, Any]]:
        url = str(image_url or "").strip()
        if not url:
            return None
        if url.startswith("data:") and ";base64," in url:
            header, data = url.split(",", 1)
            mime = header[5:].split(";", 1)[0] or "image/png"
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime,
                    "data": data,
                },
            }
        return {
            "type": "image",
            "source": {
                "type": "url",
                "url": url,
            },
        }

    def _anthropic_blocks_from_content(self, content: Any) -> List[Dict[str, Any]]:
        if content is None:
            return []
        if isinstance(content, str):
            return [{"type": "text", "text": content}] if content else []
        if not isinstance(content, list):
            text = self._stringify_anthropic_content(content)
            return [{"type": "text", "text": text}] if text else []

        blocks: List[Dict[str, Any]] = []
        for block in content:
            if isinstance(block, str):
                if block:
                    blocks.append({"type": "text", "text": block})
                continue
            if not isinstance(block, dict):
                text = self._stringify_anthropic_content(block)
                if text:
                    blocks.append({"type": "text", "text": text})
                continue

            block_type = str(block.get("type") or "").strip()
            if block_type in {"text", "input_text", "output_text"}:
                text = str(block.get("text") or "")
                if text:
                    normalized = {"type": "text", "text": text}
                    if isinstance(block.get("cache_control"), dict):
                        normalized["cache_control"] = dict(block.get("cache_control") or {})
                    blocks.append(normalized)
                continue
            if block_type == "image_url":
                image_url = str((block.get("image_url") or {}).get("url") or "")
                image_block = self._anthropic_image_block(image_url)
                if image_block:
                    blocks.append(image_block)
                continue
            if block.get("text"):
                normalized = {"type": "text", "text": str(block.get("text") or "")}
                if isinstance(block.get("cache_control"), dict):
                    normalized["cache_control"] = dict(block.get("cache_control") or {})
                blocks.append(normalized)
        return blocks

    def _build_anthropic_messages(
        self,
        messages: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        system_blocks: List[Dict[str, Any]] = []
        anthropic_messages: List[Dict[str, Any]] = []

        for msg in messages:
            role = str(msg.get("role") or "").strip().lower()
            if role == "system":
                system_blocks.extend(self._anthropic_blocks_from_content(msg.get("content")))
                continue

            if role == "user":
                self._coalesce_anthropic_message(
                    anthropic_messages,
                    "user",
                    self._anthropic_blocks_from_content(msg.get("content")),
                )
                continue

            if role == "assistant":
                assistant_blocks = self._anthropic_blocks_from_content(msg.get("content"))
                for tool_call in msg.get("tool_calls") or []:
                    function = tool_call.get("function") or {}
                    raw_args = function.get("arguments")
                    parsed_args: Any = {}
                    if isinstance(raw_args, str):
                        try:
                            parsed_args = json.loads(raw_args) if raw_args.strip() else {}
                        except Exception:
                            parsed_args = {"raw": raw_args}
                    elif raw_args is not None:
                        parsed_args = raw_args
                    if not isinstance(parsed_args, dict):
                        parsed_args = {"value": parsed_args}
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": str(tool_call.get("id") or ""),
                        "name": str(function.get("name") or ""),
                        "input": parsed_args,
                    })
                self._coalesce_anthropic_message(anthropic_messages, "assistant", assistant_blocks)
                continue

            if role == "tool":
                tool_use_id = str(msg.get("tool_call_id") or "")
                if not tool_use_id:
                    raise ValueError("Anthropic direct tool result is missing tool_call_id.")
                raw_content = msg.get("content")
                # Anthropic direct API supports list of content blocks (including
                # cache_control) as tool_result content, so pass through as-is.
                # For plain strings, pass them directly. Only JSON-serialize
                # dicts and other non-string non-list values.
                if isinstance(raw_content, list):
                    tool_result_content: Any = raw_content
                else:
                    tool_result_content = self._stringify_anthropic_content(raw_content)
                self._coalesce_anthropic_message(
                    anthropic_messages,
                    "user",
                    [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": tool_result_content,
                    }],
                )

        return system_blocks, anthropic_messages

    @staticmethod
    def _build_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        anthropic_tools: List[Dict[str, Any]] = []
        for tool in LLMClient._sanitize_chat_completion_tools(tools):
            function = tool.get("function") or {}
            name = str(function.get("name") or "").strip()
            if not name:
                continue
            anthropic_tools.append({
                "name": name,
                "description": LLMClient._stringify_tool_description(function.get("description")),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            })
        return anthropic_tools

    @staticmethod
    def _sanitize_chat_completion_tools(
        tools: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        sanitized_tools: List[Dict[str, Any]] = []
        seen_tool_names: Set[str] = set()
        provider_name_re = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
        for tool in tools or []:
            if not isinstance(tool, dict):
                continue
            tool_copy = dict(tool)
            function = tool_copy.get("function") or {}
            if isinstance(function, dict):
                function_copy = dict(function)
                name = str(function_copy.get("name") or "").strip()
                if not name:
                    continue
                if not provider_name_re.match(name):
                    log.warning("Dropping provider-invalid tool schema name: %s", name)
                    continue
                if name in seen_tool_names:
                    log.warning("Dropping duplicate tool schema: %s", name)
                    continue
                seen_tool_names.add(name)
                function_copy["name"] = name
                function_copy["description"] = LLMClient._stringify_tool_description(
                    function_copy.get("description")
                )
                if not isinstance(function_copy.get("parameters"), dict):
                    function_copy["parameters"] = {"type": "object", "properties": {}}
                tool_copy["function"] = function_copy
            else:
                continue
            sanitized_tools.append(tool_copy)
        sanitized_tools.sort(key=lambda tool: str((tool.get("function") or {}).get("name") or ""))
        return sanitized_tools

    @staticmethod
    def _build_anthropic_tool_choice(tool_choice: Any) -> Optional[Dict[str, Any]]:
        if not tool_choice or tool_choice == "auto":
            return None
        if tool_choice in {"required", "any"}:
            return {"type": "any"}
        if tool_choice == "none":
            return {"type": "none"}
        if isinstance(tool_choice, dict):
            function = tool_choice.get("function") or {}
            name = str(function.get("name") or "").strip()
            if name:
                return {"type": "tool", "name": name}
        if isinstance(tool_choice, str):
            return {"type": "tool", "name": tool_choice}
        return None

    def _normalize_anthropic_response(
        self,
        resp_dict: Dict[str, Any],
        target: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        content_blocks = resp_dict.get("content") or []
        text_parts: List[str] = []
        tool_calls: List[Dict[str, Any]] = []
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").strip()
            if block_type == "text":
                text = str(block.get("text") or "")
                if text:
                    text_parts.append(text)
            elif block_type == "tool_use":
                tool_calls.append({
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name") or ""),
                        "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                    },
                })

        raw_usage = resp_dict.get("usage") or {}
        usage: Dict[str, Any] = {
            "prompt_tokens": int(raw_usage.get("input_tokens") or 0),
            "completion_tokens": int(raw_usage.get("output_tokens") or 0),
            "cached_tokens": int(raw_usage.get("cache_read_input_tokens") or 0),
            "cache_write_tokens": int(raw_usage.get("cache_creation_input_tokens") or 0),
            "provider": "anthropic",
            "resolved_model": str(target.get("usage_model") or target.get("resolved_model") or ""),
        }
        if usage["prompt_tokens"] or usage["completion_tokens"]:
            from ouroboros.pricing import estimate_cost

            estimated_cost = estimate_cost(
                usage["resolved_model"],
                usage["prompt_tokens"],
                usage["completion_tokens"],
                usage["cached_tokens"],
                usage["cache_write_tokens"],
            )
            if estimated_cost:
                usage["cost"] = estimated_cost

        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts),
        }
        if tool_calls:
            message["tool_calls"] = tool_calls
        return message, usage

    def _chat_anthropic(
        self,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float] = None,
        no_proxy: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        import requests

        del reasoning_effort  # Anthropic direct works without an extra effort payload here.

        system, anthropic_messages = self._build_anthropic_messages(messages)
        payload: Dict[str, Any] = {
            "model": str(target.get("resolved_model") or ""),
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system
        if temperature is not None:
            payload["temperature"] = temperature

        anthropic_tools = self._build_anthropic_tools(tools)
        if anthropic_tools:
            payload["tools"] = anthropic_tools
            anthropic_tool_choice = self._build_anthropic_tool_choice(tool_choice)
            if anthropic_tool_choice:
                payload["tool_choice"] = anthropic_tool_choice

        url = f"{str(target.get('base_url') or '').rstrip('/')}/messages"
        headers = {
            "x-api-key": str(target.get("api_key") or ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        if no_proxy:
            # Build a session with proxy detection disabled for macOS fork-safety.
            # Use context manager (or explicit close) to avoid connection-pool leaks.
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(url, headers=headers, json=payload, timeout=120)
        else:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
        response.raise_for_status()
        return self._normalize_anthropic_response(response.json(), target)

    def _build_remote_kwargs(
        self,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float],
        tools: Optional[List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        resolved_model = str(target.get("resolved_model") or "")
        token_limit_key = "max_tokens"
        if str(target.get("provider") or "") == "openai" and resolved_model.startswith("gpt-5"):
            token_limit_key = "max_completion_tokens"
        if not target.get("supports_openrouter_extensions"):
            # Strip cache_control from message content blocks — non-OpenRouter providers
            # (OpenAI, openai-compatible, Cloud.ru) do not accept this field.
            clean_messages = self._strip_cache_control(messages)
            kwargs: Dict[str, Any] = {
                "model": resolved_model,
                "messages": clean_messages,
                token_limit_key: max_tokens,
            }
            if temperature is not None:
                kwargs["temperature"] = temperature
            if tools:
                kwargs["tools"] = [
                    {k: v for k, v in t.items() if k != "cache_control"}
                    for t in self._sanitize_chat_completion_tools(tools)
                ]
                kwargs["tool_choice"] = tool_choice
            return kwargs

        effort = normalize_reasoning_effort(reasoning_effort)

        extra_body: Dict[str, Any] = {
            "reasoning": {"effort": effort, "exclude": True},
        }

        if resolved_model.startswith("anthropic/"):
            extra_body["provider"] = {
                "require_parameters": True,
            }

        kwargs: Dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        if tools:
            tools_with_cache = self._sanitize_chat_completion_tools(tools)
            if tools_with_cache:
                last_tool = {**tools_with_cache[-1]}  # copy last tool
                last_tool["cache_control"] = {"type": "ephemeral", "ttl": "1h"}
                tools_with_cache[-1] = last_tool
            kwargs["tools"] = tools_with_cache
            kwargs["tool_choice"] = tool_choice

        # Drop kwargs that this model's OpenRouter providers don't list in
        # `supported_parameters`. Combined with `provider.require_parameters:
        # true` (set above for anthropic/), unknown params cause a 404
        # "No endpoints found that can handle the requested parameters".
        # Capabilities cache returns None for unknown models → no stripping.
        supported = self._get_supported_parameters(resolved_model)
        if supported is not None:
            for sampling_param in ("temperature", "top_p", "top_k"):
                if sampling_param not in supported and sampling_param in kwargs:
                    log.debug(
                        "Model %s does not list %s in supported_parameters; stripping",
                        resolved_model, sampling_param,
                    )
                    kwargs.pop(sampling_param, None)
        return kwargs

    def _normalize_remote_response(
        self,
        resp_dict: Dict[str, Any],
        target: Dict[str, Any],
        skip_cost_fetch: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Normalise a raw OpenAI-compatible response dict into (message, usage).

        skip_cost_fetch=True suppresses the _fetch_generation_cost() call that
        uses requests.get() with default proxy / OS lookup.  Set this whenever
        the call was made inside a forked process (no_proxy=True path) to keep
        the entire call chain free of SCDynamicStore / CFPreferences access.
        Cost is still estimated from token counts via the local pricing table.
        """
        usage = resp_dict.get("usage") or {}
        choices = resp_dict.get("choices") or [{}]
        msg = (choices[0] if choices else {}).get("message") or {}

        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])
        # NB: LM Studio MLX does NOT emit ``cached_tokens`` anywhere in its
        # OpenAI-compatible response (verified 2026-05-02 across
        # ``/v1/chat/completions``, ``/api/v0/chat/completions``, and
        # streaming with ``stream_options.include_usage=true``). The MLX
        # backend's prefix-cache stats are written only to LM Studio's
        # stderr/log output (e.g. ``[cache_wrapper] Prompt cache: using
        # 70623/70821 tokens from cache``). For LM Studio targets,
        # ``cached_tokens=0`` in events.jsonl is therefore the *correct*
        # API-level reading even when the MLX prefix cache is hitting at
        # >99%. If we ever need cache-hit telemetry for LM Studio, the
        # right path is the LM Studio native ``/api/v0/chat/completions``
        # endpoint which exposes ``stats.time_to_first_token`` —
        # a strong cache-proxy signal — but that is its own follow-up.

        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (
                    prompt_details_for_write.get("cache_write_tokens")
                    or prompt_details_for_write.get("cache_creation_tokens")
                    or prompt_details_for_write.get("cache_creation_input_tokens")
                )
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        if target.get("supports_openrouter_extensions") and not skip_cost_fetch:
            if not usage.get("cost"):
                gen_id = resp_dict.get("id") or ""
                if gen_id:
                    cost = self._fetch_generation_cost(gen_id, target)
                    if cost is not None:
                        usage["cost"] = cost

        usage["provider"] = str(target.get("provider") or "openrouter")
        usage["resolved_model"] = str(target.get("usage_model") or target.get("resolved_model") or "")
        if not usage.get("cost") and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            from ouroboros.pricing import estimate_cost

            estimated_cost = estimate_cost(
                usage["resolved_model"],
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("cached_tokens") or 0),
                int(usage.get("cache_write_tokens") or 0),
            )
            if estimated_cost:
                usage["cost"] = estimated_cost

        return msg, usage

    def _chat_remote(
        self,
        target: Dict[str, Any],
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        reasoning_effort: str,
        max_tokens: int,
        tool_choice: str,
        temperature: Optional[float] = None,
        no_proxy: bool = False,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Send a chat request to the resolved remote provider.

        When no_proxy=True a temporary one-shot httpx.Client is built with
        ``trust_env=False`` and an empty mounts map to bypass macOS fork-safe
        proxy detection (SCDynamicStoreCopyProxiesWithOptions).  The client is
        closed in a finally block after the response is received to avoid
        connection-pool leaks.  This flag does not affect other callers.
        """
        if target.get("provider") == "anthropic":
            return self._chat_anthropic(
                target, messages, tools, reasoning_effort, max_tokens, tool_choice, temperature,
                no_proxy=no_proxy,
            )

        if no_proxy:
            _oa_client, _http_client = self._make_no_proxy_client(target)
            try:
                kwargs = self._build_remote_kwargs(
                    target, messages, reasoning_effort, max_tokens, tool_choice, temperature, tools
                )
                resp = _oa_client.chat.completions.create(**kwargs)
                # Pass no_proxy=True to _normalize_remote_response so the
                # _fetch_generation_cost fallback (which uses requests.get with
                # default proxy / OS lookup) is skipped — it would re-introduce
                # the same SCDynamicStore code path that causes the SIGSEGV.
                return self._normalize_remote_response(resp.model_dump(), target, skip_cost_fetch=True)
            finally:
                try:
                    _http_client.close()
                except Exception:
                    pass

        client = self._get_remote_client(target)
        kwargs = self._build_remote_kwargs(
            target, messages, reasoning_effort, max_tokens, tool_choice, temperature, tools
        )
        resp = client.chat.completions.create(**kwargs)
        return self._normalize_remote_response(resp.model_dump(), target)

    def vision_query(
        self,
        prompt: str,
        images: List[Dict[str, Any]],
        model: str = "anthropic/claude-sonnet-4.6",
        max_tokens: int = 32768,
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
        return os.environ.get("OUROBOROS_MODEL", "anthropic/claude-opus-4.6")

    def available_models(self) -> List[str]:
        """Return list of available models from env (for switch_model tool schema)."""
        main = os.environ.get("OUROBOROS_MODEL", "anthropic/claude-opus-4.6")
        code = os.environ.get("OUROBOROS_MODEL_CODE", "")
        light = os.environ.get("OUROBOROS_MODEL_LIGHT", "")
        models = [main]
        if code and code != main:
            models.append(code)
        if light and light != main and light != code:
            models.append(light)
        return models

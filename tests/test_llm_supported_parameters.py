"""Tests for LLMClient.supported_parameters cache and dynamic kwarg filtering.

v4.33.0 adds a per-process cache of OpenRouter model capabilities so we can
strip sampling parameters (`temperature`, `top_p`, `top_k`) the resolved
model doesn't list in `supported_parameters`. Combined with
`provider.require_parameters: true` on Anthropic-prefixed models, unknown
params used to cause 404 "No endpoints found" from OpenRouter (this is why
`anthropic/claude-opus-4.6` was silently dropped from every triad review
for the whole v4.32.x line — it simply doesn't support `temperature`).

These tests cover:
- A known-incompatible model has `temperature` stripped.
- A known-compatible model keeps `temperature`.
- Fetch failure (network/parse error) falls back to broad support — no stripping.
- The cache is populated once per process, not per call.
"""

from __future__ import annotations

from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _reset_llm_cache():
    """Reset the class-level supported_parameters cache before/after each test."""
    from ouroboros.llm import LLMClient
    LLMClient._SUPPORTED_PARAMS_CACHE.clear()
    LLMClient._SUPPORTED_PARAMS_FETCHED = False
    yield
    LLMClient._SUPPORTED_PARAMS_CACHE.clear()
    LLMClient._SUPPORTED_PARAMS_FETCHED = False


def _install_fake_response(monkeypatch, data: dict[str, Any]) -> dict[str, int]:
    """Patch requests.get used by _fetch_openrouter_capabilities with a canned response."""
    call_count = {"n": 0}

    class _Resp:
        status_code = 200
        def json(self):
            return data

    def fake_get(url: str, timeout: int = 15):
        call_count["n"] += 1
        return _Resp()

    import ouroboros.llm as llm_mod
    # _fetch_openrouter_capabilities does `import requests` lazily inside the function,
    # so we patch the module attribute that it will see after the lazy import.
    import requests as _real_requests
    monkeypatch.setattr(_real_requests, "get", fake_get)
    return call_count


class TestSupportedParametersFilter:
    def test_temperature_stripped_for_unsupported_model(self, monkeypatch):
        from ouroboros.llm import LLMClient

        _install_fake_response(monkeypatch, {
            "data": [{
                "id": "anthropic/claude-opus-4.6",
                "supported_parameters": [
                    "include_reasoning", "max_tokens", "reasoning",
                    "response_format", "stop", "structured_outputs",
                    "tool_choice", "tools", "verbosity",
                ],
            }]
        })

        client = LLMClient(api_key="test")
        target = client._resolve_remote_target("anthropic/claude-opus-4.6")
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=0.2,
            tools=None,
        )
        assert "temperature" not in kwargs, (
            "temperature must be stripped when the model's supported_parameters omits it"
        )

    def test_temperature_kept_for_supported_model(self, monkeypatch):
        from ouroboros.llm import LLMClient

        _install_fake_response(monkeypatch, {
            "data": [{
                "id": "anthropic/claude-opus-4.6",
                "supported_parameters": [
                    "include_reasoning", "max_tokens", "reasoning",
                    "response_format", "stop", "structured_outputs",
                    "temperature", "tool_choice", "tools", "top_k",
                    "top_p", "verbosity",
                ],
            }]
        })

        client = LLMClient(api_key="test")
        target = client._resolve_remote_target("anthropic/claude-opus-4.6")
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=0.2,
            tools=None,
        )
        assert kwargs.get("temperature") == 0.2

    def test_fetch_failure_falls_back_to_no_stripping(self, monkeypatch):
        """When fetch fails (network/parse/missing), cache is empty and no params are stripped."""
        from ouroboros.llm import LLMClient

        def exploding_get(url: str, timeout: int = 15):
            raise RuntimeError("simulated transport failure")

        import requests
        monkeypatch.setattr(requests, "get", exploding_get)

        client = LLMClient(api_key="test")
        target = client._resolve_remote_target("anthropic/claude-opus-4.6")
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=0.2,
            tools=None,
        )
        # The cache is empty → _get_supported_parameters returns None → no stripping.
        # Temperature survives (zero-regression fallback when offline).
        assert kwargs.get("temperature") == 0.2

    def test_opus47_temperature_stripped_even_when_capability_fetch_fails(self, monkeypatch):
        from ouroboros.llm import LLMClient

        def exploding_get(url: str, timeout: int = 15):
            raise RuntimeError("simulated transport failure")

        import requests
        monkeypatch.setattr(requests, "get", exploding_get)

        client = LLMClient(api_key="test")
        target = client._resolve_remote_target("anthropic/claude-opus-4.7")
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=0.2,
            tools=None,
        )

        assert kwargs["model"] == "anthropic/claude-opus-4.7"
        assert "temperature" not in kwargs

    def test_cache_fetched_at_most_once(self, monkeypatch):
        from ouroboros.llm import LLMClient

        call_count = _install_fake_response(monkeypatch, {
            "data": [{
                "id": "anthropic/claude-opus-4.6",
                "supported_parameters": ["max_tokens"],
            }]
        })

        client = LLMClient(api_key="test")
        target = client._resolve_remote_target("anthropic/claude-opus-4.6")
        # Two back-to-back calls: the second must hit the cache, not the network.
        for _ in range(2):
            client._build_remote_kwargs(
                target=target,
                messages=[{"role": "user", "content": "hi"}],
                reasoning_effort="medium",
                max_tokens=256,
                tool_choice="auto",
                temperature=0.2,
                tools=None,
            )
        assert call_count["n"] == 1, (
            f"Expected supported_parameters fetch to run exactly once per process, "
            f"got {call_count['n']} calls"
        )

    def test_unknown_model_falls_back_to_no_stripping(self, monkeypatch):
        """A model missing from OpenRouter's /models list keeps all kwargs."""
        from ouroboros.llm import LLMClient

        _install_fake_response(monkeypatch, {
            "data": [{
                "id": "anthropic/claude-opus-4.6",
                "supported_parameters": ["max_tokens"],  # no temperature
            }]
        })

        client = LLMClient(api_key="test")
        # Query a model NOT in our fake response
        target = client._resolve_remote_target("anthropic/future-unknown-model")
        kwargs = client._build_remote_kwargs(
            target=target,
            messages=[{"role": "user", "content": "hi"}],
            reasoning_effort="medium",
            max_tokens=256,
            tool_choice="auto",
            temperature=0.2,
            tools=None,
        )
        # Unknown model → cache miss → None → no stripping
        assert kwargs.get("temperature") == 0.2

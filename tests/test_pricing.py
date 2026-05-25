"""Tests for ouroboros.pricing — extracted in v3.13.1, zero coverage until now."""

import os
import queue
import threading
import pytest
from unittest.mock import patch, MagicMock

from ouroboros.pricing import (
    MODEL_PRICING_STATIC,
    estimate_cost,
    infer_api_key_type,
    infer_model_category,
    emit_llm_usage_event,
    get_pricing,
)
from ouroboros.llm import fetch_openrouter_pricing

# The fetch function lives in ouroboros.llm but is imported dynamically
# inside get_pricing(). We must mock it at its source module.
FETCH_PRICING_PATH = "ouroboros.llm.fetch_openrouter_pricing"


# --- estimate_cost ---

class TestEstimateCost:
    """Cost estimation from token counts."""

    def test_known_model_no_cache(self):
        # Sonnet 4.6: input=$3/M, cached=$0.30/M, output=$15/M
        cost = estimate_cost(
            "anthropic/claude-sonnet-4.6",
            prompt_tokens=1000, completion_tokens=500, cached_tokens=0,
        )
        expected = 1000 * 3.0 / 1e6 + 500 * 15.0 / 1e6  # 0.003 + 0.0075
        assert abs(cost - expected) < 1e-6

    def test_known_model_with_cache(self):
        cost = estimate_cost(
            "anthropic/claude-sonnet-4.6",
            prompt_tokens=10000, completion_tokens=1000,
            cached_tokens=8000,
        )
        # 2000 regular input + 8000 cached + 1000 output
        expected = (2000 * 3.0 + 8000 * 0.30 + 1000 * 15.0) / 1e6
        assert abs(cost - expected) < 1e-6

    def test_cache_write_tokens_are_subtracted_from_regular_input(self):
        with patch("ouroboros.pricing.get_pricing", return_value={
            "anthropic/claude-sonnet-4.6": (3.0, 0.30, 15.0),
        }):
            cost = estimate_cost(
                "anthropic/claude-sonnet-4.6",
                prompt_tokens=10000, completion_tokens=1000,
                cached_tokens=3000, cache_write_tokens=2000,
            )
        # 5000 regular input + 3000 cached reads + 2000 cache writes at 1.25x + output
        expected = (5000 * 3.0 + 3000 * 0.30 + 2000 * (3.0 * 1.25) + 1000 * 15.0) / 1e6
        assert abs(cost - expected) < 1e-6

    def test_anthropic_one_hour_cache_write_multiplier(self):
        with patch("ouroboros.pricing.get_pricing", return_value={
            "anthropic/claude-sonnet-4.6": (3.0, 0.30, 15.0),
        }):
            cost = estimate_cost(
                "anthropic/claude-sonnet-4.6",
                prompt_tokens=2000, completion_tokens=0,
                cached_tokens=0, cache_write_tokens=2000,
                prompt_cache_ttl="1h",
            )
        expected = 2000 * (3.0 * 2.0) / 1e6
        assert abs(cost - expected) < 1e-6

    def test_non_anthropic_cache_write_defaults_to_input_price(self):
        with patch("ouroboros.pricing.get_pricing", return_value={
            "google/gemini-3.5-flash": (1.50, 0.15, 9.00),
        }):
            cost = estimate_cost(
                "google/gemini-3.5-flash",
                prompt_tokens=2000, completion_tokens=0,
                cached_tokens=0, cache_write_tokens=1000,
            )
        expected = (1000 * 1.50 + 1000 * 1.50) / 1e6
        assert abs(cost - expected) < 1e-6

    def test_explicit_cache_write_price_overrides_fallback(self):
        with patch("ouroboros.pricing.get_pricing", return_value={
            "provider/model": (1.0, 0.2, 1.7, 3.0),
        }):
            cost = estimate_cost(
                "provider/model",
                prompt_tokens=3000, completion_tokens=1000,
                cached_tokens=500, cache_write_tokens=1000,
                prompt_cache_ttl="1h",
            )
        expected = (1500 * 1.0 + 500 * 0.2 + 1000 * 1.7 + 1000 * 3.0) / 1e6
        assert abs(cost - expected) < 1e-6

    def test_unknown_model_returns_zero(self):
        cost = estimate_cost("unknown/model-xyz", 1000, 500)
        assert cost == 0.0

    def test_zero_tokens(self):
        cost = estimate_cost("anthropic/claude-sonnet-4.6", 0, 0)
        assert cost == 0.0

    def test_prefix_match(self):
        """Models with suffixes should match via longest prefix."""
        cost = estimate_cost(
            "anthropic/claude-sonnet-4.6:beta",
            prompt_tokens=1000, completion_tokens=0,
        )
        # Should match "anthropic/claude-sonnet-4.6" prefix
        assert cost > 0

    def test_cached_greater_than_prompt_clamped(self):
        """If cached > prompt (shouldn't happen, but defensive), regular_input is 0."""
        cost = estimate_cost(
            "anthropic/claude-sonnet-4.6",
            prompt_tokens=100, completion_tokens=0, cached_tokens=200,
        )
        # regular_input = max(0, 100-200) = 0, only cached portion
        expected = 200 * 0.30 / 1e6
        assert abs(cost - expected) < 1e-6

    def test_all_static_models_have_valid_pricing_tuple(self):
        """Every static entry is (input, cached, output) or (input, cached, write, output)."""
        for model, prices in MODEL_PRICING_STATIC.items():
            assert len(prices) in {3, 4}, f"{model} has {len(prices)} prices, expected 3 or 4"
            assert all(isinstance(p, (int, float)) for p in prices), f"{model} has non-numeric prices"
            assert all(p >= 0 for p in prices), f"{model} has negative prices"

    def test_gpt_55_static_pricing_is_registered(self):
        assert MODEL_PRICING_STATIC["openai/gpt-5.5"] == (1.75, 0.175, 14.0)
        assert MODEL_PRICING_STATIC["openai/gpt-5.5-pro"] == (1.75, 0.175, 14.0)
        assert MODEL_PRICING_STATIC["openai/gpt-5.5-mini"] == (0.75, 0.075, 4.50)
        assert MODEL_PRICING_STATIC["google/gemini-3.5-flash"] == (1.50, 0.15, 9.00)
        assert MODEL_PRICING_STATIC["google/gemini-3.1-pro-preview"] == (2.0, 0.20, 12.0)
        assert MODEL_PRICING_STATIC["google/gemini-3.1-flash-lite"] == (0.25, 0.025, 1.50)
        assert MODEL_PRICING_STATIC["google/gemini-3-flash-preview"] == (0.15, 0.015, 0.60)


# --- infer_api_key_type ---

class TestInferApiKeyType:

    @pytest.mark.parametrize("model,expected", [
        ("anthropic/claude-sonnet-4.6", "openrouter"),
        ("google/gemini-3.5-flash", "openrouter"),
        ("openai/gpt-5.2", "openrouter"),
        ("x-ai/grok-3-mini", "openrouter"),
        ("qwen/qwen3.5-plus-02-15", "openrouter"),
    ])
    def test_openrouter_prefixes(self, model, expected):
        assert infer_api_key_type(model) == expected

    def test_bare_claude_is_anthropic(self):
        assert infer_api_key_type("claude-sonnet-4.6") == "anthropic"

    def test_provider_override_uses_official_openai(self):
        assert infer_api_key_type("openai/gpt-5.2", provider="openai") == "openai"

    def test_openai_double_colon_is_official_openai(self):
        assert infer_api_key_type("openai::gpt-5.2") == "openai"

    def test_anthropic_double_colon_is_direct_anthropic(self):
        assert infer_api_key_type("anthropic::claude-sonnet-4-6") == "anthropic"

    def test_unknown_defaults_openrouter(self):
        assert infer_api_key_type("some-random-model") == "openrouter"


# --- infer_model_category ---

class TestInferModelCategory:

    def test_matches_main_model(self):
        with patch.dict(os.environ, {"OUROBOROS_MODEL": "anthropic/claude-sonnet-4.6"}):
            assert infer_model_category("anthropic/claude-sonnet-4.6") == "main"

    def test_matches_light_model(self):
        with patch.dict(os.environ, {"OUROBOROS_MODEL_LIGHT": "google/gemini-3.5-flash"}, clear=True):
            assert infer_model_category("google/gemini-3.5-flash") == "light"

    def test_matches_local_usage_suffix(self):
        with patch.dict(os.environ, {"OUROBOROS_MODEL_LIGHT": "google/gemini-3.5-flash"}, clear=True):
            assert infer_model_category("google/gemini-3.5-flash (local)") == "light"

    def test_matches_openai_double_colon_against_resolved_usage_name(self):
        with patch.dict(os.environ, {"OUROBOROS_MODEL": "openai::gpt-5.2"}):
            assert infer_model_category("openai/gpt-5.2") == "main"

    def test_matches_anthropic_double_colon_against_normalized_usage_name(self):
        with patch.dict(os.environ, {"OUROBOROS_MODEL": "anthropic::claude-sonnet-4.6"}):
            assert infer_model_category("anthropic/claude-sonnet-4-6") == "main"

    def test_no_match_returns_other(self):
        with patch.dict(os.environ, {}, clear=True):
            assert infer_model_category("unknown/model") == "other"


# --- emit_llm_usage_event ---

class TestEmitLlmUsageEvent:

    def test_emits_to_queue(self):
        q = queue.Queue()
        emit_llm_usage_event(
            event_queue=q,
            task_id="test-123",
            model="anthropic/claude-sonnet-4.6",
            usage={"prompt_tokens": 1000, "completion_tokens": 500},
            cost=0.0105,
            category="task",
        )
        event = q.get_nowait()
        assert event["type"] == "llm_usage"
        assert event["task_id"] == "test-123"
        assert event["model"] == "anthropic/claude-sonnet-4.6"
        assert event["prompt_tokens"] == 1000
        assert event["completion_tokens"] == 500
        assert event["cost"] == 0.0105
        assert event["category"] == "task"
        assert "ts" in event

    def test_provider_override_sets_api_key_type(self):
        q = queue.Queue()
        emit_llm_usage_event(
            event_queue=q,
            task_id="test-123",
            model="openai/gpt-5.2",
            usage={"prompt_tokens": 100, "completion_tokens": 50},
            cost=0.01,
            provider="openai",
        )
        event = q.get_nowait()
        assert event["provider"] == "openai"
        assert event["api_key_type"] == "openai"

    def test_cost_estimated_override_survives_usage_cost(self):
        q = queue.Queue()
        emit_llm_usage_event(
            event_queue=q,
            task_id="test-123",
            model="anthropic/claude-sonnet-4.6",
            usage={"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.01},
            cost=0.01,
            cost_estimated=True,
        )

        event = q.get_nowait()
        assert event["cost_estimated"] is True

    def test_none_queue_no_error(self):
        # Should silently do nothing
        emit_llm_usage_event(None, "t", "m", {}, 0.0)

    def test_missing_usage_fields_default_zero(self):
        q = queue.Queue()
        emit_llm_usage_event(q, "t", "m", {}, 0.0)
        event = q.get_nowait()
        assert event["prompt_tokens"] == 0
        assert event["completion_tokens"] == 0
        assert event["cached_tokens"] == 0

    def test_full_queue_no_crash(self):
        q = queue.Queue(maxsize=1)
        q.put("filler")  # fill it
        # Should not raise
        emit_llm_usage_event(q, "t", "m", {}, 0.0)


# --- get_pricing ---

class TestGetPricing:

    def setup_method(self):
        """Reset module-level caching state before each test."""
        import ouroboros.pricing as mod
        mod._pricing_fetched = False
        mod._cached_pricing = None

    def test_returns_static_when_fetch_fails(self):
        with patch(FETCH_PRICING_PATH, side_effect=Exception("network")):
            pricing = get_pricing()
        # Should still have static entries
        assert "anthropic/claude-sonnet-4.6" in pricing
        assert len(pricing) >= len(MODEL_PRICING_STATIC)

    def test_merges_live_pricing(self):
        live = {"new-model/test": (1.0, 0.1, 2.0)}
        with patch(FETCH_PRICING_PATH, return_value=live):
            pricing = get_pricing()
        # Live had < 5 entries, should NOT merge
        assert "new-model/test" not in pricing

    def test_merges_live_pricing_when_enough_entries(self):
        live = {f"provider/model-{i}": (1.0, 0.1, 2.0) for i in range(6)}
        with patch(FETCH_PRICING_PATH, return_value=live):
            pricing = get_pricing()
        assert "provider/model-0" in pricing
        # Static entries still present
        assert "anthropic/claude-sonnet-4.6" in pricing

    def test_fetch_openrouter_pricing_maps_cache_write_price(self):
        class FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": [
                        {
                            "id": "google/gemini-test",
                            "pricing": {
                                "prompt": "0.000001",
                                "input_cache_read": "0.0000001",
                                "input_cache_write": "0.00000125",
                                "completion": "0.000003",
                            },
                        }
                    ] * 6
                }

        with patch("requests.get", return_value=FakeResponse()):
            pricing = fetch_openrouter_pricing()

        assert pricing["google/gemini-test"] == (1.0, 0.1, 1.25, 3.0)

    def test_caches_after_successful_fetch(self):
        import ouroboros.pricing as mod
        live = {f"p/m-{i}": (1.0, 0.1, 2.0) for i in range(6)}
        mock_fetch = MagicMock(return_value=live)
        with patch(FETCH_PRICING_PATH, mock_fetch):
            get_pricing()
            get_pricing()  # Second call should not fetch again
        mock_fetch.assert_called_once()

    def test_retries_after_failed_fetch(self):
        import ouroboros.pricing as mod
        mock_fetch = MagicMock(side_effect=Exception("down"))
        with patch(FETCH_PRICING_PATH, mock_fetch):
            get_pricing()
            get_pricing()  # Should retry since first failed
        assert mock_fetch.call_count == 2

    def test_ignores_small_live_pricing(self):
        """If fetch returns < 5 entries, don't merge (probably broken)."""
        import ouroboros.pricing as mod
        live = {"a/b": (1, 0.1, 2)}  # Only 1 entry
        with patch(FETCH_PRICING_PATH, return_value=live):
            pricing = get_pricing()
        assert "a/b" not in pricing  # Not merged because < 5

    def test_thread_safety(self):
        """Multiple threads calling get_pricing() simultaneously shouldn't crash."""
        import ouroboros.pricing as mod
        results = []
        errors = []

        def worker():
            try:
                p = get_pricing()
                results.append(len(p))
            except Exception as e:
                errors.append(e)

        with patch(FETCH_PRICING_PATH, return_value={}):
            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 10
        # All threads should get the same count
        assert len(set(results)) == 1

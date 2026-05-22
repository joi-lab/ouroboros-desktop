from unittest.mock import patch

from ouroboros.loop import _provider_failure_hint
from ouroboros.loop_llm_call import call_llm_with_retry


class _FailingLLM:
    def chat(self, **kwargs):
        raise RuntimeError("AuthenticationError('401 invalid_api_key')")


class _SuccessfulLLM:
    def chat(self, **kwargs):
        return {"content": "ok"}, {"provider": "anthropic", "resolved_model": "anthropic/claude-sonnet-4-6"}


def test_call_llm_with_retry_records_last_error(tmp_path):
    usage = {}

    msg, cost = call_llm_with_retry(
        _FailingLLM(),
        [{"role": "user", "content": "hi"}],
        "openai::gpt-5.5",
        None,
        "medium",
        1,
        tmp_path,
        "task-1",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg is None
    assert cost == 0.0
    assert "invalid_api_key" in usage["_last_llm_error"]


def test_call_llm_with_retry_clears_stale_last_error_on_success(tmp_path):
    usage = {"_last_llm_error": "old error"}

    msg, _cost = call_llm_with_retry(
        _SuccessfulLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic::claude-sonnet-4-6",
        None,
        "medium",
        1,
        tmp_path,
        "task-2",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg == {"content": "ok"}
    assert "_last_llm_error" not in usage


def test_provider_failure_hint_formats_detail():
    hint = _provider_failure_hint({"_last_llm_error": "  AuthenticationError('401 invalid_api_key')  "})

    assert hint == " Last provider error: AuthenticationError('401 invalid_api_key')"


def test_provider_failure_hint_empty_without_error():
    assert _provider_failure_hint({}) == ""


def test_call_llm_with_retry_accumulates_estimated_cost(tmp_path):
    import queue

    class _EstimatedCostLLM:
        def chat(self, **kwargs):
            return (
                {"content": "ok"},
                {
                    "provider": "openai",
                    "resolved_model": "openai/gpt-5.5",
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost": 0.0,
                },
            )

    usage = {}
    event_queue = queue.Queue()
    with patch("ouroboros.loop_llm_call.estimate_cost", return_value=0.123456):
        _msg, _cost = call_llm_with_retry(
            _EstimatedCostLLM(),
            [{"role": "user", "content": "hi"}],
            "openai::gpt-5.5",
            None,
            "medium",
            1,
            tmp_path,
            "task-3",
            1,
            event_queue,
            usage,
            "task",
            False,
        )

    assert usage["cost"] == 0.123456
    events = [event_queue.get_nowait() for _ in range(event_queue.qsize())]
    usage_event = next(evt for evt in events if evt.get("type") == "llm_usage")
    assert usage_event["cost_estimated"] is True

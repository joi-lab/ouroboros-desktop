"""Tests for ``ouroboros.tools.search._web_search`` (OpenAI Responses API).

Merged from former ``test_search_tool.py`` (provider routing / required-env
contract) and ``test_web_search_streaming.py`` (streaming events, progress,
cost). Both files exercised the same `_web_search` function with overlapping
mocks; the merged file shares one ``_FakeStream`` / event factory.
"""
from __future__ import annotations

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

import ouroboros.tools.search as search_module
from ouroboros.tools.search import _web_search


# ---------------------------------------------------------------------------
# Shared streaming fixtures
# ---------------------------------------------------------------------------


def _make_event(etype: str, **kwargs):
    return types.SimpleNamespace(type=etype, **kwargs)


def _make_completed_event(input_tokens: int = 100, output_tokens: int = 50):
    usage_obj = MagicMock()
    usage_obj.model_dump.return_value = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    resp_obj = MagicMock()
    resp_obj.usage = usage_obj
    return _make_event("response.completed", response=resp_obj)


class _FakeStream:
    """Iterable that yields pre-built streaming events."""

    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)


@pytest.fixture
def ctx():
    c = MagicMock()
    c.pending_events = []
    c.emit_progress_fn = MagicMock()
    c.task_id = "task-web"
    c.task_metadata = {
        "root_task_id": "root-web",
        "parent_task_id": "parent-web",
        "delegation_role": "subagent",
    }
    return c


@pytest.fixture
def patch_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


@pytest.fixture
def mock_openai():
    """Inject a fake openai module so the lazy import inside _web_search works."""
    mock_client = MagicMock()
    mock_module = MagicMock()
    mock_module.OpenAI.return_value = mock_client
    with patch.dict(sys.modules, {"openai": mock_module}):
        yield mock_client


# ---------------------------------------------------------------------------
# Provider routing / required-env contract
# ---------------------------------------------------------------------------


def test_web_search_requires_official_openai_without_legacy_base(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "compat-key")

    result = json.loads(
        search_module._web_search(types.SimpleNamespace(pending_events=[]), "latest news")
    )

    assert result == {
        "error": "web_search requires the official OpenAI Responses API. "
        "Set OPENAI_API_KEY and leave OPENAI_BASE_URL empty."
    }


def test_web_search_uses_official_openai_responses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", raising=False)

    calls: dict = {}

    class _Usage:
        def model_dump(self):
            return {"input_tokens": 11, "output_tokens": 7}

    class _CompletedResponse:
        usage = _Usage()

    class _Stream:
        def __iter__(self):
            yield _make_event("response.web_search_call.searching",
                              item_id="ws1", output_index=0, sequence_number=1)
            yield _make_event("response.output_text.delta",
                              delta="fresh answer", content_index=0,
                              item_id="m1", output_index=1, sequence_number=2,
                              logprobs=[])
            yield _make_event("response.completed",
                              response=_CompletedResponse(), sequence_number=3)

    class _Responses:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs
            return _Stream()

    class _Client:
        def __init__(self, api_key=None, base_url=None):
            calls["api_key"] = api_key
            calls["base_url"] = base_url
            self.responses = _Responses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_Client))
    request_ctx = types.SimpleNamespace(pending_events=[])

    result = json.loads(
        search_module._web_search(request_ctx, "latest news", model="gpt-5.2")
    )

    assert result == {"answer": "fresh answer", "sources": []}
    assert calls["api_key"] == "openai-key"
    assert calls["base_url"] is None
    assert calls["kwargs"]["model"] == "gpt-5.2"
    assert calls["kwargs"]["stream"] is True
    assert calls["kwargs"]["tools"][0]["type"] == "web_search"
    assert request_ctx.pending_events[0]["provider"] == "openai"
    assert request_ctx.pending_events[0]["model"] == "gpt-5.2"


# ---------------------------------------------------------------------------
# Streaming behavior
# ---------------------------------------------------------------------------


def test_streaming_emits_progress_on_search(ctx, patch_env, mock_openai):
    events = [
        _make_event("response.web_search_call.in_progress", item_id="ws1", output_index=0, sequence_number=1),
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=2),
        _make_event("response.output_text.delta", delta="Hello ", content_index=0,
                    item_id="m1", output_index=1, sequence_number=3, logprobs=[]),
        _make_event("response.output_text.delta", delta="world", content_index=0,
                    item_id="m1", output_index=1, sequence_number=4, logprobs=[]),
        _make_completed_event(200, 80),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    result = _web_search(ctx, "test query")

    ctx.emit_progress_fn.assert_called_once()
    call_text = ctx.emit_progress_fn.call_args[0][0]
    assert "test query" in call_text

    data = json.loads(result)
    assert data["answer"] == "Hello world"

    mock_openai.responses.create.assert_called_once()
    call_kwargs = mock_openai.responses.create.call_args[1]
    assert call_kwargs["stream"] is True


def test_streaming_cost_tracking(ctx, patch_env, mock_openai):
    events = [
        _make_event("response.output_text.delta", delta="Answer", content_index=0,
                    item_id="m1", output_index=0, sequence_number=1, logprobs=[]),
        _make_completed_event(500, 100),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    _web_search(ctx, "cost test")

    assert len(ctx.pending_events) == 1
    ev = ctx.pending_events[0]
    assert ev["type"] == "llm_usage"
    assert ev["prompt_tokens"] == 500
    assert ev["completion_tokens"] == 100
    assert ev["model_category"] == "websearch"
    assert ev["task_id"] == "task-web"
    assert ev["root_task_id"] == "root-web"
    assert ev["parent_task_id"] == "parent-web"
    assert ev["delegation_role"] == "subagent"
    assert ev["source"] == "web_search"
    assert ev["cost"] > 0


def test_streaming_returns_cited_sources(ctx, patch_env, mock_openai):
    class _Usage:
        def model_dump(self):
            return {"input_tokens": 50, "output_tokens": 10}

    class _CompletedResponse:
        usage = _Usage()

        def model_dump(self):
            return {
                "output": [{
                    "content": [{
                        "type": "output_text",
                        "annotations": [{
                            "type": "url_citation",
                            "url": "https://example.com/article",
                            "title": "Example Article",
                            "snippet": "Short source summary",
                        }],
                    }],
                }]
            }

    events = [
        _make_event("response.output_text.delta", delta="Answer", content_index=0,
                    item_id="m1", output_index=0, sequence_number=1, logprobs=[]),
        _make_event("response.completed", response=_CompletedResponse(), sequence_number=2),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    result = json.loads(_web_search(ctx, "source test"))

    assert result["answer"] == "Answer"
    assert result["sources"] == [{
        "url": "https://example.com/article",
        "title": "Example Article",
        "snippet": "Short source summary",
    }]


def test_streaming_sanitizes_progress_and_cited_sources(ctx, patch_env, mock_openai):
    leaked_secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"

    class _Usage:
        def model_dump(self):
            return {"input_tokens": 50, "output_tokens": 10}

    class _CompletedResponse:
        usage = _Usage()

        def model_dump(self):
            return {
                "output": [{
                    "content": [{
                        "type": "output_text",
                        "annotations": [{
                            "type": "url_citation",
                            "url": f"https://user:{leaked_secret}@example.com/article?token={leaked_secret}",
                            "title": f"Leaked {leaked_secret}",
                            "snippet": f"Snippet {leaked_secret}",
                        }],
                    }],
                }]
            }

    events = [
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=1),
        _make_event("response.output_text.delta", delta="Answer", content_index=0,
                    item_id="m1", output_index=0, sequence_number=2, logprobs=[]),
        _make_event("response.completed", response=_CompletedResponse(), sequence_number=3),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    result = json.loads(_web_search(ctx, f"query {leaked_secret}"))

    progress_text = ctx.emit_progress_fn.call_args[0][0]
    serialized = json.dumps(result)
    assert leaked_secret not in progress_text
    assert leaked_secret not in serialized
    assert "***REDACTED***" in progress_text
    assert "***REDACTED***" in serialized


def test_web_search_sanitizes_provider_errors(ctx, patch_env, monkeypatch):
    leaked_secret = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"

    class _Responses:
        def create(self, **_kwargs):
            raise RuntimeError(f"provider rejected Authorization: Bearer {leaked_secret}")

    class _Client:
        def __init__(self, api_key=None, base_url=None):
            self.responses = _Responses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_Client))

    result = json.loads(_web_search(ctx, "error query"))

    assert result["error"].startswith("OpenAI web search failed (RuntimeError):")
    assert leaked_secret not in result["error"]
    assert "***REDACTED***" in result["error"]


def test_streaming_no_progress_without_search_events(ctx, patch_env, mock_openai):
    events = [
        _make_event("response.output_text.delta", delta="Direct answer", content_index=0,
                    item_id="m1", output_index=0, sequence_number=1, logprobs=[]),
        _make_completed_event(50, 20),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    result = _web_search(ctx, "simple query")

    ctx.emit_progress_fn.assert_not_called()
    data = json.loads(result)
    assert data["answer"] == "Direct answer"


def test_streaming_empty_text_fallback(ctx, patch_env, mock_openai):
    events = [_make_completed_event(10, 0)]
    mock_openai.responses.create.return_value = _FakeStream(events)

    result = _web_search(ctx, "empty query")

    data = json.loads(result)
    assert data["answer"] == "(no answer)"


def test_streaming_progress_fires_only_once(ctx, patch_env, mock_openai):
    events = [
        _make_event("response.web_search_call.in_progress", item_id="ws1", output_index=0, sequence_number=1),
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=2),
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=3),
        _make_event("response.output_text.delta", delta="Result", content_index=0,
                    item_id="m1", output_index=1, sequence_number=4, logprobs=[]),
        _make_completed_event(100, 50),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    _web_search(ctx, "multi-search query")

    assert ctx.emit_progress_fn.call_count == 1

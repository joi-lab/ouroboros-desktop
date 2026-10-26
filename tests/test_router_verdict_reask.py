"""OpenRouter's own final verdict is re-asked once without fallbacks.

The recorded shape (BYOK incident receipts, 2026-10-22): an HTTP-200 stream whose only
frame is OpenRouter's ``402 payment_required`` (a credit check on paid shared capacity),
with ``openrouter_metadata.attempts`` listing twelve Anthropic attempts that each answered
400; the same request with ``provider.allow_fallbacks=False`` answered the upstream's own
``400 prompt is too long: 1067828 tokens > 1000000 maximum``. Nothing here touches the
network: the SDK create call is a scripted fake, and accounting runs on a temp root.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import openai
import pytest

from ouroboros import usage_accounting as ua
from ouroboros.llm import LLMClient
from ouroboros.llm_attempt import single_physical_send
from ouroboros.llm_stream import AssembledResponse, ProviderStreamError
from ouroboros.loop_llm_call import classify_llm_exception
from ouroboros.openrouter_attribution import OPENROUTER_APP_HEADERS
from tests._usage_store_testing import ledger_rows

MODEL = "anthropic/claude-opus-5.5"
OVERFLOW = "prompt is too long: 1067828 tokens > 1000000 maximum"
CREDITS = ("This request's maximum cost exceeds your available credits. "
           "Add credits, or lower max_tokens or prompt size.")


def receipt(*statuses):
    """The router receipt as the incident recorded it (provider display names, statuses only)."""
    return {"requested": MODEL, "strategy": "direct", "summary": f"available=16, attempts={len(statuses)}",
            "attempt": len(statuses), "is_byok": False,
            "attempts": [{"provider": "Anthropic", "model": "anthropic/claude-opus-5.5-20260921", "status": s}
                         for s in statuses]}


def error_frame(code, message, metadata):
    frame = {"id": f"gen-{code}", "model": "unknown", "provider": "unknown",
             "error": {"code": code, "message": message,
                       "metadata": {"error_type": "payment_required" if code == 402 else "invalid_request"}}}
    if metadata is not None:
        frame["openrouter_metadata"] = metadata
    return frame


MASKED = error_frame(402, CREDITS, receipt(*[400] * 12))
UPSTREAM = error_frame(400, OVERFLOW, receipt(*[400] * 12))
ANSWER = {"id": "gen-answer", "choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": "stop"}],
          "usage": {"cost": 0.5, "prompt_tokens": 10, "completion_tokens": 2}}


class Wire:
    """An SSE body that yields the given frames, like the SDK's raw stream response."""

    def __init__(self, *frames):
        self.chunks = [f if isinstance(f, bytes) else ("data: " + json.dumps(f) + "\n\n").encode() for f in frames]
        self.headers = {}

    def iter_bytes(self):
        yield from self.chunks

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk

    def close(self):
        pass

    async def aclose(self):
        pass


def http_error(status, body):
    """A real SDK status error: ``body`` keeps only ``error``; the read response keeps all."""
    response = httpx.Response(status, json=body, request=httpx.Request(
        "POST", "https://openrouter.ai/api/v1/chat/completions"))
    error_class = openai.BadRequestError if status == 400 else openai.APIStatusError
    return error_class(f"Error code: {status} - {json.dumps(body)}", response=response, body=body.get("error", body))


def target(provider="openrouter"):
    return {"provider": provider, "resolved_model": MODEL, "usage_model": MODEL,
            "base_url": "https://openrouter.ai/api/v1", "api_key": "fixture-key",
            "supports_openrouter_extensions": provider == "openrouter", "supports_generation_cost": True}


def request_kwargs(*, stream=True, routing=None):
    kwargs = {"model": MODEL, "messages": [{"role": "user", "content": "hello"}], "max_tokens": 100,
              "extra_body": {"reasoning": {"effort": "xhigh", "exclude": False},
                             "session_id": "ouroboros-session-fixture",
                             "provider": dict(routing or {"require_parameters": True})}}
    if stream:
        kwargs.update(stream=True, stream_options={"include_usage": True})
    return kwargs


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    import ouroboros.pricing as pricing
    import ouroboros.request_wire_contract as wire

    monkeypatch.setattr(wire, "canonical_wire_evidence_root", lambda: tmp_path)
    monkeypatch.setattr(pricing, "estimate_cost_optional", lambda *a, **k: None)
    monkeypatch.setattr(ua, "_reservation_cost", lambda request: 2.0)
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    with ua.usage_scope(ua.UsageScope(drive_root=tmp_path, task_id="reask-task", root_task_id="reask-task")):
        yield tmp_path


def drive(script, *, asynchronous=False, provider="openrouter", kwargs=None):
    """Run one ladder call over scripted replies; return (result or raised error, sends)."""
    script, sends = list(script), []

    def reply(candidate):
        sends.append(candidate)
        item = script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def create_async(**candidate):
        return reply(candidate)

    client = LLMClient()
    payload = kwargs or request_kwargs()
    try:
        if asynchronous:
            result = asyncio.run(client._create_chat_completion_with_retries_async(
                create_async, payload, target(provider)))
        else:
            result = client._create_chat_completion_with_retries(
                lambda **candidate: reply(candidate), payload, target(provider))
    except Exception as exc:  # noqa: BLE001 - the raised error is the outcome under test
        result = exc
    assert not script, "every scripted reply was consumed"
    return result, sends


def routing(send):
    return send["extra_body"]["provider"]


@pytest.mark.parametrize("asynchronous", [False, True])
def test_masked_402_is_reasked_once_and_the_upstream_overflow_classifies(isolated, asynchronous):
    outcome, sends = drive([Wire(MASKED), Wire(UPSTREAM)], asynchronous=asynchronous)

    assert len(sends) == 2
    assert "allow_fallbacks" not in routing(sends[0])
    # The re-ask forbids fallbacks only: the owner/route routing and the other carriers stay.
    assert routing(sends[1]) == {"require_parameters": True, "allow_fallbacks": False}
    assert "only" not in routing(sends[1])  # attempts name providers, never slugs
    assert sends[1]["extra_body"]["session_id"] == sends[0]["extra_body"]["session_id"]
    assert sends[1]["messages"] == sends[0]["messages"]
    assert isinstance(outcome, ProviderStreamError) and outcome.status_code == 400
    classification = classify_llm_exception(outcome)
    assert classification.kind == "context_overflow"
    assert classification.retry_same_request is False
    # Each send is its own physical attempt in the ledger.
    assert len(ledger_rows(isolated)) == 2


@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_successful_reask_is_the_calls_answer(isolated, asynchronous):
    outcome, sends = drive([Wire(MASKED), Wire(ANSWER, b"data: [DONE]\n\n")], asynchronous=asynchronous)

    assert len(sends) == 2 and routing(sends[1])["allow_fallbacks"] is False
    assert outcome.model_dump()["choices"][0]["message"]["content"] == "answer"
    rows = ledger_rows(isolated)
    assert len(rows) == 2
    assert rows[1]["state"] == "settled" and rows[1]["cost_usd"] == 0.5 and rows[1]["cost_final"] is True


@pytest.mark.parametrize("metadata", [
    receipt(400, 402),  # an upstream itself answered 402: its own billing verdict
    None,  # no receipt at all
    {"requested": MODEL, "attempt": 0, "summary": "available=16"},  # refused before any upstream
], ids=["402-among-attempts", "no-receipt", "no-attempts"])
@pytest.mark.parametrize("asynchronous", [False, True])
def test_a_genuine_402_stays_quota_without_reask(isolated, asynchronous, metadata):
    outcome, sends = drive([Wire(error_frame(402, CREDITS, metadata))], asynchronous=asynchronous)

    assert len(sends) == 1
    assert isinstance(outcome, ProviderStreamError)
    assert classify_llm_exception(outcome).kind == "quota_exhausted"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_no_reask_when_fallbacks_are_already_forbidden(isolated, asynchronous):
    kwargs = request_kwargs(routing={"require_parameters": True, "allow_fallbacks": False})
    outcome, sends = drive([Wire(MASKED)], asynchronous=asynchronous, kwargs=kwargs)

    assert len(sends) == 1
    assert classify_llm_exception(outcome).kind == "quota_exhausted"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_no_reask_on_a_non_openrouter_route(isolated, asynchronous):
    outcome, sends = drive([Wire(MASKED)], asynchronous=asynchronous, provider="openai-compatible")

    assert len(sends) == 1 and isinstance(outcome, ProviderStreamError)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_no_reask_after_model_output_arrived(isolated, asynchronous):
    partial = {"id": "gen-402", "choices": [{"index": 0, "delta": {"content": "partial"}}]}
    outcome, sends = drive([Wire(partial, MASKED)], asynchronous=asynchronous)

    assert len(sends) == 1
    assert outcome.stream_receipt["model_output_received"] is True


@pytest.mark.parametrize("asynchronous", [False, True])
def test_no_reask_inside_a_single_physical_send_scope(isolated, asynchronous):
    with single_physical_send(lambda reservation: None):
        outcome, sends = drive([Wire(MASKED)], asynchronous=asynchronous)

    assert len(sends) == 1
    assert classify_llm_exception(outcome).kind == "quota_exhausted"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_an_http_status_verdict_reads_the_receipt_from_the_response(isolated, asynchronous):
    masked = {key: MASKED[key] for key in ("error", "openrouter_metadata")}
    upstream = {key: UPSTREAM[key] for key in ("error", "openrouter_metadata")}
    outcome, sends = drive([http_error(402, masked), http_error(400, upstream)],
                           asynchronous=asynchronous, kwargs=request_kwargs(stream=False))

    assert len(sends) == 2 and routing(sends[1])["allow_fallbacks"] is False
    assert isinstance(outcome, openai.BadRequestError)
    assert classify_llm_exception(outcome).kind == "context_overflow"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_an_http_200_body_verdict_is_reasked_and_answered(isolated, asynchronous):
    masked = AssembledResponse({**{key: MASKED[key] for key in ("id", "error", "openrouter_metadata")},
                                "choices": None})
    answer = AssembledResponse({"id": "gen-answer", "usage": {"cost": 0.5, "prompt_tokens": 10, "completion_tokens": 2},
                                "choices": [{"message": {"role": "assistant", "content": "answer"},
                                             "finish_reason": "stop"}]})
    outcome, sends = drive([masked, answer], asynchronous=asynchronous, kwargs=request_kwargs(stream=False))

    assert len(sends) == 2 and routing(sends[1])["allow_fallbacks"] is False
    assert outcome.model_dump()["choices"][0]["message"]["content"] == "answer"


def test_stream_error_retains_the_router_receipt():
    error = ProviderStreamError(MASKED)
    assert error.openrouter_metadata == MASKED["openrouter_metadata"]
    assert error.openrouter_metadata is not MASKED["openrouter_metadata"]
    assert ProviderStreamError({"error": {"code": 402, "message": CREDITS}}).openrouter_metadata is None


def test_openrouter_targets_ask_for_the_routing_receipt():
    client = LLMClient()
    headers = client._resolve_remote_target(MODEL)["default_headers"]
    assert headers["X-OpenRouter-Metadata"] == "enabled"
    assert {key: headers[key] for key in OPENROUTER_APP_HEADERS} == OPENROUTER_APP_HEADERS
    assert "X-OpenRouter-Metadata" not in client._resolve_remote_target(
        "anthropic::claude-opus-5.5")["default_headers"]

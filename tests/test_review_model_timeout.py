from __future__ import annotations

import asyncio


def test_query_model_timeout_becomes_error_actor(monkeypatch):
    from ouroboros.tools.review import _query_model

    class HangingClient:
        async def chat_async(self, **_kwargs):
            await asyncio.sleep(1)
            return {"content": "late"}, {}

    monkeypatch.setenv("OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC", "0.01")

    model, result, headers = asyncio.run(
        _query_model(HangingClient(), "fake/reviewer", [], asyncio.Semaphore(1))
    )

    assert model == "fake/reviewer"
    assert headers is None
    assert result["error"] == "Error: Timeout after 0.01s"
    assert result["prompt_ref"]["manifest_ref"]["path"]
    assert result["response_ref"]["manifest_ref"]["path"]

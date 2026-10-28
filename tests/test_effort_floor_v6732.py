"""v6.73.2 — reasoning-effort FLOORS for endpoints where reasoning is MANDATORY.

Live incident 2026-07-19/20: google/gemini-3.5-flash on OpenRouter answered
``400 Reasoning is mandatory for this endpoint and cannot be disabled.`` to the
safety supervisor's ``reasoning_effort="none"`` → the safety check failed CLOSED
and blocked benign tools. These tests pin the exact-route floor recovery through
the real retry ladder (no model-global learning), the clamp-note discard on a
terminal or accounting failure, the diagnostic floor/ceiling band and the
durable store semantics.
"""

import time

import pytest

LIVE_GEMINI_ERROR = (
    "Error code: 400 - {'error': {'message': 'Reasoning is mandatory for this "
    "endpoint and cannot be disabled.', 'code': 400}}"
)


class _Observed400(RuntimeError):
    status_code = 400
    body = {"error": {
        "message": "Reasoning is mandatory for this endpoint and cannot be disabled.",
        "code": 400,
    }}


def _wire_target():
    return {
        "provider": "openrouter",
        "usage_model": "google/gemini-3.5-flash",
        "resolved_model": "google/gemini-3.5-flash",
        "base_url": "https://openrouter.example/v1",
    }


@pytest.fixture
def _clean_effort_caches():
    from ouroboros.llm import LLMClient

    for store in (
        LLMClient._EFFORT_CEILING_CACHE,
        LLMClient._EFFORT_FLOOR_CACHE,
        LLMClient._EFFORT_FLOOR_LOADED,
    ):
        store.clear()
    LLMClient._EFFORT_CEILING_LOADED.clear()
    yield
    for store in (
        LLMClient._EFFORT_CEILING_CACHE,
        LLMClient._EFFORT_FLOOR_CACHE,
        LLMClient._EFFORT_FLOOR_LOADED,
    ):
        store.clear()
    LLMClient._EFFORT_CEILING_LOADED.clear()


@pytest.fixture
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    return tmp_path


# --- exact-route floor recovery through the real retry ladder -----------------


def test_mandatory_reasoning_400_through_retry_ladder(
    _clean_effort_caches, _isolated_data_dir, monkeypatch
):
    """End-to-end through the REAL OpenRouter retry seam
    (_create_chat_completion_with_retries with a fake create_fn): first send
    400s with the live Gemini text, the floored retry succeeds, and the second
    physical payload carries effort low with the carrier present."""
    import ouroboros.llm_attempt as llm_attempt_mod
    from ouroboros.llm import LLMClient

    monkeypatch.setattr(llm_attempt_mod, "execute_physical_attempt", lambda req, fn: fn())
    client = LLMClient(api_key="test")
    sends = []

    def create_fn(**kwargs):
        sends.append(kwargs)
        if len(sends) == 1:
            raise _Observed400(LIVE_GEMINI_ERROR)
        return {"ok": True}

    kwargs = {
        "model": "google/gemini-3.5-flash",
        "messages": [],
        "extra_body": {"reasoning": {"effort": "none", "exclude": False}},
    }
    target = _wire_target()
    resp = client._create_chat_completion_with_retries(create_fn, kwargs, target)
    assert resp == {"ok": True}
    assert len(sends) == 2
    assert sends[1]["extra_body"]["reasoning"]["effort"] == "low"
    _, usage = client._normalize_remote_response(
        {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
         "usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        {"provider": "openrouter", "resolved_model": "gemini-3.5-flash",
         "usage_model": "google/gemini-3.5-flash"},
        skip_cost_fetch=True,
    )
    assert "reasoning_effort_clamped" not in usage
    assert "google/gemini-3.5-flash" not in LLMClient._EFFORT_FLOOR_CACHE


def test_terminal_retry_death_discards_clamp_note(
    _clean_effort_caches, _isolated_data_dir, monkeypatch
):
    """A failed exact-route floor retry commits no model-global authority."""
    import pytest as _pytest

    import ouroboros.llm_attempt as llm_attempt_mod
    from ouroboros.llm import LLMClient

    monkeypatch.setattr(llm_attempt_mod, "execute_physical_attempt", lambda req, fn: fn())
    client = LLMClient(api_key="test")
    sends = []

    def create_fn(**kwargs):
        sends.append(kwargs)
        raise _Observed400(LIVE_GEMINI_ERROR)

    kwargs = {
        "model": "google/gemini-3.5-flash",
        "messages": [],
        "extra_body": {"reasoning": {"effort": "none", "exclude": False}},
    }
    target = _wire_target()
    with _pytest.raises(RuntimeError):
        client._create_chat_completion_with_retries(create_fn, kwargs, target)
    assert len(sends) == 2  # one floored retry, no loop
    assert client._pop_effort_clamp_disclosure() is None
    assert "google/gemini-3.5-flash" not in LLMClient._EFFORT_FLOOR_CACHE


def test_mandatory_reasoning_body_400_in_http200_recovers(
    _clean_effort_caches, _isolated_data_dir, monkeypatch
):
    """HTTP-200 body 400 uses the same exact-route floor recovery seam."""

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def model_dump(self):
            return self._body

    import ouroboros.llm_attempt as llm_attempt_mod
    from ouroboros.llm import LLMClient

    monkeypatch.setattr(llm_attempt_mod, "execute_physical_attempt", lambda req, fn: fn())
    client = LLMClient(api_key="test")
    sends = []
    body_err = _FakeResp({
        "error": {"code": 400, "message": "Reasoning is mandatory for this endpoint and cannot be disabled."},
        "choices": None,
    })
    good = _FakeResp({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})

    def create_fn(**kwargs):
        sends.append(kwargs)
        return body_err if len(sends) == 1 else good

    kwargs = {
        "model": "google/gemini-3.5-flash",
        "messages": [],
        "extra_body": {"reasoning": {"effort": "none", "exclude": False}},
    }
    target = _wire_target()
    resp = client._create_chat_completion_with_retries(create_fn, kwargs, target)
    assert resp is good
    assert len(sends) == 2
    assert sends[1]["extra_body"]["reasoning"]["effort"] == "low"
    assert "google/gemini-3.5-flash" not in LLMClient._EFFORT_FLOOR_CACHE
    assert client._pop_effort_clamp_disclosure() is None

    # Unmatched body-400: returned unchanged, single send, nothing learned.
    sends.clear()
    other = _FakeResp({"error": {"code": 400, "message": "malformed request body"}, "choices": None})
    resp2 = client._create_chat_completion_with_retries(
        lambda **kw: (sends.append(kw) or other), dict(kwargs), target,
    )
    assert resp2 is other and len(sends) == 1


def test_usage_accounting_abort_discards_clamp_note(
    _clean_effort_caches, _isolated_data_dir, monkeypatch
):
    """Triad r2 regression (sync + async drivers): an admission failure
    (UsageAccountingError) before dispatch leaves no response to consume a
    build-time clamp note — the note must be discarded on the re-raise so it
    cannot misattach to a later non-clamping call on the same thread. The
    Anthropic driver carries the identical structural discard."""
    import asyncio

    import pytest as _pytest

    import ouroboros.llm_attempt as llm_attempt_mod
    from ouroboros.llm import LLMClient
    from ouroboros.llm_capability_policy import _EFFORT_CLAMP_CVAR
    from ouroboros.usage_accounting import UsageAccountingError

    def _abort(req, fn):
        raise UsageAccountingError("budget rail")

    monkeypatch.setattr(llm_attempt_mod, "execute_physical_attempt", _abort)

    async def _abort_async(req, fn):
        raise UsageAccountingError("budget rail")

    monkeypatch.setattr(llm_attempt_mod, "execute_physical_attempt_async", _abort_async)
    client = LLMClient(api_key="test")
    target = {"usage_model": "vendor/uae-test", "resolved_model": "vendor/uae-test"}

    # A build-time tier note, staged the way the DeepSeek/Z.ai payload builders stage it.
    note = {"requested": "none", "applied": "low", "reason": "provider_wire_mapping", "model": "vendor/uae-test"}
    _EFFORT_CLAMP_CVAR.set(dict(note))
    with _pytest.raises(UsageAccountingError):
        client._create_chat_completion_with_retries(lambda **kw: {"ok": True}, {"model": "m"}, target)
    assert client._pop_effort_clamp_disclosure() is None

    # The note lives in a ContextVar (isolated per thread AND per asyncio
    # task, like the reasoning pin), so stage it where chat_async does: inside
    # the coroutine that runs the driver.
    async def _stage_then_abort():
        _EFFORT_CLAMP_CVAR.set(dict(note))
        with _pytest.raises(UsageAccountingError):
            await client._create_chat_completion_with_retries_async(
                lambda **kw: {"ok": True}, {"model": "m"}, target,
            )
        assert client._pop_effort_clamp_disclosure() is None

    asyncio.run(_stage_then_abort())


def test_uae_on_floored_resend_discards_clamp_note(
    _clean_effort_caches, _isolated_data_dir, monkeypatch
):
    """Triad r4 regression: the CENTRAL _send discard — when the FLOORED
    body-400 resend (which just recorded a learned_floor note) is aborted by
    the accounting rail, the note is discarded before the abort propagates."""

    class _FakeResp:
        def __init__(self, body):
            self._body = body

        def model_dump(self):
            return self._body

    import pytest as _pytest

    import ouroboros.llm_attempt as llm_attempt_mod
    from ouroboros.llm import LLMClient
    from ouroboros.usage_accounting import UsageAccountingError

    calls = {"n": 0}

    def _gate(req, fn):
        calls["n"] += 1
        if calls["n"] == 2:
            raise UsageAccountingError("budget rail")
        return fn()

    monkeypatch.setattr(llm_attempt_mod, "execute_physical_attempt", _gate)
    client = LLMClient(api_key="test")
    body_err = _FakeResp({
        "error": {"code": 400, "message": "Reasoning is mandatory for this endpoint and cannot be disabled."},
        "choices": None,
    })
    kwargs = {
        "model": "google/gemini-3.5-flash",
        "messages": [],
        "extra_body": {"reasoning": {"effort": "none", "exclude": False}},
    }
    target = _wire_target()
    with _pytest.raises(UsageAccountingError):
        client._create_chat_completion_with_retries(lambda **kw: body_err, kwargs, target)
    assert calls["n"] == 2  # initial send + aborted floored resend
    assert client._pop_effort_clamp_disclosure() is None


# --- diagnostic clamp: the [floor, ceiling] band -----------------------------


def test_floor_clamps_up(_clean_effort_caches, _isolated_data_dir):
    from ouroboros.llm import LLMClient

    LLMClient._EFFORT_FLOOR_CACHE["vendor/mandatory-model"] = "low"
    LLMClient._EFFORT_FLOOR_LOADED["vendor/mandatory-model"] = time.monotonic()
    assert LLMClient.clamp_effort_for_route("vendor/mandatory-model", "none") == "low"
    # In-band values are untouched.
    assert LLMClient.clamp_effort_for_route("vendor/mandatory-model", "medium") == "medium"


def test_floor_ceiling_interaction(_clean_effort_caches, _isolated_data_dir):
    from ouroboros.llm import LLMClient

    LLMClient._EFFORT_FLOOR_CACHE["vendor/banded"] = "low"
    LLMClient._EFFORT_FLOOR_LOADED["vendor/banded"] = time.monotonic()
    LLMClient._EFFORT_CEILING_CACHE["vendor/banded"] = "high"
    assert LLMClient.clamp_effort_for_route("vendor/banded", "none") == "low"
    assert LLMClient.clamp_effort_for_route("vendor/banded", "max") == "high"
    assert LLMClient.clamp_effort_for_route("vendor/banded", "medium") == "medium"


def test_floor_cache_hourly_resync_evicts_expired(
    _clean_effort_caches, _isolated_data_dir, monkeypatch
):
    """Long-running processes heal like restarts: the hourly re-sync REPLACES
    the in-process floor from the durable store, so a 14-day-expired durable
    floor evicts without a restart."""
    from ouroboros import capability_evidence as ce
    from ouroboros.llm import LLMClient

    ce.record_effort_floor(_isolated_data_dir, "vendor/heals", "low")
    assert LLMClient._effort_floor_for("vendor/heals") == "low"
    # Age the durable record past the TTL, then age the process cache past the
    # reload interval.
    with ce._STORE_LOCK:
        data = ce._load(_isolated_data_dir)
        data["effort_floors"]["vendor/heals"]["observed_at"] = "2020-01-01T00:00:00+00:00"
        ce._save(_isolated_data_dir, data)
    LLMClient._EFFORT_FLOOR_LOADED["vendor/heals"] = (
        time.monotonic() - LLMClient._EFFORT_FLOOR_RELOAD_SEC - 1
    )
    assert LLMClient._effort_floor_for("vendor/heals") == ""


# --- capability_evidence durable layer (first direct effort-namespace tests) --


def test_effort_floor_roundtrip_and_higher_wins(tmp_path):
    from ouroboros import capability_evidence as ce

    ce.record_effort_floor(tmp_path, "vendor/m", "low")
    assert ce.get_effort_floor(tmp_path, "vendor/m") == "low"
    ce.record_effort_floor(tmp_path, "vendor/m", "medium")
    assert ce.get_effort_floor(tmp_path, "vendor/m") == "medium"
    # A LOWER floor never silently wins within the window.
    ce.record_effort_floor(tmp_path, "vendor/m", "low")
    assert ce.get_effort_floor(tmp_path, "vendor/m") == "medium"


def test_effort_floor_expiry_and_fail_open(tmp_path):
    from ouroboros import capability_evidence as ce

    ce.record_effort_floor(tmp_path, "vendor/exp", "low")
    with ce._STORE_LOCK:
        data = ce._load(tmp_path)
        data["effort_floors"]["vendor/exp"]["observed_at"] = "2020-01-01T00:00:00+00:00"
        ce._save(tmp_path, data)
    assert ce.get_effort_floor(tmp_path, "vendor/exp") == ""
    # Fail-open on absence and on junk input.
    assert ce.get_effort_floor(tmp_path, "vendor/never-seen") == ""
    assert ce.get_effort_floor(tmp_path, "") == ""
    ce.record_effort_floor(tmp_path, "", "low")  # no-op, never raises


def test_effort_ceiling_roundtrip_parity(tmp_path):
    """First direct unit coverage of the (pre-existing) ceiling store."""
    from ouroboros import capability_evidence as ce

    ce.record_effort_ceiling(tmp_path, "vendor/c", "high")
    assert ce.get_effort_ceiling(tmp_path, "vendor/c") == "high"
    assert ce.get_effort_ceiling(tmp_path, "vendor/none") == ""


def test_load_seeds_all_namespaces(tmp_path):
    from ouroboros import capability_evidence as ce

    data = ce._load(tmp_path)
    for ns in ("probes", "owner_acks", "effort_ceilings", "effort_floors", "rejected_params"):
        assert ns in data

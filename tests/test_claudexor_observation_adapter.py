"""One observation adapter: the compact wait on a repaired engine, the detail read elsewhere."""

import pytest

from ouroboros.gateways import claudexor_observation as obs
from ouroboros.gateways.claudexor import ClaudexorUnavailable

ENGINE_TOKEN = "A" * 43
OTHER_TOKEN = "B" * 43
WAIT_MANY_ROW = {"method": "POST", "path": "/v2/runs/wait-many"}


class FakeGateway:
    """The two surfaces the adapter may touch, scripted per test."""

    def __init__(self, *, version="4.8.1", sha="sha-1", routes=(WAIT_MANY_ROW,), waits=(), details=None):
        self.engine_version = version
        self.engine_build_sha = sha
        self.routes = list(routes)
        self.waits = list(waits)
        self.details = dict(details or {})
        self.catalog_reads = 0
        self.wait_requests = []
        self.detail_reads = []

    def operations(self, *, timeout_sec=None):
        self.catalog_reads += 1
        return list(self.routes)

    def wait_run_attention(self, targets, *, wait_ms, timeout_sec):
        self.wait_requests.append({"targets": targets, "wait_ms": wait_ms, "timeout_sec": timeout_sec})
        answer = self.waits.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get_run(self, run_id, *, timeout_sec=None):
        self.detail_reads.append((run_id, timeout_sec))
        answer = self.details[run_id]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture(autouse=True)
def _fresh_support_cache(monkeypatch):
    monkeypatch.setattr(obs, "_SUPPORT", {})


def _summary(status="running", pending=False, token=ENGINE_TOKEN):
    return {"status": status, "pending": pending, "token": token, "problem": None}


def _problem(kind):
    return {"status": None, "pending": None, "token": None, "problem": kind}


# -- support ------------------------------------------------------------------------------

def test_support_needs_the_repaired_floor_and_the_catalog():
    below = FakeGateway(version="4.8.0")
    assert obs.wait_many_supported(below, timeout_sec=1) is False
    assert below.catalog_reads == 0, "an engine below the repaired floor is not asked for its catalog"
    unlisted = FakeGateway(version="4.8.1", routes=())
    assert obs.wait_many_supported(unlisted, timeout_sec=1) is False
    listed = FakeGateway(version="4.9.0")
    assert obs.wait_many_supported(listed, timeout_sec=1) is True
    assert obs.wait_many_supported(listed, timeout_sec=1) is True
    assert listed.catalog_reads == 1, "one catalog read per engine identity"


def test_a_double_without_the_seam_or_a_handshake_is_unsupported_without_a_request():
    class Bare:
        engine_version = "9.9.9"

        def get_run(self, run_id, **_kw):
            return {}

    assert obs.wait_many_supported(Bare(), timeout_sec=1) is False
    assert obs.wait_many_supported(FakeGateway(version=""), timeout_sec=1) is False


# -- the engine path -----------------------------------------------------------------------

def test_engine_rows_and_attention_are_projected_and_only_engine_tokens_are_echoed():
    gateway = FakeGateway(waits=[{
        "wakeReason": "attention", "attention": [1],
        "summaries": [_summary(), _summary(pending=True, token=OTHER_TOKEN), _problem("not_found")],
    }])
    wake = obs.wait_attention(gateway, [("run-a", "local:abc"), ("run-b", ENGINE_TOKEN), ("run-c", None)],
                              wait_sec=99.0, timeout_sec=5.0)
    request = gateway.wait_requests[0]
    assert request["targets"] == [{"runId": "run-a"}, {"runId": "run-b", "seen": ENGINE_TOKEN}, {"runId": "run-c"}]
    assert request["wait_ms"] == 50_000, "the window is clamped to the engine's maximum"
    assert request["timeout_sec"] == 5.0
    assert wake.reason == "attention" and wake.waited is True and wake.source == "wait_many"
    assert wake.attention == [1] and wake.needs_attention(1) and not wake.needs_attention(0)
    assert [row.run_id for row in wake.rows] == ["run-a", "run-b", "run-c"]
    assert wake.rows[1].pending is True and wake.rows[1].token == OTHER_TOKEN and wake.rows[1].observed
    assert wake.rows[2].problem == "not_found" and not wake.rows[2].observed and wake.rows[2].token is None


@pytest.mark.parametrize("body", [
    {"wakeReason": "attention", "attention": [], "summaries": [_summary()]},  # batch of the wrong size
    {"wakeReason": "later", "attention": [], "summaries": [_summary(), _summary()]},
    {"wakeReason": "attention", "attention": [5], "summaries": [_summary(), _summary()]},
    {"wakeReason": "timeout", "attention": [], "summaries": [_summary(token="short"), _summary()]},
    {"wakeReason": "problems", "attention": [], "summaries": [_problem("vanished"), _summary()]},
])
def test_a_malformed_engine_answer_is_a_typed_refusal_never_a_fabricated_row(body):
    gateway = FakeGateway(waits=[body])
    with pytest.raises(ClaudexorUnavailable) as caught:
        obs.wait_attention(gateway, [("run-a", None), ("run-b", None)], wait_sec=1.0, timeout_sec=1.0)
    assert caught.value.code == "malformed_response"


@pytest.mark.parametrize("failure", [
    ClaudexorUnavailable("daemon_unreachable", "read bound expired", observation_timeout=True,
                         observation_reason="observation_read_timeout"),
    ClaudexorUnavailable("daemon_stopping", "no new product request was admitted", status_code=503),
    ClaudexorUnavailable("daemon_busy", "local RPC timed out", status_code=503),
])
def test_a_hole_is_an_unobserved_wake_not_a_raise(failure):
    gateway = FakeGateway(waits=[failure])
    wake = obs.wait_attention(gateway, [("run-a", None)], wait_sec=1.0, timeout_sec=1.0)
    assert wake.reason == "unobserved" and wake.hole is failure and wake.waited is False
    assert [row.observed for row in wake.rows] == [False]


def test_a_received_refusal_of_the_read_itself_propagates():
    gateway = FakeGateway(waits=[ClaudexorUnavailable("invalid_request", "targets required", status_code=400)])
    with pytest.raises(ClaudexorUnavailable) as caught:
        obs.wait_attention(gateway, [("run-a", None)], wait_sec=1.0, timeout_sec=1.0)
    assert caught.value.status_code == 400


def test_a_404_on_the_route_falls_back_to_detail_in_the_same_call_and_names_no_run_absent():
    detail = {"summary": {"state": "running"}, "pendingInteractions": [], "lastSeq": 4}
    gateway = FakeGateway(waits=[ClaudexorUnavailable("not_found", "no such route", status_code=404)],
                          details={"run-a": detail})
    wake = obs.wait_attention(gateway, [("run-a", None)], wait_sec=1.0, timeout_sec=2.0)
    assert wake.source == "detail" and wake.rows[0].observed and wake.rows[0].detail is detail
    assert obs.wait_many_supported(gateway, timeout_sec=1) is False, "the route's absence is remembered"
    assert gateway.wait_requests and gateway.detail_reads == [("run-a", 2.0)]


# -- the fallback path ---------------------------------------------------------------------

def _pending(interaction_id):
    return {"interactionId": interaction_id, "questions": [
        {"id": "q1", "question": "Which?", "options": [], "multi_select": False}]}


def test_detail_fallback_derives_rows_and_attention_and_keeps_todays_beat():
    live = {"summary": {"state": "running"}, "pendingInteractions": [], "lastSeq": 1}
    asked = {"summary": {"state": "running", "waitingOnUser": True}, "pendingInteractions": [_pending("i-1")]}
    done = {"summary": {"state": "succeeded"}, "pendingInteractions": [], "lastSeq": 9}
    gateway = FakeGateway(version="4.7.0", details={"run-a": live, "run-b": asked, "run-c": done})
    wake = obs.wait_attention(gateway, [("run-a", None), ("run-b", None), ("run-c", None)],
                              wait_sec=3.0, timeout_sec=5.0)
    assert wake.source == "detail" and wake.waited is False and wake.reason == "attention"
    assert wake.attention == [1, 2]
    assert [row.detail for row in wake.rows] == [live, asked, done]
    assert wake.rows[0].status == "running" and wake.rows[0].pending is False
    assert wake.rows[1].pending is True and wake.rows[2].terminal
    assert all(row.token.startswith("local:") for row in wake.rows), "fallback tokens never look like the engine's"
    assert gateway.wait_requests == [] and len(gateway.detail_reads) == 3


def test_fallback_tokens_follow_the_compact_facts_so_seen_suppresses_a_known_question():
    asked = {"summary": {"state": "running", "waitingOnUser": True}, "pendingInteractions": [_pending("i-1")]}
    gateway = FakeGateway(version="4.7.0", details={"run-b": asked})
    first = obs.wait_attention(gateway, [("run-b", None)], wait_sec=0, timeout_sec=1.0)
    assert first.attention == [0]
    token = first.rows[0].token
    again = obs.wait_attention(gateway, [("run-b", token)], wait_sec=0, timeout_sec=1.0)
    assert again.attention == [] and again.rows[0].token == token
    gateway.details["run-b"] = {**asked, "pendingInteractions": [_pending("i-2")]}
    replaced = obs.wait_attention(gateway, [("run-b", token)], wait_sec=0, timeout_sec=1.0)
    assert replaced.attention == [0] and replaced.rows[0].token != token


def test_fallback_keeps_a_runs_own_refusal_on_its_row_and_a_hole_for_the_whole_batch():
    gone = ClaudexorUnavailable("run_not_found", "no such run", status_code=404)
    gateway = FakeGateway(version="4.7.0", details={
        "run-a": {"summary": {"state": "running"}, "pendingInteractions": []}, "run-b": gone})
    wake = obs.wait_attention(gateway, [("run-a", None), ("run-b", None)], wait_sec=0, timeout_sec=1.0)
    assert wake.reason == "problems" and wake.rows[1].problem == "not_found" and wake.rows[1].error is gone
    assert wake.rows[0].observed
    hole = ClaudexorUnavailable("daemon_unreachable", "socket refused", observation_timeout=True,
                                observation_reason="daemon_unreachable")
    gateway.details["run-a"] = hole
    unobserved = obs.wait_attention(gateway, [("run-a", None), ("run-b", None)], wait_sec=0, timeout_sec=1.0)
    assert unobserved.reason == "unobserved" and unobserved.hole is hole and unobserved.source == "detail"

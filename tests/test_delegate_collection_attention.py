"""The body observer watches held runs through the compact wait and reads detail on a reason."""
from __future__ import annotations

import threading

import pytest

from ouroboros import delegate_collection as body
from ouroboros import delegate_custody_current as current
from ouroboros.gateways import claudexor_observation as obs
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from tests.test_delegate_body_collection import started, terminal

TOKEN_A = "a" * 43
TOKEN_B = "b" * 43


def _summary(status="running", pending=False, token=TOKEN_A):
    return {"status": status, "pending": pending, "token": token, "problem": None}


def _wake(reason, summaries, attention=()):
    return {"wakeReason": reason, "attention": list(attention), "summaries": summaries}


def _question(interaction_id="int-1"):
    return {"interactionId": interaction_id, "runId": "run-one", "questions": [
        {"id": "q1", "question": "Which branch?", "options": [], "multi_select": False}]}


class Engine:
    """A 4.8.1 engine: the compact wait scripted per pass, details on demand."""
    engine_version = "4.8.1"
    engine_build_sha = "sha-attention"
    store_identity = ""
    routes = [{"method": "POST", "path": "/v2/runs/wait-many"}]
    wakes: list = []
    details: dict = {}
    requests: list = []
    detail_reads: list = []

    def operations(self, *, timeout_sec=None):
        return list(self.routes)

    def wait_run_attention(self, targets, *, wait_ms, timeout_sec):
        Engine.requests.append({"targets": targets, "wait_ms": wait_ms})
        answer = Engine.wakes.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get_run(self, rid, **kwargs):
        Engine.detail_reads.append(rid)
        answer = Engine.details[rid]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def close(self):
        pass


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    """The real runtime with its driver parked; ``step`` runs one tick and joins its passes."""
    monkeypatch.setattr(obs, "_SUPPORT", {})
    Engine.wakes, Engine.details, Engine.requests, Engine.detail_reads = [], {}, [], []
    release, entered = threading.Event(), threading.Event()
    tick = body.BodyRuntime.tick

    def park(_runtime):
        entered.set()
        assert release.wait(10)

    monkeypatch.setattr(body.BodyRuntime, "tick", park)
    instance = body.start_body_runtime(tmp_path, gateway_factory=Engine, max_workers=2)
    assert entered.wait(3)
    # Support is learned from the engine's catalog by the first read of any run
    # (``test_support_is_learned_by_the_first_read``); these tests start past that point.
    instance._compact_wait = True

    def step():
        tick(instance)
        with instance.lock:
            jobs = list(instance.reads.values()) + list(instance.collections.values())
        for job in jobs:
            job.result(timeout=5)
        with instance.lock:
            jobs = list(instance.reads.values())
        for job in jobs:  # a fallback pass submits per-run reads of its own
            job.result(timeout=5)

    try:
        yield instance, step
    finally:
        instance.close(wait=False)
        release.set()
        instance.close(wait=True)


def test_a_quiet_run_is_watched_without_a_detail_read(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.wakes.append(_wake("timeout", [_summary()]))
    step()
    assert Engine.requests[0]["targets"] == [{"runId": entry.run_id}]
    assert Engine.requests[0]["wait_ms"] is None, "the hold is the engine's default window"
    assert Engine.detail_reads == []
    fact = instance.observation(entry.run_id)
    assert fact["answered"] is True and fact["run_state"] == "running" and fact["waiting_on_user"] is False


def test_attention_fetches_the_question_before_the_token_is_acknowledged(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    asked = {"summary": {"runId": entry.run_id, "state": "running", "waitingOnUser": True},
             "lastSeq": 2, "pendingInteractions": [_question()]}
    Engine.details[entry.run_id] = asked
    Engine.wakes.append(_wake("attention", [_summary(pending=True, token=TOKEN_B)], attention=[0]))
    step()
    assert Engine.detail_reads == [entry.run_id]
    assert current.run(tmp_path, entry.run_id).pending_interaction_ids == ["int-1"]
    assert instance.seen[entry.run_id] == TOKEN_B
    Engine.wakes.append(_wake("timeout", [_summary(pending=True, token=TOKEN_B)]))
    step()
    assert Engine.requests[1]["targets"] == [{"runId": entry.run_id, "seen": TOKEN_B}]
    assert Engine.detail_reads == [entry.run_id], "a known question is not fetched again"


def test_a_failed_fetch_leaves_the_token_unacknowledged_so_the_question_wakes_again(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.details[entry.run_id] = ClaudexorUnavailable("daemon_busy", "local RPC timed out", status_code=503)
    Engine.wakes.append(_wake("attention", [_summary(pending=True, token=TOKEN_B)], attention=[0]))
    step()
    assert Engine.detail_reads == [entry.run_id]
    assert entry.run_id not in instance.seen
    assert instance.observation(entry.run_id)["error_code"] == "daemon_busy"


def test_a_terminal_wake_retains_the_source_through_one_detail_read(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.details[entry.run_id] = terminal(entry.run_id)
    Engine.wakes.append(_wake("attention", [_summary(status="succeeded", token=TOKEN_B)], attention=[0]))
    step()
    fresh = current.run(tmp_path, entry.run_id)
    assert fresh.terminal_state == "succeeded" and fresh.terminal_source
    assert Engine.detail_reads == [entry.run_id]


def test_a_hole_is_remembered_as_unanswered_and_never_raises_out_of_the_pass(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.wakes.append(ClaudexorUnavailable("daemon_unreachable", "read bound expired",
                                             observation_timeout=True, observation_reason="observation_read_timeout"))
    step()
    fact = instance.observation(entry.run_id)
    assert fact["answered"] is False and fact["error_code"] == "daemon_unreachable"
    assert Engine.detail_reads == []


@pytest.mark.parametrize("kind", ["not_found", "snapshot_unavailable"])
def test_a_problem_row_takes_the_addressed_read_that_may_say_absent_then_acknowledges_it(tmp_path, runtime, kind):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.details[entry.run_id] = {"summary": {"runId": entry.run_id, "state": "running"}, "lastSeq": 1,
                                    "pendingInteractions": []}
    Engine.wakes.append(_wake("problems", [{"status": None, "pending": None, "token": None, "problem": kind}]))
    step()
    assert Engine.detail_reads == [entry.run_id], "a problem row is not absence; the addressed read decides"
    assert instance.observation(entry.run_id)["run_state"] == "running"
    assert instance.seen[entry.run_id] == obs.ACKNOWLEDGED_PROBLEM
    Engine.wakes.append(_wake("timeout", [{"status": None, "pending": None, "token": None, "problem": kind}]))
    step()
    assert Engine.requests[1]["targets"] == [{"runId": entry.run_id, "seen": None}], "acknowledged: the engine holds again"
    assert Engine.detail_reads == [entry.run_id, entry.run_id], "the addressed read is taken again, not spun"


def test_a_problem_row_whose_addressed_read_fails_stays_unacknowledged(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.details[entry.run_id] = ClaudexorUnavailable("daemon_busy", "local RPC timed out", status_code=503)
    Engine.wakes.append(_wake("problems", [{"status": None, "pending": None, "token": None, "problem": "not_found"}]))
    step()
    assert Engine.detail_reads == [entry.run_id] and entry.run_id not in instance.seen


def test_an_engine_below_the_floor_keeps_todays_beat_of_one_detail_per_run(tmp_path, runtime, monkeypatch):
    instance, step = runtime
    instance._compact_wait = False  # never learned on this engine
    monkeypatch.setattr(Engine, "engine_version", "4.8.0")
    entry = started(tmp_path)
    other = started(tmp_path, rid="run-two", tid="task-two")
    for rid in (entry.run_id, other.run_id):
        Engine.details[rid] = {"summary": {"runId": rid, "state": "running"}, "lastSeq": 1, "pendingInteractions": []}
    step()
    assert Engine.requests == [] and sorted(Engine.detail_reads) == [entry.run_id, other.run_id]
    step()
    assert len(Engine.detail_reads) == 4


def test_support_is_learned_by_the_first_read_then_the_pass_takes_over(tmp_path, runtime):
    instance, step = runtime
    instance._compact_wait = False
    entry = started(tmp_path)
    Engine.details[entry.run_id] = {"summary": {"runId": entry.run_id, "state": "running"}, "lastSeq": 1,
                                    "pendingInteractions": []}
    step()
    assert Engine.detail_reads == [entry.run_id] and Engine.requests == []
    assert instance._compact_wait is True
    Engine.wakes.append(_wake("timeout", [_summary()]))
    step()
    assert Engine.detail_reads == [entry.run_id] and len(Engine.requests) == 1


def test_an_engine_that_lost_the_route_returns_the_observer_to_its_old_beat(tmp_path, runtime):
    instance, step = runtime
    entry = started(tmp_path)
    Engine.details[entry.run_id] = {"summary": {"runId": entry.run_id, "state": "running"}, "lastSeq": 1,
                                    "pendingInteractions": []}
    Engine.wakes.append(ClaudexorUnavailable("not_found", "no such route", status_code=404))
    step()
    assert Engine.detail_reads == [entry.run_id], "the pass that met the 404 read the detail itself"
    assert instance._compact_wait is False
    step()
    assert Engine.detail_reads == [entry.run_id, entry.run_id] and len(Engine.requests) == 1

"""The supervised wait's beat on a repaired engine: speech from the journal, attention from the engine."""
from __future__ import annotations

import contextlib
import json

import pytest

from ouroboros import delegate_activity, delegate_observe
from ouroboros.gateways import claudexor_observation as obs
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from tests._delegated_transport_shared import _delegating_ctx

TOKEN_A = "a" * 43
TOKEN_B = "b" * 43
ROUTES = [{"method": "POST", "path": "/v2/runs/wait-many"}, {"method": "GET", "path": "/v2/runs/:id/events"}]


def _summary(status="running", pending=False, token=TOKEN_A):
    return {"status": status, "pending": pending, "token": token, "problem": None}


def _wake(reason, summaries, attention=()):
    return {"wakeReason": reason, "attention": list(attention), "summaries": summaries}


def _frames(events, *, end=False):
    text = ""
    for seq, value in events:
        text += f"id: {seq}\nevent: {value['type']}\ndata: {json.dumps(value)}\n\n"
    if end:
        text += "event: end\ndata: {}\n\n"
    return text


class _Stream:
    def __init__(self, text):
        self.status_code, self._text = 200, text

    def iter_text(self):
        yield self._text


class Engine:
    engine_version = "4.8.1"
    engine_build_sha = "sha-observe"
    routes = ROUTES

    def __init__(self, *, streams=(), wakes=(), details=()):
        self.streams, self.wakes, self.details = list(streams), list(wakes), list(details)
        self.stream_opens, self.requests, self.detail_reads = [], [], []

    def operations(self, *, timeout_sec=None):
        return list(self.routes)

    def open_run_events(self, run_id, after_seq, timeout_sec):
        self.stream_opens.append((run_id, after_seq, timeout_sec))
        return contextlib.nullcontext(_Stream(self.streams.pop(0) if self.streams else ""))

    def wait_run_attention(self, targets, *, wait_ms, timeout_sec):
        self.requests.append({"targets": targets, "wait_ms": wait_ms})
        answer = self.wakes.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get_run(self, run_id, *, timeout_sec=None):
        self.detail_reads.append(run_id)
        return self.details.pop(0)


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(obs, "_SUPPORT", {})
    delegate_activity.reset_process_memo()
    context = _delegating_ctx(tmp_path, acting=False)
    frames = []
    context.emit_progress_fn = lambda text, **meta: frames.append((text, meta))
    context.frames = frames
    return context


def _speech(seq, text):
    return (seq, {"type": "harness.event", "seq": seq, "run_id": "run-s",
                  "payload": {"type": "message", "text": text, "harness_id": "codex", "attempt_id": "a1"}})


def test_a_quiet_beat_reads_no_detail_and_speech_reaches_the_owner(ctx):
    gateway = Engine(streams=[_frames([_speech(1, "hello"), _speech(2, "world")])],
                     wakes=[_wake("timeout", [_summary()])])
    step = delegate_observe.step(ctx, gateway, "run-s", window=3.0, since_seq=0, read_window=lambda: 30.0)
    assert step.detail is None and gateway.detail_reads == []
    payload = json.loads(step.payload)
    assert payload["status"] == "progress" and payload["last_seq"] == 2 and payload["state"] == "running"
    assert payload["answered"] is True and payload["waiting_on_user"] is False
    assert gateway.stream_opens == [("run-s", 0, 3.0)]
    assert gateway.requests == [{"targets": [{"runId": "run-s"}], "wait_ms": 0}], "attention is a present-state read"
    assert len(ctx.frames) == 1 and "hello" in ctx.frames[0][0]
    assert ctx.frames[0][1]["delegated_activity"]["through_seq"] == 2
    again = delegate_observe.step(ctx, Engine(wakes=[_wake("timeout", [_summary()])]), "run-s",
                                  window=3.0, since_seq=2, read_window=lambda: 30.0)
    assert json.loads(again.payload)["status"] == "no_progress", "a silent beat is quiet"


def test_attention_earns_the_detail_and_acknowledges_the_token_after_it(ctx):
    asked = {"summary": {"state": "running", "waitingOnUser": True}, "lastSeq": 3, "pendingInteractions": [
        {"interactionId": "i-1", "questions": [{"id": "q", "question": "?", "options": [], "multi_select": False}]}]}
    gateway = Engine(streams=[""], wakes=[_wake("attention", [_summary(pending=True, token=TOKEN_B)], [0]),
                                          _wake("timeout", [_summary(pending=True, token=TOKEN_B)])],
                     details=[asked])
    step = delegate_observe.step(ctx, gateway, "run-s", window=3.0, since_seq=0, read_window=lambda: 30.0)
    assert step.detail is asked and gateway.detail_reads == ["run-s"]
    gateway.streams.append("")
    delegate_observe.step(ctx, gateway, "run-s", window=3.0, since_seq=3, read_window=lambda: 30.0)
    assert gateway.requests[1]["targets"] == [{"runId": "run-s", "seen": TOKEN_B}]


def test_the_streams_end_earns_the_terminal_detail(ctx):
    done = {"summary": {"state": "succeeded"}, "lastSeq": 2, "pendingInteractions": []}
    gateway = Engine(streams=[_frames([_speech(1, "bye")], end=True)],
                     wakes=[_wake("attention", [_summary(status="succeeded", token=TOKEN_B)], [0])], details=[done])
    step = delegate_observe.step(ctx, gateway, "run-s", window=3.0, since_seq=0, read_window=lambda: 30.0)
    assert step.detail is done and len(ctx.frames) == 1


def test_a_hole_on_the_attention_read_raises_the_gateways_typed_hole(ctx):
    hole = ClaudexorUnavailable("daemon_unreachable", "read bound expired", observation_timeout=True,
                                observation_reason="observation_read_timeout")
    gateway = Engine(streams=[""], wakes=[hole])
    with pytest.raises(ClaudexorUnavailable) as caught:
        delegate_observe.step(ctx, gateway, "run-s", window=1.0, since_seq=0, read_window=lambda: 30.0)
    assert caught.value is hole


def test_a_problem_row_hands_the_beat_back_to_the_detail_read(ctx):
    gateway = Engine(streams=[""], wakes=[_wake("problems", [
        {"status": None, "pending": None, "token": None, "problem": "not_found"}])])
    assert delegate_observe.step(ctx, gateway, "run-s", window=1.0, since_seq=0, read_window=lambda: 30.0) is None


@pytest.mark.parametrize("routes", [[ROUTES[0]], [ROUTES[1]], []])
def test_an_engine_missing_either_surface_keeps_the_detail_beat(ctx, routes):
    gateway = Engine()
    gateway.routes = routes
    assert delegate_observe.step(ctx, gateway, "run-s", window=1.0, since_seq=0, read_window=lambda: 30.0) is None
    assert gateway.stream_opens == [] and gateway.requests == []

"""The review session poller holds on the engine's compact wait and reads detail on a reason."""
from __future__ import annotations

import pytest

from ouroboros import review_execution as rx
from ouroboros.gateways import claudexor_observation as obs
from ouroboros.gateways.claudexor import ClaudexorUnavailable

TOKEN_A = "a" * 43
TOKEN_B = "b" * 43


def _summary(status="running", pending=False, token=TOKEN_A):
    return {"status": status, "pending": pending, "token": token, "problem": None}


def _wake(reason, summaries, attention=()):
    return {"wakeReason": reason, "attention": list(attention), "summaries": summaries}


class Engine:
    engine_version = "4.8.1"
    engine_build_sha = "sha-review"
    routes = [{"method": "POST", "path": "/v2/runs/wait-many"}]

    def __init__(self, wakes, details):
        self.wakes, self.details = list(wakes), list(details)
        self.requests, self.detail_reads = [], 0

    def operations(self, *, timeout_sec=None):
        return list(self.routes)

    def wait_run_attention(self, targets, *, wait_ms, timeout_sec):
        self.requests.append({"targets": targets, "wait_ms": wait_ms})
        answer = self.wakes.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def get_run(self, run_id, *, timeout_sec=None):
        self.detail_reads += 1
        answer = self.details.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Custody:
    @staticmethod
    def is_terminal(detail):
        return (detail.get("summary") or {}).get("state") in {"succeeded", "failed", "cancelled", "interrupted"}

    @staticmethod
    def summary_of(detail):
        return detail.get("summary") or {}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(obs, "_SUPPORT", {})
    monkeypatch.setattr(rx, "_SESSION_POLL_SEC", 0.01)


RUNNING = {"summary": {"state": "running"}, "lastSeq": 1, "pendingInteractions": []}
DONE = {"summary": {"state": "succeeded"}, "lastSeq": 5, "pendingInteractions": [], "primaryOutput": {"text": "ok"}}


def test_the_slot_holds_on_the_engine_and_reads_detail_only_when_the_run_ends(tmp_path, monkeypatch):
    gateway = Engine(
        wakes=[_wake("timeout", [_summary()]), _wake("attention", [_summary(status="succeeded", token=TOKEN_B)], [0])],
        details=[RUNNING, DONE])
    slept = []
    monkeypatch.setattr(rx.time, "sleep", lambda s: slept.append(s))
    detail = rx._poll_session_terminal(gateway, _Custody(), tmp_path, object(), "run-r", 30.0)
    assert detail is DONE
    assert gateway.detail_reads == 2, "the first read of the slot and the one the terminal wake earned"
    assert [r["targets"] for r in gateway.requests] == [[{"runId": "run-r"}], [{"runId": "run-r"}]]
    assert slept == [], "the engine held the call; the slot slept nothing of its own"


def test_the_token_is_echoed_as_seen_only_after_its_detail_was_read(tmp_path, monkeypatch):
    unread = ClaudexorUnavailable("daemon_busy", "local RPC timed out", status_code=503)
    gateway = Engine(
        wakes=[_wake("attention", [_summary(pending=False, token=TOKEN_B)], [0]),
               _wake("attention", [_summary(status="succeeded", token=TOKEN_B)], [0])],
        details=[RUNNING, unread, DONE])
    monkeypatch.setattr(rx.time, "sleep", lambda s: None)
    detail = rx._poll_session_terminal(gateway, _Custody(), tmp_path, object(), "run-r", 30.0)
    assert detail is DONE
    # The first wake's detail read was refused: its token was NOT acknowledged, so the next
    # request still carries no ``seen`` and the engine wakes the slot again for the same facts.
    assert gateway.requests[1]["targets"] == [{"runId": "run-r"}]


def test_a_hole_on_the_wait_leaves_the_detail_read_to_decide(tmp_path, monkeypatch):
    hole = ClaudexorUnavailable("daemon_unreachable", "read bound expired", observation_timeout=True,
                                observation_reason="observation_read_timeout")
    gateway = Engine(wakes=[hole], details=[RUNNING, DONE])
    slept = []
    monkeypatch.setattr(rx.time, "sleep", lambda s: slept.append(s))
    detail = rx._poll_session_terminal(gateway, _Custody(), tmp_path, object(), "run-r", 30.0)
    assert detail is DONE and gateway.detail_reads == 2
    assert slept, "an unheld beat sleeps the session's own interval before reading, as before"


def test_an_engine_without_the_wait_keeps_the_old_sleep_and_read_beat(tmp_path, monkeypatch):
    class Old(Engine):
        engine_version = "4.8.0"

    gateway = Old(wakes=[], details=[RUNNING, RUNNING, DONE])
    slept = []
    monkeypatch.setattr(rx.time, "sleep", lambda s: slept.append(s))
    detail = rx._poll_session_terminal(gateway, _Custody(), tmp_path, object(), "run-r", 30.0)
    assert detail is DONE and gateway.requests == [] and gateway.detail_reads == 3 and len(slept) == 2

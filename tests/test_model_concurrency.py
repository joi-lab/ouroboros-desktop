"""#4 per-model concurrency cap (model_concurrency.model_call_slot).

Covers: the cap actually serializes concurrent calls, the slot is released even when
the wrapped body raises, the disabled mode is a pass-through, and acquisition is
deadline-bounded fail-soft (a slot that can't be acquired before the deadline proceeds
WITHOUT throttling rather than blocking the task).
"""

from __future__ import annotations

import threading
import time

import pytest


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    from ouroboros import model_concurrency

    model_concurrency.reset_for_tests()
    yield
    model_concurrency.reset_for_tests()


def test_cap_serializes_concurrent_calls(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "2")
    model_concurrency.reset_for_tests()
    live = []
    peak = [0]
    lock = threading.Lock()

    def worker():
        with model_concurrency.model_call_slot("z-ai/glm-5.2", False, deadline_ts=time.time() + 30):
            with lock:
                live.append(1)
                peak[0] = max(peak[0], len(live))
            time.sleep(0.15)
            with lock:
                live.pop()

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] <= 2, f"cap=2 violated, peak={peak[0]}"


def test_slot_released_on_exception(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    model_concurrency.reset_for_tests()
    with pytest.raises(ValueError):
        with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 30):
            raise ValueError("boom")
    # If the slot leaked, this second acquire (cap=1) would block past the test timeout.
    acquired = []
    with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 5):
        acquired.append(True)
    assert acquired == [True]


def test_disabled_is_passthrough(monkeypatch):
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "0")
    model_concurrency.reset_for_tests()
    assert model_concurrency.enabled() is False
    ran = []
    with model_concurrency.model_call_slot("m", False, None):
        ran.append(True)
    assert ran == [True]


def test_deadline_failsoft_does_not_block(monkeypatch):
    """With cap=1 and a slot already held, a second acquire whose deadline is already
    past must NOT block — it proceeds without the slot (fail-soft)."""
    from ouroboros import model_concurrency

    monkeypatch.setenv("OUROBOROS_MODEL_MAX_CONCURRENCY", "1")
    model_concurrency.reset_for_tests()
    held = threading.Event()
    release = threading.Event()

    def holder():
        with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() + 30):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    assert held.wait(2)
    # Deadline already in the past -> acquire times out fast -> proceeds without throttle.
    t0 = time.time()
    ran = []
    with model_concurrency.model_call_slot("m", False, deadline_ts=time.time() - 1):
        ran.append(True)
    assert ran == [True]
    assert time.time() - t0 < 2.0, "fail-soft acquire must not block on a past deadline"
    release.set()
    t.join()

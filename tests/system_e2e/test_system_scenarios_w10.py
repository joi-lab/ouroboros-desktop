"""Wave 10 — S40: a silent worker draws attention, not a clock's kill (#1376, #2286).

A keyless real server runs from a byte-faithful checkout of the tree under test (S33's
``s33_candidate``: working-tree edits included, its process tree held until proven gone; a
HEAD clone only for a clean tree) with ONE real worker process. The owner acts only through the
HTTP door the UI posts to (Stop, with the UI's cascade); the idle rail, the silence episode, the
diagnostic admission, the Stop custody and its kill are the shipped ones.

Doubled, and only these: the model is the loopback stub; the hung worker is a real worker
frozen with SIGSTOP while its round sleeps out a provider 503's retry backoff, so no model or
tool call is in flight and the idle rail sees what a wedged worker shows it, a live and silent
process; the idle window and grace are shortened through their own settings (121 s, 10 s).

Silence past the window dates one episode on the source's record and admits one read-only
diagnostic of it, which waits for the only (frozen) slot. No clock kills, retries or replaces
the worker: #2286 retired the idle reap and its new-ID retry, the subject of #1729 / #1730
(their consumers keep unit coverage). The owner's Stop ends the tree: the frozen worker dies,
the root settles cancelled and the waiting diagnostic is withdrawn before any model call.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from tests.system_e2e.harness import (
    LANE_MOCK,
    ArtifactOracle,
    ScriptedStubModel,
    StubHttpError,
    body_text,
    keyless_settings,
    require_lane,
    start_server,
    submit_running,
    wait_durable_result,
    wait_until,
)
from tests.system_e2e.test_presence_resilience import s33_candidate  # noqa: F401 - pytest fixture

KEEPALIVE = {"tool": "list_files", "arguments": {"path": "."}}
# The idle window is max(idle setting, per-call ceiling + 120 s): both at their floors, and one worker.
SETTINGS = {"OUROBOROS_MAX_WORKERS": 1, "OUROBOROS_TASK_IDLE_TIMEOUT_SEC": 60,
            "OUROBOROS_PER_CALL_TIMEOUT_CEILING_SEC": 1, "OUROBOROS_FINALIZATION_GRACE_SEC": 10}
IDLE_WINDOW, GRACE = 121, 10


class _SilenceStub(ScriptedStubModel):
    """Keepalive rounds; while ``failing`` the agent rounds meet a provider 503."""

    def __init__(self) -> None:
        super().__init__(latency_sec=0.2)
        self.failing, self.failed_at = False, 0.0

    def _next_step(self, _body):
        return dict(KEEPALIVE)

    def _completion(self, body):
        if self.failing and body.get("tools"):
            self.failed_at = self.failed_at or time.time()
            raise StubHttpError(503, "stub upstream unavailable")
        return super()._completion(body)


def _rows(oracle) -> list:
    snapshot = oracle.queue_snapshot()
    return [dict(row.get("task") or {}, id=row.get("id"), queue=state)
            for state in ("pending", "running") for row in snapshot.get(state) or [] if isinstance(row, dict)]


def _alive(pid: int) -> bool:
    stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _descends(pid: int, ancestor: int) -> bool:
    while pid > 1:
        if pid == ancestor:
            return True
        parent = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True).stdout
        if not parent.strip():
            return False
        pid = int(parent)
    return False


def _worker_pid(oracle, server) -> int:
    """The one live worker of this server, by its own boot row (pid), proven a descendant."""
    live = sorted({int(row["pid"]) for row in oracle.events("worker_boot") if row.get("pid")
                   and _alive(int(row["pid"])) and _descends(int(row["pid"]), server.proc.pid)})
    assert len(live) == 1, (live, oracle.events("worker_boot"))
    return live[0]


@pytest.mark.integration
@pytest.mark.serial
# Evaluated before any fixture: the hang is SIGSTOP on a real worker read through ``ps``.
@pytest.mark.skipif(os.name != "posix", reason="freezes a real worker with SIGSTOP and ps: POSIX only")
def test_s40_a_silent_worker_draws_attention_and_the_owner_stop_ends_it(s33_candidate, tmp_path_factory):  # noqa: F811
    require_lane(LANE_MOCK)
    root = tmp_path_factory.mktemp("s40")
    frozen, facts = 0, {"tree": getattr(s33_candidate, "identity", str(s33_candidate))}
    with _SilenceStub() as stub:
        server = start_server(s33_candidate, root, keyless_settings(stub, **SETTINGS))
        oracle = ArtifactOracle(server.data_root)
        try:
            origin = submit_running(server, "Keep listing the repository files. [S40]")
            assert wait_until(lambda: len(stub.calls) >= 3, 60), stub.kinds()
            stub.failing = True
            assert wait_until(lambda: stub.failed_at, 120), stub.kinds()
            time.sleep(1.5)  # inside the 4 s retry backoff, the failed call's events delivered
            frozen = _worker_pid(oracle, server)
            os.kill(frozen, signal.SIGSTOP)
            stub.failing = False
            facts.update(origin=origin, frozen_pid=frozen, frozen_at=time.time())

            def children():
                return [row for row in _rows(oracle) if row["id"] != origin and row.get("root_task_id") == origin]

            # One episode: the source's record dates it and one read-only diagnostic of it waits for the slot.
            assert wait_until(children, IDLE_WINDOW + GRACE + 120, interval=2), _rows(oracle)
            (diagnostic,) = children()
            fact = oracle.task_result(origin).get("silence_attention") or {}
            facts.update(diagnostic=diagnostic["id"], silence=fact, after_sec=round(time.time() - facts["frozen_at"]))
            assert diagnostic.get("parent_task_id") == origin and diagnostic["queue"] == "pending", facts
            assert fact.get("diagnostic_id") == diagnostic["id"] and fact.get("self_delivered"), facts

            # No clock kills, retries or replaces it: past the window, its grace and a reap's margin.
            time.sleep(max(0.0, IDLE_WINDOW + GRACE + 30 - (time.time() - facts["frozen_at"])))
            assert _alive(frozen) and origin in oracle.running_ids(), _rows(oracle)
            assert [row["id"] for row in children()] == [diagnostic["id"]], _rows(oracle)

            # The owner's Stop (the UI posts cascade) ends the tree before the diagnostic runs.
            stopped = server.cancel_task(origin, cascade=True)
            assert stopped["status"] in (200, 202), stopped
            assert wait_until(lambda: not _alive(frozen), 60), "the owner's Stop never reached the frozen worker"
            assert wait_durable_result(oracle, origin, timeout=180).get("status") == "cancelled"
            assert wait_until(lambda: (oracle.task_result(diagnostic["id"]) or {}).get("status") == "cancelled"
                              and not children(), 120), _rows(oracle)
            with stub._lock:
                assert not [kind for kind, body in stub.calls if diagnostic["id"] in body_text(body)], \
                    "the withdrawn diagnostic reached the model"
        finally:
            if frozen and _alive(frozen):
                os.kill(frozen, signal.SIGKILL)
            print("S40_FACTS", json.dumps(facts, default=str))
            server.stop()

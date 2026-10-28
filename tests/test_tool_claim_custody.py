"""A returned call never stays «running»; an ended owner's local custody retires on positive evidence only.

The 28.10.2026 incident: a ``run_command`` returned in five seconds, its durable
claim could not be marked returned while the store was locked, the idle worker
that held it retired with no task (so nothing retired its claims), and macOS
gave its pid to another program, so Resume could never prove the owner dead.
Here: the process owes such a write until it lands (``custody_owed``); every
confirmed exit retires the exact process's claims across tasks and attempts;
startup and addressed actions retire only on positive evidence (kernel birth of
one kind, own pid of an earlier image, ESRCH, zombie); a known return never
regresses; a replica never carries custody.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from ouroboros import custody_owed, store, tool_custody
from ouroboros import local_custody_repair as repair
# Bind these modules' platform names BEFORE any test patches ``platform_layer``: a module first
# imported under a patch would keep the fake for every later test in this process.
from ouroboros import platform_layer, process_containment, process_custody, process_identity  # noqa: F401
from ouroboros.task_results import load_task_result, write_task_result

pytestmark = pytest.mark.serial

INCIDENT_BIRTH = "Tue Oct 27 14:57:17 2026"


def _claim(root, holder, op, *, pid, birth=INCIDENT_BIRTH, attempt=1, kernel="", state="claimed", extra=None):
    row = load_task_result(root, holder) or {}
    claims = dict(row.get("launch_handoffs") or {})
    claim = {"tool": "run_command", "task_id": holder, "root_task_id": "root", "state": state,
             "claimed_at": "2026-10-27T17:23:28+00:00",
             "local_owner": {"pid": pid, "process_birth": birth, "task_attempt": attempt}}
    if kernel:
        claim["owner_proof"] = {"pid": pid, "kernel_birth": kernel, "session": "an-earlier-session"}
    claim.update(extra or {})
    claims[op] = claim
    write_task_result(root, holder, row.get("status") or "completed", root_task_id="root", launch_handoffs=claims)


def _claims(root, holder):
    return dict((load_task_result(root, holder) or {}).get("launch_handoffs") or {})


def _retired(root, holder):
    return dict((load_task_result(root, holder) or {}).get("retired_tool_invocations") or {})


def _busy():
    raise store.StoreBusy("store held by a long reader", reason="contention")


# --- owed facts ---------------------------------------------------------------------------------------------------


def test_an_owed_fact_stays_until_its_write_lands_and_a_newer_observation_replaces_it():
    attempts = []
    custody_owed.owe("row", lambda: attempts.append("old") or _busy(), label="op-1")
    assert custody_owed.drain() == 1 and custody_owed.labels() == ["op-1"], "a failed write keeps the duty"
    landed = []
    custody_owed.owe("row", lambda: landed.append(True), label="op-1")
    assert custody_owed.drain() == 0 and landed == [True] and custody_owed.labels() == []
    assert attempts == ["old"], "the replaced observation is never written again"


def test_one_failing_fact_never_starves_another():
    custody_owed.owe("a", lambda: (_ for _ in ()).throw(OSError("disk")), label="a")
    done = []
    custody_owed.owe("b", lambda: done.append(True), label="b")
    assert custody_owed.drain() == 1 and done == [True] and custody_owed.labels() == ["a"]


def test_a_process_that_owes_a_custody_write_is_not_quiescent():
    baseline = tool_custody.local_producers_settled()
    custody_owed.owe("row", _busy, label="op-1")
    assert tool_custody.local_producers_settled() is False, "idle retirement must not forget an owed write"
    custody_owed._FACTS.clear()
    assert tool_custody.local_producers_settled() is baseline


def test_the_pause_names_owed_writes_without_waiting_for_them(tmp_path):
    from ouroboros.budget_pause import local_producer_observation

    custody_owed.owe("row", _busy, label="op-owed")
    ctx = SimpleNamespace(task_id="paused-task", drive_root=str(tmp_path), budget_drive_root=str(tmp_path),
                          task_metadata={}, task_attempt=1)
    observed = local_producer_observation(ctx, timeout_sec=0)
    assert observed["publication_pending"] == ["op-owed"]
    custody_owed._FACTS.clear()
    assert local_producer_observation(ctx, timeout_sec=0)["publication_pending"] == []


# --- exit doors: the exact process, task or no task --------------------------------------------------------------


def test_an_exit_retires_every_claim_of_that_exact_process_across_tasks_and_attempts(tmp_path):
    write_task_result(tmp_path, "root", "completed", root_task_id="root")
    _claim(tmp_path, "t1", "op-a", pid=63888, attempt=1)
    _claim(tmp_path, "t2", "op-b", pid=63888, attempt=3)
    _claim(tmp_path, "t2", "op-other", pid=63888, birth="another process at the same pid")
    _claim(tmp_path, "t3", "op-receipt", pid=63888, state="returned",
           extra={"returned_at": "2026-10-27T17:33:12+00:00",
                  "control_receipts": [{"type": "steer_task", "routing_token": ""}]})
    tool_custody.retire_process_invocations(tmp_path, pid=63888, process_birth=INCIDENT_BIRTH)
    assert "op-a" not in _claims(tmp_path, "t1") and _retired(tmp_path, "t1")["op-a"]["effect_outcome"] == "unknown"
    assert _retired(tmp_path, "t1")["op-a"]["replay_authorized"] is False, "an unknown effect is never replayed"
    assert "op-b" not in _claims(tmp_path, "t2"), "attempt is a scope of the work, not of the process"
    assert "op-other" in _claims(tmp_path, "t2"), "another process at the same pid stays held"
    assert _claims(tmp_path, "t3")["op-receipt"]["state"] == "returned", "a known return never regresses"


def test_kernel_evidence_decides_when_both_sides_carry_it(tmp_path):
    _claim(tmp_path, "t1", "op-k", pid=7, kernel="darwin-start:1793102237.000001")
    tool_custody.retire_process_invocations(tmp_path, pid=7, process_birth=INCIDENT_BIRTH,
                                            kernel_birth="darwin-start:1793132901.017497")
    assert "op-k" in _claims(tmp_path, "t1"), "same pid and spelling but another kernel birth: not this process"
    tool_custody.retire_process_invocations(tmp_path, pid=7, process_birth="",
                                            kernel_birth="darwin-start:1793102237.000001")
    assert "op-k" not in _claims(tmp_path, "t1")


def test_a_confirmed_exit_is_owed_under_the_lock_and_written_by_the_drain(tmp_path, monkeypatch):
    from supervisor import worker_health

    _claim(tmp_path, "t1", "op-a", pid=63888)
    monkeypatch.setattr(worker_health, "_pool", lambda: SimpleNamespace(DRIVE_ROOT=tmp_path))
    alive = SimpleNamespace(proc=SimpleNamespace(pid=63888, exitcode=None, is_alive=lambda: True),
                            process_birth=INCIDENT_BIRTH, kernel_birth="")
    worker_health.owe_worker_exit(alive)
    assert custody_owed.pending() == 0, "a live process is never an exit"
    dead = SimpleNamespace(proc=SimpleNamespace(pid=63888, exitcode=0, is_alive=lambda: False),
                           process_birth=INCIDENT_BIRTH, kernel_birth="")
    worker_health.owe_worker_exit(dead)
    assert custody_owed.pending() == 1 and "op-a" in _claims(tmp_path, "t1"), "only memory under the queue lock"
    assert custody_owed.drain() == 0
    assert "op-a" not in _claims(tmp_path, "t1") and _retired(tmp_path, "t1")["op-a"]["state"] == "owner_dead"


def test_retirement_keeps_a_known_return_and_rewrites_nothing_twice(tmp_path):
    _claim(tmp_path, "t1", "op-r", pid=5, birth="b", state="returned",
           extra={"returned_at": "2026-10-27T17:33:12+00:00",
                  "control_receipts": [{"type": "steer_task", "routing_token": ""}]})
    _claim(tmp_path, "t1", "op-c", pid=5, birth="b")
    tool_custody.retire_tool_invocations(tmp_path, "t1", "root", pid=5, process_birth="b", task_attempt=1)
    assert _claims(tmp_path, "t1")["op-r"]["state"] == "returned" and "op-r" not in _retired(tmp_path, "t1")
    first = _retired(tmp_path, "t1")["op-c"]
    tool_custody.retire_tool_invocations(tmp_path, "t1", "root", pid=5, process_birth="b", task_attempt=1)
    assert _retired(tmp_path, "t1")["op-c"] == first, "an already retired row is not rewritten"


# --- positive evidence at startup and at addressed actions ------------------------------------------------------


def _platform(monkeypatch, *, gone=(), kernel=None, lstart="Tue Oct 27 23:28:21 2026"):
    monkeypatch.setattr(platform_layer, "pid_provably_gone", lambda pid: pid in set(gone))
    monkeypatch.setattr(process_containment, "pid_is_zombie", lambda _pid: False)
    monkeypatch.setattr(process_identity, "process_kernel_birth", lambda pid: (kernel or {}).get(pid))
    monkeypatch.setattr(platform_layer, "process_start_time", lambda _pid: lstart)


@pytest.mark.parametrize("live, recorded, ended", [
    ("darwin-start:1793132901.017497", "darwin-start:1793102237.000001", True),
    ("darwin-start:1793102237.000001", "darwin-start:1793102237.000001", False),
    ("123." + "a" * 32, "darwin-start:1793102237.000001", False),
    (None, "darwin-start:1793102237.000001", False),
])
def test_a_reused_pid_is_ended_only_by_kernel_evidence_of_one_kind(monkeypatch, live, recorded, ended):
    _platform(monkeypatch, kernel={63888: live})
    proof = {"pid": 63888, "kernel_birth": recorded, "session": "s"}
    assert repair.owner_ended(63888, INCIDENT_BIRTH, proof) is ended


def test_a_legacy_lstart_owner_on_a_reused_pid_stays_held(monkeypatch):
    _platform(monkeypatch, kernel={63888: "darwin-start:1793132901.017497"})
    assert repair.owner_ended(63888, INCIDENT_BIRTH) is False, "two ps renderings never prove another process"
    assert repair.owner_ended(63888, INCIDENT_BIRTH, {"pid": 63888, "kernel_birth": "", "session": "s"}) is False


def test_an_earlier_image_of_this_very_pid_ended_at_exec():
    from ouroboros.process_custody import current_custody_session_id

    proof = {"pid": os.getpid(), "kernel_birth": "", "session": "an-earlier-image"}
    assert repair.owner_ended(os.getpid(), "unvalidated", proof) is True
    assert repair.owner_ended(os.getpid(), "unvalidated", {**proof, "session": current_custody_session_id()}) is False


def test_startup_reconcile_retires_only_positively_ended_owners(tmp_path, monkeypatch):
    _platform(monkeypatch, gone={111}, kernel={222: "darwin-start:5.000005", 333: "darwin-start:9.000009"})
    write_task_result(tmp_path, "root", "completed", root_task_id="root")
    _claim(tmp_path, "t-dead", "op-dead", pid=111)
    _claim(tmp_path, "t-live", "op-live", pid=222, kernel="darwin-start:5.000005")
    _claim(tmp_path, "t-reused", "op-reused", pid=333, kernel="darwin-start:1.000001")
    _claim(tmp_path, "t-legacy", "op-legacy", pid=444)
    retired = repair.reconcile_open_local_custody(tmp_path)
    assert {row["task_id"] for row in retired} == {"t-dead", "t-reused"}
    assert "op-live" in _claims(tmp_path, "t-live"), "a live owner of one kernel birth stays held"
    assert "op-legacy" in _claims(tmp_path, "t-legacy"), "an unprovable legacy owner stays held"
    assert repair.reconcile_open_local_custody(tmp_path) == [], "the second pass rests"


def test_a_failed_retirement_is_owed_by_the_process_not_left_as_a_witness(tmp_path, monkeypatch):
    from ouroboros import obligations

    _claim(tmp_path, "t1", "op-a", pid=5, birth="b")
    retire = tool_custody.retire_tool_invocations
    monkeypatch.setattr(tool_custody, "retire_tool_invocations",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("retirement disk failure")))
    assert repair.retire_ended_owner(tmp_path, "t1", "root", pid=5, birth="b", attempt=1, producer="repair") is False
    assert custody_owed.pending() == 1 and obligations.members(tmp_path, repair.WITNESS_SET) == {}
    monkeypatch.setattr(tool_custody, "retire_tool_invocations", retire)
    assert custody_owed.drain() == 0
    assert _retired(tmp_path, "t1")["op-a"]["effect_outcome"] == "unknown"


# --- replicas carry results, not custody ------------------------------------------------------------------------


def test_a_replica_never_carries_custody_fields():
    from ouroboros.post_task_checkpoint import CANONICAL_CUSTODY_FIELDS, project_replica_task_result_fields

    canonical = {"task_id": "t", "status": "running", "launch_handoffs": {"op": {"state": "claimed"}}}
    replica = {"task_id": "t", "status": "running", "result": "child result", "launch_handoffs": {},
               "retired_tool_invocations": {"x": {"state": "owner_dead"}}, "retired_model_consumers": {"c": {}}}
    overlay = project_replica_task_result_fields(canonical, replica)
    assert not set(CANONICAL_CUSTODY_FIELDS) & set(overlay), "a stale replica can neither revive nor erase custody"
    assert overlay.get("result") == "child result", "results still flow"


# --- the owner reads what is known, never «has not returned» ------------------------------------------------------


def test_a_held_call_is_worded_by_what_is_actually_known(monkeypatch):
    from ouroboros.process_custody import current_custody_session_id
    from ouroboros.work_stop import resume_outcome

    _platform(monkeypatch, kernel={63888: "darwin-start:1793132901.017497"})
    own = {"pid": os.getpid(), "kernel_birth": "", "session": current_custody_session_id()}
    claims = {
        "live": {"tool": "run_command", "state": "claimed", "owner_proof": own,
                 "local_owner": {"pid": os.getpid(), "process_birth": "x", "task_attempt": 1}},
        "unknown": {"tool": "run_command", "state": "claimed",
                    "local_owner": {"pid": 63888, "process_birth": INCIDENT_BIRTH, "task_attempt": 1}},
        "ended": {"tool": "mcp_gitlab__get_commit", "state": "owner_dead",
                  "external_pending": {"t": "mcp_transport"}},
        "returned": {"tool": "tell_task", "state": "returned",
                     "control_receipts": [{"type": "steer_task", "routing_token": "r"}]},
    }
    reasons = {name: tool_custody.tool_handoff_reason(claim) for name, claim in claims.items()}
    assert reasons["live"]["reason"] == "local_call_live"
    assert reasons["unknown"]["reason"] == "local_completion_unknown", "a reused pid is not proof of life"
    assert reasons["ended"] == {"reason": "owner_ended_effect_unknown", "tool": "mcp_gitlab__get_commit",
                                "effects": ["MCP request"]}
    assert reasons["returned"]["reason"] == "returned_effect_unconfirmed"
    blockers = [{"kind": "tool_handoff", "task_id": "96ac2f7c981b4f54", **reason} for reason in reasons.values()]
    message = resume_outcome({"ok": False, "blockers": blockers})["message"]
    assert "has not returned" not in message
    assert "it is unknown whether the run_command call of task 96ac2f7c981b4f54 finished" in message
    assert "the outcome of its MCP request is unknown" in message and "its message to a task is not confirmed" in message

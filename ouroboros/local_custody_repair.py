"""Retire a positively ENDED local owner's capability: at exit doors, at startup and at addressed actions.

A task's local tool invocation (``launch_handoffs`` claim) and its model-answer
receiver (``local_answer_consumer_id``) are writers only while their exact local
owner can still act. The existing writers retire them (``tool_custody``'s
retirement, ``model_wait.retire_model_consumers``) on positive evidence only:

- the supervisor's joined exit of a pool member: every exit door hands the
  exact process to ``retire_exited_process``, task or no task, and its open
  claims and receivers across tasks and attempts retire (#1554's missed doors);
- a platform-qualified ABSENCE of the recorded owner (``owner_ended``): its pid
  is positively gone (``platform_layer.pid_provably_gone``: POSIX ESRCH,
  Windows no such pid or a read exit code — never access denial, EPERM or an
  unexplained error), an exited zombie, the pid now belongs to a process whose
  validated birth differs (``process_identity``: kernel-derived, the recorded
  ``owner_proof`` first), or the pid is this very process but an earlier image
  wrote the row (its custody session differs: exec keeps pid and kernel birth).
  A ``ps`` wall-clock ``lstart`` string is rendered in its reader's timezone and
  locale, so two readings of one living process can differ: it never proves a
  different process. A mixed or unreadable identity, a live owner and a row
  without a recorded pid stay held;
- a legacy ``local_custody_witnesses`` row, which older releases kept when a
  retirement write failed: read and discharged, never written any more. A
  failed retirement write is now owed by the process that knows it
  (``custody_owed``).

The proof runs once per generation in the startup custody sweep, after the
process reaper (``reconcile_open_local_custody``, over the open set only), and
at each addressed action — Continue admission, Resume, the held-Continue
selection — for its own tree before it reads custody
(``repair_ended_local_custody``). Passive readers (census, GET) never write.
Generation change, queue absence, task terminality and age prove nothing.
Retirement changes local capability only: the effect stays ``unknown`` and is
never replayed, a known return stays ``returned``; delegated runs, processes,
merge and control receipts, patches and money keep their own custody.

Separately, ``ended_unbound_model_request`` reads an unbound send's retained
raised-failure fact for the passive writer census. It classifies an ended
request, retires nothing and preserves unresolved money and late-receipt rights.
"""

from __future__ import annotations

import logging
import os
import pathlib
from typing import Any, Dict, Iterable, List, Optional

log = logging.getLogger(__name__)

WITNESS_SET = "local_custody_witnesses"  # legacy rows only: read and discharged, never written


def owner_ended(pid: Any, birth: Any, proof: Any = None) -> bool:
    """Positive evidence that the local owner ``(pid, birth)`` (with its ``owner_proof``) no longer runs."""
    from ouroboros.platform_layer import pid_provably_gone, process_start_time
    from ouroboros.process_custody import current_custody_session_id
    from ouroboros.process_identity import different_birth, process_kernel_birth

    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    proof = proof if isinstance(proof, dict) else {}
    birth, kernel = str(birth or ""), str(proof.get("kernel_birth") or "")
    if pid <= 0 or not (birth or kernel):
        return False
    if pid == os.getpid():
        session = str(proof.get("session") or "")
        if session and session != current_custody_session_id():
            return True  # an earlier image of this pid wrote it; its memory ended at exec
    elif pid_provably_gone(pid):
        return True
    else:
        try:
            from ouroboros.process_containment import pid_is_zombie

            if pid_is_zombie(pid):
                return True  # exited; only its parent has not reaped it
        except Exception:
            return False
    if kernel and different_birth(process_kernel_birth(pid) or "", kernel):
        return True
    return bool(birth) and different_birth(process_start_time(pid), birth)


def owner_live(pid: Any, birth: Any, proof: Any = None) -> bool:
    """Positive evidence the recorded owner STILL runs: its pid carries the same validated birth.

    Used to word a hold honestly (``tool_custody.tool_handoff_reason``) and to
    refuse a decision to go on beside a live call; retirement never reads it. A ``ps`` string, a mixed kind or an unreadable
    birth is not evidence of either life or death.
    """
    from ouroboros.platform_layer import pid_provably_gone, process_start_time
    from ouroboros.process_custody import current_custody_session_id
    from ouroboros.process_identity import birth_identity, process_kernel_birth

    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    proof = proof if isinstance(proof, dict) else {}
    if pid <= 0:
        return False
    if pid == os.getpid():
        session = str(proof.get("session") or "")
        return not session or session == current_custody_session_id()
    if pid_provably_gone(pid):
        return False
    try:
        from ouroboros.process_containment import pid_is_zombie

        if pid_is_zombie(pid):
            return False
    except Exception:
        return False
    kernel = birth_identity(str(proof.get("kernel_birth") or ""))
    if kernel:
        return birth_identity(process_kernel_birth(pid) or "") == kernel
    recorded = birth_identity(str(birth or ""))
    return bool(recorded) and birth_identity(process_start_time(pid)) == recorded


def ended_unbound_model_request(attempt: Dict[str, Any]) -> bool:
    """An unbound attempt whose answer path already ended is not a live writer.

    The ``raised_exception`` failure is retained once the one path that could
    hand this attempt's answer to its caller ended without one: by
    ``usage_accounting._terminalize_failed_attempt`` after the send raised or
    could not start (an abandoned sender's late settlement included), or by
    the Responses search leg after its request or stream raised or closed
    incomplete. No receiver was bound, so nothing local can still take that
    answer; a detached I/O thread may only retain evidence. A missing HTTP
    status or a transport cause describes the remote exchange, whose outcome
    and money stay unresolved with their late-receipt right; nothing is
    retried. Bound or partially bound receivers, still-dispatched rows and
    rows without the retained fact stay held. No writes.
    """
    if (attempt.get("state") != "unresolved" or attempt.get("local_answer_consumer_id")
            or attempt.get("local_answer_owner_birth")
            or attempt.get("local_answer_task_attempt") is not None):
        return False
    failure = attempt.get("physical_failure")
    return isinstance(failure, dict) and failure.get("stage") == "raised_exception"


def _write_ended_owner(root: Any, task_id: str, root_task_id: str, *, pid: int, birth: str,
                       attempt: int, holder: str = "") -> None:
    """Both retirement writes for one ended owner; raises when either did not land."""
    from ouroboros.model_wait import retire_model_consumers
    from ouroboros.tool_custody import retire_tool_invocations
    from ouroboros import usage_store

    retire_tool_invocations(pathlib.Path(root), task_id, root_task_id, pid=pid, process_birth=birth,
                            task_attempt=attempt, holder=holder)
    with usage_store.read(pathlib.Path(root)) as txn:  # this task's rows only (task index)
        rows = txn.attempts("task_id = ?", (str(task_id),))
    consumers = {row["local_answer_consumer_id"]: attempt for row in rows
                 if row.get("task_id") == task_id and row.get("root_task_id") == root_task_id
                 and row.get("local_answer_owner_pid") == pid and row.get("local_answer_owner_birth") == birth
                 and type(row.get("local_answer_task_attempt")) is int
                 and row["local_answer_task_attempt"] == attempt
                 and isinstance(row.get("local_answer_consumer_id"), str) and row["local_answer_consumer_id"]}
    if consumers:
        retire_model_consumers(pathlib.Path(root), task_id, consumers)


def retire_ended_owner(root: Any, task_id: str, root_task_id: str, *, pid: int, birth: str,
                       attempt: int, producer: str, holder: str = "") -> bool:
    """Retire every claim and receiver of one ended local owner; owe the writes on failure.

    The exact owner identity (pid, birth, attempt, task and tree) selects; any
    other claim or consumer is untouched. Returns True when both writes landed;
    otherwise this process owes them (``custody_owed``) and keeps retrying.
    """
    from ouroboros import custody_owed

    def write():
        _write_ended_owner(root, task_id, root_task_id, pid=pid, birth=birth, attempt=attempt, holder=holder)
    try:
        write()
        return True
    except Exception:
        log.warning("Ended owner of %s (%s): retirement owed; retried by this process", task_id, producer,
                    exc_info=True)
        custody_owed.owe(("ended_owner", str(root), holder or task_id, task_id, pid, birth, attempt), write,
                         label=f"owner:{task_id}")
        return False


def retire_process_custody(root: Any, *, pid: int, process_birth: str, kernel_birth: str = "") -> None:
    """Every open claim and receiver one ended process held, across tasks and attempts; raises unwritten."""
    from ouroboros.model_wait import retire_model_consumers
    from ouroboros.tool_custody import retire_process_invocations
    from ouroboros import usage_store

    retire_process_invocations(root, pid=pid, process_birth=process_birth, kernel_birth=kernel_birth)
    with usage_store.read(pathlib.Path(root)) as txn:
        rows = [row for row in txn.open_attempts() if row.get("local_answer_owner_pid") == pid]
    by_task: Dict[str, Dict[str, int]] = {}
    for row in rows:
        proof = row.get("local_answer_owner_proof") if isinstance(row.get("local_answer_owner_proof"), dict) else {}
        same = (proof.get("kernel_birth") == kernel_birth if kernel_birth and proof.get("kernel_birth")
                else bool(process_birth) and row.get("local_answer_owner_birth") == process_birth)
        consumer, attempt = row.get("local_answer_consumer_id"), row.get("local_answer_task_attempt")
        if same and isinstance(consumer, str) and consumer and type(attempt) is int:
            by_task.setdefault(str(row.get("task_id") or ""), {})[consumer] = attempt
    for task_id, consumers in _unretired(root, by_task).items():
        retire_model_consumers(pathlib.Path(root), task_id, consumers)


def retire_exited_process(root: Any, *, pid: int, process_birth: str, kernel_birth: str = "") -> None:
    """An exit door's positive fact, owed in memory: safe under the queue lock; written off it.

    The supervisor confirmed this exact process ended (a joined retained
    Process). Its delivery duty applies the retirement (``custody_owed``).
    """
    from ouroboros import custody_owed

    if not pid or not (process_birth or kernel_birth):
        return
    custody_owed.owe(("exit", str(root), int(pid), str(process_birth), str(kernel_birth)),
                     lambda: retire_process_custody(root, pid=int(pid), process_birth=str(process_birth),
                                                    kernel_birth=str(kernel_birth)),
                     label=f"exit:{pid}")


def _unretired(root: Any, by_task: Dict[str, Dict[str, int]]) -> Dict[str, Dict[str, int]]:
    """Drop consumers whose retirement already landed: an idempotent pass writes nothing twice."""
    from ouroboros.store import open_store

    if not by_task:
        return {}
    with open_store(pathlib.Path(root), write=False) as txn:
        return {task_id: kept for task_id, consumers in by_task.items()
                if (kept := {consumer: attempt for consumer, attempt in consumers.items()
                             if txn.handoff(task_id, consumer) is None})}


def _discharge(root: Any, witness_id: str, facts: Dict[str, Any]) -> bool:
    """A legacy witness row: apply its retirement once, then remove it."""
    from ouroboros import obligations
    from ouroboros.model_wait import retire_model_consumers

    task_id = str(facts.get("task_id") or "")
    consumers = facts.get("model_consumers")
    owner = facts.get("local_owner") if isinstance(facts.get("local_owner"), dict) else {}
    if isinstance(consumers, dict) and consumers:
        retire_model_consumers(pathlib.Path(root), task_id, {str(k): int(v) for k, v in consumers.items()})
    elif not (owner and retire_ended_owner(root, task_id, str(facts.get("root_task_id") or task_id),
                                           pid=int(owner.get("pid") or 0), birth=str(owner.get("process_birth") or ""),
                                           attempt=int(owner.get("task_attempt") or 0), producer="repair",
                                           holder=str(facts.get("holder") or ""))):
        return False
    obligations.remove(root, WITNESS_SET, witness_id)
    return True


def _discharge_witnesses(root: Any, addressed) -> List[Dict[str, Any]]:
    from ouroboros import obligations

    repaired: List[Dict[str, Any]] = []
    try:
        witnesses = obligations.members(root, WITNESS_SET)
    except Exception:
        log.warning("Legacy local-custody witnesses unreadable; repair uses platform evidence only", exc_info=True)
        return repaired
    for witness_id, facts in witnesses.items():
        if not addressed(facts):
            continue
        try:
            if _discharge(root, witness_id, facts):
                repaired.append({"kind": "witness", "task_id": facts.get("task_id"),
                                 "producer": facts.get("producer"), "reason": facts.get("reason")})
        except Exception:
            log.warning("Local-custody witness %s not discharged", witness_id, exc_info=True)
    return repaired


def _repair_holder_claims(root: Any, holder: str) -> List[Dict[str, Any]]:
    """The claimed rows one task result holds, retired where their recorded owner positively ended."""
    from ouroboros.task_results import load_task_result
    from ouroboros.tool_custody import retire_process_invocations

    try:
        row = load_task_result(pathlib.Path(root), holder, strict=True) or {}
    except Exception:
        return []  # unreadable authority: nothing is retired on a guess
    owners: Dict[tuple, Dict[str, Any]] = {}
    for claim in (row.get("launch_handoffs") or {}).values():
        if not isinstance(claim, dict) or claim.get("state") != "claimed":
            continue  # no exact identity, or nothing local left to retire
        owner = claim.get("local_owner") if isinstance(claim.get("local_owner"), dict) else {}
        proof = claim.get("owner_proof") if isinstance(claim.get("owner_proof"), dict) else {}
        key = (str(claim.get("task_id") or ""), str(claim.get("root_task_id") or ""), owner.get("pid"),
               owner.get("process_birth"), owner.get("task_attempt"),
               proof.get("pid"), str(proof.get("kernel_birth") or ""), str(proof.get("session") or ""))
        owners[key] = proof
    repaired: List[Dict[str, Any]] = []
    for (task_id, tree, pid, birth, attempt, proof_pid, kernel, _session), proof in owners.items():
        if type(attempt) is int and pid and owner_ended(pid, birth, proof if proof_pid == pid else None):
            if retire_ended_owner(root, task_id, tree, pid=int(pid), birth=str(birth), attempt=attempt,
                                  producer="repair", holder=holder):
                repaired.append({"kind": "tool_owner_absent", "task_id": task_id, "pid": pid})
        elif not pid and proof_pid and kernel and owner_ended(proof_pid, "", proof):
            # An unbound invocation has no local_owner; its proof names the process.
            retire_process_invocations(root, pid=int(proof_pid), process_birth="", kernel_birth=kernel)
            repaired.append({"kind": "tool_owner_absent", "task_id": task_id, "pid": proof_pid})
    return repaired


def repair_ended_local_custody(root: Any, task_ids: Iterable[str], *, root_task_id: str) -> List[Dict[str, Any]]:
    """The addressed action's repair of ITS tree members; returns what it retired.

    Legacy witnesses first, then recorded owners with platform-qualified
    positive absence. Every failure leaves the custody exactly as held.
    """
    members = {str(task_id) for task_id in task_ids if task_id}

    def addressed(facts):
        holder = str(facts.get("holder") or "")
        return (holder in members and str(facts.get("root_task_id") or "") == str(root_task_id)
                if holder else str(facts.get("task_id") or "") in members)
    repaired = _discharge_witnesses(root, addressed)
    for holder in sorted(members):
        repaired.extend(_repair_holder_claims(root, holder))
    repaired.extend(_repair_absent_receivers(root, members, str(root_task_id)))
    return repaired


def reconcile_open_local_custody(root: Any) -> List[Dict[str, Any]]:
    """Once per generation, after the process reaper: retire open local custody whose owner positively ended.

    The open set only (claimed rows, open receivers, legacy witnesses), never
    task history; a live or unprovable owner stays held and is shown as such.
    """
    from ouroboros.store import open_store

    repaired = _discharge_witnesses(root, lambda _facts: True)
    with open_store(pathlib.Path(root), write=False) as txn:
        holders = sorted({str(row[0]) for row in txn.conn.execute(
            "SELECT DISTINCT task_id FROM task_handoffs WHERE kind='launch_handoffs' AND state='claimed'")})
    for holder in holders:
        repaired.extend(_repair_holder_claims(root, holder))
    repaired.extend(_repair_absent_receivers(root, None, None))
    return repaired


ACCEPTABLE_UNKNOWN = frozenset({"local_completion_unknown", "owner_ended_effect_unknown",
                                "returned_effect_unconfirmed"})


def receiver_reason(attempt: Dict[str, Any]) -> str:
    """An open model attempt's receiver, as ``tool_custody.tool_handoff_reason`` words a tool call.

    ``no_local_receiver``: no exact consumer and attempt are bound, so nothing local can be retired.
    """
    if not (isinstance(attempt.get("local_answer_consumer_id"), str) and attempt["local_answer_consumer_id"]
            and type(attempt.get("local_answer_task_attempt")) is int):
        return "no_local_receiver"
    proof = attempt.get("local_answer_owner_proof") if isinstance(attempt.get("local_answer_owner_proof"), dict) else {}
    try:
        live = owner_live(attempt.get("local_answer_owner_pid"), attempt.get("local_answer_owner_birth"),
                          proof if proof.get("pid") == attempt.get("local_answer_owner_pid") else None)
    except Exception:
        live = False  # an unreadable probe proves nothing either way
    return "local_call_live" if live else "local_completion_unknown"


def accept_unknown_custody(root: Any, members: Iterable[str], ids: Iterable[str], *, root_task_id: str,
                           by: Dict[str, Any]) -> Dict[str, List[Any]]:
    """The caller's decision to go on past NAMED held calls of one tree whose outcome is unknown.

    Only what the census words as unknown is accepted: a tool call
    (``tool_custody.tool_handoff_reason``) or a model answer receiver
    (``receiver_reason``) in a worker that cannot be proven live or ended, or a
    local call that is over while its outside effect is unconfirmed. A call in a
    live worker is refused, so no second writer starts beside it. The accepted
    tool row leaves ``launch_handoffs`` and stays in ``retired_tool_invocations``
    as observed, with ``effect_outcome="unknown_accepted"``,
    ``replay_authorized=False`` and the decision ``accepted`` (``by``: who, when,
    why, and the reason it was unknown); an accepted receiver is retired with the
    same record. Nothing is re-run and the outside effect is declared neither
    done nor failed; a late return of an accepted call writes nothing.
    """
    from ouroboros.model_wait import retire_model_consumers
    from ouroboros.owner_pause import tree_model_requests
    from ouroboros.task_results import load_task_result, require_writable_task_result_schema, stamp_task_result_schema
    from ouroboros.task_store import patch_document
    from ouroboros.tool_custody import tool_handoff_reason
    from ouroboros.utils import utc_now_iso

    wanted, at = {str(value) for value in ids if value}, utc_now_iso()
    accepted: List[str] = []
    refused: List[Dict[str, str]] = []

    def judged(op: str, task_id: str, reason: str) -> bool:
        if reason not in ACCEPTABLE_UNKNOWN:
            refused.append({"id": op, "task_id": task_id, "reason": reason or "unreadable"})
        return reason in ACCEPTABLE_UNKNOWN
    for holder in sorted({str(task_id) for task_id in members if task_id}):
        try:  # the reasons are probed before the write: no process probe holds the store
            claims = (load_task_result(pathlib.Path(root), holder, strict=True) or {}).get("launch_handoffs") or {}
        except Exception:
            continue  # unreadable authority: nothing is accepted on a guess
        reasons = {op: tool_handoff_reason(claim)["reason"] for op, claim in claims.items()
                   if op in wanted and isinstance(claim, dict)}
        decided = {op: (claims[op].get("state"), why) for op, why in reasons.items() if judged(op, holder, why)}
        landed: List[str] = []

        def accept(current, decided=decided, landed=landed):
            require_writable_task_result_schema(current)
            landed.clear()
            rows = dict(current.get("launch_handoffs") or {})
            retired = dict(current.get("retired_tool_invocations") or {})
            for op, (state, reason) in decided.items():
                if isinstance(rows.get(op), dict) and rows[op].get("state") == state:  # as judged, or left held
                    retired[op] = {**rows.pop(op), "retired_at": at, "effect_outcome": "unknown_accepted",
                                   "replay_authorized": False, "accepted": {**by, "reason": reason}}
                    landed.append(op)
            return stamp_task_result_schema({**current, "launch_handoffs": rows,
                                             "retired_tool_invocations": retired}) if landed else None
        if decided:
            patch_document(pathlib.Path(root), holder, accept)
            accepted.extend(landed)
    attempts = [row for row in tree_model_requests(pathlib.Path(root), root_task_id)  # the census's own read
                if str(row.get("attempt_id") or "") in wanted]
    for attempt in attempts:
        consumer, number = attempt.get("local_answer_consumer_id"), attempt.get("local_answer_task_attempt")
        why = receiver_reason(attempt)
        if why != "no_local_receiver" and not _unretired(root, {str(attempt.get("task_id") or ""): {consumer: number}}):
            continue  # already retired: nothing held, nothing to record twice
        if judged(str(attempt["attempt_id"]), str(attempt.get("task_id") or ""), why):
            retire_model_consumers(pathlib.Path(root), str(attempt.get("task_id") or ""), {consumer: number},
                                   record={"accepted": {**by, "reason": why}})
            accepted.append(str(attempt["attempt_id"]))
    return {"accepted": accepted, "refused": refused,
            "unmatched": sorted(wanted - set(accepted) - {row["id"] for row in refused})}


def _repair_absent_receivers(root: Any, members: Optional[set], root_task_id: Optional[str]) -> List[Dict[str, Any]]:
    """Open attempts (of this tree, or all) whose recorded answer receiver's process positively ended."""
    from ouroboros.model_wait import retire_model_consumers
    from ouroboros import usage_store

    try:  # the tree's own open rows (root index), or the open set at startup; never history
        with usage_store.read(pathlib.Path(root)) as txn:
            rows = [row for row in txn.open_attempts(root_task_id=root_task_id)
                    if members is None or str(row.get("task_id") or "") in members]
    except Exception:
        log.warning("Open attempts unreadable; receivers stay held", exc_info=True)
        return []
    by_task: Dict[str, Dict[str, int]] = {}
    pids: Dict[str, Any] = {}
    for row in rows:
        consumer, attempt = row.get("local_answer_consumer_id"), row.get("local_answer_task_attempt")
        if type(attempt) is not int or not isinstance(consumer, str) or not consumer:
            continue
        proof = row.get("local_answer_owner_proof") if isinstance(row.get("local_answer_owner_proof"), dict) else {}
        if owner_ended(row.get("local_answer_owner_pid"), row.get("local_answer_owner_birth"),
                       proof if proof.get("pid") == row.get("local_answer_owner_pid") else None):
            task_id = str(row.get("task_id") or "")
            by_task.setdefault(task_id, {})[consumer] = attempt
            pids[task_id] = row.get("local_answer_owner_pid")
    repaired: List[Dict[str, Any]] = []
    try:
        for task_id, consumers in _unretired(root, by_task).items():
            try:
                retire_model_consumers(pathlib.Path(root), task_id, consumers)
                repaired.append({"kind": "receiver_owner_absent", "task_id": task_id, "pid": pids[task_id]})
            except Exception:
                log.warning("Ended receivers of %s not retired; they stay held", task_id, exc_info=True)
    except Exception:
        log.warning("Retired receivers unreadable; receivers stay held", exc_info=True)
    return repaired

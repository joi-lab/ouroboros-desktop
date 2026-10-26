"""Synthetic legacy jobs for the residual timeout-retry consumer tests.

No runtime producer creates idle-retry jobs after #1376; the reap queue is
memory-only. These fixtures cover retained code, not current silence policy.
"""
import uuid

from supervisor import queue, workers


def _tick_until_reap_decision(clock, jobs, task_id="held"):
    idle = max(float(queue.get_task_idle_timeout_sec()), float(queue.get_per_call_timeout_ceiling_sec()) + 120.0)
    clock[0] += idle + float(queue.FINALIZATION_GRACE_SEC) + 2
    meta = workers.RUNNING.pop(task_id)
    task = meta["task"]
    worker = workers.WORKERS[meta["worker_id"]]
    worker.busy_task_id, worker.reaping = None, True
    will_retry = meta["attempt"] <= queue.QUEUE_MAX_RETRIES
    same_id = task.get("type") == "evolution" or task.get("delegation_role") == "subagent"
    retry_id = (task_id if same_id else uuid.uuid4().hex[:8]) if will_retry else ""
    job = {"worker_id": meta["worker_id"], "worker": worker, "proc": worker.proc,
           "drive_root": str(queue.DRIVE_ROOT), "meta": meta, "task_id": task_id,
           "task": dict(task), "task_type": task.get("type") or "task", "attempt": meta["attempt"],
           "terminal_reason": "idle_timeout", "runtime_sec": clock[0] - meta["started_at"],
           "hb_lag_sec": idle, "hb_stale": True, "owner_chat_id": 0,
           "deadline_reached": False, "ceiling_reached": False, "orchestrator": False,
           "will_retry": will_retry, "retry_task_id": retry_id}
    return job, idle

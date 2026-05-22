"""Supervisor task queue, persistence, timeouts, and evolution scheduling."""

from __future__ import annotations

import datetime
import json
import logging
import pathlib
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import (
    load_state, save_state, append_jsonl, atomic_write_text,
    QUEUE_SNAPSHOT_PATH, budget_pct, TOTAL_BUDGET_LIMIT,
    budget_remaining, EVOLUTION_BUDGET_RESERVE,
)
from supervisor.message_bus import send_with_budget
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


DRIVE_ROOT: pathlib.Path = pathlib.Path.home() / "Ouroboros" / "data"
SOFT_TIMEOUT_SEC: int = 600
HARD_TIMEOUT_SEC: int = 1800
HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1


def init(drive_root: pathlib.Path, soft_timeout: int, hard_timeout: int) -> None:
    global DRIVE_ROOT, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    DRIVE_ROOT = drive_root
    SOFT_TIMEOUT_SEC = soft_timeout
    HARD_TIMEOUT_SEC = hard_timeout


def refresh_timeouts_from_settings(settings: dict) -> None:
    """Hot-reload soft/hard timeouts independently, ignoring bad values."""
    global SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    soft_raw = settings.get("OUROBOROS_SOFT_TIMEOUT_SEC")
    if soft_raw is not None:
        try:
            SOFT_TIMEOUT_SEC = int(soft_raw)
        except (TypeError, ValueError):
            pass
    hard_raw = settings.get("OUROBOROS_HARD_TIMEOUT_SEC")
    if hard_raw is not None:
        try:
            HARD_TIMEOUT_SEC = int(hard_raw)
        except (TypeError, ValueError):
            pass


# Set by workers.init_queue_refs().
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
QUEUE_SEQ_COUNTER_REF: Dict[str, int] = {"value": 0}

# Guards PENDING/RUNNING mutations across main loop, direct chat, watchdog.
_queue_lock = threading.RLock()


def init_queue_refs(pending: List[Dict[str, Any]], running: Dict[str, Dict[str, Any]],
                    seq_counter_ref: Dict[str, int]) -> None:
    """Bind queue structures owned by workers.py."""
    global PENDING, RUNNING, QUEUE_SEQ_COUNTER_REF
    PENDING = pending
    RUNNING = running
    QUEUE_SEQ_COUNTER_REF = seq_counter_ref


def _task_priority(task_type: str) -> int:
    t = str(task_type or "").strip().lower()
    if t in ("task", "review", "deep_self_review"):
        return 0
    if t == "evolution":
        return 1
    return 2


def _queue_sort_key(task: Dict[str, Any]) -> Tuple[int, int]:
    _pr = task.get("priority")
    pr = int(_pr) if _pr is not None else _task_priority(str(task.get("type") or ""))
    _seq = task.get("_queue_seq")
    seq = int(_seq) if _seq is not None else 0
    return pr, seq


def sort_pending() -> None:
    """Sort pending queue by priority and insertion sequence."""
    PENDING.sort(key=_queue_sort_key)


def drain_all_pending() -> list:
    """Drain pending tasks during crash-storm cleanup; caller holds _queue_lock."""
    drained = list(PENDING)
    PENDING.clear()
    persist_queue_snapshot(reason="drain_all_pending")
    return drained


def enqueue_task(task: Dict[str, Any], front: bool = False) -> Dict[str, Any]:
    """Add task to PENDING."""
    t = dict(task)
    QUEUE_SEQ_COUNTER_REF["value"] += 1
    seq = QUEUE_SEQ_COUNTER_REF["value"]
    t.setdefault("priority", _task_priority(str(t.get("type") or "")))
    _att = t.get("_attempt")
    t.setdefault("_attempt", int(_att) if _att is not None else 1)
    t["_queue_seq"] = -seq if front else seq
    t["queued_at"] = utc_now_iso()
    PENDING.append(t)
    sort_pending()
    return t


def queue_has_task_type(task_type: str) -> bool:
    """Return whether this task type is pending or running."""
    tt = str(task_type or "")
    if any(str(t.get("type") or "") == tt for t in PENDING):
        return True
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        if isinstance(task, dict) and str(task.get("type") or "") == tt:
            return True
    return False


def persist_queue_snapshot(reason: str = "") -> None:
    """Persist queue snapshot for restart/recovery diagnostics."""
    pending_rows = []
    for t in PENDING:
        pending_rows.append({
            "id": t.get("id"), "type": t.get("type"), "priority": t.get("priority"),
            "attempt": t.get("_attempt"), "queued_at": t.get("queued_at"),
            "queue_seq": t.get("_queue_seq"),
            "task": {
                "id": t.get("id"), "type": t.get("type"), "chat_id": t.get("chat_id"),
                "text": t.get("text"), "priority": t.get("priority"),
                "depth": t.get("depth"), "description": t.get("description"),
                "context": t.get("context"), "parent_task_id": t.get("parent_task_id"),
                "root_task_id": t.get("root_task_id"), "session_id": t.get("session_id"),
                "actor_id": t.get("actor_id"), "delegation_role": t.get("delegation_role"),
                "workspace_root": t.get("workspace_root"), "workspace_mode": t.get("workspace_mode"),
                "memory_mode": t.get("memory_mode"), "drive_root": t.get("drive_root"),
                "budget_drive_root": t.get("budget_drive_root"),
                "metadata": t.get("metadata"),
                "_attempt": t.get("_attempt"), "review_reason": t.get("review_reason"),
                "review_source_task_id": t.get("review_source_task_id"),
            },
        })
    running_rows = []
    now = time.time()
    for task_id, meta in RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) else {}
        started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
        hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
        running_rows.append({
            "id": task_id, "type": task.get("type"), "priority": task.get("priority"),
            "attempt": meta.get("attempt"), "worker_id": meta.get("worker_id"),
            "runtime_sec": round(max(0.0, now - started), 2) if started > 0 else 0.0,
            "heartbeat_lag_sec": round(max(0.0, now - hb), 2) if hb > 0 else None,
            "soft_sent": bool(meta.get("soft_sent")), "task": task,
        })
    payload = {
        "ts": utc_now_iso(),
        "reason": reason,
        "pending_count": len(PENDING), "running_count": len(RUNNING),
        "pending": pending_rows, "running": running_rows,
    }
    try:
        atomic_write_text(QUEUE_SNAPSHOT_PATH, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        log.warning("Failed to persist queue snapshot (reason=%s)", reason, exc_info=True)
        pass


def parse_iso_to_ts(iso_ts: str) -> Optional[float]:
    """Parse ISO timestamp to Unix time."""
    txt = str(iso_ts or "").strip()
    if not txt:
        return None
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        log.debug("Failed to parse ISO timestamp: %s", txt, exc_info=True)
        return None


def restore_pending_from_snapshot(max_age_sec: int = 900) -> int:
    """Restore recent pending tasks from queue snapshot."""
    if PENDING:
        return 0
    try:
        if not QUEUE_SNAPSHOT_PATH.exists():
            return 0
        snap = json.loads(QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return 0
        ts = str(snap.get("ts") or "")
        ts_unix = parse_iso_to_ts(ts)
        if ts_unix is None:
            return 0
        if (time.time() - ts_unix) > max_age_sec:
            return 0
        restored = 0
        for row in (snap.get("pending") or []):
            task = row.get("task") if isinstance(row, dict) else None
            if not isinstance(task, dict):
                continue
            chat_id = task.get("chat_id")
            if not task.get("id") or chat_id is None or chat_id == "":
                continue
            enqueue_task(task)
            restored += 1
        if restored > 0:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "queue_restored_from_snapshot",
                    "restored_pending": restored,
                },
            )
            persist_queue_snapshot(reason="queue_restored")
        return restored
    except Exception:
        log.warning("Failed to restore pending queue from snapshot", exc_info=True)
        return 0


def cancel_task_by_id(task_id: str) -> bool:
    """Cancel a pending or running task by id."""
    from supervisor import workers

    with _queue_lock:
        for i, t in enumerate(list(PENDING)):
            if t["id"] == task_id:
                PENDING.pop(i)
                try:
                    from ouroboros.task_results import STATUS_CANCELLED, write_task_result
                    write_task_result(
                        DRIVE_ROOT, task_id, STATUS_CANCELLED,
                        result="Task cancelled by user/agent request.",
                    )
                except Exception:
                    pass
                persist_queue_snapshot(reason="cancel_pending")
                return True

        for w in workers.WORKERS.values():
            if w.busy_task_id == task_id:
                RUNNING.pop(task_id, None)
                try:
                    from ouroboros.task_results import STATUS_CANCELLED, write_task_result
                    write_task_result(
                        DRIVE_ROOT, task_id, STATUS_CANCELLED,
                        result="Running task cancelled and worker terminated.",
                    )
                except Exception:
                    pass
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
                if w.proc.is_alive() and w.proc.pid:
                    from ouroboros.platform_layer import kill_pid_tree
                    kill_pid_tree(w.proc.pid)
                    w.proc.join(timeout=2)
                workers.respawn_worker(w.wid)
                persist_queue_snapshot(reason="cancel_running")
                return True
    return False


def enforce_task_timeouts() -> None:
    """Enforce soft/hard timeouts for running tasks."""
    # Avoid circular dependency during module load.
    from supervisor import workers
    
    if not RUNNING:
        return
    now = time.time()
    st = load_state()
    owner_chat_id = int(st.get("owner_chat_id") or 0)

    for task_id, meta in list(RUNNING.items()):
        if not isinstance(meta, dict):
            continue
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        started_at = float(meta.get("started_at") or 0.0)
        if started_at <= 0:
            continue
        last_hb = float(meta.get("last_heartbeat_at") or started_at)
        runtime_sec = max(0.0, now - started_at)
        hb_lag_sec = max(0.0, now - last_hb)
        hb_stale = hb_lag_sec >= HEARTBEAT_STALE_SEC
        _wid = meta.get("worker_id")
        worker_id = int(_wid) if _wid is not None else -1
        task_type = str(task.get("type") or "")
        _att = meta.get("attempt")
        if _att is None:
            _att = task.get("_attempt")
        attempt = int(_att) if _att is not None else 1

        effective_soft = 3000 if task_type == "deep_self_review" else SOFT_TIMEOUT_SEC
        effective_hard = 3600 if task_type == "deep_self_review" else HARD_TIMEOUT_SEC

        if runtime_sec >= effective_soft and not bool(meta.get("soft_sent")):
            meta["soft_sent"] = True
            if owner_chat_id:
                send_with_budget(
                    owner_chat_id,
                    f"⏱️ Task {task_id} running for {int(runtime_sec)}s. "
                    f"type={task_type}, heartbeat_lag={int(hb_lag_sec)}s. Continuing.",
                )

        if runtime_sec < effective_hard:
            continue

        RUNNING.pop(task_id, None)
        if worker_id in workers.WORKERS and workers.WORKERS[worker_id].busy_task_id == task_id:
            workers.WORKERS[worker_id].busy_task_id = None

        if worker_id in workers.WORKERS:
            w = workers.WORKERS[worker_id]
            try:
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
                if w.proc.is_alive() and w.proc.pid:
                    from ouroboros.platform_layer import kill_pid_tree
                    kill_pid_tree(w.proc.pid)
                    w.proc.join(timeout=2)
            except Exception:
                log.warning("Failed to terminate worker %d during hard timeout", worker_id, exc_info=True)
            workers.respawn_worker(worker_id)

        try:
            from ouroboros.task_results import STATUS_FAILED, write_task_result
            write_task_result(
                DRIVE_ROOT, task_id, STATUS_FAILED,
                result=f"Task killed by hard timeout after {int(runtime_sec)}s.",
            )
        except Exception:
            pass

        requeued = False
        new_attempt = attempt
        if attempt <= QUEUE_MAX_RETRIES and isinstance(task, dict):
            retried = dict(task)
            retried["original_task_id"] = task_id
            retried["id"] = uuid.uuid4().hex[:8]
            retried["_attempt"] = attempt + 1
            retried["timeout_retry_from"] = task_id
            retried["timeout_retry_at"] = utc_now_iso()
            enqueue_task(retried, front=True)
            requeued = True
            new_attempt = attempt + 1

        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "task_hard_timeout",
                "task_id": task_id, "task_type": task_type,
                "worker_id": worker_id, "runtime_sec": round(runtime_sec, 2),
                "heartbeat_lag_sec": round(hb_lag_sec, 2), "heartbeat_stale": hb_stale,
                "attempt": attempt, "requeued": requeued, "new_attempt": new_attempt,
                "max_retries": QUEUE_MAX_RETRIES,
            },
        )

        if owner_chat_id:
            if requeued:
                send_with_budget(owner_chat_id, (
                    f"🛑 Hard-timeout: task {task_id} killed after {int(runtime_sec)}s.\n"
                    f"Worker {worker_id} restarted. Task queued for retry attempt={new_attempt}."
                ))
            else:
                send_with_budget(owner_chat_id, (
                    f"🛑 Hard-timeout: task {task_id} killed after {int(runtime_sec)}s.\n"
                    f"Worker {worker_id} restarted. Retry limit exhausted, task stopped."
                ))

        persist_queue_snapshot(reason="task_hard_timeout")


def build_evolution_task_text(cycle: int) -> str:
    """Build minimal evolution trigger; SYSTEM.md carries instructions."""
    return f"EVOLUTION #{cycle}"


def queue_deep_self_review_task(reason: str, model: str = "", force: bool = False) -> Optional[str]:
    """Queue a deep self-review task."""
    st = load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return None
    if (not force) and queue_has_task_type("deep_self_review"):
        return None
    tid = uuid.uuid4().hex[:8]
    enqueue_task({
        "id": tid,
        "type": "deep_self_review",
        "chat_id": int(owner_chat_id),
        "text": reason or "Deep self-review",
        "model": model,
    })
    persist_queue_snapshot(reason="deep_self_review_enqueued")
    send_with_budget(int(owner_chat_id), f"🔎 Deep self-review queued: {tid} ({reason})")
    return tid


def get_evolution_status_snapshot() -> Dict[str, Any]:
    """Return a non-mutating evolution scheduling snapshot."""
    st = load_state()
    enabled = bool(st.get("evolution_mode_enabled"))
    owner_chat_id = int(st.get("owner_chat_id") or 0)
    consecutive_failures = int(st.get("evolution_consecutive_failures") or 0)
    remaining = round(float(budget_remaining(st)), 2)
    queued_task = next((t for t in PENDING if str(t.get("type") or "") == "evolution"), None)
    running_task = next(
        (
            (meta.get("task") if isinstance(meta, dict) else None)
            for meta in RUNNING.values()
            if isinstance(meta, dict)
            and isinstance(meta.get("task"), dict)
            and str(meta["task"].get("type") or "") == "evolution"
        ),
        None,
    )
    status = "disabled"
    detail = "Evolution mode is off."

    if isinstance(running_task, dict):
        status = "running"
        detail = "Evolution task is running now."
    elif isinstance(queued_task, dict):
        status = "queued"
        detail = "Evolution task is queued and waiting for a worker."
    elif consecutive_failures >= 3:
        status = "paused_failures"
        detail = (
            f"Paused after {consecutive_failures} consecutive failures. "
            "Use Evolve again after investigating the failure."
        )
    elif enabled and not owner_chat_id:
        status = "waiting_for_owner_chat"
        detail = "Waiting for the first owner chat binding before scheduling evolution."
    elif enabled and remaining < EVOLUTION_BUDGET_RESERVE:
        status = "budget_blocked"
        detail = (
            f"Budget reserve active: ${remaining:.2f} remaining, "
            f"${EVOLUTION_BUDGET_RESERVE:.0f} reserved for conversations."
        )
    elif enabled and (PENDING or RUNNING):
        status = "waiting_for_idle"
        detail = "Waiting for active tasks to finish before the next evolution cycle."
    elif enabled:
        status = "idle_ready"
        detail = "Idle and ready to queue the next evolution cycle."
    elif remaining < EVOLUTION_BUDGET_RESERVE and str(st.get("last_evolution_task_at") or "").strip():
        status = "budget_stopped"
        detail = (
            f"Evolution auto-stopped because only ${remaining:.2f} remains, "
            f"below the ${EVOLUTION_BUDGET_RESERVE:.0f} conversation reserve."
        )

    return {
        "enabled": enabled,
        "status": status,
        "detail": detail,
        "cycle": int(st.get("evolution_cycle") or 0),
        "owner_chat_bound": bool(owner_chat_id),
        "last_task_at": str(st.get("last_evolution_task_at") or ""),
        "consecutive_failures": consecutive_failures,
        "budget_remaining_usd": remaining,
        "budget_reserve_usd": float(EVOLUTION_BUDGET_RESERVE),
        "pending_count": len(PENDING),
        "running_count": len(RUNNING),
        "queued_task_id": str((queued_task or {}).get("id") or ""),
        "running_task_id": str((running_task or {}).get("id") or ""),
    }


def enqueue_evolution_task_if_needed() -> None:
    """Queue evolution only when idle, enabled, within budget, and not failure-paused."""
    if PENDING or RUNNING:
        return
    st = load_state()
    if not bool(st.get("evolution_mode_enabled")):
        return
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    consecutive_failures = int(st.get("evolution_consecutive_failures") or 0)
    if consecutive_failures >= 3:
        st["evolution_mode_enabled"] = False
        save_state(st)
        send_with_budget(
            int(owner_chat_id),
            f"🧬⚠️ Evolution paused: {consecutive_failures} consecutive failures. "
            f"Use /evolve start to resume after investigating the issue."
        )
        return

    remaining = budget_remaining(st)
    if remaining < EVOLUTION_BUDGET_RESERVE:
        st["evolution_mode_enabled"] = False
        save_state(st)
        send_with_budget(int(owner_chat_id), f"💸 Evolution stopped: ${remaining:.2f} remaining (reserve ${EVOLUTION_BUDGET_RESERVE:.0f} for conversations).")
        return
    cycle = int(st.get("evolution_cycle") or 0) + 1
    tid = uuid.uuid4().hex[:8]
    enqueue_task({
        "id": tid, "type": "evolution",
        "chat_id": int(owner_chat_id),
        "text": build_evolution_task_text(cycle),
    })
    st["evolution_cycle"] = cycle
    st["last_evolution_task_at"] = utc_now_iso()
    save_state(st)
    send_with_budget(int(owner_chat_id), f"🧬 Evolution #{cycle}: {tid}")

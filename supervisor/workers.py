"""Worker lifecycle, health, and direct-chat handling for the supervisor."""

from __future__ import annotations
import logging
log = logging.getLogger(__name__)

import json
import multiprocessing as mp
import os
import pathlib
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from supervisor.state import load_state, append_jsonl
from supervisor import git_ops
from supervisor.message_bus import send_with_budget
from ouroboros.utils import utc_now_iso


REPO_DIR: pathlib.Path = pathlib.Path.home() / "Ouroboros" / "repo"
DRIVE_ROOT: pathlib.Path = pathlib.Path.home() / "Ouroboros" / "data"
MAX_WORKERS: int = 5
SOFT_TIMEOUT_SEC: int = 600
HARD_TIMEOUT_SEC: int = 1800
HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1
TOTAL_BUDGET_LIMIT: float = 0.0
BRANCH_DEV: str = "ouroboros"
BRANCH_STABLE: str = "ouroboros-stable"

_CTX = None
_LAST_SPAWN_TIME: float = 0.0  # grace period: don't count dead workers right after spawn
_SPAWN_GRACE_SEC: float = 90.0  # workers need up to ~60s to init (spawn + pip)

# Avoid spawn in frozen Unix apps: it re-imports __main__ and can fork-bomb.
# Workers do not touch GUI, so fork is safe there; Windows only supports spawn.
_DEFAULT_WORKER_START_METHOD = "spawn" if sys.platform == "win32" else "fork"
_WORKER_START_METHOD = str(os.environ.get("OUROBOROS_WORKER_START_METHOD", _DEFAULT_WORKER_START_METHOD) or _DEFAULT_WORKER_START_METHOD).strip().lower()
if _WORKER_START_METHOD not in {"fork", "spawn", "forkserver"}:
    _WORKER_START_METHOD = _DEFAULT_WORKER_START_METHOD


def _get_ctx():
    """Return the multiprocessing context for workers."""
    global _CTX
    if _CTX is None:
        _CTX = mp.get_context(_WORKER_START_METHOD)
    return _CTX


def init(repo_dir: pathlib.Path, drive_root: pathlib.Path, max_workers: int,
         soft_timeout: int, hard_timeout: int, total_budget_limit: float,
         branch_dev: str = "ouroboros", branch_stable: str = "ouroboros-stable") -> None:
    global REPO_DIR, DRIVE_ROOT, MAX_WORKERS, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    global TOTAL_BUDGET_LIMIT, BRANCH_DEV, BRANCH_STABLE
    REPO_DIR = repo_dir
    DRIVE_ROOT = drive_root
    MAX_WORKERS = max_workers
    SOFT_TIMEOUT_SEC = soft_timeout
    HARD_TIMEOUT_SEC = hard_timeout
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BRANCH_DEV = branch_dev
    BRANCH_STABLE = branch_stable

    from supervisor import queue
    queue.init(drive_root, soft_timeout, hard_timeout)
    queue.init_queue_refs(PENDING, RUNNING, QUEUE_SEQ_COUNTER_REF)

@dataclass
class Worker:
    wid: int
    proc: mp.Process
    in_q: Any
    busy_task_id: Optional[str] = None


_EVENT_Q = None


def get_event_q():
    """Return EVENT_Q, creating it lazily."""
    global _EVENT_Q
    if _EVENT_Q is None:
        _EVENT_Q = _get_ctx().Queue()
    return _EVENT_Q


WORKERS: Dict[int, Worker] = {}
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
CRASH_TS: List[float] = []
QUEUE_SEQ_COUNTER_REF: Dict[str, int] = {"value": 0}

# Shared queue lock; queue.py owns the canonical definition.
from supervisor.queue import _queue_lock


def get_running_task_ids() -> List[str]:
    """Return task IDs currently assigned to workers."""
    return [w.busy_task_id for w in WORKERS.values() if w.busy_task_id]

_chat_agent = None
# Serializes every direct-chat caller; _chat_agent has mutable per-call state.
import threading as _threading
_chat_agent_lock = _threading.Lock()


def _get_chat_agent():
    global _chat_agent
    if _chat_agent is None:
        if not getattr(sys, 'frozen', False):
            sys.path.insert(0, str(REPO_DIR))
        from ouroboros.agent import make_agent
        _chat_agent = make_agent(
            repo_dir=str(REPO_DIR),
            drive_root=str(DRIVE_ROOT),
            event_queue=get_event_q(),
        )
    return _chat_agent


def handle_chat_direct(chat_id: int, text: str, image_data: Optional[Union[Tuple[str, str], Tuple[str, str, str]]] = None, task_constraint: Optional[dict] = None) -> None:
    with _chat_agent_lock:
        _handle_chat_direct_locked(chat_id, text, image_data, task_constraint=task_constraint)


def _handle_chat_direct_locked(chat_id: int, text: str, image_data: Optional[Union[Tuple[str, str], Tuple[str, str, str]]] = None, task_constraint: Optional[dict] = None) -> None:
    from supervisor.state import budget_remaining, load_state
    if budget_remaining(load_state()) <= 0:
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().send_message(chat_id, "🚫 Budget exhausted. Task rejected. Please increase TOTAL_BUDGET in settings.")
        except Exception:
            pass
        return
        
    try:
        agent = _get_chat_agent()
        task = {
            "id": uuid.uuid4().hex[:8],
            "type": "task",
            "chat_id": chat_id,
            "text": text,
            "_is_direct_chat": True,
        }
        if task_constraint:
            task["task_constraint"] = dict(task_constraint)
        if image_data:
            # image_data is (base64, mime) or (base64, mime, caption).
            task["image_base64"] = image_data[0]
            task["image_mime"] = image_data[1]
            if len(image_data) > 2 and image_data[2]:
                task["image_caption"] = image_data[2]
                if not text:
                    task["text"] = image_data[2]
        if not task["text"]:
            task["text"] = "(image attached)" if image_data else ""
        events = agent.handle_task(task)
        for e in events:
            get_event_q().put(e)
    except Exception as e:
        import traceback
        err_msg = f"⚠️ Error: {type(e).__name__}: {e}"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "direct_chat_error",
                "error": repr(e),
                "traceback": str(traceback.format_exc())[:2000],
            },
        )
        try:
            from supervisor.message_bus import get_bridge
            get_bridge().send_message(chat_id, err_msg)
        except Exception:
            log.debug("Suppressed exception", exc_info=True)

def auto_resume_after_restart() -> None:
    """Auto-resume after a recent restart when scratchpad still has work."""
    try:
        owner_restart_flag = DRIVE_ROOT / "state" / "owner_restart_no_resume.flag"
        if owner_restart_flag.exists():
            owner_restart_flag.unlink(missing_ok=True)
            panic_compat_flag = DRIVE_ROOT / "state" / "panic_stop.flag"
            try:
                if panic_compat_flag.read_text(encoding="utf-8").strip() == "owner_restart_no_resume":
                    panic_compat_flag.unlink(missing_ok=True)
            except FileNotFoundError:
                pass
            except Exception:
                log.debug("Failed to consume owner restart compatibility flag", exc_info=True)
            log.info("Owner restart flag detected — skipping auto-resume.")
            return

        # Panic/owner-restart flags suppress auto-resume and are consumed.
        panic_flag = DRIVE_ROOT / "state" / "panic_stop.flag"
        if panic_flag.exists():
            panic_flag.unlink(missing_ok=True)
            log.info("Panic flag detected — skipping auto-resume.")
            return

        st = load_state()
        chat_id = st.get("owner_chat_id")
        if not chat_id:
            return

        restart_verify_path = DRIVE_ROOT / "state" / "pending_restart_verify.json"
        recent_restart = False
        if restart_verify_path.exists():
            recent_restart = True
        else:
            sup_log = DRIVE_ROOT / "logs" / "supervisor.jsonl"
            if sup_log.exists():
                try:
                    lines = sup_log.read_text(encoding="utf-8").strip().split("\n")
                    for line in reversed(lines[-20:]):
                        if not line.strip():
                            continue
                        evt = json.loads(line)
                        if evt.get("type") in ("launcher_start", "restart"):
                            recent_restart = True
                            break
                except Exception:
                    log.debug("Suppressed exception", exc_info=True)

        if not recent_restart:
            return

        scratchpad_path = DRIVE_ROOT / "memory" / "scratchpad.md"
        if not scratchpad_path.exists():
            return

        scratchpad = scratchpad_path.read_text(encoding="utf-8")
        stripped = scratchpad.strip()
        if not stripped or stripped == "# Scratchpad" or "(empty" in stripped.lower():
            content_lines = [
                ln.strip() for ln in stripped.splitlines()
                if ln.strip() and not ln.strip().startswith("#") and ln.strip() != "- (empty)"
            ]
            content_lines = [ln for ln in content_lines if not ln.startswith("UpdatedAt:")]
            if not content_lines:
                return

        time.sleep(2)  # Let everything initialize
        agent = _get_chat_agent()
        if not agent._busy:
            import threading
            threading.Thread(
                target=handle_chat_direct,
                args=(int(chat_id),
                      "[auto-resume after restart] Continue your work. Read scratchpad and identity — they contain context of what you were doing.",
                      None),
                daemon=True,
            ).start()
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "auto_resume_triggered",
                },
            )
    except Exception as e:
        append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
            "ts": utc_now_iso(),
            "type": "auto_resume_error",
            "error": repr(e),
        })

def worker_main(wid: int, in_q: Any, out_q: Any, repo_dir: str, drive_root: str) -> None:
    from ouroboros.platform_layer import create_new_session
    create_new_session()
    import sys as _sys
    import traceback as _tb
    import pathlib as _pathlib
    if not getattr(_sys, 'frozen', False):
        _sys.path.insert(0, repo_dir)
    _drive = _pathlib.Path(drive_root)
    # Spawned workers must pin the runtime-mode baseline from the parent env;
    # forked workers inherit it. This keeps the elevation ratchet consistent.
    try:
        from ouroboros.config import initialize_runtime_mode_baseline
        initialize_runtime_mode_baseline()
    except Exception:
        # Non-fatal: save_settings still has env-var fallback gating.
        try:
            _log_worker_crash(wid, _drive, "init_baseline", None, _tb.format_exc())
        except Exception:
            pass
    try:
        from ouroboros.agent import make_agent
        agent = make_agent(repo_dir=repo_dir, drive_root=drive_root, event_queue=out_q)
    except Exception as _e:
        _log_worker_crash(wid, _drive, "make_agent", _e, _tb.format_exc())
        return
    while True:
        try:
            task = in_q.get()
            if task is None or task.get("type") == "shutdown":
                break
            task_drive_root = str(task.get("drive_root") or drive_root)
            if task_drive_root != str(drive_root):
                task_agent = make_agent(repo_dir=repo_dir, drive_root=task_drive_root, event_queue=out_q)
                events = task_agent.handle_task(task)
            else:
                events = agent.handle_task(task)
            for e in events:
                e2 = dict(e)
                e2["worker_id"] = wid
                out_q.put(e2)
        except Exception as _e:
            _log_worker_crash(wid, _drive, "handle_task", _e, _tb.format_exc())


def _write_failure_result(
    task_id: str,
    reason: str = "Worker process crashed (crash storm). Task was not completed.",
    status: str = "",
) -> None:
    """Write failure result for a crashed/orphaned task."""
    if not task_id:
        return
    try:
        from ouroboros.task_results import (
            STATUS_FAILED, STATUS_COMPLETED, STATUS_REJECTED_DUPLICATE,
            STATUS_CANCELLED, load_task_result, write_task_result,
        )
        # STATUS_INTERRUPTED is not final; it is written before requeue.
        _FINAL_STATUSES = {STATUS_COMPLETED, STATUS_FAILED, STATUS_REJECTED_DUPLICATE, STATUS_CANCELLED}
        existing = load_task_result(DRIVE_ROOT, task_id)
        if existing and existing.get("status") in _FINAL_STATUSES:
            return
        write_task_result(
            DRIVE_ROOT,
            task_id,
            status or STATUS_FAILED,
            result=reason,
            cost_usd=0,
            total_rounds=0,
        )
    except Exception:
        log.warning("Failed to write failure result for task %s", task_id, exc_info=True)


def _log_worker_crash(wid: int, drive_root: pathlib.Path, phase: str, exc: Exception, tb: str) -> None:
    """Best-effort worker-side crash logging."""
    import os as _os
    try:
        path = drive_root / "logs" / "supervisor.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts": utc_now_iso(),
            "type": "worker_crash",
            "worker_id": wid,
            "pid": _os.getpid(),
            "phase": phase,
            "error": repr(exc),
            "traceback": str(tb)[:3000],
        }, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        log.debug("Suppressed exception", exc_info=True)


def _first_worker_boot_event_since(offset_bytes: int) -> Optional[Dict[str, Any]]:
    """Read first worker_boot event after a file offset."""
    path = DRIVE_ROOT / "logs" / "events.jsonl"
    if not path.exists():
        return None
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            safe_offset = offset_bytes if 0 <= offset_bytes <= size else 0
            f.seek(safe_offset)
            data = f.read().decode("utf-8", errors="replace")
    except Exception:
        log.debug("Suppressed exception", exc_info=True)
        return None

    for line in data.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            evt = json.loads(raw)
        except Exception:
            log.debug("Suppressed exception in loop", exc_info=True)
            continue
        if isinstance(evt, dict) and str(evt.get("type") or "") == "worker_boot":
            return evt
    return None


def _verify_worker_sha_after_spawn(events_offset: int, timeout_sec: float = 90.0) -> None:
    """Verify newly spawned workers booted at expected current_sha."""
    st = load_state()
    expected_sha = str(st.get("current_sha") or "").strip()
    if not expected_sha:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "worker_sha_verify_skipped",
                "reason": "missing_current_sha",
            },
        )
        return

    deadline = time.time() + max(float(timeout_sec), 1.0)
    boot_evt = None
    while time.time() < deadline:
        boot_evt = _first_worker_boot_event_since(events_offset)
        if boot_evt is not None:
            break
        time.sleep(0.25)

    if boot_evt is None:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "worker_sha_verify_timeout",
                "expected_sha": expected_sha,
            },
        )
        return

    observed_sha = str(boot_evt.get("git_sha") or "").strip()
    ok = bool(observed_sha and observed_sha == expected_sha)
    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "worker_sha_verify",
            "ok": ok,
            "expected_sha": expected_sha,
            "observed_sha": observed_sha,
            "worker_pid": boot_evt.get("pid"),
        },
    )
    if not ok and st.get("owner_chat_id"):
        send_with_budget(
            int(st["owner_chat_id"]),
            f"⚠️ Worker SHA mismatch after spawn: expected {expected_sha[:8]}, got {(observed_sha or 'unknown')[:8]}",
        )


def spawn_workers(n: int = 0) -> None:
    global _CTX, _EVENT_Q
    # Fresh context ensures workers use current code.
    _CTX = mp.get_context(_WORKER_START_METHOD)
    _EVENT_Q = _CTX.Queue()
    events_path = DRIVE_ROOT / "logs" / "events.jsonl"
    try:
        events_offset = int(events_path.stat().st_size)
    except Exception:
        events_offset = 0

    count = n or MAX_WORKERS
    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "worker_spawn_start",
            "start_method": _WORKER_START_METHOD,
            "count": count,
        },
    )
    WORKERS.clear()
    for i in range(count):
        in_q = _CTX.Queue()
        proc = _CTX.Process(target=worker_main,
                           args=(i, in_q, _EVENT_Q, str(REPO_DIR), str(DRIVE_ROOT)))
        proc.daemon = True
        proc.start()
        WORKERS[i] = Worker(wid=i, proc=proc, in_q=in_q, busy_task_id=None)
    global _LAST_SPAWN_TIME
    _LAST_SPAWN_TIME = time.time()
    # Verify asynchronously so spawn does not block the supervisor loop.
    threading.Thread(target=_verify_worker_sha_after_spawn, args=(events_offset,), daemon=True).start()


def kill_workers(
    force: bool = True,
    *,
    result_reason: str = "Worker process crashed (crash storm). Task was not completed.",
    result_status: str = "",
) -> None:
    from supervisor import queue
    with _queue_lock:
        cleared_running = len(RUNNING)
        for w in WORKERS.values():
            if w.proc.is_alive():
                w.proc.terminate()
        for w in WORKERS.values():
            w.proc.join(timeout=3)
        _kill_survivors()
        WORKERS.clear()
        try:
            orphaned_ids = []
            for task_id in list(RUNNING):
                try:
                    _write_failure_result(task_id, reason=result_reason, status=result_status)
                    orphaned_ids.append(task_id)
                except Exception:
                    log.warning("Failed to write failure result for running task %s", task_id, exc_info=True)
            drained = queue.drain_all_pending()
            drained_ids = []
            for task in drained:
                tid = task.get("id")
                if tid:
                    try:
                        _write_failure_result(tid, reason=result_reason, status=result_status)
                        drained_ids.append(tid)
                    except Exception:
                        log.warning("Failed to write failure result for pending task %s", tid, exc_info=True)
            if orphaned_ids or drained_ids:
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "zombie_prevention_cleanup",
                        "orphaned_running": orphaned_ids,
                        "drained_pending": drained_ids,
                    },
                )
        except Exception:
            log.warning("Zombie prevention cleanup failed", exc_info=True)
        RUNNING.clear()
    queue.persist_queue_snapshot(reason="kill_workers")
    if cleared_running:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "running_cleared_on_kill", "count": cleared_running,
                "force": force,
            },
        )


def _kill_survivors() -> None:
    """Force-kill any workers and their entire descendant trees."""
    from ouroboros.platform_layer import kill_pid_tree
    for w in WORKERS.values():
        pid = w.proc.pid
        if pid is None:
            continue
        if w.proc.is_alive():
            kill_pid_tree(pid)
            w.proc.join(timeout=2)


def respawn_worker(wid: int) -> None:
    ctx = _get_ctx()
    in_q = ctx.Queue()
    proc = ctx.Process(target=worker_main,
                       args=(wid, in_q, get_event_q(), str(REPO_DIR), str(DRIVE_ROOT)))
    proc.daemon = True
    proc.start()
    WORKERS[wid] = Worker(wid=wid, proc=proc, in_q=in_q, busy_task_id=None)
    # Do not reset _LAST_SPAWN_TIME here; respawn grace would hide crash storms.


def assign_tasks() -> None:
    from supervisor import queue
    from supervisor.state import budget_remaining, EVOLUTION_BUDGET_RESERVE
    with _queue_lock:
        st = load_state()
        remaining = budget_remaining(st)
        
        if remaining <= 0:
            return  # Stop assigning ALL tasks if budget is completely exhausted
            
        for w in WORKERS.values():
            if w.busy_task_id is None and PENDING:
                # Find first suitable task (skip over-budget evolution tasks)
                chosen_idx = None
                for i, candidate in enumerate(PENDING):
                    if str(candidate.get("type") or "") == "evolution" and remaining < EVOLUTION_BUDGET_RESERVE:
                        continue
                    chosen_idx = i
                    break
                if chosen_idx is None:
                    # Only over-budget evolution tasks remain — clean them out
                    PENDING[:] = [t for t in PENDING if str(t.get("type") or "") != "evolution"]
                    queue.persist_queue_snapshot(reason="evolution_dropped_budget")
                    continue
                task = PENDING.pop(chosen_idx)
                w.busy_task_id = task["id"]
                w.in_q.put(task)
                now_ts = time.time()
                RUNNING[task["id"]] = {
                    "task": dict(task), "worker_id": w.wid,
                    "started_at": now_ts, "last_heartbeat_at": now_ts,
                    "soft_sent": False, "attempt": int(task.get("_attempt") or 1),
                }
                task_type = str(task.get("type") or "")
                if task_type in ("evolution", "review"):
                    st = load_state()
                    if st.get("owner_chat_id"):
                        emoji = '🧬' if task_type == 'evolution' else '🔎'
                        send_with_budget(
                            int(st["owner_chat_id"]),
                            f"{emoji} {task_type.capitalize()} task {task['id']} started.",
                        )
                queue.persist_queue_snapshot(reason="assign_task")

def ensure_workers_healthy() -> None:
    from supervisor import queue
    # Workers need init time after spawn.
    if (time.time() - _LAST_SPAWN_TIME) < _SPAWN_GRACE_SEC:
        return
    busy_crashes = 0
    dead_detections = 0
    crashed_tasks = []
    for wid, w in list(WORKERS.items()):
        if not w.proc.is_alive():
            dead_detections += 1
            if w.busy_task_id is not None:
                busy_crashes += 1
            exitcode = w.proc.exitcode
            meta = RUNNING.get(w.busy_task_id, {}) if w.busy_task_id else {}
            task_info = meta.get("task", {}) if isinstance(meta, dict) else {}
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "worker_dead_detected",
                    "worker_id": wid,
                    "exitcode": exitcode,
                    "busy_task_id": w.busy_task_id,
                    "task_type": task_info.get("type") if isinstance(task_info, dict) else None,
                    "task_description": (task_info.get("description", "") or "")[:200] if isinstance(task_info, dict) else None,
                    "uptime_sec": round(time.time() - meta["started_at"]) if isinstance(meta, dict) and meta.get("started_at") else None,
                    "attempt": meta.get("attempt") if isinstance(meta, dict) else None,
                    "signal": -exitcode if isinstance(exitcode, int) and exitcode < 0 else None,
                },
            )
            if w.busy_task_id and isinstance(meta, dict) and meta.get("task"):
                crashed_tasks.append({"task_id": w.busy_task_id, "task_type": task_info.get("type") if isinstance(task_info, dict) else None})
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "worker_crash_task_dump",
                        "worker_id": wid,
                        "task": meta["task"],
                        "started_at": meta.get("started_at"),
                        "last_heartbeat_at": meta.get("last_heartbeat_at"),
                        "attempt": meta.get("attempt"),
                    },
                )
            if w.busy_task_id and w.busy_task_id in RUNNING:
                meta = RUNNING.pop(w.busy_task_id) or {}
                task = meta.get("task") if isinstance(meta, dict) else None
                if isinstance(task, dict):
                    task_type = str(task.get("type") or "")
                    # deep_self_review SIGSEGVs can loop forever on macOS fork-safety paths.
                    is_crash_signal = isinstance(exitcode, int) and exitcode < 0
                    no_retry_types = {"deep_self_review"}
                    if task_type in no_retry_types and is_crash_signal:
                        try:
                            from ouroboros.task_results import STATUS_FAILED, write_task_result
                            write_task_result(
                                DRIVE_ROOT, str(w.busy_task_id), STATUS_FAILED,
                                result=(
                                    f"❌ Deep self-review worker crashed (signal {-exitcode}). "
                                    "This is likely a platform fork-safety issue on macOS. "
                                    "The task will not be retried automatically. "
                                    "Use /restart and then /review to try again after a clean restart."
                                ),
                            )
                        except Exception:
                            log.debug("Failed to write failed status for %s", w.busy_task_id, exc_info=True)
                        # Message before task_done: otherwise the UI may close the card first.
                        chat_id = int(task.get("chat_id") or 1)
                        try:
                            from supervisor.message_bus import get_bridge
                            bridge = get_bridge()
                            if bridge is not None:
                                bridge.send_message(
                                    chat_id,
                                    "❌ Deep self-review failed: worker process crashed (SIGSEGV). "
                                    "This is a known macOS fork-safety limitation. "
                                    "Please use `/restart` and then `/review` to retry with a fresh process.",
                                )
                        except Exception:
                            log.debug("Failed to send deep_self_review crash message", exc_info=True)
                        try:
                            get_event_q().put({
                                "type": "task_done",
                                "task_id": str(w.busy_task_id),
                                "chat_id": chat_id,
                                "status": "failed",
                            })
                        except Exception:
                            log.debug("Failed to emit task_done for deep_self_review crash %s", w.busy_task_id, exc_info=True)
                    else:
                        attempt = int(task.get("_attempt") or 1)
                        # Check if this task already completed via inline/direct-chat path
                        already_done = False
                        try:
                            from ouroboros.task_results import (
                                load_task_result,
                                STATUS_COMPLETED, STATUS_FAILED, STATUS_REJECTED_DUPLICATE,
                                STATUS_CANCELLED,
                            )
                            # STATUS_INTERRUPTED precedes requeue and is not terminal.
                            _TERMINAL_STATUSES = {
                                STATUS_COMPLETED, STATUS_FAILED,
                                STATUS_REJECTED_DUPLICATE, STATUS_CANCELLED,
                            }
                            existing = load_task_result(DRIVE_ROOT, str(w.busy_task_id))
                            if existing and existing.get("status") in _TERMINAL_STATUSES:
                                already_done = True
                                log.info(
                                    "Skipping requeue for task %s — already in terminal state: %s",
                                    w.busy_task_id, existing.get("status"),
                                )
                        except Exception:
                            log.debug("Failed to check existing result for %s", w.busy_task_id, exc_info=True)

                        if already_done:
                            pass
                        elif attempt > QUEUE_MAX_RETRIES:
                            log.warning(
                                "Task %s exceeded crash retry limit (%d/%d) — marking failed",
                                w.busy_task_id, attempt, QUEUE_MAX_RETRIES,
                            )
                            try:
                                from ouroboros.task_results import STATUS_FAILED, write_task_result
                                write_task_result(
                                    DRIVE_ROOT, str(w.busy_task_id), STATUS_FAILED,
                                    result=(
                                        f"❌ Task failed after {attempt} crash(es) "
                                        f"(signal {-exitcode if isinstance(exitcode, int) and exitcode < 0 else exitcode}). "
                                        "Worker process crashed repeatedly — likely a platform-level issue. "
                                        "Please try again or use a different approach."
                                    ),
                                )
                            except Exception:
                                log.debug("Failed to write failed status for %s", w.busy_task_id, exc_info=True)
                            chat_id = int(task.get("chat_id") or 1)
                            try:
                                from supervisor.message_bus import get_bridge
                                bridge = get_bridge()
                                if bridge is not None:
                                    bridge.send_message(
                                        chat_id,
                                        f"❌ Task `{str(w.busy_task_id)[:8]}` failed after "
                                        f"{attempt} crash(es). Worker process crashed "
                                        "repeatedly. Please try again.",
                                    )
                            except Exception:
                                log.debug("Failed to send failure message for %s", w.busy_task_id, exc_info=True)
                            try:
                                get_event_q().put({
                                    "type": "task_done",
                                    "task_id": str(w.busy_task_id),
                                    "chat_id": chat_id,
                                    "status": "failed",
                                })
                            except Exception:
                                log.debug("Failed to emit terminal event for %s", w.busy_task_id, exc_info=True)
                        else:
                            task = dict(task)
                            task["_attempt"] = attempt + 1
                            try:
                                from ouroboros.task_results import STATUS_INTERRUPTED, write_task_result
                                write_task_result(
                                    DRIVE_ROOT, str(w.busy_task_id), STATUS_INTERRUPTED,
                                    result=f"Worker process died mid-task (attempt {attempt}). Retrying.",
                                )
                            except Exception:
                                log.debug("Failed to write interrupted status for %s", w.busy_task_id, exc_info=True)
                            queue.enqueue_task(task, front=True)
            respawn_worker(wid)
            queue.persist_queue_snapshot(reason="worker_respawn_after_crash")

    now = time.time()
    alive_now = sum(1 for w in WORKERS.values() if w.proc.is_alive())
    if dead_detections:
        # Only count busy crashes or all-workers-dead as storm signals.
        if busy_crashes > 0 or alive_now == 0:
            CRASH_TS.extend([now] * max(1, dead_detections))
        else:
            CRASH_TS.clear()

    CRASH_TS[:] = [t for t in CRASH_TS if (now - t) < 60.0]
    if len(CRASH_TS) >= 3:
        # Do not execv on crash storms; keep direct-chat mode alive.
        st = load_state()
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "crash_storm_detected",
                "crash_count": len(CRASH_TS),
                "worker_count": len(WORKERS),
                "crashed_tasks": crashed_tasks,
            },
        )
        if st.get("owner_chat_id"):
            send_with_budget(
                int(st["owner_chat_id"]),
                "⚠️ Frequent worker crashes. Multiprocessing workers disabled, "
                "continuing in direct-chat mode (threading).",
            )
        kill_workers()
        CRASH_TS.clear()

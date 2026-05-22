"""Single FIFO lane for mutating skill install/review/dependency/enable work."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import pathlib
import re
import threading
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, Optional

from ouroboros.utils import utc_now_iso as _now_iso

log = logging.getLogger(__name__)

_MAX_EVENTS = 80
_STALE_RUNNING_JOB_SEC = int(os.environ.get("OUROBOROS_SKILL_LIFECYCLE_STALE_SEC", "1800"))


def _iso_age_seconds(value: str) -> int:
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int((datetime.now(timezone.utc) - dt).total_seconds()))
    except Exception:
        return 0


@dataclass
class LifecycleJob:
    id: str
    kind: str
    target: str
    source: str = ""
    dedupe_key: str = ""
    status: str = "queued"
    message: str = ""
    error: str = ""
    queued_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        age_seconds = _iso_age_seconds(self.started_at or self.queued_at)
        stale = bool(self.status == "running" and age_seconds >= _STALE_RUNNING_JOB_SEC)
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "source": self.source,
            "dedupe_key": self.dedupe_key,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "age_seconds": age_seconds,
            "stale": stale,
            "stale_reason": "running_too_long" if stale else "",
            "recovery_hint": (
                "Lifecycle work is still running in-process. Restart Ouroboros to clear "
                "a stuck worker after preserving logs."
                if stale else ""
            ),
        }


@dataclass
class LifecycleJobOptions:
    """Optional lifecycle hooks and formatters kept out of the main API arity."""

    drive_root: pathlib.Path | str | None = None
    result_message: Callable[[Any], str] | None = None
    result_error: Callable[[Any], str] | None = None
    progress_target: Optional["JobProgressTarget"] = None
    on_started: Callable[[LifecycleJob], None] | None = None
    on_finished: Callable[[LifecycleJob, Any, BaseException | None], None] | None = None


_lock: Optional[threading.Lock] = None
_state_lock = threading.Lock()
_dedupe_jobs: Dict[str, LifecycleJob] = {}
_events: Deque[LifecycleJob] = deque(maxlen=_MAX_EVENTS)
_active: Optional[LifecycleJob] = None


class DuplicateLifecycleJobError(RuntimeError):
    """Raised when a caller attempts to queue an already active lifecycle job."""

    def __init__(self, job: LifecycleJob) -> None:
        self.job = job
        super().__init__(f"lifecycle job already {job.status}: {job.kind}:{job.target}")


def _get_lock() -> threading.Lock:
    global _lock
    if _lock is None:
        _lock = threading.Lock()
    return _lock


def _store(job: LifecycleJob) -> None:
    if job not in _events:
        _events.append(job)


def _chat_task_id(job: LifecycleJob) -> str:
    suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(job.target or "skill")).strip("_")
    job_suffix = re.sub(r"[^a-zA-Z0-9_.-]+", "_", str(job.id or "")).strip("_")
    return f"skill_lifecycle_{job.kind}_{suffix or 'skill'}_{job_suffix or 'job'}"


def _notify_chat_progress(job: LifecycleJob, phase: str) -> None:
    try:
        from supervisor.message_bus import send_with_budget

        detail = job.error or job.message or job.status
        lifecycle = job.to_dict()
        lifecycle["phase"] = str(phase or "")
        send_with_budget(
            0,
            f"Skill {job.kind}: `{job.target}` — {phase}{f' — {detail}' if detail else ''}",
            is_progress=True,
            task_id=_chat_task_id(job),
            progress_meta={"lifecycle": lifecycle},
        )
    except Exception:
        return


@contextlib.asynccontextmanager
async def _async_thread_lock(lock: threading.Lock):
    acquired = False
    while not acquired:
        acquired = lock.acquire(blocking=False)
        if not acquired:
            await asyncio.sleep(0.01)
    try:
        yield
    finally:
        if acquired:
            lock.release()


def _register_dedupe(job: LifecycleJob) -> None:
    if not job.dedupe_key:
        _store(job)
        return
    with _state_lock:
        existing = _dedupe_jobs.get(job.dedupe_key)
        if existing is not None and existing.status in {"queued", "running"}:
            raise DuplicateLifecycleJobError(existing)
        _dedupe_jobs[job.dedupe_key] = job
        _store(job)


def _release_dedupe(job: LifecycleJob) -> None:
    if not job.dedupe_key:
        return
    with _state_lock:
        if _dedupe_jobs.get(job.dedupe_key) is job:
            _dedupe_jobs.pop(job.dedupe_key, None)


@contextlib.contextmanager
def skill_lifecycle_file_lock(drive_root: pathlib.Path):
    from ouroboros.platform_layer import file_lock_exclusive, file_unlock

    lock_dir = pathlib.Path(drive_root) / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "skill_lifecycle.lock"
    with lock_path.open("a+") as fh:
        file_lock_exclusive(fh.fileno())
        try:
            yield
        finally:
            file_unlock(fh.fileno())


@contextlib.asynccontextmanager
async def async_skill_lifecycle_file_lock(drive_root: pathlib.Path):
    from ouroboros.platform_layer import file_lock_exclusive_nb, file_unlock

    lock_dir = pathlib.Path(drive_root) / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "skill_lifecycle.lock"
    with lock_path.open("a+") as fh:
        acquired = False
        while not acquired:
            try:
                file_lock_exclusive_nb(fh.fileno())
                acquired = True
            except OSError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            if acquired:
                file_unlock(fh.fileno())


def _notify_chat(job: LifecycleJob) -> None:
    if job.status not in {"succeeded", "failed", "cancelled"}:
        return
    phase = {
        "succeeded": "completed",
        "cancelled": "cancelled",
    }.get(job.status, "failed")
    _notify_chat_progress(job, phase)


def _job_snapshot(job: LifecycleJob) -> Dict[str, Any]:
    payload = job.to_dict()
    payload["chat_task_id"] = _chat_task_id(job)
    return payload


async def run_lifecycle_job(
    *,
    kind: str,
    target: str,
    runner: Callable[[], Awaitable[Any]],
    source: str = "",
    message: str = "",
    dedupe_key: str = "",
    options: LifecycleJobOptions | None = None,
) -> Any:
    """Run runner through the lifecycle lane with optional progress updates."""

    global _active
    opts = options or LifecycleJobOptions()
    job = LifecycleJob(
        id=f"skill-job-{uuid.uuid4().hex}",
        kind=str(kind or "operation"),
        target=str(target or "skill"),
        source=str(source or ""),
        dedupe_key=str(dedupe_key or ""),
        message=str(message or ""),
    )
    _register_dedupe(job)
    _notify_chat_progress(job, "queued")
    if opts.progress_target is not None:
        opts.progress_target.bind(job)
    result: Any = None
    error_obj: BaseException | None = None
    terminal_notified = False
    try:
        async with _async_thread_lock(_get_lock()):
            _active = job
            job.status = "running"
            job.started_at = _now_iso()
            _notify_chat_progress(job, "running")
            if opts.drive_root is None:
                from ouroboros.config import DATA_DIR

                lock_root = pathlib.Path(DATA_DIR)
            else:
                lock_root = pathlib.Path(opts.drive_root)
            try:
                async with async_skill_lifecycle_file_lock(lock_root):
                    if opts.on_started is not None:
                        opts.on_started(job)
                    result = await runner()
                error = opts.result_error(result) if opts.result_error else ""
                job.error = str(error or "")
                job.status = "failed" if job.error else "succeeded"
                if opts.result_message:
                    job.message = opts.result_message(result)
                elif not job.message:
                    job.message = job.status
                return result
            except BaseException as exc:
                error_obj = exc
                job.status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed"
                job.error = str(exc) or type(exc).__name__
                raise
            finally:
                job.finished_at = _now_iso()
                if opts.on_finished is not None:
                    try:
                        opts.on_finished(job, result, error_obj)
                    except BaseException:
                        log.exception(
                            "skill lifecycle on_finished hook failed for %s:%s",
                            job.kind,
                            job.target,
                        )
                _release_dedupe(job)
                if _active is job:
                    _active = None
                if opts.progress_target is not None:
                    opts.progress_target.release()
                _notify_chat(job)
                terminal_notified = True
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.error = job.error or "CancelledError"
        job.finished_at = job.finished_at or _now_iso()
        _release_dedupe(job)
        if _active is job:
            _active = None
        if opts.progress_target is not None:
            opts.progress_target.release()
        if not terminal_notified:
            _notify_chat(job)
        raise


async def run_blocking_preserving_cancellation(
    func: Callable[..., Any],
    *args: Any,
    log_label: str = "lifecycle work",
    **kwargs: Any,
) -> Any:
    """Keep the lifecycle lane held until non-killable thread work returns."""

    task = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    warned = False
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()
            if not warned:
                log.warning(
                    "Client task was cancelled while %s is still running; "
                    "waiting for the worker thread to finish because Python threads cannot be killed safely.",
                    log_label,
                )
                warned = True
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                while current.cancelling():  # type: ignore[attr-defined]
                    current.uncancel()  # type: ignore[attr-defined]
            # Python <=3.10: next shield await blocks until worker finishes.


def run_lifecycle_job_blocking(
    *,
    kind: str,
    target: str,
    runner: Callable[[], Any],
    source: str = "",
    message: str = "",
    dedupe_key: str = "",
    options: LifecycleJobOptions | None = None,
) -> Any:
    """Run a lifecycle job from a synchronous tool handler.

    Tool handlers already run outside the Starlette event loop. This wrapper
    gives them the same lifecycle lane, dedupe, and notifications as HTTP
    handlers without making each caller manage an event loop.
    """

    async def _runner() -> Any:
        return await run_blocking_preserving_cancellation(
            runner,
            log_label=f"blocking {kind} lifecycle operation",
        )

    async def _main() -> Any:
        return await run_lifecycle_job(
            kind=kind,
            target=target,
            source=source,
            message=message,
            dedupe_key=dedupe_key,
            runner=_runner,
            options=options,
        )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_main())

    box: Dict[str, Any] = {}

    def _thread_main() -> None:
        try:
            box["result"] = asyncio.run(_main())
        except BaseException as exc:
            box["error"] = exc

    thread = threading.Thread(target=_thread_main, name=f"skill-lifecycle-{kind}", daemon=False)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")


class JobProgressTarget:
    """Thread-safe relay for worker progress into the active lifecycle job."""

    __slots__ = ("_job", "_done")

    def __init__(self) -> None:
        self._job: Optional[LifecycleJob] = None
        self._done = False

    def bind(self, job: LifecycleJob) -> None:
        self._job = job

    def release(self) -> None:
        self._done = True

    def set(self, message: str) -> None:
        if self._done or self._job is None:
            return
        self._job.message = str(message or "")
        _notify_chat_progress(self._job, "running")


def queue_snapshot() -> Dict[str, Any]:
    """Return a JSON-friendly view of recent lifecycle activity."""

    return {
        "active": _job_snapshot(_active) if _active else None,
        "events": [_job_snapshot(job) for job in list(_events)],
    }

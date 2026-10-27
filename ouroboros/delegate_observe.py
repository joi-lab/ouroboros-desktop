"""One observation step of the supervised wait: speech from the journal, attention from the engine.

The supervising wait used to learn everything from one full run detail per three-second beat:
whether the run spoke (``lastSeq`` moved), whether it asks a question, whether it ended. On an
engine that serves the compact attention wait and the run's own event stream, the beat is two
cheap reads instead: the journal read IS the wait (it returns as events arrive, or at the
window's end, and every event it carries reaches the owner's live stream exactly as before),
then one body-free present-state read says whether a question or a terminal is due. Only then
is the run's detail read — the addressed read the attention earned — and the engine's token
acknowledged as ``seen`` after it, never before. An engine without either surface keeps the
detail beat (``tools/delegate._delegate_wait``), as do a problem row and a route that vanished
mid-call, so the old path and its semantics decide everything this step does not understand.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

# The engine's attention token this process last fetched a run's rows under, per (task, run):
# in memory, like the activity memo — a restarted host asks the engine again, harmlessly.
_SEEN: Dict[Tuple[str, str], str] = {}
_SEEN_LOCK = threading.Lock()


@dataclass
class Step:
    """Either the detail the wait must now handle, or the quiet tick's own payload (JSON)."""

    detail: Optional[Dict[str, Any]] = None
    payload: Optional[str] = None


def reset_process_seen() -> None:
    with _SEEN_LOCK:
        _SEEN.clear()


def step(ctx: Any, gateway: Any, run_id: str, *, window: float, since_seq: Optional[int],
         read_window: Callable[[], float]) -> Optional[Step]:
    """``None`` when this engine has no compact wait or no event stream: the caller reads detail."""
    from ouroboros import delegate_activity, delegate_progress
    from ouroboros.gateways import claudexor_observation as observation
    from ouroboros.gateways.claudexor import SHORT_POLL_TIMEOUT_SEC
    from ouroboros.gateways.claudexor_run_events import read_run_events, run_events_supported

    bound = min(max(0.0, float(read_window())), SHORT_POLL_TIMEOUT_SEC)
    if bound <= 0 or not observation.wait_many_supported(gateway, timeout_sec=bound) \
            or not run_events_supported(gateway, timeout_sec=bound):
        return None
    task_id = str(getattr(ctx, "task_id", "") or "")
    key = (task_id, str(run_id))
    started = time.monotonic()
    # 1. Speech: the journal read is the wait. It resumes after what this process has shown.
    after = delegate_activity.shown_cursor(ctx, task_id, run_id, since_seq)
    read = read_run_events(gateway, run_id, after_seq=after, through_seq=None, timeout_sec=max(0.0, float(window)))
    delegate_progress.emit_stream(ctx, run_id, read, after_seq=after)
    # 2. Attention: the present state, body-free. A hole raises the gateway's own typed refusal,
    # which the wait already reports as a quiet observation hole.
    with _SEEN_LOCK:
        seen = _SEEN.get(key)
    wake = observation.wait_attention(gateway, [(run_id, seen)], wait_sec=0.0, timeout_sec=bound)
    if wake.hole is not None:
        raise wake.hole
    row = wake.rows[0]
    if row.detail is not None:
        return Step(detail=row.detail)  # the route vanished: this call read the detail, the old path takes it
    if row.problem is not None or row.error is not None:
        return None  # the engine could not place the run: the addressed read decides what it is
    if wake.needs_attention(0) or read.ended:
        detail = delegate_progress.bounded_poll(gateway, run_id, read_window())
        with _SEEN_LOCK:
            _SEEN[key] = row.token  # acknowledged only once the rows it stands for were fetched
        return Step(detail=detail)
    cursor = max(int(after), int(read.through_seq or 0), int(since_seq or 0))
    payload = {"status": "progress" if read.events else "no_progress", "run_id": str(run_id),
               "state": row.status or "", "last_seq": cursor, "waited_sec": int(time.monotonic() - started),
               "waiting_on_user": bool(row.pending), "answered": True}
    return Step(payload=json.dumps(payload, ensure_ascii=False))

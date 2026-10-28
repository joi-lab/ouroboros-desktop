"""Local custody facts a process knows but could not yet write: owed, never forgotten.

A returned tool invocation, a confirmed external token, a closed model receiver
and a supervisor-confirmed worker exit are facts the process that observed them
KNOWS. When the durable write of such a fact meets a busy or failing store, the
fact waits here, compact (store addresses and stable timestamps; never results,
prompts or tool contexts), and the process's existing drivers write it again:
the server's delivery duty beside ``dialogue_retry``
(``server_maintenance``), a pool worker's idle loop (woken through its own
queue), the process's next claim write and its task-scope close. Each attempt is one ordinary bounded store
acquisition; a failed attempt ends that acquisition, never the duty. Nothing
here expires, counts or decides that work ended. Process death loses the map:
the supervisor's exit door and the startup/addressed proof
(``local_custody_repair``) then close local custody from evidence instead.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Hashable, List, Tuple

log = logging.getLogger(__name__)

_LOCK = threading.Lock()
_DRAINING = threading.Lock()
_FACTS: Dict[Hashable, Tuple[Callable[[], Any], str]] = {}
_wake: Callable[[], Any] | None = None


def bind_wake(wake: Callable[[], Any]) -> None:
    """The server binds its delivery duty's wake, a pool worker its idle wait's.

    Binding the same wake again changes nothing: a fact that keeps failing is
    retried at its driver's own cadence, not on every bind.
    """
    global _wake
    if _wake is not None and _wake == wake:
        return
    _wake = wake
    if pending():
        wake()


def owe(key: Hashable, apply: Callable[[], Any], *, label: str = "") -> None:
    """Keep one fact until ``apply`` (an idempotent write) returns without raising.

    A newer fact under the same key replaces the older one: the key names the
    row, and the newer observation carries everything the older one did.
    """
    with _LOCK:
        _FACTS[key] = (apply, label)
    if _wake is not None:
        try:
            _wake()
        except Exception:
            log.debug("Owed custody wake failed", exc_info=True)


def pending() -> int:
    with _LOCK:
        return len(_FACTS)


def labels() -> List[str]:
    """Owed facts by label (operation or consumer ids), for the pause record and reasons."""
    with _LOCK:
        return [label for _apply, label in _FACTS.values() if label]


def drain() -> int:
    """Attempt every owed fact once and forget those written; return how many remain.

    One failing root never starves another: every fact gets its attempt. The
    drain lock is nonblocking, so a producer never waits behind a store writer.
    """
    if not _DRAINING.acquire(blocking=False):
        return pending()
    try:
        with _LOCK:
            batch = list(_FACTS.items())
        for key, (apply, label) in batch:
            try:
                apply()
            except Exception:
                log.debug("Owed custody fact %s still unwritten", label or key, exc_info=True)
                continue
            with _LOCK:
                if _FACTS.get(key, (None, ""))[0] is apply:  # a newer observation stays owed
                    _FACTS.pop(key, None)
        return pending()
    finally:
        _DRAINING.release()

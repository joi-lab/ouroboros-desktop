"""Who retires or shrinks what each counted place holds, and how its last pass ended.

Dashboard -> Storage shows one line per place from ``place_removers``; the runtime ``storage`` fact and
``query_data(kind="storage")`` read the same facts. They are read fresh on every projection, with or
without a count. A place names its removers (``PLACE_REMOVERS``): the maintenance child's job rows
(``maintenance_jobs``: the row's state and, while its last run threw, ``last_error``) and, for the
engine place, the engine's last retention pass (the read-only ``GET /v2/maintenance/gc``, which never
starts one). An engine that cannot be asked is ``unavailable`` and one that keeps no such record
``unsupported``: unknown, never zero. ``not_ok`` names the outcomes the runtime fact lists.
"""
from __future__ import annotations

import pathlib
from typing import Any, Dict, List, Optional

try:  # Python 3.11+
    from typing import NotRequired, TypedDict  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - CI supports Python 3.10.
    from typing_extensions import NotRequired, TypedDict  # type: ignore[assignment]

from ouroboros import maintenance_jobs, store

# place id -> its removers: the maintenance child's jobs that retire or shrink its bytes, and ``engine``.
PLACE_REMOVERS: Dict[str, tuple] = {
    "model_calls": ("evidence_packing",),
    "engine": ("engine",),
    "task_results": ("evidence_packing", "record_compression"),
    "archives": ("evidence_packing",),
    "work_folders": ("custody_trash",),
    "app_state": ("runtime_retirement", "record_compression", "custody_trash"),
    "data_other": ("runtime_retirement",),
}
ENGINE_READ_SEC = 2.0  # a hung engine reads as unavailable; it never stalls the page or a context build
_ENGINE_OUTCOMES = ("completed", "held", "truncated", "failed")


class StorageRemover(TypedDict):
    """One remover of a place: a maintenance job's row or the engine's last retention pass."""

    id: str  # engine | runtime_retirement | custody_trash | evidence_packing | record_compression
    state: str  # a job: pending | running | done | failed | never | unsupported | unavailable; the engine: completed | held | truncated | failed | none | unavailable | unsupported
    at: Optional[str]  # the job row's last write; the engine pass's end
    last_error: NotRequired[Dict[str, str]]  # a job whose last run threw: {class, at}, until a run returns normally
    compressed: NotRequired[int]  # record_compression: files compressed so far
    saved_bytes: NotRequired[Optional[int]]  # record_compression: allocation saved; null where none is reported
    freed_bytes: NotRequired[int]  # the engine pass
    held: NotRequired[int]  # the engine pass: folders it held (project roots and native homes)
    held_since: NotRequired[str]  # the earliest of those holds
    errors: NotRequired[int]  # the engine pass's errors
    first_error: NotRequired[Optional[str]]


def place_removers(data_root: Any) -> Dict[str, List[StorageRemover]]:
    """Each place's removers as read now (module docstring); a place nothing removes is absent."""
    root = pathlib.Path(data_root)
    names = sorted({name for names in PLACE_REMOVERS.values() for name in names if name != "engine"})
    rows: Optional[Dict[str, Dict[str, Any]]] = {}  # no store file yet: no job has recorded a fact (and a
    if store.store_path(root).is_file():            # passive read never lists a folder to learn that)
        try:
            rows = maintenance_jobs.jobs(root, names)
        except Exception:  # an unreadable store: each job's outcome is unknown, never "not run"
            rows = None
    facts: Dict[str, Optional[StorageRemover]] = {name: _job(name, rows) for name in names}
    facts["engine"] = _engine(root)
    found = {place: [facts[name] for name in removers if facts[name] is not None]
             for place, removers in PLACE_REMOVERS.items()}
    return {place: removers for place, removers in found.items() if removers}  # type: ignore[misc]


def _job(name: str, rows: Optional[Dict[str, Dict[str, Any]]]) -> StorageRemover:
    if rows is None:
        return {"id": name, "state": "unavailable", "at": None}
    row = rows.get(name)
    if not row:
        return {"id": name, "state": "never", "at": None}
    fact: StorageRemover = {"id": name, "state": "unsupported" if row.get("unsupported") else str(row.get("state") or "never"),
                            "at": row.get("updated_at")}
    error = row.get("last_error")
    if isinstance(error, dict):
        fact["last_error"] = {"class": str(error.get("class") or ""), "at": str(error.get("at") or "")}
    if name == "record_compression" and "compressed" in row:
        fact["compressed"], fact["saved_bytes"] = int(row.get("compressed") or 0), row.get("saved_bytes")
    return fact


def _engine(root: pathlib.Path) -> Optional[StorageRemover]:
    """The engine's last retention pass; None where no engine was ever provisioned under ROOT."""
    from ouroboros.gateways.claudexor import ClaudexorGateway, ClaudexorUnavailable, discover_daemon_at
    from ouroboros.storage import ENGINE_DIR

    home = root / ENGINE_DIR
    if not (home / "daemon" / "control-api.json").is_file():
        return None
    try:
        with ClaudexorGateway(discover_daemon_at(home)) as gateway:
            last = gateway.maintenance_gc_last(timeout_sec=ENGINE_READ_SEC)["last"]
    except ClaudexorUnavailable as exc:
        return {"id": "engine", "state": "unsupported" if exc.code == "maintenance_unavailable" else "unavailable", "at": None}
    except Exception:
        return {"id": "engine", "state": "unavailable", "at": None}
    return {"id": "engine", "state": "none", "at": None} if last is None else _engine_pass(last)


def _engine_pass(last: Dict[str, Any]) -> StorageRemover:
    """The engine's record (its ``GcLastPass``) as one remover; a record of another shape is unavailable."""
    try:
        held, errors = last["held"], last["errors"]
        fact: StorageRemover = {
            "id": "engine", "state": str(last["outcome"]), "at": str(last["finished_at"]),
            "freed_bytes": int(last["freed_bytes"]), "held": int(held.get("roots") or 0) + int(held.get("native_homes") or 0),
            "errors": int(errors["count"]), "first_error": errors.get("first")}
        since = sorted(str(hold["since"]) for hold in last.get("holds") or () if hold.get("since"))
    except (KeyError, TypeError, ValueError, AttributeError):
        return {"id": "engine", "state": "unavailable", "at": None}
    if fact["state"] not in _ENGINE_OUTCOMES:
        return {"id": "engine", "state": "unavailable", "at": None}
    if since:
        fact["held_since"] = since[0]
    return fact


def not_ok(found: Dict[str, List[StorageRemover]]) -> List[str]:
    """One phrase per remover whose last outcome is not clean, each once though it serves several places:
    a job that failed, whose last run threw or whose facts are unreadable; an engine pass that held,
    stopped early, failed or reported errors, or an engine record that is absent or cannot be read."""
    phrases, seen = [], set()
    for removers in found.values():
        for fact in removers:
            if fact["id"] not in seen:
                seen.add(fact["id"])
                phrase = _not_ok_phrase(fact)
                if phrase:
                    phrases.append(phrase)
    return phrases


def _not_ok_phrase(fact: StorageRemover) -> str:
    state = fact["state"]
    if fact["id"] != "engine":
        error = fact.get("last_error")
        if error:
            return f"{fact['id']}: last run threw {error['class']} at {error['at']}"
        return f"{fact['id']}: {state}" if state in ("failed", "unavailable") else ""
    if state in ("none", "unavailable", "unsupported"):
        return f"engine retention: {state}"
    if state == "completed" and not fact.get("errors"):
        return ""
    detail = [f"last pass {state} at {fact['at']}"]
    if fact.get("held"):
        detail.append(f"holding {fact['held']} folders since {fact.get('held_since') or 'unknown'}")
    if fact.get("errors"):
        detail.append(f"{fact['errors']} errors, first: {fact.get('first_error')}")
    return "engine retention: " + "; ".join(detail)

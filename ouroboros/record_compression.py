"""Transparent compression of finished records: the maintenance child's ``record_compression`` job.

Finished records stay whole (PERSISTENCE ``[record]``); where the filesystem compresses transparently
their bytes can occupy less with no byte a reader sees changed. Targets:

- published evidence of terminal tasks, ``task_results/artifacts/<task>/delegated_runs/*/evidence*/**``
  and ``.../source_handles/test-evidence/**``: terminal by the predicate that moves the store's
  terminal-write counter (``task_status.SETTLED_STATUSES``), the row read again right before each replace;
- reviewer seat homes ``review_checking_prune.finished_seat_homes`` offers (copy gone, task proven over;
  this child holds no queue, so its probe is the queue snapshot's, ``task_has_live_queue_ownership``):
  their ``evidence*`` and ``.test-evidence`` folders, under the seat lock held through each replace.

Never ``area``/``area.noindex`` or a ``.git`` path; every file passes the one floor
(``artifact_storage.compression_outcome``). The job owns its interrupted temporaries: in every folder it
walks an atomic-signature name is never a candidate and is reaped (under the seat lock in a home;
``temp_sweep`` never enters ``state/review_checkouts``).

Pacing: at most ``FILES_PER_RUN`` files past the stat floor a run, a stop check between files, the cursor
durable in the job row. The first pass walks every task folder and seat home (the backlog); later passes
look at the rows written terminal since the counter the last one covered and at the seat homes changed
since it looked or still waiting for their task. Due while a pass is unfinished or new terminal writes
arrived, never by elapsed time; Linux, Docker and Colab record ``unsupported`` once.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from ouroboros import artifact_storage, maintenance_jobs, store
from ouroboros.temp_sweep import is_atomic_temp

JOB = "record_compression"
FILES_PER_RUN = 2_000  # files past the stat floor one run reads (and mostly rewrites): the rest waits a run
_SEATS = pathlib.Path("state") / "review_checkouts" / "seats"


def _counter(root: pathlib.Path) -> int:
    with store.open_store(root, write=False) as txn:
        return int(txn.meta(store.TERMINAL_WRITES) or 0)


def due(root: Any, row: Dict[str, Any], facts: Dict[str, Any], child: bool) -> bool:
    """A pass unfinished (the backlog, or a run its budget or a stop ended) or rows written terminal
    since the counter the last pass covered."""
    if not artifact_storage.compression_supported():
        return row.get("unsupported") != sys.platform
    return row.get("state") != "done" or row.get("counter") != _counter(pathlib.Path(root))


def _settled(root: pathlib.Path, task_ids: List[str]) -> Dict[str, bool]:
    """Each task's row terminal now, in one short read."""
    from ouroboros.task_status import SETTLED_STATUSES

    with store.open_store(root, write=False) as txn:
        return {task_id: txn.row(task_id, "status") in SETTLED_STATUSES for task_id in task_ids}


def _evidence_part(parts: Tuple[str, ...], directory: bool) -> bool:
    """``delegated_runs/<run>/evidence*/**`` and ``source_handles/test-evidence/**`` of a task folder."""
    depth, named = {"delegated_runs": (3, lambda: parts[2].startswith("evidence")),
                    "source_handles": (2, lambda: parts[1] == "test-evidence")}.get(parts[0], (0, None))
    return bool(depth) and (len(parts) < depth or named()) and (directory or len(parts) > depth)


def _seat_part(parts: Tuple[str, ...], directory: bool) -> bool:
    """A home's ``evidence``, ``evidence-<capture>`` and ``.test-evidence`` folders (never its copy)."""
    return (parts[0].startswith("evidence") or parts[0] == ".test-evidence") and (directory or len(parts) > 1)


def _linked(entry: os.DirEntry) -> bool:
    return entry.is_symlink() or (os.name == "nt" and bool(entry.stat(follow_symlinks=False).st_file_attributes & 0x400))


def _files(base: pathlib.Path, wanted: Callable[[Tuple[str, ...], bool], bool], after: Tuple[str, ...],
           reaped: List[int]) -> Iterator[Tuple[Tuple[str, ...], os.DirEntry]]:
    """Regular files under BASE that WANTED keeps, in the order of their parts (siblings by name, a folder
    in its place among them), strictly after AFTER; never through a link, never into ``.git``. An
    atomic-signature name on the way is reaped, never yielded."""
    def walk(directory: str, parts: Tuple[str, ...]) -> Iterator[Tuple[Tuple[str, ...], os.DirEntry]]:
        try:
            with os.scandir(directory) as listing:
                entries = sorted(listing, key=lambda entry: entry.name)
        except OSError:
            return
        for entry in entries:
            here = (*parts, entry.name)
            try:
                if entry.name == ".git" or _linked(entry):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if wanted(here, True) and (here >= after or after[:len(here)] == here):
                        yield from walk(entry.path, here)
                elif entry.is_file(follow_symlinks=False) and wanted(here, False):
                    if is_atomic_temp(entry.name):  # an interrupted compression's sibling: reaped here
                        os.unlink(entry.path)
                        reaped[0] += 1
                    elif here > after:
                        yield here, entry
            except OSError:
                continue
    yield from walk(os.fspath(base), ())


class _Run:
    """One run of the job: the planes in order, the file budget, the tally and where it stopped."""

    def __init__(self, root: pathlib.Path, row: Dict[str, Any], should_stop: Callable[[], bool]) -> None:
        self.root, self.row, self.should_stop = root, row, should_stop
        self.left = FILES_PER_RUN
        self.cursor: Optional[Dict[str, Any]] = None
        self.census = False  # whether this run read the evidence census (its position may move)
        self.reaped = [0]
        self.tally: Dict[str, Any] = {"compressed": 0, "saved_bytes": 0, "skipped": {}}

    def files(self, plane: str, key: str, base: pathlib.Path, wanted, owned=None) -> bool:
        """Compress KEY's files under BASE; False once the run must stop (its budget or a stop)."""
        resume = self.row.get("cursor") or {}
        after = tuple(resume.get("path") or ()) if (resume.get("plane"), resume.get("key")) == (plane, key) else ()
        for parts, entry in _files(base, wanted, after, self.reaped):
            if self.should_stop() or self.left <= 0:
                return False
            try:
                before = entry.stat(follow_symlinks=False)
            except OSError:
                continue  # gone since the listing
            outcome = artifact_storage.compression_outcome(pathlib.Path(entry.path), owned=owned)
            if outcome not in artifact_storage.STAT_SKIPS:
                self.left -= 1
            if outcome == "compressed":
                self.tally["compressed"] += 1
                blocks = getattr(before, "st_blocks", None)
                if blocks is None or self.tally["saved_bytes"] is None:
                    self.tally["saved_bytes"] = None  # no allocation figure on this platform: unknown, never zero
                else:
                    self.tally["saved_bytes"] += max(0, (blocks - os.lstat(entry.path).st_blocks) * 512)
            elif outcome not in artifact_storage.STAT_SKIPS:
                self.tally["skipped"][outcome] = self.tally["skipped"].get(outcome, 0) + 1
            self.cursor = {"plane": plane, "key": key, "path": list(parts)}
        return not self.should_stop()

    def evidence(self) -> Optional[List[str]]:
        """The evidence plane; the task ids still owed when the run stopped, else None."""
        artifacts, resume = self.root / "task_results" / "artifacts", self.row.get("cursor") or {}
        if resume.get("plane") == "seats":  # this pass finished the plane before it stopped
            self.cursor = dict(resume)
            return None
        self.census = True
        if self.row.get("counter") is None:  # the backlog: every task folder, by name
            try:
                with os.scandir(artifacts) as listing:
                    names = sorted(entry.name for entry in listing if entry.is_dir(follow_symlinks=False))
            except OSError:
                names = []
        else:
            from ouroboros.task_store import terminal_writes_since

            owed = list((self.row.get("owed") or {}).get("evidence") or ())
            names = owed + [task for task in terminal_writes_since(self.root, self.row["counter"]) if task not in owed]
        if resume.get("plane") == "evidence" and resume.get("key") in names:
            names = names[names.index(resume["key"]):]
        settled = _settled(self.root, names)
        for index, task_id in enumerate(names):
            if not settled.get(task_id):
                continue
            if not self.files("evidence", task_id, artifacts / task_id, _evidence_part,
                              owned=lambda task_id=task_id: _settled(self.root, [task_id])[task_id]):
                return names[index:]
        self.cursor = {"plane": "seats", "key": "", "path": []}  # the plane is done for this pass
        return None

    def seats(self) -> Tuple[Optional[List[str]], List[str]]:
        """The seat plane: ``(homes still owed when the run stopped or None, homes waiting for their task)``."""
        from ouroboros.review_checking_prune import finished_home_held, finished_seat_homes

        seats, since = self.root / _SEATS, self.row.get("since")
        waiting = set(self.row.get("waiting") or ())
        try:
            with os.scandir(seats) as listing:
                present = {entry.name: entry.stat(follow_symlinks=False).st_mtime for entry in listing
                           if entry.is_dir(follow_symlinks=False) and not entry.name.startswith(".")}
        except OSError:
            present = {}
        wanted = set((self.row.get("owed") or {}).get("seats") or ()) | waiting
        wanted = (set(present) if since is None else wanted | {name for name, at in present.items() if at >= since}) & set(present)
        resume = self.row.get("cursor") or {}  # the backlog resumes by name; a later pass owes its rest by name
        start = resume.get("key") if resume.get("plane") == "seats" and since is None else None
        names = sorted(name for name in wanted if start is None or name >= start)
        live, proven = _queue_live(self.root), {}
        verdicts = {home.name: verdict for home, verdict, _task in
                    finished_seat_homes(self.root, live, self.should_stop, proven, only=set(names))}
        waiting = {name for name in waiting if name in present}
        for index, name in enumerate(names):
            if self.should_stop():
                return names[index:], sorted(waiting)
            waiting.discard(name)
            if verdicts.get(name) == "in_use":
                waiting.add(name)
            if verdicts.get(name) != "offer":
                continue
            home = seats / name
            try:
                with finished_home_held(self.root, home, live, proven) as why:
                    if why:
                        waiting.add(name)
                        continue
                    if not self.files("seats", name, home, _seat_part):
                        return names[index:], sorted(waiting)
            except (OSError, ValueError, AttributeError):
                continue  # a marker that went unreadable: the home is no longer offered
        return None, sorted(waiting)


def _queue_live(root: pathlib.Path) -> Callable[[str], bool]:
    """The child's liveness probe: the queue snapshot's (live or unknown answers True)."""
    from ouroboros.task_status import task_has_live_queue_ownership

    return lambda task_id: task_has_live_queue_ownership(root, task_id)


def run(root: Any, should_stop: Callable[[], bool]) -> Dict[str, Any]:
    """One paced run (module docstring); its position, cursor and tally go to the job row."""
    root = pathlib.Path(root)
    row = maintenance_jobs.job(root, JOB) or {}
    if not artifact_storage.compression_supported():
        maintenance_jobs.record(root, JOB, state="done", unsupported=sys.platform)
        return {"state": "done", "items": 0}
    counter, now = _counter(root), time.time()
    began = row.get("began") or {"counter": counter, "at": now}  # a backlog pass keeps its start facts
    job = _Run(root, row, should_stop)
    owed_evidence = job.evidence()
    owed_seats, waiting = (None, list(row.get("waiting") or ())) if owed_evidence is not None else job.seats()
    stopped = owed_evidence is not None or owed_seats is not None
    backlog = {"evidence": row.get("counter") is None, "seats": row.get("since") is None}
    facts: Dict[str, Any] = {  # a backlog resumes at its cursor; a later pass owes its rest by name
        "state": "pending" if stopped else "done", "waiting": waiting, "cursor": job.cursor if stopped else None,
        "owed": {"evidence": [] if backlog["evidence"] else owed_evidence or [],
                 "seats": [] if backlog["seats"] else owed_seats or []}}
    if job.census and (owed_evidence is None or not backlog["evidence"]):  # the census position moved
        facts["counter"] = began["counter"] if backlog["evidence"] else counter
    if owed_evidence is None and (owed_seats is None or not backlog["seats"]):  # the seats were looked at
        facts["since"] = began["at"] if backlog["seats"] else now
    facts["began"] = began if stopped and (backlog["evidence"] or backlog["seats"]) else None
    tally, known = job.tally, row.get("saved_bytes", 0)
    facts.update(last={"at": now, **tally, "reaped": job.reaped[0]},
                 compressed=int(row.get("compressed") or 0) + tally["compressed"],
                 saved_bytes=None if known is None or tally["saved_bytes"] is None else known + tally["saved_bytes"])
    maintenance_jobs.record(root, JOB, **facts)
    return {"state": facts["state"], "items": tally["compressed"]}

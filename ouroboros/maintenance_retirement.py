"""History-sized filesystem retirement, owned by the maintenance child.

Prunes report their earliest retained item's age eligibility. Producers schedule
new artifacts; startup and explicit repair discover legacy ones. Directory times
and unrelated completions never wake a sweep. Age grants no deletion authority.
"""
from __future__ import annotations

import logging
import pathlib
import uuid

from ouroboros import maintenance_jobs, store
from ouroboros.retention import get_gc_retention_days, time

log = logging.getLogger(__name__)
JOB = "runtime_retirement"
STARTUP = "startup_recovery"


def record_startup(root, *, gaps):
    """Publish the recovery gate before a child may retire task sources. Missing means unknown."""
    from ouroboros.plan_collection_job import recover_startup

    with store.open_store(root, write=True) as txn:
        txn.set_meta(STARTUP, {"generation": uuid.uuid4().hex, "gaps": bool(gaps)})
        recover_startup(txn)


def _inputs(root):
    with store.open_store(root, write=False) as txn:
        return {"startup": txn.meta(STARTUP), "retention": get_gc_retention_days()}


def schedule_in(txn, eligible_at):
    """A producer's own artifact, never a polling interval or an install-wide counter."""
    row = txn.meta(maintenance_jobs.PREFIX + JOB) or {}
    due = min(row.get("next_due") or eligible_at, eligible_at)
    maintenance_jobs.record_in(txn, JOB, next_due=due, arrival=uuid.uuid4().hex)


def artifact_changed(root, *, timestamp=None, immediate=False):
    """Call after publishing disposable bytes. A missed notification waits for startup/repair."""
    try:
        stamp = time.time() if timestamp is None else float(timestamp)
        with store.acquisition_budget(0), store.open_store(root, write=True) as txn:
            schedule_in(txn, stamp + (0 if immediate else get_gc_retention_days() * 86400))
    except Exception:
        log.debug("Retirement publication awaits startup discovery", exc_info=True)


def task_written(txn, task_id, columns):
    """A task can wake retirement only if it owns physical retirement inputs."""
    from ouroboros.task_status import SETTLED_STATUSES
    if txn.root is None or columns.get("status") not in SETTLED_STATUSES:
        return
    names = (f"task_trees/{task_id}", f"services/{task_id}",
             f"state/delegate_recovery/{task_id}.json", f"state/delegate_supervision/{task_id}.json")
    deadlines = []
    for name in names:
        try:
            stamp = (txn.root / name).stat().st_mtime
            if name.startswith("task_trees/") and columns.get("drive_retention_at"):
                stamp = float(columns["drive_retention_at"])
        except (OSError, TypeError, ValueError):
            continue
        deadlines.append(stamp + get_gc_retention_days() * 86400)
    if deadlines:
        schedule_in(txn, min(deadlines))


def due(root, row, facts, child):
    return (row.get("state") == "pending" or row.get("inputs") != _inputs(root)
            or row.get("next_due") is not None and time.time() > row["next_due"])


def run(root, should_stop):
    from ouroboros.code_intelligence import prune_stale_code_intel_roots
    from ouroboros.delegate_state_sweep import sweep_settled_delegate_state
    from ouroboros.extension_reconcile_queue import prune_failed_reconcile_markers
    from ouroboros.headless import prune_task_trees
    from ouroboros.skill_uninstall_state import sweep_uninstalled_skill_state
    from ouroboros.startup_task_files import startup_tree_exclusions
    from ouroboros.tools.services import prune_service_logs
    from ouroboros.utils import append_jsonl, utc_now_iso

    inputs, reports, errors = _inputs(root), {}, []
    before = maintenance_jobs.job(root, JOB) or {}
    steps = [("delegate_state", sweep_settled_delegate_state),
             ("skill_state", sweep_uninstalled_skill_state), ("code_intel", prune_stale_code_intel_roots),
             ("failed_reconcile", prune_failed_reconcile_markers), ("agent_media", prune_agent_media_uploads)]
    # Read the durable gate again immediately before source retirement. A fresh child
    # must never substitute its empty memory for the server's recovery result.
    with store.open_store(root, write=False) as txn:
        startup = txn.meta(STARTUP)
    if isinstance(startup, dict) and startup.get("gaps") is False and not should_stop():
        exclusions = startup_tree_exclusions(root)
        if exclusions is not None:
            steps[:0] = [("task_trees", lambda root: prune_task_trees(root, exclude_root_ids=exclusions)),
                         ("services", prune_service_logs)]
    for name, retire in steps:
        if should_stop():
            break
        try:
            report = retire(root) or {}
            reports[name] = report
            if report.get("errors"):
                errors.append(name)
        except Exception:
            errors.append(name)
            log.warning("%s retirement failed; sources stay owed", name, exc_info=True)
    state = "pending" if should_stop() else "failed" if errors else "done"
    deadlines = [report["next_due"] for report in reports.values() if report.get("next_due") is not None]
    with store.open_store(root, write=True) as txn:
        current = txn.meta(maintenance_jobs.PREFIX + JOB) or {}
        if current.get("arrival") != before.get("arrival") and current.get("next_due") is not None:
            deadlines.append(current["next_due"])
        maintenance_jobs.record_in(txn, JOB, state=state, inputs=inputs, errors=errors,
                                   next_due=min(deadlines) if deadlines else None)
    if errors or any(report.get(key) for report in reports.values()
                     for key in ("removed", "swept", "restored", "deleted_dirs", "deleted_files")):
        append_jsonl(pathlib.Path(root) / "logs/events.jsonl",
                     {"ts": utc_now_iso(), "type": "runtime_retirement", "reports": reports, "errors": errors})
    return {"state": state, "items": len(reports)}


def prune_agent_media_uploads(
    drive_root: pathlib.Path,
    retention_days: "int | None" = None,
    *,
    now: "float | None" = None,
) -> dict:
    """Age-prune AGENT-generated media under uploads/ (CPL4-C21, owner 6A).

    Only ``uploads/screenshots/`` (browser tool) and ``uploads/views/``
    (view_image durable copies) follow GC retention — owner attachments live
    in the uploads/ ROOT and are owner-explicit-delete only, untouched here.
    Readers already skip missing files (the eviction placeholder only formats
    a re-view hint), so a pruned screenshot degrades to a stale hint, never
    an error. Fail-soft per file.

    CONTAINED (audit #15-13): the sweep deletes only REGULAR FILES that live
    inside the real family directory. ``is_file()``/``stat()`` follow symlinks,
    so a symlinked ``uploads/screenshots`` — or a single symlink inside it —
    made an age sweep of the drive unlink old files anywhere on the host. Both
    shapes are now skipped by ``lstat`` and counted, never followed.
    """
    import stat as stat_module

    from ouroboros.retention import age_cutoff, get_gc_retention_days, keep_until

    if retention_days is None:
        retention_days = get_gc_retention_days()
    cutoff = age_cutoff(retention_days, now)
    report: dict = {"removed": 0, "kept": 0, "skipped": 0, "errors": 0}
    for family in ("screenshots", "views"):
        family_dir = pathlib.Path(drive_root) / "uploads" / family
        try:
            if family_dir.is_symlink():
                report["skipped"] += 1  # the whole family points out of the drive
                continue
            entries = sorted(family_dir.iterdir())
        except OSError:
            continue
        for path in entries:
            try:
                info = path.lstat()
                if not stat_module.S_ISREG(info.st_mode):
                    report["skipped"] += 1  # symlink, directory or special file
                    continue
                if info.st_mtime >= cutoff:
                    keep_until(report, info.st_mtime, retention_days)
                    report["kept"] += 1
                    continue
                path.unlink()
                report["removed"] += 1
            except OSError:
                report["errors"] += 1
    return report



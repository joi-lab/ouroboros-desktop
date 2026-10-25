"""Immutable named-evidence generations for ordinary delegated runs.

The custody row publishes the latest receipt only after a complete directory rename.
Earlier receipt paths (including legacy ``evidence/``) are never rewritten. The caller
holds the snapshot's disposition lock; only its unfinished staging directories may be
removed. A failed capture is a receipt too, keeping the source and its gaps visible.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import stat
import uuid

from ouroboros import delegate_custody as custody
from ouroboros.delegate_capture_attempt import POLICY, failure_retry
from ouroboros.utils import atomic_write_json


def linked(path):
    """Include Windows junction/reparse parents, which Path.is_symlink misses on 3.10."""
    info = pathlib.Path(path).lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def private_directory(path, anchor):
    """Create below an owned root without writing through an existing link or junction."""
    path, anchor = pathlib.Path(path), pathlib.Path(anchor)
    for relative in reversed((path.relative_to(anchor), *path.relative_to(anchor).parents)):
        if str(relative) == ".":
            continue
        directory = anchor / relative
        try:
            directory.mkdir()
        except FileExistsError:
            pass
        if linked(directory) or not directory.is_dir():
            raise OSError(f"evidence parent is not a private directory: {directory}")


def saved_named_evidence(root, entry, *, observe=True):
    home = custody.delegated_capture_dir(root, entry.task_id, custody.capture_key(entry))
    name = str(entry.named_evidence.get("directory") or "evidence")
    # Only the writer's local directory name is accepted, never a stored absolute parent.
    valid_name = pathlib.Path(name).name == name and name.startswith("evidence")
    path = home / (name if valid_name else "evidence") / "summary.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8")) if valid_name else {}
    except (OSError, ValueError):
        saved = {}
    if not isinstance(saved, dict):
        saved = {}
    if observe and not saved and entry.named_evidence and entry.named_evidence.get("status") != "unreadable":
        # A consumer discovered a lost receipt, a new physical fact. It requests no
        # recapture itself; the body's ordinary due rule sees the changed custody.
        outcome = {**entry.named_evidence, "status": "unreadable"}
        custody.emit(root, custody.NAMED_EVIDENCE_RETAINED,
                     {"run_id": entry.run_id, "task_id": entry.task_id, "outcome": outcome})
        entry.named_evidence = outcome
    return path, saved


def prune_abandoned_generations(home):
    """Under the disposition lock, remove only unpublished staging; never receipt directories.

    Copies keep source modes in their manifests, not filesystem attributes, so Windows
    cleanup needs no chmod. A busy handle leaves staging for the next addressed capture.
    """
    for path in pathlib.Path(home).glob(".evidence-*.pending"):
        try:
            if linked(path):
                # A junction is a directory reparse point: unlink refuses it on Windows.
                if stat.S_ISDIR(path.lstat().st_mode):
                    path.rmdir()
                else:
                    path.unlink()
            else:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass  # A contended orphan never blocks publication of a fresh generation.


def _publish(root, entry, name, summary):
    outcome = {"directory": name, "status": summary["status"], "inputs": summary["inputs"],
               "retry": summary.get("retry", "")}
    if entry.named_evidence != outcome and not custody.emit(root, custody.NAMED_EVIDENCE_RETAINED,
            {"run_id": entry.run_id, "task_id": entry.task_id, "outcome": outcome}):
        raise OSError("named-evidence receipt publication failed")
    entry.named_evidence = outcome


def _selection(entry, area, whole):
    if not whole:
        return set()
    from ouroboros.subagent_worktrees import find_execution_snapshot
    from ouroboros.workspace_patch_capture import baseline_blob_sizes

    snapshot = find_execution_snapshot(entry.snapshot_id) or {}
    clone = pathlib.Path(str(snapshot.get("path") or area))
    prefix = pathlib.PurePosixPath(area.resolve().relative_to(clone.resolve()).as_posix())
    errors = []
    baseline = baseline_blob_sizes(clone, entry.baseline_sha, errors=errors)
    if errors:
        raise ValueError(f"evidence baseline could not be read: {errors}")
    paths = map(pathlib.PurePosixPath, set(baseline) | set(snapshot.get("file_baseline") or {}))
    return {path.relative_to(prefix).as_posix() for path in paths if path.is_relative_to(prefix)}


def retain_named(root, entry, *, whole=False, recapture=False, reread=False, publish=True):
    """Observe inputs on an addressed request; reuse partial as well as complete receipts.

    No BodyRuntime tick calls the witness walker. A new terminal observation, explicit
    recapture, or this foreground retention boundary is what observes changed sources.
    """
    from ouroboros.review_checking import named_evidence_witness, retain_named_evidence

    root = pathlib.Path(root).resolve()
    home = custody.delegated_capture_dir(root, entry.task_id, custody.capture_key(entry))
    previous, known = saved_named_evidence(root, entry, observe=publish)
    area = pathlib.Path(str(entry.execution_root or ""))
    if reread or not entry.execution_root or not area.is_dir():
        return known or {"status": "copy_missing", "files": [], "gaps": []}
    source = {"policy": POLICY, "limit_bytes": None,
              "whole": whole, "baseline_sha": entry.baseline_sha,
              "terminal_sha": entry.terminal_source.get("sha256"), "invocation_id": entry.invocation_id,
              "snapshot_id": entry.snapshot_id, "capture_id": entry.capture_id,
              "witness": named_evidence_witness(area)}
    products, error = set(), None
    try:
        products = _selection(entry, area, whole)
        source["product_selection"] = hashlib.sha256(json.dumps(sorted(products)).encode()).hexdigest()
    except Exception as exc:
        error = exc
    if not recapture and error is None and known.get("inputs") == source:
        if publish:
            _publish(root, entry, previous.parent.name, known)
        return known
    private_directory(home, root)
    prune_abandoned_generations(home)
    token = uuid.uuid4().hex
    staging = home / f".evidence-{token}.pending"
    # Keep the first legacy-shaped address usable, but never reuse any existing destination.
    name = "evidence" if not os.path.lexists(home / "evidence") else f"evidence-{token}"
    final = home / name
    staging.mkdir()
    files, gaps = [], []
    try:
        if error is not None:
            raise error
        files = retain_named_evidence(area, staging / "files", [float("inf")], gaps,
                                      product_paths=products, preserve_mode=False)
        # Links and special entries are recorded, never followed: nothing more can be
        # retained, so they do not hold the copy. Only bytes left behind do (a legacy budget).
        summary = {"status": "partial" if any(gap.get("reason") == "evidence_budget_exceeded" for gap in gaps)
                   else "retained", "files": files, "gaps": gaps}
    except Exception as exc:
        # A raised copier has no complete inventory. Keep the source, not unlisted
        # partial copies in every immutable retry generation.
        if (staging / "files").exists():
            shutil.rmtree(staging / "files", ignore_errors=True)
            if (staging / "files").exists():
                # A busy handle leaves bytes to the staging remover, never in a published receipt.
                staging = home / f".evidence-{uuid.uuid4().hex}.pending"
                staging.mkdir()
        summary = {"status": "capture_failed", "files": [], "gaps": gaps,
                   "error": f"{type(exc).__name__}: {exc}",
                   "retry": failure_retry(exc)}
    summary.update(evidence_dir=str(final), inputs=source, witness=source["witness"],
        summary_read={"root": "artifact_store", "path": f"delegated_runs/{home.name}/{name}/summary.json"})
    try:
        atomic_write_json(staging / "summary.json", summary)
        staging.rename(final)  # only a never-published, unique destination
        if publish:
            _publish(root, entry, name, summary)
        return summary
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def run_named_evidence(root, entry, *, recapture=False, publish=True):
    from ouroboros.delegate_directory import is_directory_run
    from ouroboros.delegate_registration_policy import candidate_apply_possible

    if (not (entry.snapshot_id and entry.execution_root) or is_directory_run(entry)
            or entry.authority_source == "skill_payload"):
        return {}
    return retain_named(root, entry, whole=candidate_apply_possible(entry), recapture=recapture,
                        reread=bool(entry.patch_disposed or entry.superseded_by), publish=publish)

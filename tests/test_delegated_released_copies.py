"""A writer's saved result outlives its checkout: commit C, two pins and the patch.

Once a delegated Git writer's capture row and its named evidence are saved, its result is
the snapshot commit B, the result commit C (parent B) and the B..C plumbing patch, so the
copy's checkout is released; the record and both pins stay until apply or reject, which
work from the saved patch and the pinned B. An empty result (C equals B, HEAD unmoved,
nothing retained beside it) closes itself as ``no_changes``. A continuation gets the
checkout back from C with HEAD and index at B. A copy recorded before this update keeps
its old capture and its checkout. A legacy 64 MiB-capped investigation receipt is retaken
whole once and then the copy goes. The patch never follows porcelain diff config, and a
same-size edit inside the index's timestamp tick still reaches B and C.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess

import pytest

from ouroboros import delegate_custody as custody
from ouroboros.subagent_worktrees import find_execution_snapshot
from ouroboros.tools.delegate import _capture_terminal_patch
from ouroboros.tools.delegate_integration import capture_terminal_patch_for_drive
from ouroboros.tools.subagent_integration import _integrate_delegated_patch
from tests._delegated_transport_shared import (  # noqa: F401 -- the autouse transport fixture
    _owned_gateway_uses_each_test_transport,
)
from tests.test_delegated_run_isolation import TestCaptureAndIntegrate as _CaptureFixture, _git, _isolated_entry


@pytest.fixture
def released(tmp_path, monkeypatch):
    """A captured writer whose checkout went: (target, ctx, handle, copy, entry, capture)."""
    target, ctx, handle = _CaptureFixture()._provisioned(tmp_path, monkeypatch)
    copy = pathlib.Path(handle.path)
    (copy / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    (copy / "new.txt").write_text("brand new\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    capture = _capture_terminal_patch(ctx, entry)
    assert capture["status"] == "ready_with_changes", capture
    yield target, ctx, handle, copy, entry, capture
    custody._CUSTODY.clear()


def _ref(target, ref):
    return _git(target, "rev-parse", "--verify", "--quiet", ref, check=False).stdout.strip()


def _pins(target):
    return _git(target, "for-each-ref", "--format=%(refname)", "refs/ouroboros").stdout.split()


# --------------------------------------------------------------------------- release

def test_a_saved_capture_releases_the_checkout_and_keeps_its_record_and_pins(released):
    target, ctx, handle, copy, entry, capture = released
    assert capture["checkout_released"] is True and not copy.exists()
    record = find_execution_snapshot(handle.snapshot_id)
    assert record["checkout_released"] and record["result_sha"] == capture["result_commit"]
    assert _ref(target, record["baseline_ref"]) == handle.baseline_sha
    assert _ref(target, record["result_ref"]) == record["result_sha"]
    assert _ref(target, f"{record['result_sha']}^") == handle.baseline_sha  # C's parent is B
    assert _git(target, "show", f"{record['result_sha']}:tracked.txt").stdout == "one\ntwo\nthree\n"
    assert _git(target, "worktree", "list", "--porcelain").stdout.count("worktree ") == 1  # admin entry pruned
    assert entry.patch_captured and not entry.patch_disposed  # held: the owner still decides
    assert (target / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\n" and not (target / "new.txt").exists()
    again = _capture_terminal_patch(ctx, entry)  # a reread is the saved result; nothing comes back to disk
    assert again["patch_artifact"] == capture["patch_artifact"] and not copy.exists()


@pytest.mark.parametrize("decision", ["apply", "reject"])
def test_apply_and_reject_after_release_use_the_saved_patch_and_the_pinned_baseline(released, decision):
    target, ctx, handle, copy, entry, _capture = released
    out = _integrate_delegated_patch(ctx, entry.run_id, decision, "decided")
    assert ("✅ Integrated" if decision == "apply" else "🚫 Rejected") in out, out
    assert "its private copy and pins are released" in out.lower()
    applied = decision == "apply"
    assert ((target / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n") is applied
    assert (target / "new.txt").exists() is applied
    if applied:
        assert {"new.txt", "tracked.txt"} <= set(_git(target, "diff", "--cached", "--name-only").stdout.split())
    assert entry.patch_disposed == ("applied" if applied else "rejected")
    assert find_execution_snapshot(handle.snapshot_id) is None and _pins(target) == []


def test_a_conflict_after_release_keeps_the_result_until_a_reject_releases_it(released):
    target, ctx, handle, _copy, entry, capture = released
    (target / "tracked.txt").write_text("the owner's own edit\n", encoding="utf-8")
    out = _integrate_delegated_patch(ctx, entry.run_id, "apply", "")
    assert "INTEGRATE_CONFLICT" in out and "result_commit" in out, out
    assert entry.patch_disposed == "" and pathlib.Path(capture["patch_artifact"]).is_file()
    assert sorted(_pins(target)) == sorted([find_execution_snapshot(handle.snapshot_id)["baseline_ref"],
                                           find_execution_snapshot(handle.snapshot_id)["result_ref"]])
    assert "🚫 Rejected" in _integrate_delegated_patch(ctx, entry.run_id, "reject", "keep mine")
    assert (target / "tracked.txt").read_text(encoding="utf-8") == "the owner's own edit\n" and _pins(target) == []


def test_a_capture_row_that_did_not_land_keeps_the_checkout(tmp_path, monkeypatch):
    """The in-memory capture flag follows the durable row, and only a durable row releases."""
    target, ctx, handle = _CaptureFixture()._provisioned(tmp_path, monkeypatch)
    copy = pathlib.Path(handle.path)
    (copy / "tracked.txt").write_text("one\ntwo\nkept\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    emit = custody.emit
    try:
        with monkeypatch.context() as broken:
            broken.setattr(custody, "emit", lambda drive, kind, payload: (
                False if kind == custody.PATCH_CAPTURED else emit(drive, kind, payload)))
            capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_with_changes" and entry.patch_captured is False
        assert "checkout_released" not in capture and copy.is_dir()  # the only proof of capture did not land
        healthy = _capture_terminal_patch(ctx, entry)
        assert entry.patch_captured is True and healthy["checkout_released"] is True and not copy.exists()
    finally:
        custody._CUSTODY.clear()


def test_a_lost_capture_row_heals_from_the_saved_result_when_the_checkout_is_gone(released):
    from ouroboros import subagent_worktrees

    target, ctx, handle, copy, entry, capture = released
    drive = custody.custody_root(ctx)
    rows = lambda: [row for row in custody.custody_rows(drive) if row.get("type") == custody.PATCH_CAPTURED]
    before = len(rows())
    lost = dataclasses.replace(entry, patch_captured=False)  # a replay that lost the capture row
    healed = capture_terminal_patch_for_drive(drive, lost)
    assert healed["status"] == "ready_with_changes" and healed["result_commit"] == capture["result_commit"]
    assert lost.patch_captured is True and len(rows()) == before + 1 and not copy.exists()
    # The other direction: a saved manifest that names another result is no proof; the run is "missing".
    subagent_worktrees._update_record(handle.path, lambda row: {**row, "result_sha": "0" * 40}, op="test")
    stranger = dataclasses.replace(entry, patch_captured=False)
    assert capture_terminal_patch_for_drive(drive, stranger)["status"] == "missing"
    assert stranger.patch_captured is False


# --------------------------------------------------------------------------- no changes

@pytest.mark.parametrize("case", ["nothing", "head_moved", "evidence", "pending_continuation"])
def test_an_empty_result_closes_itself_only_when_nothing_else_happened(tmp_path, monkeypatch, case):
    target, ctx, handle = _CaptureFixture()._provisioned(tmp_path, monkeypatch)
    copy, drive = pathlib.Path(handle.path), custody.custody_root(ctx)
    entry = _isolated_entry(ctx, target, handle)
    if case == "head_moved":  # a commit that changes nothing still breaks the no-commit instruction
        _git(copy, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-qm", "the run committed")
    elif case == "evidence":
        (copy / ".review-evidence").mkdir()
        (copy / ".review-evidence" / "check.log").write_text("ran the checks\n", encoding="utf-8")
    elif case == "pending_continuation":
        assert custody.emit(drive, custody.START_REQUESTED, {
            "invocation_id": "inv-next", "task_id": "t-nanny", "continuation_of": entry.run_id,
            "capture_id": "inv-next", "snapshot_id": handle.snapshot_id, "request": {"prompt": "continue"}})
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_no_changes", capture
        if case == "nothing":
            assert capture["disposition"] == entry.patch_disposed == custody.NO_CHANGES
            assert find_execution_snapshot(handle.snapshot_id) is None and not copy.exists() and _pins(target) == []
            return
        assert "disposition" not in capture and entry.patch_disposed == ""
        assert find_execution_snapshot(handle.snapshot_id) is not None
        if case == "pending_continuation":  # a starting continuation may still claim this checkout
            assert copy.is_dir() and "checkout_released" not in capture
        else:
            assert capture["checkout_released"] is True and not copy.exists()
        if case == "head_moved":
            assert capture["head_moved"]["baseline"] == handle.baseline_sha != capture["head_moved"]["current"]
        if case == "evidence":
            assert [row["path"] for row in capture["named_evidence"]["files"]] == [".review-evidence/check.log"]
    finally:
        custody._CUSTODY.clear()


# --------------------------------------------------------------------------- continuation

def test_a_continuation_restores_the_released_checkout_from_its_result(tmp_path, monkeypatch):
    from tests.test_delegate_continuation import _settle, _start

    first, _payload, ctx, _calls = _start(tmp_path, monkeypatch, acting=True, run_id="run-first")
    snapshot_root = pathlib.Path(first["execution"]["workspaceRoot"])
    (snapshot_root / "first.txt").write_text("from the first run\n", encoding="utf-8")
    _settle(tmp_path, "run-first", ctx.task_id)
    capture = capture_terminal_patch_for_drive(tmp_path, custody.replay(tmp_path)["run-first"])
    assert capture["status"] == "ready_with_changes" and capture["checkout_released"] and not snapshot_root.exists()
    pred = custody.replay(tmp_path)["run-first"]
    request, payload, _ctx, calls = _start(tmp_path, monkeypatch, acting=True, run_id="run-next",
                                           start_kwargs={"continue_from": "run-first"})
    assert payload["status"] == "started" and len(calls) == 1, payload
    assert payload["continuation"]["workspace"] == "same_snapshot" and payload["continuation"]["checkout"] == "recreated"
    assert request["execution"]["workspaceRoot"] == str(snapshot_root)
    assert "restored from the saved result" in request["prompt"] and ".review-evidence/ files are not here" in request["prompt"]
    # Files of C, HEAD and index at B: the successor's capture measures the whole work from B.
    assert (snapshot_root / "first.txt").read_text(encoding="utf-8") == "from the first run\n"
    assert _git(snapshot_root, "rev-parse", "HEAD").stdout.strip() == pred.baseline_sha
    assert "checkout_released" not in find_execution_snapshot(pred.snapshot_id)
    (snapshot_root / "second.txt").write_text("from the continuation\n", encoding="utf-8")
    _settle(tmp_path, "run-next", ctx.task_id)
    block = capture_terminal_patch_for_drive(tmp_path, custody.replay(tmp_path)["run-next"])
    assert block["status"] == "ready_with_changes" and "head_moved" not in block, block
    manifest = json.loads(pathlib.Path(block["manifest_artifact"]).read_text(encoding="utf-8"))
    assert {"first.txt", "second.txt"} <= set(manifest["untracked_included"])  # one cumulative B..C2 patch
    assert custody.replay(tmp_path)["run-first"].superseded_by == "run-next"


def test_a_released_checkout_without_a_saved_result_is_never_restored_from_b(tmp_path, monkeypatch):
    from ouroboros import subagent_worktrees
    from ouroboros.delegate_continuation import REFUSAL_SNAPSHOT_MISSING
    from tests.test_delegate_continuation import _settle, _start

    first, _payload, ctx, _calls = _start(tmp_path, monkeypatch, acting=True, run_id="run-first")
    snapshot_root = pathlib.Path(first["execution"]["workspaceRoot"])
    (snapshot_root / "first.txt").write_text("work only this checkout held\n", encoding="utf-8")
    _settle(tmp_path, "run-first", ctx.task_id)
    capture_terminal_patch_for_drive(tmp_path, custody.replay(tmp_path)["run-first"])
    pred = custody.replay(tmp_path)["run-first"]
    subagent_worktrees._update_record(str(snapshot_root), lambda row: {
        key: value for key, value in row.items() if key not in ("result_sha", "result_ref", "result_tree")}, op="test")
    request, payload, _ctx, calls = _start(tmp_path, monkeypatch, acting=True, run_id="run-next",
                                           start_kwargs={"continue_from": "run-first"})
    assert request is None and calls == [] and payload["reason"] == REFUSAL_SNAPSHOT_MISSING, payload
    assert payload["definitely_unrun"] is True and not snapshot_root.exists()
    assert not custody.replay(tmp_path)["run-first"].superseded_by and pred.patch_captured


# --------------------------------------------------------------------------- legacy shapes

def test_a_copy_recorded_before_the_update_keeps_its_old_capture_and_checkout_until_disposition(tmp_path, monkeypatch):
    from ouroboros import subagent_worktrees

    target, ctx, handle = _CaptureFixture()._provisioned(tmp_path, monkeypatch)
    subagent_worktrees._update_record(handle.path, lambda row: {**row, "file_baseline": {}}, op="test")
    copy = pathlib.Path(handle.path)
    (copy / "tracked.txt").write_text("one\ntwo\nlegacy\n", encoding="utf-8")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        manifest = json.loads(pathlib.Path(capture["manifest_artifact"]).read_text(encoding="utf-8"))
        assert capture["status"] == "ready_with_changes" and "result_commit" not in manifest  # the old capture
        assert "checkout_released" not in capture and copy.is_dir()  # kept until disposition, as before
        out = _integrate_delegated_patch(ctx, entry.run_id, "apply", "")
        assert "✅ Integrated" in out, out
        assert (target / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\nlegacy\n"
        assert not copy.exists() and find_execution_snapshot(handle.snapshot_id) is None and _pins(target) == []
    finally:
        custody._CUSTODY.clear()


def test_a_legacy_capped_investigation_receipt_is_retaken_whole_once_then_released(tmp_path, monkeypatch):
    from ouroboros import delegate_collection as body
    from ouroboros import delegate_custody_current as current
    from ouroboros import delegate_named_evidence as named
    from ouroboros.delegate_capture_attempt import collection_due, legacy_cap
    from ouroboros.tools import subagent_integration_delegated as delegated
    from tests.test_delegate_body_collection import terminal
    from tests.test_recovered_capture import EVIDENCE, prepared

    root = tmp_path / "data"
    entry, area, _source = prepared(tmp_path, root, "investigation")
    with monkeypatch.context() as crashed:  # a body that captured and stopped before retention
        crashed.setattr(delegated, "retain_investigation_evidence", lambda drive, entry, block, gateway=None, **kw: block)
        body.collect_terminal(root, entry.run_id, detail=terminal(entry.run_id))
    fresh = current.run(root, entry.run_id)
    assert fresh.patch_captured and not fresh.patch_disposed and area.is_dir()
    # The receipt an earlier release published under its 64 MiB bound: the output a gap only the copy holds.
    whole = named.retain_named(root, fresh, publish=False)
    receipt = pathlib.Path(whole["evidence_dir"])
    for kept in (receipt / "files").rglob("*"):
        if kept.is_file():
            kept.unlink()
    capped_inputs = {**whole["inputs"], "limit_bytes": 64 * 1024 * 1024}
    capped = {**whole, "status": "partial", "files": [], "inputs": capped_inputs,
              "gaps": [{"path": ".review-evidence/result.bin", "reason": "evidence_budget_exceeded"}]}
    (receipt / "summary.json").write_text(json.dumps(capped), encoding="utf-8")
    assert custody.emit(root, custody.NAMED_EVIDENCE_RETAINED, {"run_id": entry.run_id, "task_id": entry.task_id,
        "outcome": {"directory": receipt.name, "status": "partial", "inputs": capped_inputs, "retry": ""}})
    custody._CUSTODY.clear()
    legacy = current.run(root, entry.run_id)
    assert legacy_cap(legacy) and collection_due(legacy) and area.is_dir()
    # The ordinary collection owner retakes it whole, publishes the new receipt and releases the copy.
    body.collect_terminal(root, entry.run_id)
    final = current.run(root, entry.run_id)
    assert final.patch_disposed == custody.EVIDENCE_RETAINED and not area.exists()
    assert final.named_evidence["status"] == "retained" and final.named_evidence["directory"] != receipt.name
    retaken = pathlib.Path(named.saved_named_evidence(root, final)[0]).parent
    assert (retaken / "files" / ".review-evidence" / "result.bin").read_bytes() == EVIDENCE
    assert json.loads((receipt / "summary.json").read_text(encoding="utf-8"))["status"] == "partial"  # kept as written
    assert not legacy_cap(final) and not collection_due(final)  # retaken once: it rests


# --------------------------------------------------------------------------- the capture's own Git

def test_a_porcelain_diff_config_never_reshapes_the_captured_patch(tmp_path, monkeypatch):
    target, ctx, handle = _CaptureFixture()._provisioned(tmp_path, monkeypatch)
    for key, value in (("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true"), ("diff.renames", "copies"),
                       ("color.diff", "always")):
        _git(target, "config", key, value)  # the copy shares this config: it is a linked worktree
    copy = pathlib.Path(handle.path)
    (copy / "tracked.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(copy, "mv", "staged.txt", "renamed.txt")
    porcelain = _git(copy, "diff", "--no-ext-diff", "--cached", "--no-color", "HEAD").stdout
    assert "diff --git i/" in porcelain or "diff --git staged.txt" in porcelain  # the config is in effect here
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        patch = pathlib.Path(capture["patch_artifact"]).read_text(encoding="utf-8")
        assert "diff --git a/tracked.txt b/tracked.txt" in patch and "\x1b[" not in patch
        assert "rename from" not in patch and "copy from" not in patch  # plumbing, no rename detection
        assert _git(target, "apply", "--check", capture["patch_artifact"], check=False).returncode == 0
        out = _integrate_delegated_patch(ctx, entry.run_id, "apply", "")
        assert "✅ Integrated" in out, out
        assert (target / "renamed.txt").read_text(encoding="utf-8") == "staged\n" and not (target / "staged.txt").exists()
        assert (target / "tracked.txt").read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    finally:
        custody._CUSTODY.clear()


def _racy(repo: pathlib.Path, path: pathlib.Path, new: bytes) -> None:
    """Rewrite ``path`` with the same size, in place, inside the timestamp tick of its index entry and of
    the index file itself (Git then trusts its stat cache only after a content check): ``core.trustctime``
    off and one fixed mtime M make the tick exact, so no clock can blur it."""
    moment = 1_700_000_000
    _git(repo, "config", "core.trustctime", "false")
    os.utime(path, (moment, moment))
    _git(repo, "update-index", "--refresh")  # the entry now records mtime M
    with open(path, "r+b") as stream:
        assert len(new) == path.stat().st_size
        stream.write(new)
    os.utime(path, (moment, moment))
    located = pathlib.Path(_git(repo, "rev-parse", "--git-path", "index").stdout.strip())
    index = located if located.is_absolute() else repo / located
    os.utime(index, (moment, moment))
    status = subprocess.run(["git", "--no-optional-locks", "status", "--porcelain"], cwd=repo, check=True,
                            capture_output=True, text=True).stdout
    rel = path.relative_to(repo).as_posix()
    assert any(line[1] == "M" and line[3:] == rel for line in status.splitlines()), status  # its own Git sees it


def test_a_same_size_edit_in_the_index_tick_reaches_the_snapshot_commit(tmp_path, monkeypatch):
    """B side: the source's real index is copied with its mtime, so Git's racy-clean check holds."""
    from ouroboros.subagent_worktrees import provision_execution_snapshot
    from tests.test_delegated_run_isolation import _nanny_ctx, _seed_target

    target = _seed_target(tmp_path)
    _git(target, "add", "tracked.txt")  # the index holds tracked.txt's current bytes
    _racy(target, target / "tracked.txt", b"ONE\nTWO\n")
    _nanny_ctx(tmp_path, target, monkeypatch)
    handle = provision_execution_snapshot(target_root=target, task_id="t-nanny", snapshot_id="snapRacyB")
    assert _git(target, "show", f"{handle.baseline_sha}:tracked.txt").stdout == "ONE\nTWO\n"
    assert (pathlib.Path(handle.path) / "tracked.txt").read_text(encoding="utf-8") == "ONE\nTWO\n"


def test_a_same_size_edit_in_the_index_tick_is_captured_and_never_closes_as_no_changes(tmp_path, monkeypatch):
    """C side: the copy's own index is copied with its mtime, so the run's last edit is never lost
    as an empty result (which would close itself and remove the only checkout holding it)."""
    target, ctx, handle = _CaptureFixture()._provisioned(tmp_path, monkeypatch)
    copy = pathlib.Path(handle.path)
    assert (copy / "tracked.txt").read_bytes() == b"one\ntwo\n"
    _racy(copy, copy / "tracked.txt", b"ONE\nTWO\n")
    entry = _isolated_entry(ctx, target, handle)
    try:
        capture = _capture_terminal_patch(ctx, entry)
        assert capture["status"] == "ready_with_changes" and "disposition" not in capture, capture
        assert _git(target, "show", f"{capture['result_commit']}:tracked.txt").stdout == "ONE\nTWO\n"
        assert "+ONE" in pathlib.Path(capture["patch_artifact"]).read_text(encoding="utf-8")
        assert entry.patch_disposed == "" and find_execution_snapshot(handle.snapshot_id) is not None
    finally:
        custody._CUSTODY.clear()

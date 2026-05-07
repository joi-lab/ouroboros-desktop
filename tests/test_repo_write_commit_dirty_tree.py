"""Tests for the 2026-05-05 ``_repo_write_commit`` checkout-on-dirty-tree fix.

The v5.6.4 resilience patch (2026-05-04) for ``_repo_commit_push`` made
the checkout step survive failures when the agent is already on
``branch_dev`` with a dirty tree (the dirty files ARE what's being
committed). The same shape exists in ``_repo_write_commit`` (the legacy
"write one file + commit" path used by ``repo_write_commit``), but the
fix was not ported. Yesterday's audit showed 2 ``GIT_ERROR (checkout)``
events from ``repo_write_commit`` with the exact same trigger.

This file pins the ported behavior so the regression class can't reopen
on either of the two checkout sites.
"""

from __future__ import annotations

import pathlib
from types import SimpleNamespace
from unittest.mock import patch


def _ctx(tmp_path: pathlib.Path) -> SimpleNamespace:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        repo_dir=repo,
        drive_root=drive,
        branch_dev="ouroboros",
        last_push_succeeded=True,
        pending_events=[],
        pending_restart_reason=None,
        pending_restart_policy=None,
        current_task_type="task",
        repo_path=lambda rel: repo / rel,
    )


def test_write_commit_proceeds_when_already_on_branch_after_checkout_failure(tmp_path):
    """When checkout fails AND we're already on branch_dev (e.g. dirty
    tree no-op-but-complained), the write+stage cycle proceeds. Mirrors
    test_checkout_already_on_branch_proceeds_after_failure for the
    ``_repo_commit_push`` path."""
    from ouroboros.tools import git as git_module

    ctx = _ctx(tmp_path)

    def fake_run(cmd, cwd=None, **_):
        if cmd[:2] == ["git", "checkout"]:
            raise Exception("would overwrite local changes")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "ouroboros\n"  # already on branch_dev
        return ""

    cycle_called = []

    def fake_stage_cycle(ctx, msg, start, **_):
        cycle_called.append(True)
        return {"status": "passed", "message": "",
                "pre_fingerprint": {"fingerprint": "x"},
                "post_fingerprint": {"fingerprint": "x"}}

    def fake_write_text(p, content):
        # Ensure the write step is reached AFTER the resilience kicks in.
        cycle_called.append("wrote")

    with patch.object(git_module, "run_cmd", side_effect=fake_run), \
         patch.object(git_module, "_run_reviewed_stage_cycle",
                      side_effect=fake_stage_cycle), \
         patch.object(git_module, "_acquire_git_lock", return_value=None), \
         patch.object(git_module, "_release_git_lock"), \
         patch.object(git_module, "_record_commit_attempt"), \
         patch.object(git_module, "_post_commit_result"), \
         patch.object(git_module, "_invalidate_advisory"), \
         patch.object(git_module, "_auto_tag_on_version_bump", return_value={}), \
         patch.object(git_module, "_auto_push", return_value="ok"), \
         patch.object(git_module, "write_text", side_effect=fake_write_text):
        try:
            git_module._repo_write_commit(
                ctx, path="docs/foo.md", content="x",
                commit_message="test commit",
            )
        except Exception:
            pass  # other downstream failures are out of scope for this test

    assert "wrote" in cycle_called, (
        "write_text was not reached — checkout failure aborted "
        "_repo_write_commit even though we were already on branch_dev "
        "(regression of the 2026-05-05 ported fix)"
    )


def test_write_commit_aborts_when_on_different_branch_with_failure(tmp_path):
    """When checkout fails AND we're on a different branch, abort with
    the original GIT_ERROR (checkout) — preserves the legitimate
    failure path."""
    from ouroboros.tools import git as git_module

    ctx = _ctx(tmp_path)

    def fake_run(cmd, cwd=None, **_):
        if cmd[:2] == ["git", "checkout"]:
            raise Exception("would overwrite local changes")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "feature/different\n"
        return ""

    write_called = []

    def fake_write_text(p, content):
        write_called.append(True)

    with patch.object(git_module, "run_cmd", side_effect=fake_run), \
         patch.object(git_module, "_acquire_git_lock", return_value=None), \
         patch.object(git_module, "_release_git_lock"), \
         patch.object(git_module, "_record_commit_attempt"), \
         patch.object(git_module, "write_text", side_effect=fake_write_text):
        result = git_module._repo_write_commit(
            ctx, path="docs/foo.md", content="x",
            commit_message="test commit",
        )

    assert "GIT_ERROR" in result and "checkout" in result
    assert not write_called, (
        "write_text should NOT be called when on a different branch with "
        "checkout failure — that's the legitimate failure path"
    )

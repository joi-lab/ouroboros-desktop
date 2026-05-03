"""Tests for the reviewed commit workflow improvements:

1. REVIEWED_MUTATIVE_TOOLS classification
2. Reviewed mutative tool timeout handling (no ambiguous timeouts)
3. CommitAttemptRecord in review_state.py
4. Commit attempt tracking in git.py
5. Block reason classification in review.py
6. Enhanced review_status output
"""

import json
import pathlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. REVIEWED_MUTATIVE_TOOLS classification
# ---------------------------------------------------------------------------

def test_reviewed_mutative_tools_contains_commit_tools():
    from ouroboros.tool_capabilities import REVIEWED_MUTATIVE_TOOLS
    assert "repo_commit" in REVIEWED_MUTATIVE_TOOLS
    assert "repo_write_commit" in REVIEWED_MUTATIVE_TOOLS


def test_reviewed_mutative_tools_disjoint_from_parallel():
    from ouroboros.tool_capabilities import REVIEWED_MUTATIVE_TOOLS, READ_ONLY_PARALLEL_TOOLS
    assert REVIEWED_MUTATIVE_TOOLS.isdisjoint(READ_ONLY_PARALLEL_TOOLS), \
        "Reviewed mutative tools must not be in the parallel-safe set"


# ---------------------------------------------------------------------------
# 2. CommitAttemptRecord and AdvisoryReviewState
# ---------------------------------------------------------------------------

def test_commit_attempt_record_creation():
    from ouroboros.review_state import CommitAttemptRecord
    record = CommitAttemptRecord(
        ts="2026-04-02T16:00:00",
        commit_message="test commit",
        status="blocked",
        block_reason="critical_findings",
        block_details="CRITICAL: tests_affected",
        duration_sec=12.5,
        task_id="abc123",
    )
    assert record.status == "blocked"
    assert record.block_reason == "critical_findings"
    assert record.duration_sec == 12.5


def test_advisory_review_state_with_commit_attempt():
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, AdvisoryRunRecord,
    )
    state = AdvisoryReviewState()
    assert state.last_commit_attempt is None

    state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-04-02T16:00:00",
        commit_message="test",
        status="succeeded",
    )
    assert state.last_commit_attempt.status == "succeeded"


def test_commit_attempt_serialization_roundtrip(tmp_path):
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, AdvisoryRunRecord,
        load_state, save_state,
    )
    drive_root = tmp_path
    (drive_root / "state").mkdir(parents=True)

    state = AdvisoryReviewState()
    state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-04-02T16:00:00",
        commit_message="v1.0: test",
        status="blocked",
        snapshot_hash="abc123",
        block_reason="critical_findings",
        block_details="CRITICAL: something went wrong",
        duration_sec=15.3,
        task_id="task-42",
    )
    run = AdvisoryRunRecord(
        snapshot_hash="abc123",
        commit_message="v1.0: test",
        status="fresh",
        ts="2026-04-02T15:59:00",
    )
    state.add_run(run)

    save_state(drive_root, state)
    loaded = load_state(drive_root)

    assert loaded.last_commit_attempt is not None
    assert loaded.last_commit_attempt.status == "blocked"
    assert loaded.last_commit_attempt.block_reason == "critical_findings"
    assert loaded.last_commit_attempt.duration_sec == 15.3
    assert loaded.last_commit_attempt.task_id == "task-42"
    assert len(loaded.runs) == 1
    assert loaded.runs[0].status == "fresh"


def test_commit_attempt_absent_in_old_state(tmp_path):
    """Old state files without last_commit_attempt should load cleanly."""
    drive_root = tmp_path
    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "advisory_review.json"
    state_file.write_text(json.dumps({
        "runs": [],
        "saved_at": "2026-04-02T15:00:00",
    }), encoding="utf-8")

    from ouroboros.review_state import load_state
    loaded = load_state(drive_root)
    assert loaded.last_commit_attempt is None


def test_legacy_last_commit_migrates_into_attempt_ledger(tmp_path):
    """Phase 1: old state files must populate attempts[] on load."""
    drive_root = tmp_path
    state_dir = drive_root / "state"
    state_dir.mkdir(parents=True)
    state_file = state_dir / "advisory_review.json"
    state_file.write_text(json.dumps({
        "runs": [],
        "last_commit_attempt": {
            "ts": "2026-04-02T16:00:00",
            "commit_message": "legacy attempt",
            "status": "blocked",
            "block_reason": "critical_findings",
            "task_id": "legacy-task",
        },
        "saved_at": "2026-04-02T15:00:00",
    }))

    from ouroboros.review_state import load_state
    loaded = load_state(drive_root)
    assert loaded.last_commit_attempt is not None
    assert len(loaded.attempts) == 1
    assert loaded.attempts[0].status == "blocked"
    assert loaded.attempts[0].task_id == "legacy-task"


# ---------------------------------------------------------------------------
# 3. format_status_section includes commit attempt
# ---------------------------------------------------------------------------

def test_format_status_shows_blocked_commit():
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, format_status_section,
    )
    state = AdvisoryReviewState()
    state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-04-02T16:00:00",
        commit_message="v1.0: test",
        status="blocked",
        block_reason="critical_findings",
        block_details="CRITICAL: bible_compliance violated",
        duration_sec=8.2,
        readiness_warnings=["rerun advisory_pre_review before commit"],
        critical_findings=[{
            "item": "bible_compliance",
            "reason": "bible_compliance violated",
            "severity": "critical",
        }],
    )
    section = format_status_section(state)
    assert "Last commit BLOCKED" in section
    assert "critical_findings" in section
    assert "bible_compliance" in section
    assert "Readiness warnings" in section
    assert "Critical findings" in section


def test_format_status_shows_failed_commit():
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, format_status_section,
    )
    state = AdvisoryReviewState()
    state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-04-02T16:00:00",
        commit_message="v1.0: test",
        status="failed",
        block_reason="infra_failure",
        block_details="Git lock timeout: could not acquire lock",
        duration_sec=30.0,
    )
    section = format_status_section(state)
    assert "Last commit FAILED" in section
    assert "infra_failure" in section
    assert "lock" in section.lower()


def test_format_status_hides_succeeded_commit():
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, format_status_section,
    )
    state = AdvisoryReviewState()
    state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-04-02T16:00:00",
        commit_message="v1.0: test",
        status="succeeded",
    )
    section = format_status_section(state)
    # Succeeded commits should not clutter the status section
    assert "Last commit" not in section


# ---------------------------------------------------------------------------
# 4. Block reason classification in review.py
# ---------------------------------------------------------------------------

def test_block_reason_set_for_quorum_failure():
    """_run_unified_review should set _last_review_block_reason='review_quorum'
    when fewer than 2 reviewers succeed."""
    from ouroboros.tools.review import _run_unified_review

    ctx = MagicMock()
    ctx.repo_dir = "/tmp/fake"
    ctx.drive_root = "/tmp/fake_data"
    ctx._review_iteration_count = 0
    ctx._review_advisory = []
    ctx._review_history = []

    # Mock: diff exists, all models error
    with patch("ouroboros.tools.review.run_cmd") as mock_run, \
         patch("ouroboros.tools.review._cfg") as mock_cfg, \
         patch("ouroboros.tools.review._handle_multi_model_review") as mock_review:

        mock_run.side_effect = [
            "diff --git a/foo.py\n+hello",  # git diff --cached
            "foo.py",  # git diff --cached --name-only
        ]
        mock_cfg.get_review_enforcement.return_value = "blocking"
        mock_cfg.get_review_models.return_value = ["m1", "m2", "m3"]

        mock_review.return_value = json.dumps({
            "results": [
                {"model": "m1", "verdict": "ERROR", "text": "Timeout"},
                {"model": "m2", "verdict": "ERROR", "text": "Timeout"},
                {"model": "m3", "verdict": "ERROR", "text": "Timeout"},
            ]
        })

        result = _run_unified_review(ctx, "test commit")
        assert result is not None
        assert "REVIEW_BLOCKED" in result
        assert ctx._last_review_block_reason == "review_quorum"


def test_block_reason_set_for_critical_findings():
    """_run_unified_review should set _last_review_block_reason='critical_findings'."""
    from ouroboros.tools.review import _run_unified_review

    ctx = MagicMock()
    ctx.repo_dir = "/tmp/fake"
    ctx.drive_root = "/tmp/fake_data"
    ctx._review_iteration_count = 0
    ctx._review_advisory = []
    ctx._review_history = []

    with patch("ouroboros.tools.review.run_cmd") as mock_run, \
         patch("ouroboros.tools.review._cfg") as mock_cfg, \
         patch("ouroboros.tools.review._handle_multi_model_review") as mock_review, \
         patch("ouroboros.tools.review.build_touched_file_pack") as mock_pack, \
         patch("ouroboros.tools.review.build_goal_section") as mock_goal, \
         patch("ouroboros.tools.review._load_checklist_section") as mock_checklist, \
         patch("ouroboros.tools.review._load_dev_guide_text") as mock_dev:

        mock_run.side_effect = [
            "diff --git a/foo.py\n+hello",  # git diff --cached
            "foo.py",  # git diff --cached --name-only
        ]
        mock_cfg.get_review_enforcement.return_value = "blocking"
        mock_cfg.get_review_models.return_value = ["m1", "m2", "m3"]
        mock_pack.return_value = ("foo.py content", [])
        mock_goal.return_value = "## Goal\nTest"
        mock_checklist.return_value = "## Checklist\nTest"
        mock_dev.return_value = "dev guide"

        findings = json.dumps([
            {"item": "bible_compliance", "verdict": "FAIL", "severity": "critical", "reason": "P5 violated"},
        ])
        mock_review.return_value = json.dumps({
            "results": [
                {"model": "m1", "verdict": "CONCERNS", "text": findings},
                {"model": "m2", "verdict": "CONCERNS", "text": findings},
                {"model": "m3", "verdict": "CONCERNS", "text": findings},
            ]
        })

        result = _run_unified_review(ctx, "test commit")
        assert result is not None
        assert "REVIEW_BLOCKED" in result
        assert ctx._last_review_block_reason == "critical_findings"


# ---------------------------------------------------------------------------
# 5. Enhanced review_status output
# ---------------------------------------------------------------------------

def test_review_status_shows_commit_attempt():
    """review_status should include last_commit_attempt in output."""
    from ouroboros.tools.claude_advisory_review import _handle_review_status
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, AdvisoryRunRecord,
        save_state,
    )

    ctx = MagicMock()
    ctx.drive_root = "/tmp/fake_status_test"
    drive_root = pathlib.Path(ctx.drive_root)

    with patch("ouroboros.tools.claude_advisory_review.load_state") as mock_load:
        state = AdvisoryReviewState()
        state.last_commit_attempt = CommitAttemptRecord(
            ts="2026-04-02T16:00:00",
            commit_message="v1.0: test",
            status="blocked",
            block_reason="no_advisory",
            block_details="No fresh advisory run found",
            duration_sec=2.1,
        )
        mock_load.return_value = state

        result = _handle_review_status(ctx)
        data = json.loads(result)
        assert data["last_commit_attempt"] is not None
        assert data["last_commit_attempt"]["status"] == "blocked"
        assert data["last_commit_attempt"]["block_reason"] == "no_advisory"
        assert "BLOCKED" in data["message"]
        assert "no_advisory" in data["message"]


def test_review_status_actionable_message_for_each_reason():
    """Each block_reason should produce a specific actionable message."""
    from ouroboros.tools.claude_advisory_review import _handle_review_status
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord,
    )

    reasons = [
        "no_advisory", "critical_findings", "review_quorum",
        "parse_failure", "infra_failure", "scope_blocked", "preflight",
    ]

    for reason in reasons:
        ctx = MagicMock()
        ctx.drive_root = "/tmp/test"

        with patch("ouroboros.tools.claude_advisory_review.load_state") as mock_load:
            state = AdvisoryReviewState()
            state.last_commit_attempt = CommitAttemptRecord(
                ts="2026-04-02T16:00:00",
                commit_message="test",
                status="blocked",
                block_reason=reason,
            )
            mock_load.return_value = state

            result = _handle_review_status(ctx)
            data = json.loads(result)
            assert reason in data["message"], f"message should mention {reason}"


def test_review_status_filters_attempt_and_advisory_history(tmp_path):
    """Phase 1: review_status should filter history by repo/tool/task/attempt."""
    from ouroboros.tools.claude_advisory_review import _handle_review_status
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, CommitAttemptRecord

    ctx = MagicMock()
    ctx.drive_root = str(tmp_path)
    ctx.repo_dir = str(tmp_path)

    state = AdvisoryReviewState()
    state.advisory_runs = [
        AdvisoryRunRecord(
            snapshot_hash="hash-a",
            commit_message="commit a",
            status="fresh",
            ts="2026-04-02T15:00:00",
            repo_key="repo-a",
            tool_name="advisory_pre_review",
            task_id="task-a",
            attempt=1,
        ),
        AdvisoryRunRecord(
            snapshot_hash="hash-b",
            commit_message="commit b",
            status="fresh",
            ts="2026-04-02T15:05:00",
            repo_key="repo-b",
            tool_name="repo_write_commit",
            task_id="task-b",
            attempt=2,
        ),
    ]
    state.attempts = [
        CommitAttemptRecord(
            ts="2026-04-02T16:00:00",
            commit_message="attempt a",
            status="blocked",
            repo_key="repo-a",
            tool_name="repo_commit",
            task_id="task-a",
            attempt=1,
        ),
        CommitAttemptRecord(
            ts="2026-04-02T16:05:00",
            commit_message="attempt b",
            status="failed",
            repo_key="repo-b",
            tool_name="repo_write_commit",
            task_id="task-b",
            attempt=2,
            block_reason="infra_failure",
            phase="infra",
        ),
    ]
    state.last_commit_attempt = state.attempts[-1]

    with patch("ouroboros.tools.claude_advisory_review.load_state", return_value=state), \
         patch("ouroboros.tools.claude_advisory_review.compute_snapshot_hash", return_value="hash-b"):
        result = _handle_review_status(
            ctx,
            repo_key="repo-b",
            tool_name="repo_write_commit",
            task_id="task-b",
            attempt=2,
        )

    data = json.loads(result)
    assert data["filters"]["repo_key"] == "repo-b"
    assert len(data["advisory_runs"]) == 1
    assert data["advisory_runs"][0]["commit_message"] == "commit b"
    assert len(data["attempts"]) == 1
    assert data["attempts"][0]["tool_name"] == "repo_write_commit"
    assert data["last_commit_attempt"]["attempt"] == 2
    assert data["last_commit_attempt"]["repo_key"] == "repo-b"


def test_repo_commit_blocks_when_staged_diff_changes_after_review(tmp_path):
    """Phase 2: genuine staged-diff drift must still trigger revalidation failure."""
    from ouroboros.tools.git import _repo_commit_push

    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    ctx.drive_root = str(tmp_path)
    ctx.branch_dev = "ouroboros"
    ctx._scope_review_history = {}
    ctx.drive_logs.return_value = tmp_path / "logs"

    with patch("ouroboros.tools.git._check_overlapping_review_attempt", return_value=None), \
         patch("ouroboros.tools.git._record_commit_attempt") as mock_record, \
         patch("ouroboros.tools.git._acquire_git_lock", return_value=object()), \
         patch("ouroboros.tools.git._release_git_lock"), \
         patch("ouroboros.tools.git._ensure_gitignore"), \
         patch("ouroboros.tools.git._unstage_binaries", return_value=[]), \
         patch("ouroboros.tools.git._check_advisory_freshness", return_value=None), \
         patch("ouroboros.tools.git._run_parallel_review", return_value=(None, None, "", [])), \
         patch("ouroboros.tools.git._aggregate_review_verdict", return_value=(
             False,
             None,
             "",
             [],
             [],
         )), \
         patch("ouroboros.tools.git._fingerprint_staged_diff", side_effect=[
             {"ok": True, "fingerprint": "before-fp", "status": "ok", "reason": ""},
             {"ok": True, "fingerprint": "after-fp", "status": "ok", "reason": ""},
         ]), \
         patch("ouroboros.tools.git.run_cmd", side_effect=[
             "",               # git checkout
             "",               # git add -A
             "M foo.py",       # git status --porcelain
             "foo.py\n",       # git diff --cached --name-only (advisory scope)
         ]):
        result = _repo_commit_push(ctx, "test commit")

    assert "REVIEW_REVALIDATION_FAILED" in result
    last_call = mock_record.call_args_list[-1]
    assert last_call.args[2] == "blocked"
    assert last_call.kwargs["block_reason"] == "revalidation_failed"
    assert last_call.kwargs["critical_findings"] == []
    assert last_call.kwargs["fingerprint_status"] == "mismatch"


def test_repo_commit_preserves_blocked_review_findings(tmp_path):
    """Blocked triad findings must be returned directly, not masked by self-unstaging."""
    from ouroboros.tools.git import _repo_commit_push

    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    ctx.drive_root = str(tmp_path)
    ctx.branch_dev = "ouroboros"
    ctx._scope_review_history = {}
    ctx.drive_logs.return_value = tmp_path / "logs"
    ctx._last_review_critical_findings = [{
        "item": "tests_affected",
        "verdict": "FAIL",
        "severity": "critical",
        "reason": "missing tests",
    }]

    with patch("ouroboros.tools.git._check_overlapping_review_attempt", return_value=None), \
         patch("ouroboros.tools.git._record_commit_attempt") as mock_record, \
         patch("ouroboros.tools.git._acquire_git_lock", return_value=object()), \
         patch("ouroboros.tools.git._release_git_lock"), \
         patch("ouroboros.tools.git._ensure_gitignore"), \
         patch("ouroboros.tools.git._unstage_binaries", return_value=[]), \
         patch("ouroboros.tools.git._check_advisory_freshness", return_value=None), \
         patch(
             "ouroboros.tools.git._run_parallel_review",
             return_value=("REVIEW_BLOCKED: critical finding", None, "critical_findings", []),
         ), \
         patch("ouroboros.tools.git._fingerprint_staged_diff", side_effect=[
             {"ok": True, "fingerprint": "same-fp", "status": "ok", "reason": ""},
             {"ok": True, "fingerprint": "same-fp", "status": "ok", "reason": ""},
         ]), \
         patch("ouroboros.tools.git.run_cmd", side_effect=["", "", "M foo.py", ""]) as mock_run:
        result = _repo_commit_push(ctx, "test commit")

    assert "REVIEW_BLOCKED: critical finding" in result
    assert "REVIEW_REVALIDATION_FAILED" not in result
    assert mock_run.call_args_list[-1].args[0] == ["git", "reset", "HEAD"]
    last_call = mock_record.call_args_list[-1]
    assert last_call.args[2] == "blocked"
    assert last_call.kwargs["block_reason"] == "critical_findings"
    assert last_call.kwargs["critical_findings"] == ctx._last_review_critical_findings
    assert last_call.kwargs["fingerprint_status"] == "matched"


def test_repo_commit_scope_block_preserves_scope_findings(tmp_path):
    """Scope-blocked commits must surface scope findings instead of revalidation noise."""
    from types import SimpleNamespace
    from ouroboros.tools.git import _repo_commit_push

    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    ctx.drive_root = str(tmp_path)
    ctx.branch_dev = "ouroboros"
    ctx._scope_review_history = {}
    ctx.drive_logs.return_value = tmp_path / "logs"
    ctx._last_review_critical_findings = []

    scope_result = SimpleNamespace(
        blocked=True,
        block_message="⚠️ SCOPE_REVIEW_BLOCKED: missing mirrored update",
        critical_findings=[{
            "item": "forgotten_touchpoints",
            "verdict": "FAIL",
            "severity": "critical",
            "reason": "docs/ARCHITECTURE.md was not updated",
        }],
        advisory_findings=[],
    )

    with patch("ouroboros.tools.git._check_overlapping_review_attempt", return_value=None), \
         patch("ouroboros.tools.git._record_commit_attempt") as mock_record, \
         patch("ouroboros.tools.git._acquire_git_lock", return_value=object()), \
         patch("ouroboros.tools.git._release_git_lock"), \
         patch("ouroboros.tools.git._ensure_gitignore"), \
         patch("ouroboros.tools.git._unstage_binaries", return_value=[]), \
         patch("ouroboros.tools.git._check_advisory_freshness", return_value=None), \
         patch(
             "ouroboros.tools.git._run_parallel_review",
             return_value=(None, scope_result, "", []),
         ), \
         patch("ouroboros.tools.git._fingerprint_staged_diff", side_effect=[
             {"ok": True, "fingerprint": "same-fp", "status": "ok", "reason": ""},
             {"ok": True, "fingerprint": "same-fp", "status": "ok", "reason": ""},
         ]), \
         patch("ouroboros.tools.git.run_cmd", side_effect=["", "", "M foo.py", ""]):
        result = _repo_commit_push(ctx, "test commit")

    assert "SCOPE_REVIEW_BLOCKED" in result
    assert "REVIEW_REVALIDATION_FAILED" not in result
    last_call = mock_record.call_args_list[-1]
    assert last_call.kwargs["block_reason"] == "scope_blocked"
    assert last_call.kwargs["critical_findings"] == scope_result.critical_findings
    assert last_call.kwargs["fingerprint_status"] == "matched"


def test_repo_write_commit_preserves_blocked_review_findings(tmp_path):
    """repo_write_commit must return genuine blocked findings before unstaging."""
    from ouroboros.tools.git import _repo_write_commit

    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    ctx.drive_root = str(tmp_path)
    ctx.branch_dev = "ouroboros"
    ctx._scope_review_history = {}
    ctx.drive_logs.return_value = tmp_path / "logs"
    ctx.repo_path.side_effect = lambda rel: tmp_path / rel
    ctx._last_review_critical_findings = [{
        "item": "self_consistency",
        "verdict": "FAIL",
        "severity": "critical",
        "reason": "workflow docs are stale",
    }]

    with patch("ouroboros.tools.git._check_overlapping_review_attempt", return_value=None), \
         patch("ouroboros.tools.git._acquire_git_lock", return_value=object()), \
         patch("ouroboros.tools.git._release_git_lock"), \
         patch("ouroboros.tools.git._invalidate_advisory"), \
         patch(
             "ouroboros.tools.git._run_reviewed_stage_cycle",
             return_value={
                 "status": "blocked",
                 "message": "REVIEW_BLOCKED: fix the docs first",
                 "block_reason": "critical_findings",
             },
         ) as mock_stage_cycle, \
         patch("ouroboros.tools.git.write_text"), \
         patch("ouroboros.tools.git.run_cmd", side_effect=[""]) as mock_run:
        result = _repo_write_commit(ctx, "foo.py", "x = 1\n", "test commit")

    assert "REVIEW_BLOCKED: fix the docs first" in result
    assert mock_stage_cycle.call_args.kwargs["paths"] == ["foo.py"]
    assert mock_run.call_args_list[0].args[0] == ["git", "checkout", ctx.branch_dev, "--"]


def test_repo_commit_blocks_when_fingerprint_unavailable(tmp_path):
    """Phase 2: reviewed commit must fail closed when staged diff fingerprinting fails."""
    from ouroboros.tools.git import _repo_commit_push

    ctx = MagicMock()
    ctx.repo_dir = str(tmp_path)
    ctx.drive_root = str(tmp_path)
    ctx.branch_dev = "ouroboros"
    ctx._scope_review_history = {}
    ctx.drive_logs.return_value = tmp_path / "logs"

    with patch("ouroboros.tools.git._check_overlapping_review_attempt", return_value=None), \
         patch("ouroboros.tools.git._record_commit_attempt") as mock_record, \
         patch("ouroboros.tools.git._acquire_git_lock", return_value=object()), \
         patch("ouroboros.tools.git._release_git_lock"), \
         patch("ouroboros.tools.git._ensure_gitignore"), \
         patch("ouroboros.tools.git._unstage_binaries", return_value=[]), \
         patch("ouroboros.tools.git._check_advisory_freshness", return_value=None), \
         patch("ouroboros.tools.git._fingerprint_staged_diff", return_value={
             "ok": False,
             "fingerprint": "",
             "status": "unavailable",
             "reason": "git diff --cached failed",
         }), \
         patch("ouroboros.tools.git.run_cmd", side_effect=[
             "",               # git checkout
             "",               # git add -A
             "M foo.py",       # git status --porcelain
             "foo.py\n",       # git diff --cached --name-only (advisory scope)
         ]):
        result = _repo_commit_push(ctx, "test commit")

    assert "REVIEW_REVALIDATION_FAILED" in result
    last_call = mock_record.call_args_list[-1]
    assert last_call.kwargs["block_reason"] == "fingerprint_unavailable"
    assert last_call.kwargs["fingerprint_status"] == "unavailable"


def test_late_result_pending_is_persisted_and_cleared(tmp_path):
    """Phase 2: soft-timeout state must persist as late_result_pending until final result arrives."""
    from ouroboros.tools.commit_gate import _mark_review_attempt_late, _record_commit_attempt
    from ouroboros.review_state import load_state

    ctx = MagicMock()
    ctx.drive_root = str(tmp_path)
    ctx.repo_dir = str(tmp_path)
    ctx.task_id = "task-late"
    ctx._current_review_tool_name = "repo_commit"
    ctx._current_review_commit_message = "late commit"

    _record_commit_attempt(ctx, "late commit", "reviewing")
    _mark_review_attempt_late(ctx, soft_timeout_sec=30, duration_sec=31.5)

    state = load_state(tmp_path)
    assert state.last_commit_attempt is not None
    assert len(state.attempts) == 1
    assert state.attempts[0].attempt == 1
    assert state.last_commit_attempt.status == "reviewing"
    assert state.last_commit_attempt.late_result_pending is True
    assert state.last_commit_attempt.phase == "late_wait"
    started_ts = state.attempts[0].started_ts

    _record_commit_attempt(ctx, "late commit", "succeeded", late_result_pending=False)
    updated = load_state(tmp_path)
    assert updated.last_commit_attempt is not None
    assert len(updated.attempts) == 1
    assert updated.attempts[0].attempt == 1
    assert updated.last_commit_attempt.status == "succeeded"
    assert updated.last_commit_attempt.late_result_pending is False
    assert updated.attempts[0].started_ts == started_ts


def test_repeated_reviewing_updates_reuse_same_attempt_and_leave_no_ghost_active_attempt(tmp_path):
    """Phase 2 regression: one logical reviewed flow must not leave sibling active attempts."""
    from ouroboros.tools.commit_gate import _check_overlapping_review_attempt, _record_commit_attempt
    from ouroboros.review_state import load_state

    ctx = MagicMock()
    ctx.drive_root = str(tmp_path)
    ctx.repo_dir = str(tmp_path)
    ctx.task_id = "task-flow"
    ctx._current_review_tool_name = "repo_commit"
    ctx._current_review_commit_message = "flow commit"

    _record_commit_attempt(ctx, "flow commit", "reviewing")
    state = load_state(tmp_path)
    assert len(state.attempts) == 1
    assert ctx._current_review_attempt_number == 1
    started_ts = state.attempts[0].started_ts

    _record_commit_attempt(
        ctx,
        "flow commit",
        "reviewing",
        phase="review",
        pre_review_fingerprint="abc123",
        fingerprint_status="pending",
    )
    _record_commit_attempt(
        ctx,
        "flow commit",
        "blocked",
        block_reason="critical_findings",
        block_details="Critical review findings present",
    )

    updated = load_state(tmp_path)
    assert len(updated.attempts) == 1
    attempt = updated.attempts[0]
    assert attempt.attempt == 1
    assert attempt.status == "blocked"
    assert attempt.started_ts == started_ts
    assert updated.get_active_attempts() == []

    fresh_ctx = MagicMock()
    fresh_ctx.drive_root = str(tmp_path)
    fresh_ctx.repo_dir = str(tmp_path)
    fresh_ctx.task_id = "task-next"
    fresh_ctx._current_review_tool_name = "repo_commit"

    msg = _check_overlapping_review_attempt(fresh_ctx)
    assert msg is None


def test_overlap_guard_blocks_active_reviewed_attempt(tmp_path):
    """Phase 2: new reviewed attempts must not overlap an active reviewing attempt."""
    from ouroboros.tools.commit_gate import _check_overlapping_review_attempt, _record_commit_attempt

    first_ctx = MagicMock()
    first_ctx.drive_root = str(tmp_path)
    first_ctx.repo_dir = str(tmp_path)
    first_ctx.task_id = "task-1"
    first_ctx._current_review_tool_name = "repo_commit"
    _record_commit_attempt(first_ctx, "first", "reviewing")

    second_ctx = MagicMock()
    second_ctx.drive_root = str(tmp_path)
    second_ctx.repo_dir = str(tmp_path)
    second_ctx.task_id = "task-2"
    second_ctx._current_review_tool_name = "repo_commit"

    msg = _check_overlapping_review_attempt(second_ctx)
    assert msg is not None
    assert "REVIEWED_ATTEMPT_IN_PROGRESS" in msg


def test_overlap_guard_auto_expires_stale_attempt_at_exact_ttl_boundary(tmp_path):
    """Phase 2: overlap guard should expire stale attempts at the exact TTL+grace boundary."""
    from ouroboros.tools.commit_gate import _check_overlapping_review_attempt, _record_commit_attempt
    from ouroboros.review_state import (
        _REVIEW_ATTEMPT_GRACE_SEC,
        _REVIEW_ATTEMPT_TTL_SEC,
        load_state,
        update_state,
    )

    ctx = MagicMock()
    ctx.drive_root = str(tmp_path)
    ctx.repo_dir = str(tmp_path)
    ctx.task_id = "task-stale"
    ctx._current_review_tool_name = "repo_commit"
    _record_commit_attempt(ctx, "stale", "reviewing")

    old_ts = "2026-01-01T00:00:00+00:00"

    def _mutate(state):
        for attempt in state.attempts:
            attempt.ts = old_ts
            attempt.started_ts = old_ts
            attempt.updated_ts = old_ts
        if state.last_commit_attempt is not None:
            state.last_commit_attempt.ts = old_ts
            state.last_commit_attempt.started_ts = old_ts
            state.last_commit_attempt.updated_ts = old_ts

    update_state(tmp_path, _mutate)

    fresh_ctx = MagicMock()
    fresh_ctx.drive_root = str(tmp_path)
    fresh_ctx.repo_dir = str(tmp_path)
    fresh_ctx.task_id = "task-fresh"
    fresh_ctx._current_review_tool_name = "repo_commit"

    with patch("ouroboros.review_state._utc_now", return_value="2026-01-01T00:32:00+00:00"):
        msg = _check_overlapping_review_attempt(fresh_ctx)
    assert msg is None

    updated = load_state(tmp_path)
    assert updated.last_commit_attempt is not None
    assert updated.last_commit_attempt.status == "failed"
    assert updated.last_commit_attempt.phase == "expired"
    assert updated.last_commit_attempt.finished_ts == "2026-01-01T00:32:00+00:00"


# ---------------------------------------------------------------------------
# 8. Review continuation persistence
# ---------------------------------------------------------------------------

def test_blocked_attempt_persists_review_continuation(tmp_path):
    from ouroboros.task_continuation import load_review_continuation
    from ouroboros.tools.commit_gate import _record_commit_attempt

    ctx = MagicMock()
    ctx.drive_root = str(tmp_path)
    ctx.repo_dir = str(tmp_path)
    ctx.task_id = "task-blocked"
    ctx._current_review_tool_name = "repo_commit"
    ctx.current_task_type = "task"

    _record_commit_attempt(
        ctx,
        "blocked commit",
        "blocked",
        block_reason="critical_findings",
        block_details="Critical review findings present",
        critical_findings=[{"severity": "critical", "reason": "fix tests"}],
        readiness_warnings=["Needs follow-up"],
    )

    continuation = load_review_continuation(tmp_path, "task-blocked")
    assert continuation is not None
    assert continuation.source == "blocked_review"
    assert continuation.stage == "blocking_review"
    assert continuation.block_reason == "critical_findings"
    assert continuation.tool_name == "repo_commit"
    assert continuation.critical_findings[0]["reason"] == "fix tests"
    assert "Needs follow-up" in continuation.warnings


def test_success_keeps_other_task_review_continuations_in_same_scope(tmp_path):
    from ouroboros.task_continuation import list_review_continuations
    from ouroboros.tools.commit_gate import _record_commit_attempt

    blocked_ctx = MagicMock()
    blocked_ctx.drive_root = str(tmp_path)
    blocked_ctx.repo_dir = str(tmp_path)
    blocked_ctx.task_id = "task-old"
    blocked_ctx._current_review_tool_name = "repo_commit"
    blocked_ctx.current_task_type = "task"
    _record_commit_attempt(
        blocked_ctx,
        "blocked commit",
        "blocked",
        block_reason="critical_findings",
        critical_findings=[{"severity": "critical", "reason": "old finding"}],
    )

    resumed_ctx = MagicMock()
    resumed_ctx.drive_root = str(tmp_path)
    resumed_ctx.repo_dir = str(tmp_path)
    resumed_ctx.task_id = "task-new"
    resumed_ctx._current_review_tool_name = "repo_commit"
    resumed_ctx.current_task_type = "task"
    _record_commit_attempt(resumed_ctx, "fixed commit", "reviewing")
    _record_commit_attempt(resumed_ctx, "fixed commit", "succeeded")

    continuations, corrupt = list_review_continuations(tmp_path)
    assert corrupt == []
    assert len(continuations) == 1
    assert continuations[0].task_id == "task-old"


def test_blocked_attempt_does_not_clear_other_task_continuations_same_scope(tmp_path):
    from ouroboros.task_continuation import list_review_continuations
    from ouroboros.tools.commit_gate import _record_commit_attempt

    first_ctx = MagicMock()
    first_ctx.drive_root = str(tmp_path)
    first_ctx.repo_dir = str(tmp_path)
    first_ctx.task_id = "task-old"
    first_ctx._current_review_tool_name = "repo_commit"
    first_ctx.current_task_type = "task"
    _record_commit_attempt(
        first_ctx,
        "blocked old",
        "blocked",
        block_reason="critical_findings",
        critical_findings=[{"severity": "critical", "reason": "first finding"}],
    )

    second_ctx = MagicMock()
    second_ctx.drive_root = str(tmp_path)
    second_ctx.repo_dir = str(tmp_path)
    second_ctx.task_id = "task-new"
    second_ctx._current_review_tool_name = "repo_commit"
    second_ctx.current_task_type = "task"
    _record_commit_attempt(
        second_ctx,
        "blocked new",
        "blocked",
        block_reason="critical_findings",
        critical_findings=[{"severity": "critical", "reason": "second finding"}],
    )

    continuations, corrupt = list_review_continuations(tmp_path)
    assert corrupt == []
    assert {item.task_id for item in continuations} == {"task-old", "task-new"}


def test_capture_review_continuation_from_state_preserves_outage_warning(tmp_path):
    from ouroboros.task_continuation import capture_review_continuation_from_state, load_review_continuation
    from ouroboros.tools.commit_gate import _record_commit_attempt

    ctx = MagicMock()
    ctx.drive_root = str(tmp_path)
    ctx.repo_dir = str(tmp_path)
    ctx.task_id = "task-outage"
    ctx._current_review_tool_name = "repo_commit"
    ctx.current_task_type = "task"
    _record_commit_attempt(
        ctx,
        "blocked commit",
        "blocked",
        block_reason="critical_findings",
        critical_findings=[{"severity": "critical", "reason": "persist me"}],
    )

    continuation = capture_review_continuation_from_state(
        tmp_path,
        {"id": "task-outage", "type": "task"},
        source="task_exception",
        warning="Provider outage",
    )

    assert continuation is not None
    reloaded = load_review_continuation(tmp_path, "task-outage")
    assert reloaded is not None
    assert reloaded.source == "task_exception"
    assert "Provider outage" in reloaded.warnings


def test_capture_review_continuation_from_state_persists_warning_without_review_attempt(tmp_path):
    from ouroboros.task_continuation import capture_review_continuation_from_state, load_review_continuation

    continuation = capture_review_continuation_from_state(
        tmp_path,
        {"id": "task-pre-review", "type": "task"},
        source="task_exception",
        warning="Provider outage",
    )

    assert continuation is not None
    reloaded = load_review_continuation(tmp_path, "task-pre-review")
    assert reloaded is not None
    assert reloaded.source == "task_exception"
    assert reloaded.stage == "task_exception"
    assert reloaded.attempt == 0
    assert reloaded.readiness_warnings == ["Provider outage"]
    assert reloaded.warnings == ["Provider outage"]


def test_capture_review_continuation_from_state_scopes_open_obligations_to_repo(tmp_path):
    from ouroboros.task_continuation import capture_review_continuation_from_state, load_review_continuation
    from ouroboros.tools.commit_gate import _record_commit_attempt

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)

    own_ctx = MagicMock()
    own_ctx.drive_root = str(tmp_path)
    own_ctx.repo_dir = str(repo_a)
    own_ctx.task_id = "task-outage"
    own_ctx._current_review_tool_name = "repo_commit"
    own_ctx.current_task_type = "task"
    _record_commit_attempt(
        own_ctx,
        "repo-a blocked",
        "blocked",
        block_reason="critical_findings",
        critical_findings=[{
            "severity": "critical",
            "verdict": "FAIL",
            "item": "own_issue",
            "reason": "keep me",
        }],
    )

    foreign_ctx = MagicMock()
    foreign_ctx.drive_root = str(tmp_path)
    foreign_ctx.repo_dir = str(repo_b)
    foreign_ctx.task_id = "task-foreign"
    foreign_ctx._current_review_tool_name = "repo_commit"
    foreign_ctx.current_task_type = "task"
    _record_commit_attempt(
        foreign_ctx,
        "repo-b blocked",
        "blocked",
        block_reason="critical_findings",
        critical_findings=[{
            "severity": "critical",
            "verdict": "FAIL",
            "item": "foreign_issue",
            "reason": "drop me",
        }],
    )

    continuation = capture_review_continuation_from_state(
        tmp_path,
        {"id": "task-outage", "type": "task"},
        source="task_exception",
        warning="Provider outage",
        repo_dir=repo_a,
    )

    assert continuation is not None
    reloaded = load_review_continuation(tmp_path, "task-outage")
    assert reloaded is not None
    items = [entry["item"] for entry in reloaded.open_obligations]
    assert "own_issue" in items
    assert "foreign_issue" not in items


def test_save_review_continuation_quarantines_corrupt_existing_file(tmp_path):
    from ouroboros.task_continuation import (
        ReviewContinuation,
        continuation_path,
        list_review_continuations,
        load_review_continuation,
        save_review_continuation,
    )

    broken_path = continuation_path(tmp_path, "task-corrupt")
    broken_path.write_text("{not valid json", encoding="utf-8")

    save_review_continuation(
        tmp_path,
        ReviewContinuation(
            task_id="task-corrupt",
            source="blocked_review",
            stage="blocking_review",
            repo_key="repo-self",
            tool_name="repo_commit",
            block_reason="critical_findings",
        ),
        expect_task_id="task-corrupt",
    )

    restored = load_review_continuation(tmp_path, "task-corrupt")
    assert restored is not None
    assert restored.source == "blocked_review"

    continuations, corrupt = list_review_continuations(tmp_path)
    assert {item.task_id for item in continuations} == {"task-corrupt"}
    assert any("quarantined corrupt continuation" in item for item in corrupt)


def test_format_commit_result_renders_structured_advisory_entries():
    from types import SimpleNamespace
    from ouroboros.tools.git import _format_commit_result

    ctx = SimpleNamespace(
        branch_dev="ouroboros",
        _review_advisory=[{
            "severity": "advisory",
            "tag": "scope",
            "item": "architecture_fit",
            "reason": "minor concern",
        }],
    )

    result = _format_commit_result(ctx, "test commit", "", "")
    assert "Advisory warnings" in result
    assert "[ADVISORY] [scope] architecture_fit: minor concern" in result


# ---------------------------------------------------------------------------
# 9. Reviewed mutative tool timeout handling
# ---------------------------------------------------------------------------

def test_reviewed_mutative_hard_ceiling_constant():
    from ouroboros.loop_tool_execution import _REVIEWED_MUTATIVE_HARD_CEILING
    assert _REVIEWED_MUTATIVE_HARD_CEILING >= 600, "Hard ceiling must be substantial"
    assert _REVIEWED_MUTATIVE_HARD_CEILING <= 3600, "Hard ceiling shouldn't be infinite"


def test_reviewed_mutative_import_in_loop():
    """REVIEWED_MUTATIVE_TOOLS should be importable from loop_tool_execution."""
    from ouroboros.loop_tool_execution import REVIEWED_MUTATIVE_TOOLS
    assert "repo_commit" in REVIEWED_MUTATIVE_TOOLS


# ---------------------------------------------------------------------------
# 10. Snapshot hash path scoping (verify existing behavior)
# ---------------------------------------------------------------------------

def test_snapshot_hash_path_scoping(tmp_path):
    """compute_snapshot_hash with paths= should only consider those paths."""
    from ouroboros.review_state import compute_snapshot_hash

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()

    # Create two files
    (repo / "a.py").write_text("aaa", encoding="utf-8")
    (repo / "b.py").write_text("bbb", encoding="utf-8")

    hash_a = compute_snapshot_hash(repo, paths=["a.py"])
    hash_b = compute_snapshot_hash(repo, paths=["b.py"])
    hash_ab = compute_snapshot_hash(repo, paths=["a.py", "b.py"])

    assert hash_a != hash_b, "Different paths should produce different hashes"
    assert hash_a != hash_ab
    assert hash_b != hash_ab

    # Same paths, same content → same hash
    hash_a2 = compute_snapshot_hash(repo, paths=["a.py"])
    assert hash_a == hash_a2


def test_snapshot_hash_ignores_commit_message(tmp_path):
    """commit_message should NOT affect the hash (decoupled per design)."""
    from ouroboros.review_state import compute_snapshot_hash

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "a.py").write_text("content", encoding="utf-8")

    h1 = compute_snapshot_hash(repo, commit_message="msg1", paths=["a.py"])
    h2 = compute_snapshot_hash(repo, commit_message="msg2", paths=["a.py"])
    assert h1 == h2


def test_update_state_serializes_concurrent_writers(tmp_path):
    """Phase 1: lockfile-backed update_state should preserve all concurrent writes."""
    from ouroboros.review_state import CommitAttemptRecord, load_state, update_state

    (tmp_path / "state").mkdir(parents=True)

    def _writer(idx: int) -> None:
        def _mutate(state):
            state.record_attempt(CommitAttemptRecord(
                ts=f"2026-04-02T16:00:{idx:02d}",
                commit_message=f"commit {idx}",
                status="failed",
                repo_key="repo-self",
                tool_name="repo_commit",
                task_id="task-concurrent",
                attempt=idx + 1,
                block_reason="infra_failure",
            ))
        update_state(tmp_path, _mutate)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_writer, range(8)))

    loaded = load_state(tmp_path)
    assert len(loaded.attempts) == 8
    raw = (tmp_path / "state" / "advisory_review.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert len(parsed["attempts"]) == 8
    assert not (tmp_path / "locks" / "advisory_review.lock").exists()


def test_acquire_review_state_lock_retries_permission_error_contention(tmp_path, monkeypatch):
    """Windows may raise PermissionError instead of FileExistsError for held O_EXCL locks."""
    import os as _os

    from ouroboros.review_state import acquire_review_state_lock, release_review_state_lock

    lock_path = tmp_path / "locks" / "advisory_review.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("held", encoding="utf-8")

    real_open = _os.open
    attempts = {"count": 0}

    def fake_open(path, flags, mode=0o777):
        if pathlib.Path(path) == lock_path and attempts["count"] == 0:
            attempts["count"] += 1
            raise PermissionError(13, "Permission denied", str(path))
        return real_open(path, flags, mode)

    def fake_sleep(_seconds: float) -> None:
        if lock_path.exists():
            lock_path.unlink()

    monkeypatch.setattr("ouroboros.review_state.os.open", fake_open)
    monkeypatch.setattr("ouroboros.review_state.time.sleep", fake_sleep)

    lock_fd = acquire_review_state_lock(tmp_path, timeout_sec=0.5, stale_sec=90.0)
    assert lock_fd is not None
    try:
        assert lock_path.exists()
    finally:
        release_review_state_lock(tmp_path, lock_fd)
    assert attempts["count"] == 1
    assert not lock_path.exists()


# ---------------------------------------------------------------------------
# 11. Startup reconciliation of stale 'reviewing' state
# ---------------------------------------------------------------------------

def test_startup_reconciles_only_stale_reviewing_attempts(tmp_path):
    """verify_system_state should auto-expire only attempts beyond TTL+grace."""
    from ouroboros.agent_startup_checks import verify_system_state
    from ouroboros.review_state import AdvisoryReviewState, CommitAttemptRecord, load_state, save_state

    class FakeEnv:
        drive_root = str(tmp_path)
        repo_dir = tmp_path

        def drive_path(self, rel: str = ""):
            return tmp_path / rel if rel else tmp_path

        def repo_path(self, rel: str):
            return tmp_path / rel

    (tmp_path / "logs").mkdir()
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "identity.md").write_text("identity", encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text("", encoding="utf-8")
    (tmp_path / "memory" / "WORLD.md").write_text("", encoding="utf-8")

    state = AdvisoryReviewState()
    state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-01-01T00:00:00+00:00",
        started_ts="2026-01-01T00:00:00+00:00",
        commit_message="stuck commit",
        status="reviewing",
        tool_name="repo_commit",
        repo_key="repo-self",
        task_id="task-startup",
        attempt=1,
    )
    state.attempts = [state.last_commit_attempt]
    save_state(tmp_path, state)

    with patch("ouroboros.agent_startup_checks.check_uncommitted_changes", return_value=({"status": "ok"}, 0)), \
         patch("ouroboros.agent_startup_checks.check_version_sync", return_value=({"status": "ok"}, 0)), \
         patch("ouroboros.agent_startup_checks.check_budget", return_value=({"status": "ok"}, 0)), \
         patch("ouroboros.review_state._utc_now", return_value="2026-01-01T00:32:10+00:00"):
        verify_system_state(FakeEnv(), "gitsha")

    reconciled = load_state(tmp_path)
    assert reconciled.last_commit_attempt.status == "failed"
    assert reconciled.last_commit_attempt.phase == "expired"


def test_startup_does_not_touch_terminal_states(tmp_path):
    """verify_system_state must NOT change already-terminal states or fresh reviewing ones."""
    from ouroboros.agent_startup_checks import verify_system_state
    from ouroboros.review_state import AdvisoryReviewState, CommitAttemptRecord, save_state, load_state

    class FakeEnv:
        drive_root = str(tmp_path)
        repo_dir = tmp_path

        def drive_path(self, rel: str = ""):
            return tmp_path / rel if rel else tmp_path

        def repo_path(self, rel: str):
            return tmp_path / rel

    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "memory").mkdir(exist_ok=True)
    (tmp_path / "memory" / "identity.md").write_text("identity", encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text("", encoding="utf-8")
    (tmp_path / "memory" / "WORLD.md").write_text("", encoding="utf-8")

    for terminal_status in ("succeeded", "failed", "blocked"):
        state = AdvisoryReviewState()
        state.last_commit_attempt = CommitAttemptRecord(
            ts="2026-01-01T00:00:00Z", commit_message="done",
            status=terminal_status,
        )
        state.attempts = [state.last_commit_attempt]
        save_state(tmp_path, state)

        with patch("ouroboros.agent_startup_checks.check_uncommitted_changes", return_value=({"status": "ok"}, 0)), \
             patch("ouroboros.agent_startup_checks.check_version_sync", return_value=({"status": "ok"}, 0)), \
             patch("ouroboros.agent_startup_checks.check_budget", return_value=({"status": "ok"}, 0)):
            verify_system_state(FakeEnv(), "gitsha")

        after = load_state(tmp_path)
        assert after.last_commit_attempt.status == terminal_status

    fresh_state = AdvisoryReviewState()
    fresh_state.last_commit_attempt = CommitAttemptRecord(
        ts="2026-01-01T00:31:30+00:00",
        started_ts="2026-01-01T00:31:30+00:00",
        commit_message="still running",
        status="reviewing",
        tool_name="repo_commit",
        repo_key="repo-self",
        task_id="task-recent",
        attempt=2,
    )
    fresh_state.attempts = [fresh_state.last_commit_attempt]
    save_state(tmp_path, fresh_state)

    with patch("ouroboros.agent_startup_checks.check_uncommitted_changes", return_value=({"status": "ok"}, 0)), \
         patch("ouroboros.agent_startup_checks.check_version_sync", return_value=({"status": "ok"}, 0)), \
         patch("ouroboros.agent_startup_checks.check_budget", return_value=({"status": "ok"}, 0)), \
         patch("ouroboros.review_state._utc_now", return_value="2026-01-01T00:32:10+00:00"):
        verify_system_state(FakeEnv(), "gitsha")

    after_recent = load_state(tmp_path)
    assert after_recent.last_commit_attempt.status == "reviewing"


def test_startup_check_surfaces_interrupted_review_continuation(tmp_path):
    from ouroboros.agent_startup_checks import check_review_continuations
    from ouroboros.task_continuation import ReviewContinuation, save_review_continuation
    from ouroboros.task_results import STATUS_INTERRUPTED, write_task_result

    class FakeEnv:
        drive_root = str(tmp_path)

    write_task_result(
        tmp_path,
        "task-interrupted",
        STATUS_INTERRUPTED,
        result="Task interrupted during blocked review.",
    )
    save_review_continuation(
        tmp_path,
        ReviewContinuation(
            task_id="task-interrupted",
            source="blocked_review",
            stage="blocking_review",
            repo_key="repo-self",
            tool_name="repo_commit",
            attempt=2,
            block_reason="critical_findings",
        ),
        expect_task_id="task-interrupted",
    )

    result, issues = check_review_continuations(FakeEnv())
    assert issues == 1
    assert result["status"] == "warning"
    assert result["open_review_continuations"][0]["task_id"] == "task-interrupted"
    assert result["interrupted_tasks"][0]["task_id"] == "task-interrupted"

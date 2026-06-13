"""Monotonic lifecycle guard for write_task_result (v6.7.0-rc.1).

Pins the "ghost subagent" / status-corruption protections:
- a stale scheduled/running mirror cannot overwrite a cancel-intent or terminal
- a terminal status is sticky against a *different* terminal status
- cancel_requested still advances to the real terminal
- normal forward progress and same-status enrichment are unaffected
"""

import pytest

from ouroboros import task_results as tr


@pytest.fixture()
def drive(tmp_path):
    return tmp_path


def _status(drive, tid):
    return tr.load_task_result(drive, tid)["status"]


def test_terminal_not_regressed_by_running(drive):
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED, result="cancelled")
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING, result="stale mirror")
    assert _status(drive, "t") == tr.STATUS_CANCELLED


def test_terminal_is_sticky_against_other_terminal(drive):
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED)
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="late completion")
    assert _status(drive, "t") == tr.STATUS_CANCELLED

    tr.write_task_result(drive, "u", tr.STATUS_COMPLETED)
    tr.write_task_result(drive, "u", tr.STATUS_FAILED)
    assert _status(drive, "u") == tr.STATUS_COMPLETED


def test_terminal_sticky_against_unknown_status(drive):
    # A typo / future / unranked status must NOT overwrite a terminal one.
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="done")
    tr.write_task_result(drive, "t", "weird_unranked_status")
    assert _status(drive, "t") == tr.STATUS_COMPLETED


def test_same_terminal_status_enrichment_allowed(drive):
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="first")
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="enriched", trace_summary="trace")
    data = tr.load_task_result(drive, "t")
    assert data["status"] == tr.STATUS_COMPLETED
    assert data["result"] == "enriched"
    assert data["trace_summary"] == "trace"


def test_cancel_requested_blocks_running_but_allows_cancelled(drive):
    tr.write_task_result(drive, "t", tr.STATUS_CANCEL_REQUESTED)
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING)
    assert _status(drive, "t") == tr.STATUS_CANCEL_REQUESTED
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED, result="done")
    assert _status(drive, "t") == tr.STATUS_CANCELLED


def test_cancel_requested_not_masked_by_late_completion(drive):
    # A worker finishing just after the cancel latch must NOT flip the task to
    # "completed" — the requested cancel wins.
    tr.write_task_result(drive, "t", tr.STATUS_CANCEL_REQUESTED)
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED, result="late success")
    assert _status(drive, "t") == tr.STATUS_CANCEL_REQUESTED
    # ...but a real teardown crash (failed) or the cancellation itself may land.
    tr.write_task_result(drive, "t", tr.STATUS_CANCELLED)
    assert _status(drive, "t") == tr.STATUS_CANCELLED


def test_normal_forward_progress_and_retry(drive):
    tr.write_task_result(drive, "t", tr.STATUS_SCHEDULED)
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING)
    tr.write_task_result(drive, "t", tr.STATUS_INTERRUPTED)  # pre-requeue
    tr.write_task_result(drive, "t", tr.STATUS_RUNNING)      # retry
    tr.write_task_result(drive, "t", tr.STATUS_COMPLETED)
    assert _status(drive, "t") == tr.STATUS_COMPLETED


def test_updated_at_is_written(drive):
    tr.write_task_result(drive, "t", tr.STATUS_SCHEDULED)
    assert tr.load_task_result(drive, "t").get("updated_at")

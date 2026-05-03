"""Tests for the stale-module hint helper.

When the supervisor's source tree drifts from the boot fingerprint, every
tool error message gets a one-line suffix telling the agent that the error
may reflect stale code. Without this hint, the agent escalated to OS-level
workarounds during the 2026-05-02 cascade.
"""

from __future__ import annotations

import os
import pathlib
from unittest import mock

import pytest

from ouroboros.tools import _stale_hint
from supervisor import state as sup_state
from supervisor.module_watcher import (
    StalenessReport,
    compute_fingerprint,
    diagnose_staleness,
    fingerprint,
)


# ---------------------------------------------------------------------------
# compute_fingerprint
# ---------------------------------------------------------------------------

def test_compute_fingerprint_is_deterministic(tmp_path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    fp1 = compute_fingerprint([tmp_path])
    fp2 = compute_fingerprint([tmp_path])
    assert fp1 == fp2
    # SHA1 hex digest is 40 chars.
    assert len(fp1) == 40


def test_compute_fingerprint_changes_when_file_changes(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    fp_before = compute_fingerprint([tmp_path])

    new_mtime = f.stat().st_mtime + 5
    os.utime(f, (new_mtime, new_mtime))

    fp_after = compute_fingerprint([tmp_path])
    assert fp_before != fp_after


def test_compute_fingerprint_empty_tree_yields_stable_hash(tmp_path):
    fp = compute_fingerprint([tmp_path])
    # Empty fingerprint dict still produces a deterministic hash.
    assert fp == compute_fingerprint([tmp_path])


# ---------------------------------------------------------------------------
# diagnose_staleness
# ---------------------------------------------------------------------------

def test_diagnose_staleness_clean_tree(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    boot_fp = compute_fingerprint([tmp_path])
    boot_baseline = fingerprint([tmp_path])

    report = diagnose_staleness(boot_fp, [tmp_path], boot_baseline=boot_baseline)
    assert isinstance(report, StalenessReport)
    assert report.is_stale is False
    assert report.changed_paths == ()


def test_diagnose_staleness_detects_drift(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    boot_fp = compute_fingerprint([tmp_path])
    boot_baseline = fingerprint([tmp_path])

    new_mtime = f.stat().st_mtime + 5
    os.utime(f, (new_mtime, new_mtime))

    report = diagnose_staleness(boot_fp, [tmp_path], boot_baseline=boot_baseline)
    assert report.is_stale is True
    assert any(p.endswith("x.py") for p in report.changed_paths)


def test_diagnose_staleness_detects_added_file(tmp_path):
    boot_fp = compute_fingerprint([tmp_path])
    boot_baseline = fingerprint([tmp_path])

    (tmp_path / "y.py").write_text("y = 2\n", encoding="utf-8")
    report = diagnose_staleness(boot_fp, [tmp_path], boot_baseline=boot_baseline)
    assert report.is_stale is True
    assert any(p.endswith("y.py") for p in report.changed_paths)


# ---------------------------------------------------------------------------
# StalenessReport.hint_sentence
# ---------------------------------------------------------------------------

def test_hint_sentence_empty_when_fresh():
    report = StalenessReport(
        changed_paths=(), boot_fingerprint="abc", current_fingerprint="abc",
    )
    assert report.hint_sentence() == ""


def test_hint_sentence_mentions_changed_files():
    report = StalenessReport(
        changed_paths=("supervisor/git_ops.py",),
        boot_fingerprint="abc", current_fingerprint="def",
    )
    s = report.hint_sentence()
    assert "STALE_MODULE_HINT" in s
    assert "supervisor/git_ops.py" in s


def test_hint_sentence_truncates_long_lists():
    report = StalenessReport(
        changed_paths=tuple(f"f{i}.py" for i in range(8)),
        boot_fingerprint="abc", current_fingerprint="def",
    )
    s = report.hint_sentence()
    assert "+5 more" in s


# ---------------------------------------------------------------------------
# _stale_hint.maybe_append (the operator-facing entry point)
# ---------------------------------------------------------------------------

def _arm_boot_fingerprint(tmp_path):
    """Test helper: capture a boot-time fingerprint of ``tmp_path``."""
    boot_fp = compute_fingerprint([tmp_path])
    boot_baseline = fingerprint([tmp_path])
    sup_state.set_boot_module_fingerprint(boot_fp, boot_baseline)


def test_maybe_append_no_op_when_msg_empty(tmp_path):
    sup_state.set_boot_module_fingerprint("", {})
    assert _stale_hint.maybe_append("") == ""


def test_maybe_append_no_op_when_no_drift(tmp_path, monkeypatch):
    (tmp_path / "supervisor").mkdir()
    (tmp_path / "supervisor" / "x.py").write_text("x = 1\n", encoding="utf-8")
    _arm_boot_fingerprint(tmp_path / "supervisor")
    monkeypatch.setattr(
        "ouroboros.tools._stale_hint.default_watch_roots",
        lambda repo_dir: [tmp_path / "supervisor"],
    )
    out = _stale_hint.maybe_append("⚠️ GIT_ERROR (checkout): boom",
                                    repo_dir=tmp_path)
    assert "STALE_MODULE_HINT" not in out


def test_maybe_append_appends_hint_when_drift(tmp_path, monkeypatch):
    (tmp_path / "supervisor").mkdir()
    f = tmp_path / "supervisor" / "git_ops.py"
    f.write_text("x = 1\n", encoding="utf-8")
    _arm_boot_fingerprint(tmp_path / "supervisor")
    monkeypatch.setattr(
        "ouroboros.tools._stale_hint.default_watch_roots",
        lambda repo_dir: [tmp_path / "supervisor"],
    )

    new_mtime = f.stat().st_mtime + 5
    os.utime(f, (new_mtime, new_mtime))

    out = _stale_hint.maybe_append("⚠️ GIT_ERROR (checkout): boom",
                                    repo_dir=tmp_path)
    assert "STALE_MODULE_HINT" in out
    assert "git_ops.py" in out


def test_maybe_append_idempotent(tmp_path, monkeypatch):
    """A second call doesn't double-append."""
    (tmp_path / "supervisor").mkdir()
    f = tmp_path / "supervisor" / "git_ops.py"
    f.write_text("x = 1\n", encoding="utf-8")
    _arm_boot_fingerprint(tmp_path / "supervisor")
    monkeypatch.setattr(
        "ouroboros.tools._stale_hint.default_watch_roots",
        lambda repo_dir: [tmp_path / "supervisor"],
    )

    new_mtime = f.stat().st_mtime + 5
    os.utime(f, (new_mtime, new_mtime))

    once = _stale_hint.maybe_append("⚠️ GIT_ERROR (checkout): boom",
                                    repo_dir=tmp_path)
    twice = _stale_hint.maybe_append(once, repo_dir=tmp_path)
    assert once == twice
    # Exactly one occurrence of the marker.
    assert once.count("STALE_MODULE_HINT") == 1


def test_maybe_append_kill_switch_disables(tmp_path, monkeypatch):
    """Setting OUROBOROS_STALE_MODULE_HINT=false silences the hint."""
    (tmp_path / "supervisor").mkdir()
    f = tmp_path / "supervisor" / "git_ops.py"
    f.write_text("x = 1\n", encoding="utf-8")
    _arm_boot_fingerprint(tmp_path / "supervisor")
    monkeypatch.setattr(
        "ouroboros.tools._stale_hint.default_watch_roots",
        lambda repo_dir: [tmp_path / "supervisor"],
    )
    monkeypatch.setenv("OUROBOROS_STALE_MODULE_HINT", "false")

    new_mtime = f.stat().st_mtime + 5
    os.utime(f, (new_mtime, new_mtime))

    out = _stale_hint.maybe_append("⚠️ GIT_ERROR (checkout): boom",
                                    repo_dir=tmp_path)
    assert "STALE_MODULE_HINT" not in out


def test_maybe_append_no_op_when_fingerprint_unset(tmp_path):
    """If supervisor.state has no boot fingerprint (e.g. dev mode), no hint."""
    sup_state.set_boot_module_fingerprint("", {})
    out = _stale_hint.maybe_append("⚠️ GIT_ERROR (boom)", repo_dir=tmp_path)
    assert "STALE_MODULE_HINT" not in out

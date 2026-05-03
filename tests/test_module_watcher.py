"""Tests for ``supervisor.module_watcher`` — the source-tree drift detector
that prevents the 2026-05-02 stale-module cascade."""

from __future__ import annotations

import os
import pathlib
import time
from unittest import mock

import pytest

from supervisor.module_watcher import (
    ModuleDriftEvent,
    ModuleWatcher,
    default_watch_roots,
    fingerprint,
)


# ---------------------------------------------------------------------------
# fingerprint()
# ---------------------------------------------------------------------------

def test_fingerprint_returns_empty_for_empty_roots():
    assert fingerprint([]) == {}


def test_fingerprint_skips_pycache(tmp_path):
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "real.cpython-312.pyc").write_text("garbage", encoding="utf-8")

    fp = fingerprint([tmp_path])
    keys = [pathlib.Path(k).name for k in fp.keys()]
    assert "real.py" in keys
    assert "real.cpython-312.pyc" not in keys


def test_fingerprint_skips_swap_and_backup_files(tmp_path):
    (tmp_path / "ok.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / ".ok.py.swp").write_text("vim", encoding="utf-8")
    (tmp_path / "ok.py~").write_text("backup", encoding="utf-8")
    (tmp_path / "ok.pyc").write_text("compiled", encoding="utf-8")

    fp = fingerprint([tmp_path])
    keys = [pathlib.Path(k).name for k in fp.keys()]
    assert "ok.py" in keys
    assert ".ok.py.swp" not in keys
    assert "ok.py~" not in keys
    assert "ok.pyc" not in keys


def test_fingerprint_handles_single_file_root(tmp_path):
    f = tmp_path / "server.py"
    f.write_text("x = 1\n", encoding="utf-8")
    fp = fingerprint([f])
    assert str(f) in fp


def test_fingerprint_is_deterministic(tmp_path):
    """Same tree → same dict (sorted-key-stable)."""
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    fp1 = fingerprint([tmp_path])
    fp2 = fingerprint([tmp_path])
    assert list(fp1.keys()) == list(fp2.keys())
    assert fp1 == fp2


# ---------------------------------------------------------------------------
# ModuleWatcher behaviour
# ---------------------------------------------------------------------------

def _watcher(tmp_path, *, debounce_sec=0.0, boot_grace_sec=0.0, clock=None):
    """Helper: build a watcher with test-friendly defaults."""
    return ModuleWatcher(
        [tmp_path],
        debounce_sec=debounce_sec,
        boot_grace_sec=boot_grace_sec,
        clock=clock or time.time,
    )


def test_watcher_no_drift_returns_none(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    w.baseline()
    assert w.check() is None


def test_watcher_detects_modified_file(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    w.baseline()

    # Bump mtime explicitly to avoid filesystem-granularity issues.
    new_mtime = time.time() + 5
    os.utime(f, (new_mtime, new_mtime))

    evt = w.check()
    assert evt is not None
    assert isinstance(evt, ModuleDriftEvent)
    assert any(p.endswith("x.py") for p in evt.changed_paths)


def test_watcher_detects_added_file(tmp_path):
    (tmp_path / "x.py").write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    w.baseline()

    (tmp_path / "y.py").write_text("y = 2\n", encoding="utf-8")
    evt = w.check()
    assert evt is not None
    assert any(p.endswith("y.py") for p in evt.changed_paths)


def test_watcher_detects_deleted_file(tmp_path):
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    w.baseline()

    f.unlink()
    evt = w.check()
    assert evt is not None
    assert any(p.endswith("x.py") for p in evt.changed_paths)


def test_watcher_respects_boot_grace():
    """Within the boot-grace window, drift is invisible."""
    fake_now = [1000.0]

    def clock():
        return fake_now[0]

    with mock.patch("supervisor.module_watcher.time.time", clock):
        with mock.patch("supervisor.module_watcher.fingerprint",
                        side_effect=[{"a": 1}, {"a": 2}]):
            w = ModuleWatcher(["/dummy"], debounce_sec=0.0, boot_grace_sec=90.0, clock=clock)
            w.baseline()

            # 30 seconds in — still in boot grace, drift suppressed.
            fake_now[0] = 1030.0
            assert w.check() is None


def test_watcher_respects_debounce():
    """Drift detected on tick N stays pending until ``debounce_sec`` after N."""
    fake_now = [1000.0]
    fingerprints = [{"a": 1}, {"a": 2}, {"a": 2}, {"a": 2}]

    def clock():
        return fake_now[0]

    with mock.patch("supervisor.module_watcher.fingerprint",
                    side_effect=fingerprints):
        w = ModuleWatcher(["/dummy"], debounce_sec=2.0, boot_grace_sec=0.0, clock=clock)
        w.baseline()  # consumes fingerprints[0]

        # First tick after baseline at t=1s: drift detected, debounce not yet
        # elapsed → pending.
        fake_now[0] = 1001.0
        assert w.check() is None

        # Second tick at t=1.5s: still within debounce window.
        fake_now[0] = 1001.5
        assert w.check() is None

        # Third tick at t=3.5s: debounce elapsed since first-seen (1001.0).
        fake_now[0] = 1003.5
        evt = w.check()
        assert evt is not None
        assert evt.first_seen_ts == 1001.0


def test_watcher_does_not_refire_on_same_drift(tmp_path):
    """After firing, baseline rolls forward; subsequent ticks see no drift."""
    f = tmp_path / "x.py"
    f.write_text("x = 1\n", encoding="utf-8")
    w = _watcher(tmp_path)
    w.baseline()

    new_mtime = time.time() + 5
    os.utime(f, (new_mtime, new_mtime))

    evt1 = w.check()
    assert evt1 is not None

    # No further changes — should be silent.
    evt2 = w.check()
    assert evt2 is None


def test_watcher_returns_none_for_empty_roots():
    w = ModuleWatcher([], debounce_sec=0.0, boot_grace_sec=0.0)
    w.baseline()
    assert w.check() is None


# ---------------------------------------------------------------------------
# default_watch_roots()
# ---------------------------------------------------------------------------

def test_default_watch_roots_includes_supervisor_and_ouroboros(tmp_path):
    (tmp_path / "supervisor").mkdir()
    (tmp_path / "ouroboros").mkdir()
    (tmp_path / "server.py").write_text("# server", encoding="utf-8")
    roots = default_watch_roots(tmp_path)
    names = {pathlib.Path(r).name for r in roots}
    assert "supervisor" in names
    assert "ouroboros" in names
    assert "server.py" in names


def test_default_watch_roots_skips_missing_paths(tmp_path):
    """Only existing paths are returned (a partial repo shouldn't crash)."""
    (tmp_path / "supervisor").mkdir()
    # ouroboros/ and server.py deliberately absent
    roots = default_watch_roots(tmp_path)
    names = {pathlib.Path(r).name for r in roots}
    assert "supervisor" in names
    assert "ouroboros" not in names
    assert "server.py" not in names


def test_default_watch_roots_empty_under_frozen_build():
    """PyInstaller bundles don't have source on disk; watcher must no-op."""
    with mock.patch("supervisor.module_watcher.sys.frozen", True, create=True):
        roots = default_watch_roots(pathlib.Path("/dummy"))
    assert roots == []


# ---------------------------------------------------------------------------
# ModuleDriftEvent
# ---------------------------------------------------------------------------

def test_drift_event_summary_sentence_singular():
    evt = ModuleDriftEvent(changed_paths=("supervisor/git_ops.py",), first_seen_ts=0.0)
    assert evt.summary_sentence() == "1 file changed: supervisor/git_ops.py"


def test_drift_event_summary_sentence_truncates_at_three():
    evt = ModuleDriftEvent(
        changed_paths=("a.py", "b.py", "c.py", "d.py", "e.py"),
        first_seen_ts=0.0,
    )
    s = evt.summary_sentence()
    assert s.startswith("5 files changed: a.py, b.py, c.py")
    assert "+2 more" in s


def test_drift_event_summary_sentence_empty_falls_back():
    evt = ModuleDriftEvent(changed_paths=(), first_seen_ts=0.0)
    assert evt.summary_sentence() == "no changed files"

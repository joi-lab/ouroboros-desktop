"""Tests for the multi-agent bughunt fixes (2026-05-01).

Eight verified bugs were found by three parallel audit agents
covering concurrency/resource leaks, silent error swallowing, and
path-safety / sandbox holes. Each test below pins one of the fixes
so a regression would be caught immediately.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

import pytest


# test_chat_id_zero_preserved_not_nulled removed in v5.15.x — was a pure
# source-string pin reading ouroboros/agent.py for the literal patch
# (`int(task.get("chat_id") or 0) or None` must not appear). Per the user's
# `delete_trust_behavioral` choice, no behavioral replacement was added.
# Note: a directed behavioral guard (a task with chat_id=0 routed through
# the agent loop without collapse to None) is NOT currently in the suite;
# adding one would be a separate follow-up if this regression class
# resurfaces.


# ---------------------------------------------------------------------------
# P0 — case-insensitive safety-critical path check
# ---------------------------------------------------------------------------

def test_safety_critical_path_case_insensitive_match():
    """Bug 2: on macOS HFS+ / Windows NTFS (case-insensitive defaults),
    ``repo_write("bible.md", ...)`` writes to BIBLE.md but used to bypass
    the guard because the comparison was case-sensitive.

    Upstream v5.6.4 moved this surface into ``runtime_mode_policy`` with
    ``protected_path_category``. Test through that public API.
    """
    from ouroboros.runtime_mode_policy import (
        is_protected_runtime_path,
        protected_path_category,
    )

    assert protected_path_category("BIBLE.md") == "safety-critical"
    assert protected_path_category("bible.md") == "safety-critical"
    assert protected_path_category("Bible.md") == "safety-critical"
    assert protected_path_category("BIBLE.MD") == "safety-critical"
    assert protected_path_category("ouroboros/safety.py") == "safety-critical"
    assert protected_path_category("OUROBOROS/SAFETY.py") == "safety-critical"
    assert is_protected_runtime_path("bible.md") is True
    assert is_protected_runtime_path("BIBLE.MD") is True
    assert is_protected_runtime_path("README.md") is False
    assert is_protected_runtime_path("docs/safety.md") is False


# ---------------------------------------------------------------------------
# P1 — append_jsonl returns bool
# ---------------------------------------------------------------------------

def test_append_jsonl_returns_true_on_success(tmp_path):
    """Bug 4: callers couldn't distinguish a written event from one where
    every retry failed silently. Now returns True/False."""
    from ouroboros.utils import append_jsonl

    p = tmp_path / "log.jsonl"
    result = append_jsonl(p, {"event": "test"})
    assert result is True
    assert p.exists()
    rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()]
    assert rows == [{"event": "test"}]


def test_append_jsonl_returns_false_and_warns_on_total_failure(tmp_path, monkeypatch, caplog):
    """If all write attempts fail, return False and log a visible warning."""
    from ouroboros.utils import append_jsonl

    # Make any open() call inside append_jsonl raise.
    real_open = os.open
    def boom(*args, **kwargs):
        # Allow lock-file open, fail on the actual log open
        if any("log.jsonl" in str(a) for a in args):
            raise OSError("simulated disk failure")
        return real_open(*args, **kwargs)
    monkeypatch.setattr("os.open", boom)
    monkeypatch.setattr(pathlib.Path, "open", lambda *a, **kw: (_ for _ in ()).throw(OSError("simulated")))

    p = tmp_path / "log.jsonl"
    caplog.set_level("WARNING")
    result = append_jsonl(p, {"event": "test"})
    assert result is False
    assert "append_jsonl: all write attempts failed" in caplog.text


# ---------------------------------------------------------------------------
# P1 — safe_relpath rejects NUL bytes and control chars
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_path", [
    "BIBLE.md\x00.pdf",
    "good\x00bad.txt",
    "with\x01control.txt",
    "with\x1fboundary.txt",
])
def test_safe_relpath_rejects_control_chars(bad_path):
    """Bug 7: most Python file ops truncate at NUL. A path like
    'BIBLE.md\\x00.pdf' would write to BIBLE.md while the safety check
    sees a different name. Reject control chars upfront."""
    from ouroboros.utils import safe_relpath

    with pytest.raises(ValueError, match="control character|NUL"):
        safe_relpath(bad_path)


@pytest.mark.parametrize("ok_path", [
    "BIBLE.md",
    "docs/architecture.md",
    "with\ttab.txt",       # tab allowed
    "with\nnewline.txt",   # newline allowed (rare but legitimate in some content tools)
    "subdir/file with spaces.txt",
])
def test_safe_relpath_accepts_normal_paths(ok_path):
    """Don't over-reject — common path forms must still pass."""
    from ouroboros.utils import safe_relpath
    safe_relpath(ok_path)  # must not raise


def test_safe_relpath_still_blocks_traversal():
    """Existing traversal guard must still fire."""
    from ouroboros.utils import safe_relpath

    with pytest.raises(ValueError, match="traversal"):
        safe_relpath("../../../etc/passwd")


# ---------------------------------------------------------------------------
# P2 — safe_read distinguishes missing vs unreadable
# ---------------------------------------------------------------------------

def test_safe_read_warns_when_file_unreadable_but_exists(tmp_path, caplog):
    """Bug 6: previously DEBUG-only when a file existed-but-unreadable.
    For BIBLE.md or identity.md that's a real infrastructure error the
    operator must see."""
    from ouroboros.context import safe_read

    f = tmp_path / "secret.md"
    f.write_text("constitutional content", encoding="utf-8")
    f.chmod(0o000)  # unreadable

    try:
        with caplog.at_level("WARNING"):
            result = safe_read(f, fallback="(fallback)")
        # On systems where root can still read (containers without proper
        # cap drops), this might succeed. Skip if the file is still readable.
        if result == "constitutional content":
            pytest.skip("OS allows reading despite chmod 000")
        assert result == "(fallback)"
        assert any("safe_read" in r.message for r in caplog.records), (
            "safe_read must log at WARNING when an existing file is unreadable"
        )
    finally:
        f.chmod(0o644)


def test_safe_read_silent_when_file_simply_missing(tmp_path, caplog):
    """Missing file is the normal case for optional sources — no warning."""
    from ouroboros.context import safe_read

    with caplog.at_level("WARNING"):
        result = safe_read(tmp_path / "does_not_exist.md", fallback="default")
    assert result == "default"
    # No warning for the simple-missing case
    assert not any("safe_read" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# P2 — PID lock FD leak
# ---------------------------------------------------------------------------

def test_pid_lock_acquire_closes_fd_on_failure(tmp_path, monkeypatch):
    """Bug 8: when the lock acquire raised IOError, the file remained open
    and was overwritten in the global _lock_fd reference, leaking FDs."""
    from ouroboros import platform_layer

    # First holder acquires the lock
    lock_path = tmp_path / "test.pid"
    assert platform_layer.pid_lock_acquire(str(lock_path)) is True
    first_fd = platform_layer._lock_fd

    # Subsequent acquires (still in same process — would conflict on real
    # systems, but the close-on-failure behavior is what we test)
    # Force fcntl.flock to raise to simulate "another process holds it".
    # On macOS the same process can re-acquire (advisory locks), so we
    # simulate the failure path explicitly.
    if sys.platform != "win32":
        import fcntl
        original_flock = fcntl.flock
        def fake_flock(fd, op):
            raise BlockingIOError("simulated already-locked")
        monkeypatch.setattr(fcntl, "flock", fake_flock)

        # New attempt — should fail AND close the new fd, leaving _lock_fd
        # pointing at the original successful one.
        result = platform_layer.pid_lock_acquire(str(tmp_path / "another.pid"))
        assert result is False
        # The global fd must NOT have been overwritten with the failed handle.
        assert platform_layer._lock_fd is first_fd

        monkeypatch.setattr(fcntl, "flock", original_flock)
    platform_layer.pid_lock_release(str(lock_path))


# ``test_imports_after_bughunt_dont_raise`` was retired in v5.8.3-rc.5.
# pytest itself imports every test module during collection, and
# ``tests/test_smoke.py`` already pins the broader import surface; the
# bughunt-specific list above duplicated coverage that pytest collection +
# smoke already provide.

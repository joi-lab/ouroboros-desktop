"""Tests for the auto-snapshot module (Resume layer Phase 1).

Phase 1 lands dormant — checkpoints are written but nothing reads them.
Phase 2 (resume detection) and Phase 3 (briefing) build on top.
"""
from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from ouroboros.task_checkpoint import (
    CHECKPOINT_DIR_RELATIVE,
    latest_checkpoint_for_chat,
    write_task_checkpoint,
)


@dataclass
class _FakeCtx:
    """ToolContext-shaped stand-in for direct testing."""
    drive_root: pathlib.Path = pathlib.Path("/tmp/_fake")
    current_chat_id: Optional[int] = 1
    task_id: Optional[str] = "task-001"
    messages: Optional[List[Dict[str, Any]]] = None
    compat_profile: Any = None
    _dedup_counts: Dict[str, int] = field(default_factory=dict)
    _gate_failure_counts: Dict[Any, int] = field(default_factory=dict)
    _dedup_ping_counts: Dict[str, int] = field(default_factory=dict)
    _wall_budget_extensions: int = 0

    def drive_path(self, rel: str) -> pathlib.Path:
        return self.drive_root / rel


@pytest.fixture
def ctx(tmp_path):
    return _FakeCtx(
        drive_root=tmp_path,
        _dedup_counts={
            "repo_read:abc123": 1,
            "repo_read:def456": 1,
            "data_read:xyz789": 2,
        },
        _gate_failure_counts={
            ("repo_commit", "tests_preflight_blocked"): 3,
        },
        _dedup_ping_counts={"repo_read": 1},
        messages=[
            {"role": "system", "content": "constitutional"},
            {"role": "user", "content": "implement local self-advisory"},
            {"role": "assistant", "content": "Let me first map the architecture..."},
        ],
    )


# ---------------------------------------------------------------------------
# Basic write path
# ---------------------------------------------------------------------------

def test_write_creates_checkpoint_file(ctx):
    path = write_task_checkpoint(
        ctx, task_id="task-001", reason="wall_budget",
        started_ts=time.time() - 1800, rounds=12,
    )
    assert path is not None
    assert path.exists()
    assert path.name == "task-001.md"
    assert path.parent.name == "task_checkpoints"


def test_checkpoint_contains_expected_sections(ctx):
    path = write_task_checkpoint(
        ctx, task_id="task-001", reason="wall_budget",
        started_ts=time.time() - 1800, rounds=12,
    )
    body = path.read_text(encoding="utf-8")
    # Header
    assert "# Task checkpoint" in body
    assert "task-001" in body
    # Reason + metadata
    assert "wall_budget" in body
    assert "Rounds:" in body
    assert "Elapsed:" in body
    # Original prompt extracted from messages
    assert "implement local self-advisory" in body
    # Last thought extracted from assistant message
    assert "Let me first map the architecture" in body
    # Tool activity
    assert "repo_read" in body
    assert "data_read" in body
    # Gate failures
    assert "repo_commit" in body
    assert "tests_preflight_blocked" in body
    # Dedup pings
    assert "ping(s)" in body or "ping" in body


def test_empty_task_id_returns_none(ctx):
    path = write_task_checkpoint(
        ctx, task_id="", reason="wall_budget",
        started_ts=time.time(), rounds=0,
    )
    assert path is None


def test_no_drive_path_method_returns_none():
    """Defensive: a non-ToolContext object without drive_path() must not crash."""
    class Bare:
        pass
    bare = Bare()
    path = write_task_checkpoint(
        bare, task_id="t1", reason="wall_budget",
        started_ts=time.time(), rounds=0,
    )
    assert path is None


def test_reason_recorded_verbatim(ctx):
    """Each escalation reason must surface as-is so operators can grep."""
    for reason in ("wall_budget", "wall_budget_hard_ceiling",
                   "gate_retry", "dedup_threshold"):
        path = write_task_checkpoint(
            ctx, task_id=f"t-{reason}", reason=reason,
            started_ts=time.time(), rounds=1,
        )
        assert path is not None
        body = path.read_text(encoding="utf-8")
        assert f"`{reason}`" in body


def test_atomic_write_no_partial_files(ctx):
    """No `.md.tmp` or partially-written files should be left behind."""
    write_task_checkpoint(
        ctx, task_id="task-001", reason="wall_budget",
        started_ts=time.time(), rounds=1,
    )
    out_dir = ctx.drive_path(CHECKPOINT_DIR_RELATIVE)
    # Only the final .md should exist; no tmp residue
    leftover = [p for p in out_dir.iterdir() if p.suffix == ".tmp"]
    assert leftover == []


def test_idempotent_overwrites(ctx):
    """Re-writing the same task_id replaces the file (last write wins)."""
    p1 = write_task_checkpoint(
        ctx, task_id="t-x", reason="wall_budget",
        started_ts=time.time(), rounds=1, extra_note="UNIQ_NOTE_ALPHA",
    )
    p2 = write_task_checkpoint(
        ctx, task_id="t-x", reason="gate_retry",
        started_ts=time.time(), rounds=2, extra_note="UNIQ_NOTE_BETA",
    )
    assert p1 == p2
    body = p2.read_text(encoding="utf-8")
    assert "UNIQ_NOTE_BETA" in body
    assert "gate_retry" in body
    # First note should be gone
    assert "UNIQ_NOTE_ALPHA" not in body


def test_messages_missing_renders_not_available(tmp_path):
    """If ctx.messages is None, the prompt and last-thought sections render
    placeholders rather than crashing."""
    ctx = _FakeCtx(drive_root=tmp_path, messages=None)
    path = write_task_checkpoint(
        ctx, task_id="t-empty", reason="wall_budget",
        started_ts=time.time(), rounds=0,
    )
    body = path.read_text(encoding="utf-8")
    assert "(not available)" in body


def test_multipart_message_content_extracts_text(tmp_path):
    """Messages with list-of-blocks content (multipart) must extract text."""
    ctx = _FakeCtx(
        drive_root=tmp_path,
        messages=[
            {"role": "system", "content": [
                {"type": "text", "text": "system part 1"},
                {"type": "text", "text": "system part 2"},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "multipart user prompt"},
            ]},
            {"role": "assistant", "content": "single string thought"},
        ],
    )
    path = write_task_checkpoint(
        ctx, task_id="t-mp", reason="wall_budget",
        started_ts=time.time(), rounds=0,
    )
    body = path.read_text(encoding="utf-8")
    assert "multipart user prompt" in body
    assert "single string thought" in body


def test_extra_note_surfaces_in_body(ctx):
    path = write_task_checkpoint(
        ctx, task_id="t-note", reason="wall_budget",
        started_ts=time.time(), rounds=1, extra_note="custom op note",
    )
    body = path.read_text(encoding="utf-8")
    assert "custom op note" in body


# ---------------------------------------------------------------------------
# Latest-checkpoint discovery (used by Phase 2 resume-detection)
# ---------------------------------------------------------------------------

def test_latest_checkpoint_returns_most_recent_for_chat(ctx, tmp_path):
    """Multiple checkpoints — newest matching chat_id wins."""
    # Older checkpoint, different chat
    ctx_other = _FakeCtx(drive_root=tmp_path, current_chat_id=2)
    write_task_checkpoint(
        ctx_other, task_id="other-1", reason="wall_budget",
        started_ts=time.time(), rounds=1,
    )
    time.sleep(0.05)
    # Newer checkpoint, our chat
    write_task_checkpoint(
        ctx, task_id="ours-1", reason="wall_budget",
        started_ts=time.time(), rounds=1,
    )
    found = latest_checkpoint_for_chat(ctx, chat_id=1)
    assert found is not None
    assert found.name == "ours-1.md"


def test_latest_checkpoint_returns_none_when_no_match(ctx, tmp_path):
    found = latest_checkpoint_for_chat(ctx, chat_id=999)
    assert found is None


def test_latest_checkpoint_respects_max_age(ctx, tmp_path):
    """Old checkpoints are ignored beyond max_age_hours."""
    write_task_checkpoint(
        ctx, task_id="old", reason="wall_budget",
        started_ts=time.time(), rounds=1,
    )
    out = ctx.drive_path(CHECKPOINT_DIR_RELATIVE) / "old.md"
    # Set mtime to 2 days ago
    old_ts = time.time() - 2 * 24 * 3600
    import os
    os.utime(out, (old_ts, old_ts))
    found = latest_checkpoint_for_chat(ctx, chat_id=1, max_age_hours=24)
    assert found is None


def test_chat_id_none_returns_any_recent(ctx, tmp_path):
    """When chat_id is None, return the newest checkpoint regardless."""
    write_task_checkpoint(
        ctx, task_id="any-1", reason="wall_budget",
        started_ts=time.time(), rounds=1,
    )
    found = latest_checkpoint_for_chat(ctx, chat_id=None)
    assert found is not None


# ---------------------------------------------------------------------------
# Integration with escalation.py — checkpoints are written at fire time
# ---------------------------------------------------------------------------

def test_wall_clock_escalation_writes_checkpoint(tmp_path):
    """When check_wall_clock fires, _checkpoint_once is invoked."""
    from ouroboros.escalation import check_wall_clock
    from ouroboros.compat import PROFILES
    import queue as _q

    ctx = _FakeCtx(
        drive_root=tmp_path,
        compat_profile=PROFILES["small_local"],
        task_id="wall-test",
    )
    ctx.event_queue = _q.Queue()
    # 35 min elapsed, low diversity → fire_unproductive
    started = time.time() - 35 * 60
    fired = check_wall_clock(ctx, task_started_ts=started, rounds=10)
    assert fired is True
    assert getattr(ctx, "_task_checkpoint_written", False) is True
    cp = ctx.drive_path(CHECKPOINT_DIR_RELATIVE) / "wall-test.md"
    assert cp.exists()
    body = cp.read_text(encoding="utf-8")
    assert "wall_budget" in body


# ---------------------------------------------------------------------------
# Phase 2 — resume detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("continue where you left off", True),
    ("Continue where you left off please", True),
    ("Please continue", True),
    ("/resume", True),
    ("CONTINUE THE PREVIOUS TASK", True),
    ("resume previous task and finish it", True),
    # Negatives — words alone aren't enough
    ("continue refactoring this", False),
    ("can you resume normal operation", False),
    ("the resume button does what", False),
    ("", False),
    (None, False),
])
def test_detect_resume_signal(text, expected):
    from ouroboros.task_checkpoint import detect_resume_signal
    assert detect_resume_signal(text) is expected


def test_build_resume_hint_block_wraps_checkpoint(ctx, tmp_path):
    """Resume hint block must include the full checkpoint plus a header
    that tells the agent NOT to re-read files already mapped."""
    from ouroboros.task_checkpoint import build_resume_hint_block

    cp_path = write_task_checkpoint(
        ctx, task_id="t-rh", reason="wall_budget",
        started_ts=time.time() - 1800, rounds=10,
    )
    block = build_resume_hint_block(cp_path)
    assert "Resume context" in block
    assert "do **not** re-read" in block.lower() or "do not re-read" in block.lower()
    assert "<checkpoint>" in block
    assert "</checkpoint>" in block
    assert "Current task" in block
    # Body should still contain the task id
    assert "t-rh" in block


def test_build_resume_hint_block_truncates_oversized():
    """Very large checkpoints must be truncated."""
    from ouroboros.task_checkpoint import build_resume_hint_block

    big_path = pathlib.Path("/tmp/_test_big_checkpoint.md")
    big_path.write_text("X" * 50_000, encoding="utf-8")
    try:
        block = build_resume_hint_block(big_path, max_chars=2000)
        assert "[... checkpoint truncated for length]" in block
    finally:
        big_path.unlink(missing_ok=True)


def test_resume_hint_only_engaged_with_flag(ctx, tmp_path, monkeypatch):
    """build_user_content must NOT inject resume hint when the flag is unset."""
    from ouroboros.context import build_user_content
    monkeypatch.delenv("OUROBOROS_RESUME_DETECTION", raising=False)

    write_task_checkpoint(
        ctx, task_id="prev-task", reason="wall_budget",
        started_ts=time.time(), rounds=5,
    )

    class FakeEnv:
        def drive_path(self, p):
            return ctx.drive_root / p
    env = FakeEnv()

    task = {"text": "please continue", "chat_id": 1}
    result = build_user_content(task, env=env)
    assert result == "please continue"
    assert "Resume context" not in result


def test_resume_hint_engaged_with_flag_and_signal(ctx, tmp_path, monkeypatch):
    """Flag on + continuation phrase + checkpoint exists → hint prepended."""
    from ouroboros.context import build_user_content
    monkeypatch.setenv("OUROBOROS_RESUME_DETECTION", "true")

    write_task_checkpoint(
        ctx, task_id="prev-task", reason="wall_budget",
        started_ts=time.time(), rounds=5,
    )

    class FakeEnv:
        def drive_path(self, p):
            return ctx.drive_root / p
    env = FakeEnv()

    task = {"text": "please continue from where you stopped", "chat_id": 1}
    result = build_user_content(task, env=env)
    assert isinstance(result, str)
    assert "Resume context" in result
    assert "prev-task" in result
    # Original prompt is preserved at the end
    assert "please continue from where you stopped" in result


def test_resume_hint_skipped_when_no_continuation_signal(ctx, tmp_path, monkeypatch):
    """Flag on but no continuation signal → text unchanged."""
    from ouroboros.context import build_user_content
    monkeypatch.setenv("OUROBOROS_RESUME_DETECTION", "true")
    write_task_checkpoint(
        ctx, task_id="prev-task", reason="wall_budget",
        started_ts=time.time(), rounds=5,
    )

    class FakeEnv:
        def drive_path(self, p):
            return ctx.drive_root / p
    env = FakeEnv()

    task = {"text": "implement a new feature X", "chat_id": 1}
    result = build_user_content(task, env=env)
    assert result == "implement a new feature X"


def test_resume_hint_skipped_when_no_checkpoint_exists(tmp_path, monkeypatch):
    """Flag on, signal present, but no checkpoint → text unchanged."""
    from ouroboros.context import build_user_content
    monkeypatch.setenv("OUROBOROS_RESUME_DETECTION", "true")

    class FakeEnv:
        def drive_path(self, p):
            return tmp_path / p
    env = FakeEnv()
    task = {"text": "please continue", "chat_id": 99}
    result = build_user_content(task, env=env)
    assert result == "please continue"


def test_resume_hint_falls_back_to_any_checkpoint_when_chat_id_missing(ctx, monkeypatch):
    """If task has no chat_id, fall back to the most recent checkpoint
    of any chat (operator-driven cli use-case)."""
    from ouroboros.context import build_user_content
    monkeypatch.setenv("OUROBOROS_RESUME_DETECTION", "true")
    write_task_checkpoint(
        ctx, task_id="any-cp", reason="wall_budget",
        started_ts=time.time(), rounds=1,
    )

    class FakeEnv:
        def drive_path(self, p):
            return ctx.drive_root / p
    env = FakeEnv()
    task = {"text": "please continue"}  # no chat_id
    result = build_user_content(task, env=env)
    # latest_checkpoint_for_chat with chat_id=None returns the most recent
    assert "Resume context" in result


def test_checkpoint_written_at_most_once_per_task(tmp_path):
    """Two escalations from the same task must not overwrite the
    first (most-informative) checkpoint."""
    from ouroboros.escalation import _checkpoint_once

    ctx = _FakeCtx(
        drive_root=tmp_path, task_id="once-test",
        _dedup_counts={"a:b": 1},
    )
    # First call writes
    _checkpoint_once(ctx, reason="wall_budget", started_ts=time.time(), rounds=1)
    cp = ctx.drive_path(CHECKPOINT_DIR_RELATIVE) / "once-test.md"
    assert cp.exists()
    first_body = cp.read_text(encoding="utf-8")
    assert "wall_budget" in first_body
    # Second call (different reason) must NOT overwrite
    _checkpoint_once(ctx, reason="gate_retry", started_ts=time.time(), rounds=2)
    second_body = cp.read_text(encoding="utf-8")
    assert second_body == first_body
    assert "gate_retry" not in second_body

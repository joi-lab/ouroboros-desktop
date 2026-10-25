"""A task's log window reuses what cannot have changed: the archive listing and immutable archives.

Every context build reads the task's own newest tools and events rows (``Memory.read_task_recent``).
A task with no rows yet meets no quota: the whole live file plus the newest archives, every turn.
Output and coverage stay those of a fresh read.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from ouroboros import jsonl_tail, utils
from ouroboros.memory import Memory


def _write(path: pathlib.Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join((row if isinstance(row, str) else json.dumps(row)) + "\n" for row in rows),
                    encoding="utf-8")


@pytest.fixture
def logs(tmp_path):
    """events.jsonl beside three rotated archives of another task's rows."""
    for day in (1, 2, 3):
        _write(tmp_path / "archive" / f"events_2026100{day}T000000.jsonl",
               [{"task_id": "other", "type": "llm_round", "day": day}] * 3)
    _write(tmp_path / "logs" / "events.jsonl", [{"task_id": "other", "type": "llm_round"}])
    return tmp_path


def _listings(monkeypatch, root) -> list:
    seen, real = [], os.scandir
    monkeypatch.setattr(jsonl_tail.os, "scandir", lambda path: (
        seen.append(path) if pathlib.Path(path) == root / "archive" else None) or real(path))
    return seen


def _rotate(root, stamp) -> None:
    """The rotator's move: the live file becomes the newest archive, a new live file follows."""
    os.replace(root / "logs" / "events.jsonl", root / "archive" / f"events_{stamp}.jsonl")
    _write(root / "logs" / "events.jsonl", [{"task_id": "other", "type": "llm_round"}])


def test_the_archive_listing_is_reused_until_the_log_rotates(logs, monkeypatch):
    monkeypatch.setattr(utils, "_RACY_NS", 0)  # trust the directory as written (the tick rule has its own case)
    listed, memory = _listings(monkeypatch, logs), Memory(drive_root=logs)

    first = memory.read_task_recent("events.jsonl", "fresh", 200)
    assert memory.read_task_recent("events.jsonl", "fresh", 200) == first
    assert len(listed) == 1 and first[1]["archives_available"] == 3

    archive = logs / "archive"
    before = archive.stat()
    _rotate(logs, "20261004T000000")
    os.utime(archive, ns=(before.st_atime_ns, before.st_mtime_ns))  # a coarse clock: only the live inode moved
    after = memory.read_task_recent("events.jsonl", "fresh", 200)[1]
    assert len(listed) == 2 and after["archives_available"] == 4
    assert after["archive_sources"][0].endswith("events_20261004T000000.jsonl")


def test_a_directory_changed_within_a_clock_tick_is_listed_again(logs, monkeypatch):
    listed, memory = _listings(monkeypatch, logs), Memory(drive_root=logs)

    memory.read_task_recent("events.jsonl", "fresh", 200)
    memory.read_task_recent("events.jsonl", "fresh", 200)
    assert len(listed) == 2


def test_a_callers_own_seams_keep_their_fresh_listing(logs, monkeypatch):
    monkeypatch.setattr(utils, "_RACY_NS", 0)
    listed, memory = _listings(monkeypatch, logs), Memory(drive_root=logs)

    for _ in range(2):
        memory.read_task_recent("events.jsonl", "fresh", 200, stat=pathlib.Path.stat)
    assert len(listed) == 2

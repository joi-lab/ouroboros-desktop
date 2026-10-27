"""Host wait rows fold for the model and never spend a window's quota.

A wait is how the host ran, not the turn's work. The browser and the model word the same
episodes alike (``web/tests/fixtures/wait_fold_parity.json``); the model's ``## Recent progress``
keeps the task's own newest rows however many waits sit between them, and a chat history page
reads past them instead of filling up with them.
"""

from __future__ import annotations

import json
import pathlib

from ouroboros import dialogue_store as ds
from ouroboros.checkpoint_waits import fold_progress, wait_episode
from ouroboros.context import build_recent_sections
from ouroboros.gateway.history_paging import progress_quota_predicate
from ouroboros.memory import Memory

FIXTURE = pathlib.Path(__file__).resolve().parents[1] / "web" / "tests" / "fixtures" / "wait_fold_parity.json"


def wait(episode, phase, elapsed=0.4, *, kind="usage_lock_wait", task="task-a"):
    text = {"entered": "Waiting for accounting access", "ended": "Accounting wait ended"}[phase]
    return {"ts": "2026-10-27T13:09:00+00:00", "type": "task_checkpoint", "task_id": task, "checkpoint_kind": kind,
            "phase": phase, "episode_id": episode, "elapsed_sec": elapsed, "owner_visible": True,
            "role": "system", "system_type": "task_checkpoint", "content": text, "text": text}


def note(text, *, task="task-a"):
    return {"ts": "2026-10-27T13:10:00+00:00", "task_id": task, "text": text, "content": text}


def section(sections, header):
    return next(text for text in sections if text.startswith(header))


def test_the_model_and_the_browser_word_the_same_cases():
    for case in json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]:
        rows = [{"checkpoint_kind": case["kind"], "episode_id": episode, "phase": phase, "elapsed_sec": elapsed}
                for episode, phase, elapsed in case["rows"]]
        assert [entry["text"] for entry in fold_progress(rows)] == [case["line"]], case["rows"]


def test_only_typed_wait_episodes_fold():
    assert wait_episode(wait("w", "ended")) == ("usage_lock_wait", "w", True, 0.4)
    assert wait_episode({**wait("w", "ended"), "elapsed_sec": True})[3] is None
    for other in ({**wait("w", "ended"), "episode_id": ""}, {**wait("w", "ended"), "checkpoint_kind": "context_view"},
                  note("Waiting for accounting access"), None):
        assert wait_episode(other) is None
    rows = [note("a"), wait("w", "entered"), {**wait("c", "ended", kind="control_store_wait")}, note("b")]
    assert [entry.get("host_waits", False) for entry in fold_progress(rows)] == [False, True, True, False]


def test_a_run_of_waits_is_one_line_and_never_spends_the_progress_limit():
    rows = [note("note-0")]
    rows += [wait(f"w{i}", phase, elapsed=0.5 + i / 10) for i in range(24) for phase in ("entered", "ended")]
    rows += [note(f"note-{i}") for i in range(1, 15)]
    lines = Memory(drive_root=pathlib.Path("unused")).summarize_progress(rows, limit=15).split("\n")
    assert len(lines) == 16
    assert lines[0].endswith("note-0") and lines[1].endswith("Accounting waits ×24 · longest 2.8 s")
    assert lines[-1].endswith("note-14")
    # Without waits the window is the newest ``limit`` rows, as before.
    plain = [note(f"n{i}") for i in range(20)]
    assert Memory(drive_root=pathlib.Path("unused")).summarize_progress(plain, limit=15).split("\n")[0].endswith("n5")


def test_waits_beyond_the_quota_do_not_push_the_tasks_note_out_of_its_window(tmp_path):
    ds.append(tmp_path, "progress", note("the decisive note"))
    for index in range(40):
        for phase in ("entered", "ended"):
            ds.append(tmp_path, "progress", wait(f"w{index}", phase))
    ds.append(tmp_path, "progress", note("neighbour", task="task-b"))
    memory = Memory(drive_root=tmp_path)
    rows, coverage = memory.read_task_recent("progress.jsonl", "task-a", 50)
    assert rows[0]["text"] == "the decisive note" and len(rows) == 81
    assert coverage["host_waits"] == 80 and not coverage["older_available"] and not coverage["quota_met"]
    progress = section(build_recent_sections(memory, env=None, task_id="task-a"), "## Recent progress")
    assert "the decisive note" in progress and "Accounting waits ×40 · longest 0.4 s" in progress
    assert "neighbour" not in progress and "rendered" not in progress


def test_the_own_quota_still_bounds_the_window_and_says_older_rows_exist(tmp_path):
    ds.append(tmp_path, "progress", note("too old"))
    ds.append(tmp_path, "progress", wait("old", "ended"))
    for index in range(3):
        ds.append(tmp_path, "progress", note(f"step {index}"))
        ds.append(tmp_path, "progress", wait(f"w{index}", "entered"))
        ds.append(tmp_path, "progress", wait(f"w{index}", "ended"))
    rows, coverage = Memory(drive_root=tmp_path).read_task_recent("progress.jsonl", "task-a", 3)
    assert [row["text"] for row in rows if "episode_id" not in row] == ["step 0", "step 1", "step 2"]
    assert len(rows) == 9 and coverage["older_available"] and coverage["quota_met"]


def test_a_history_page_reads_past_wait_rows_without_counting_them():
    counts = progress_quota_predicate(lambda chat_id, row: True, lambda chat_id, default: chat_id or default)
    assert counts({**note("a step"), "chat_id": 1})
    assert not counts({**wait("w", "entered"), "chat_id": 1}) and not counts({**wait("w", "ended"), "chat_id": 1})
    assert counts({**wait("w", "ended"), "episode_id": "", "chat_id": 1})  # an untyped row still counts

"""The history, activity, breadth and rows the cost contract's turn law (B10) lays on a booted root.

tests/_cost_driver.py lays them after the archive import, untagged, between the turns it measures
(tests/test_cost_contract.py, B10). Every chat row goes in through ONE store transaction that runs the
statements ``dialogue_store.append`` runs for each (tests/test_cost_meter.py holds the two to the same
stored row) and every page in ONE publication (a part folds against its room's head, one publication per
room), so a k=30 root takes seconds, not minutes.
"""

from __future__ import annotations

import json
import os
import pathlib
import time
from typing import Any, Dict, List

SEALED_ROWS = 12  # per unit of k: rows of a room after the frontier, all sealed by one page
OPEN_ROWS = 3  # per breadth room: rows no page seals
BREADTH = 40  # live rooms the breadth step adds at k=1, with a quarter as many running roots
MORE_ROWS = 650  # open rows the rows step adds over the same rooms (B9's 650 -> 1,300, in the real turn)
ARCHIVED = 40  # archived knowledge notes and old rescue snapshots the archive step adds at k=1
RESCUE_AGE_SEC = 3 * 86_400.0  # an old restart's rescue snapshot: older than the health block's two hours


def _author(task_id: str) -> Dict[str, Any]:
    return {"kind": "mind", "task_id": task_id, "focus": {"role": "root", "task_id": task_id}}


def _rows(chat_id: int, count: int, tag: str) -> List[Dict[str, Any]]:
    from ouroboros.utils import utc_now_iso

    now = utc_now_iso()
    return [{"chat_id": chat_id, "direction": "out" if i % 2 else "in", "text": f"{tag} {chat_id}-{i}", "ts": now,
             **({"task_id": f"w{chat_id}"} if i % 2 else {})} for i in range(count)]


def append_rows(root: pathlib.Path, rows: List[Dict[str, Any]]) -> List[str]:
    """Chat rows in ONE store transaction, through the statements ``dialogue_store.append`` runs for each
    (tests/test_cost_meter.py holds the two to the same stored row); their source row ids."""
    from ouroboros import chat_chain, dialogue_store as ds

    with ds.transaction(root, write=True) as conn:
        for row in rows:
            body, columns = ds.encode(row), ds.facts(row)
            length, counted = len(body.encode("utf-8")) + 1, ds.stream_row("chat", columns)
            marks = conn.execute(
                "SELECT next_seq, live, next_ordinal, next_pos FROM streams WHERE stream='chat'").fetchone()
            seq = int(marks["next_seq"])
            ds.insert_row(conn, "chat", seq, row, body, length=length, segment=int(marks["live"]),
                          ordinal=int(marks["next_ordinal"]), pos=int(marks["next_pos"]) if counted else None)
            conn.execute("UPDATE streams SET next_seq=?, next_ordinal=next_ordinal+1, next_pos=next_pos+? "
                         "WHERE stream='chat'", (seq + length, int(counted)))
    return [chat_chain.source_row_id(row) for row in rows]


def _page(chat_id: int, ids: List[str], text: str) -> Dict[str, Any]:
    room = str(chat_id)
    return {"kind": "page", "room_id": room, "text": text, "author": _author(f"w{chat_id}"), "metadata": {},
            "covers": {"room_id": room, "rows": ids, "stream_span": [0, len(ids) - 1]}}


def _old_memory(root: pathlib.Path, k: int) -> int:
    """The old memory the activation imports: one block over the stream's oldest rows (the requested room's,
    constant) whose retelling has a section for each of k rooms that had no row in it, so a part folds each.
    Its message count meets the old cursor, so the range is exact and the frontier lies after those rows."""
    from ouroboros import chat_chain

    oldest = 0
    for _address, row, _pos in chat_chain.iter_rows(root):
        if not str(row.get("ts") or "").startswith("2024-"):
            break
        oldest += 1
    memory = root / "memory"
    memory.mkdir(parents=True, exist_ok=True)
    (memory / "dialogue_meta.json").write_text(json.dumps({
        "chat_log_signature": {"first_line_sha256": "dialogue:chat"}, "last_consolidated_offset": oldest}),
        encoding="utf-8")
    (memory / "dialogue_blocks.json").write_text(json.dumps([{
        "ts": "2024-01-02T00:00:00+00:00", "type": "summary", "message_count": oldest, "content": "The first days.",
        "rooms": [{"room_id": str(2000 + unit), "label": f"Room {2000 + unit}", "message_count": 0,
                   "content": f"What room {2000 + unit} settled before the chronicle began."} for unit in range(k)]}]),
        encoding="utf-8")
    return oldest


def archive_history(root: pathlib.Path, n: int) -> Dict[str, Any]:
    """n knowledge notes the mind archived and n old restarts' rescue snapshots: history no turn needs to list."""
    shelf, rescue = root / "memory" / "knowledge", root / "archive" / "rescue"
    shelf.mkdir(parents=True, exist_ok=True)
    old = time.time() - RESCUE_AGE_SEC
    for unit in range(n):
        (shelf / f"campaign-{unit:03d}.md").write_text(
            f"---\ntitle: Campaign {unit}\nsummary: what campaign {unit} learned\narchive:\n"
            f"  at: '2025-01-02T00:00:00+00:00'\n  reason: superseded by the current notes\n---\n"
            f"# Campaign {unit}\n\n" + "A finding of that campaign, kept for its history. " * 40, encoding="utf-8")
        snapshot = rescue / f"20250101T{unit:06d}"
        snapshot.mkdir(parents=True)
        (snapshot / "rescue_meta.json").write_text(json.dumps({"reason": "restart", "files": 1}), encoding="utf-8")
        (snapshot / "changes.diff").write_text("--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
        os.utime(snapshot, (old, old))
    return {"notes": n, "snapshots": n, "rows": 0}


def _rotated_logs(root: pathlib.Path) -> List[str]:
    """A live install's logs rotate (``supervisor.state.rotate_jsonl_log_if_needed``, at 800 KB), and a turn reads
    each live file's tail plus at most its three newest archives. Rotated here as the server rotates them, the live
    files the turns read hold the same rows at both sizes; what the build and the boot wrote, k-sized, is archived."""
    from supervisor.state import rotate_jsonl_log_if_needed

    names = ["events", "tools", "supervisor"]
    for name in names:
        rotate_jsonl_log_if_needed(root, f"{name}.jsonl", name, max_bytes=0)
    return names


def _consume_closed_outputs(root: pathlib.Path, k: int) -> int:
    """The scale root's closed runs are copies of a run whose output was never read, so each would stand as a
    current obligation; history's closed runs had theirs consumed."""
    from ouroboros import store
    from tests._scale_root import CLOSED_RUNS_PER_K, CUSTODY_PER_K

    ids = [f"run-hist-{unit}-{index}" for unit in range(k) for index in range(CUSTODY_PER_K)]
    ids += [f"run-closed-{unit}-{index}" for unit in range(k) for index in range(CLOSED_RUNS_PER_K)]
    with store.open_store(root, write=True) as txn:
        for run_id in ids:
            txn.put_custody(run_id, {**txn.custody_row(run_id), "output_consumed": True}, state="closed")
    return len(ids)


def turn_history(root: pathlib.Path, k: int) -> Dict[str, Any]:
    """History a turn must not reread, every row in ONE transaction and every page in ONE publication: the old
    memory imported at activation, its k sections folded by parts; the root's archived dialogue sealed, one page
    per room and day; k rooms whose rows came after the frontier, sealed; the closed runs' outputs consumed; the
    logs rotated."""
    from ouroboros import chat_chain
    from ouroboros.chronicle_store import ChronicleStore

    oldest = _old_memory(root, k)
    store = ChronicleStore(root)
    activation = store.ensure_activated(wait=True)
    groups: Dict[tuple, List[str]] = {}
    for _address, row, _pos in chat_chain.iter_rows(root):
        if str(row.get("ts") or "").startswith("2025-"):
            day = (int(row.get("chat_id") or 0), str(row["ts"])[:10])
            groups.setdefault(day, []).append(chat_chain.source_row_id(row))
    records = [_page(chat_id, ids, f"page of room {chat_id} on {day}")
               for (chat_id, day), ids in sorted(groups.items())]
    fresh = {2000 + unit: _rows(2000 + unit, SEALED_ROWS, "sealed") for unit in range(k)}
    ids = append_rows(root, [row for rows in fresh.values() for row in rows])
    for index, chat_id in enumerate(fresh):
        records.append(_page(chat_id, ids[index * SEALED_ROWS:(index + 1) * SEALED_ROWS], f"page of room {chat_id}"))
    published = store.publish(records)
    if not published.ok:
        raise RuntimeError(f"sealing pages: {published}")
    for chat_id in fresh:  # a part folds the room's retelling: one publication per room, against its head
        folded = store.publish_part(room_id=chat_id, text=f"the old story of room {chat_id}",
                                    member_ids=[f"legacy-b00-r{chat_id}"], author=_author(f"w{chat_id}"),
                                    expected_sequence=store.room_head(chat_id))
        if not folded.ok:
            raise RuntimeError(f"folding the retelling of room {chat_id}: {folded}")
    return {"activation": activation.get("kind"), "legacy_rows": oldest, "sealed_pages": len(records),
            "folded_sections": len(fresh), "consumed_outputs": _consume_closed_outputs(root, k),
            "rotated_logs": _rotated_logs(root)}


def foreign_activity(root: pathlib.Path) -> Dict[str, Any]:
    """What a busy install does between two owner turns: a row lands in another room, a page is published
    there (the driver adds a task that completes)."""
    from ouroboros.chronicle_store import ChronicleStore
    from supervisor import message_bus

    message_bus.log_chat("out", 515151, 0, "a new row of another room", drive_root=root, require_write=True)
    ids = append_rows(root, _rows(4999, 2, "published elsewhere"))
    if not ChronicleStore(root).publish([_page(4999, ids, "a page another task published")]).ok:
        raise RuntimeError("the page published elsewhere did not land")
    return {"rooms": [515151, 4999]}


def grow_breadth(root: pathlib.Path, n: int) -> Dict[str, Any]:
    """n more live rooms with story (open rows, a page, a note) and n // 4 running roots: current breadth."""
    from ouroboros.chronicle_store import ChronicleStore
    from ouroboros.task_results import write_task_result
    from supervisor import queue

    open_rows, paged = [], {}
    for i in range(n):
        open_rows += _rows(3000 + i, OPEN_ROWS, "open")
        paged[3000 + i] = _rows(3000 + i, 2, "paged")
    append_rows(root, open_rows)
    ids = append_rows(root, [row for rows in paged.values() for row in rows])
    records = []
    for index, chat_id in enumerate(paged):
        records.append(_page(chat_id, ids[index * 2:index * 2 + 2], f"page {chat_id}"))
        records.append({"kind": "note", "room_id": str(chat_id), "task_id": f"w{chat_id}", "text": f"note of {chat_id}",
                        "author": _author(f"w{chat_id}")})
    if not ChronicleStore(root).publish(records).ok:
        raise RuntimeError("the breadth rooms' story did not land")
    for i in range(n // 4):
        task_id, text = f"cur-broad-{i:03d}", ("a running root at work with a long brief " * 1500)[:60_000]
        write_task_result(root, task_id, "running", chat_id=1, text=text, root_task_id=task_id)
        with queue._queue_lock:
            queue.RUNNING[task_id] = {"task": {"id": task_id, "type": "task", "chat_id": 1, "text": text[:200],
                                               "root_task_id": task_id}, "worker_id": 100 + i, "attempt": 1,
                                      "started_at": time.time()}
    return {"rooms": n, "roots": n // 4, "rows": len(open_rows) + len(ids)}


def grow_rows(root: pathlib.Path, n: int) -> Dict[str, Any]:
    """n more open rows over the rooms already live; Main's newest of them is what its next turn must carry."""
    rooms = [1] + [3000 + i for i in range(BREADTH)]
    rows = [row for i, chat_id in enumerate(rooms)
            for row in _rows(chat_id, n // len(rooms) + (i < n % len(rooms)), "more")]
    append_rows(root, rows)
    main = [row["text"] for row in rows if row["chat_id"] == 1]
    return {"rows": len(rows), "rooms": len(rooms), "main_newest": main[-1]}

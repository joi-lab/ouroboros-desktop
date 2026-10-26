"""Own-work candidate selection and terminal links; neither invents page coverage."""
from __future__ import annotations

import json

from ouroboros import chat_chain, dialogue_store, memory_inventory
from ouroboros.chronicle_store import DELEGATED_FOCUS_ROLES
from ouroboros.contracts.chat_id_policy import is_a2a_chat_id


def failed_work_units(root, store):
    """Candidates over the open rows of finished failures (failed or cancelled) the mind never sealed.

    Every post-task run asks this while consciousness is on, a direct turn's in the server after its
    answer, so it is addressed by the open rows: they name their tasks, whose typed status alone is
    read (never a task body); only when one of them failed does the chronicle answer which of them
    the mind sealed (its page author index) and which rows a pending candidate of their room covers."""
    from ouroboros.memory_fallback import FallbackUnit
    from ouroboros.memory_inventory import OpenSegment
    from ouroboros.task_store import task_facts

    open_rows = memory_inventory.open_rows_by_room(root)
    named = sorted({str(entry[1].get("task_id") or "") for entries in open_rows.values() for entry in entries} - {""})
    # The named rows alone, through the primary key: a status filter in SQL lets the planner walk
    # every failed task through the status index instead.
    facts = task_facts(root, where="task_id IN (SELECT value FROM json_each(?))", params=(json.dumps(named),)) if named else {}
    failed = {task_id for task_id, fact in facts.items() if fact["status"] in ("failed", "cancelled")}
    if failed:
        failed -= store.mind_page_authors(failed)
    units = []
    for room, entries in open_rows.items():
        entries = [entry for entry in entries if str(entry[1].get("task_id") or "") in failed]
        pending = {ref for record in store.pending_candidates(room) for ref in record["covers"]["rows"]} if entries else set()
        by_task = {}
        for entry in entries:
            if entry[0]["row_sha256"] not in pending:
                by_task.setdefault(str(entry[1].get("task_id") or ""), []).append(entry)
        for task_id, rows in by_task.items():
            segment = OpenSegment(room, rows[0][0], rows[-1][0], tuple(rows), store.room_head(room))
            units.append(FallbackUnit("page", room, f"candidate:{task_id}:{rows[0][0]['row_sha256'][:12]}",
                                      segment=segment, candidate_task=task_id))
    return sorted(units, key=lambda unit: unit.segment.rows[0][2])


def _terminal_writer(record):
    author = record.get("author", {})
    task_id = str(author.get("task_id") or "")
    own_writer = author.get("kind") == "mind" or (author.get("kind") == "helper" and
        author.get("focus", {}).get("role") in DELEGATED_FOCUS_ROLES)
    return task_id if record.get("kind") == "page" and own_writer else ""


def _terminal_after(record):
    return int(record.get("metadata", {}).get("terminal_after_pos",
        (record.get("covers", {}).get("stream_span") or [0, 0])[-1]))


def capture_terminal_rows(root, records):
    """Group the writers' later terminal rows once, through task indexes at one captured boundary."""
    starts = {}
    for record in records:
        task_id = _terminal_writer(record)
        if task_id:
            key = (str(record["room_id"]), task_id)
            starts[key] = min(starts.get(key, _terminal_after(record)), _terminal_after(record))
    if not starts:
        return {}
    upper = dialogue_store.stream_end(root, "chat")
    lower = chat_chain.position_boundary(root, min(starts.values()) + 1)
    dialogue_store.require_imported(root, "chat", lower=lower, upper=upper)
    tasks = {task_id for _room, task_id in starts}
    facts, grouped, after = memory_inventory.membership_facts(root), {}, lower - 1
    while entries := dialogue_store.task_rows(root, tasks, stream="chat", after=after, before=upper):
        chosen = []
        for entry in entries:
            row = entry.row
            task_id = str(row.get("task_id") or "")
            host = row.get("type") in ("task_summary", "project_completion_summary")
            final = row.get("direction") == "out" and not row.get("is_progress") and (
                row.get("terminal_origin") or row.get("task_terminal_status") or row.get("task_phase") == "finalizing")
            if task_id in tasks and (host or final) and not is_a2a_chat_id(row.get("chat_id", 1)):
                chosen.append((entry, task_id))
        # The page's terminal rows: their task lineages in one read, then their rooms; addressed together.
        facts.rooms.warm({str(entry.row.get(key) or "") for entry, _task in chosen
                          for key in ("task_id", "parent_task_id", "root_task_id")} - {""})
        chosen = [(entry, task_id, memory_inventory.rooms_of_row(memory_inventory.row_meta(entry.row), facts))
                  for entry, task_id in chosen]
        for (address, row, pos), (_entry, task_id, rooms) in zip(
                chat_chain._addressed(root, [entry for entry, _task, _rooms in chosen]), chosen):
            for room in rooms:
                key = (room, task_id)
                if key in starts and pos > starts[key]:
                    grouped.setdefault(key, []).append((address, row, pos))
        after = entries[-1].seq
    return grouped


def terminal_links(root, record, cache=None):
    """Only this page writer's later typed terminal rows, as separately attributed addresses."""
    task_id = _terminal_writer(record)
    if not task_id:
        return []
    end, links = _terminal_after(record), []
    cache = capture_terminal_rows(root, (record,)) if cache is None else cache
    for address, row, pos in cache.get((str(record["room_id"]), task_id), ()):
        if pos <= end:
            continue
        addr = chat_chain.format_address(address)
        speaker = "host summary" if row.get("type") in ("task_summary", "project_completion_summary") else "task final report"
        status = row.get("status") or row.get("task_terminal_status") or "status not recorded"
        links.append(f"- Linked {speaker} of task {task_id} ({status}); not page coverage; "
                     f"memory_read(room_id='{record['room_id']}', rows=true, from='{addr}', to='{addr}')")
    return links

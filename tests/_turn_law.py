"""The turn law's gap table and the function that holds a law to it (tests/test_cost_contract.py B10,
tests/test_cost_host.py).

A breach in today's code is a ``TURN_GAPS`` row: its cause with today's numbers and the commits that remove it,
and the counters it excuses, each up to today's growth. The law's test is then a strict xfail (``turn_gap``) that
flips (XPASS) the moment the removers land, and the row is deleted; a counter the row does not name, or growth
past a ceiling, fails for real, so the marker cannot hide a new regression.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Tuple

import pytest


class TurnLawBreach(AssertionError):
    """A turn law broken where a ``TURN_GAPS`` row excuses it. Any other failure is a real one."""


_STORY = ("a chronicle _index call per room with story and reader (memory_view._fixes, memory_inventory."
          "record_period, story_counts, memory_view._story_pages), each a lock file, a replay of the index, a "
          "connection and about 13 statements")
_REMOVERS = ("Removers: lane A H1 (chronicle reads take no lock and replay nothing while current), M2 (one chronicle "
             "snapshot per capture, scoped to the view's rooms), M3 (the rows after the frontier keep their facts "
             "beside the chronicle); lane B C6 (the archive listing kept until the log rotates)")
# (law, tag) -> (the cause: today's numbers and the commits that remove it; the counters it excuses, each with the
# growth it may not pass meanwhile: measured at k=30 for the history laws, scaled by the units of history added).
TURN_GAPS: Dict[Tuple[str, str], Tuple[str, Dict[str, int]]] = {
    ("L1", "T:room"): (
        f"A project room's turn, k=1 -> k=30 (26.10): {_STORY}, 29 more rooms (connect, lock_acquired, open:root "
        "+116, statements +2,830); every dialogue row after the frontier decoded, sealed or not (rows_decoded 206 -> "
        "5,078) with its position and legacy lookups (vm_steps +371,200); other rooms' story records read four "
        "times (json_chars +3.7 MB); the log archive listed and its three newest segments reread for a task with no "
        f"rows of its own (dir_entries:archive +609, read_bytes:archive +97 KB). {_REMOVERS}; lane B C7 (an archive "
        "holding none of a task's rows is not parsed again for it).",
        {"connect": 118, "connect:root": 118, "lock_acquired": 118, "open:root": 118, "statements": 2832,
         "begins": 29, "commits": 29, "json_loads": 6044, "json_chars": 3683487, "rows_decoded": 4874,
         "stat:root": 1417, "stat:outside": 3598, "dir_entries:archive": 611, "read_bytes:archive": 101293,
         "vm_steps": 372040}),
    ("L2", "T:main"): (
        f"The process's first owner turn, k=1 -> k=30 (26.10): {_STORY}, plus memory_inventory._sealed per room on "
        "a cold process (connect, lock_acquired, os_open +145, open +146, statements +3,272); rows decoded 326 -> "
        f"6,996 (begins +27); the log archive listed (dir_entries +609). {_REMOVERS}.",
        {"connect": 147, "lock_acquired": 147, "os_open": 147, "open": 148, "statements": 3274, "begins": 29,
         "dir_entries": 611, "rows_decoded": 6672}),
    ("L2", "T:mainw"): (
        f"Main's next turn, k=1 -> k=30 (26.10): {_STORY} (connect, lock_acquired, os_open +116, open +117, "
        f"statements +2,779); rows decoded 303 -> 6,973 (begins +27); the log archive listed (dir_entries +609). "
        f"{_REMOVERS}.",
        {"connect": 118, "lock_acquired": 118, "os_open": 118, "open": 119, "statements": 2781, "begins": 29,
         "dir_entries": 611, "rows_decoded": 6672}),
    ("L2", "T:main:foreign"): (
        f"Main's turn after a row and a page elsewhere and a completed task, k=1 -> k=30 (26.10): {_STORY}, plus "
        "memory_inventory._sealed per room again (its memo is keyed on the chronicle file, which the page moved): "
        "connect, lock_acquired, os_open +145, open +146, statements +3,330; rows decoded 312 -> 6,982 (begins "
        f"+27); the log archive listed (dir_entries +609). {_REMOVERS}.",
        {"connect": 147, "lock_acquired": 147, "os_open": 147, "open": 148, "statements": 3332, "begins": 29,
         "dir_entries": 611, "rows_decoded": 6672}),
    ("L3", "T:main:broad"): (
        "40 live rooms with story and 10 running roots (k=1, 26.10): connect, lock_acquired, open and os_open +252, "
        "statements +5,738, begins +150 (allowance 8). Six chronicle _index calls per added room, each a lock file, a "
        "replay of the index and a connection (_sealed, room_facts, story_counts, _story_pages, _notes_by_room "
        "twice); about nine whole-document reads per running root (server_routing_context._add and its genexpr, "
        "owner_mailbox.mailbox_drain_ended, queue_timeouts._task_drive_for_task: +7.7 MB). Removers: lane A H1 and "
        "M2 (one chronicle snapshot per capture); lane B C4 (one routing snapshot per message, status by column).",
        {"connect": 254, "lock_acquired": 254, "open": 254, "os_open": 254, "begins": 152, "statements": 5740}),
    ("L3", "T:main:archive"): (
        "40 archived knowledge notes and 40 old rescue snapshots (k=1, the turn after the one that met them): "
        "open +40 (knowledge._navigation_descriptor rereads every note body each turn) and dir_entries +123 (the "
        "knowledge shelf's rglob 80, the health block's rescue listing 40). No remover in this update: lane B C8 "
        "parses a note once per file identity (the opens go), the shelf listing and the rescue scan stay.",
        {"open": 42, "dir_entries": 125}),
    ("L5", "T:main"): (
        "A process's first owner turn looks openrouter.ai up twice before its first request "
        "(llm_capability_policy fetches the OpenRouter catalogue on the first capability read; 5 s timeout on a "
        "live install, refused here). Remover: lane B C10 (the catalogue recorded and prefetched at boot).",
        {"network_refused": 2}),
    ("host", "census"): (
        "check_stray_server_processes spawns one ps per listed process: 2 children for one, 25 for 24 (26.10); a "
        "process's first owner turn on the owner's Mac paid 373 to 632. Remover: razzant/ouroboros#2025 (one ps "
        "per 256 pids), owned by the voice sprint (owner decision 22.10, 3A).",
        {"popen": 25}),
}


def turn_gap(law: str, tag: str) -> tuple:
    gap = TURN_GAPS.get((law, tag))
    return (pytest.mark.xfail(strict=True, raises=TurnLawBreach, reason=gap[0]),) if gap else ()


def hold_turn_law(law: str, tag: str, growth: Dict[str, int], allowed: Dict[str, int], readers: Any, *,
                  scale: float = 1.0) -> None:
    """Each counter's growth within its ``allowed`` growth; a ``TURN_GAPS`` row excuses the counters it names,
    each up to its ceiling (times ``scale``, the units of history added over the 29 they were measured on), and
    nothing else. ``readers`` names the readers: a value, or a function of the breaching counters."""
    breaches = {key: delta for key, delta in growth.items() if delta > allowed[key]}
    named = readers(breaches) if callable(readers) else readers
    ceilings = {key: math.ceil(ceiling * scale) for key, ceiling in TURN_GAPS.get((law, tag), ("", {}))[1].items()}
    unexcused = {key: (delta, ceilings.get(key)) for key, delta in breaches.items() if delta > ceilings.get(key, -1)}
    if ceilings:
        assert not unexcused, f"{law} {tag} grew past its gap (growth, ceiling): {unexcused}; readers: {named}"
    if breaches:
        raise TurnLawBreach(f"{law} {tag} grew {breaches}; readers: {named}")


def readers_of(before: Dict[str, Any], after: Dict[str, Any]) -> Callable[[Dict[str, int]], List[tuple]]:
    """The readers that grew the breaching counters most (a read transaction is named by its statements)."""
    def named(breaches: Dict[str, int]) -> List[tuple]:
        kinds = {key.split(":")[0] for key in breaches}
        kinds |= {"statements"} if {"begins", "commits"} & kinds else set()
        old = before.get("callers", {})
        grown = [(source, amount - old.get(source, 0)) for source, amount in after.get("callers", {}).items()
                 if source.split(" ", 1)[0].split(":")[0] in kinds and amount > old.get(source, 0)]
        return sorted(grown, key=lambda item: -item[1])[:8]
    return named

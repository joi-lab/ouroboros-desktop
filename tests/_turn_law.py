"""The turn law's gap table and the function that holds a law to it (tests/test_cost_contract.py B10,
tests/test_cost_host.py).

A breach in today's code is a ``TURN_GAPS`` row: its cause with today's numbers and its remover, or why it has
none, and the counters it excuses, each up to today's growth. The law's test is then a strict xfail (``turn_gap``)
that flips (XPASS) the moment its cause is gone, and the row is deleted; a counter the row does not name, or growth
past a ceiling, fails for real, so the marker cannot hide a new regression.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Tuple

import pytest


class TurnLawBreach(AssertionError):
    """A turn law broken where a ``TURN_GAPS`` row excuses it. Any other failure is a real one."""


# (law, tag) -> (the cause: today's numbers, its readers and its remover, or why it has none; the counters it
# excuses, each with the growth it may not pass meanwhile: measured at k=30 for the history laws, scaled by the
# units of history added). Measured 26.10 on the integrated tree in its steady state (tests/_turn_shapes.py).
TURN_GAPS: Dict[Tuple[str, str], Tuple[str, Dict[str, int]]] = {
    ("L1", "T:room"): (
        "A project room's turn, k=1 -> k=30: its capture reads the whole chronicle index, one statement per "
        "table, the stored tail facts of every room included (chronicle_store.capture: json_chars +4.0 MB, "
        "vm_steps +93,200), and the story readers parse every record they filter from it, the live rooms' pages "
        "included (memory_inventory.story_counts, memory_view._story_pages, room_records, room_facts, "
        "folded_members: json_loads +841, json_chars +3.2 MB). No remover in this update: a capture that reads "
        "what its view shows (its rooms' records, their open rows, counts kept per room) is lane A's to design.",
        {"json_loads": 843, "json_chars": 7172359, "vm_steps": 93840}),
    ("L3", "T:main:broad"): (
        "40 live rooms with story and 10 running roots (k=1): the turn's capture stores the facts of the 200 "
        "rows the step added, one INSERT execution each (memory_inventory._read_tail -> ChronicleStore."
        "extend_tail: statements +200; the routing pre-pass reads the running roots' pause fences in one "
        "statement, server_routing_context.addressable_roots). No remover in this update: the tail facts "
        "stored in one statement per chunk, as M4 reads a page (lane A).",
        {"statements": 216}),
    ("L4", "T:main:rows"): (
        "650 open rows over Main and 40 rooms (k=1): the turn's capture stores the facts of the rows appended "
        "since the last capture, one INSERT execution per row (memory_inventory._read_tail -> ChronicleStore."
        "extend_tail, executemany: statements +450 beyond the 200 of the turn before), each a SQLite step that "
        "releases the GIL; the dialogue's page reads stay inside B9's allowance (begins +3). No remover in this "
        "update: the tail facts stored in one statement per chunk (json_each), as M4 reads a page (lane A).",
        {"statements": 468}),
    ("L3", "T:main:archive"): (
        "40 archived knowledge notes and 40 old rescue snapshots (k=1, the turn after the one that met them): "
        "the knowledge shelf is listed for the index the mind is shown, which counts the archived notes "
        "(knowledge.inventory_knowledge: dir_entries +80, scandir +2), and the health block lists the rescue "
        "folder (context_health._collect_log_analysis_checks: dir_entries +40, listdir +1); no note body is "
        "read again (knowledge._navigation_row). NEVER: both listings are what the turn shows (the shelf's "
        "index; the rescue scan, kept by decision); the row stays so that any other work per item fails.",
        {"dir_entries": 122, "scandir": 4, "listdir": 3}),
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

"""The memory's reads over the shapes of the owner's install on 24.10 (tests/_memory_capture_shape.py).

Main's view held 241 live rooms and its readers looked rows, row positions, task lineages and
legacy authors up one at a time: 2,615 dialogue transactions for one view of 1,300 open rows, a
server core busy for the first quarter hour after an update. Each reader now reads a page of rows
together; each test holds both the answer (the one-at-a-time reader's) and the number of reads.
"""
from __future__ import annotations

import collections
import itertools
import sys

from ouroboros import chat_chain as cc, dialogue_store as ds, memory_view as mv
from tests import _memory_capture_shape as shape


def _counted(monkeypatch, module, name):
    """Each call of ``module.name`` from now on, by its caller's name."""
    calls, real = [], getattr(module, name)

    def counted(*args, **kwargs):
        calls.append(sys._getframe(1).f_code.co_name)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, counted)
    return calls


def _one_at_a_time(root, addresses, **_kwargs):
    """The row reader before 24.10: ``resolve_row`` for each address."""
    found = {}
    for address in addresses:
        row, _status = cc.resolve_row(root, address)
        if row is not None:
            found.setdefault(address["row_sha256"], row)
    return found


def test_main_view_reads_its_open_rows_a_page_at_a_time_and_reads_as_before(tmp_path, monkeypatch):
    made = shape.open_rows(tmp_path, 1_300)
    opened = _counted(monkeypatch, ds, "transaction")
    batched = mv.capture_memory_view(tmp_path, made["task"], made["spec"])
    assert batched.active and len(batched.live_rooms) == shape.ROOMS - 1
    pages = -(-1_300 // cc._ADDRESS_PAGE)
    # The stream's pages, two lookups a page, the selected rows' one read, and the readers' bounds.
    assert len(opened) <= 3 * pages + 8, collections.Counter(opened)
    monkeypatch.setattr(cc, "rows_by_address", _one_at_a_time)
    assert mv.capture_memory_view(tmp_path, made["task"], made["spec"]) == batched


def test_rows_by_address_answers_as_resolve_row_and_a_wrong_hint_still_finds_its_row(tmp_path):
    shape.open_rows(tmp_path, 60, rooms=3)
    addresses = [address for address, _row, _pos in cc.iter_rows(tmp_path)]
    assert cc.rows_by_address(tmp_path, addresses) == _one_at_a_time(tmp_path, addresses)
    moved = {**addresses[5], "hint": {"seq": addresses[9]["hint"]["seq"]}}  # a hint naming another row
    absent = cc.row_address({"chat_id": 1, "ts": "2026-01-01T00:00:00+00:00", "text": "never written"})
    for odd in (moved, absent):  # alone: an address keyed like another one would join it
        assert cc.rows_by_address(tmp_path, [odd]) == _one_at_a_time(tmp_path, [odd])
    assert moved["row_sha256"] in cc.rows_by_address(tmp_path, [moved])
    assert cc.rows_by_address(tmp_path, [absent]) == {}


def test_a_filtered_room_reader_that_stops_early_reads_at_most_a_page_beyond_what_it_used(tmp_path, monkeypatch):
    for i in range(2):
        ds.append(tmp_path, "chat", {"chat_id": 1, "direction": "out", "task_id": "wanted", "text": f"wanted {i}"})
    for i in range(5_000):
        ds.append(tmp_path, "chat", {"chat_id": 1, "direction": "out", "task_id": f"other{i % 50}", "text": f"other {i}"})
    probes = _counted(monkeypatch, ds, "probe_rows")
    first = list(itertools.islice(cc.iter_room_rows(tmp_path, "1", task_ids=["wanted"]), 2))
    assert [row["text"] for _address, row, _pos in first] == ["wanted 0", "wanted 1"]
    assert len(probes) <= 3  # one page of examined rows, not pages of kept rows over the room's 5,000 others


def test_story_terminal_links_are_addressed_together(tmp_path, monkeypatch):
    store = shape.terminal_links(tmp_path, 40)
    reference = mv._story_pages(store, lambda room, *_args: str(room), {})
    opened = _counted(monkeypatch, ds, "transaction")
    story = mv._story_pages(store, lambda room, *_args: str(room), {})
    assert story == reference and sum(len(page["terminal_links"]) for page in story) == 80
    assert len(opened) <= 10, collections.Counter(opened)  # 160 when each terminal row was addressed alone


def test_room_membership_reads_the_rows_task_lineages_together(tmp_path, monkeypatch):
    from ouroboros import memory_inventory, projects_registry

    shape.open_rows(tmp_path, 1_300)
    reads = _counted(monkeypatch, projects_registry, "_read_lineage")
    assert len(memory_inventory.open_rows_by_room(tmp_path)) == shape.ROOMS
    assert len(reads) <= 2, reads  # one read a generation of lineage; 300 tasks read one by one before


def test_legacy_authors_are_read_together_and_answer_as_one_lookup_a_task(tmp_path, monkeypatch):
    from ouroboros import task_results, task_store

    made = shape.legacy_authorship(tmp_path, 240)
    alone = _counted(monkeypatch, task_results, "load_task_result")
    together = _counted(monkeypatch, task_store, "documents_by_ids")
    warmed = mv.capture_memory_view(tmp_path, made["task"], made["spec"])
    assert alone == [] and together == ["admitted_results"]
    monkeypatch.setattr(mv, "warm_lineage", lambda *_args: None)
    assert mv.capture_memory_view(tmp_path, made["task"], made["spec"]) == warmed
    assert len(alone) == 240  # the same authors, one task result at a time

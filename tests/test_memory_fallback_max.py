"""The helper's early trigger (owner 27.10, 2A): with consciousness off, an open lost-Max episode that folding my
memory can end makes the late phase's one Light call fold one room's oldest run of pages into a signed part."""
from __future__ import annotations

import pytest

from ouroboros import memory_max as mm, store
from ouroboros.chronicle_store import ChronicleStore
from tests import _memory_inventory_shared as shared
from tests.test_memory_fallback import _consciousness, _drafts, _Light, _mind_page, _run, light_route

PART = '{"text": "Ouroboros and the owner worked through the morning.", "quotes": []}'


@pytest.fixture
def light(monkeypatch):
    return light_route(monkeypatch)


def _episode(root, *, zero_need=400_000, window=1_000_000, closed=False):
    row = {"id": "e1", "opened_at": "2026-10-27T10:00:00+00:00", "task": "t", "starts": 1, "window": window,
           "levels": 2, "first": {}, "last": {"zero_need": zero_need, "window": window, "task": "t"},
           "notices": {"open": {"id": "memory_max:e1:open", "recorded": True}}}
    with store.open_store(root, write=True) as txn:
        txn.put_row(mm.SET, mm.IDENTITY, {**row, **({"closed": {"at": "2026-10-27T11:00:00+00:00"}} if closed else {})})


def _main_pages(root):
    return [_mind_page(root, "1", 10, 11)["id"], _mind_page(root, "1", 12, 12)["id"]]


def test_an_episode_memory_can_end_folds_one_rooms_oldest_pages_mine_included(tmp_path, monkeypatch, light):
    shared.world(tmp_path)
    _consciousness(monkeypatch, False)
    pages = _main_pages(tmp_path)
    _episode(tmp_path)
    llm = _Light(PART)
    run = _run(tmp_path, llm, direct=True)
    assert run.outcome == "published" and run.unit.key == f"max:1:{pages[0]}" and len(llm.prompts) == 1
    [part] = _drafts(tmp_path)
    assert part["kind"] == "part" and part["covers"]["member_ids"] == pages and part["room_id"] == "1"
    assert part["author"]["writer"] == "fallback_part" and part["author"]["attribution"] == "helper draft, not lived"
    assert run.effect.startswith("a helper draft folded 2 records: ") and "next start re-measures its Max room" in run.effect
    assert not [r for r in ChronicleStore(tmp_path).records(kinds=("account",))]  # never an account across rooms


@pytest.mark.parametrize("episode", [{"zero_need": 1_100_000}, {"closed": True}, None])
def test_no_trigger_when_schemas_tip_it_the_episode_closed_or_none_exists(tmp_path, monkeypatch, light, episode):
    shared.world(tmp_path)
    _consciousness(monkeypatch, False)
    _main_pages(tmp_path)
    if episode is not None:
        _episode(tmp_path, **episode)
    llm = _Light(PART)
    assert _run(tmp_path, llm, direct=True).outcome == "nothing"  # the old selection: no shortage after a direct turn
    assert llm.prompts == [] and _drafts(tmp_path) == []


def test_with_consciousness_on_an_episode_drafts_no_acting_part(tmp_path, monkeypatch, light):
    shared.world(tmp_path)
    _consciousness(monkeypatch, True)
    _main_pages(tmp_path)
    _episode(tmp_path)
    llm = _Light(PART)
    assert _run(tmp_path, llm, direct=True).outcome == "consciousness_on"
    assert llm.prompts == [] and _drafts(tmp_path) == []

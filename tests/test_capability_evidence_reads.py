"""Readers of ``state/capability_evidence.json`` share one parse; its writers keep their fresh copy.

A context build asks the store for effort bands, rejected parameters, densities, owner
acknowledgements and the probe cache, five to six times per turn; writes come about twice
per model round.
"""

from __future__ import annotations

import json

import pytest

from ouroboros import capability_evidence as ce
from ouroboros import utils


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(utils, "_RACY_NS", 0)  # trust files as written (the clock-tick rule has its own case)
    ce.record_effort_ceiling(tmp_path, "openai/model", "high")
    ce.record_rejected_params(tmp_path, "openai/model", ["temperature"])
    return tmp_path


def _parses_of(monkeypatch, path) -> list:
    seen, real = [], json.loads
    monkeypatch.setattr(utils.json, "loads", lambda raw, *args, **kwargs: (
        seen.append(raw) if raw == path.read_text(encoding="utf-8") else None) or real(raw, *args, **kwargs))
    return seen


def test_a_turns_readers_share_one_parse_and_see_each_write(store, monkeypatch):
    parses = _parses_of(monkeypatch, ce._store_path(store))

    assert ce.get_effort_ceiling(store, "openai/model") == "high"
    assert ce.get_effort_floor(store, "openai/model") == ""
    assert ce.get_rejected_params(store, "openai/model") == {"temperature"}
    assert ce.get_token_density(store, "openai/model") == 0.0
    assert ce.list_owner_acks(store) == []
    assert ce.resolve_review_token_density(store, "openai/model")[1] == "cold_conservative"
    assert len(parses) == 1

    ce.record_effort_ceiling(store, "openai/model", "medium")  # the writer's rename moves the identity
    assert ce.get_effort_ceiling(store, "openai/model") == "medium"


def test_a_writers_copy_stays_invisible_until_it_saves(store, monkeypatch):
    assert ce.get_effort_ceiling(store, "openai/model") == "high"  # the shared parse is warm

    def refused(_root, _data):
        return False  # a failed save: _save reports it and the writer goes on

    with monkeypatch.context() as patch:
        patch.setattr(ce, "_save", refused)
        ce.record_effort_ceiling(store, "openai/model", "low")
        ce.record_rejected_params(store, "openai/model", ["top_p"])

    assert ce.get_effort_ceiling(store, "openai/model") == "high"
    assert ce.get_rejected_params(store, "openai/model") == {"temperature"}

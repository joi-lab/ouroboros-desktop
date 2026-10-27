"""A return to the primary resumes the view it showed before a configured route acted.

A narrower configured route rewrites the memory of every book in the plan; without the hold a return keeps that
narrowing for the rest of the task. The primary's own view is held at the first rebind that leaves it and resumed
when a rebind returns to it; its own lowering (start fit, real overflow) and my selected forms stay, and a switch
on the same route, or to a primary changed meanwhile, never raises the mode.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ouroboros import context, loop
from ouroboros import memory_floor as floor
from ouroboros.primary_route_observation import hold_selection, record_round_route_result
from ouroboros.tools.registry import ToolRegistry
from tests.test_memory_view_lever import plan as synthetic_plan
from tests.test_memory_view_modes import _built

WINDOWS = {"primary": 1_000_000, "reserve": 65_537}


@pytest.fixture
def routes(monkeypatch):
    """Each model's own window, as the route evidence states it."""
    monkeypatch.setattr(context, "_context_fit_route", lambda task, **_kw: (
        {"model": task["model"], "provider": "openrouter", "base_url": "", "use_local": False},
        SimpleNamespace(status="confirmed", stale=False, window_tokens=WINDOWS[task["model"]],
                        route_fp=f"{task['model']}-route")))


def _registry(tmp_path):
    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry._ctx.task_id, registry._ctx.event_queue = "k04", None
    registry._ctx.primary_route = {"model": "primary", "use_local": False, "role": "main"}
    registry._ctx.active_model, registry._ctx.active_use_local = "primary", False
    return registry


def _rebind(plan, registry, messages, model, mode):
    return loop._rebind_context_fit_plan(plan, registry, messages, model=model, use_local=False,
                                         start_mode=mode, tool_schemas=[])


def _switch(plan, registry, messages, model, mode):
    rebound, mode = _rebind(plan, registry, messages, model, mode)
    registry._ctx.active_model = model  # the walk adopts the route that answered
    return rebound, mode


def _leave(plan, registry, messages, model, mode):
    """The walk's order: the candidate's rebind, then its observed account's rebind, both before adoption."""
    away, mode = _rebind(plan, registry, messages, model, mode)
    return _switch(away, registry, messages, model, mode)


def test_a_return_resumes_the_primary_view_byte_for_byte(tmp_path, monkeypatch, routes):
    _core, build = _built(tmp_path, monkeypatch, ratio=1, page_chars=400_000)
    plan = build(WINDOWS["primary"])
    registry = _registry(tmp_path)
    messages = plan.messages_for(plan.initial_mode)
    shown = messages[0]
    assert plan.initial_mode == "max"
    away, away_mode = _leave(plan, registry, messages, "reserve", "max")
    assert away_mode in {"low", "nano"} and messages[0] != shown
    assert away.projection("max").memory_facts["floor"]["steps"]  # the reserve's cut reached every book
    back, back_mode = _switch(away, registry, messages, "primary", away_mode)
    assert back_mode == "max"
    assert messages[0] == shown  # without the hold the return keeps the reserve's narrow view
    assert back.projection("max").memory_facts["floor"]["steps"] == {}


def test_the_primary_own_low_stays_low_after_a_return(tmp_path, monkeypatch, routes):
    _core, build = _built(tmp_path, monkeypatch, books=600_000, ratio=1)
    plan = build(200_000)  # the primary's own window could not hold Max at the start
    monkeypatch.setitem(WINDOWS, "primary", 200_000)
    registry = _registry(tmp_path)
    messages = plan.messages_for(plan.initial_mode)
    assert plan.initial_mode == "low"
    away, away_mode = _leave(plan, registry, messages, "reserve", plan.initial_mode)
    _back, back_mode = _switch(away, registry, messages, "primary", away_mode)
    assert back_mode == "low"


def test_a_primary_changed_meanwhile_is_a_new_route(tmp_path, monkeypatch, routes):
    _core, build = _built(tmp_path, monkeypatch, ratio=1, page_chars=400_000)
    plan = build(WINDOWS["primary"])
    monkeypatch.setitem(WINDOWS, "successor", WINDOWS["primary"])
    registry = _registry(tmp_path)
    messages = plan.messages_for(plan.initial_mode)
    away, away_mode = _leave(plan, registry, messages, "reserve", "max")
    registry._ctx.primary_route = {"model": "successor", "use_local": False, "role": "main"}  # Settings changed it
    _new, new_mode = _switch(away, registry, messages, "successor", away_mode)
    assert new_mode == away_mode != "max"  # the old primary's view was never this route's own
    assert registry._ctx._primary_view is None


def test_a_switch_on_the_same_route_never_raises_the_mode(tmp_path, monkeypatch, routes):
    _core, build = _built(tmp_path, monkeypatch, ratio=1, page_chars=400_000)
    plan = build(WINDOWS["primary"])
    registry = _registry(tmp_path)
    narrowed = plan.reproject_for_route(window_tokens=WINDOWS["reserve"], known_window=True, ratio=1,
                                        output_reserve=65_536, tool_schemas=None, start_mode="max")
    messages = narrowed.messages_for(narrowed.initial_mode)
    # An account rotation or a wait's reprepare rebinds the acting primary itself: D2 keeps the shown mode.
    _same, mode = _switch(narrowed, registry, messages, "primary", narrowed.initial_mode)
    assert mode == narrowed.initial_mode != "max"
    assert getattr(registry._ctx, "_primary_view", None) is None


def test_forms_i_select_away_hold_on_the_primary_view():
    held, snap = synthetic_plan()
    owner = SimpleNamespace(_primary_view=(("primary", False), held))
    hold_selection(owner, ["F5:page-000"], owner_note=False)
    home, view = owner._primary_view
    assert home == ("primary", False) and "Page 0." in held.projection("max").system_content_json
    assert "Page 0." not in view.projection("max").system_content_json
    assert dict(floor.kept_level(snap, view.projection("max").memory_facts).addressed)["F5"] == ("page-000",)
    hold_selection(owner, ["core:BIBLE"], owner_note=False)  # a form the held view refuses releases the hold
    assert owner._primary_view is None


def test_the_primary_answering_releases_the_hold(tmp_path):
    owner = SimpleNamespace(primary_route={"model": "primary", "use_local": False, "role": "main"},
                            _primary_view=object(), _primary_return_requested=None, _execution_trace={})
    usage = {"_last_round_cache_usage": {"round": 3, "model": "primary"}, "_model_route": {}}
    call = SimpleNamespace(tools=SimpleNamespace(_ctx=owner), active_model="primary", active_use_local=False,
                           accumulated_usage=usage, round_idx=3)
    record_round_route_result(call, "main", accepted=True)
    assert owner._primary_view is None

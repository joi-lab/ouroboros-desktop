"""Room Off refuses new helpers while admitted work and the review pool still run."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros import config, subagent_runtime as runtime
from ouroboros.composer_cognition import update_project_cognition
from ouroboros.projects_registry import create_project
from ouroboros.settings_integrity import live_subagents_mode, task_settings_scope
from tests.test_project_cognition_runtime import snapshot
from tests.test_task_settings_snapshot import document, isolated_runtime, save_owner  # noqa: F401


def configured():
    values = document("old")
    catalog = json.loads(values["OUROBOROS_SUBAGENTS"])
    catalog["items"][0]["delivery"] = "packet"
    catalog["items"].append({"subagent_id": "session", "recommended_use": "Implementation",
        "route": {"kind": "agent_session", "target_id": "codex=gpt-5.6-sol", "credential_profile_id": "profile-1"}})
    values["OUROBOROS_SUBAGENTS"] = json.dumps(catalog)
    save_owner(values)
    create_project(config.DATA_DIR, "alpha")
    return values


def test_live_off_refuses_schedule_and_session_but_same_task_review_runs(monkeypatch):
    from ouroboros import review_substrate
    from ouroboros.tools import plan_review_runtime
    from ouroboros.tools.registry import ToolContext, ToolRegistry
    from ouroboros.tools import delegate
    from ouroboros.delegate_shared import delegate_result

    configured()
    task = {"id": "parent", "project_id": "alpha"}
    ctx = ToolContext(repo_dir=config.REPO_DIR, drive_root=config.DATA_DIR, task_id="parent")
    ctx.task_metadata = {"project_id": "alpha"}
    registry = ToolRegistry(repo_dir=config.REPO_DIR, drive_root=config.DATA_DIR)
    registry.set_context(ctx)
    started, reviewed = [], []

    def start(*args, **kwargs):
        started.append(True)
        return delegate_result({"status": "started", "run_id": "existing-run"})

    def review(request, *, slots, **kwargs):
        reviewed.extend(slot.slot_id for slot in slots)
        return SimpleNamespace(actors=[{"slot_id": s.slot_id, "model": s.model, "status": "ok",
            "raw_text": '[{"item":"x","verdict":"PASS","severity":"advisory","reason":"checked"}]',
            "usage": {}, "prompt_ref": {}, "response_ref": {}} for s in slots])

    monkeypatch.setattr(delegate, "_delegate_start", start)
    monkeypatch.setattr(review_substrate, "run_review_request", review)
    monkeypatch.setattr(plan_review_runtime, "LLMClient", lambda *a, **k: object())
    with task_settings_scope(snapshot(task)):
        admitted, _ = runtime.select_subagent_snapshot(config.runtime_settings(), subagent_id="session")
        for mode in ("auto", "swarm", "off", "auto"):
            update_project_cognition(config.DATA_DIR, "alpha", subagents_mode=mode)
            assert live_subagents_mode() == mode
            result = runtime.exact_start(ctx, "work", {"subagent_id": "session"})
            if mode == "off":
                assert "subagents_off_in_room" in result.text
                assert runtime.SUBAGENTS_OFF_DETAIL in result.text
                scheduled = registry.execute_result("schedule_subagent", {
                    "subagent_id": "critic", "objective": "Check", "expected_output": "Findings"})
                assert scheduled.meta["reason"] == "subagents_off_in_room"
                assert not runtime.current_model_visible_subagent_catalog()
                assert runtime.validate_subagent_snapshot(admitted) == admitted
                slots = plan_review_runtime.plan_review_slots()
                raw = asyncio.run(plan_review_runtime.run_plan_review_slots(
                    ctx, slots, system_prompt="Review", user_content="Changes"))
                assert reviewed == ["critic"] and raw[0]["slot_id"] == "critic"
                assert not raw[0]["error"]
            else:
                assert "started" in result.text
                assert runtime.select_subagent_snapshot(config.runtime_settings(), subagent_id="critic")[0]
                assert runtime.current_model_visible_subagent_catalog()
        assert len(started) == 3


@pytest.mark.parametrize("task", [
    {"type": "evolution", "metadata": {"work_kind": "owner"}},
    {"metadata": {"evolution_root_task_id": "evolve", "work_kind": "owner"}},
    {"metadata": {"initiator": "consciousness"}},
    {"metadata": {"work_kind": "consciousness"}},
])
def test_autonomous_work_keeps_its_own_controls(task):
    configured()
    update_project_cognition(config.DATA_DIR, "alpha", subagents_mode="off")
    task = {**task, "id": "autonomous", "project_id": "alpha"}
    with task_settings_scope(snapshot(task)):
        assert live_subagents_mode() == "auto"
        assert runtime.select_subagent_snapshot(config.runtime_settings(), subagent_id="critic")[0]
    assert live_subagents_mode(task={"id": "owner", "project_id": "alpha"}) == "off"


def test_an_admitted_child_finishes_its_bound_session_after_off(monkeypatch):
    from ouroboros.tools import delegate
    from ouroboros.delegate_shared import delegate_result

    configured()
    with task_settings_scope(snapshot({"id": "child", "project_id": "alpha"})):
        admitted, _ = runtime.select_subagent_snapshot(config.runtime_settings(), subagent_id="session")
        ctx = SimpleNamespace(task_id="child", drive_root=config.DATA_DIR,
                              _configured_actor_bootstrap={"zero_run_receipt_recorded": False})
        monkeypatch.setattr("ouroboros.subagent_bootstrap._durable_zero_run_receipt", lambda *a, **k: {})
        monkeypatch.setattr(delegate, "_delegate_start", lambda *a, **k: delegate_result({"status": "started", "run_id": "run"}))
        update_project_cognition(config.DATA_DIR, "alpha", subagents_mode="off")
        result = runtime.exact_start(ctx, "admitted work", {"snapshot": admitted, "compiled_work_order": True})
        assert "started" in result.text and ctx._configured_actor_bootstrap["physical_started"]
        assert "subagents_off_in_room" in runtime.exact_start(ctx, "new work", {"subagent_id": "session"}).text


@pytest.mark.parametrize("mode", ["off", "auto", "swarm"])
@pytest.mark.parametrize("inputs", ["shared", "declared"])
def test_both_context_builders_omit_only_the_off_catalog(mode, inputs, monkeypatch, tmp_path):
    from ouroboros.context import _capture_context_core
    from ouroboros.memory import Memory
    from tests.test_available_subagents_catalog import _context_env

    values = configured()
    values["OUROBOROS_SUBAGENTS_MODE"] = mode
    save_owner(values)
    env = _context_env(tmp_path)
    task = {"id": "task", "type": "task", "text": "work", "delegation_role": "subagent",
            "task_contract": {"input_sources": inputs},
            "configured_subagent": {"route": {"kind": "api_model", "target_id": "fixture"}}}
    core = _capture_context_core(env, Memory(drive_root=tmp_path), task, None, None)
    assert ("## Available subagents" in core.semi_stable_text) is (mode != "off")
    assert "Review" in core.semi_stable_text


@pytest.mark.parametrize("mode", ["off", "auto", "swarm"])
def test_host_rechecks_room_mode_on_delayed_admission(mode):
    from supervisor import events
    from tests.test_nested_rights_depth import _fake_ctx, _schedule_event
    from ouroboros.task_results import load_task_result

    values = configured()
    values["OUROBOROS_SUBAGENTS_MODE"] = mode
    save_owner(values)
    enqueued = []
    ctx = _fake_ctx(config.DATA_DIR, enqueued)
    events._handle_schedule_task(_schedule_event("new-child", "parent", drive_root=config.DATA_DIR), ctx)
    if mode == "off":
        assert not enqueued
        assert load_task_result(config.DATA_DIR, "new-child")["reason_code"] == "subagents_off_in_room"
    else:
        assert len(enqueued) == 1


def test_global_settings_off_withholds_new_admission_from_running_actor():
    values = configured()
    with task_settings_scope(snapshot({"id": "parent", "project_id": "alpha"})):
        catalog = json.loads(values["OUROBOROS_SUBAGENTS"])
        catalog["enabled"] = False
        save_owner({**values, "OUROBOROS_SUBAGENTS": json.dumps(catalog)})
        assert not runtime.current_model_visible_subagent_catalog()
        with pytest.raises(runtime.SubagentSelectionError) as refused:
            runtime.select_subagent_snapshot(config.runtime_settings(), subagent_id="critic")
        assert refused.value.code == "subagents_disabled"

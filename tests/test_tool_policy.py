"""Tests for task-start tool visibility policy."""

import inspect
import pathlib
import tempfile

import ouroboros.loop as loop_mod
from ouroboros.tool_policy import CORE_TOOL_NAMES, initial_tool_schemas, list_non_core_tools
from ouroboros.tools.registry import ToolRegistry


def _build_registry() -> ToolRegistry:
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolRegistry(repo_dir=tmp, drive_root=tmp)


def test_core_surface_includes_user_message_and_media():
    assert "send_photo" in CORE_TOOL_NAMES
    assert "send_video" in CORE_TOOL_NAMES
    assert "send_user_message" in CORE_TOOL_NAMES


def test_initial_tool_schemas_include_media_and_meta_tools():
    registry = _build_registry()
    names = {schema["function"]["name"] for schema in initial_tool_schemas(registry)}
    assert "send_photo" in names
    assert "list_available_tools" in names
    assert "enable_tools" in names


def test_non_core_listing_excludes_core_media_tools():
    registry = _build_registry()
    names = {entry["name"] for entry in list_non_core_tools(registry)}
    assert "send_photo" not in names
    assert "plan_task" in names


def test_loop_bootstraps_from_tool_policy():
    source = inspect.getsource(loop_mod)
    assert "initial_tool_schemas(tools)" in source
    assert "schemas(core_only=True)" not in source


def test_advisory_tools_in_core_tool_names():
    """advisory_review and review_status must be core tools."""
    assert "advisory_review" in CORE_TOOL_NAMES
    assert "review_status" in CORE_TOOL_NAMES


def test_advisory_tools_in_initial_schemas():
    """advisory_review and review_status must appear in initial tool schemas."""
    registry = _build_registry()
    names = {schema["function"]["name"] for schema in initial_tool_schemas(registry)}
    assert "advisory_review" in names
    assert "review_status" in names


def test_heal_skill_tools_are_core_visible_in_initial_schemas():
    """Heal prompts must be able to call skill_review and
    skill_preflight without enable_tools (enable_tools is blocked in heal
    mode). Pin both the tool_policy SSOT and registry.core_only fallback."""
    assert "skill_review" in CORE_TOOL_NAMES
    assert "skill_preflight" in CORE_TOOL_NAMES
    registry = _build_registry()
    names = {schema["function"]["name"] for schema in initial_tool_schemas(registry)}
    assert "skill_review" in names
    assert "skill_preflight" in names
    core_only_names = {schema["function"]["name"] for schema in registry.schemas(core_only=True)}
    assert "skill_review" in core_only_names
    assert "skill_preflight" in core_only_names


def test_enable_tools_does_not_duplicate_active_tool_schemas():
    registry = _build_registry()
    tool_schemas = initial_tool_schemas(registry)
    messages = []
    tool_schemas, _enabled_extra = loop_mod._setup_dynamic_tools(registry, tool_schemas, messages)

    core_result = registry.execute("enable_tools", {"tools": "advisory_review"})
    names_after_core = [schema["function"]["name"] for schema in tool_schemas]
    assert names_after_core.count("advisory_review") == 1
    assert "already active" in core_result

    extra_result = registry.execute("enable_tools", {"tools": "plan_task"})
    names_after_extra = [schema["function"]["name"] for schema in tool_schemas]
    assert names_after_extra.count("plan_task") == 1
    assert "Enabled: plan_task" in extra_result

    extra_again_result = registry.execute("enable_tools", {"tools": "plan_task"})
    names_after_extra_again = [schema["function"]["name"] for schema in tool_schemas]
    assert names_after_extra_again.count("plan_task") == 1
    assert "already active" in extra_again_result


def test_list_available_tools_hides_enabled_extra_tools():
    registry = _build_registry()
    tool_schemas = initial_tool_schemas(registry)
    messages = []
    loop_mod._setup_dynamic_tools(registry, tool_schemas, messages)

    before = registry.execute("list_available_tools", {})
    assert "plan_task" in before

    registry.execute("enable_tools", {"tools": "plan_task"})
    after = registry.execute("list_available_tools", {})
    assert "plan_task" not in after


def test_live_extension_tools_are_initial_not_non_core(monkeypatch):
    from ouroboros import extension_loader

    registry = _build_registry()
    tool_name = extension_loader.extension_surface_name("weather", "forecast")
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "handler": lambda ctx: "ok",
            "description": "Forecast",
            "schema": {"type": "object", "properties": {}},
            "timeout_sec": 5,
            "skill": "weather",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_a, **_k: True)
    try:
        initial_names = {schema["function"]["name"] for schema in initial_tool_schemas(registry)}
        non_core_names = {entry["name"] for entry in list_non_core_tools(registry)}
        assert tool_name in initial_names
        assert tool_name in non_core_names
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


def test_list_skills_is_core_visible_for_repair(tmp_path):
    from ouroboros.tools.registry import ToolRegistry
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    names = {schema.get("name") or schema.get("function", {}).get("name") for schema in registry.schemas(core_only=True)}
    assert "list_skills" in names

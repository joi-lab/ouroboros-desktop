"""Representative calls retain the executable contracts of the edited descriptions.

Six of the seven remain; the seventh tool, task_acceptance_review, left with the review fold.
"""

import pytest

from ouroboros.tools.registry import ToolRegistry


@pytest.mark.parametrize("constraint", [
    {}, {"mode": "local_readonly_subagent"},
    {"mode": "acting_subagent", "surface": "self_worktree", "root_task_id": "projection"},
], ids=["full", "readonly", "acting"])
def test_schedule_readonly_choice_survives_custom_projection(tmp_path, constraint):
    from ouroboros.openai_chat_custom import project_function_tools_to_openai_custom

    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    registry._ctx.task_constraint = constraint
    schema = next(tool for tool in registry.schemas() if tool["function"]["name"] == "schedule_subagent")
    function = schema["function"]
    # Normal/pruned function schemas carry parameter prose; the custom codec
    # strips it and truncates the tool prose to fit its description bound.
    assert "Select readonly for read-only session execution" in function["parameters"]["properties"]["access"]["description"]
    catalog = project_function_tools_to_openai_custom([schema])
    description = catalog.wire_tools()[0]["custom"]["description"]
    assert "access='readonly' selects read-only session execution" in description
    assert "evidence-only private checking copy" in description
    assert catalog.schema_binding("schedule_subagent").schema() == function["parameters"]


@pytest.mark.serial
@pytest.mark.parametrize("name,arguments,invalid_field,invalid_value", [
    ("schedule_subagent", {"subagent_id": "actor-1", "objective": "Inspect interfaces",
                          "expected_output": "Findings", "role": "", "write_surface": "read_only"},
     "write_surface", "invented_surface"),
    ("plan_task", {"goal": "Repair interfaces", "plan": "Inspect and verify", "affected_paths": [],
                   "done_when": [{"id": "c1", "claim": "Interfaces remain compatible", "support": "run the suite"}],
                   "ask": "council", "message": "First look."},
     "ask", "everyone"),
    ("delegate_start", {"prompt": "New evidence", "subagent_id": "actor-1",
                        "continue_from": "settled-run", "continue_carrier": "packet"},
     "continue_carrier", "restart"),
    ("verify_and_record", {"contract_kind": "explicit_command", "check": ["python", "check.py"],
                           "expected_match": "bytes_equal", "artifact_paths": ["actual", "expected"]},
     "expected_match", "approximate"),
    ("start_task", {"objective": "Inspect supplied project", "title": "Supplied project inspection",
                              "workspace": "none", "context_requires_self_body_docs": False},
     "context_requires_self_body_docs", "false"),
    ("preflight_review", {"commit_message": "Repair interfaces", "deterministic_only": True,
                          "source": "index"},
     "source", "head"),
])
def test_edited_description_schemas_keep_representative_call_shapes(
        tmp_path, name, arguments, invalid_field, invalid_value):
    """Exercise the exported parameter contracts, including continuation aliases."""
    from jsonschema import Draft7Validator

    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    parameters = registry.get_schema_by_name(name)["function"]["parameters"]
    Draft7Validator.check_schema(parameters)
    validator = Draft7Validator(parameters)
    assert not list(validator.iter_errors(arguments))
    assert list(validator.iter_errors({**arguments, invalid_field: invalid_value}))

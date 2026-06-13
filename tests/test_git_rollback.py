"""Tests for ouroboros/tools/git_rollback.py — rollback_to_target tool."""

import subprocess
import types
from unittest import mock

import pytest

from ouroboros.tools.registry import ToolContext


@pytest.fixture
def ctx(tmp_path):
    """Minimal ToolContext pointing at a temporary git repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, capture_output=True)
    (repo / "f.txt").write_text("init")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, capture_output=True)
    return ToolContext(repo_dir=str(repo), drive_root=str(tmp_path / "data"))


def test_rollback_empty_target(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    result = _rollback_to_target(ctx, target="", confirm=True)
    assert "ROLLBACK_ERROR" in result
    assert "required" in result


def test_rollback_preview_valid_sha(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ctx.repo_dir, capture_output=True, text=True,
    ).stdout.strip()
    result = _rollback_to_target(ctx, target=sha, confirm=False)
    assert "Will reset" in result
    assert sha[:8] in result
    assert "confirm=true" in result


def test_rollback_preview_invalid_target(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    result = _rollback_to_target(ctx, target="nonexistent_tag_xyz", confirm=False)
    assert "ROLLBACK_ERROR" in result
    assert "Cannot resolve" in result


def test_rollback_confirm_calls_git_ops(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    with mock.patch(
        "ouroboros.tools.git_rollback.rollback_to_version",
        create=True,
    ) as mock_rb:
        # Simulate successful rollback via git_ops import inside function
        mock_rb.return_value = (True, "Rolled back to abc123 (abc12345)")
        # We need to patch the import inside the function
        fake_git_ops = types.ModuleType("supervisor.git_ops")
        fake_git_ops.rollback_to_version = mock_rb
        with mock.patch.dict("sys.modules", {"supervisor.git_ops": fake_git_ops}):
            result = _rollback_to_target(ctx, target="HEAD", confirm=True)
    assert "abc12345" in result or "Rolled back" in result


def test_rollback_confirm_failure(ctx):
    from ouroboros.tools.git_rollback import _rollback_to_target
    fake_git_ops = types.ModuleType("supervisor.git_ops")
    fake_git_ops.rollback_to_version = mock.Mock(
        return_value=(False, "Cannot resolve badref")
    )
    with mock.patch.dict("sys.modules", {"supervisor.git_ops": fake_git_ops}):
        result = _rollback_to_target(ctx, target="badref", confirm=True)
    assert "ROLLBACK_ERROR" in result


def test_get_tools_exports():
    from ouroboros.tools.git_rollback import get_tools
    tools = get_tools()
    assert len(tools) == 1
    assert tools[0].name == "vcs_rollback"
    schema = tools[0].schema
    assert "target" in schema["parameters"]["properties"]
    assert "confirm" in schema["parameters"]["properties"]


def test_rollback_in_core_tool_names():
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "vcs_rollback" in CORE_TOOL_NAMES


def test_rollback_in_initial_tool_schemas():
    """rollback_to_target must be visible in the runtime initial_tool_schemas path."""
    import pathlib, tempfile
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import initial_tool_schemas
    with tempfile.TemporaryDirectory() as d:
        reg = ToolRegistry(repo_dir=pathlib.Path(d), drive_root=pathlib.Path(d))
        schemas = initial_tool_schemas(reg)
        names = {s["function"]["name"] for s in schemas}
        assert "vcs_rollback" in names


def test_rollback_requests_restart_on_success(ctx):
    """After successful rollback, ctx.pending_restart_reason is set."""
    from ouroboros.tools.git_rollback import _rollback_to_target
    import types
    fake_git_ops = types.ModuleType("supervisor.git_ops")
    fake_git_ops.rollback_to_version = mock.Mock(
        return_value=(True, "Rolled back to abc123")
    )
    with mock.patch.dict("sys.modules", {"supervisor.git_ops": fake_git_ops}):
        result = _rollback_to_target(ctx, target="HEAD", confirm=True)
    assert "restart has been requested" in result
    assert ctx.pending_restart_reason == "rollback_to_target completed"

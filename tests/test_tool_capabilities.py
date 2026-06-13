"""Tests for tool capability SSOT and no-drift invariants.

Verifies:
- tool_capabilities.py is the single source of truth
- tool_policy.py imports from capabilities (no local copy)
- loop_tool_execution.py imports from capabilities (no local copy)
- search_code is classified correctly
- run_shell list-cmd happy path (string-cmd cascade lives in test_shell_run_shell.py)
- search_code tool works
"""
import inspect
import os
import pathlib
import re
import tempfile

import pytest

# ---------------------------------------------------------------------------
# SSOT drift tests
# ---------------------------------------------------------------------------


def test_tool_policy_imports_from_capabilities():
    """tool_policy.py must import CORE_TOOL_NAMES from tool_capabilities, not define its own."""
    import ouroboros.tool_policy as tp
    source = inspect.getsource(tp)
    assert "from ouroboros.tool_capabilities import" in source
    # Must NOT define its own frozenset of core tools
    assert "CORE_TOOL_NAMES" not in source.split("from ouroboros.tool_capabilities")[0]


def test_loop_execution_imports_from_capabilities():
    """loop_tool_execution.py must import sets from tool_capabilities."""
    import ouroboros.loop_tool_execution as lte
    source = inspect.getsource(lte)
    assert "from ouroboros.tool_capabilities import" in source
    # Must NOT have local frozenset definitions for these sets
    for name in ("READ_ONLY_PARALLEL_TOOLS", "STATEFUL_BROWSER_TOOLS",
                 "_UNTRUNCATED_TOOL_RESULTS", "_UNTRUNCATED_REPO_READ_PATHS"):
        # Check there's no local `X = frozenset({` pattern
        pattern = rf'^{re.escape(name)}\s*[:=]\s*frozenset'
        assert not re.search(pattern, source, re.MULTILINE), (
            f"{name} is locally defined in loop_tool_execution.py — should import from tool_capabilities"
        )


def test_capabilities_sets_are_frozensets():
    """All exported sets must be frozensets (immutable)."""
    from ouroboros.tool_capabilities import (
        CORE_TOOL_NAMES, META_TOOL_NAMES, READ_ONLY_PARALLEL_TOOLS,
        STATEFUL_BROWSER_TOOLS, UNTRUNCATED_TOOL_RESULTS,
        UNTRUNCATED_REPO_READ_PATHS,
    )
    for name, obj in [
        ("CORE_TOOL_NAMES", CORE_TOOL_NAMES),
        ("META_TOOL_NAMES", META_TOOL_NAMES),
        ("READ_ONLY_PARALLEL_TOOLS", READ_ONLY_PARALLEL_TOOLS),
        ("STATEFUL_BROWSER_TOOLS", STATEFUL_BROWSER_TOOLS),
        ("UNTRUNCATED_TOOL_RESULTS", UNTRUNCATED_TOOL_RESULTS),
        ("UNTRUNCATED_REPO_READ_PATHS", UNTRUNCATED_REPO_READ_PATHS),
    ]:
        assert isinstance(obj, frozenset), f"{name} must be a frozenset"


def test_policy_and_capabilities_core_names_identical():
    """The CORE_TOOL_NAMES used by tool_policy must be the exact same object."""
    from ouroboros.tool_policy import CORE_TOOL_NAMES as policy_names
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES as cap_names
    assert policy_names is cap_names


def test_loop_execution_parallel_tools_from_capabilities():
    """READ_ONLY_PARALLEL_TOOLS in loop_tool_execution is from capabilities."""
    from ouroboros.loop_tool_execution import READ_ONLY_PARALLEL_TOOLS as loop_set
    from ouroboros.tool_capabilities import READ_ONLY_PARALLEL_TOOLS as cap_set
    assert loop_set is cap_set


# ---------------------------------------------------------------------------
# search_code classification tests
# ---------------------------------------------------------------------------


def test_search_code_in_core_tools():
    """search_code must be in CORE_TOOL_NAMES."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "search_code" in CORE_TOOL_NAMES


def test_search_code_is_parallel_safe():
    """search_code must be in READ_ONLY_PARALLEL_TOOLS."""
    from ouroboros.tool_capabilities import READ_ONLY_PARALLEL_TOOLS
    assert "search_code" in READ_ONLY_PARALLEL_TOOLS


def test_search_code_has_result_limit():
    """search_code must have an explicit result size limit."""
    from ouroboros.tool_capabilities import TOOL_RESULT_LIMITS
    assert "search_code" in TOOL_RESULT_LIMITS
    from ouroboros.tool_capabilities import UNTRUNCATED_TOOL_RESULTS
    assert "plan_task" in UNTRUNCATED_TOOL_RESULTS
    from ouroboros.tool_capabilities import FOREGROUND_MUTATIVE_TOOLS
    assert "claude_code_edit" in FOREGROUND_MUTATIVE_TOOLS


# ---------------------------------------------------------------------------
# search_code tool behavior tests
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext
    from unittest.mock import MagicMock
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = tmp_path
    ctx.repo_path = lambda p: tmp_path / p
    return ctx


def _populate_repo(tmp_path):
    """Create a mini repo structure for search tests."""
    (tmp_path / "foo.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (tmp_path / "bar.py").write_text("import os\ndef hello_bar():\n    pass\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "baz.py").write_text("class MyClass:\n    hello = True\n", encoding="utf-8")
    # Binary-like file (should be skipped)
    (tmp_path / "data.png").write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)
    # Cache dir (should be skipped)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "foo.cpython-310.pyc").write_bytes(b'\x00' * 50)


def test_code_search_literal(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "hello")
    assert "foo.py:1:" in result
    assert "bar.py:2:" in result
    assert "sub/baz.py:2:" in result


def test_code_search_regex(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, r"def \w+\(\)", regex=True)
    assert "foo.py:1:" in result
    assert "bar.py:2:" in result


def test_code_search_scoped_path(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "hello", path="sub")
    assert "sub/baz.py" in result
    assert "foo.py" not in result


def test_code_search_include_filter(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    (tmp_path / "readme.md").write_text("hello from markdown\n", encoding="utf-8")
    result = _code_search(ctx, "hello", include="*.md")
    assert "readme.md" in result
    assert "foo.py" not in result


def test_code_search_no_matches(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "zzz_nonexistent_zzz")
    assert "No matches found" in result


def test_code_search_skips_binaries(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "PNG")
    # .png file should be skipped even though it contains "PNG" bytes
    assert "data.png" not in result


def test_code_search_skips_cache_dirs(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "foo")
    assert "__pycache__" not in result


def test_code_search_max_results(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    # Create many matching lines
    lines = "\n".join(f"match_line_{i}" for i in range(50))
    (tmp_path / "many.py").write_text(lines, encoding="utf-8")
    result = _code_search(ctx, "match_line", max_results=10)
    assert "truncated at 10" in result


def test_code_search_empty_query(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    result = _code_search(ctx, "")
    assert "SEARCH_ERROR" in result


def test_code_search_invalid_regex(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    result = _code_search(ctx, "[invalid", regex=True)
    assert "SEARCH_ERROR" in result


# ---------------------------------------------------------------------------
# run_shell string contract
# ---------------------------------------------------------------------------
#
# String-cmd recovery (shlex.split for plain strings, json.loads for JSON
# arrays, ast.literal_eval for Python literals) is covered by
# tests/test_shell_run_shell.py::TestShellArgContract.  This file keeps only
# the list-cmd happy-path sibling so the capability sets module owns the
# round-1 tool surface assertions, not the string-cascade contract itself.


def test_run_shell_list_cmd_works(tmp_path):
    """run_shell with a list cmd should work normally."""
    from ouroboros.tools.shell import _run_shell
    from unittest.mock import MagicMock
    from ouroboros.tools.registry import ToolContext
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = tmp_path
    ctx.drive_logs.return_value = tmp_path
    result = _run_shell(ctx, ["echo", "hello"])
    assert "hello" in result


# ---------------------------------------------------------------------------
# Initial tool visibility
# ---------------------------------------------------------------------------


def test_search_code_in_initial_schemas():
    """search_code must appear in initial tool schemas."""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import initial_tool_schemas
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {s["function"]["name"] for s in initial_tool_schemas(registry)}
    assert "search_code" in names


def test_search_code_registered():
    """search_code must be registered in the tool registry."""
    from ouroboros.tools.registry import ToolRegistry
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    available = {t["function"]["name"] for t in registry.schemas()}
    assert "search_code" in available


# ---------------------------------------------------------------------------
# schedule_subagent core classification tests
# ---------------------------------------------------------------------------


def test_schedule_subagent_in_core():
    """schedule_subagent is core for first-class parallel delegation."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "schedule_subagent" in CORE_TOOL_NAMES


def test_wait_task_in_core():
    """wait_task/wait_tasks are core so delegated work can be joined."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "wait_task" in CORE_TOOL_NAMES
    assert "wait_tasks" in CORE_TOOL_NAMES


def test_get_task_result_in_core():
    """get_task_result is core so child handoffs can be read."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "get_task_result" in CORE_TOOL_NAMES


def test_schedule_subagent_available_in_registry():
    """schedule_subagent must still be registered."""
    from ouroboros.tools.registry import ToolRegistry
    import pathlib, tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    all_names = {t["function"]["name"] for t in registry.schemas()}
    assert "schedule_subagent" in all_names, (
        "schedule_subagent must be discoverable via list_available_tools / enable_tools"
    )


def test_schedule_subagent_in_initial_schemas():
    """schedule_subagent appears in parent initial schemas as a core tool."""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import initial_tool_schemas
    import pathlib, tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {s["function"]["name"] for s in initial_tool_schemas(registry)}
    assert "schedule_subagent" in names


def test_local_readonly_subagent_initial_schemas_are_allowlisted(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tool_capabilities import LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    from ouroboros.tool_policy import initial_tool_schemas, list_non_core_tools
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(
        ToolContext(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
        )
    )

    names = {s["function"]["name"] for s in initial_tool_schemas(registry)}
    assert LOCAL_READONLY_SUBAGENT_TOOL_NAMES <= names
    assert "enable_tools" not in names
    assert "schedule_subagent" not in names
    assert "write_file" not in names
    assert "run_command" not in names
    assert "browse_page" in names
    assert "browser_action" in names
    schemas = {s["function"]["name"]: s["function"] for s in initial_tool_schemas(registry)}
    for tool_name in ("read_file", "list_files", "search_code"):
        root_enum = schemas[tool_name]["parameters"]["properties"]["root"]["enum"]
        assert "user_files" not in root_enum
    assert set(schemas["search_code"]["parameters"]["properties"]["root"]["enum"]) == {"active_workspace", "system_repo"}
    action_schema = schemas["browser_action"]["parameters"]["properties"]["action"]
    assert "evaluate" not in action_schema["enum"]
    assert "send_photo" not in schemas["browse_page"]["description"]
    assert "analyze_screenshot" in schemas["browse_page"]["description"]
    assert list_non_core_tools(registry) == []


def test_local_readonly_subagent_execute_blocks_forbidden_tools(tmp_path, monkeypatch):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext, ToolRegistry
    import ouroboros.mcp_client as mcp_client

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(
        ToolContext(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
        )
    )

    assert registry.get_schema_by_name("write_file") is None
    assert registry.get_schema_by_name("enable_tools") is None
    assert registry.get_schema_by_name("schedule_subagent") is None
    monkeypatch.setattr(mcp_client, "ensure_configured_from_settings", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("MCP touched")))
    assert "LOCAL_READONLY_SUBAGENT_BLOCKED" not in registry.execute("list_files", {"dir": "."})
    blocked_tools = [
        "write_file",
        "edit_text",
        "claude_code_edit",
        "knowledge_write",
        "update_scratchpad",
        "update_identity",
        "commit_reviewed",
        "advisory_review",
        "task_acceptance_review",
        "skill_review",
        "request_restart",
        "switch_model",
        "enable_tools",
        "schedule_subagent",
        "run_command",
        "skill_exec",
        "list_skills",
        "ext_4_demo_tool",
        "mcp_demo_tool",
    ]
    for name in blocked_tools:
        assert registry.get_schema_by_name(name) is None
        assert "LOCAL_READONLY_SUBAGENT_BLOCKED" in registry.execute(name, {})


def test_local_readonly_subagent_data_read_denies_secret_files(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    (tmp_path / "settings.json").write_text('{"OPENROUTER_API_KEY":"secret"}', encoding="utf-8")
    (tmp_path / "settings.tmp").write_text('{"OPENROUTER_API_KEY":"secret"}', encoding="utf-8")
    (tmp_path / ".settings.json.tmp.123").write_text('{"OPENROUTER_API_KEY":"secret"}', encoding="utf-8")
    (tmp_path / ".env.local").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "prod.env").write_text("TOKEN=secret", encoding="utf-8")
    (tmp_path / "state" / "skills" / "weather").mkdir(parents=True)
    (tmp_path / "state" / "skills" / "weather" / "grants.json").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "skills" / "weather" / ".grants.json.tmp.123").write_text("{}", encoding="utf-8")
    (tmp_path / "state" / "skills" / "weather" / "review.json.lock").write_text("{}", encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "events.jsonl").write_text("{}", encoding="utf-8")
    try:
        os.symlink("settings.json", tmp_path / "alias.txt")
    except (OSError, NotImplementedError):
        pass
    try:
        os.link(tmp_path / "settings.json", tmp_path / "hardlink.txt")
    except (OSError, NotImplementedError):
        pass

    registry = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    registry.set_context(
        ToolContext(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
        )
    )

    blocked = registry.execute("read_file", {"root": "runtime_data", "path": "settings.json"})
    assert "DATA_READ_BLOCKED" in blocked
    assert "DATA_READ_BLOCKED" in registry.execute("read_file", {"root": "runtime_data", "path": "settings.tmp"})
    assert "DATA_READ_BLOCKED" in registry.execute("read_file", {"root": "runtime_data", "path": ".settings.json.tmp.123"})
    assert "DATA_READ_BLOCKED" in registry.execute("read_file", {"root": "runtime_data", "path": ".env.local"})
    assert "DATA_READ_BLOCKED" in registry.execute("read_file", {"root": "runtime_data", "path": "prod.env"})
    assert "DATA_READ_BLOCKED" in registry.execute("read_file", {"root": "runtime_data", "path": "state/skills/weather/.grants.json.tmp.123"})
    assert "DATA_READ_BLOCKED" in registry.execute("read_file", {"root": "runtime_data", "path": "state/skills/weather/review.json.lock"})
    alias_result = registry.execute("read_file", {"root": "runtime_data", "path": "alias.txt"})
    if (tmp_path / "alias.txt").exists():
        assert "DATA_READ_BLOCKED" in alias_result
    hardlink_result = registry.execute("read_file", {"root": "runtime_data", "path": "hardlink.txt"})
    if (tmp_path / "hardlink.txt").exists():
        assert "DATA_READ_BLOCKED" in hardlink_result
    listing = registry.execute("list_files", {"root": "runtime_data", "dir": "."})
    assert "settings.json" not in listing
    assert "settings.tmp" not in listing
    assert ".settings.json.tmp.123" not in listing
    assert ".env.local" not in listing
    assert "prod.env" not in listing
    assert "alias.txt" not in listing
    assert "hardlink.txt" not in listing
    assert "secret/control" in listing
    skill_state_listing = registry.execute("list_files", {"root": "runtime_data", "dir": "state/skills/weather"})
    assert "grants.json" not in skill_state_listing
    assert ".grants.json.tmp.123" not in skill_state_listing
    assert "review.json.lock" not in skill_state_listing
    assert "secret/control" in skill_state_listing
    assert "DATA_LIST_BLOCKED" in registry.execute("list_files", {"root": "runtime_data", "dir": "state/skills/weather/grants.json"})
    assert "DATA_LIST_BLOCKED" in registry.execute("list_files", {"root": "runtime_data", "dir": "state/skills/weather/.grants.json.tmp.123"})
    readable = registry.execute("read_file", {"root": "runtime_data", "path": "logs/events.jsonl"})
    assert "{}" in readable


def test_local_readonly_subagent_repo_read_denies_secret_files(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    repo = tmp_path / "repo"
    data = tmp_path / "data"
    (repo / ".git").mkdir(parents=True)
    data.mkdir()
    (repo / ".git" / "credentials").write_text("https://token@example.invalid\n", encoding="utf-8")
    (repo / ".git" / "config").write_text("[credential]\n", encoding="utf-8")
    (repo / ".env.local").write_text("TOKEN=secret\nLEAK_MARKER=env\n", encoding="utf-8")
    (repo / "auth_token.json").write_text('{"token":"TOKEN_LEAK"}\n', encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "public.py").write_text("print('ok')\n", encoding="utf-8")
    (repo / "src" / "skill_token.py").write_text("TOKEN_NAME = 'safe source symbol'\n", encoding="utf-8")
    try:
        os.symlink(".git/credentials", repo / "alias.txt")
    except (OSError, NotImplementedError):
        pass
    try:
        os.link(repo / ".git" / "credentials", repo / "hardlink.txt")
    except (OSError, NotImplementedError):
        pass

    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry.set_context(
        ToolContext(
            repo_dir=repo,
            drive_root=data,
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
        )
    )

    assert "REPO_READ_BLOCKED" in registry.execute("read_file", {"path": ".git/credentials"})
    assert "READ_FILE_BLOCKED" in registry.execute("read_file", {"root": "system_repo", "path": ".git/credentials"})
    assert "REPO_READ_BLOCKED" in registry.execute("read_file", {"path": ".git/config"})
    assert "READ_FILE_BLOCKED" in registry.execute("read_file", {"root": "system_repo", "path": ".git/config"})
    assert "REPO_READ_BLOCKED" in registry.execute("read_file", {"path": ".env.local"})
    assert "REPO_READ_BLOCKED" in registry.execute("read_file", {"path": "auth_token.json"})
    alias_result = registry.execute("read_file", {"path": "alias.txt"})
    if (repo / "alias.txt").exists():
        assert "REPO_READ_BLOCKED" in alias_result
    hardlink_result = registry.execute("read_file", {"path": "hardlink.txt"})
    if (repo / "hardlink.txt").exists():
        assert "REPO_READ_BLOCKED" in hardlink_result
    listing = registry.execute("list_files", {"dir": "."})
    assert ".git/" not in listing
    assert ".env.local" not in listing
    assert "auth_token.json" not in listing
    assert "alias.txt" not in listing
    assert "hardlink.txt" not in listing
    assert "src/" in listing
    assert "secret/control" in listing
    system_listing = registry.execute("list_files", {"root": "system_repo", "dir": "."})
    assert ".git/" not in system_listing
    assert "auth_token.json" not in system_listing
    assert "secret/control" in system_listing
    assert "REPO_LIST_BLOCKED" in registry.execute("list_files", {"dir": ".git"})
    readable = registry.execute("read_file", {"path": "src/public.py"})
    assert "print('ok')" in readable
    source_with_token_name = registry.execute("read_file", {"path": "src/skill_token.py"})
    assert "safe source symbol" in source_with_token_name
    secret_search = registry.execute("search_code", {"query": "TOKEN_LEAK"})
    assert "No matches found" in secret_search
    assert "auth_token.json:" not in secret_search
    assert "SEARCH_BLOCKED" in registry.execute("search_code", {"query": "TOKEN_LEAK", "path": "auth_token.json"})
    public_search = registry.execute("search_code", {"query": "safe source symbol"})
    assert "src/skill_token.py" in public_search
    digest = registry.execute("codebase_digest", {})
    assert "auth_token.json" not in digest
    assert ".env.local" not in digest
    assert "src/skill_token.py" in digest
    cached = list((data / "state" / "code_intel").glob("*/inventory.json"))
    assert not cached


def test_local_readonly_subagent_task_drive_and_skill_payload_filters(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tools.registry import ToolContext, ToolRegistry

    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    (data / "settings.json").write_text('{"OPENROUTER_API_KEY":"secret"}', encoding="utf-8")
    (data / "skills" / "external" / "alpha").mkdir(parents=True)
    (data / "skills" / "external" / "alpha" / "skill.md").write_text("hello", encoding="utf-8")
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry.set_context(
        ToolContext(
            repo_dir=repo,
            drive_root=data,
            task_constraint=TaskConstraint(mode="local_readonly_subagent", allow_enable=False),
        )
    )

    assert "READ_FILE_BLOCKED" in registry.execute("read_file", {"root": "task_drive", "path": "settings.json"})
    traversal = registry.execute(
        "read_file",
        {"root": "skill_payload", "bucket": "external", "skill_name": "../../settings.json", "path": "."},
    )
    assert "TOOL_ACCESS_BLOCKED" in traversal or "READ_FILE_ERROR" in traversal or "TOOL_ARG_ERROR" in traversal
    skill_payload_read = registry.execute(
        "read_file",
        {"root": "skill_payload", "bucket": "external", "skill_name": "alpha", "path": "skill.md"},
    )
    assert "TOOL_ACCESS_BLOCKED" in skill_payload_read


# ---------------------------------------------------------------------------
# Discovery path drift test
# ---------------------------------------------------------------------------


def test_discovery_uses_ssot_not_registry_core_names():
    """tool_discovery.py must use SSOT (via tool_policy), not registry.CORE_TOOL_NAMES."""
    import ouroboros.tools.tool_discovery as td
    source = inspect.getsource(td)
    # Must import from tool_policy (SSOT-aware)
    assert "tool_policy" in source, (
        "tool_discovery.py must import from tool_policy for SSOT-aware non-core listing"
    )
    # Must NOT call _registry.list_non_core_tools() — that uses the registry's own set
    assert "_registry.list_non_core_tools()" not in source, (
        "tool_discovery.py must not call _registry.list_non_core_tools() — "
        "that uses registry.py's local CORE_TOOL_NAMES, not the SSOT"
    )


def test_discovery_path_consistent_with_policy():
    """list_available_tools must return the same non-core set as tool_policy.list_non_core_tools."""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import list_non_core_tools as policy_non_core
    import ouroboros.tools.tool_discovery as td

    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    td.set_registry(registry)

    # Get what tool_policy says (SSOT)
    policy_names = {t["name"] for t in policy_non_core(registry)}
    # Remove meta-tools (discovery excludes them from its listing)
    policy_names -= {"list_available_tools", "enable_tools"}

    # Get what discovery tool shows
    from ouroboros.tools.registry import ToolContext
    ctx = ToolContext(repo_dir=tmp, drive_root=tmp)
    output = td._list_available_tools(ctx)

    if not policy_names:
        assert "All tools are already" in output
    else:
        for name in policy_names:
            assert name in output, (
                f"tool_policy says '{name}' is non-core but discovery doesn't show it"
            )

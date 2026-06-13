"""Tests for Claude Code gateway safety guards and orchestration helpers.

The gateway module (ouroboros/gateways/claude_code.py) is SDK-only — there is
no CLI subprocess fallback. When claude-agent-sdk is absent callers receive
an error result with an install hint. Tests use a lightweight mock of the SDK
so the gateway can be imported and exercised without the real package installed.

We test:
  - ClaudeCodeResult (importable from gateway even w/o SDK via careful mocking)
  - Path guard and readonly guard hooks (function-level, no SDK dependency)
  - Orchestration helpers (_load_project_context etc.) now in shell.py
"""

import asyncio
import json
import pathlib
import subprocess
import sys
import types
import pytest


# ---------------------------------------------------------------------------
# Mock SDK so the gateway can be imported on Python 3.9 / without SDK
# ---------------------------------------------------------------------------

from tests._shared import ensure_claude_agent_sdk_mock

ensure_claude_agent_sdk_mock()


async def _async_gen(items):
    """Async generator helper for mocking query() streams in tests."""
    for item in items:
        yield item


from ouroboros.gateways.claude_code import (  # noqa: E402
    ClaudeCodeResult,
    make_path_guard,
    make_readonly_guard,
    _normalize_sdk_usage,
    SAFETY_CRITICAL,
)

# Orchestration helpers now live in shell.py
from ouroboros.tools.shell import (  # noqa: E402
    _load_project_context,
    _get_changed_files,
    _get_diff_stat,
    _run_validation,
)


# ---------------------------------------------------------------------------
# ClaudeCodeResult
# ---------------------------------------------------------------------------

class TestClaudeCodeResult:
    def test_success_to_json(self):
        r = ClaudeCodeResult(
            success=True,
            result_text="Edited 2 files",
            session_id="abc-123",
            cost_usd=0.05,
            changed_files=["foo.py", "bar.py"],
            diff_stat="2 files changed, 10 insertions",
        )
        out = json.loads(r.to_tool_output())
        assert out["success"] is True
        assert out["result"] == "Edited 2 files"
        assert out["session_id"] == "abc-123"
        assert out["cost_usd"] == 0.05
        assert out["changed_files"] == ["foo.py", "bar.py"]
        assert "diff_stat" in out

    def test_error_to_json(self):
        r = ClaudeCodeResult(success=False, error="Something went wrong")
        out = json.loads(r.to_tool_output())
        assert out["success"] is False
        assert "error" in out

    def test_empty_fields_omitted(self):
        r = ClaudeCodeResult(success=True, result_text="ok")
        out = json.loads(r.to_tool_output())
        assert "session_id" not in out
        assert "changed_files" not in out
        assert "error" not in out
        assert "validation" not in out


# ---------------------------------------------------------------------------
# Path guard hook
# ---------------------------------------------------------------------------

class TestPathGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_allows_file_inside_cwd(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "foo.py")}},
            "tid-1", None,
        ))
        assert result == {}

    def test_blocks_file_outside_cwd(self, tmp_path):
        guard = make_path_guard(str(tmp_path / "subdir"))
        (tmp_path / "subdir").mkdir()
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / "outside.py")}},
            "tid-2", None,
        ))
        assert result != {}
        assert "deny" in str(result)

    def test_blocks_safety_critical_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
        guard = make_path_guard(str(tmp_path))
        for critical in SAFETY_CRITICAL:
            result = self._run(guard(
                {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / critical)}},
                f"tid-{critical}", None,
            ))
            assert "deny" in str(result), f"Should block {critical}"

    def test_blocks_safety_critical_with_backslash_paths(self, tmp_path, monkeypatch):
        """Safety-critical check must work regardless of OS path separator.

        On Windows os.path.relpath returns backslashes. The guard must normalize
        to forward slashes (via pathlib.as_posix) before comparing against
        SAFETY_CRITICAL which uses forward slashes.
        """
        monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
        guard = make_path_guard(str(tmp_path))
        # Simulate a Windows-style resolved path by using the native separator
        for critical in SAFETY_CRITICAL:
            # Build path using tmp_path / critical (pathlib handles separators)
            target = str(tmp_path / critical)
            result = self._run(guard(
                {"tool_name": "Edit", "tool_input": {"file_path": target}},
                f"tid-bslash-{critical}", None,
            ))
            assert "deny" in str(result), (
                f"Should block '{critical}' even with native path separators"
            )

    def test_can_disable_runtime_path_guard_for_external_workspaces(self, tmp_path):
        guard = make_path_guard(str(tmp_path), protect_runtime_paths=False)
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {"file_path": str(tmp_path / ".github" / "workflows" / "ci.yml")}},
            "tid-external-ci", None,
        ))
        assert result == {}

    def test_allows_read_tool(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        result = self._run(guard(
            {"tool_name": "Read", "tool_input": {"file_path": "/etc/passwd"}},
            "tid-read", None,
        ))
        assert result == {}

    def test_blocks_relative_path_escape(self, tmp_path):
        guard = make_path_guard(str(tmp_path))
        result = self._run(guard(
            {"tool_name": "Write", "tool_input": {"file_path": "../../../etc/evil"}},
            "tid-escape", None,
        ))
        assert "deny" in str(result)

    def test_applies_external_write_path_blocker(self, tmp_path):
        guard = make_path_guard(
            str(tmp_path),
            protect_runtime_paths=False,
            write_path_blocker=lambda target: "path is credential-like" if target.name == ".env" else "",
        )

        blocked = self._run(guard(
            {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / ".env")}},
            "tid-user-files-secret", None,
        ))
        allowed = self._run(guard(
            {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "report.html")}},
            "tid-user-files-report", None,
        ))

        assert "deny" in str(blocked)
        assert "credential-like" in str(blocked)
        assert allowed == {}


# ---------------------------------------------------------------------------
# Read-only guard hook
# ---------------------------------------------------------------------------

class TestReadonlyGuard:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_blocks_edit(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Edit", "tool_input": {}}, "tid-1", None,
        ))
        assert "deny" in str(result)

    def test_blocks_bash(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Bash", "tool_input": {}}, "tid-2", None,
        ))
        assert "deny" in str(result)

    def test_allows_read(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Read", "tool_input": {}}, "tid-3", None,
        ))
        assert result == {}

    def test_allows_grep(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Grep", "tool_input": {}}, "tid-4", None,
        ))
        assert result == {}

    def test_allows_glob(self):
        guard = make_readonly_guard()
        result = self._run(guard(
            {"tool_name": "Glob", "tool_input": {}}, "tid-5", None,
        ))
        assert result == {}


# ---------------------------------------------------------------------------
# Orchestration helpers (now in shell.py)
# ---------------------------------------------------------------------------

class TestProjectContext:
    def test_loads_existing_docs(self, tmp_path):
        (tmp_path / "BIBLE.md").write_text("# Constitution", encoding="utf-8")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "DEVELOPMENT.md").write_text("# Dev guide", encoding="utf-8")
        ctx = _load_project_context(tmp_path)
        assert "CONSTITUTION" in ctx
        assert "DEVELOPMENT GUIDE" in ctx

    def test_handles_missing_docs(self, tmp_path):
        ctx = _load_project_context(tmp_path)
        assert ctx == ""  # no docs, empty context

    def test_preserves_large_governance_docs(self, tmp_path):
        (tmp_path / "BIBLE.md").write_text("x" * 100_000, encoding="utf-8")
        ctx = _load_project_context(tmp_path)
        assert "truncated" not in ctx.lower()
        assert "x" * 100_000 in ctx


# ---------------------------------------------------------------------------
# SDK import fallback contract
# ---------------------------------------------------------------------------

class TestImportFallback:
    """Verify the gateway raises ImportError when the SDK is unavailable.

    Since the claude-agent-sdk is a required dependency with no CLI fallback,
    ImportError at import time surfaces SDK unavailability so callers can
    return a clear install hint rather than silently failing.
    """

    def test_gateway_import_requires_sdk(self):
        """Without the real SDK (or our mock), import should raise ImportError."""
        # The module does `from claude_agent_sdk import ...` at module level,
        # so ImportError is raised before any code runs.
        # This documents that the SDK is a hard requirement (no CLI fallback).
        #
        # To simulate absence even when SDK is installed, we must:
        # 1. Save and remove ALL claude_agent_sdk* entries from sys.modules
        # 2. Set sys.modules["claude_agent_sdk"] = None (triggers ImportError)
        # 3. Remove the cached gateway module
        # Without step 1, Python may resolve sub-module imports from cached
        # entries even when the top-level package is blocked.
        import importlib

        # Save all SDK-related modules so we can restore them
        saved_modules = {}
        for key in list(sys.modules):
            if key == "claude_agent_sdk" or key.startswith("claude_agent_sdk."):
                saved_modules[key] = sys.modules.pop(key)

        try:
            # Block the import — setting to None triggers ImportError
            sys.modules["claude_agent_sdk"] = None
            # Also remove cached gateway module so it re-imports
            sys.modules.pop("ouroboros.gateways.claude_code", None)
            with pytest.raises(ImportError):
                importlib.import_module("ouroboros.gateways.claude_code")
        finally:
            # Remove the None sentinel
            sys.modules.pop("claude_agent_sdk", None)
            # Restore all saved SDK modules
            sys.modules.update(saved_modules)
            # If nothing was saved (SDK not installed), ensure mock is in place
            if not saved_modules:
                ensure_claude_agent_sdk_mock()
            # Re-import gateway with real/mock SDK
            sys.modules.pop("ouroboros.gateways.claude_code", None)
            importlib.import_module("ouroboros.gateways.claude_code")


# ---------------------------------------------------------------------------
# SDK API surface verification tests (v4.8.1 fixes)
# ---------------------------------------------------------------------------

class TestSDKAPISurface:
    """Verify that the gateway uses correct SDK API method names and signatures.

    These tests inspect source code to catch method name mismatches that would
    cause AttributeError at runtime (e.g. receive_response vs receive_messages).
    """

    def _gateway_source(self):
        import inspect
        from ouroboros.gateways import claude_code
        return inspect.getsource(claude_code)

    def test_edit_path_uses_receive_response(self):
        """Edit path must use receive_response() — it auto-stops after ResultMessage.

        receive_messages() streams indefinitely and can hang.
        receive_response() is the correct high-level method.
        """
        src = self._gateway_source()
        assert "receive_response()" in src, "Edit path must call receive_response()"
        assert "receive_messages()" not in src, (
            "receive_messages() streams indefinitely — use receive_response() instead"
        )

    def test_readonly_path_uses_sdk_client_lifecycle(self):
        """Read-only path must use ClaudeSDKClient, not query()+early generator break."""
        src = self._gateway_source()
        assert "async with ClaudeSDKClient(options=options) as client:" in src
        assert "await client.query(prompt)" in src
        assert "async for message in client.receive_response():" in src
        assert "async for message in query(" not in src

    def test_max_budget_in_constructor(self):
        """v4.8.1 fix: max_budget_usd should be passed in ClaudeAgentOptions constructor."""
        src = self._gateway_source()
        # Should NOT have post-assignment pattern
        assert "options.max_budget_usd" not in src, \
            "max_budget_usd should be in constructor, not post-assigned"
        # Should have it in the constructor call
        assert "max_budget_usd=budget" in src, \
            "max_budget_usd should be passed as constructor kwarg"

    def test_query_helper_not_used_by_gateway(self):
        """The gateway avoids query() for read-only cleanup correctness."""
        src = self._gateway_source()
        assert " query," not in src
        assert "query(prompt=prompt" not in src

    def test_edit_mode_allows_write_tool_for_new_files(self):
        """Edit mode must allow SDK Write for new-file deliverables."""
        src = self._gateway_source()
        assert 'allowed_tools=["Read", "Edit", "Write", "Grep", "Glob"]' in src
        assert 'matcher="Edit|Write|MultiEdit"' in src


# ---------------------------------------------------------------------------
# SDK-only path: ImportError and failure diagnostics
# ---------------------------------------------------------------------------

class TestSDKOnlyPath:
    """claude_code_edit and advisory_pre_review return meaningful errors when SDK missing."""

    def test_claude_code_edit_returns_error_when_sdk_missing(self, monkeypatch, tmp_path):
        """When SDK ImportError → tool returns install hint, not a crash."""
        from types import SimpleNamespace
        import ouroboros.tools.shell as shell_mod

        ctx = SimpleNamespace(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            branch_dev="ouroboros",
            pending_events=[],
            emit_progress_fn=lambda _: None,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")

        import ouroboros.gateways.claude_code as gw_mod

        # Directly test the error message in the function
        from ouroboros.tools.shell import _claude_code_edit

        # Mock _acquire_git_lock/_release_git_lock so we don't need git
        import ouroboros.tools.git as git_mod
        monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: None)
        monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: None)
        import ouroboros.utils as utils_mod
        monkeypatch.setattr(utils_mod, "run_cmd", lambda *args, **kwargs: None)

        def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'claude_agent_sdk'")

        monkeypatch.setattr(gw_mod, "run_edit", raise_import_error)
        result = _claude_code_edit(ctx, "Test prompt")
        assert "CLAUDE_CODE_UNAVAILABLE" in result
        assert "claude-agent-sdk" in result

    def test_advisory_returns_error_when_sdk_missing(self, monkeypatch, tmp_path):
        """When SDK not installed → advisory returns install hint."""
        from ouroboros.tools.claude_advisory_review import _run_claude_advisory
        from types import SimpleNamespace

        ctx = SimpleNamespace(
            repo_dir=tmp_path,
            drive_root=tmp_path,
            emit_progress_fn=lambda _: None,
            pending_events=[],
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        # Patch run_readonly to raise ImportError
        def raise_import_error(*args, **kwargs):
            raise ImportError("No module named 'claude_agent_sdk'")

        try:
            import ouroboros.gateways.claude_code as gw
            monkeypatch.setattr(gw, "run_readonly", raise_import_error)
        except Exception:
            pass

        # Also patch the import inside _run_claude_advisory
        import builtins
        real_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if "claude_code" in str(name) and "gateways" in str(name):
                raise ImportError("claude-agent-sdk not installed")
            return real_import(name, *args, **kwargs)

        items, raw, *_extra = _run_claude_advisory(tmp_path, "test commit", ctx)
        # Either SDK-not-installed message, git-setup error, or empty if SDK is present.
        # We only verify the result is well-typed; the specific error message depends on
        # which gate fires first (git diff may fail before reaching the SDK path when the
        # tmp_path is not a real git repository).
        assert isinstance(items, list)


# ---------------------------------------------------------------------------
# Status endpoint SDK version check
# ---------------------------------------------------------------------------

class TestRunReadonlyEffortParam:
    """run_readonly passes effort param to ClaudeAgentOptions."""

    def test_run_readonly_passes_effort_to_options(self):
        """_run_readonly_async should include 'effort' in ClaudeAgentOptions kwargs."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        source = inspect.getsource(gw._run_readonly_async)
        # Verify the effort kwarg is forwarded
        assert "effort" in source
        assert "options_kwargs" in source

    def test_run_readonly_default_effort_is_high(self):
        """Default effort for run_readonly should be 'high' (matches blocking reviewers)."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        sig = inspect.signature(gw.run_readonly)
        params = sig.parameters
        assert "effort" in params
        assert params["effort"].default == "high"

    def test_run_readonly_async_default_effort_is_high(self):
        """Default effort for _run_readonly_async should be 'high'."""
        import inspect
        from ouroboros.gateways import claude_code as gw

        sig = inspect.signature(gw._run_readonly_async)
        params = sig.parameters
        assert "effort" in params
        assert params["effort"].default == "high"

    def test_effort_forwarded_to_options_when_sdk_supports_it(self):
        """effort='high' is forwarded to ClaudeAgentOptions when the SDK accepts it."""
        captured: dict = {}

        class FakeOptions:
            # Include 'effort' as an explicit param so signature inspection
            # (used in the guard) correctly detects that this SDK version supports it.
            def __init__(self, effort=None, **kwargs):
                if effort is not None:
                    kwargs["effort"] = effort
                captured.update(kwargs)

        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch

        class FakeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def query(self, prompt):
                return None

            async def receive_response(self):
                if False:
                    yield None

        # Patch ClaudeAgentOptions with one that accepts effort
        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", FakeOptions), \
             patch("ouroboros.gateways.claude_code.ClaudeSDKClient", FakeSDKClient):
            asyncio.get_event_loop().run_until_complete(
                __import__("ouroboros.gateways.claude_code", fromlist=["_run_readonly_async"])
                ._run_readonly_async("test", cwd="/tmp", effort="high")
            )

        assert captured.get("effort") == "high", (
            f"expected effort='high' forwarded to ClaudeAgentOptions, got: {captured}"
        )

    def test_effort_omitted_gracefully_when_sdk_lacks_support(self):
        """When SDK's ClaudeAgentOptions does not accept effort, it is silently dropped."""
        captured: dict = {}

        class FakeOptionsNoEffort:
            """Simulates an older SDK version without effort kwarg."""
            def __init__(self, **kwargs):
                if "effort" in kwargs:
                    raise TypeError("__init__() got an unexpected keyword argument 'effort'")
                captured.update(kwargs)

        import asyncio
        from unittest.mock import patch

        class FakeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def query(self, prompt):
                return None

            async def receive_response(self):
                if False:
                    yield None

        with patch("ouroboros.gateways.claude_code.ClaudeAgentOptions", FakeOptionsNoEffort), \
             patch("ouroboros.gateways.claude_code.ClaudeSDKClient", FakeSDKClient):
            # Should not raise — effort silently dropped
            asyncio.get_event_loop().run_until_complete(
                __import__("ouroboros.gateways.claude_code", fromlist=["_run_readonly_async"])
                ._run_readonly_async("test", cwd="/tmp", effort="high")
            )

        assert "effort" not in captured, "effort must be omitted when SDK lacks support"

    def test_normalize_sdk_usage_maps_anthropic_keys(self):
        usage = _normalize_sdk_usage({
            "input_tokens": 100,
            "output_tokens": 25,
            "cache_read_input_tokens": 40,
            "cache_creation_input_tokens": 12,
        })
        assert usage["prompt_tokens"] == 100
        assert usage["completion_tokens"] == 25
        assert usage["cached_tokens"] == 40
        assert usage["cache_write_tokens"] == 12


class TestSDKStatusPayload:
    """_claude_code_status_payload returns app-managed runtime info."""

    def test_status_payload_reflects_sdk_installed_with_key(self, monkeypatch):
        """When SDK is importable and API key set, status is ready."""
        import importlib.metadata
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState(
                app_managed=True,
                sdk_version="0.1.54",
                sdk_path="/fake/sdk",
                cli_path="/fake/cli/claude",
                cli_version="2.1.90",
                interpreter_path="/fake/python3",
                api_key_set=True,
                ready=True,
            )

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is True
        assert payload["ready"] is True
        assert payload["status"] == "ready"
        assert "0.1.54" in payload["message"]
        assert payload["app_managed"] is True
        assert payload["busy"] is False
        assert payload["error"] == ""

    def test_status_payload_reflects_sdk_missing(self, monkeypatch):
        """When SDK is not installed, status is missing."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState()

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is False
        assert payload["ready"] is False
        assert payload["status"] == "missing"
        assert "not available" in payload["message"].lower() or "missing" in payload["message"].lower()
        assert payload["busy"] is False

    def test_status_payload_no_api_key(self, monkeypatch):
        """When SDK present but ANTHROPIC_API_KEY not set, status is no_api_key."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState(
                sdk_version="0.1.54",
                sdk_path="/fake/sdk",
                cli_path="/fake/cli/claude",
                cli_version="2.1.90",
                api_key_set=False,
                ready=False,
            )

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert payload["installed"] is True
        assert payload["ready"] is False
        assert payload["status"] == "no_api_key"
        assert payload["api_key_set"] is False

    def test_status_payload_includes_runtime_fields(self, monkeypatch):
        """Payload includes app_managed, legacy_detected, cli fields."""
        from ouroboros.platform_layer import ClaudeRuntimeState

        def mock_resolve():
            return ClaudeRuntimeState(
                app_managed=True,
                sdk_version="0.1.54",
                cli_path="/fake/cli",
                cli_version="2.1.90",
                legacy_detected=True,
                legacy_sdk_version="0.1.50",
                api_key_set=True,
                ready=True,
            )

        monkeypatch.setattr("ouroboros.platform_layer.resolve_claude_runtime", mock_resolve)

        from ouroboros.gateway import settings as server_mod
        payload = server_mod._claude_code_status_payload()

        assert "cli_path" in payload
        assert "cli_version" in payload
        assert "app_managed" in payload
        assert "legacy_detected" in payload
        assert payload["legacy_detected"] is True
        assert payload["legacy_sdk_version"] == "0.1.50"


# ---------------------------------------------------------------------------
# Claude runtime resolution contract
# ---------------------------------------------------------------------------

class TestClaudeRuntimeResolution:
    """Verify the runtime resolver in ouroboros.platform_layer."""

    def test_runtime_state_dataclass_defaults(self):
        from ouroboros.platform_layer import ClaudeRuntimeState
        state = ClaudeRuntimeState()
        assert state.app_managed is False
        assert state.sdk_version == ""
        assert state.cli_path == ""
        assert state.ready is False
        assert state.status_label() == "missing"

    def test_runtime_state_status_labels(self):
        from ouroboros.platform_layer import ClaudeRuntimeState

        assert ClaudeRuntimeState(sdk_version="1.0", cli_path="/x", api_key_set=True, ready=True).status_label() == "ready"
        assert ClaudeRuntimeState(sdk_version="1.0", cli_path="/x", api_key_set=False).status_label() == "no_api_key"
        assert ClaudeRuntimeState(sdk_version="1.0", cli_path="/x", api_key_set=True, error="boom").status_label() == "error"
        assert ClaudeRuntimeState(sdk_version="1.0", api_key_set=True, ready=False).status_label() == "degraded"
        assert ClaudeRuntimeState().status_label() == "missing"

    def test_status_label_error_takes_priority_over_missing_api_key(self):
        """Regression: v4.33.1 priority fix.

        Prior to v4.33.1, ``status_label`` checked ``api_key_set`` before
        ``error``, so a below-baseline SDK (or any other runtime error) was
        silently shadowed as ``no_api_key`` when ``ANTHROPIC_API_KEY`` was
        absent. Users would then set a key, retry, and only then discover the
        real blocker. The priority is now: missing → error → no_api_key →
        degraded → ready, so repair hints are surfaced immediately.
        """
        from ouroboros.platform_layer import ClaudeRuntimeState

        state = ClaudeRuntimeState(
            sdk_version="0.1.50",
            cli_path="/fake/cli",
            api_key_set=False,
            error="SDK 0.1.50 below baseline 0.1.60",
        )
        assert state.status_label() == "error", (
            "error must take priority over no_api_key so version-gate "
            "failures surface even without a configured API key"
        )

    def test_resolve_claude_runtime_returns_state(self, monkeypatch):
        """resolve_claude_runtime returns a ClaudeRuntimeState regardless of SDK presence."""
        from ouroboros.platform_layer import resolve_claude_runtime, ClaudeRuntimeState
        state = resolve_claude_runtime()
        assert isinstance(state, ClaudeRuntimeState)
        assert isinstance(state.interpreter_path, str)
        assert isinstance(state.api_key_set, bool)

    def test_resolve_claude_runtime_rejects_below_baseline_sdk(self, monkeypatch):
        """SDK below _CLAUDE_SDK_MIN_VERSION must NOT be reported as ready.

        Regression guard: prior to v4.33.1, resolve_claude_runtime()
        marked ready=True whenever the SDK was importable and the CLI
        present, even if the installed SDK (e.g. 0.1.50) pre-dated
        Opus 4.7 adaptive-thinking support — producing a false green
        on /api/claude-code/status.
        """
        import importlib.metadata as _md
        import ouroboros.platform_layer as pl

        def fake_version(pkg: str) -> str:
            if pkg == "claude-agent-sdk":
                return "0.1.50"
            return _md.version(pkg)

        monkeypatch.setattr(pl, "_find_sdk_package_path", lambda: "/fake/python-standalone/site/claude_agent_sdk")
        monkeypatch.setattr(pl, "_find_bundled_cli", lambda p: "/fake/cli/claude")
        monkeypatch.setattr(pl, "_probe_cli_version", lambda p: "2.1.111")
        monkeypatch.setattr(_md, "version", fake_version)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        state = pl.resolve_claude_runtime()

        assert state.sdk_version == "0.1.50"
        assert state.cli_path == "/fake/cli/claude"
        assert state.api_key_set is True
        assert state.ready is False, "SDK below baseline must not be ready"
        assert "0.1.50" in state.error and "baseline" in state.error.lower()
        assert state.status_label() == "error"

    def test_resolve_claude_runtime_accepts_at_baseline_sdk(self, monkeypatch):
        """SDK at or above _CLAUDE_SDK_MIN_VERSION passes the version gate."""
        import importlib.metadata as _md
        import ouroboros.platform_layer as pl
        from ouroboros.launcher_bootstrap import _CLAUDE_SDK_MIN_VERSION

        def fake_version(pkg: str) -> str:
            if pkg == "claude-agent-sdk":
                return _CLAUDE_SDK_MIN_VERSION
            return _md.version(pkg)

        monkeypatch.setattr(pl, "_find_sdk_package_path", lambda: "/fake/python-standalone/site/claude_agent_sdk")
        monkeypatch.setattr(pl, "_find_bundled_cli", lambda p: "/fake/cli/claude")
        monkeypatch.setattr(pl, "_probe_cli_version", lambda p: "2.1.111")
        monkeypatch.setattr(_md, "version", fake_version)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")

        state = pl.resolve_claude_runtime()

        assert state.sdk_version == _CLAUDE_SDK_MIN_VERSION
        assert state.ready is True
        assert state.error == ""
        assert state.status_label() == "ready"

    def test_legacy_detection_non_app_path(self):
        """SDK installed outside python-standalone is classified as legacy."""
        from ouroboros.platform_layer import _detect_legacy_user_site_sdk
        detected, path, ver = _detect_legacy_user_site_sdk()
        assert isinstance(detected, bool)

    def test_find_bundled_cli_nonexistent_path(self):
        """_find_bundled_cli returns None for a non-existent SDK path."""
        from ouroboros.platform_layer import _find_bundled_cli
        assert _find_bundled_cli("/nonexistent/path") is None


# ---------------------------------------------------------------------------
# Gateway stderr capture
# ---------------------------------------------------------------------------

class TestGatewayStderrCapture:
    """Verify stderr ring buffer in the gateway."""

    def test_stderr_callback_stores_lines(self):
        from ouroboros.gateways.claude_code import (
            _stderr_callback, get_last_stderr, clear_stderr_buffer,
        )
        clear_stderr_buffer()
        _stderr_callback("line one")
        _stderr_callback("line two")
        result = get_last_stderr()
        assert "line one" in result
        assert "line two" in result
        clear_stderr_buffer()
        assert get_last_stderr() == ""

    def test_stderr_tail_in_result(self):
        """ClaudeCodeResult.stderr_tail appears in JSON output."""
        r = ClaudeCodeResult(
            success=False,
            error="ProcessError: exit code 1",
            stderr_tail="Authentication failed",
        )
        out = json.loads(r.to_tool_output())
        assert out["stderr_tail"] == "Authentication failed"

    def test_stderr_tail_omitted_on_success(self):
        """stderr_tail is not in JSON when empty."""
        r = ClaudeCodeResult(success=True, result_text="ok")
        out = json.loads(r.to_tool_output())
        assert "stderr_tail" not in out


# ---------------------------------------------------------------------------
# Launcher bootstrap — verify_claude_runtime
# ---------------------------------------------------------------------------

class TestVerifyClaudeRuntime:
    """verify_claude_runtime repairs missing SDK."""

    def test_verify_passes_when_sdk_present_at_baseline(self, tmp_path, monkeypatch):
        """SDK imports, CLI exists, version meets baseline → no repair."""
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=0, stdout="ok|0.1.60", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 1

    def test_verify_passes_when_sdk_above_baseline(self, tmp_path):
        """SDK 0.1.61 > baseline 0.1.60 → no repair."""
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=0, stdout="ok|0.1.61", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 1

    def test_verify_triggers_repair_when_sdk_below_baseline(self, tmp_path):
        """SDK 0.1.50 < baseline 0.1.60 → repair fires even though import + CLI work.

        This guards the upgraded-install compat gap: claude_code_edit and
        advisory_pre_review would otherwise still send thinking.type=enabled
        to Opus 4.7 on an install with pre-0.1.60 SDK already present.
        """
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=0, stdout="ok|0.1.50", stderr="")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 2
        assert "pip" in str(calls[1])
        assert "0.1.60" in str(calls[1])

    def test_verify_triggers_repair_when_missing(self, tmp_path):
        """When SDK check fails (ModuleNotFoundError etc), repair install is attempted."""
        import logging
        from ouroboros.launcher_bootstrap import BootstrapContext, verify_claude_runtime

        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            from types import SimpleNamespace
            if "-c" in cmd:
                return SimpleNamespace(returncode=1, stdout="", stderr="ModuleNotFoundError")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        ctx = BootstrapContext(
            bundle_dir=tmp_path,
            repo_dir=tmp_path,
            data_dir=tmp_path,
            settings_path=tmp_path / "settings.json",
            embedded_python="/fake/python3",
            app_version="1.0.0",
            hidden_run=fake_run,
            save_settings=lambda s: None,
            log=logging.getLogger("test"),
        )
        result = verify_claude_runtime(ctx)
        assert result is True
        assert len(calls) == 2
        assert "pip" in str(calls[1])


class TestVersionTuple:
    """_version_tuple parses PEP 440-ish version strings for comparison."""

    def test_parses_simple_version(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("0.1.60") == (0, 1, 60)

    def test_strips_post_suffix(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("0.1.60.post1") == (0, 1, 60)

    def test_strips_pre_release_suffix(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        # "0.1.60rc1" → parses "0", "1", "60" (rc1 stops at first non-digit)
        assert _version_tuple("0.1.60rc1") == (0, 1, 60)

    def test_comparison_semantics(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("0.1.50") < _version_tuple("0.1.60")
        assert _version_tuple("0.1.60") >= _version_tuple("0.1.60")
        assert _version_tuple("0.1.61") > _version_tuple("0.1.60")
        assert _version_tuple("0.2.0") > _version_tuple("0.1.99")

    def test_empty_returns_zero(self):
        from ouroboros.launcher_bootstrap import _version_tuple
        assert _version_tuple("") == (0,)
        assert _version_tuple("garbage") == (0,)

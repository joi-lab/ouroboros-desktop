"""Tests for process memory infrastructure.

Covers:
- Execution reflection trigger logic (should_generate_reflection)
- Error detail collection and marker detection
- Reflection loading into context
"""

import inspect
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ─────────────────────────────────────────────────────────────────────────────
# should_generate_reflection
# ─────────────────────────────────────────────────────────────────────────────

class TestReflectionTrigger:
    """should_generate_reflection(llm_trace) must detect error conditions."""

    def test_clean_trace_no_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
            {"tool": "run_command", "args": {}, "result": "ok", "is_error": False},
        ]}
        assert should_generate_reflection(trace) is False

    def test_error_tool_triggers_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "run_command", "args": {}, "result": "⚠️ TOOL_ERROR: failed", "is_error": True},
        ]}
        assert should_generate_reflection(trace) is True

    def test_review_blocked_marker_triggers_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": False,
             "result": "⚠️ REVIEW_BLOCKED (attempt 1/3): reviewer flagged version sync"},
        ]}
        assert should_generate_reflection(trace) is True

    def test_tests_failed_marker_triggers_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": False,
             "result": "OK: committed\n\n⚠️ TESTS_FAILED: VERSION not in README"},
        ]}
        assert should_generate_reflection(trace) is True

    def test_empty_trace_no_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        assert should_generate_reflection({"tool_calls": []}) is False
        assert should_generate_reflection({}) is False

    def test_nontrivial_rounds_triggers_reflection(self):
        """rounds >= NONTRIVIAL_ROUNDS_THRESHOLD fires even on a clean trace."""
        from ouroboros.reflection import should_generate_reflection, NONTRIVIAL_ROUNDS_THRESHOLD
        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace, rounds=NONTRIVIAL_ROUNDS_THRESHOLD) is True
        assert should_generate_reflection(clean_trace, rounds=NONTRIVIAL_ROUNDS_THRESHOLD - 1) is False

    def test_nontrivial_cost_triggers_reflection(self):
        """cost_usd >= NONTRIVIAL_COST_THRESHOLD fires even on a clean trace."""
        from ouroboros.reflection import should_generate_reflection, NONTRIVIAL_COST_THRESHOLD
        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace, rounds=0, cost_usd=NONTRIVIAL_COST_THRESHOLD) is True
        assert should_generate_reflection(clean_trace, rounds=0, cost_usd=NONTRIVIAL_COST_THRESHOLD - 0.01) is False

    def test_default_kwargs_clean_trace_no_reflection(self):
        """Default kwargs (rounds=0, cost_usd=0.0) keep original behaviour unchanged."""
        from ouroboros.reflection import should_generate_reflection
        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
        ]}
        assert should_generate_reflection(clean_trace) is False

    def test_structured_non_zero_exit_triggers_reflection(self):
        from ouroboros.reflection import should_generate_reflection
        trace = {"tool_calls": [
            {
                "tool": "run_command",
                "args": {},
                "result": "exit_code=-9",
                "is_error": False,
                "status": "non_zero_exit",
                "exit_code": -9,
                "signal": "SIGKILL",
            },
        ]}
        assert should_generate_reflection(trace) is True


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions
# ─────────────────────────────────────────────────────────────────────────────

class TestHelperFunctions:
    """_detect_markers and _collect_error_details must extract structured info."""

    def test_detect_markers_finds_all(self):
        from ouroboros.reflection import _detect_markers
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True,
             "result": "⚠️ REVIEW_BLOCKED: test"},
            {"tool": "run_command", "is_error": False,
             "result": "⚠️ TESTS_FAILED: something"},
        ]}
        markers = _detect_markers(trace)
        assert "REVIEW_BLOCKED" in markers
        assert "TESTS_FAILED" in markers

    def test_detect_markers_empty_trace(self):
        from ouroboros.reflection import _detect_markers
        assert _detect_markers({}) == []
        assert _detect_markers({"tool_calls": []}) == []

    def test_collect_error_details_includes_tool_name(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True,
             "result": "⚠️ REVIEW_BLOCKED: test"},
        ]}
        details = _collect_error_details(trace)
        assert "commit_reviewed" in details
        assert "REVIEW_BLOCKED" in details

    def test_collect_error_details_respects_cap(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "run_command", "is_error": True,
             "result": "x" * 5000},
        ]}
        details = _collect_error_details(trace, cap=200)
        assert len(details) <= 210  # cap + small overhead from "..."

    def test_collect_error_details_skips_clean_results(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {"tool": "read_file", "is_error": False, "result": "file contents"},
            {"tool": "run_command", "is_error": True, "result": "error happened"},
        ]}
        details = _collect_error_details(trace)
        assert "read_file" not in details
        assert "run_command" in details

    def test_collect_error_details_includes_structured_status(self):
        from ouroboros.reflection import _collect_error_details
        trace = {"tool_calls": [
            {
                "tool": "run_command",
                "is_error": True,
                "status": "non_zero_exit",
                "exit_code": -9,
                "signal": "SIGKILL",
                "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=-9 (signal=SIGKILL).",
            },
        ]}
        details = _collect_error_details(trace)
        assert "status=non_zero_exit" in details
        assert "signal=SIGKILL" in details

    def test_run_reflection_pipeline_maps_usage_keys_correctly(self):
        """_run_reflection maps usage['rounds'] and usage['cost'] to the correct kwargs.

        Pins the dict-key contract: if 'cost' were renamed to 'cost_usd' inside
        _run_reflection, the cost threshold trigger would silently return 0.0 and
        this test would catch it.
        """
        import unittest.mock as mock
        from ouroboros.agent_task_pipeline import _run_reflection

        class FakeEnv:
            drive_root = __import__("pathlib").Path("/tmp/fake_drive")

        class FakeLlm:
            pass

        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "ok", "is_error": False},
        ]}
        high_cost_usage = {"rounds": 20, "cost": 6.0}

        with mock.patch("ouroboros.reflection.should_generate_reflection",
                        wraps=lambda trace, *, rounds=0, cost_usd=0.0: True) as mock_sgr, \
             mock.patch("ouroboros.reflection.generate_reflection",
                        return_value={"reflection": "ok", "backlog_candidates": []}) as mock_gen, \
             mock.patch("ouroboros.reflection.append_reflection") as mock_append:

            result = _run_reflection(
                FakeEnv(),
                FakeLlm(),
                {"id": "t1", "type": "task", "text": "goal"},
                high_cost_usage,
                clean_trace,
                {},
            )

        # should_generate_reflection must be called with the correct kwargs from usage dict
        mock_sgr.assert_called_once()
        _, kwargs = mock_sgr.call_args
        assert kwargs.get("rounds") == 20, f"Expected rounds=20, got {kwargs.get('rounds')}"
        assert kwargs.get("cost_usd") == 6.0, f"Expected cost_usd=6.0, got {kwargs.get('cost_usd')}"

        # When should_generate_reflection returns True, generate+append must be called
        mock_gen.assert_called_once()
        mock_append.assert_called_once()
        assert result is not None

    def test_generate_reflection_uses_nontrivial_prompt_for_clean_trace(self):
        """generate_reflection picks the non-error prompt for a clean, high-round trace."""
        from ouroboros.reflection import generate_reflection

        captured = {}

        class FakeLlm:
            def chat(self, *, messages, model, reasoning_effort, max_tokens):
                captured["prompt"] = messages[0]["content"]
                return {"content": "Friction was in repeated advisory runs."}, {"cost": 0}

        clean_trace = {"tool_calls": [
            {"tool": "read_file", "args": {}, "result": "file contents", "is_error": False},
            {"tool": "run_command", "args": {}, "result": "ok", "is_error": False},
        ]}
        entry = generate_reflection(
            task={"id": "task-2", "type": "task", "text": "A 20-round clean task"},
            llm_trace=clean_trace,
            trace_summary="20 tool calls, 0 errors",
            llm_client=FakeLlm(),
            usage_dict={"rounds": 20, "cost": 6.0},
        )

        prompt = captured["prompt"]
        # Non-error prompt markers must be present
        assert "high round count or high cost" in prompt, "Expected nontrivial prompt framing"
        assert "Where was the friction?" in prompt, "Expected friction question"
        # Error-only prompt text must NOT appear
        assert "The task had errors" not in prompt, "Error-only prompt must not be used for clean trace"
        assert entry["reflection"] == "Friction was in repeated advisory runs."

    def test_generate_reflection_uses_error_prompt_for_error_trace(self):
        """generate_reflection picks the error prompt when trace contains blocking markers."""
        from ouroboros.reflection import generate_reflection

        captured = {}

        class FakeLlm:
            def chat(self, *, messages, model, reasoning_effort, max_tokens):
                captured["prompt"] = messages[0]["content"]
                return {"content": "Root cause was missing tests."}, {"cost": 0}

        error_trace = {"tool_calls": [
            {"tool": "commit_reviewed", "args": {}, "is_error": False,
             "result": "⚠️ REVIEW_BLOCKED: tests_affected"},
        ]}
        generate_reflection(
            task={"id": "task-3", "type": "task", "text": "Blocked commit"},
            llm_trace=error_trace,
            trace_summary="1 tool call, 0 errors (but REVIEW_BLOCKED)",
            llm_client=FakeLlm(),
            usage_dict={"rounds": 5, "cost": 1.0},
        )
        prompt = captured["prompt"]
        assert "The task had errors or blocking events" in prompt
        assert "high round count or high cost" not in prompt

    def test_generate_reflection_includes_review_evidence(self):
        from ouroboros.reflection import generate_reflection

        captured = {}

        class FakeLlm:
            def chat(self, *, messages, model, reasoning_effort, max_tokens):
                captured["prompt"] = messages[0]["content"]
                return {"content": "Reflection mentions tests_affected."}, {"cost": 0}

        entry = generate_reflection(
            task={"id": "task-1", "type": "task", "text": "Fix commit flow"},
            llm_trace={"tool_calls": [{
                "tool": "commit_reviewed",
                "is_error": False,
                "result": "⚠️ REVIEW_BLOCKED: blocked by tests_affected",
            }]},
            trace_summary="repo_commit blocked",
            llm_client=FakeLlm(),
            usage_dict={"rounds": 3, "cost": 0.01},
            review_evidence={
                "has_evidence": True,
                "recent_attempts": [{
                    "status": "blocked",
                    "critical_findings": [{
                        "severity": "critical",
                        "item": "tests_affected",
                        "reason": "broken",
                    }],
                }],
            },
        )

        assert "Structured review evidence" in captured["prompt"]
        assert "tests_affected" in captured["prompt"]
        assert entry["review_evidence"]["has_evidence"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Reflection context loading
# ─────────────────────────────────────────────────────────────────────────────

class TestReflectionContextLoading:
    """build_recent_sections must load execution reflections from JSONL."""

    def test_reflections_loaded_when_file_exists(self):
        from ouroboros.context import build_recent_sections
        source = inspect.getsource(build_recent_sections)
        assert "task_reflections.jsonl" in source, (
            "build_recent_sections must load from task_reflections.jsonl"
        )
        assert "Execution reflections" in source, (
            "Section header must contain 'Execution reflections'"
        )

    def test_reflection_entry_format(self):
        """Reflection entries must include required fields."""
        from ouroboros.reflection import _detect_markers, _collect_error_details
        trace = {"tool_calls": [
            {"tool": "commit_reviewed", "is_error": True,
             "result": "⚠️ REVIEW_BLOCKED: test"},
        ]}
        markers = _detect_markers(trace)
        assert "REVIEW_BLOCKED" in markers

        details = _collect_error_details(trace)
        assert "commit_reviewed" in details
        assert "REVIEW_BLOCKED" in details


# ─────────────────────────────────────────────────────────────────────────────
# emit_task_results: reflection stays OFF the reply critical path
# ─────────────────────────────────────────────────────────────────────────────

class TestEmitTaskResultsReflectionNotOnCriticalPath:
    """Regression guard for the v4.39.0 UX regression: reflection and backlog
    persistence must not run on the reply critical path. The synchronous
    variant added a 1–3 s LLM round before `send_message` was dispatched;
    the async daemon-thread variant keeps reply latency low.
    """

    def _make_minimal_env(self, tmp_path):
        import pathlib

        class FakeEnv:
            drive_root = tmp_path
            repo_dir = str(tmp_path)

            def drive_path(self, sub):
                return pathlib.Path(self.drive_root) / sub

        return FakeEnv()

    def test_reflection_not_called_synchronously_in_emit_task_results(self, tmp_path):
        """emit_task_results must NOT invoke `_run_reflection` inline — it
        belongs inside the daemon thread started by
        `_run_post_task_processing_async`."""
        import unittest.mock as mock
        from ouroboros.agent_task_pipeline import emit_task_results

        env = self._make_minimal_env(tmp_path)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)

        task = {"id": "t1", "type": "task", "text": "goal", "chat_id": 1}
        usage = {"rounds": 5, "cost": 1.0, "prompt_tokens": 0, "completion_tokens": 0}
        llm_trace = {"tool_calls": []}

        with mock.patch("ouroboros.agent_task_pipeline._run_reflection") as mock_refl, \
             mock.patch("ouroboros.agent_task_pipeline._update_improvement_backlog") as mock_bl, \
             mock.patch("ouroboros.agent_task_pipeline._run_post_task_processing_async") as mock_async, \
             mock.patch("ouroboros.agent_task_pipeline._run_chat_consolidation"), \
             mock.patch("ouroboros.agent_task_pipeline._run_scratchpad_consolidation"), \
             mock.patch("ouroboros.agent_task_pipeline._store_task_result"), \
             mock.patch("ouroboros.review_evidence.collect_review_evidence",
                        return_value={}, create=True):
            pending_events: list = []
            emit_task_results(
                env=env, memory=mock.MagicMock(), llm=mock.MagicMock(),
                pending_events=pending_events,
                task=task, text="reply",
                usage=usage, llm_trace=llm_trace,
                start_time=0.0,
                drive_logs=tmp_path / "logs",
            )

        # Reflection and backlog are the responsibility of the async helper —
        # emit_task_results must not call them directly. Otherwise reply
        # latency regresses.
        mock_refl.assert_not_called()
        mock_bl.assert_not_called()
        # The async helper must still be invoked exactly once.
        mock_async.assert_called_once()

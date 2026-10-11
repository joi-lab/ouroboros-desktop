"""WS6: reflection surfaces a tool-usage profile so the LLM can spot under-use."""

from __future__ import annotations

from ouroboros.reflection import _tool_usage_profile


def test_tool_usage_profile_counts_and_flags_shell_reader():
    trace = {"tool_calls": [
        {"tool": "run_command", "args": {"cmd": "grep -r foo ."}},
        {"tool": "run_command", "args": {"cmd": "cat src/main.py"}},
        {"tool": "search_code", "args": {"query": "foo"}},
        {"tool": "search_code", "args": {"query": "bar"}},
        {"tool": "read_file", "args": {"path": "x.py"}},
    ]}
    profile = _tool_usage_profile(trace)
    assert "search_code×2" in profile
    assert "run_command×2" in profile
    assert "read_file×1" in profile
    # grep + cat via run_command are flagged as shell-as-reader/search.
    assert "shell-as-reader/search" in profile
    assert "2 call(s)" in profile


def test_tool_usage_profile_no_shell_reader_note_when_clean():
    trace = {"tool_calls": [
        {"tool": "query_code", "args": {"op": "symbols"}},
        {"tool": "read_file", "args": {"path": "x.py"}},
    ]}
    profile = _tool_usage_profile(trace)
    assert "query_code×1" in profile
    assert "shell-as-reader" not in profile


def test_tool_usage_profile_empty():
    assert _tool_usage_profile({"tool_calls": []}) == "(no tool calls recorded)"
    assert _tool_usage_profile({}) == "(no tool calls recorded)"

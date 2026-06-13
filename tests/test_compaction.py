"""Tests for tool-history compaction protection (context_compaction.py)."""
from ouroboros.context_compaction import compact_tool_history, _COMPACTION_PROTECTED_TOOLS


def _make_messages(tool_name: str, result_content: str, num_rounds: int = 8):
    """Build a message list with num_rounds of tool calls, all using the same tool."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result_content,
        })
    return messages


def _make_large_arg_messages(tool_name: str, num_rounds: int = 8):
    """Build messages whose old assistant tool-call payloads should compact."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    large_args = '{"content": "' + ("x" * 1000) + '"}'
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": large_args},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": "ok",
        })
    return messages


def test_protected_tool_results_survive_compaction():
    """repo_commit results must not be truncated even in old rounds."""
    original_result = "OK: committed to ouroboros: v3.19.0 review feedback applied"
    msgs = _make_messages("commit_reviewed", original_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    commit_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == original_result
    ]
    assert len(commit_results) == 10, "All repo_commit results must survive compaction"


def test_warning_results_survive_compaction():
    """Results starting with warning emoji must not be truncated."""
    warn_result = "\u26a0\ufe0f REVIEW_BLOCKED: tests failed, commit rejected. Fix errors first."
    msgs = _make_messages("run_command", warn_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    warning_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == warn_result
    ]
    assert len(warning_results) == 10, "Warning-prefixed results must survive compaction"


def test_old_assistant_tool_payloads_are_compacted():
    """Fallback compaction should compact oversized old assistant tool-call payloads."""
    msgs = _make_large_arg_messages("write_file", num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    compacted_assistants = [
        m for m in compacted
        if m.get("role") == "assistant"
        and m.get("tool_calls")
        and "<<CONTENT_OMITTED len=" in m["tool_calls"][0]["function"]["arguments"]
    ]
    assert len(compacted_assistants) >= 4, "Old oversized assistant tool-call payloads should be compacted"


# ── Protected-content detection ──────────────────────────────────────────────
#
# v4.34.0: the structured-reflection checkpoint ceremony was retired, so
# assistant messages with `CHECKPOINT_REFLECTION` / `CHECKPOINT_ANOMALY`
# text no longer need compaction protection — they no longer exist. The
# remaining protected-content rule covers tool-result messages for
# critical tools and explicit error markers (`⚠️`-prefixed tool output).


def test_round_has_protected_content_ignores_normal_assistant_text():
    """Normal assistant messages (no tool role, no error marker) must not be protected.

    Previously the function also protected `CHECKPOINT_REFLECTION` /
    `CHECKPOINT_ANOMALY` markers; that branch was removed in v4.34.0 along
    with the audit-only checkpoint ceremony. This test guards against a
    regression that would re-introduce any checkpoint-text protection.
    """
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "Normal reasoning without any reflection marker",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "file content",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is False


def test_round_has_protected_content_does_not_protect_checkpoint_text():
    """v4.34.0 regression guard: legacy CHECKPOINT_REFLECTION text is no longer
    protected. A future edit that accidentally re-adds the assistant-content
    detection would silently bloat transcripts with stale audit artifacts.
    """
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "CHECKPOINT_REFLECTION:\n- Known: x\n- Blocker: none",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert _round_has_protected_content(messages, 0, 1) is False


def test_round_has_protected_content_protects_error_tool_results():
    """Tool-result messages prefixed with ⚠️ remain protected from compaction —
    this was the other half of the pre-v4.34.0 rule and is unaffected by the
    checkpoint refactor.
    """
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "⚠️ failed to read path: permission denied",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is True

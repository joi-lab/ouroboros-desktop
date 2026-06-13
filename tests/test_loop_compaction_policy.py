"""
Tests for the compaction policy in ouroboros/loop.py.

All tests that exercise the compaction branch import and patch the PRODUCTION
symbols (`compact_tool_history_llm`, `_estimate_messages_chars`) so that drift
in the real loop code causes test failures rather than silently passing against
a copied replica.

The multi-round tests keep `call_llm_with_retry` alive for N rounds by
returning a tool-calling response for the first N-1 rounds, then a text-only
response to stop the loop.  `handle_tool_calls` is also patched to avoid
importing actual tool implementations.

Covers:
1. _estimate_messages_chars: content, multipart (text + non-text), tool_calls,
   tool_call_id, image_url blocks, empty list.
2. Remote mode: no routine compaction up to round 13 under normal context.
3. Remote mode: emergency compaction fires when _estimate_messages_chars > 1.2M.
4. Emergency path fires unconditionally on checkpoint rounds.
5. Local mode: routine compaction fires at round 7 when messages > 40
   (verified by driving the real loop to round 7).
6. Local routine compaction is suppressed on checkpoint rounds.
7. Manual _pending_compaction fires in both modes and is not suppressed
   by checkpoint rounds.
8. Manual compaction takes precedence over emergency threshold.
"""
from __future__ import annotations

import json
import queue
import types
import unittest
from unittest.mock import MagicMock, patch, call as mock_call


# ---------------------------------------------------------------------------
# Imports from production code
# ---------------------------------------------------------------------------
from ouroboros.loop import _estimate_messages_chars as _emc


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------

def _msg(role: str, content="", tool_calls=None, tool_call_id: str = "") -> dict:
    m: dict = {"role": role, "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    if tool_call_id:
        m["tool_call_id"] = tool_call_id
    return m


def _tool_round(tc_id: str, content: str = "r", result: str = "ok") -> list:
    """One assistant+tool pair."""
    return [
        _msg("assistant", content, tool_calls=[
            {"id": tc_id, "function": {"name": "noop", "arguments": "{}"}}
        ]),
        _msg("tool", result, tool_call_id=tc_id),
    ]


def _make_tool_rounds(n: int, content_size: int = 10) -> list:
    msgs = []
    for i in range(n):
        msgs.extend(_tool_round(f"tc{i}", "r" * content_size, "s" * content_size))
    return msgs


def _make_fake_registry(messages, pending_compaction=None):
    """Build a minimal fake ToolRegistry with the compaction context attrs."""
    fake_ctx = types.SimpleNamespace(
        event_queue=None,
        task_id="test",
        messages=messages,
        active_model_override=None,
        active_effort_override=None,
        active_use_local_override=None,
        pending_events=[],
        _pending_compaction=pending_compaction,
    )
    fake_registry = MagicMock()
    fake_registry._ctx = fake_ctx
    return fake_registry


def _run_loop(messages, *, use_local=False, pending_compaction=None,
              rounds_before_stop=1, checkpoint_on_round=None,
              checkpoint_persist_ok=True):
    """
    Drive run_llm_loop with mocked LLM and tools.

    Parameters
    ----------
    rounds_before_stop:
        Number of tool-calling rounds before the LLM returns a text-only
        response to terminate the loop.
    checkpoint_on_round:
        If set, _maybe_inject_self_check returns True only on that round
        number (simulating a checkpoint injection).
    checkpoint_persist_ok:
        Whether the forensic pre-compaction checkpoint write succeeds.

    Returns
    -------
    compaction_calls : list of dict
        Each entry: {"keep_recent": N}
    """
    import ouroboros.loop as loop_mod

    compaction_calls = []

    def fake_compact(msgs, keep_recent=6):
        compaction_calls.append({"keep_recent": keep_recent})
        return list(msgs), {"cost": 0.0}

    call_count = [0]

    def fake_llm_call(llm, msgs, model, tools, effort,
                      max_retries, drive_logs, task_id, round_idx,
                      event_queue, accum, task_type, use_local=False):
        call_count[0] += 1
        if call_count[0] < rounds_before_stop:
            # Return a tool-calling response to keep the loop alive
            return ({"role": "assistant", "content": "",
                     "tool_calls": [{"id": f"tc_live_{call_count[0]}",
                                     "function": {"name": "noop", "arguments": "{}"}}]}, 0.0)
        # Final text-only response — terminates the loop
        return ({"role": "assistant", "content": "done", "tool_calls": []}, 0.0)

    def fake_handle_tool_calls(tool_calls, tools, drive_logs, task_id,
                                stateful_executor, messages, llm_trace, emit_progress):
        # Append a fake tool result so the message list grows
        for tc in tool_calls:
            messages.append({"role": "tool",
                              "content": "fake_result",
                              "tool_call_id": tc.get("id", "x")})
        return 0  # no errors

    def fake_self_check(round_idx, max_rounds, messages, accumulated_usage,
                        emit_progress, **kwargs):
        if checkpoint_on_round is not None and round_idx == checkpoint_on_round:
            return True
        return False

    fake_registry = _make_fake_registry(messages, pending_compaction)
    fake_llm = MagicMock()
    fake_llm.default_model.return_value = "test-model"

    env_patch = {"USE_LOCAL_MAIN": "1" if use_local else ""}

    with patch.object(loop_mod, "compact_tool_history_llm", side_effect=fake_compact), \
         patch.object(loop_mod, "call_llm_with_retry", side_effect=fake_llm_call), \
         patch.object(loop_mod, "_drain_incoming_messages", return_value=None), \
         patch.object(loop_mod, "_maybe_inject_self_check", side_effect=fake_self_check), \
         patch.object(loop_mod, "_persist_compaction_checkpoint", return_value=checkpoint_persist_ok), \
         patch.object(loop_mod, "seal_task_transcript", return_value=None), \
         patch.object(loop_mod, "initial_tool_schemas", return_value=[]), \
         patch.object(loop_mod, "_setup_dynamic_tools",
                      side_effect=lambda t, s, m: (s, set())), \
         patch.object(loop_mod, "handle_tool_calls", side_effect=fake_handle_tool_calls), \
         patch.dict("os.environ", env_patch, clear=False):
        loop_mod.run_llm_loop(
            messages=messages,
            tools=fake_registry,
            llm=fake_llm,
            drive_logs=MagicMock(),
            emit_progress=lambda _: None,
            incoming_messages=queue.Queue(),
            task_type="task",
            task_id="test",
        )

    return compaction_calls


# ===========================================================================
# 1.  _estimate_messages_chars
# ===========================================================================

class TestEstimateMessagesChars(unittest.TestCase):

    def test_plain_content_counted(self):
        msgs = [_msg("assistant", "hello")]
        self.assertEqual(_emc(msgs), 5)

    def test_multipart_text_block_counted(self):
        # A text block is serialised as {"type":"text","text":"abc"} → counted fully
        block = {"type": "text", "text": "abc"}
        msgs = [_msg("user", [block])]
        expected = len(json.dumps(block, ensure_ascii=False))
        self.assertEqual(_emc(msgs), expected)

    def test_multipart_image_url_block_counted(self):
        """Non-text multipart blocks (image_url) must be fully counted."""
        block = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 100}}
        msgs = [_msg("user", [block])]
        result = _emc(msgs)
        # Must be substantially more than just len("") = 0
        self.assertGreater(result, 100,
            "image_url block must be counted in size estimate")
        self.assertEqual(result, len(json.dumps(block, ensure_ascii=False)))

    def test_tool_calls_counted(self):
        tc = [{"id": "t1", "function": {"name": "foo", "arguments": "{}"}}]
        msgs = [_msg("assistant", "", tool_calls=tc)]
        expected = len(json.dumps(tc, ensure_ascii=False))
        self.assertEqual(_emc(msgs), expected)

    def test_tool_call_id_counted(self):
        msgs = [_msg("tool", "ok", tool_call_id="tid123")]
        self.assertEqual(_emc(msgs), len("ok") + len("tid123"))

    def test_empty_list_returns_zero(self):
        self.assertEqual(_emc([]), 0)

    def test_multiple_messages_summed(self):
        msgs = [_msg("user", "abc"), _msg("assistant", "de")]
        self.assertEqual(_emc(msgs), 5)

    def test_large_tool_call_arguments_counted(self):
        big_arg = "x" * 10_000
        tc = [{"id": "t1", "function": {"name": "foo", "arguments": big_arg}}]
        msgs = [_msg("assistant", "", tool_calls=tc)]
        self.assertGreater(_emc(msgs), 10_000)


# ===========================================================================
# 2.  Remote mode — no routine compaction
# ===========================================================================

class TestCompactionPolicyRemote(unittest.TestCase):

    def test_no_routine_compaction_round_1_to_13(self):
        """Remote: no compaction in the first 13 rounds with small context."""
        messages = _make_tool_rounds(5, content_size=50)
        # Run 13 rounds (12 tool-calling + 1 final text)
        calls = _run_loop(messages, use_local=False, rounds_before_stop=13)
        self.assertEqual(calls, [],
            "Remote mode should not compact with small context over 13 rounds")

    def test_emergency_compaction_fires_when_large_context(self):
        """Remote: emergency fires when _estimate_messages_chars > 1.2M."""
        messages = _make_tool_rounds(5, content_size=50)
        # Add a huge message to push size over threshold
        messages.append({"role": "user", "content": "x" * 1_300_000})
        calls = _run_loop(messages, use_local=False, rounds_before_stop=1)
        self.assertTrue(
            any(c["keep_recent"] == 50 for c in calls),
            "Emergency compaction (keep_recent=50) must fire at >1.2M chars",
        )

    def test_emergency_fires_on_checkpoint_round(self):
        """Emergency compaction is NOT suppressed by checkpoint injection."""
        messages = _make_tool_rounds(5, content_size=50)
        messages.append({"role": "user", "content": "x" * 1_300_000})
        # Simulate checkpoint on round 1
        calls = _run_loop(messages, use_local=False, rounds_before_stop=1,
                          checkpoint_on_round=1)
        self.assertTrue(
            any(c["keep_recent"] == 50 for c in calls),
            "Emergency compaction must be unconditional — not suppressed by checkpoint",
        )

    def test_emergency_counts_image_url_blocks(self):
        """image_url multipart blocks must be counted by the emergency guard."""
        big_image_block = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + "A" * 1_300_000},
        }
        messages = [{"role": "user", "content": [big_image_block]}]
        size = _emc(messages)
        self.assertGreater(size, 1_200_000,
            "_estimate_messages_chars must count image_url blocks in size")
        calls = _run_loop(messages, use_local=False, rounds_before_stop=1)
        self.assertTrue(
            any(c["keep_recent"] == 50 for c in calls),
            "Emergency compaction must fire when image_url content pushes size over threshold",
        )

    def test_emergency_compaction_skips_when_checkpoint_persist_fails(self):
        """Compaction is fail-closed: pre-compaction transcript must be durable."""
        messages = _make_tool_rounds(5, content_size=50)
        messages.append({"role": "user", "content": "x" * 1_300_000})
        calls = _run_loop(
            messages,
            use_local=False,
            rounds_before_stop=1,
            checkpoint_persist_ok=False,
        )
        self.assertEqual(
            calls,
            [],
            "Compaction must not mutate context when the forensic checkpoint write fails",
        )


# ===========================================================================
# 3.  Local mode — routine compaction at round > 6 when messages > 40
# ===========================================================================

class TestCompactionPolicyLocal(unittest.TestCase):

    def test_local_routine_compaction_fires_at_round_7(self):
        """
        Local: routine compaction fires once messages > 40 after round > 6.

        We seed 50 messages before the loop starts so the count check passes
        on round 7.  Each fake handle_tool_calls appends 1 tool-result message
        per round, so by round 7 there are at least 57 messages — well above 40.
        """
        messages = _make_tool_rounds(25, content_size=10)  # 50 messages
        calls = _run_loop(messages, use_local=True, rounds_before_stop=8)
        self.assertTrue(
            any(c["keep_recent"] == 20 for c in calls),
            "Local routine compaction (keep_recent=20) must fire at round 7 when messages > 40",
        )

    def test_local_routine_suppressed_on_checkpoint_round(self):
        """
        Local routine compaction is suppressed when _maybe_inject_self_check
        returns True on a round where routine compaction would otherwise fire.

        Strategy: seed 50 messages (> 40 from round 1), checkpoint on round 7.
        Round 7 is the ONLY round in this 8-round run where checkpoint fires.
        We verify that across all rounds, compact was called FEWER times than
        total rounds-where-eligible (i.e. the checkpoint round was skipped).

        We compare against a baseline run without checkpoint injection: if
        suppression works, the checkpoint run has fewer compact calls.
        """
        messages_base = _make_tool_rounds(25, content_size=10)
        messages_ckpt = _make_tool_rounds(25, content_size=10)

        calls_base = _run_loop(messages_base, use_local=True, rounds_before_stop=8)
        calls_ckpt = _run_loop(messages_ckpt, use_local=True, rounds_before_stop=8,
                               checkpoint_on_round=7)

        routine_base = [c for c in calls_base if c["keep_recent"] == 20]
        routine_ckpt = [c for c in calls_ckpt if c["keep_recent"] == 20]

        self.assertLess(
            len(routine_ckpt), len(routine_base),
            "Checkpoint suppression should reduce the number of routine local compact calls",
        )

    def test_local_boundary_exactly_3_messages_no_compact(self):
        """Boundary: very few messages → strict len > 40 means no compact even after many rounds."""
        # Start with only 3 messages; fake handle_tool_calls adds 1 per round.
        # After 7 rounds: 3 + 7 = 10 messages — never exceeds 40.
        messages = _make_tool_rounds(1, content_size=10)  # 2 messages
        messages.append({"role": "user", "content": "hello"})  # 3 total
        calls = _run_loop(messages, use_local=True, rounds_before_stop=8)
        routine_calls = [c for c in calls if c["keep_recent"] == 20]
        self.assertEqual(
            routine_calls, [],
            "With only ~10 messages after 7 rounds, local compaction must NOT fire",
        )


# ===========================================================================
# 4.  Manual _pending_compaction
# ===========================================================================

class TestCompactionPolicyManual(unittest.TestCase):

    def test_manual_fires_in_remote_mode(self):
        messages = _make_tool_rounds(5, content_size=20)
        calls = _run_loop(messages, use_local=False, pending_compaction=30)
        self.assertTrue(any(c["keep_recent"] == 30 for c in calls),
                        "Manual compaction must fire in remote mode")

    def test_manual_fires_in_local_mode(self):
        messages = _make_tool_rounds(5, content_size=20)
        calls = _run_loop(messages, use_local=True, pending_compaction=15)
        self.assertTrue(any(c["keep_recent"] == 15 for c in calls),
                        "Manual compaction must fire in local mode")

    def test_manual_fires_on_checkpoint_round(self):
        """_pending_compaction is not suppressed on checkpoint rounds."""
        messages = _make_tool_rounds(5, content_size=20)
        calls = _run_loop(messages, use_local=False, pending_compaction=25,
                          checkpoint_on_round=1)
        self.assertTrue(any(c["keep_recent"] == 25 for c in calls),
                        "Manual compaction must fire even on checkpoint rounds")

    def test_manual_takes_precedence_over_emergency(self):
        """_pending_compaction wins (elif chain — manual is first)."""
        messages = [{"role": "user", "content": "x" * 1_300_000}]
        calls = _run_loop(messages, use_local=False, pending_compaction=10)
        self.assertTrue(calls, "Compaction must fire")
        # First call uses manual keep_recent, not emergency keep_recent=50
        self.assertEqual(calls[0]["keep_recent"], 10,
                         "Manual keep_recent must take precedence over emergency")


if __name__ == "__main__":
    unittest.main()

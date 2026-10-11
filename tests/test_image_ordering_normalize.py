"""P4a: an image-bearing user turn injected mid-tool-round must never split an assistant
tool_use from its matching tool_result. The fix lives in the single send-time chokepoint
LLMClient._normalize_system_message_placement (covers every provider builder)."""
from __future__ import annotations

from ouroboros.llm import LLMClient


def _img_user(text="see this"):
    return {"role": "user", "content": [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]}


def _assistant_toolcall(cid="c1", name="view_image"):
    return {"role": "assistant", "content": None,
            "tool_calls": [{"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}]}


def test_image_user_turn_deferred_after_tool_result():
    out = LLMClient()._normalize_system_message_placement([
        {"role": "user", "content": "start"},
        _assistant_toolcall(),
        _img_user(),  # injected mid-round (the bug) — must be deferred
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "user", "content": "continue"},
    ])
    roles = [m["role"] for m in out]
    ai = roles.index("assistant")
    assert roles[ai + 1] == "tool", roles  # tool_result adjacent to its tool_use
    img_positions = [i for i, m in enumerate(out) if m["role"] == "user" and isinstance(m.get("content"), list)]
    assert img_positions and all(p > ai + 1 for p in img_positions), roles


def test_tool_result_bearing_user_turn_not_deferred():
    """Negative guard: a user turn that IS the tool answer (carries a tool_result block) must
    stay adjacent to the assistant tool_use, even if it also has an image block."""
    out = LLMClient()._normalize_system_message_placement([
        {"role": "user", "content": "start"},
        _assistant_toolcall(name="x"),
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "ok"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
        ]},
        {"role": "user", "content": "next"},
    ])
    roles = [m["role"] for m in out]
    ai = roles.index("assistant")
    nxt = out[ai + 1]
    assert isinstance(nxt.get("content"), list)
    assert any(b.get("type") == "tool_result" for b in nxt["content"]), "tool answer was wrongly deferred"


def test_mixed_system_notice_and_image_both_flush_after_window():
    out = LLMClient()._normalize_system_message_placement([
        {"role": "user", "content": "start"},
        _assistant_toolcall(name="x"),
        {"role": "system", "content": "late notice"},
        _img_user(),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        {"role": "user", "content": "go"},
    ])
    roles = [m["role"] for m in out]
    ai = roles.index("assistant")
    assert roles[ai + 1] == "tool", roles  # tool_result still adjacent
    assert roles[ai + 2] == "user" and roles[ai + 3] == "user"  # demoted notice + image both after


def test_normalizer_idempotent():
    base = [
        _assistant_toolcall(name="x"),
        _img_user(),
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    once = LLMClient()._normalize_system_message_placement(base)
    twice = LLMClient()._normalize_system_message_placement(once)
    assert [m["role"] for m in once] == [m["role"] for m in twice]

"""Tests for the 2026-05-05 TOOL_ARG_ERROR signature hint.

When the model emits a tool call with an unexpected kwarg / missing
required arg / wrong arity, the runtime catches the TypeError at the
dispatch boundary in ``ouroboros/tools/registry.py``. The bare Python
exception message says what *broke* but not what would have *worked*,
so smaller local-LLM coders loop on the same malformation.

Observed 2026-05-04: a consolidation task on qwen3.6-35b called
``data_write(path=..., content=..., force=True)`` and the runtime
surfaced "got an unexpected keyword argument 'force'" with no list of
valid kwargs. The signature hint enriches the error with a one-line
reminder of the handler's accepted arguments + defaults so the next
attempt can self-correct.

Pinned tests so the recovery surface can't be silently removed.
"""

from __future__ import annotations

from ouroboros.tools.registry import _format_handler_signature_hint


def _data_write(ctx, path: str, content: str, mode: str = "overwrite") -> str:
    """Mirror of ``ouroboros.tools.core._data_write`` signature for testing."""
    return ""


def _no_kwargs(ctx) -> str:
    """Handler that accepts only ctx — exercise the empty-params branch."""
    return ""


def _has_var_kwargs(ctx, path: str, **opts) -> str:
    """Handler with **opts — VAR_KEYWORD must be skipped from the hint."""
    return ""


def test_unexpected_kwarg_emits_signature_hint():
    err = "_data_write() got an unexpected keyword argument 'force'"
    hint = _format_handler_signature_hint("data_write", _data_write, err)
    assert "Valid args for data_write" in hint
    assert "path" in hint and "content" in hint
    assert "mode='overwrite'" in hint
    # The internal ctx must never appear in the surfaced hint.
    assert "ctx" not in hint


def test_missing_required_arg_emits_signature_hint():
    err = "_data_write() missing 1 required positional argument: 'content'"
    hint = _format_handler_signature_hint("data_write", _data_write, err)
    assert "Valid args for data_write" in hint
    assert "path" in hint and "content" in hint


def test_multiple_values_emits_signature_hint():
    err = "_data_write() got multiple values for argument 'path'"
    hint = _format_handler_signature_hint("data_write", _data_write, err)
    assert "Valid args for data_write" in hint


def test_unrelated_typeerror_emits_no_hint():
    """A TypeError raised inside the handler body (e.g. ``int + str``)
    must NOT trigger the signature hint — that would be misleading
    noise. Only kwarg/arity-shaped TypeErrors get the hint."""
    err = "unsupported operand type(s) for +: 'int' and 'str'"
    hint = _format_handler_signature_hint("data_write", _data_write, err)
    assert hint == ""


def test_handler_with_only_ctx_emits_no_hint():
    """If the handler accepts only ``ctx``, there are no model-visible
    args to enumerate — the hint suppresses to avoid an empty string
    that would still allocate a header line."""
    err = "_no_kwargs() got an unexpected keyword argument 'foo'"
    hint = _format_handler_signature_hint("no_kwargs", _no_kwargs, err)
    assert hint == ""


def test_var_keyword_param_is_skipped():
    """``**opts`` is variadic — listing it as a literal arg name would
    be misleading. The hint shows only concrete named params."""
    err = "_has_var_kwargs() got an unexpected keyword argument 'unknown'"
    hint = _format_handler_signature_hint("vk", _has_var_kwargs, err)
    assert "path" in hint
    assert "opts" not in hint
    assert "**" not in hint

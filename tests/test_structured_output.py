"""Phase 7 — generalized three-layer structured-output parser.

Lifts the proven safety-parser pattern into a reusable helper. The
existing ``test_safety_response_parser.py`` is a regression guard for the
safety call site (its 16 tests must keep passing after the refactor);
this file covers the helper in isolation so other call sites have
documented behaviour to depend on.
"""

from __future__ import annotations

import pytest

from ouroboros.structured_output import (
    coerce_structured,
    _default_reason_extractor,
    _try_strict_json,
    _try_embedded_json,
)


SAFETY_MAP = {
    "DANGEROUS":  {"status": "DANGEROUS"},
    "SUSPICIOUS": {"status": "SUSPICIOUS"},
    "SAFE":       {"status": "SAFE"},
}


def _restrictive_winner(found):
    return "DANGEROUS" if "DANGEROUS" in found else (
        "SUSPICIOUS" if "SUSPICIOUS" in found else "SAFE"
    )


# ---------------------------------------------------------------------------
# Layer 1 — strict JSON
# ---------------------------------------------------------------------------

def test_strict_json_object():
    assert _try_strict_json('{"x": 1, "y": "ok"}') == {"x": 1, "y": "ok"}


def test_strict_json_with_json_fence():
    assert _try_strict_json('```json\n{"v": 1}\n```') == {"v": 1}


def test_strict_json_with_plain_fence():
    assert _try_strict_json('```\n{"v": 1}\n```') == {"v": 1}


def test_strict_json_returns_none_for_non_dict():
    """Lists / scalars / nulls are valid JSON but not what callers want."""
    assert _try_strict_json("[1,2,3]") is None
    assert _try_strict_json('"hello"') is None
    assert _try_strict_json("null") is None


def test_strict_json_returns_none_for_garbage():
    assert _try_strict_json("not json") is None
    assert _try_strict_json("") is None


# ---------------------------------------------------------------------------
# Layer 2 — embedded JSON
# ---------------------------------------------------------------------------

def test_embedded_json_in_prose():
    text = 'Here is my verdict: {"status": "SAFE"} — proceed.'
    assert _try_embedded_json(text) == {"status": "SAFE"}


def test_embedded_json_picks_first_valid():
    """When multiple braces appear, first parseable object wins."""
    text = 'noise { broken } valid {"v": 1} more {"v": 2}'
    assert _try_embedded_json(text) == {"v": 1}


def test_embedded_json_handles_nested():
    text = 'prefix {"outer": {"inner": 1}} suffix'
    assert _try_embedded_json(text) == {"outer": {"inner": 1}}


def test_embedded_json_returns_none_when_no_braces():
    assert _try_embedded_json("no braces here") is None


def test_embedded_json_returns_none_when_no_valid_object():
    assert _try_embedded_json("{ malformed { broken }") is None


# ---------------------------------------------------------------------------
# Layer 3 — keyword fallback
# ---------------------------------------------------------------------------

def test_keyword_fallback_picks_only_match():
    out = coerce_structured("**Verdict:** SAFE", keyword_fallback_map=SAFETY_MAP)
    assert out == {"status": "SAFE"}


def test_keyword_fallback_default_first_match_wins():
    """No tie-breaker → iteration order of the map (DANGEROUS first here)."""
    out = coerce_structured(
        "may be SAFE or DANGEROUS depending",
        keyword_fallback_map=SAFETY_MAP,
    )
    assert out == {"status": "DANGEROUS"}


def test_keyword_fallback_with_restrictive_winner():
    out = coerce_structured(
        "Initially I thought SAFE, but actually DANGEROUS",
        keyword_fallback_map=SAFETY_MAP,
        restrictive_winner=_restrictive_winner,
    )
    assert out == {"status": "DANGEROUS"}


def test_keyword_fallback_returns_none_when_no_match():
    out = coerce_structured(
        "I'm not sure how to evaluate this",
        keyword_fallback_map=SAFETY_MAP,
    )
    assert out is None


def test_keyword_fallback_with_reason_extractor():
    out = coerce_structured(
        "**SAFE**: read-only operation on localhost",
        keyword_fallback_map=SAFETY_MAP,
        restrictive_winner=_restrictive_winner,
        reason_extractor=_default_reason_extractor,
    )
    assert out["status"] == "SAFE"
    assert "read-only" in out["reason"]


def test_keyword_fallback_with_reason_extractor_no_tail():
    """When the verdict word is at the end of the text, no reason extractable."""
    out = coerce_structured(
        "**SAFE**",
        keyword_fallback_map=SAFETY_MAP,
        restrictive_winner=_restrictive_winner,
        reason_extractor=_default_reason_extractor,
    )
    assert out["status"] == "SAFE"
    assert "reason" not in out  # extractor returned empty, no override


# ---------------------------------------------------------------------------
# coerce_structured layer ordering
# ---------------------------------------------------------------------------

def test_strict_json_preempts_embedded():
    """If the entire text parses as JSON, layer 2 is not exercised."""
    out = coerce_structured(
        '{"status": "SAFE", "extra": "..."}',
        keyword_fallback_map=SAFETY_MAP,
        restrictive_winner=_restrictive_winner,
    )
    assert out == {"status": "SAFE", "extra": "..."}


def test_embedded_json_preempts_keyword():
    """JSON inside prose wins over keyword-only matching."""
    out = coerce_structured(
        'My verdict: {"status": "SUSPICIOUS"} — but it could also be SAFE',
        keyword_fallback_map=SAFETY_MAP,
        restrictive_winner=_restrictive_winner,
    )
    # Embedded JSON found first → returns SUSPICIOUS without keyword fallback
    assert out == {"status": "SUSPICIOUS"}


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_empty_text_returns_none():
    assert coerce_structured("", keyword_fallback_map=SAFETY_MAP) is None
    assert coerce_structured(None, keyword_fallback_map=SAFETY_MAP) is None  # type: ignore[arg-type]


def test_whitespace_only_returns_none():
    assert coerce_structured("   \n\t  ", keyword_fallback_map=SAFETY_MAP) is None


def test_payload_is_copied_not_aliased():
    """Caller mutating the returned dict must not corrupt the keyword_fallback_map."""
    out = coerce_structured("**SAFE**", keyword_fallback_map=SAFETY_MAP)
    out["mutated"] = True
    assert "mutated" not in SAFETY_MAP["SAFE"]


def test_reason_extractor_failure_does_not_propagate():
    def boom(_t, _v):
        raise RuntimeError("extractor crashed")
    out = coerce_structured(
        "**SAFE** read-only",
        keyword_fallback_map=SAFETY_MAP,
        reason_extractor=boom,
    )
    # Crash is swallowed; verdict still returned with no reason
    assert out == {"status": "SAFE"}

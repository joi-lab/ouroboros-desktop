"""Regression test for PR-B: retry backoff (#15).

(#14 — not billing provider-glitch empties — was moved to CONSULT-BUGS.md: doing
it correctly requires deciding whether the durable usage SSOT in events.jsonl
should exclude finish_reason=null responses, a provider-billing semantics call
left to the maintainer.)
"""

from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_backoff_doubled_with_cap():
    """Backoff stays exponential (x4 base) and per-class capped: 30s for
    generic retryable errors, 60s for transient provider classes (v6.28.0)."""
    src = (REPO / "ouroboros" / "loop_llm_call.py").read_text(encoding="utf-8")
    assert "2.0 ** attempt * 4" in src             # doubled per-attempt backoff
    assert "min(2 ** attempt * 2, 30)" not in src  # old value gone
    assert "_TRANSIENT_BACKOFF_CAP_SEC if is_transient else 30.0" in src

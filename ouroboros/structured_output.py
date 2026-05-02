"""Reusable three-layer structured-output parser.

Lifted from ``ouroboros.safety._parse_safety_response`` so other tools
that prompt local models for structured replies (review verdicts,
plan_task JSON, etc.) can reuse the same tolerance pattern. Behaviour
of the safety call site is preserved bit-for-bit; this module is a
straight refactor.

The three layers, in order:

  1. **Strict JSON** after stripping ``` and ```json fences. Matches the
     documented contract small models try (and often fail) to follow.
  2. **Embedded JSON** — find the first balanced ``{ ... }`` block anywhere
     in the text and try to parse it. Catches well-meaning models that
     wrap their JSON in prose ("Here is my verdict: { ... }").
  3. **Keyword fallback** — scan the text for verdict words. The caller
     supplies the keyword → default-payload map and the tie-breaker
     ("most restrictive wins" for safety; could be "first wins" for plan
     verdicts; etc.). When multiple keywords appear, the tie-breaker
     decides which one carries.

Returns ``None`` when no layer produces a parseable result. Callers
decide what to do with ``None`` — safety fails closed (treats as
DANGEROUS), plan_task may default to "no_plan", review may surface
the parse failure as a degradation note, etc.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger(__name__)


def _try_strict_json(text: str) -> Optional[Dict[str, Any]]:
    """Layer 1: strip code fences and json.loads."""
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(clean)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass
    return None


def _try_embedded_json(text: str) -> Optional[Dict[str, Any]]:
    """Layer 2: scan for a balanced {...} block and try to parse it.

    Walks the text from the first ``{`` looking for the matching ``}``
    by counting brace depth. On parse failure, advances to the next ``{``
    and tries again. First successful parse wins.
    """
    clean = text.replace("```json", "").replace("```", "").strip()
    brace_start = clean.find("{")
    while brace_start != -1:
        depth = 0
        for i in range(brace_start, len(clean)):
            ch = clean[i]
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = clean[brace_start: i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
                    break
        brace_start = clean.find("{", brace_start + 1)
    return None


def _default_reason_extractor(text: str, verdict: str, char_limit: int = 200) -> str:
    """Pull up to ``char_limit`` chars after the verdict word as a heuristic
    reason. Strips leading punctuation/markdown noise."""
    upper = text.upper()
    idx = upper.find(verdict)
    if idx == -1:
        return ""
    tail = text[idx + len(verdict):].strip(" \t\n:*-—–.")
    return tail[:char_limit].strip() if tail else ""


def coerce_structured(
    text: str,
    *,
    keyword_fallback_map: Dict[str, Dict[str, Any]],
    restrictive_winner: Optional[Callable[[List[str]], str]] = None,
    reason_extractor: Optional[Callable[[str, str], str]] = None,
) -> Optional[Dict[str, Any]]:
    """Three-layer parse: strict JSON → embedded JSON → keyword fallback.

    Parameters:
      ``text`` — raw model response.
      ``keyword_fallback_map`` — dict from canonical keyword (uppercase) to
        the default payload returned when only that keyword matches.
        Example: ``{"DANGEROUS": {"status": "DANGEROUS"}, ...}``.
      ``restrictive_winner`` — when multiple keywords appear in the text,
        this callable picks the winner. Receives the list of matching
        keywords (in iteration order of ``keyword_fallback_map``) and
        returns the chosen one. Default: first match wins.
      ``reason_extractor`` — optional ``(text, verdict) -> reason_str``
        helper to fill the ``reason`` field on keyword-fallback hits.
        When omitted, no reason is added.

    Returns the dict from the first layer that succeeds, or ``None``.
    """
    if not text:
        return None
    raw = text.strip()
    if not raw:
        return None

    # Layer 1
    parsed = _try_strict_json(raw)
    if parsed is not None:
        return parsed

    # Layer 2
    parsed = _try_embedded_json(raw)
    if parsed is not None:
        return parsed

    # Layer 3
    upper = raw.upper()
    found: List[str] = []
    for keyword in keyword_fallback_map.keys():
        if keyword.upper() in upper:
            found.append(keyword)
    if not found:
        return None

    if restrictive_winner is not None:
        chosen = restrictive_winner(found)
    else:
        chosen = found[0]
    if chosen not in keyword_fallback_map:
        return None
    payload = dict(keyword_fallback_map[chosen])  # copy so callers can mutate freely

    if reason_extractor is not None:
        try:
            reason = reason_extractor(raw, chosen)
        except Exception:
            reason = ""
        # An extracted reason is more useful than a placeholder default —
        # always prefer it when non-empty.
        if reason:
            payload["reason"] = reason

    return payload

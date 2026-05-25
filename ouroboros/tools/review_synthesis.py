"""Synthesize raw reviewer claims into canonical durable review issues.

Reviewer findings are claims; one cheap LLM deduplicates before obligations are
created. On any failure, raw findings pass through unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from ouroboros.triad_review import extract_json_array
from ouroboros.tools.review_helpers import emit_review_usage

log = logging.getLogger(__name__)

# Bound cost and avoid mixed canonical/raw output on oversized finding sets.
_MAX_CLAIMS_FOR_SYNTHESIS = 30

_MIN_CLAIMS_FOR_SYNTHESIS = 2

_SYNTHESIS_PROMPT_TEMPLATE = (
    "You are a code-review claim synthesizer. You receive a list of raw findings\n"
    "from multiple independent reviewers (triad diff-reviewers + one Atlas-backed\n"
    "scope reviewer). Your job is to produce a deduplicated canonical list.\n"
    "\n"
    "## Rules\n"
    "\n"
    "1. Merge claims that share the same **root cause** in the same file/symbol\n"
    "   into ONE canonical entry. Use the most specific/concrete reason text.\n"
    "2. **Do NOT merge** findings about genuinely different bugs, even if they are\n"
    "   in the same file. One root cause = one canonical issue.\n"
    "3. If an incoming claim already carries an `obligation_id` that matches an\n"
    "   open obligation from a previous round (provided below), PRESERVE that\n"
    "   `obligation_id` on the canonical entry. This allows durable obligations\n"
    "   to survive across retries without ID rotation.\n"
    "4. If no existing obligation matches, leave `obligation_id` as \"\" — a new\n"
    "   obligation will be assigned downstream.\n"
    "5. Do NOT invent new findings. Only deduplicate what you have been given.\n"
    "6. For each canonical entry, list `evidence_from_reviewers`: which reviewer(s)\n"
    "   independently flagged this issue (use the `tag` or `model` field if present).\n"
    "7. Output ONLY valid JSON — a JSON array of canonical findings, no markdown fences,\n"
    "   no prose outside the array.\n"
    "\n"
    "## Output format (each element)\n"
    "\n"
    '{"item": "<checklist item name>", "severity": "critical|advisory",\n'
    ' "reason": "<most concrete reason>", "obligation_id": "<existing id or empty>",\n'
    ' "evidence_from_reviewers": ["<tag/model1>", "<tag/model2>"]}\n'
    "\n"
    "## Open obligations from previous rounds (match by item + reason similarity)\n"
    "\n"
    "OPEN_OBLIGATIONS_PLACEHOLDER\n"
    "\n"
    "## Raw reviewer claims to deduplicate\n"
    "\n"
    "CLAIMS_PLACEHOLDER\n"
    "\n"
    "Respond with ONLY the JSON array. No explanation.\n"
)


def _redact(text: str) -> str:
    """Redact secret-like values from a string before including it in an LLM prompt."""
    try:
        from ouroboros.tools.review_helpers import redact_prompt_secrets
        redacted, _ = redact_prompt_secrets(str(text or ""))
        return redacted
    except Exception:
        return ""


def _format_obligations(open_obligations: List[Any]) -> str:
    """Render open obligations as compact secret-redacted JSON."""
    if not open_obligations:
        return "[]"
    from ouroboros.utils import truncate_review_artifact
    items = []
    for o in open_obligations:
        raw_reason = str(getattr(o, "reason", "") or "")
        redacted_reason = _redact(raw_reason)
        items.append({
            "obligation_id": str(getattr(o, "obligation_id", "") or ""),
            "item": str(getattr(o, "item", "") or ""),
            "reason_excerpt": truncate_review_artifact(redacted_reason, limit=500),
        })
    try:
        return json.dumps(items, ensure_ascii=False, indent=2)
    except Exception:
        return "[]"


def _format_claims(findings: List[Dict[str, Any]]) -> str:
    """Render raw findings as compact JSON with secret-redacted reasons."""
    try:
        safe = []
        for f in findings:
            entry = dict(f)
            if "reason" in entry:
                entry["reason"] = _redact(str(entry["reason"] or ""))
            safe.append(entry)
        return json.dumps(safe, ensure_ascii=False, indent=2)
    except Exception:
        return "[]"


def _normalize_evidence(value: Any) -> List[str]:
    """Normalize evidence_from_reviewers without splitting bare strings into chars."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, str)]
    return []


def _parse_synthesis_output(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the synthesizer's JSON array response. Returns None on failure."""
    if not raw:
        return None
    parsed = extract_json_array(raw)
    if not isinstance(parsed, list):
        return None
    result = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if not entry.get("item"):
            continue
        canonical = {
            "item": str(entry.get("item", "") or ""),
            "severity": str(entry.get("severity", "advisory") or "advisory"),
            "reason": str(entry.get("reason", "") or ""),
            "obligation_id": str(entry.get("obligation_id", "") or ""),
            "evidence_from_reviewers": _normalize_evidence(entry.get("evidence_from_reviewers")),
            # FAIL default ensures synthesized findings create obligations downstream.
            "verdict": str(entry.get("verdict", "") or "FAIL"),
        }
        for key in ("tag", "model"):
            if key in entry:
                canonical[key] = entry[key]
        result.append(canonical)
    return result if result else None


def synthesize_to_canonical_issues(
    critical_findings: List[Dict[str, Any]],
    *,
    open_obligations: Optional[List[Any]] = None,
    ctx: Any = None,
) -> List[Dict[str, Any]]:
    """Return deduplicated findings, or original findings on any synthesis failure."""
    if not critical_findings:
        return critical_findings

    if len(critical_findings) < _MIN_CLAIMS_FOR_SYNTHESIS:
        return critical_findings

    # Oversized sets pass through unchanged; no hybrid canonical/raw tail.
    if len(critical_findings) > _MAX_CLAIMS_FOR_SYNTHESIS:
        log.debug(
            "review_synthesis: %d claims exceeds limit %d — skipping synthesis, "
            "returning original findings unchanged",
            len(critical_findings),
            _MAX_CLAIMS_FOR_SYNTHESIS,
        )
        return critical_findings

    obligations = list(open_obligations or [])

    try:
        prompt = (
            _SYNTHESIS_PROMPT_TEMPLATE
            .replace("OPEN_OBLIGATIONS_PLACEHOLDER", _format_obligations(obligations))
            .replace("CLAIMS_PLACEHOLDER", _format_claims(critical_findings))
        )
    except Exception as exc:
        log.warning("review_synthesis: failed to build prompt: %s", exc)
        return critical_findings

    try:
        raw_response = _call_synthesis_llm(prompt, ctx=ctx)
    except Exception as exc:
        log.debug("review_synthesis: LLM call raised exception: %s — using original findings", exc)
        return critical_findings

    if raw_response is None:
        log.debug("review_synthesis: LLM call returned None — using original findings")
        return critical_findings

    canonical = _parse_synthesis_output(raw_response)
    if canonical is None:
        log.debug("review_synthesis: failed to parse LLM output — using original findings")
        return critical_findings

    log.debug(
        "review_synthesis: %d raw → %d canonical",
        len(critical_findings),
        len(canonical),
    )
    return canonical


def _call_synthesis_llm(prompt: str, *, ctx: Any = None) -> Optional[str]:
    """Call the light LLM and emit usage so synthesis spend is accounted."""
    try:
        from ouroboros.llm import LLMClient
        from ouroboros.config import get_light_model

        model = get_light_model()

        client = LLMClient()

        # no_proxy avoids macOS fork-safety crashes in worker processes.
        msg, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=16384,
            reasoning_effort="low",
            no_proxy=True,
        )

        if _has_billable_usage(usage):
            resolved_model = str((usage or {}).get("resolved_model") or "") or model
            provider = str((usage or {}).get("provider") or "") if isinstance(usage, dict) else ""
            emit_review_usage(
                ctx,
                model=resolved_model,
                usage=usage,
                source="review_synthesis",
                provider=provider,
            )

        if not msg:
            return None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ]
            return "\n".join(t for t in texts if t) or None
        return str(content) if content else None

    except Exception as exc:
        log.debug("review_synthesis: LLM call failed: %s", exc)
        return None


def _has_billable_usage(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(
        usage.get(key)
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "cost", "total_cost")
    )

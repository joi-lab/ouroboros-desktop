"""Shared tri-model review primitives.

Both repo commit review and skill review ask multiple reviewer models to
return a JSON array of checklist findings. Keep parsing, quorum accounting,
and observability in one place so future review entrypoints do not re-learn
the same truncation / parse-failure bugs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ouroboros.utils import append_jsonl, utc_now_iso


@dataclass
class ReviewActorRecord:
    model_id: str
    status: str
    raw_text: str
    parsed_items: List[Dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    slot: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "status": self.status,
            "raw_text": self.raw_text,
            "parsed_items": list(self.parsed_items),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
        }


@dataclass
class ParsedTriadReview:
    findings: List[Dict[str, Any]]
    responsive_models: List[str]
    actor_records: List[ReviewActorRecord]
    errors: List[str] = field(default_factory=list)

    @property
    def quorum_met(self) -> bool:
        return len(self.responsive_models) >= 2

    @property
    def degraded_reasons(self) -> List[str]:
        degraded = [r for r in self.actor_records if r.status in {"error", "parse_failure", "partial"}]
        if not degraded or not self.quorum_met:
            return []
        reasons = [f"{r.model_id}={r.status}" for r in degraded]
        return [f"DEGRADED: {', '.join(reasons)} (quorum still met)"]


def extract_json_array(
    raw: str,
    *,
    normalize: bool = False,
    unwrap_result: bool = False,
    validate_fn: Optional[Callable[[List[Any]], bool]] = None,
) -> Optional[List[Any]]:
    """Best-effort extraction of a JSON array from model output."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("["):
                text = chunk
                break
    try:
        obj = json.loads(text)
        if unwrap_result and isinstance(obj, dict) and "result" in obj:
            text = str(obj["result"]).strip()
            obj = json.loads(text)
        if isinstance(obj, list):
            return _accepted_json_array(obj, normalize=normalize, validate_fn=validate_fn)
    except (json.JSONDecodeError, ValueError):
        pass
    ends: List[int] = []
    search_from = 0
    while True:
        pos = text.find("]", search_from)
        if pos == -1:
            break
        ends.append(pos)
        search_from = pos + 1
    for end in reversed(ends):
        starts: List[int] = []
        search_from = 0
        while True:
            pos = text.find("[", search_from)
            if pos == -1 or pos > end:
                break
            starts.append(pos)
            search_from = pos + 1
        for start in reversed(starts):
            try:
                obj = json.loads(text[start:end + 1])
                if isinstance(obj, list):
                    accepted = _accepted_json_array(obj, normalize=normalize, validate_fn=validate_fn)
                    if accepted is not None:
                        return accepted
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _accepted_json_array(
    obj: List[Any],
    *,
    normalize: bool,
    validate_fn: Optional[Callable[[List[Any]], bool]],
) -> Optional[List[Any]]:
    if validate_fn is not None and not validate_fn(obj):
        return None
    return _normalize_items(obj) if normalize else obj


def _normalize_items(items: List[Any]) -> List[Any]:
    try:
        from ouroboros.tools.review_helpers import normalize_reviewer_items
        return normalize_reviewer_items(items)
    except Exception:
        return items


def parse_model_review_results(
    result_json: Dict[str, Any],
    *,
    required_items: Optional[Sequence[str]] = None,
) -> ParsedTriadReview:
    """Parse model result envelopes into normalized findings and actor records.

    ``required_items`` enforces the skill-review matrix contract: a reviewer
    that omits a checklist item is non-responsive for quorum.
    """
    findings: List[Dict[str, Any]] = []
    responsive: List[str] = []
    records: List[ReviewActorRecord] = []
    required = set(required_items or [])
    for idx, actor in enumerate(result_json.get("results") or []):
        if not isinstance(actor, dict):
            continue
        model = str(actor.get("model") or actor.get("request_model") or "").strip()
        raw_text = str(actor.get("text") or "")
        tokens_in = int(actor.get("tokens_in", 0) or 0)
        tokens_out = int(actor.get("tokens_out", 0) or 0)
        cost_usd = float(actor.get("cost_estimate", 0.0) or 0.0)
        model_label = model or "reviewer"
        if str(actor.get("verdict") or "").upper() == "ERROR":
            records.append(ReviewActorRecord(
                model_id=model_label,
                status="error",
                raw_text=raw_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                slot=idx + 1,
            ))
            continue
        parsed = extract_json_array(raw_text, normalize=not required)
        if parsed is None:
            records.append(ReviewActorRecord(
                model_id=model_label,
                status="parse_failure",
                raw_text=raw_text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                slot=idx + 1,
            ))
            continue
        actor_findings: List[Dict[str, Any]] = []
        covered_items: set[str] = set()
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            item = str(entry.get("item") or "")
            verdict = str(entry.get("verdict") or "").upper()
            if not item or verdict not in {"PASS", "FAIL"}:
                continue
            covered_items.add(item)
            actor_findings.append({
                "item": item,
                "verdict": verdict,
                "severity": str(entry.get("severity") or "advisory").lower(),
                "reason": str(entry.get("reason") or "").strip(),
                "model": model_label,
                **({"obligation_id": str(entry.get("obligation_id") or "")} if entry.get("obligation_id") else {}),
            })
        if required and not required.issubset(covered_items):
            records.append(ReviewActorRecord(
                model_id=model_label,
                status="partial",
                raw_text=raw_text,
                parsed_items=actor_findings,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                slot=idx + 1,
            ))
            continue
        findings.extend(actor_findings)
        responsive.append(f"{model_label}#{idx + 1}")
        records.append(ReviewActorRecord(
            model_id=model_label,
            status="responded",
            raw_text=raw_text,
            parsed_items=actor_findings,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            slot=idx + 1,
        ))
    return ParsedTriadReview(findings=findings, responsive_models=responsive, actor_records=records)


def emit_review_model_error_events(ctx: Any, parsed: ParsedTriadReview, *, source: str, skill_name: str = "") -> None:
    """Persist model error / parse-failure events for observability."""
    try:
        log_path = ctx.drive_logs() / "events.jsonl"
    except Exception:
        return
    for record in parsed.actor_records:
        if record.status not in {"error", "parse_failure", "partial"}:
            continue
        if source == "skill_review":
            note = (
                "Full raw response preserved in review.json raw_actor_records "
                "when quorum succeeds; otherwise in review_history.jsonl."
            )
        else:
            note = "Full raw response preserved in triad_raw_results."
        try:
            append_jsonl(log_path, {
                "ts": utc_now_iso(),
                "type": "review_model_error",
                "source": source,
                "skill": skill_name,
                "model": record.model_id,
                "status": record.status,
                "error_note": note,
            })
        except Exception:
            pass

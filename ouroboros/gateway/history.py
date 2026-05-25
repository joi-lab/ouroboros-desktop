"""History/cost endpoints extracted from server.py."""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.gateway._helpers import iter_jsonl_objects
from ouroboros.utils import iter_llm_usage_events, llm_usage_cost, utc_now_iso

log = logging.getLogger(__name__)

_PROGRESS_META_FIELDS = (
    "subagent_event",
    "subagent_task_id",
    "root_task_id",
    "parent_task_id",
    "delegation_role",
    "subagent_role",
    "worker_saturation_warning",
)


def make_cost_breakdown_endpoint(data_dir: pathlib.Path):
    async def api_cost_breakdown(_request: Request) -> JSONResponse:
        """Aggregate llm_usage events from events.jsonl into cost breakdowns."""
        events_path = data_dir / "logs" / "events.jsonl"
        by_model: Dict[str, Dict[str, Any]] = {}
        by_api_key: Dict[str, Dict[str, Any]] = {}
        by_model_category: Dict[str, Dict[str, Any]] = {}
        by_task_category: Dict[str, Dict[str, Any]] = {}
        total_cost = 0.0
        total_calls = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_cached_tokens = 0
        total_cache_write_tokens = 0
        prompt_cache_ttls: Dict[str, int] = {}

        def _acc(d, key):
            if key not in d:
                d[key] = {
                    "cost": 0.0,
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "prompt_cache_ttls": {},
                }
            return d[key]

        try:
            for evt in iter_llm_usage_events(events_path):
                cost = llm_usage_cost(evt)
                model = str(evt.get("model") or "unknown")
                api_key_type = str(evt.get("api_key_type") or evt.get("provider") or "openrouter")
                model_cat = str(evt.get("model_category") or "other")
                task_cat = str(evt.get("category") or "task")
                token_values: Dict[str, int] = {}
                for field in ("prompt_tokens", "completion_tokens", "cached_tokens", "cache_write_tokens"):
                    try:
                        token_values[field] = int(evt.get(field) or 0)
                    except (TypeError, ValueError):
                        log.debug("Ignoring malformed %s in llm_usage event", field)
                        token_values[field] = 0
                prompt_tokens = token_values["prompt_tokens"]
                completion_tokens = token_values["completion_tokens"]
                cached_tokens = token_values["cached_tokens"]
                cache_write_tokens = token_values["cache_write_tokens"]
                prompt_cache_ttl = str(evt.get("prompt_cache_ttl") or "").strip()

                total_cost += cost
                total_calls += 1
                total_prompt_tokens += prompt_tokens
                total_completion_tokens += completion_tokens
                total_cached_tokens += cached_tokens
                total_cache_write_tokens += cache_write_tokens
                if prompt_cache_ttl:
                    prompt_cache_ttls[prompt_cache_ttl] = int(prompt_cache_ttls.get(prompt_cache_ttl, 0)) + 1

                for bucket, key in (
                    (by_model, model),
                    (by_api_key, api_key_type),
                    (by_model_category, model_cat),
                    (by_task_category, task_cat),
                ):
                    acc = _acc(bucket, key)
                    acc["cost"] += cost
                    acc["calls"] += 1
                    acc["prompt_tokens"] += prompt_tokens
                    acc["completion_tokens"] += completion_tokens
                    acc["cached_tokens"] += cached_tokens
                    acc["cache_write_tokens"] += cache_write_tokens
                    if prompt_cache_ttl:
                        ttl_counts = acc["prompt_cache_ttls"]
                        ttl_counts[prompt_cache_ttl] = int(ttl_counts.get(prompt_cache_ttl, 0)) + 1
        except Exception:
            pass

        def _sorted(d):
            return dict(sorted(d.items(), key=lambda x: x[1]["cost"], reverse=True))

        return JSONResponse({
            "total_cost": round(total_cost, 4),
            "total_calls": total_calls,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_cached_tokens": total_cached_tokens,
            "total_cache_write_tokens": total_cache_write_tokens,
            "prompt_cache_ttls": prompt_cache_ttls,
            "by_model": _sorted(by_model),
            "by_api_key": _sorted(by_api_key),
            "by_model_category": _sorted(by_model_category),
            "by_task_category": _sorted(by_task_category),
        })

    return api_cost_breakdown


def make_chat_history_endpoint(data_dir: pathlib.Path):
    async def api_chat_history(request: Request) -> JSONResponse:
        """Return recent chat, system, and progress messages merged chronologically."""
        try:
            limit = max(0, min(int(request.query_params.get("limit", 1000)), 2000))
        except (ValueError, TypeError):
            limit = 1000

        combined: list = []

        chat_path = data_dir / "logs" / "chat.jsonl"
        try:
            for entry in iter_jsonl_objects(chat_path):
                # Skip A2A virtual chat_ids so A2A task traffic does not appear in human chat history.
                if is_a2a_chat_id(entry.get("chat_id", 1)):
                    continue
                direction = str(entry.get("direction", "")).lower()
                role = {"in": "user", "out": "assistant", "system": "system"}.get(direction)
                if role is None:
                    continue
                rec = {
                    "text": str(entry.get("text", "")),
                    "role": role,
                    "ts": str(entry.get("ts", "")),
                    "is_progress": False,
                    "system_type": str(entry.get("type", "")),
                    "markdown": str(entry.get("format", "")).lower() == "markdown",
                    "source": str(entry.get("source", "")),
                    "sender_label": str(entry.get("sender_label", "")),
                    "sender_session_id": str(entry.get("sender_session_id", "")),
                    "client_message_id": str(entry.get("client_message_id", "")),
                    "task_id": str(entry.get("task_id", "")),
                    "telegram_chat_id": int(entry.get("telegram_chat_id") or 0),
                }
                # Pass task metadata for task_summary entries so the frontend can decide whether to show a live card.
                if entry.get("type") == "task_summary":
                    if "tool_calls" in entry:
                        rec["tool_calls"] = int(entry["tool_calls"])
                    if "rounds" in entry:
                        rec["rounds"] = int(entry["rounds"])
                combined.append(rec)
        except Exception as exc:
            log.warning("Failed to read chat history: %s", exc)

        progress_path = data_dir / "logs" / "progress.jsonl"
        try:
            for entry in iter_jsonl_objects(progress_path):
                # Skip A2A virtual chat_ids.
                if is_a2a_chat_id(entry.get("chat_id", 1)):
                    continue
                text = str(entry.get("content", entry.get("text", "")))
                if not text:
                    continue
                rec = {
                    "text": text,
                    "role": "assistant",
                    "ts": str(entry.get("ts", "")),
                    "is_progress": True,
                    "markdown": str(entry.get("format", "")).lower() == "markdown",
                    "task_id": str(entry.get("task_id", "")),
                }
                if isinstance(entry.get("lifecycle"), dict):
                    rec["lifecycle"] = dict(entry.get("lifecycle") or {})
                for field in _PROGRESS_META_FIELDS:
                    if field in entry:
                        rec[field] = entry[field]
                combined.append(rec)
        except Exception as exc:
            log.warning("Failed to read progress log: %s", exc)

        try:
            from ouroboros.skill_lifecycle_queue import queue_snapshot

            active = queue_snapshot().get("active")
            if isinstance(active, dict) and active.get("status") == "running":
                label = "stale" if active.get("stale") else "running"
                detail = active.get("error") or active.get("message") or active.get("status") or ""
                text = (
                    f"Skill {active.get('kind') or 'operation'}: `{active.get('target') or 'skill'}`"
                    f" — {label}{f' — {detail}' if detail else ''}"
                )
                lifecycle = dict(active)
                lifecycle["phase"] = label
                combined.append({
                    "text": text,
                    "role": "assistant",
                    "ts": utc_now_iso(),
                    "is_progress": True,
                    "markdown": False,
                    "task_id": str(active.get("chat_task_id") or ""),
                    "lifecycle": lifecycle,
                    "lifecycle_virtual": True,
                })
        except Exception as exc:
            log.debug("Failed to synthesize active lifecycle history: %s", exc)

        combined.sort(key=lambda m: m.get("ts", ""))
        messages = combined[-limit:] if len(combined) > limit else combined
        return JSONResponse({"messages": messages})

    return api_chat_history

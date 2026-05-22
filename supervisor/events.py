"""Dispatch worker EVENT_Q messages to supervisor handlers."""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict, Optional

from ouroboros.utils import utc_now_iso
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_SCHEDULED,
    write_task_result,
)

log = logging.getLogger(__name__)


_PARENT_CONTEXT_MARKER = "[BEGIN_PARENT_CONTEXT"
_PARENT_CONTEXT_END = "[END_PARENT_CONTEXT]"


def _extract_task_description_and_context(task: Dict[str, Any]) -> tuple[str, str]:
    description = str(task.get("description") or "").strip()
    context = str(task.get("context") or "").strip()
    if description or context:
        return description, context

    text = str(task.get("text") or task.get("description") or "").strip()
    if not text:
        return "", ""
    if _PARENT_CONTEXT_MARKER not in text or _PARENT_CONTEXT_END not in text:
        return text, ""

    before_marker, after_marker = text.split(_PARENT_CONTEXT_MARKER, 1)
    description = before_marker.split("\n\n---\n", 1)[0].strip()
    if "]\n" in after_marker:
        after_marker = after_marker.split("]\n", 1)[1]
    context = after_marker.rsplit(_PARENT_CONTEXT_END, 1)[0].strip()
    return description, context


def _format_task_for_dedup(task_id: str, description: str, context: str) -> str:
    return (
        f"Task ID: {task_id}\n"
        f"Description:\n{description or '(empty)'}\n\n"
        f"Context:\n{context or '(none)'}"
    )


def _handle_llm_usage(evt: Dict[str, Any], ctx: Any) -> None:
    usage_raw = evt.get("usage")
    usage: Dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}

    # Normalize usage across loop.py, web_search, and claude_code_edit producers.
    prompt_tokens = int(
        usage.get("prompt_tokens")
        or usage.get("input_tokens")
        or evt.get("prompt_tokens")
        or 0
    )
    completion_tokens = int(
        usage.get("completion_tokens")
        or usage.get("output_tokens")
        or evt.get("completion_tokens")
        or 0
    )
    cached_tokens = int(
        usage.get("cached_tokens")
        or evt.get("cached_tokens")
        or 0
    )
    cache_write_tokens = int(
        usage.get("cache_write_tokens")
        or evt.get("cache_write_tokens")
        or 0
    )
    prompt_cache_ttl = str(
        usage.get("prompt_cache_ttl")
        or evt.get("prompt_cache_ttl")
        or ""
    )

    raw_cost = usage.get("cost")
    if raw_cost is None:
        raw_cost = evt.get("cost")
    try:
        resolved_cost = float(raw_cost or 0.0)
    except (TypeError, ValueError):
        resolved_cost = 0.0

    usage_for_budget = {
        **usage,
        "cost": resolved_cost,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "prompt_cache_ttl": prompt_cache_ttl,
    }
    ctx.update_budget_from_usage(usage_for_budget)

    from ouroboros.utils import utc_now_iso, append_jsonl
    try:
        append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "llm_usage",
            "task_id": evt.get("task_id", ""),
            "category": evt.get("category", "other"),
            "model": evt.get("model", ""),
            "api_key_type": evt.get("api_key_type", ""),
            "model_category": evt.get("model_category", "other"),
            "provider": evt.get("provider", ""),
            "source": evt.get("source", ""),
            "cost_estimated": bool(evt.get("cost_estimated", False)),
            "cost": resolved_cost,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "prompt_cache_ttl": prompt_cache_ttl,
        })
    except Exception:
        log.warning("Failed to log llm_usage event to events.jsonl", exc_info=True)
        pass


def _handle_task_heartbeat(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "")
    if task_id and task_id in ctx.RUNNING:
        meta = ctx.RUNNING.get(task_id) or {}
        meta["last_heartbeat_at"] = time.time()
        phase = str(evt.get("phase") or "")
        if phase:
            meta["heartbeat_phase"] = phase
        ctx.RUNNING[task_id] = meta
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        started_at = float(meta.get("started_at") or 0.0)
        runtime_sec = round(max(0.0, time.time() - started_at), 1) if started_at > 0 else None
        try:
            ctx.bridge.push_log({
                "ts": evt.get("ts", utc_now_iso()),
                "type": "task_heartbeat",
                "task_id": task_id,
                "task_type": task.get("type"),
                "phase": phase or meta.get("heartbeat_phase") or "running",
                "runtime_sec": runtime_sec,
            })
        except Exception:
            log.debug("Failed to forward task heartbeat to live logs", exc_info=True)


def _handle_typing_start(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        chat_id = int(evt.get("chat_id") or 0)
        if chat_id:
            ctx.bridge.send_chat_action(chat_id, "typing")
    except Exception:
        log.debug("Failed to send typing action to chat", exc_info=True)
        pass


def _handle_send_message(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        log_text = evt.get("log_text")
        fmt = str(evt.get("format") or "")
        is_progress = bool(evt.get("is_progress"))
        raw_ts = evt.get("ts")
        ctx.send_with_budget(
            int(evt["chat_id"]),
            str(evt.get("text") or ""),
            log_text=(str(log_text) if isinstance(log_text, str) else None),
            fmt=fmt,
            is_progress=is_progress,
            task_id=str(evt.get("task_id") or ""),
            ts=(str(raw_ts) if raw_ts else None),
        )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "send_message_event_error", "error": repr(e),
            },
        )


def _handle_task_done(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = evt.get("task_id")
    task_type = str(evt.get("task_type") or "")
    wid = evt.get("worker_id")
    meta = ctx.RUNNING.get(str(task_id or ""), {}) if task_id else {}
    task = meta.get("task") if isinstance(meta, dict) and isinstance(meta.get("task"), dict) else {}

    # Persist here so send_message reaches the UI before task_done collapses the card.
    from ouroboros.utils import utc_now_iso, append_jsonl
    try:
        append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "task_done",
            "task_id": task_id,
            "task_type": task_type,
            "cost_usd": float(evt.get("cost_usd") or 0),
            "total_rounds": int(evt.get("total_rounds") or 0),
            "prompt_tokens": int(evt.get("prompt_tokens") or 0),
            "completion_tokens": int(evt.get("completion_tokens") or 0),
        })
    except Exception:
        log.warning("Failed to log task_done to events.jsonl", exc_info=True)

    if task_type == "evolution":
        st = ctx.load_state()
        # Meaningful evolution work has non-trivial cost plus at least one round.
        cost = float(evt.get("cost_usd") or 0)
        rounds = int(evt.get("total_rounds") or 0)

        evo_cost_threshold = float(os.environ.get("OUROBOROS_EVO_COST_THRESHOLD", "0.10"))
        if cost > evo_cost_threshold and rounds >= 1:
            st["evolution_consecutive_failures"] = 0
            ctx.save_state(st)
        else:
            failures = int(st.get("evolution_consecutive_failures") or 0) + 1
            st["evolution_consecutive_failures"] = failures
            ctx.save_state(st)
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "evolution_task_failure_tracked",
                    "task_id": task_id,
                    "consecutive_failures": failures,
                    "cost_usd": cost,
                    "rounds": rounds,
                },
            )

    if task_id:
        try:
            from ouroboros.headless import copy_child_task_result, finalize_task_artifacts

            if task:
                copy_child_task_result(ctx.DRIVE_ROOT, task)
                finalize_task_artifacts(ctx.DRIVE_ROOT, task)
        except Exception:
            log.warning("Failed to finalize headless artifacts for task %s", task_id, exc_info=True)
        ctx.RUNNING.pop(str(task_id), None)
    if wid in ctx.WORKERS and ctx.WORKERS[wid].busy_task_id == task_id:
        ctx.WORKERS[wid].busy_task_id = None
    ctx.persist_queue_snapshot(reason="task_done")
    try:
        ctx.bridge.push_log({
            "ts": evt.get("ts", utc_now_iso()),
            "type": "task_done",
            "task_id": task_id,
            "task_type": task_type,
            "cost_usd": evt.get("cost_usd"),
            "total_rounds": evt.get("total_rounds"),
            "prompt_tokens": evt.get("prompt_tokens"),
            "completion_tokens": evt.get("completion_tokens"),
        })
    except Exception:
        log.debug("Failed to forward task_done to live logs", exc_info=True)

    try:
        from pathlib import Path
        results_dir = Path(ctx.DRIVE_ROOT) / "task_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        result_file = results_dir / f"{task_id}.json"
        if not result_file.exists():
            write_task_result(
                ctx.DRIVE_ROOT,
                str(task_id or ""),
                STATUS_COMPLETED,
                result="",
                cost_usd=float(evt.get("cost_usd", 0)),
                ts=evt.get("ts", ""),
            )
    except Exception as e:
        log.warning("Failed to store task result in events: %s", e)


def _handle_task_metrics(evt: Dict[str, Any], ctx: Any) -> None:
    payload = {
        "ts": str(evt.get("ts") or utc_now_iso()),
        "type": "task_metrics_event",
        "task_id": str(evt.get("task_id") or ""),
        "task_type": str(evt.get("task_type") or ""),
        "duration_sec": round(float(evt.get("duration_sec") or 0.0), 3),
        "tool_calls": int(evt.get("tool_calls") or 0),
        "tool_errors": int(evt.get("tool_errors") or 0),
    }
    ctx.append_jsonl(ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl", payload)
    try:
        ctx.bridge.push_log(payload)
    except Exception:
        log.debug("Failed to forward task_metrics to live logs", exc_info=True)


def _handle_deep_self_review_request(evt: Dict[str, Any], ctx: Any) -> None:
    ctx.queue_deep_self_review_task(
        reason=str(evt.get("reason") or "agent_self_review"),
        model=str(evt.get("model") or ""),
    )


def _handle_promote_to_stable(evt: Dict[str, Any], ctx: Any) -> None:
    import subprocess as sp
    # Local branch promotion always works without a remote.
    try:
        sp.run(
            ["git", "branch", "-f", ctx.BRANCH_STABLE, ctx.BRANCH_DEV],
            cwd=str(ctx.REPO_DIR), check=True,
        )
        new_sha = sp.run(
            ["git", "rev-parse", ctx.BRANCH_STABLE],
            cwd=str(ctx.REPO_DIR), capture_output=True, text=True, check=True,
        ).stdout.strip()
    except Exception as e:
        st = ctx.load_state()
        if st.get("owner_chat_id"):
            ctx.send_with_budget(int(st["owner_chat_id"]), f"❌ Failed to promote to stable: {e}")
        return

    # Optional remote push; local promotion remains authoritative.
    remote_status = ""
    try:
        sp.run(["git", "remote", "get-url", "origin"], cwd=str(ctx.REPO_DIR),
               capture_output=True, check=True)
        sp.run(
            ["git", "push", "origin", f"{ctx.BRANCH_DEV}:{ctx.BRANCH_STABLE}"],
            cwd=str(ctx.REPO_DIR), check=True,
        )
        remote_status = " (pushed to origin)"
    except Exception:
        log.debug("No remote or push failed — local-only promote")

    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            f"✅ Promoted: {ctx.BRANCH_DEV} → {ctx.BRANCH_STABLE} ({new_sha[:8]}){remote_status}",
        )


def _find_duplicate_task(desc: str, task_context: str, pending: list, running: dict) -> Optional[str]:
    """Use a light LLM to reject only true duplicate active tasks."""
    existing = []
    for task in pending:
        description, context = _extract_task_description_and_context(task)
        if description.strip():
            existing.append({
                "id": str(task.get("id", "?")),
                "description": description,
                "context": context,
            })
    for task_id, meta in running.items():
        task_data = meta.get("task") if isinstance(meta, dict) else None
        if not isinstance(task_data, dict):
            continue
        description, context = _extract_task_description_and_context(task_data)
        if description.strip():
            existing.append({
                "id": str(task_id),
                "description": description,
                "context": context,
            })

    if not existing:
        return None

    existing_lines = "\n\n".join(
        _format_task_for_dedup(e["id"], e["description"], e["context"])
        for e in existing
    )
    prompt = (
        "Determine whether the NEW task is a true duplicate of any EXISTING active task.\n"
        "Only return a task ID if the requested work is materially the same.\n"
        "Tasks that share a broad goal but differ in target model, creative focus, "
        "scope, parent context, or intended output are NOT duplicates.\n\n"
        "NEW TASK\n"
        f"{_format_task_for_dedup('NEW', desc, task_context)}\n\n"
        f"EXISTING ACTIVE TASKS\n{existing_lines}\n\n"
        "Reply ONLY with the task ID if duplicate, or NONE if not."
    )

    try:
        from ouroboros.config import get_light_model
        from ouroboros.llm import LLMClient
        light_model = get_light_model()
        client = LLMClient()
        resp_msg, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=light_model,
            reasoning_effort="low",
            max_tokens=50,
        )
        # Supervisor runs outside task context; update budget directly.
        if usage:
            try:
                from supervisor.state import update_budget_from_usage
                update_budget_from_usage(usage)
            except Exception:
                pass
        answer = (resp_msg.get("content") or "NONE").strip()
        if answer.upper() == "NONE" or not answer:
            return None
        answer_lower = answer.lower()
        for e in existing:
            if e["id"].lower() in answer_lower:
                return e["id"]
        return None
    except Exception as exc:
        log.warning("LLM dedup unavailable, accepting task: %s", exc)
        return None


def _handle_schedule_task(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    tid = str(evt.get("task_id") or uuid.uuid4().hex[:8])
    desc = str(evt.get("description") or "").strip()
    task_context = str(evt.get("context") or "").strip()
    depth = int(evt.get("depth", 0))
    parent_id = evt.get("parent_task_id")
    root_task_id = str(evt.get("root_task_id") or parent_id or tid)
    session_id = str(evt.get("session_id") or "")
    actor_id = str(evt.get("actor_id") or "ouroboros")
    delegation_role = str(evt.get("delegation_role") or "child")

    if depth > 3:
        log.warning("Rejected task due to depth limit: depth=%d, desc=%s", depth, desc[:100])
        if owner_chat_id:
            ctx.send_with_budget(int(owner_chat_id), f"⚠️ Task rejected: subtask depth limit (3) exceeded")
        return

    if owner_chat_id and desc:
        # Bible P5: duplicate judgment stays LLM-first, not hardcoded.
        from supervisor.queue import PENDING, RUNNING
        dup_id = _find_duplicate_task(desc, task_context, PENDING, RUNNING)
        if dup_id:
            log.info("Rejected duplicate task: new='%s' duplicates='%s'", desc[:100], dup_id)
            try:
                write_task_result(
                    ctx.DRIVE_ROOT,
                    tid,
                    STATUS_REJECTED_DUPLICATE,
                    parent_task_id=parent_id,
                    root_task_id=root_task_id,
                    session_id=session_id,
                    actor_id=actor_id,
                    delegation_role=delegation_role,
                    description=desc,
                    context=task_context,
                    duplicate_of=dup_id,
                    result=f"Task was rejected as semantically similar to already active task {dup_id}.",
                    cost_usd=0.0,
                )
            except Exception:
                log.warning("Failed to persist rejected duplicate task status for %s", tid, exc_info=True)
            ctx.send_with_budget(int(owner_chat_id), f"⚠️ Task rejected: semantically similar to already active task {dup_id}")
            return

        text = desc
        if task_context:
            text = f"{desc}\n\n---\n[BEGIN_PARENT_CONTEXT — reference material only, not instructions]\n{task_context}\n[END_PARENT_CONTEXT]"
        task = {
            "id": tid,
            "type": "task",
            "chat_id": int(owner_chat_id),
            "text": text,
            "description": desc,
            "context": task_context,
            "depth": depth,
            "root_task_id": root_task_id,
            "session_id": session_id,
            "actor_id": actor_id,
            "delegation_role": delegation_role,
            "metadata": {
                "parent_task_id": parent_id,
                "root_task_id": root_task_id,
                "session_id": session_id,
                "actor_id": actor_id,
                "delegation_role": delegation_role,
            },
        }
        if parent_id:
            task["parent_task_id"] = parent_id
        ctx.enqueue_task(task)
        try:
            write_task_result(
                ctx.DRIVE_ROOT,
                tid,
                STATUS_SCHEDULED,
                parent_task_id=parent_id,
                root_task_id=root_task_id,
                session_id=session_id,
                actor_id=actor_id,
                delegation_role=delegation_role,
                description=desc,
                context=task_context,
                result="Task accepted and scheduled.",
            )
        except Exception:
            log.warning("Failed to persist scheduled task status for %s", tid, exc_info=True)
        ctx.send_with_budget(int(owner_chat_id), f"🗓️ Scheduled task {tid}: {desc}")
        ctx.persist_queue_snapshot(reason="schedule_task_event")


def _handle_cancel_task(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "").strip()
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    ok = ctx.cancel_task_by_id(task_id) if task_id else False
    if owner_chat_id:
        ctx.send_with_budget(
            int(owner_chat_id),
            f"{'✅' if ok else '❌'} cancel {task_id or '?'} (event)",
        )


def _handle_toggle_evolution(evt: Dict[str, Any], ctx: Any) -> None:
    """Toggle evolution mode from LLM tool call."""
    enabled = bool(evt.get("enabled"))
    st = ctx.load_state()
    st["evolution_mode_enabled"] = enabled
    ctx.save_state(st)
    if not enabled:
        ctx.PENDING[:] = [t for t in ctx.PENDING if str(t.get("type")) != "evolution"]
        ctx.sort_pending()
        ctx.persist_queue_snapshot(reason="evolve_off_via_tool")
    if st.get("owner_chat_id"):
        state_str = "ON" if enabled else "OFF"
        ctx.send_with_budget(int(st["owner_chat_id"]), f"🧬 Evolution: {state_str} (via agent tool)")


def _handle_toggle_consciousness(evt: Dict[str, Any], ctx: Any) -> None:
    """Toggle background consciousness from LLM tool call."""
    from supervisor.state import update_state
    action = str(evt.get("action") or "status")
    if action in ("start", "on"):
        result = ctx.consciousness.start()
        update_state(lambda st: st.__setitem__("bg_consciousness_enabled", True))
    elif action in ("stop", "off"):
        result = ctx.consciousness.stop()
        update_state(lambda st: st.__setitem__("bg_consciousness_enabled", False))
    else:
        status = "running" if ctx.consciousness.is_running else "stopped"
        result = f"Background consciousness: {status}"
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(int(st["owner_chat_id"]), f"🧠 {result}")


def _handle_send_photo(evt: Dict[str, Any], ctx: Any) -> None:
    """Send a photo to the owner's chat."""
    import base64 as b64mod
    try:
        chat_id = int(evt.get("chat_id") or 0)
        image_b64 = str(evt.get("image_base64") or "")
        caption = str(evt.get("caption") or "")
        mime = str(evt.get("mime") or "image/png")
        if not chat_id or not image_b64:
            return
        photo_bytes = b64mod.b64decode(image_b64)
        ok, err = ctx.bridge.send_photo(chat_id, photo_bytes, caption=caption, mime=mime)
        if not ok:
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "send_photo_error",
                    "chat_id": chat_id, "error": err,
                },
            )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "send_photo_event_error", "error": repr(e),
            },
        )


def _handle_owner_message_injected(evt: Dict[str, Any], ctx: Any) -> None:
    """Log owner injections so health checks can detect duplicate processing."""
    from ouroboros.utils import utc_now_iso
    try:
        ctx.append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "owner_message_injected",
            "task_id": evt.get("task_id", ""),
            "text": evt.get("text", ""),
        })
    except Exception:
        log.warning("Failed to log owner_message_injected event", exc_info=True)


def _handle_log_event(evt: Dict[str, Any], ctx: Any) -> None:
    """Forward live events; persist durable task checkpoints."""
    data = evt.get("data")
    if not isinstance(data, dict):
        return
    payload = {
        "ts": data.get("ts", utc_now_iso()),
        **data,
    }
    try:
        ctx.bridge.push_log(payload)
    except Exception:
        log.debug("Failed to forward live log event", exc_info=True)
    if data.get("type") == "task_checkpoint":
        try:
            ctx.append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", payload)
        except Exception:
            log.debug("Failed to persist %s event to events.jsonl", data.get("type"), exc_info=True)


def _handle_skill_lifecycle(evt: Dict[str, Any], ctx: Any) -> None:
    payload = dict(evt)
    payload.setdefault("ts", utc_now_iso())
    try:
        ctx.append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", payload)
    except Exception:
        log.debug("Failed to persist skill lifecycle event", exc_info=True)
    try:
        ctx.bridge.push_log(payload)
    except Exception:
        log.debug("Failed to forward skill lifecycle event to live logs", exc_info=True)
    try:
        from ouroboros.event_bus import SKILL_LIFECYCLE, publish_event

        publish_event(SKILL_LIFECYCLE, payload)
    except Exception:
        log.debug("Failed to publish skill lifecycle event", exc_info=True)

EVENT_HANDLERS = {
    "llm_usage": _handle_llm_usage,
    "task_heartbeat": _handle_task_heartbeat,
    "typing_start": _handle_typing_start,
    "send_message": _handle_send_message,
    "task_done": _handle_task_done,
    "task_metrics": _handle_task_metrics,
    "deep_self_review_request": _handle_deep_self_review_request,
    "promote_to_stable": _handle_promote_to_stable,
    "schedule_task": _handle_schedule_task,
    "cancel_task": _handle_cancel_task,
    "send_photo": _handle_send_photo,
    "toggle_evolution": _handle_toggle_evolution,
    "toggle_consciousness": _handle_toggle_consciousness,
    "owner_message_injected": _handle_owner_message_injected,
    "log_event": _handle_log_event,
    "skill_exec_finished": _handle_skill_lifecycle,
    "skill_exec_failed": _handle_skill_lifecycle,
}


def dispatch_event(evt: Dict[str, Any], ctx: Any) -> None:
    """Dispatch a single worker event to its handler."""
    if not isinstance(evt, dict):
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "invalid_worker_event",
                "error": "event is not dict",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    event_type = str(evt.get("type") or "").strip()
    if not event_type:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "invalid_worker_event",
                "error": "missing event.type",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "unknown_worker_event",
                "event_type": event_type,
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    try:
        handler(evt, ctx)
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "worker_event_handler_error",
                "event_type": event_type,
                "error": repr(e),
            },
        )

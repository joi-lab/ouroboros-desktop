"""Thin agent orchestrator around context, LLM loop, tools, memory, and review."""

from __future__ import annotations

import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

log = logging.getLogger(__name__)

from ouroboros.utils import (
    append_jsonl,
    emit_log_event,
    get_git_info,
    read_json_dict,
    safe_relpath,
    sanitize_task_for_event,
    truncate_for_log,
    utc_now_iso,
)
from ouroboros.llm import LLMClient
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext
from ouroboros.memory import Memory
from ouroboros.context import build_llm_messages
from ouroboros.loop import run_llm_loop
from ouroboros.config import resolve_effort
from ouroboros.agent_startup_checks import (
    inject_crash_report,
    verify_restart,
    verify_system_state,
)
from ouroboros.agent_task_pipeline import (
    build_trace_summary, emit_task_results, build_review_context,
)
from ouroboros.task_results import STATUS_RUNNING, write_task_result
from ouroboros.contracts.task_constraint import normalize_task_constraint


_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


class OuroborosAgent:
    """Per-worker agent instance; long-term state lives on Drive."""

    def __init__(self, env: Env, event_queue: Any = None):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self._current_chat_id: Optional[int] = None
        self._current_task_type: Optional[str] = None
        self._current_task_id: Optional[str] = None
        self._current_task_metadata: Dict[str, Any] = {}

        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)
        self.memory.ensure_files()

        self._log_worker_boot_once()

    def inject_message(
        self,
        text: str,
        image_data: Optional[Union[Tuple[str, str], Tuple[str, str, str]]] = None,
    ) -> None:
        """Thread-safe: inject a user message into the active conversation."""
        if image_data:
            payload: Dict[str, Any] = {
                "text": text,
                "image_base64": image_data[0],
                "image_mime": image_data[1],
            }
            if len(image_data) > 2 and image_data[2]:
                payload["image_caption"] = image_data[2]
            self._incoming_messages.put(payload)
            return
        self._incoming_messages.put(text)

    def _emit_live_log(self, event_type: str, **fields: Any) -> None:
        """Send a session-only live log event to supervisor/UI."""
        emit_log_event(
            self._event_queue,
            {"type": event_type, "ts": utc_now_iso(), **fields},
            blocking=True,
            log_label="agent live",
        )

    def _log_worker_boot_once(self) -> None:
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
            git_branch, git_sha = get_git_info(self.env.repo_dir)
            append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                'ts': utc_now_iso(), 'type': 'worker_boot',
                'pid': os.getpid(), 'git_branch': git_branch, 'git_sha': git_sha,
            })
            verify_restart(self.env, git_sha)
            verify_system_state(self.env, git_sha)
            inject_crash_report(self.env)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _prepare_task_context(self, task: Dict[str, Any]) -> Tuple[ToolContext, List[Dict[str, Any]], Dict[str, Any]]:
        """Set up ToolContext, build messages, return (ctx, messages, cap_info)."""
        drive_logs = self.env.drive_path("logs")
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})
        try:
            write_task_result(
                self.env.drive_root,
                str(task.get("id") or ""),
                STATUS_RUNNING,
                parent_task_id=task.get("parent_task_id"),
                root_task_id=task.get("root_task_id"),
                session_id=task.get("session_id"),
                actor_id=task.get("actor_id"),
                delegation_role=task.get("delegation_role"),
                role=task.get("role"),
                description=task.get("description"),
                objective=task.get("objective") or task.get("description"),
                expected_output=task.get("expected_output"),
                constraints=task.get("constraints"),
                context=task.get("context"),
                memory_mode=task.get("memory_mode"),
                drive_root=task.get("drive_root"),
                child_drive_root=task.get("child_drive_root") or task.get("drive_root"),
                budget_drive_root=task.get("budget_drive_root"),
                task_constraint=task.get("task_constraint"),
                result="Task is running.",
            )
        except Exception:
            log.debug("Failed to persist running task status", exc_info=True)
        self._emit_live_log(
            "context_building_started",
            task_id=str(task.get("id") or ""),
            task_type=str(task.get("type") or ""),
        )
        if str(task.get("delegation_role") or "") == "subagent" and self._event_queue is not None and self._current_chat_id is not None:
            try:
                self._event_queue.put({
                    "type": "send_message",
                    "chat_id": self._current_chat_id,
                    "text": f"▶️ Subagent {task.get('id')} running ({task.get('role') or 'researcher'}).",
                    "format": "markdown",
                    "is_progress": True,
                    "task_id": str(task.get("id") or ""),
                    "progress_meta": {
                        "subagent_event": "running",
                        "subagent_task_id": str(task.get("id") or ""),
                        "root_task_id": str(task.get("root_task_id") or ""),
                        "parent_task_id": str(task.get("parent_task_id") or ""),
                        "delegation_role": "subagent",
                        "subagent_role": str(task.get("role") or ""),
                    },
                    "ts": utc_now_iso(),
                })
            except Exception:
                log.debug("Failed to emit subagent running progress", exc_info=True)

        task_metadata = dict(task.get("metadata") or {}) if isinstance(task.get("metadata"), dict) else {}
        for key in (
            "parent_task_id",
            "root_task_id",
            "session_id",
            "actor_id",
            "delegation_role",
            "role",
            "workspace_root",
            "workspace_mode",
            "memory_mode",
            "drive_root",
            "child_drive_root",
            "budget_drive_root",
        ):
            if task.get(key) not in (None, ""):
                task_metadata[key] = task.get(key)
        self._current_task_metadata = dict(task_metadata)

        ctx = ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            branch_dev=self.env.branch_dev,
            system_repo_dir=self.env.repo_dir,
            workspace_root=pathlib.Path(task["workspace_root"]).resolve(strict=False)
            if str(task.get("workspace_root") or "").strip()
            else None,
            workspace_mode=str(task.get("workspace_mode") or ""),
            memory_mode=str(task.get("memory_mode") or ""),
            task_metadata=task_metadata,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            emit_progress_fn=self._emit_progress,
            event_queue=self._event_queue,
            task_id=str(task.get("id") or ""),
            task_depth=int(task.get("depth", 0)),
            is_direct_chat=bool(task.get("_is_direct_chat")),
            task_constraint=normalize_task_constraint(task.get("task_constraint")),
        )
        self.tools.set_context(ctx)

        self._emit_typing_start()

        _use_local = os.environ.get("USE_LOCAL_MAIN", "").lower() in ("true", "1")
        _soft_cap = 200_000
        if _use_local:
            _local_ctx = int(os.environ.get("LOCAL_MODEL_CONTEXT_LENGTH", "0"))
            if _local_ctx <= 0:
                try:
                    from ouroboros.local_model import get_manager
                    _local_ctx = get_manager().get_context_length()
                except Exception:
                    _local_ctx = 0
            if _local_ctx <= 0:
                _local_ctx = 16384
            _soft_cap = max(2048, _local_ctx // 2)

        messages, cap_info = build_llm_messages(
            env=self.env,
            memory=self.memory,
            task=task,
            review_context_builder=lambda: build_review_context(self.env),
            soft_cap_tokens=_soft_cap,
        )

        budget_remaining = None
        try:
            budget_root_text = str(task.get("budget_drive_root") or "").strip()
            budget_root = pathlib.Path(budget_root_text) if budget_root_text else self.env.drive_root
            state_data = read_json_dict(budget_root / "state" / "state.json") or {}
            total_budget = float(os.environ.get("TOTAL_BUDGET", "1"))
            spent = float(state_data.get("spent_usd", 0))
            if total_budget > 0:
                budget_remaining = max(0, total_budget - spent)
        except Exception:
            pass

        cap_info["budget_remaining"] = budget_remaining
        self._emit_live_log(
            "context_building_finished",
            task_id=str(task.get("id") or ""),
            task_type=str(task.get("type") or ""),
            message_count=len(messages),
            budget_remaining_usd=budget_remaining,
        )
        return ctx, messages, cap_info

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Hot-reload settings so UI changes affect the next task without restart.
        try:
            from ouroboros.config import load_settings, apply_settings_to_env
            apply_settings_to_env(load_settings())
        except Exception:
            pass

        self._busy = True
        start_time = time.time()
        self._task_started_ts = start_time
        self._last_progress_ts = start_time
        self._pending_events = []
        # Preserve chat_id=0; it is a real session, not missing.
        _raw_chat = task.get("chat_id")
        if _raw_chat is None or _raw_chat == "":
            self._current_chat_id = None
        else:
            try:
                self._current_chat_id = int(_raw_chat)
            except (TypeError, ValueError):
                self._current_chat_id = None
        self._current_task_type = str(task.get("type") or "")
        self._current_task_id = str(task.get("id") or "") or None
        self._emit_live_log(
            "task_started",
            task_id=self._current_task_id or "",
            task_type=self._current_task_type,
            task_text=str(task.get("text") or "")[:200],
            direct_chat=bool(task.get("_is_direct_chat")),
        )

        drive_logs = self.env.drive_path("logs")
        heartbeat_stop = self._start_task_heartbeat_loop(str(task.get("id") or ""))

        try:
            ctx, messages, cap_info = self._prepare_task_context(task)
            budget_remaining = cap_info.get("budget_remaining")

            usage: Dict[str, Any] = {}
            llm_trace: Dict[str, Any] = {"reasoning_notes": [], "tool_calls": []}

            task_type_str = str(task.get("type") or "").lower()
            initial_effort = resolve_effort(task_type_str)

            if task_type_str == "deep_self_review":
                # Deep self-review bypasses the tool loop.
                try:
                    from ouroboros.deep_self_review import run_deep_self_review, is_review_available
                    self._emit_progress("Starting deep self-review... This may take several minutes.")
                    review_model = str(task.get("model") or "")
                    if not review_model:
                        avail, review_model = is_review_available()
                        if not avail:
                            review_model = ""
                    if not review_model:
                        text = "❌ Deep self-review unavailable: no OPENROUTER_API_KEY or OPENAI_API_KEY configured."
                        usage = {
                            "result_status": "infra_failed",
                            "reason_code": "deep_self_review_unavailable",
                        }
                    else:
                        text, usage = run_deep_self_review(
                            repo_dir=self.env.repo_dir,
                            drive_root=self.env.drive_root,
                            llm=self.llm,
                            emit_progress=self._emit_progress,
                            event_queue=self._event_queue,
                            model=review_model,
                        )
                    if usage:
                        self._pending_events.append({
                            "type": "llm_usage",
                            "ts": utc_now_iso(),
                            "task_id": str(task.get("id") or ""),
                            "model": review_model,
                            "usage": usage,
                            "category": "deep_self_review",
                        })
                    try:
                        review_path = pathlib.Path(self.env.drive_root) / "memory" / "deep_review.md"
                        review_path.write_text(text, encoding="utf-8")
                    except Exception as save_err:
                        log.warning("Failed to save deep review to memory: %s", save_err)
                    llm_trace = {"reasoning_notes": ["deep_self_review"], "tool_calls": []}
                except Exception as e:
                    tb = traceback.format_exc()
                    append_jsonl(drive_logs / "events.jsonl", {
                        "ts": utc_now_iso(), "type": "task_error",
                        "task_id": task.get("id"), "error": repr(e),
                        "traceback": truncate_for_log(tb, 2000),
                    })
                    text = f"⚠️ Deep self-review error: {type(e).__name__}: {e}"
                    usage = {
                        "result_status": "infra_failed",
                        "reason_code": "deep_self_review_error",
                    }
                    llm_trace = {"reasoning_notes": ["deep_self_review_error"], "tool_calls": []}
            else:
                try:
                    text, usage, llm_trace = run_llm_loop(
                        messages=messages,
                        tools=self.tools,
                        llm=self.llm,
                        drive_logs=drive_logs,
                        emit_progress=self._emit_progress,
                        incoming_messages=self._incoming_messages,
                        task_type=task_type_str,
                        task_id=str(task.get("id") or ""),
                        budget_remaining_usd=budget_remaining,
                        event_queue=self._event_queue,
                        initial_effort=initial_effort,
                        drive_root=self.env.drive_root,
                    )
                except Exception as e:
                    tb = traceback.format_exc()
                    append_jsonl(drive_logs / "events.jsonl", {
                        "ts": utc_now_iso(), "type": "task_error",
                        "task_id": task.get("id"), "error": repr(e),
                        "traceback": truncate_for_log(tb, 2000),
                    })
                    text = f"⚠️ Error during processing: {type(e).__name__}: {e}"
                    usage = {
                        "result_status": "infra_failed",
                        "reason_code": "task_exception",
                    }
                    try:
                        from ouroboros.task_results import STATUS_FAILED, write_task_result
                        write_task_result(
                            self.env.drive_root,
                            str(task.get("id") or ""),
                            STATUS_FAILED,
                            result=text,
                            result_status="infra_failed",
                            reason_code="task_exception",
                        )
                    except Exception:
                        pass
                    try:
                        from ouroboros.task_continuation import capture_review_continuation_from_state
                        capture_review_continuation_from_state(
                            self.env.drive_root,
                            task,
                            source="task_exception",
                            warning=f"{type(e).__name__}: {e}",
                            repo_dir=self.env.repo_dir,
                        )
                    except Exception:
                        log.debug("Failed to persist review continuation after task exception", exc_info=True)

            if not isinstance(text, str) or not text.strip():
                text = "⚠️ Model returned an empty response. Try rephrasing your request."

            emit_task_results(
                self.env, self.memory, self.llm,
                self._pending_events, task, text,
                usage, llm_trace, start_time, drive_logs,
                ctx=ctx,
            )
            return list(self._pending_events)

        finally:
            self._busy = False
            try:
                from ouroboros.tools.browser import cleanup_browser
                cleanup_browser(self.tools._ctx)
            except Exception:
                log.debug("Failed to cleanup browser", exc_info=True)
                pass
            while not self._incoming_messages.empty():
                try:
                    self._incoming_messages.get_nowait()
                except queue.Empty:
                    break
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._current_task_type = None
            self._current_task_id = None
            self._current_task_metadata = {}

    def _emit_progress(self, text: str) -> None:
        self._last_progress_ts = time.time()
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            event = {
                "type": "send_message", "chat_id": self._current_chat_id,
                "text": f"💬 {text}", "format": "markdown", "is_progress": True,
                "task_id": self._current_task_id or "",
                "ts": utc_now_iso(),
            }
            progress_meta = self._subagent_progress_meta("progress")
            if progress_meta:
                event["progress_meta"] = progress_meta
            self._event_queue.put(event)
        except Exception:
            log.warning("Failed to emit progress event", exc_info=True)
            pass

    def _emit_typing_start(self) -> None:
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "typing_start", "chat_id": self._current_chat_id,
                "ts": utc_now_iso(),
            })
        except Exception:
            log.warning("Failed to emit typing start event", exc_info=True)
            pass

    def _emit_task_heartbeat(self, task_id: str, phase: str) -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put({
                "type": "task_heartbeat", "task_id": task_id,
                "phase": phase, "ts": utc_now_iso(),
                **self._subagent_progress_meta(phase),
            })
        except Exception:
            log.warning("Failed to emit task heartbeat event", exc_info=True)
            pass

    def _subagent_progress_meta(self, event: str) -> Dict[str, Any]:
        metadata = self._current_task_metadata if isinstance(self._current_task_metadata, dict) else {}
        if str(metadata.get("delegation_role") or "").lower() != "subagent":
            return {}
        task_id = str(self._current_task_id or metadata.get("subagent_task_id") or metadata.get("task_id") or "")
        return {
            "subagent_event": str(event or "progress"),
            "subagent_task_id": task_id,
            "root_task_id": str(metadata.get("root_task_id") or ""),
            "parent_task_id": str(metadata.get("parent_task_id") or ""),
            "delegation_role": "subagent",
            "subagent_role": str(metadata.get("role") or ""),
        }

    def _start_task_heartbeat_loop(self, task_id: str) -> Optional[threading.Event]:
        if self._event_queue is None or not task_id.strip():
            return None
        interval = 30
        stop = threading.Event()
        self._emit_task_heartbeat(task_id, "start")

        def _loop() -> None:
            while not stop.wait(interval):
                self._emit_task_heartbeat(task_id, "running")

        threading.Thread(target=_loop, daemon=True).start()
        return stop


def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)

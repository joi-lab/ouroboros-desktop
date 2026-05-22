"""LLM-loop tool execution: dispatch, timeouts, truncation, live logs."""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

import logging

from ouroboros.config import load_settings
from ouroboros.tool_capabilities import (
    READ_ONLY_PARALLEL_TOOLS,
    REVIEWED_MUTATIVE_TOOLS,
    STATEFUL_BROWSER_TOOLS,
    TOOL_RESULT_LIMITS as _TOOL_RESULT_LIMITS,
    DEFAULT_TOOL_RESULT_LIMIT as _DEFAULT_TOOL_RESULT_LIMIT,
    UNTRUNCATED_TOOL_RESULTS as _UNTRUNCATED_TOOL_RESULTS,
    UNTRUNCATED_REPO_READ_PATHS as _UNTRUNCATED_REPO_READ_PATHS,
)
from ouroboros.tools.registry import ToolRegistry
from ouroboros.utils import (
    append_jsonl,
    emit_log_event,
    sanitize_tool_args_for_log,
    sanitize_tool_result_for_log,
    truncate_for_log,
    utc_now_iso,
)

log = logging.getLogger(__name__)

_FAILURE_PREFIXES = (
    "⚠️ TOOL_",
    "⚠️ SHELL_",
    "⚠️ CLAUDE_CODE_",
)
_EXIT_CODE_RE = re.compile(r"exit_code=(-?\d+)")
_SIGNAL_RE = re.compile(r"signal=([A-Z0-9_]+)")

# Reviewed mutative tools get a hard ceiling after their soft timeout.
_REVIEWED_MUTATIVE_HARD_CEILING = 1800


def _emit_live_log(tools: ToolRegistry, payload: Dict[str, Any]) -> None:
    """Emit a live log through the registry context queue."""
    event_queue = getattr(getattr(tools, "_ctx", None), "event_queue", None)
    emit_log_event(
        event_queue,
        {"ts": utc_now_iso(), **payload},
        log_label="tool live",
    )


def _get_tool_timeout(tools: ToolRegistry, tool_name: str) -> int:
    """Return max(settings/env timeout, per-tool minimum)."""
    settings_val = 0
    try:
        settings_val = int(load_settings().get("OUROBOROS_TOOL_TIMEOUT_SEC") or 0)
    except Exception:
        pass
    if settings_val <= 0:
        env_val = os.environ.get("OUROBOROS_TOOL_TIMEOUT_SEC")
        if env_val:
            try:
                parsed = int(env_val)
                if parsed > 0:
                    settings_val = parsed
            except ValueError:
                pass
    per_tool = tools.get_timeout(tool_name)
    return max(settings_val, per_tool) if settings_val > 0 else per_tool


def _path_is_cognitive_artifact(tool_name: str, tool_args: Optional[Dict[str, Any]]) -> bool:
    """Return whether a read target must stay whole."""
    if not tool_args:
        return False

    raw_path = str(tool_args.get("path") or "").strip()
    if not raw_path:
        return False

    normalized = raw_path.replace("\\", "/").lstrip("./")

    if tool_name == "data_read":
        return normalized.startswith("memory/") and "/_backup/" not in normalized

    if tool_name == "repo_read":
        return normalized.startswith("prompts/") or normalized in _UNTRUNCATED_REPO_READ_PATHS

    return False


def _should_skip_tool_result_truncation(
    tool_name: str,
    tool_args: Optional[Dict[str, Any]] = None,
) -> bool:
    """Canonical/cognitive reads must remain whole."""
    return tool_name in _UNTRUNCATED_TOOL_RESULTS or _path_is_cognitive_artifact(tool_name, tool_args)


def _truncate_tool_result(
    result: Any,
    tool_name: str = "",
    tool_args: Optional[Dict[str, Any]] = None,
) -> str:
    """Cap tool result unless it is an untruncated artifact."""
    limit = _TOOL_RESULT_LIMITS.get(tool_name, _DEFAULT_TOOL_RESULT_LIMIT)
    s = str(result)
    if _should_skip_tool_result_truncation(tool_name, tool_args):
        return s
    if len(s) <= limit:
        return s
    return s[:limit] + f"\n... (truncated from {len(s)} chars, limit={limit})"


def _is_tool_execution_failure(tool_ok: bool, result: Any) -> bool:
    """Treat only executor/runtime failures as UI tool failures."""
    if not tool_ok:
        return True
    text = str(result or "")
    if text.startswith("⚠️ SHELL_REGEX_AUTO_CORRECTED") and "⚠️ SHELL_EXIT_ERROR" not in text:
        return False
    return text.startswith(_FAILURE_PREFIXES)


def _extract_result_metadata(fn_name: str, result: Any, is_error: bool) -> Dict[str, Any]:
    """Extract structured outcome facts for summaries and reflections."""
    text = str(result or "")
    status = "error" if is_error else "ok"
    if text.startswith("⚠️ TOOL_TIMEOUT"):
        status = "timeout"
    elif text.startswith("⚠️ SHELL_REGEX_AUTO_CORRECTED") and "⚠️ SHELL_EXIT_ERROR" not in text:
        status = "ok_autocorrected"
    elif text.startswith("⚠️ SHELL_EXIT_ERROR"):
        status = "non_zero_exit"
    elif text.startswith("⚠️ SHELL_"):
        status = "shell_error"
    elif text.startswith("⚠️ CLAUDE_CODE_TIMEOUT"):
        status = "timeout"
    elif text.startswith("⚠️ CLAUDE_CODE_INSTALL_ERROR"):
        status = "install_error"
    elif text.startswith("⚠️ CLAUDE_CODE_UNAVAILABLE"):
        status = "unavailable"
    elif text.startswith("⚠️ CLAUDE_CODE_"):
        status = "claude_code_error"

    meta: Dict[str, Any] = {"status": status}
    exit_match = _EXIT_CODE_RE.search(text)
    if exit_match:
        try:
            meta["exit_code"] = int(exit_match.group(1))
        except ValueError:
            pass
    signal_match = _SIGNAL_RE.search(text)
    if signal_match:
        meta["signal"] = signal_match.group(1)
    if fn_name == "run_shell" and not is_error and meta.get("exit_code") == 0:
        if status == "ok_autocorrected":
            meta["status"] = "ok_autocorrected"
        else:
            meta["status"] = "ok"
    return meta


def _execute_single_tool(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    task_id: str = "",
) -> Dict[str, Any]:
    """
    Execute a single tool call and return all needed info.

    Returns dict with: tool_call_id, fn_name, result, is_error, args_for_log, is_code_tool
    """
    requested_fn_name = tc["function"]["name"]
    fn_name = str(requested_fn_name or "").strip()
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS

    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
    except (json.JSONDecodeError, ValueError) as e:
        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{requested_fn_name}': {e}"
        return {
            "tool_call_id": tool_call_id,
            "fn_name": fn_name,
            "result": result,
            "is_error": True,
            "tool_args": {},
            "args_for_log": {},
            "is_code_tool": is_code_tool,
            "result_meta": _extract_result_metadata(fn_name, result, True),
        }

    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

    tool_ok = True
    try:
        result = tools.execute(fn_name, args)
    except Exception as e:
        tool_ok = False
        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "tool_error", "task_id": task_id,
            "tool": fn_name, "args": args_for_log, "error": repr(e),
        })

    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "type": "tool_call", "tool": fn_name, "task_id": task_id,
        "args": args_for_log,
        "result_preview": sanitize_tool_result_for_log(truncate_for_log(result, 2000)),
    })

    is_error = _is_tool_execution_failure(tool_ok, result)

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": is_error,
        "tool_args": args if isinstance(args, dict) else {},
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
        "result_meta": _extract_result_metadata(fn_name, result, is_error),
    }


class StatefulToolExecutor:
    """Thread-sticky executor for Playwright/greenlet stateful tools."""
    def __init__(self):
        self._executor: Optional[ThreadPoolExecutor] = None

    def submit(self, fn, *args, **kwargs):
        """Submit work to the sticky thread."""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stateful_tool")
        return self._executor.submit(fn, *args, **kwargs)

    def reset(self):
        """Reset the sticky thread after timeout/error."""
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def shutdown(self, wait=True, cancel_futures=False):
        """Shutdown the sticky executor."""
        if self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=cancel_futures)
            self._executor = None


def _make_timeout_result(
    fn_name: str,
    tool_call_id: str,
    is_code_tool: bool,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    reset_msg: str = "",
) -> Dict[str, Any]:
    """Create and log a timeout result."""
    args_for_log = {}
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
        args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})
    except Exception:
        pass

    result = (
        f"⚠️ TOOL_TIMEOUT ({fn_name}): exceeded {timeout_sec}s limit. "
        f"The tool is still running in background but control is returned to you. "
        f"{reset_msg}Try a different approach or inform the user{' about the issue' if not reset_msg else ''}."
    )

    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(), "type": "tool_timeout",
        "tool": fn_name, "args": args_for_log,
        "timeout_sec": timeout_sec,
    })
    append_jsonl(drive_logs / "tools.jsonl", {
        "ts": utc_now_iso(), "type": "tool_call", "tool": fn_name,
        "args": args_for_log, "result_preview": result,
    })

    return {
        "tool_call_id": tool_call_id,
        "fn_name": fn_name,
        "result": result,
        "is_error": True,
        "args_for_log": args_for_log,
        "is_code_tool": is_code_tool,
        "result_meta": _extract_result_metadata(fn_name, result, True),
    }


def _execute_with_timeout(
    tools: ToolRegistry,
    tc: Dict[str, Any],
    drive_logs: pathlib.Path,
    timeout_sec: int,
    task_id: str = "",
    stateful_executor: Optional[StatefulToolExecutor] = None,
) -> Dict[str, Any]:
    """Execute one tool call with timeout handling."""
    requested_fn_name = tc["function"]["name"]
    fn_name = str(requested_fn_name or "").strip()
    tool_call_id = tc["id"]
    is_code_tool = fn_name in tools.CODE_TOOLS
    use_stateful = stateful_executor and fn_name in STATEFUL_BROWSER_TOOLS
    started_at = time.perf_counter()
    args_for_log = {}
    try:
        args = json.loads(tc["function"]["arguments"] or "{}")
        if isinstance(args, dict):
            args_for_log = sanitize_tool_args_for_log(fn_name, args)
    except Exception:
        pass
    _emit_live_log(tools, {
        "type": "tool_call_started",
        "task_id": task_id,
        "tool": fn_name,
        "timeout_sec": timeout_sec,
        "args": args_for_log,
    })

    if use_stateful:
        future = stateful_executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
        try:
            result = future.result(timeout=timeout_sec)
            result_meta = result.get("result_meta") or {}
            _emit_live_log(tools, {
                "type": "tool_call_finished",
                "task_id": task_id,
                "tool": fn_name,
                "args": result.get("args_for_log", args_for_log),
                "duration_sec": round(time.perf_counter() - started_at, 3),
                "is_error": bool(result.get("is_error")),
                "status": result_meta.get("status"),
                "exit_code": result_meta.get("exit_code"),
                "signal": result_meta.get("signal"),
                "result_preview": sanitize_tool_result_for_log(
                    truncate_for_log(result.get("result", ""), 500)
                ),
            })
            return result
        except (TimeoutError, concurrent.futures.TimeoutError):
            stateful_executor.reset()
            reset_msg = "Browser state has been reset. "
            timeout_result = _make_timeout_result(
                fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                timeout_sec, task_id, reset_msg
            )
            _emit_live_log(tools, {
                "type": "tool_call_timeout",
                "task_id": task_id,
                "tool": fn_name,
                "args": args_for_log,
                "duration_sec": round(time.perf_counter() - started_at, 3),
                "timeout_sec": timeout_sec,
            })
            return timeout_result
    else:
        executor = ThreadPoolExecutor(max_workers=1)
        try:
            future = executor.submit(_execute_single_tool, tools, tc, drive_logs, task_id)
            try:
                result = future.result(timeout=timeout_sec)
                result_meta = result.get("result_meta") or {}
                _emit_live_log(tools, {
                    "type": "tool_call_finished",
                    "task_id": task_id,
                    "tool": fn_name,
                    "args": result.get("args_for_log", args_for_log),
                    "duration_sec": round(time.perf_counter() - started_at, 3),
                    "is_error": bool(result.get("is_error")),
                    "status": result_meta.get("status"),
                    "exit_code": result_meta.get("exit_code"),
                    "signal": result_meta.get("signal"),
                    "result_preview": sanitize_tool_result_for_log(
                        truncate_for_log(result.get("result", ""), 500)
                    ),
                })
                return result
            except (TimeoutError, concurrent.futures.TimeoutError):
                is_reviewed_mutative = fn_name in REVIEWED_MUTATIVE_TOOLS

                if is_reviewed_mutative:
                    # Commit/review tools must not end with ambiguous timeout.
                    try:
                        from ouroboros.tools.commit_gate import _mark_review_attempt_late
                        ctx = getattr(tools, "_ctx", None)
                        if ctx is not None:
                            _mark_review_attempt_late(
                                ctx,
                                soft_timeout_sec=timeout_sec,
                                duration_sec=round(time.perf_counter() - started_at, 1),
                            )
                    except Exception:
                        log.debug("Failed to mark reviewed attempt as late_result_pending", exc_info=True)
                    _emit_live_log(tools, {
                        "type": "tool_call_late",
                        "task_id": task_id,
                        "tool": fn_name,
                        "args": args_for_log,
                        "soft_timeout_sec": timeout_sec,
                        "message": (
                            f"Reviewed mutative tool '{fn_name}' exceeded "
                            f"{timeout_sec}s — still waiting for result "
                            f"(hard ceiling: {_REVIEWED_MUTATIVE_HARD_CEILING}s)"
                        ),
                    })
                    try:
                        ceiling = max(_REVIEWED_MUTATIVE_HARD_CEILING, timeout_sec + 60)
                        remaining = max(1, ceiling - timeout_sec)
                        result = future.result(timeout=remaining)
                        result_meta = result.get("result_meta") or {}
                        _emit_live_log(tools, {
                            "type": "tool_call_finished",
                            "task_id": task_id,
                            "tool": fn_name,
                            "args": result.get("args_for_log", args_for_log),
                            "duration_sec": round(time.perf_counter() - started_at, 3),
                            "is_error": bool(result.get("is_error")),
                            "status": result_meta.get("status"),
                            "late": True,
                        })
                        return result
                    except (TimeoutError, concurrent.futures.TimeoutError):
                        # Hard ceiling records terminal state; late real result may overwrite.
                        try:
                            from ouroboros.tools.commit_gate import _record_commit_attempt
                            ctx = getattr(tools, "_ctx", None)
                            if ctx is not None:
                                _record_commit_attempt(
                                    ctx,
                                    commit_message=str(getattr(ctx, "_current_review_commit_message", "") or ""),
                                    status="failed",
                                    block_reason="infra_failure",
                                    block_details=(
                                        f"Hard ceiling timeout ({_REVIEWED_MUTATIVE_HARD_CEILING}s). "
                                        "The underlying operation may still complete later."
                                    ),
                                    duration_sec=round(time.perf_counter() - started_at, 1),
                                    late_result_pending=True,
                                    phase="late_hard_ceiling",
                                    readiness_warnings=[
                                        "Reviewed mutative tool exceeded the hard ceiling; late result may still arrive."
                                    ],
                                    degraded_reasons=[
                                        f"hard_ceiling_timeout:{_REVIEWED_MUTATIVE_HARD_CEILING}"
                                    ],
                                )
                        except Exception:
                            pass
                        timeout_result = _make_timeout_result(
                            fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                            _REVIEWED_MUTATIVE_HARD_CEILING, task_id,
                            reset_msg=(
                                f"CRITICAL: Reviewed mutative tool hit hard ceiling "
                                f"({_REVIEWED_MUTATIVE_HARD_CEILING}s). "
                                "Check git state manually. "
                            ),
                        )
                        _emit_live_log(tools, {
                            "type": "tool_call_timeout",
                            "task_id": task_id,
                            "tool": fn_name,
                            "args": args_for_log,
                            "duration_sec": round(time.perf_counter() - started_at, 3),
                            "timeout_sec": _REVIEWED_MUTATIVE_HARD_CEILING,
                            "hard_ceiling": True,
                        })
                        return timeout_result
                else:
                    timeout_result = _make_timeout_result(
                        fn_name, tool_call_id, is_code_tool, tc, drive_logs,
                        timeout_sec, task_id, reset_msg=""
                    )
                    _emit_live_log(tools, {
                        "type": "tool_call_timeout",
                        "task_id": task_id,
                        "tool": fn_name,
                        "args": args_for_log,
                        "duration_sec": round(time.perf_counter() - started_at, 3),
                        "timeout_sec": timeout_sec,
                    })
                    return timeout_result
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def handle_tool_calls(
    tool_calls: List[Dict[str, Any]],
    tools: ToolRegistry,
    drive_logs: pathlib.Path,
    task_id: str,
    stateful_executor: StatefulToolExecutor,
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """Execute tool calls, append results, and return error count."""
    can_parallel = (
        len(tool_calls) > 1 and
        all(
            str(tc.get("function", {}).get("name") or "").strip() in READ_ONLY_PARALLEL_TOOLS
            for tc in tool_calls
        )
    )

    if not can_parallel:
        results = [
            _execute_with_timeout(tools, tc, drive_logs,
                                  _get_tool_timeout(tools, str(tc["function"]["name"] or "").strip()), task_id,
                                  stateful_executor)
            for tc in tool_calls
        ]
    else:
        max_workers = min(len(tool_calls), 8)
        executor = ThreadPoolExecutor(max_workers=max_workers)
        try:
            future_to_index = {
                executor.submit(
                    _execute_with_timeout, tools, tc, drive_logs,
                    _get_tool_timeout(tools, str(tc["function"]["name"] or "").strip()), task_id,
                    stateful_executor,
                ): idx
                for idx, tc in enumerate(tool_calls)
            }
            results = [None] * len(tool_calls)
            for future in as_completed(future_to_index):
                idx = future_to_index[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    tc = tool_calls[idx]
                    requested_fn_name = tc.get("function", {}).get("name", "unknown")
                    fn_name = str(requested_fn_name or "").strip()
                    results[idx] = {
                        "tool_call_id": tc.get("id", ""),
                        "fn_name": fn_name,
                        "result": f"⚠️ TOOL_ERROR: Unexpected error: {exc}",
                        "is_error": True,
                        "tool_args": {},
                        "args_for_log": {},
                        "is_code_tool": fn_name in tools.CODE_TOOLS,
                        "result_meta": _extract_result_metadata(
                            fn_name,
                            f"⚠️ TOOL_ERROR: Unexpected error: {exc}",
                            True,
                        ),
                    }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    return process_tool_results(results, messages, llm_trace, emit_progress)


def process_tool_results(
    results: List[Dict[str, Any]],
    messages: List[Dict[str, Any]],
    llm_trace: Dict[str, Any],
    emit_progress: Callable[[str], None],
) -> int:
    """Append tool results to messages/trace and return error count."""
    error_count = 0

    for exec_result in results:
        fn_name = exec_result["fn_name"]
        is_error = exec_result["is_error"]

        if is_error:
            error_count += 1

        truncated_result = _truncate_tool_result(
            exec_result["result"],
            tool_name=fn_name,
            tool_args=exec_result.get("tool_args"),
        )

        messages.append({
            "role": "tool",
            "tool_call_id": exec_result["tool_call_id"],
            "content": truncated_result
        })

        llm_trace["tool_calls"].append({
            "tool": fn_name,
            "args": _safe_args(exec_result["args_for_log"]),
            "result": truncate_for_log(exec_result["result"], 700),
            "is_error": is_error,
            **(exec_result.get("result_meta") or {}),
        })

    return error_count


def _safe_args(v: Any) -> Any:
    """Ensure args are JSON-serializable for trace logging."""
    try:
        return json.loads(json.dumps(v, ensure_ascii=False, default=str))
    except Exception:
        log.debug("Failed to serialize args for trace logging", exc_info=True)
        return {"_repr": repr(v)}

"""Claude Agent SDK transport for edit and read-only advisory paths.

Callers own orchestration and validation. This layer keeps SDK hooks,
ANTHROPIC_API_KEY auth, bundled CLI resolution, stderr capture, and no
CLI fallback when the SDK is missing.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.config import get_runtime_mode
from ouroboros.runtime_mode_policy import (
    SAFETY_CRITICAL_PATHS,
    is_protected_runtime_path,
    mode_allows_protected_write,
    protected_write_block_message,
)

log = logging.getLogger(__name__)

# Eager import preserves the no-CLI-fallback install hint path.
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    AssistantMessage, ResultMessage,
)

_STDERR_MAX_LINES = 200
_stderr_lock = threading.Lock()
_stderr_buffer: collections.deque[str] = collections.deque(maxlen=_STDERR_MAX_LINES)
DEFAULT_CLAUDE_CODE_MAX_TURNS = 50


def _stderr_callback(line: str) -> None:
    """Store raw CLI stderr for failure diagnostics."""
    log.warning("claude-cli stderr: %s", line)
    with _stderr_lock:
        _stderr_buffer.append(line)


def get_last_stderr(max_chars: int = 4000) -> str:
    """Return recent CLI stderr."""
    with _stderr_lock:
        lines = list(_stderr_buffer)
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def clear_stderr_buffer() -> None:
    """Clear captured CLI stderr."""
    with _stderr_lock:
        _stderr_buffer.clear()

SAFETY_CRITICAL = SAFETY_CRITICAL_PATHS


@dataclass
class ClaudeCodeResult:
    """Structured SDK invocation result."""

    success: bool
    result_text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    usage: Dict[str, int] = field(default_factory=dict)
    error: str = ""
    stderr_tail: str = ""
    # Populated by callers after invocation.
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""
    validation_summary: str = ""

    def to_tool_output(self) -> str:
        """Return structured JSON for tool output."""
        out: Dict[str, Any] = {
            "success": self.success,
            "result": self.result_text,
        }
        if self.session_id:
            out["session_id"] = self.session_id
        if self.cost_usd:
            out["cost_usd"] = round(self.cost_usd, 6)
        if self.usage:
            out["usage"] = self.usage
        if self.changed_files:
            out["changed_files"] = self.changed_files
        if self.diff_stat:
            out["diff_stat"] = self.diff_stat
        if self.error:
            out["error"] = self.error
        if self.stderr_tail:
            out["stderr_tail"] = self.stderr_tail
        if self.validation_summary:
            out["validation"] = self.validation_summary
        return json.dumps(out, ensure_ascii=False, indent=2)


def _coerce_usage_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_sdk_usage(usage: Any) -> Dict[str, Any]:
    """Map Anthropic token usage names to Ouroboros budget/log keys."""
    if not isinstance(usage, dict):
        return {}
    normalized = dict(usage)
    normalized["prompt_tokens"] = _coerce_usage_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0))
    )
    normalized["completion_tokens"] = _coerce_usage_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0))
    )
    normalized["cached_tokens"] = _coerce_usage_int(
        usage.get("cached_tokens", usage.get("cache_read_input_tokens", 0))
    )
    normalized["cache_write_tokens"] = _coerce_usage_int(
        usage.get("cache_write_tokens", usage.get("cache_creation_input_tokens", 0))
    )
    return normalized


def make_path_guard(cwd: str, repo_root: str | None = None):
    """Block SDK writes outside cwd or runtime-protected paths."""
    cwd_resolved = pathlib.Path(cwd).resolve()
    repo_root_resolved = pathlib.Path(repo_root).resolve() if repo_root else None

    async def path_guard(input_data: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        if tool_name not in ("Edit", "Write", "MultiEdit"):
            return {}

        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
        if not file_path:
            return {}

        target = pathlib.Path(file_path)
        if not target.is_absolute():
            target = cwd_resolved / target
        target = target.resolve()

        try:
            target.relative_to(cwd_resolved)
        except ValueError:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"SAFETY: Write blocked — target path '{file_path}' "
                        f"resolves outside the allowed working directory '{cwd}'."
                    ),
                }
            }

        # Prefer repo-root relative paths so subdir cwd still hits protection tables.
        rel = target.relative_to(cwd_resolved).as_posix()
        if repo_root_resolved is not None:
            try:
                rel = target.relative_to(repo_root_resolved).as_posix()
            except ValueError:
                pass
        try:
            from ouroboros.config import DATA_DIR
            from ouroboros.tools.core import is_skill_control_plane_path

            if is_skill_control_plane_path(target, pathlib.Path(DATA_DIR).resolve(strict=False)):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "SAFETY: Write blocked — skill provenance, "
                            "launcher seed, marketplace, dependency, and "
                            "self-authored markers are control-plane state."
                        ),
                    }
                }
        except Exception:
            log.debug("Claude Code skill control-plane guard probe failed", exc_info=True)
        try:
            runtime_mode = get_runtime_mode()
        except Exception:
            runtime_mode = "advanced"
        if is_protected_runtime_path(rel) and not mode_allows_protected_write(runtime_mode):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        protected_write_block_message(
                            path=rel,
                            runtime_mode=runtime_mode,
                            action="delegate-edit",
                        )
                    ),
                }
            }

        return {}

    return path_guard


def make_readonly_guard():
    """Deny all mutating tools in advisory mode."""

    async def readonly_guard(input_data: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "")
        if tool_name in ("Edit", "Write", "MultiEdit", "Bash"):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"SAFETY: '{tool_name}' is not allowed in read-only advisory mode. "
                        "Only Read, Grep, Glob are permitted."
                    ),
                }
            }
        return {}

    return readonly_guard


async def _run_edit_async(
    prompt: str,
    cwd: str,
    model: str = "opus",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    budget: Optional[float] = None,
    system_prompt: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> ClaudeCodeResult:
    """Run edit-mode SDK with safety hooks."""
    path_guard = make_path_guard(cwd, repo_root=repo_root)
    clear_stderr_buffer()

    options = ClaudeAgentOptions(
        cwd=cwd,
        model=model,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit", "Grep", "Glob"],
        disallowed_tools=["Bash", "MultiEdit"],
        max_turns=max_turns,
        max_budget_usd=budget,
        system_prompt=system_prompt,
        stderr=_stderr_callback,
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Edit|Write|MultiEdit", hooks=[path_guard]),
            ],
        },
    )

    result = ClaudeCodeResult(success=True)
    text_parts: List[str] = []

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result.session_id = getattr(message, "session_id", "") or ""
                    result.cost_usd = getattr(message, "total_cost_usd", 0) or 0
                    usage = getattr(message, "usage", None)
                    result.usage = _normalize_sdk_usage(usage)
                    subtype = getattr(message, "subtype", "")
                    if subtype and subtype != "success":
                        result.success = False
                        result.error = f"Agent ended with subtype: {subtype}"
                    break
    except Exception as e:
        result.success = False
        result.error = f"{type(e).__name__}: {e}"

    if not result.success:
        result.stderr_tail = get_last_stderr()
    result.result_text = "\n".join(text_parts) if text_parts else "(no output)"
    return result


async def _run_readonly_async(
    prompt: str,
    cwd: str,
    model: str = "opus",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    effort: Optional[str] = "high",
) -> ClaudeCodeResult:
    """Run read-only advisory SDK with the client lifecycle to avoid stream races."""
    clear_stderr_buffer()
    options_kwargs: Dict[str, Any] = dict(
        cwd=cwd,
        model=model,
        permission_mode="default",  # no auto-approve
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Bash", "Edit", "Write", "MultiEdit"],
        max_turns=max_turns,
        stderr=_stderr_callback,
    )
    if effort is not None:
        # Older SDKs may lack effort; omit it rather than failing advisory.
        import inspect as _inspect
        try:
            _sig = _inspect.signature(ClaudeAgentOptions.__init__)
            if "effort" in _sig.parameters:
                options_kwargs["effort"] = effort
        except (ValueError, TypeError):
            options_kwargs["effort"] = effort

    try:
        options = ClaudeAgentOptions(**options_kwargs)
    except TypeError:
        options_kwargs.pop("effort", None)
        options = ClaudeAgentOptions(**options_kwargs)

    result = ClaudeCodeResult(success=True)
    text_parts: List[str] = []

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result.session_id = getattr(message, "session_id", "") or ""
                    result.cost_usd = getattr(message, "total_cost_usd", 0) or 0
                    usage = getattr(message, "usage", None)
                    result.usage = _normalize_sdk_usage(usage)
                    subtype = getattr(message, "subtype", "")
                    if subtype and subtype != "success":
                        result.success = False
                        result.error = f"Agent ended with subtype: {subtype}"
                    break
    except Exception as e:
        result.success = False
        result.error = f"{type(e).__name__}: {e}"

    if not result.success:
        result.stderr_tail = get_last_stderr()
    result.result_text = "\n".join(text_parts) if text_parts else "(no output)"
    return result


def _run_async(coro):
    """Run async SDK code from synchronous tool handlers."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()


def run_edit(
    prompt: str,
    cwd: str,
    model: str = "claude-opus-4-6[1m]",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    budget: Optional[float] = None,
    system_prompt: Optional[str] = None,
    repo_root: Optional[str] = None,
) -> ClaudeCodeResult:
    """Synchronous edit-mode SDK entry point."""
    return _run_async(_run_edit_async(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        budget=budget,
        system_prompt=system_prompt,
        repo_root=repo_root,
    ))


def resolve_claude_code_model(default: str = "claude-opus-4-6[1m]") -> str:
    """Return the env/settings Claude Code model, aligned with config defaults."""
    return os.environ.get("CLAUDE_CODE_MODEL", default).strip() or default


def run_readonly(
    prompt: str,
    cwd: str,
    model: str = "claude-opus-4-6[1m]",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    effort: Optional[str] = "high",
) -> ClaudeCodeResult:
    """Synchronous read-only advisory entry point."""
    return _run_async(_run_readonly_async(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
    ))

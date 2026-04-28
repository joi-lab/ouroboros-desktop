"""Shell tools: run_shell, claude_code_edit."""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import threading
from subprocess import Popen, CompletedProcess
from typing import Any, Dict, List

from ouroboros.platform_layer import IS_WINDOWS, kill_process_tree, subprocess_new_group_kwargs
from ouroboros.config import load_settings
from ouroboros.tools.commit_gate import _invalidate_advisory
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, run_cmd

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subprocess process-group registry (for panic kill)
# ---------------------------------------------------------------------------
_active_subprocesses: set = set()
_subprocess_lock = threading.Lock()

_RUN_SHELL_DEFAULT_TIMEOUT_SEC = 360


def _tracked_subprocess_run(cmd, **kwargs):
    """subprocess.run() replacement with process group tracking.

    Each subprocess gets its own session (start_new_session=True) so the
    entire process tree can be killed via os.killpg() on panic.
    """
    timeout = kwargs.pop("timeout", None)
    kwargs.update(subprocess_new_group_kwargs())
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    proc = Popen(cmd, **kwargs)
    with _subprocess_lock:
        _active_subprocesses.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait(timeout=5)
        raise
    finally:
        with _subprocess_lock:
            _active_subprocesses.discard(proc)


def _kill_process_group(proc):
    """Kill a subprocess and its entire process tree."""
    kill_process_tree(proc)


def kill_all_tracked_subprocesses():
    """Kill all tracked subprocess trees. Called on panic."""
    with _subprocess_lock:
        procs = list(_active_subprocesses)
    for proc in procs:
        _kill_process_group(proc)
    with _subprocess_lock:
        _active_subprocesses.clear()


def _resolve_effective_timeout(default_timeout_sec: int) -> int:
    """Resolve effective timeout from settings.json with env fallback."""
    try:
        settings_val = int(load_settings().get("OUROBOROS_TOOL_TIMEOUT_SEC") or 0)
        if settings_val > 0:
            return settings_val
    except Exception:
        pass
    raw = str(os.environ.get("OUROBOROS_TOOL_TIMEOUT_SEC", "") or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return max(int(default_timeout_sec), 1)


def _describe_returncode(returncode: int) -> str:
    """Render a return code with signal details when applicable."""
    if int(returncode) < 0:
        signal_num = abs(int(returncode))
        try:
            signal_name = signal.Signals(signal_num).name
        except ValueError:
            signal_name = f"SIG{signal_num}"
        return f"exit_code={returncode} (signal={signal_name})"
    return f"exit_code={returncode}"


def _format_process_output(stdout: str, stderr: str, *, limit: int = 50_000) -> str:
    """Render stdout/stderr sections with truncation."""
    stdout_text = str(stdout or "").strip()
    stderr_text = str(stderr or "").strip()
    parts: List[str] = []
    if stdout_text:
        parts.append(f"STDOUT:\n{stdout_text}")
    if stderr_text:
        parts.append(f"STDERR:\n{stderr_text}")
    rendered = "\n\n".join(parts) if parts else "STDOUT:\n(empty)"
    if len(rendered) > limit:
        rendered = rendered[: limit // 2] + "\n...(truncated)...\n" + rendered[-limit // 2 :]
    return rendered


def _format_process_failure(prefix: str, action: str, res: CompletedProcess) -> str:
    """Render a subprocess failure with output context."""
    return (
        f"{prefix}: {action} with {_describe_returncode(res.returncode)}.\n\n"
        f"{_format_process_output(res.stdout or '', res.stderr or '')}"
    )


def _resolve_git_root(path: pathlib.Path) -> pathlib.Path | None:
    try:
        from ouroboros.review_state import discover_repo_root
        root = discover_repo_root(path)
        return root if (root / ".git").exists() else None
    except Exception:
        return None


def _status_snapshot(repo_dir: pathlib.Path | None) -> list[str]:
    if repo_dir is None:
        return []
    return sorted(_get_changed_files(repo_dir))


# ---------------------------------------------------------------------------
# Shell builtins / operators that cannot run via subprocess
# ---------------------------------------------------------------------------
_SHELL_BUILTINS = frozenset([
    "cd", "source", ".", "export", "alias", "eval",
    "set", "unset", "pushd", "popd", "read", "ulimit",
])

_SHELL_OPERATORS = frozenset(["&&", "||", "|", ";", ">", ">>", "<", "<<"])
_SHELL_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "fish",
    "cmd", "cmd.exe",
    "powershell", "powershell.exe",
    "pwsh", "pwsh.exe",
})
_ENV_REF_PATTERN = re.compile(r'\$(?:\{[A-Z][A-Z0-9_]*\}|[A-Z][A-Z0-9_]*)')


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------
def _run_shell(ctx: ToolContext, cmd, cwd: str = "") -> str:
    if isinstance(cmd, str):
        # Cascade recovery: try to parse the string into a list before failing.
        # LLMs frequently pass cmd as a string instead of a JSON array.
        recovered = None
        # 1. JSON array: '["grep", "-r", "pattern"]'
        try:
            parsed = json.loads(cmd)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                recovered = parsed
        except (json.JSONDecodeError, ValueError):
            pass
        # 2. Python literal: "['grep', '-r']"
        if recovered is None:
            try:
                parsed = ast.literal_eval(cmd)
                if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                    recovered = parsed
            except (ValueError, SyntaxError):
                pass
        # 3. Shell-style string: "grep -rn pattern ."
        if recovered is None:
            try:
                parts = shlex.split(cmd)
                if parts:
                    recovered = parts
            except ValueError:
                pass
        if recovered is not None:
            cmd = recovered
        else:
            return (
                '⚠️ SHELL_ARG_ERROR: `cmd` must be a JSON array of strings, not a plain string.\n\n'
                'Correct usage:\n'
                '  run_shell(cmd=["grep", "-r", "pattern", "path/"])\n'
                '  run_shell(cmd=["python", "-c", "print(1+1)"])\n\n'
                'Wrong usage:\n'
                '  run_shell(cmd="grep -r pattern path/")\n\n'
                'For reading files, prefer `repo_read` / `data_read`.\n'
                'For searching code, prefer `code_search`.'
            )

    if not isinstance(cmd, list):
        return "⚠️ SHELL_ARG_ERROR: cmd must be a list of strings."
    cmd = [str(x) for x in cmd]

    executable_name = pathlib.Path(cmd[0]).name.lower() if cmd else ""
    if executable_name not in _SHELL_INTERPRETERS:
        for arg in cmd:
            match = _ENV_REF_PATTERN.search(arg)
            if match:
                return (
                    f'⚠️ SHELL_ENV_ERROR: Found literal env reference "{match.group(0)}" in cmd array. '
                    "run_shell executes argv directly, so shell variables are not expanded. "
                    'Use ["sh", "-c", "..."] if you intentionally need shell expansion, '
                    "or read the environment variable inside the called program."
                )

    # Reject shell builtins (they are not executables)
    if cmd and cmd[0] in _SHELL_BUILTINS:
        if cmd[0] == "cd":
            return (
                '⚠️ SHELL_CMD_ERROR: "cd" is a shell builtin, not an executable. '
                'Use the "cwd" parameter instead: '
                'run_shell(cmd=["git", "log"], cwd="/target/dir")'
            )
        return (
            f'⚠️ SHELL_CMD_ERROR: "{cmd[0]}" is a shell builtin and cannot '
            'be executed directly via subprocess. '
            'Use ["sh", "-c", "your command"] if you need shell builtins.'
        )

    # Reject shell operators in cmd array (subprocess doesn't interpret them)
    found_ops = _SHELL_OPERATORS.intersection(cmd)
    if found_ops:
        op = sorted(found_ops)[0]
        return (
            f'⚠️ SHELL_CMD_ERROR: Shell operator "{op}" found in cmd array. '
            'Subprocess does not interpret shell syntax. '
            'Options: (1) Split into separate run_shell calls. '
            '(2) For pipes/chaining: ["sh", "-c", "cmd1 && cmd2"]'
        )

    work_dir = ctx.repo_dir
    if cwd and cwd.strip() not in ("", ".", "./"):
        candidate = (ctx.repo_dir / cwd).resolve()
        if candidate.exists() and candidate.is_dir():
            work_dir = candidate
    repo_root = _resolve_git_root(pathlib.Path(work_dir))
    before_changed = _status_snapshot(repo_root)

    timeout_sec = _resolve_effective_timeout(_RUN_SHELL_DEFAULT_TIMEOUT_SEC)
    try:
        res = _tracked_subprocess_run(
            cmd, cwd=str(work_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout_sec,
        )
        if res.returncode != 0:
            return _format_process_failure(
                "⚠️ SHELL_EXIT_ERROR",
                "command exited",
                res,
            )
        after_changed = _status_snapshot(repo_root)
        if after_changed != before_changed:
            _invalidate_advisory(
                ctx,
                changed_paths=after_changed or before_changed,
                mutation_root=repo_root,
                source_tool="run_shell",
            )
        return f"exit_code=0\n{_format_process_output(res.stdout or '', res.stderr or '')}"
    except subprocess.TimeoutExpired:
        return (
            f"⚠️ TOOL_TIMEOUT (run_shell): command exceeded {timeout_sec}s. "
            "Subprocess tree was terminated."
        )
    except Exception as e:
        return f"⚠️ SHELL_ERROR: {e}"


# ---------------------------------------------------------------------------
# Orchestration helpers (live in tool layer, not in gateway)
# ---------------------------------------------------------------------------

def _load_project_context(repo_dir: pathlib.Path) -> str:
    """Load project docs for Claude Code system_prompt injection."""
    docs = [
        ("BIBLE.md", "CONSTITUTION"),
        ("docs/DEVELOPMENT.md", "DEVELOPMENT GUIDE"),
        ("docs/CHECKLISTS.md", "REVIEW CHECKLISTS"),
        ("docs/ARCHITECTURE.md", "ARCHITECTURE"),
    ]
    parts: list = []
    for relpath, label in docs:
        fpath = repo_dir / relpath
        if fpath.is_file():
            try:
                content = fpath.read_text(encoding="utf-8")
                if len(content) > 50_000:
                    content = content[:50_000] + "\n\n[... truncated for context size ...]"
                parts.append(f"## {label}\n\n{content}")
            except Exception:
                pass
    return "\n\n---\n\n".join(parts)


def _get_changed_files(repo_dir: pathlib.Path) -> list:
    """Return list of changed files after an edit."""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return [line[3:].strip() for line in res.stdout.strip().splitlines() if len(line) > 3]
    except Exception:
        pass
    return []


def _get_diff_stat(repo_dir: pathlib.Path) -> str:
    """Return git diff --stat output."""
    try:
        res = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _run_validation(repo_dir: pathlib.Path) -> str:
    """Run basic validation after edit (tests). Returns summary.

    Pattern #4 architectural fix (v5.3.4 extension): uses sys.executable so the
    post-edit validator runs pytest in the same interpreter that has all agent
    dependencies. Previously hardcoded ``python`` from PATH, which doesn't exist
    in packaged bundles.
    """
    agent_python = sys.executable or os.environ.get("OUROBOROS_AGENT_PYTHON") or "python3"
    try:
        res = subprocess.run(
            [agent_python, "-m", "pytest", "tests/", "--tb=line", "-q"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
        )
        if res.returncode == 0:
            return "PASS: all tests passed"
        output = (res.stdout or "")[-500:]
        return f"FAIL: tests failed (exit {res.returncode})\n{output}"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: validation exceeded 60s"
    except Exception as e:
        return f"ERROR: validation failed: {e}"


# ---------------------------------------------------------------------------
# claude_code_edit — SDK-only path
# ---------------------------------------------------------------------------

def _claude_code_edit(ctx: ToolContext, prompt: str, cwd: str = "",
                      budget: float = 5.0, validate: bool = False) -> str:
    """Delegate code edits via the Claude Agent SDK gateway.

    Uses the claude-agent-sdk Python package with PreToolUse safety hooks
    that block writes outside cwd and to protected runtime paths.
    """
    from ouroboros.tools.git import _acquire_git_lock, _release_git_lock

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "⚠️ CLAUDE_CODE_UNAVAILABLE: ANTHROPIC_API_KEY not set."

    work_dir = str(ctx.repo_dir)
    if cwd and cwd.strip() not in ("", ".", "./"):
        candidate = (ctx.repo_dir / cwd).resolve()
        if candidate.exists():
            work_dir = str(candidate)
    work_dir_path = pathlib.Path(work_dir).resolve()
    target_repo_root = _resolve_git_root(work_dir_path) or pathlib.Path(ctx.repo_dir)
    before_changed = _status_snapshot(target_repo_root)

    from ouroboros.gateways.claude_code import resolve_claude_code_model
    model = resolve_claude_code_model()

    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"

        ctx.emit_progress_fn("Delegating to Claude Agent SDK...")

        try:
            from ouroboros.gateways.claude_code import (
                DEFAULT_CLAUDE_CODE_MAX_TURNS,
                run_edit,
            )

            system_prompt = (
                f"STRICT: Only modify files inside {work_dir}. "
                f"Git branch: {ctx.branch_dev}. Do NOT commit or push.\n\n"
                + _load_project_context(pathlib.Path(ctx.repo_dir))
            )

            result = run_edit(
                prompt=prompt,
                cwd=work_dir,
                model=model,
                max_turns=DEFAULT_CLAUDE_CODE_MAX_TURNS,
                budget=budget,
                system_prompt=system_prompt,
            )

            result.changed_files = _get_changed_files(target_repo_root)
            result.diff_stat = _get_diff_stat(target_repo_root)

            if validate and result.success:
                result.validation_summary = _run_validation(target_repo_root)

            if result.cost_usd > 0:
                ctx.pending_events.append({
                    "type": "llm_usage",
                    "provider": "claude_agent_sdk",
                    "model": model,
                    "api_key_type": "anthropic",
                    "model_category": "claude_code",
                    "usage": result.usage or {"cost": result.cost_usd},
                    "cost": result.cost_usd,
                    "source": "claude_code_edit",
                    "ts": utc_now_iso(),
                    "category": "task",
                })

            if not result.success:
                return f"⚠️ CLAUDE_CODE_ERROR: {result.error}\n\n{result.result_text}"

            after_changed = _status_snapshot(target_repo_root)
            if after_changed != before_changed:
                _invalidate_advisory(
                    ctx,
                    changed_paths=result.changed_files or after_changed or before_changed,
                    mutation_root=target_repo_root,
                    source_tool="claude_code_edit",
                )

            return result.to_tool_output()

        except ImportError:
            return (
                "⚠️ CLAUDE_CODE_UNAVAILABLE: claude-agent-sdk not installed. "
                "Install: pip install 'ouroboros[claude-sdk]'"
            )
        except Exception as e:
            import sys
            sdk_version = "(unknown)"
            try:
                import importlib.metadata
                sdk_version = importlib.metadata.version("claude-agent-sdk")
            except Exception:
                pass
            return (
                f"⚠️ CLAUDE_CODE_FAILED: {type(e).__name__}: {e}\n"
                f"Diagnostic: sdk_version={sdk_version}, python={sys.executable}"
            )

    finally:
        _release_git_lock(lock)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("run_shell", {
            "name": "run_shell",
            "description": "Run a shell command (list of args) inside the repo. Returns stdout+stderr.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "default": ""},
            }, "required": ["cmd"]},
        }, _run_shell, is_code_tool=True, timeout_sec=_RUN_SHELL_DEFAULT_TIMEOUT_SEC),
        ToolEntry("claude_code_edit", {
            "name": "claude_code_edit",
            "description": (
                "Delegate code edits to Claude Code (via Agent SDK with safety guards). "
                "Prefer this for anything beyond one exact replacement: large single-file "
                "edits, repeated coordinated edits, multi-hunk work, multi-file changes, "
                "renames/signature changes, or uncertain scope. Prefer it over chaining "
                "many str_replace_editor calls. Follow with repo_commit."
            ),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string", "default": ""},
                "budget": {"type": "number",
                           "description": "Max USD for this Claude Code call. Default: 5.0"},
                "validate": {"type": "boolean", "default": False,
                             "description": "Run post-edit validation (tests). Returns summary in result."},
            }, "required": ["prompt"]},
        }, _claude_code_edit, is_code_tool=True, timeout_sec=1200),
    ]

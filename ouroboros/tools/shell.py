"""Shell tools: run_shell, claude_code_edit."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shlex
import shutil
import subprocess
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, run_cmd, append_jsonl, truncate_for_log

log = logging.getLogger(__name__)


def _run_shell(ctx: ToolContext, cmd, cwd: str = "") -> str:
    # Recover from LLM sending cmd as JSON string instead of list
    if isinstance(cmd, str):
        raw_cmd = cmd
        warning = "run_shell_cmd_string"
        try:
            parsed = json.loads(cmd)
            if isinstance(parsed, list):
                cmd = parsed
                warning = "run_shell_cmd_string_json_list_recovered"
            elif isinstance(parsed, str):
                try:
                    cmd = shlex.split(parsed)
                except ValueError:
                    cmd = parsed.split()
                warning = "run_shell_cmd_string_json_string_split"
            else:
                try:
                    cmd = shlex.split(cmd)
                except ValueError:
                    cmd = cmd.split()
                warning = "run_shell_cmd_string_json_non_list_split"
        except Exception:
            try:
                cmd = shlex.split(cmd)
            except ValueError:
                cmd = cmd.split()
            warning = "run_shell_cmd_string_split_fallback"

        try:
            append_jsonl(ctx.drive_logs() / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "tool_warning",
                "tool": "run_shell",
                "warning": warning,
                "cmd_preview": truncate_for_log(raw_cmd, 500),
            })
        except Exception:
            log.debug("Failed to log run_shell warning to events.jsonl", exc_info=True)
            pass

    if not isinstance(cmd, list):
        return "⚠️ SHELL_ARG_ERROR: cmd must be a list of strings."
    cmd = [str(x) for x in cmd]

    work_dir = ctx.repo_dir
    if cwd and cwd.strip() not in ("", ".", "./"):
        candidate = (ctx.repo_dir / cwd).resolve()
        if candidate.exists() and candidate.is_dir():
            work_dir = candidate

    try:
        res = subprocess.run(
            cmd, cwd=str(work_dir),
            capture_output=True, text=True, timeout=120,
        )
        out = res.stdout + ("\n--- STDERR ---\n" + res.stderr if res.stderr else "")
        if len(out) > 50000:
            out = out[:25000] + "\n...(truncated)...\n" + out[-25000:]
        prefix = f"exit_code={res.returncode}\n"
        return prefix + out
    except subprocess.TimeoutExpired:
        return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 600s. Stop retrying this exact prompt blindly!"
        return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 600s. Stop retrying this exact prompt blindly!"
        return "⚠️ TIMEOUT: command exceeded 120s."
    except Exception as e:
        return f"⚠️ SHELL_ERROR: {e}"


def _run_claude_cli(work_dir: str, prompt: str, env: dict) -> subprocess.CompletedProcess:
    """Run Claude CLI with permission-mode fallback."""
    claude_bin = shutil.which("claude")
    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "json",
        "--max-turns", "12",
        "--tools", "Read,Edit,Grep,Glob",
    ]

    # Try --permission-mode first, fallback to --dangerously-skip-permissions
    perm_mode = os.environ.get("OUROBOROS_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions").strip()
    primary_cmd = cmd + ["--permission-mode", perm_mode]
    legacy_cmd = cmd + ["--dangerously-skip-permissions"]

    res = subprocess.run(
        primary_cmd, cwd=work_dir,
        capture_output=True, text=True, timeout=600, env=env,
    )

    if res.returncode != 0:
        combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
        if "--permission-mode" in combined and any(
            m in combined for m in ("unknown option", "unknown argument", "unrecognized option", "unexpected argument")
        ):
            res = subprocess.run(
                legacy_cmd, cwd=work_dir,
                capture_output=True, text=True, timeout=600, env=env,
            )

    return res


def _check_uncommitted_changes(repo_dir: pathlib.Path) -> str:
    """Check git status after edit, return warning string or empty string."""
    try:
        status_res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status_res.returncode == 0 and status_res.stdout.strip():
            diff_res = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if diff_res.returncode == 0 and diff_res.stdout.strip():
                return (
                    f"\n\n⚠️ UNCOMMITTED CHANGES detected after Claude Code edit:\n"
                    f"{diff_res.stdout.strip()}\n"
                    f"Remember to run git_status and repo_commit_push!"
                )
    except Exception as e:
        log.debug("Failed to check git status after claude_code_edit: %s", e, exc_info=True)
    return ""


def _parse_claude_output(stdout: str, ctx: ToolContext) -> str:
    """Parse JSON output and emit cost event, return result string."""
    try:
        payload = json.loads(stdout)
        out: Dict[str, Any] = {
            "result": payload.get("result", ""),
            "session_id": payload.get("session_id"),
        }
        if isinstance(payload.get("total_cost_usd"), (int, float)):
            ctx.pending_events.append({
                "type": "llm_usage",
                "provider": "claude_code_cli",
                "usage": {"cost": float(payload["total_cost_usd"])},
                "source": "claude_code_edit",
                "ts": utc_now_iso(),
                "category": "task",
            })
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception:
        log.debug("Failed to parse claude_code_edit JSON output", exc_info=True)
        return stdout


def _claude_code_edit(ctx: ToolContext, prompt: str, cwd: str = "") -> str:
    """Delegate code edits to Claude Code CLI."""
    from ouroboros.tools.git import _acquire_git_lock, _release_git_lock

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "⚠️ ANTHROPIC_API_KEY not set, claude_code_edit unavailable."

    work_dir = str(ctx.repo_dir)
    if cwd and cwd.strip() not in ("", ".", "./"):
        candidate = (ctx.repo_dir / cwd).resolve()
        if candidate.exists():
            work_dir = str(candidate)

    claude_bin = shutil.which("claude")
    if not claude_bin:
        return "⚠️ Claude CLI not found. Ensure ANTHROPIC_API_KEY is set."

    ctx.emit_progress_fn("Delegating to Claude Code CLI...")

    lock = _acquire_git_lock(ctx)
    try:
        try:
            run_cmd(["git", "checkout", ctx.branch_dev], cwd=ctx.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (checkout): {e}"

        full_prompt = (
            f"STRICT: Only modify files inside {work_dir}. "
            f"Git branch: {ctx.branch_dev}. Do NOT commit or push.\n\n"
            f"{prompt}"
        )

        env = os.environ.copy()
        env["ANTHROPIC_API_KEY"] = api_key
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                env.setdefault("IS_SANDBOX", "1")
        except Exception:
            log.debug("Failed to check geteuid for sandbox detection", exc_info=True)
            pass
        local_bin = str(pathlib.Path.home() / ".local" / "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

        res = _run_claude_cli(work_dir, full_prompt, env)

        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()
        if res.returncode != 0:
            return f"⚠️ CLAUDE_CODE_ERROR: exit={res.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        if not stdout:
            stdout = "OK: Claude Code completed with empty output."

        # Check for uncommitted changes and append warning BEFORE finally block
        warning = _check_uncommitted_changes(ctx.repo_dir)
        if warning:
            stdout += warning

    except subprocess.TimeoutExpired:
        return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 600s. Stop retrying this exact prompt blindly!"
        return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 600s. Stop retrying this exact prompt blindly!"
        return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 600s."
    except Exception as e:
        return f"⚠️ CLAUDE_CODE_FAILED: {type(e).__name__}: {e}"
    finally:
        _release_git_lock(lock)

    # Parse JSON output and account cost
    return _parse_claude_output(stdout, ctx)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("run_shell", {
            "name": "run_shell",
            "description": "Run a shell command (list of args) inside the repo. Returns stdout+stderr.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "default": ""},
            }, "required": ["cmd"]},
        }, _run_shell, is_code_tool=True),
        ToolEntry("claude_code_edit", {
            "name": "claude_code_edit",
            "description": "Delegate code edits to Claude Code CLI. Preferred for multi-file changes and refactors. Follow with repo_commit_push.",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string", "default": ""},
            }, "required": ["prompt"]},
        }, _claude_code_edit, is_code_tool=True, timeout_sec=600),
    ]

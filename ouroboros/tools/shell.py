"""Shell tools: run_shell, claude_code_edit."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import platform
import shlex
import shutil
import subprocess
import threading
from subprocess import Popen, CompletedProcess
from typing import Any, Dict, List, Optional

from ouroboros.compat import IS_WINDOWS, PATH_SEP, kill_process_tree, node_download_info
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, run_cmd, append_jsonl, truncate_for_log

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subprocess process-group registry (for panic kill)
# ---------------------------------------------------------------------------
_active_subprocesses: set = set()
_subprocess_lock = threading.Lock()


def _tracked_subprocess_run(cmd, **kwargs):
    """subprocess.run() replacement with process group tracking.

    Each subprocess gets its own session (start_new_session=True) so the
    entire process tree can be killed via os.killpg() on panic.
    """
    timeout = kwargs.pop("timeout", None)
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
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


# ---------------------------------------------------------------------------
# Shell builtins / operators that cannot run via subprocess
# ---------------------------------------------------------------------------
_SHELL_BUILTINS = frozenset([
    "cd", "source", ".", "export", "alias", "eval",
    "set", "unset", "pushd", "popd", "read", "ulimit",
])

_SHELL_OPERATORS = frozenset(["&&", "||", "|", ";", ">", ">>", "<", "<<"])


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------
def _run_shell(ctx: ToolContext, cmd, cwd: str = "") -> str:
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

    try:
        res = _tracked_subprocess_run(
            cmd, cwd=str(work_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=120,
        )
        out = res.stdout + ("\n--- STDERR ---\n" + res.stderr if res.stderr else "")
        if len(out) > 50000:
            out = out[:25000] + "\n...(truncated)...\n" + out[-25000:]
        prefix = f"exit_code={res.returncode}\n"
        return prefix + out
    except subprocess.TimeoutExpired:
        return "⚠️ TIMEOUT: command exceeded 120s."
    except Exception as e:
        return f"⚠️ SHELL_ERROR: {e}"


# ---------------------------------------------------------------------------
# Claude Code CLI: auto-install
# ---------------------------------------------------------------------------
_NODE_DIR = pathlib.Path.home() / "Ouroboros" / "node"
_NODE_BIN = _NODE_DIR if IS_WINDOWS else _NODE_DIR / "bin"
_install_lock = threading.Lock()
_path_initialized = False


def _ensure_claude_cli(ctx: ToolContext) -> Optional[str]:
    """Ensure claude CLI is available. Auto-install if needed.

    Returns error string or None on success.
    """
    _ensure_path()
    if shutil.which("claude"):
        return None

    with _install_lock:
        _ensure_path()
        if shutil.which("claude"):
            return None

        ctx.emit_progress_fn("Claude CLI not found. Installing Node.js + Claude Code...")

        node_bin = _NODE_BIN / ("node.exe" if IS_WINDOWS else "node")
        if not node_bin.exists():
            err = _install_node()
            if err:
                return err
            _ensure_path()

        npm_name = "npm.cmd" if IS_WINDOWS else "npm"
        npm = str(_NODE_BIN / npm_name)
        if not pathlib.Path(npm).exists():
            return "⚠️ npm not found after Node.js install."

        try:
            _tracked_subprocess_run(
                [npm, "install", "-g", "@anthropic-ai/claude-code"],
                env={**os.environ, "PATH": _build_augmented_path()},
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=180,
            )
        except Exception as e:
            return f"⚠️ npm install failed: {e}"

        _ensure_path()
        if shutil.which("claude"):
            ctx.emit_progress_fn("Claude Code CLI installed successfully.")
            return None
        return "⚠️ Claude Code CLI binary not found in PATH after auto-install."


def _install_node() -> Optional[str]:
    """Download and extract Node.js LTS binary. Returns error string or None."""
    import urllib.request

    node_version = "v22.14.0"
    url, dir_name, archive_type = node_download_info(node_version)

    _NODE_DIR.mkdir(parents=True, exist_ok=True)
    archive_path = _NODE_DIR / ("node.zip" if archive_type == "zip" else "node.tar.gz")
    try:
        urllib.request.urlretrieve(url, archive_path)

        if archive_type == "zip":
            import zipfile
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(_NODE_DIR)
        else:
            import tarfile
            with tarfile.open(archive_path) as tf:
                tf.extractall(_NODE_DIR, filter="data")

        extracted = _NODE_DIR / dir_name
        if extracted.exists():
            for item in extracted.iterdir():
                dest = _NODE_DIR / item.name
                if dest.exists():
                    if dest.is_dir():
                        shutil.rmtree(dest)
                    else:
                        dest.unlink()
                shutil.move(str(item), str(dest))
            extracted.rmdir()
        archive_path.unlink(missing_ok=True)
        return None
    except Exception as e:
        archive_path.unlink(missing_ok=True)
        return f"⚠️ Node.js download/install failed: {e}"


def _build_augmented_path() -> str:
    """Build a PATH string with node/local dirs prepended. Pure function, no side effects."""
    current = os.environ.get("PATH", "")
    parts = []
    for d in [
        str(_NODE_BIN),
        str(_NODE_DIR / "lib" / "node_modules" / ".bin"),
        str(pathlib.Path.home() / ".local" / "bin"),
    ]:
        if d not in current and (d == str(_NODE_BIN) or pathlib.Path(d).exists()):
            parts.append(d)
    if parts:
        return PATH_SEP.join(parts) + PATH_SEP + current
    return current


def _ensure_path():
    """Set PATH once with node/local dirs. Idempotent — only mutates env on first call."""
    global _path_initialized
    if _path_initialized:
        return
    os.environ["PATH"] = _build_augmented_path()
    _path_initialized = True


# ---------------------------------------------------------------------------
# Claude Code CLI: run + parse
# ---------------------------------------------------------------------------
def _run_claude_cli(work_dir: str, prompt: str, env: dict,
                    model: str = "", budget: Optional[float] = None) -> CompletedProcess:
    """Run Claude CLI with permission-mode fallback."""
    claude_bin = shutil.which("claude")
    cmd = [
        claude_bin, "-p", prompt,
        "--output-format", "json",
        "--max-turns", "12",
        "--tools", "Read,Edit,Grep,Glob",
        "--no-session-persistence",
    ]
    if model:
        cmd += ["--model", model]
    if budget is not None:
        cmd += ["--max-budget-usd", str(budget)]

    perm_mode = os.environ.get("OUROBOROS_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions").strip()
    primary_cmd = cmd + ["--permission-mode", perm_mode]
    legacy_cmd = cmd + ["--dangerously-skip-permissions"]

    res = _tracked_subprocess_run(
        primary_cmd, cwd=work_dir,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=300, env=env,
    )

    if res.returncode != 0:
        combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
        if "--permission-mode" in combined and any(
            m in combined for m in ("unknown option", "unknown argument", "unrecognized option", "unexpected argument")
        ):
            res = _tracked_subprocess_run(
                legacy_cmd, cwd=work_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, timeout=300, env=env,
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
                    f"Remember to run git_status and repo_commit!"
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
                "model": os.environ.get("CLAUDE_CODE_MODEL", "sonnet"),
                "api_key_type": "anthropic",
                "model_category": "claude_code",
                "usage": {"cost": float(payload["total_cost_usd"])},
                "cost": float(payload["total_cost_usd"]),
                "source": "claude_code_edit",
                "ts": utc_now_iso(),
                "category": "task",
            })
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception:
        log.debug("Failed to parse claude_code_edit JSON output", exc_info=True)
        return stdout


def _claude_code_edit(ctx: ToolContext, prompt: str, cwd: str = "",
                      budget: float = 1.0) -> str:
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

    install_err = _ensure_claude_cli(ctx)
    if install_err:
        return install_err

    ctx.emit_progress_fn("Delegating to Claude Code CLI...")

    model = os.environ.get("CLAUDE_CODE_MODEL", "sonnet").strip()

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

        _ensure_path()
        env["PATH"] = os.environ["PATH"]

        res = _run_claude_cli(work_dir, full_prompt, env,
                              model=model, budget=budget)

        stdout = (res.stdout or "").strip()
        stderr = (res.stderr or "").strip()
        if res.returncode != 0:
            return f"⚠️ CLAUDE_CODE_ERROR: exit={res.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
        if not stdout:
            stdout = "OK: Claude Code completed with empty output."

        warning = _check_uncommitted_changes(ctx.repo_dir)
        if warning:
            stdout += warning

    except subprocess.TimeoutExpired:
        return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 300s."
    except Exception as e:
        return f"⚠️ CLAUDE_CODE_FAILED: {type(e).__name__}: {e}"
    finally:
        _release_git_lock(lock)

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
            "description": "Delegate code edits to Claude Code CLI. Preferred for multi-file changes and refactors. Follow with repo_commit.",
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string", "default": ""},
                "budget": {"type": "number",
                           "description": "Max USD for this Claude Code call. Default: 1.0"},
            }, "required": ["prompt"]},
        }, _claude_code_edit, is_code_tool=True, timeout_sec=300),
    ]

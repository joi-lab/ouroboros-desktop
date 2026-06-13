"""Process tools: run_command and run_script."""

from __future__ import annotations

import ast
import hashlib
from hashlib import sha256
import json
import logging
import os
import pathlib
import re
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import threading
import uuid
from subprocess import Popen, CompletedProcess
from typing import Any, Dict, List

from ouroboros.artifacts import artifact_store_path_block_reason, copy_file_to_task_artifacts
from ouroboros.platform_layer import IS_WINDOWS, bootstrap_process_path, kill_process_tree, subprocess_new_group_kwargs
from ouroboros.config import get_runtime_mode, load_settings
from ouroboros.runtime_mode_policy import (
    core_patch_notice,
    is_protected_runtime_path,
    mode_allows_protected_write,
    protected_paths_in,
)
from ouroboros.tools.commit_gate import _invalidate_advisory
from ouroboros.tools.shell_parse import EMBEDDED_ABSOLUTE_PATH_RE, shell_argv_with_inline
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.tool_access import (
    active_tool_profile,
    decide_tool_access,
    path_is_relative_to,
    resource_root_path,
    resolve_shell_cwd,
    user_files_path_block_reason,
)
from ouroboros.utils import safe_relpath, utc_now_iso, run_cmd
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.contracts.skill_payload_policy import (
    SKILL_PAYLOAD_CONTROL_DIRNAMES,
    SKILL_PAYLOAD_CONTROL_FILENAMES,
    SkillPayloadPathError,
    cross_skill_redirect_error,
    decide_payload_short_form,
    resolve_skill_payload_target,
)

log = logging.getLogger(__name__)

# Tracked process groups let panic kill descendant trees too.
_active_subprocesses: set = set()
_subprocess_lock = threading.Lock()

_RUN_SHELL_DEFAULT_TIMEOUT_SEC = 360
_CONTROL_DIR_BACKUP_MAX_BYTES = 5 * 1024 * 1024


def _tracked_subprocess_run(cmd, **kwargs):
    """subprocess.run replacement with process-tree tracking."""
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
    """Kill a subprocess tree."""
    kill_process_tree(proc)


def kill_all_tracked_subprocesses():
    """Kill all tracked subprocess trees on panic."""
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


def _describe_returncode(returncode: int, *, cwd: pathlib.Path | str | None = None) -> str:
    """Render a return code with signal details when applicable."""
    suffix: list[str] = []
    if int(returncode) < 0:
        signal_num = abs(int(returncode))
        try:
            signal_name = signal.Signals(signal_num).name
        except ValueError:
            signal_name = f"SIG{signal_num}"
        suffix.append(f"signal={signal_name}")
    if cwd is not None:
        suffix.append(f"cwd={pathlib.Path(cwd).resolve(strict=False)}")
    rendered_suffix = f" ({', '.join(suffix)})" if suffix else ""
    return f"exit_code={returncode}{rendered_suffix}"


def _format_process_output(stdout: str, stderr: str, *, limit: int = 50_000) -> str:
    """Render bounded stdout/stderr sections."""
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


def _allowed_output_roots(ctx: ToolContext, work_dir: pathlib.Path, cwd_root: str = "") -> list[tuple[str, pathlib.Path]]:
    roots: list[tuple[str, pathlib.Path]] = []
    root_label = str(cwd_root or "cwd").strip() or "cwd"
    roots.append((root_label, pathlib.Path(work_dir).resolve(strict=False)))
    profile = active_tool_profile(ctx)
    for label in ("task_drive", "artifact_store", "user_files"):
        if not decide_tool_access(profile=profile, root=label, operation="read").allow:  # type: ignore[arg-type]
            continue
        try:
            root_path = resource_root_path(ctx, label)  # type: ignore[arg-type]
        except Exception:
            continue
        if not any(path_is_relative_to(root_path, existing) and path_is_relative_to(existing, root_path) for _, existing in roots):
            roots.append((label, root_path))
    return roots


def _protected_output_source_reason(ctx: ToolContext, source: pathlib.Path, label: str, changed_paths: set[str]) -> str:
    """Return a block reason for protected/control-plane output sources."""

    name_lower = source.name.lower()
    if (
        source.name.startswith(".")
        or name_lower in _SENSITIVE_OUTPUT_NAMES
        or name_lower.endswith(_SENSITIVE_OUTPUT_SUFFIXES)
        or any(marker in name_lower for marker in _SENSITIVE_OUTPUT_MARKERS)
    ):
        return f"credential-like output {source.name} is not a deliverable artifact"

    try:
        system_repo = pathlib.Path(getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir")).resolve(strict=False)
    except Exception:
        system_repo = pathlib.Path(getattr(ctx, "repo_dir")).resolve(strict=False)
    if path_is_relative_to(source, system_repo):
        try:
            rel = source.relative_to(system_repo).as_posix()
        except ValueError:
            rel = source.name
        if is_protected_runtime_path(rel):
            return f"protected repo output {rel} is not a deliverable artifact"
        if label in {"active_workspace", "system_repo"} and rel not in changed_paths:
            return f"unchanged repo output {rel} is not a generated deliverable"

    try:
        drive = pathlib.Path(getattr(ctx, "drive_root")).resolve(strict=False)
        if path_is_relative_to(source, drive):
            task_drive = resource_root_path(ctx, "task_drive")
            artifact_store = resource_root_path(ctx, "artifact_store")
            if not (path_is_relative_to(source, task_drive) or path_is_relative_to(source, artifact_store)):
                return "runtime data output is not a user deliverable; use task_drive or artifact_store"
    except Exception:
        pass

    return ""


def _resolve_declared_output(
    ctx: ToolContext,
    raw_item: str,
    work_dir: pathlib.Path,
    cwd_root: str = "",
    changed_paths: set[str] | None = None,
) -> tuple[pathlib.Path | None, str]:
    text = str(raw_item or "").strip()
    if not text:
        return None, "empty output path"
    raw = pathlib.Path(text).expanduser()
    if raw.is_absolute() or text.startswith("~"):
        source = raw.resolve(strict=False)
    else:
        source = (pathlib.Path(work_dir) / safe_relpath(text)).resolve(strict=False)
    changed = changed_paths or set()
    for label, root in _allowed_output_roots(ctx, work_dir, cwd_root):
        if not path_is_relative_to(source, root):
            continue
        if label == "user_files":
            reason = user_files_path_block_reason(ctx, source)
            if reason:
                return None, f"protected user_files output {text}: {reason}"
        protected_reason = _protected_output_source_reason(ctx, source, label, changed)
        if protected_reason:
            return None, protected_reason
        return source, ""
    allowed = ", ".join(f"{label}={root}" for label, root in _allowed_output_roots(ctx, work_dir, cwd_root))
    return None, f"output escapes allowed artifact roots: {text}; allowed_roots: {allowed}"


def _fingerprint_output(path: pathlib.Path) -> tuple[bool, int, str]:
    try:
        if not path.is_file():
            return False, -1, ""
        raw = path.read_bytes()
        return True, len(raw), sha256(raw).hexdigest()
    except OSError:
        return False, -1, ""


def _snapshot_declared_outputs(ctx: ToolContext, outputs: List[str] | None, work_dir: pathlib.Path, cwd_root: str = "") -> Dict[str, tuple[bool, int, str]]:
    snapshots: Dict[str, tuple[bool, int, str]] = {}
    for raw_item in outputs or []:
        source, block_reason = _resolve_declared_output(ctx, str(raw_item or ""), work_dir, cwd_root=cwd_root)
        if source is not None and not block_reason:
            snapshots[str(source)] = _fingerprint_output(source)
    return snapshots


def _register_process_outputs(
    ctx: ToolContext,
    outputs: List[str] | None,
    work_dir: pathlib.Path,
    cwd_root: str = "",
    changed_paths: set[str] | None = None,
    before_outputs: Dict[str, tuple[bool, int, str]] | None = None,
) -> tuple[str, bool]:
    """Copy declared command outputs into the task artifact store."""

    if not outputs:
        return "", False
    notes: list[str] = []
    failed = False
    for raw_item in outputs:
        text = str(raw_item or "").strip()
        source, block_reason = _resolve_declared_output(
            ctx,
            text,
            work_dir,
            cwd_root=cwd_root,
            changed_paths=changed_paths,
        )
        if block_reason:
            notes.append(block_reason)
            failed = True
            continue
        if source is None:
            notes.append(f"invalid output: {text}")
            failed = True
            continue
        if not source.exists():
            notes.append(f"missing output: {text}")
            failed = True
            continue
        if not source.is_file():
            notes.append(f"skipped non-file output: {text}")
            failed = True
            continue
        before = (before_outputs or {}).get(str(source), (False, -1, ""))
        after = _fingerprint_output(source)
        if before[0] and before == after:
            notes.append(f"unchanged output: {text}")
            failed = True
            continue
        try:
            record = copy_file_to_task_artifacts(ctx, source, kind="process_output")
        except OSError as exc:
            notes.append(f"failed output copy {text}: {type(exc).__name__}: {exc}")
            failed = True
            continue
        if record:
            notes.append(
                f"registered output {source} -> artifact_store:{record.get('name')} "
                f"sha256={str(record.get('sha256') or '')[:12]}"
            )
        else:
            notes.append(f"failed output copy {text}: source is not a regular file")
            failed = True
    if not notes:
        return "", False
    prefix = "⚠️ ARTIFACT_OUTPUT_ERROR" if failed else "ARTIFACT_OUTPUTS"
    return "\n\n" + prefix + ":\n" + "\n".join(f"- {note}" for note in notes), failed


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


def _protected_runtime_dirty_paths(repo_dir: pathlib.Path) -> list[str]:
    dirty: set[str] = set()
    for cmd in (["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]):
        try:
            res = subprocess.run(
                cmd,
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0:
                dirty.update(rel for rel in res.stdout.splitlines() if is_protected_runtime_path(rel))
        except Exception:
            pass
    return sorted(dirty)


def _restore_protected_runtime_paths(repo_dir: pathlib.Path, paths: list[str]) -> list[str]:
    restored: list[str] = []
    for rel in sorted(set(paths)):
        try:
            subprocess.run(
                ["git", "reset", "HEAD", "--", rel],
                cwd=str(repo_dir),
                capture_output=True,
                timeout=5,
            )
            subprocess.run(
                ["git", "checkout", "--", rel],
                cwd=str(repo_dir),
                capture_output=True,
                timeout=5,
            )
            restored.append(rel)
        except Exception:
            pass
    return restored


def _tree_fingerprint(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    root = pathlib.Path(path)
    if not root.exists():
        return ""
    for child in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            st = child.lstat()
        except OSError:
            continue
        try:
            rel = child.relative_to(root).as_posix()
        except ValueError:
            rel = safe_relpath(str(child))
        digest.update(rel.encode("utf-8", errors="replace"))
        digest.update(str(st.st_mode).encode())
        digest.update(str(st.st_size).encode())
        digest.update(str(st.st_mtime_ns).encode())
        if stat.S_ISLNK(st.st_mode):
            try:
                digest.update(os.readlink(child).encode("utf-8", errors="replace"))
            except OSError:
                pass
    return digest.hexdigest()


def _snapshot_skill_control_paths(payload_root: pathlib.Path) -> Dict[pathlib.Path, Any]:
    snapshots: Dict[pathlib.Path, Any] = {}
    root = pathlib.Path(payload_root).resolve(strict=False)
    control_file_names = set(SKILL_PAYLOAD_CONTROL_FILENAMES) | {"SKILL.openclaw.md"}
    existing_names: set[str] = set()
    try:
        existing_names = {child.name for child in root.iterdir() if child.name.lower() in SKILL_PAYLOAD_CONTROL_FILENAMES}
    except OSError:
        existing_names = set()
    for name in sorted(control_file_names | existing_names):
        path = root / name
        try:
            snapshots[path] = ("file", path.read_bytes() if path.exists() else None)
        except OSError:
            snapshots[path] = ("file", None)
    for name in SKILL_PAYLOAD_CONTROL_DIRNAMES:
        path = root / name
        backup = None
        if path.exists() and path.is_dir():
            before_fingerprint = _tree_fingerprint(path)
            try:
                total = 0
                for child in sorted(path.rglob("*"), key=lambda item: item.as_posix()):
                    try:
                        total += child.lstat().st_size
                    except OSError:
                        continue
                    if total > _CONTROL_DIR_BACKUP_MAX_BYTES:
                        break
                if total <= _CONTROL_DIR_BACKUP_MAX_BYTES:
                    backup = pathlib.Path(
                        shutil.copytree(
                            path,
                            root.parent / f".ouroboros-control-backup-{uuid.uuid4().hex}" / name,
                            symlinks=True,
                        )
                    )
            except Exception:
                backup = None
            snapshots[path] = ("dir", True, before_fingerprint, backup)
        elif path.exists():
            try:
                snapshots[path] = ("dir_file", path.read_bytes())
            except OSError:
                snapshots[path] = ("dir_file", None)
        else:
            snapshots[path] = ("dir", False, "", None)
    return snapshots


def _restore_skill_control_changes(snapshots: Dict[pathlib.Path, Any]) -> list[str]:
    changed: list[str] = []
    for path, state in snapshots.items():
        kind = state[0]
        before = state[1:]
        name = path.name
        try:
            if kind == "file":
                before_bytes = before[0] if before else None
                after = path.read_bytes() if path.exists() else None
                if after != before_bytes:
                    if before_bytes is None:
                        path.unlink(missing_ok=True)
                    else:
                        path.write_bytes(before_bytes)
                    changed.append(name)
            elif kind == "dir":
                existed, before_fingerprint, backup = before
                after_fingerprint = _tree_fingerprint(path) if path.exists() and path.is_dir() else None
                if not existed:
                    if path.exists():
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink(missing_ok=True)
                        changed.append(name)
                elif after_fingerprint != before_fingerprint:
                    if backup is not None and pathlib.Path(backup).exists():
                        if path.exists():
                            if path.is_dir():
                                shutil.rmtree(path)
                            else:
                                path.unlink(missing_ok=True)
                        shutil.move(str(backup), str(path))
                    changed.append(name)
                if backup is not None:
                    try:
                        shutil.rmtree(pathlib.Path(backup).parent, ignore_errors=True)
                    except OSError:
                        pass
            elif kind == "dir_file":
                before_bytes = before[0] if before else None
                after = path.read_bytes() if path.exists() and path.is_file() else None
                if after != before_bytes:
                    if path.exists():
                        if path.is_dir():
                            shutil.rmtree(path)
                        else:
                            path.unlink(missing_ok=True)
                    if before_bytes is not None:
                        path.write_bytes(before_bytes)
                    changed.append(name)
            elif kind == "dir_unmoved":
                before_fingerprint, temp_root = before
                after_fingerprint = _tree_fingerprint(path) if path.exists() else None
                if after_fingerprint != before_fingerprint:
                    changed.append(name)
                try:
                    shutil.rmtree(temp_root, ignore_errors=True)
                except OSError:
                    pass
        except OSError:
            changed.append(name)
    return sorted(set(changed))


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
_SENSITIVE_OUTPUT_NAMES = frozenset({
    ".env",
    ".env.local",
    "credentials.json",
    "secrets.json",
    "token.json",
})
_SENSITIVE_OUTPUT_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
_SENSITIVE_OUTPUT_MARKERS = (
    "api_key",
    "apikey",
    "access_token",
    "bearer_token",
    "credential",
    "password",
    "refresh_token",
    "secret",
)
_OUTPUT_CALL_PATH_RE = r"(?:~?/[^'\"]+|[A-Za-z]:[\\/][^'\"]+|\\\\[^'\"]+)"
_OUTPUT_REDIRECT_PATH_RE = r"(?:~?/[^\s;|&'\"]+|[A-Za-z]:[\\/][^\s;|&'\"]+|\\\\[^\s;|&'\"]+)"
_EMBEDDED_OUTPUT_PATH_RE = re.compile(_OUTPUT_CALL_PATH_RE)
_USER_FILE_WRITE_CALL_RE = re.compile(
    rf"(?:write_text|write_bytes)\s*\(\s*['\"](?P<path>{_OUTPUT_CALL_PATH_RE})['\"]",
    re.I,
)
_USER_FILE_OPEN_WRITE_CALL_RE = re.compile(
    rf"open\s*\(\s*['\"](?P<path>{_OUTPUT_CALL_PATH_RE})['\"]\s*,\s*['\"][^'\"]*[wax+][^'\"]*['\"]",
    re.I,
)
_USER_FILE_REDIRECT_RE = re.compile(
    rf"(?:^|\s)(?:>|>>|1>|2>|&>)\s*(?:['\"](?P<quoted>{_OUTPUT_REDIRECT_PATH_RE})['\"]|(?P<bare>{_OUTPUT_REDIRECT_PATH_RE}))"
)

# Portable grep fix: GNU basic-regex "\|" fails on BSD grep in argv mode.
_GREP_TOOLS = frozenset(("grep", "egrep", "fgrep"))
_GREP_REGEX_MODE_FLAGS = frozenset((
    "-E", "--extended-regexp",
    "-P", "--perl-regexp",
    "-F", "--fixed-strings",
    "-G", "--basic-regexp",
))
_GREP_BACKSLASH_PIPE_PATTERN = re.compile(r'\\\|')
_NO_MATCH_EXIT_TOOLS = frozenset(("grep", "egrep", "fgrep", "rg", "ag", "ack"))


def _is_search_no_match(res: CompletedProcess) -> bool:
    tool = pathlib.Path(str(res.args[0] if res.args else "")).name.lower()
    return (
        int(res.returncode) == 1
        and tool in _NO_MATCH_EXIT_TOOLS
        and not str(res.stderr or "").strip()
    )


def _grep_has_explicit_regex_mode(cmd: List[str]) -> bool:
    """Return whether grep argv already chooses regex/string flavor."""
    if not cmd:
        return False
    tool = pathlib.Path(cmd[0]).name.lower()
    if tool in ("egrep", "fgrep"):
        return True
    for arg in cmd[1:]:
        if not isinstance(arg, str):
            continue
        if arg in _GREP_REGEX_MODE_FLAGS:
            return True
        if arg.startswith("--"):
            continue
        # Short options may be clustered, e.g. `grep -rnE pattern path`.
        if arg.startswith("-") and any(flag in arg[1:] for flag in ("E", "P", "F", "G")):
            return True
    return False


def _maybe_autocorrect_grep_backslash_pipe(cmd: List[str]) -> tuple[List[str], str]:
    if not cmd or pathlib.Path(cmd[0]).name.lower() not in _GREP_TOOLS:
        return cmd, ""
    if _grep_has_explicit_regex_mode(cmd):
        return cmd, ""
    corrected = list(cmd)
    changed_args: list[str] = []
    for idx, arg in enumerate(corrected[1:], start=1):
        if isinstance(arg, str) and _GREP_BACKSLASH_PIPE_PATTERN.search(arg):
            corrected[idx] = _GREP_BACKSLASH_PIPE_PATTERN.sub("|", arg)
            changed_args.append(arg)
    if not changed_args:
        return cmd, ""
    corrected.insert(1, "-E")
    return corrected, (
        "⚠️ SHELL_REGEX_AUTO_CORRECTED: converted grep backslash-escaped "
        "alternation (\\|) to extended regex mode (`grep -E`) and rewrote "
        f"{changed_args!r} to use `|`.\n"
    )


def _mentioned_user_file_outputs_without_declaration(ctx: ToolContext, cmd: List[str], outputs: List[str] | None) -> list[str]:
    """Best-effort audit for scripts that write absolute user_files without outputs."""

    if outputs:
        return []
    mentioned: list[str] = []
    for token in shell_argv_with_inline(cmd):
        token_text = str(token)
        token_lower = token_text.lower()
        redirect_paths = [
            match.group("quoted") or match.group("bare")
            for match in _USER_FILE_REDIRECT_RE.finditer(token_text)
        ]
        has_write_open = bool(_USER_FILE_OPEN_WRITE_CALL_RE.search(token_text))
        if not redirect_paths and not has_write_open and not any(marker in token_lower for marker in ("write_text", "write_bytes", ".write(", "writefile", "createwritestream")):
            continue
        candidates = EMBEDDED_ABSOLUTE_PATH_RE.findall(str(token))
        candidates.extend(_EMBEDDED_OUTPUT_PATH_RE.findall(str(token)))
        candidates.extend(match.group("path") for match in _USER_FILE_WRITE_CALL_RE.finditer(str(token)))
        candidates.extend(match.group("path") for match in _USER_FILE_OPEN_WRITE_CALL_RE.finditer(str(token)))
        candidates.extend(redirect_paths)
        for candidate in candidates:
            try:
                path = pathlib.Path(candidate).expanduser().resolve(strict=False)
            except Exception:
                continue
            try:
                user_root = resource_root_path(ctx, "user_files")
            except Exception:
                continue
            if not path_is_relative_to(path, user_root):
                continue
            if user_files_path_block_reason(ctx, path):
                continue
            path_text = str(path)
            if path_text in mentioned:
                continue
            mentioned.append(path_text)
    return mentioned


def _run_shell(ctx: ToolContext, cmd, cwd: str = "", outputs: List[str] | None = None) -> str:
    if isinstance(cmd, str):
        # Recover common stringified argv mistakes before failing.
        recovered = None
        try:
            parsed = json.loads(cmd)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                recovered = parsed
        except (json.JSONDecodeError, ValueError):
            pass
        if recovered is None:
            try:
                parsed = ast.literal_eval(cmd)
                if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                    recovered = parsed
            except (ValueError, SyntaxError):
                pass
        # Malformed structured literals are not shell commands; refuse explicitly.
        if recovered is None:
            stripped = cmd.lstrip()
            is_posix_test_cmd = stripped.startswith("[ ") and stripped.rstrip().endswith(" ]")
            if stripped[:1] in ("[", "{") and not is_posix_test_cmd:
                return (
                    '⚠️ SHELL_ARG_ERROR: `cmd` looks like a JSON/Python list literal '
                    'but failed to parse cleanly (likely an escape or quote-mismatch '
                    'issue). Pass cmd as an actual array, not a stringified array.\n\n'
                    'Correct usage:\n'
                    '  run_command(cmd=["git", "log", "--oneline", "-10"])\n\n'
                    'Wrong usage (the failure that brought you here):\n'
                    '  run_command(cmd=\'["git", "log", "--oneline", "-10"]\')\n\n'
                    'For reading files, prefer `read_file`.\n'
                    'For searching code, prefer `search_code`.'
                )
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
                '  run_command(cmd=["grep", "-r", "pattern", "path/"])\n'
                '  run_command(cmd=["python", "-c", "print(1+1)"])\n\n'
                'Wrong usage:\n'
                '  run_command(cmd="grep -r pattern path/")\n\n'
                'For reading files, prefer `read_file`.\n'
                'For searching code, prefer `search_code`.'
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
                    "run_command executes argv directly, so shell variables are not expanded. "
                    'Use ["sh", "-c", "..."] if you intentionally need shell expansion, '
                    "or read the environment variable inside the called program."
                )

    if cmd and cmd[0] in _SHELL_BUILTINS:
        if cmd[0] == "cd":
            return (
                '⚠️ SHELL_CMD_ERROR: "cd" is a shell builtin, not an executable. '
                'Use the "cwd" parameter instead: '
                'run_command(cmd=["git", "log"], cwd="/target/dir")'
            )
        return (
            f'⚠️ SHELL_CMD_ERROR: "{cmd[0]}" is a shell builtin and cannot '
            'be executed directly via subprocess. '
            'Use ["sh", "-c", "your command"] if you need shell builtins.'
        )

    cmd, autocorrect_note = _maybe_autocorrect_grep_backslash_pipe(cmd)

    found_ops = _SHELL_OPERATORS.intersection(cmd)
    if found_ops:
        op = sorted(found_ops)[0]
        return (
            f'⚠️ SHELL_CMD_ERROR: Shell operator "{op}" found in cmd array. '
            'Subprocess does not interpret shell syntax. '
            'Options: (1) Split into separate run_command calls. '
            '(2) For pipes/chaining: ["sh", "-c", "cmd1 && cmd2"]'
        )

    active_repo_dir = active_repo_dir_for(ctx)
    active_root = pathlib.Path(active_repo_dir).resolve(strict=False)
    try:
        work_dir, cwd_root, allowed_roots = resolve_shell_cwd(ctx, cwd)
    except (OSError, ValueError) as exc:
        try:
            _, _, allowed_roots = resolve_shell_cwd(ctx, "")
        except Exception:
            allowed_roots = [("active_workspace", active_root)]
        roots = ", ".join(f"{name}={pathlib.Path(root).resolve(strict=False)}" for name, root in allowed_roots)
        return (
            f"⚠️ SHELL_CWD_BLOCKED: cwd escapes allowed roots: {exc}. "
            f"allowed_roots: {roots}. For user-visible files use an absolute/~/ cwd "
            "under user_files, root=artifact_store, or root=task_drive."
        )
    if not work_dir.exists() or not work_dir.is_dir():
        roots = ", ".join(f"{name}={pathlib.Path(root).resolve(strict=False)}" for name, root in allowed_roots)
        return f"⚠️ SHELL_CWD_BLOCKED: cwd is not a directory: {cwd or work_dir}. allowed_roots: {roots}"
    repo_root = _resolve_git_root(pathlib.Path(work_dir))
    before_changed = _status_snapshot(repo_root)
    before_outputs = _snapshot_declared_outputs(ctx, outputs, pathlib.Path(work_dir), cwd_root=cwd_root)

    timeout_sec = _resolve_effective_timeout(_RUN_SHELL_DEFAULT_TIMEOUT_SEC)
    bootstrap_process_path()
    try:
        res = _tracked_subprocess_run(
            cmd, cwd=str(work_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout_sec,
        )
        if res.returncode != 0:
            if _is_search_no_match(res):
                return autocorrect_note + (
                    f"{_describe_returncode(res.returncode, cwd=work_dir)} (no matches)\n"
                    f"{_format_process_output(res.stdout or '', '')}"
                )
            return autocorrect_note + f"⚠️ SHELL_EXIT_ERROR: command exited with {_describe_returncode(res.returncode, cwd=work_dir)}.\n\n{_format_process_output(res.stdout or '', res.stderr or '')}"
        after_changed = _status_snapshot(repo_root)
        if after_changed != before_changed:
            _invalidate_advisory(
                ctx,
                changed_paths=after_changed or before_changed,
                mutation_root=repo_root,
                source_tool="run_command",
            )
        undeclared_user_outputs = _mentioned_user_file_outputs_without_declaration(ctx, cmd, outputs)
        if undeclared_user_outputs:
            return (
                autocorrect_note
                + "⚠️ ARTIFACT_OUTPUT_ERROR: command appears to write user_files outputs "
                "without declaring outputs=[...]. Declare generated user-visible files so "
                "they are copied into the task artifact store before claiming completion. "
                f"Paths: {', '.join(undeclared_user_outputs[:5])}.\n\n"
                + f"{_describe_returncode(0, cwd=work_dir)}\n"
                + _format_process_output(res.stdout or "", res.stderr or "")
            )
        artifact_note, artifact_failed = _register_process_outputs(
            ctx,
            outputs,
            pathlib.Path(work_dir),
            cwd_root=cwd_root,
            changed_paths=set(after_changed or []),
            before_outputs=before_outputs,
        )
        audit_note = ""
        if cwd_root == "user_files" and not outputs:
            audit_note = (
                "\n\n⚠️ ARTIFACT_AUDIT_GAP: command ran in user_files cwd without "
                "outputs=[...]. If it created a deliverable, rerun/register the file "
                "with outputs or write_file(root=artifact_store) before claiming it."
            )
        if artifact_failed:
            return (
                autocorrect_note
                + f"⚠️ ARTIFACT_OUTPUT_ERROR: command succeeded but declared output registration failed. "
                + f"{_describe_returncode(0, cwd=work_dir)}\n"
                + f"{_format_process_output(res.stdout or '', res.stderr or '')}"
                + artifact_note
            )
        return autocorrect_note + f"{_describe_returncode(0, cwd=work_dir)}\n{_format_process_output(res.stdout or '', res.stderr or '')}{artifact_note}{audit_note}"
    except subprocess.TimeoutExpired:
        return (
            f"⚠️ TOOL_TIMEOUT (run_command): command exceeded {timeout_sec}s. "
            f"Subprocess tree was terminated. cwd={work_dir}"
        )
    except Exception as e:
        return f"⚠️ SHELL_ERROR: {e}. cwd={work_dir}"


def _load_project_context(repo_dir: pathlib.Path) -> str:
    """Load governance docs for Claude Code system_prompt injection."""
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
                parts.append(f"## {label}\n\n{content}")
            except Exception:
                pass
    return "\n\n---\n\n".join(parts)


def _get_changed_files(repo_dir: pathlib.Path) -> list:
    """Return changed files after an edit."""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return [line[3:].strip() for line in res.stdout.splitlines() if len(line) > 3 and line.strip()]
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
    """Run basic post-edit validation."""
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


def _control_restore_note(restored: list[str]) -> str:
    if not restored:
        return ""
    return (
        "\n\n⚠️ SKILL_PAYLOAD_CONTROL_RESTORED: restored skill provenance/control-plane "
        "paths after claude_code_edit: "
        + ", ".join(sorted(set(restored)))
        + "."
    )


def _claude_code_edit(ctx: ToolContext, prompt: str, cwd: str = "", budget: float = 5.0, validate: bool = False, bucket: str = "", skill_name: str = "", outputs: List[str] | None = None) -> str:
    """Delegate SDK edits with cwd and protected-path safety hooks."""
    from ouroboros.tools.git import _acquire_git_lock, _release_git_lock

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "⚠️ CLAUDE_CODE_UNAVAILABLE: ANTHROPIC_API_KEY not set."

    active_root = active_repo_dir_for(ctx).resolve(strict=False)
    system_repo_root = pathlib.Path(ctx.repo_dir).resolve(strict=False)
    existing_tc = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    workspace_mode = str(getattr(ctx, "workspace_mode", "") or "").strip()
    workspace_task_mode = bool(workspace_mode and workspace_mode != "self")
    work_dir = str(active_root)
    work_dir_root = "active_workspace"
    skill_payload_root = None
    short_form_path_text = cwd if str(cwd or "").strip() else str(active_root)
    synth = None
    ignored_reason = ""
    if workspace_task_mode and not (existing_tc and existing_tc.mode == "skill_repair"):
        if str(bucket or "").strip() or str(skill_name or "").strip():
            return (
                "⚠️ CLAUDE_CODE_ERROR: skill payload short-form is unavailable in workspace mode. "
                "Use a workspace-relative cwd, or run a skill_repair task for data skill payload edits."
            )
    else:
        short_form = decide_payload_short_form(
            bucket=bucket,
            skill_name=skill_name,
            path_text=short_form_path_text,
            repo_dir=active_root,
            drive_root=pathlib.Path(ctx.drive_root),
        )
        if short_form.error:
            return f"⚠️ CLAUDE_CODE_ERROR: {short_form.error}"
        synth = short_form.constraint
        ignored_reason = short_form.ignored_reason
    redirect_err = cross_skill_redirect_error(existing_tc, synth)
    if redirect_err:
        return f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}"
    # Real skill_repair constraint wins; repair confinement is sticky.
    if existing_tc and existing_tc.mode == "skill_repair":
        task_constraint = existing_tc
    else:
        task_constraint = synth or existing_tc
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            resolved_skill_target = resolve_skill_payload_target(
                pathlib.Path(ctx.drive_root),
                cwd or ".",
                constraint=task_constraint,
                allow_short_relative=True,
            )
            work_dir = str(resolved_skill_target.target_path)
            work_dir_root = "skill_payload"
            skill_payload_root = resolved_skill_target.payload_root
        except (SkillPayloadPathError, ValueError) as e:
            return f"⚠️ CLAUDE_CODE_ERROR: {e}"
    elif cwd and cwd.strip() not in ("", ".", "./"):
        raw_cwd = cwd.strip()
        if workspace_task_mode:
            try:
                candidate, cwd_root, allowed_roots = resolve_shell_cwd(ctx, raw_cwd)
            except (OSError, ValueError) as e:
                try:
                    _, _, allowed_roots = resolve_shell_cwd(ctx, "")
                except Exception:
                    allowed_roots = []
                allowed_text = ", ".join(f"{name}={pathlib.Path(root).resolve(strict=False)}" for name, root in allowed_roots)
                return f"⚠️ CLAUDE_CODE_ERROR: cwd escapes allowed workspace edit roots. {e}. allowed_roots: {allowed_text}. Use the active workspace, task_drive, or artifact_store for workspace tasks."
            if cwd_root not in {"active_workspace", "task_drive", "artifact_store"}:
                return "⚠️ CLAUDE_CODE_ERROR: cwd root is unavailable for workspace task edits."
            work_dir_root = cwd_root
        else:
            try:
                resolved_skill_target = resolve_skill_payload_target(pathlib.Path(ctx.drive_root), raw_cwd)
                candidate = resolved_skill_target.target_path
                work_dir_root = "skill_payload"
                skill_payload_root = resolved_skill_target.payload_root
            except SkillPayloadPathError as exc:
                normalized_cwd = raw_cwd.replace("\\", "/").strip().lstrip("/")
                if normalized_cwd.startswith("data/skills/") or normalized_cwd.startswith("skills/"):
                    return f"⚠️ CLAUDE_CODE_ERROR: skill cwd is invalid: {exc}"
                try:
                    candidate, cwd_root, allowed_roots = resolve_shell_cwd(ctx, raw_cwd)
                    work_dir_root = cwd_root
                except (OSError, ValueError) as e:
                    try:
                        _, _, allowed_roots = resolve_shell_cwd(ctx, "")
                    except Exception:
                        allowed_roots = []
                    allowed_text = ", ".join(f"{name}={pathlib.Path(root).resolve(strict=False)}" for name, root in allowed_roots)
                    return f"⚠️ CLAUDE_CODE_ERROR: cwd escapes allowed edit roots. {e}. allowed_roots: {allowed_text}. Use an active repo/workspace cwd, an absolute/~/ user_files cwd, root=task_drive/artifact_store paths, or explicit data/skills/<bucket>/<skill>/... for skill payload edits."
        if not candidate.exists() or not candidate.is_dir():
            return f"⚠️ CLAUDE_CODE_ERROR: cwd not found or not a directory: {cwd}"
        work_dir = str(candidate)
    work_dir_path = pathlib.Path(work_dir).resolve()
    before_outputs = _snapshot_declared_outputs(ctx, outputs, work_dir_path, cwd_root=work_dir_root)
    skill_control_snapshots = {}
    sidecar_root = pathlib.Path(skill_payload_root).resolve() if skill_payload_root is not None else None
    if sidecar_root is not None:
        skill_control_snapshots = _snapshot_skill_control_paths(sidecar_root)

    target_repo_root = _resolve_git_root(work_dir_path)
    repo_mode = target_repo_root is not None
    if target_repo_root is None:
        target_repo_root = work_dir_path
    system_repo_mode = repo_mode and pathlib.Path(target_repo_root).resolve(strict=False) == system_repo_root
    runtime_mode = get_runtime_mode()
    if system_repo_mode and not mode_allows_protected_write(runtime_mode):
        protected_dirty_before = _protected_runtime_dirty_paths(target_repo_root)
        if protected_dirty_before:
            restored_sidecars = _restore_skill_control_changes(skill_control_snapshots) if skill_control_snapshots else []
            return (
                "⚠️ CORE_PROTECTION_BLOCKED: protected runtime files are already dirty; "
                "refusing claude_code_edit so existing human/operator changes are not overwritten. "
                "Resolve or commit them before delegating edits. Files: "
                + ", ".join(protected_dirty_before)
                + _control_restore_note(restored_sidecars)
            )
    before_changed = _status_snapshot(target_repo_root)
    invalidate_if_changed = lambda: (
        _invalidate_advisory(
            ctx,
            changed_paths=_status_snapshot(target_repo_root) or before_changed,
            mutation_root=target_repo_root,
            source_tool="claude_code_edit",
        )
        if repo_mode and _status_snapshot(target_repo_root) != before_changed
        else None
    )

    lock = _acquire_git_lock(ctx) if system_repo_mode else None
    try:
        if system_repo_mode:
            try:
                run_cmd(["git", "checkout", ctx.branch_dev], cwd=ctx.repo_dir)
            except Exception as e:
                restored_sidecars = _restore_skill_control_changes(skill_control_snapshots) if skill_control_snapshots else []
                return f"⚠️ GIT_ERROR (checkout): {e}" + _control_restore_note(restored_sidecars)

        ctx.emit_progress_fn("Delegating to Claude Agent SDK...")

        try:
            from ouroboros.gateways.claude_code import (
                DEFAULT_CLAUDE_CODE_MAX_TURNS,
                resolve_claude_code_model,
                run_edit,
            )
            model = resolve_claude_code_model()

            system_prompt = (
                f"STRICT: Only modify files inside {work_dir}. "
                f"Git branch: {ctx.branch_dev}. Do NOT commit or push.\n\n"
                + _load_project_context(system_repo_root)
            )
            write_path_blocker = None
            if work_dir_root == "user_files":
                write_path_blocker = lambda target: user_files_path_block_reason(ctx, pathlib.Path(target))
            elif work_dir_root == "artifact_store":
                write_path_blocker = lambda target: artifact_store_path_block_reason(pathlib.Path(target))

            result = run_edit(
                prompt=prompt,
                cwd=work_dir,
                model=model,
                max_turns=DEFAULT_CLAUDE_CODE_MAX_TURNS,
                budget=budget,
                system_prompt=system_prompt,
                repo_root=str(target_repo_root if repo_mode else work_dir_path),
                protect_runtime_paths=system_repo_mode,
                write_path_blocker=write_path_blocker,
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
                restored_sidecars = _restore_skill_control_changes(skill_control_snapshots) if skill_control_snapshots else []
                invalidate_if_changed()
                return (
                    f"⚠️ CLAUDE_CODE_ERROR: {result.error}\n\n{result.result_text}"
                    + _control_restore_note(restored_sidecars)
                )

            restored_sidecars = _restore_skill_control_changes(skill_control_snapshots) if skill_control_snapshots else []
            if restored_sidecars:
                invalidate_if_changed()
                return (
                    "⚠️ SKILL_PAYLOAD_CONTROL_BLOCKED: claude_code_edit attempted to modify "
                    "skill provenance/control-plane paths: "
                    + ", ".join(sorted(set(restored_sidecars)))
                    + ". Created control paths and sidecar changes were reverted where possible; edit payload code files instead."
                )

            if system_repo_mode and not mode_allows_protected_write(runtime_mode):
                protected_dirty_after = _protected_runtime_dirty_paths(target_repo_root)
                if protected_dirty_after:
                    restored = _restore_protected_runtime_paths(target_repo_root, protected_dirty_after)
                    invalidate_if_changed()
                    return (
                        "⚠️ CORE_PROTECTION_BLOCKED: claude_code_edit attempted to modify "
                        "protected Ouroboros runtime files in non-pro mode. Reverted: "
                        + ", ".join(restored or protected_dirty_after)
                        + ". Switch to pro mode only after an explicit reviewed plan."
                    )

            after_changed = _status_snapshot(target_repo_root)
            if repo_mode and after_changed != before_changed:
                _invalidate_advisory(
                    ctx,
                    changed_paths=result.changed_files or after_changed or before_changed,
                    mutation_root=target_repo_root,
                    source_tool="claude_code_edit",
                )

            output = result.to_tool_output()
            artifact_note, artifact_failed = _register_process_outputs(
                ctx,
                outputs,
                work_dir_path,
                cwd_root=work_dir_root,
                changed_paths=set(after_changed or []),
                before_outputs=before_outputs,
            )
            if artifact_failed:
                return (
                    "⚠️ ARTIFACT_OUTPUT_ERROR: claude_code_edit succeeded but declared "
                    "output registration failed.\n\n"
                    f"{output}"
                    f"{artifact_note}"
                    + _control_restore_note(restored_sidecars)
                )
            if artifact_note:
                output += artifact_note
            elif work_dir_root == "user_files" and not outputs:
                output += (
                    "\n\n⚠️ ARTIFACT_AUDIT_GAP: claude_code_edit ran in user_files cwd "
                    "without outputs=[...]. If it created a deliverable, register it "
                    "with outputs or write_file(root=artifact_store) before claiming it."
                )
            if system_repo_mode and mode_allows_protected_write(runtime_mode):
                protected_written = protected_paths_in(result.changed_files or after_changed)
                if protected_written:
                    output += "\n\n" + core_patch_notice(protected_written)
            if ignored_reason:
                output += f"\n\n⚠️ SKILL_SHORT_FORM_IGNORED: {ignored_reason}."
            return output

        except ImportError:
            restored_sidecars = _restore_skill_control_changes(skill_control_snapshots) if skill_control_snapshots else []
            return (
                "⚠️ CLAUDE_CODE_UNAVAILABLE: claude-agent-sdk not installed. "
                "Install: pip install 'ouroboros[claude-sdk]'"
                + _control_restore_note(restored_sidecars)
            )
        except Exception as e:
            restored_sidecars = _restore_skill_control_changes(skill_control_snapshots) if skill_control_snapshots else []
            invalidate_if_changed()
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
                + _control_restore_note(restored_sidecars)
            )

    finally:
        if lock is not None:
            _release_git_lock(lock)


def _run_script(
    ctx: ToolContext,
    script: str,
    interpreter: str = "python3",
    args: List[str] | None = None,
    cwd: str = "",
    outputs: List[str] | None = None,
) -> str:
    """Write a task-scoped temporary script and run it as a foreground command."""
    interp = str(interpreter or "python3").strip()
    allowed = {"python", "python3", "bash", "sh", "node", "ruby"}
    if pathlib.PurePath(interp).name not in allowed:
        return f"⚠️ RUN_SCRIPT_BLOCKED: interpreter must be one of {sorted(allowed)}."
    body = str(script or "")
    if not body.strip():
        return "⚠️ TOOL_ARG_ERROR (run_script): script is required."
    undeclared_user_outputs = _mentioned_user_file_outputs_without_declaration(ctx, [interp, "-c", body], outputs)
    if undeclared_user_outputs:
        return (
            "⚠️ ARTIFACT_OUTPUT_ERROR: run_script would write user_files without declaring outputs: "
            + ", ".join(undeclared_user_outputs)
            + ". Re-run with outputs=[...] or write the canonical deliverable via root=artifact_store."
        )
    try:
        root = pathlib.Path(ctx.task_drive_root()) / "tmp_scripts"
    except Exception:
        root = pathlib.Path(ctx.drive_root) / "tmp_scripts"
    root.mkdir(parents=True, exist_ok=True)
    suffix = ".py" if "python" in pathlib.PurePath(interp).name else ".sh"
    script_path = root / f"script_{uuid.uuid4().hex}{suffix}"
    script_path.write_text(body, encoding="utf-8")
    try:
        os.chmod(script_path, 0o600)
    except OSError:
        pass
    argv = [interp, str(script_path), *[str(item) for item in (args or [])]]
    effective_cwd = str(cwd or "")
    if (
        not effective_cwd.strip()
        and get_runtime_mode() == "light"
        and not bool(getattr(ctx, "is_workspace_mode", lambda: False)())
    ):
        effective_cwd = str(pathlib.Path(ctx.task_drive_root()).resolve(strict=False))
    result = _run_shell(ctx, argv, cwd=effective_cwd, outputs=outputs)
    if str(result).lstrip().startswith("⚠️"):
        return f"{result}\n# script_path={script_path}"
    return f"# script_path={script_path}\n{result}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("run_command", {
            "name": "run_command",
            "description": (
                "Run a foreground bounded command in an allowed resource-root cwd. Returns stdout+stderr. "
                "Every result header echoes the resolved cwd. "
                "cmd MUST be an array of strings, never a single shell-style "
                "string. Use cwd= for working directory; cd is rejected. "
                "For pipes/chaining use [\"sh\", \"-c\", \"cmd1 && cmd2\"]."
            ),
            "parameters": {"type": "object", "properties": {
                "cmd": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Argv as a JSON array of strings. Example: "
                        "[\"git\", \"log\", \"--oneline\", \"-10\"]. NEVER "
                        "pass a single string like \"git log\" or a "
                        "stringified array like '[\"git\", \"log\"]'."
                    ),
                },
	                "cwd": {
	                    "type": "string", "default": "",
	                    "description": (
	                        "Working directory. Relative paths resolve under allowed task/workspace roots; "
	                        "absolute or ~ paths under user_files are allowed for external user deliverables. "
	                        "Use "
	                        "this instead of `cd` (which is a shell builtin "
	                        "and is rejected)."
	                    ),
	                },
	                "outputs": {
	                    "type": "array",
	                    "items": {"type": "string"},
	                    "default": [],
	                    "description": "Generated file paths to copy/register into the task artifact store after success.",
	                },
	            }, "required": ["cmd"]},
        }, _run_shell, is_code_tool=True, timeout_sec=_RUN_SHELL_DEFAULT_TIMEOUT_SEC),
        ToolEntry("claude_code_edit", {
            "name": "claude_code_edit",
            "description": (
                "Delegate a bounded code-editing task to the Claude Agent SDK. "
                "Use this as the strongest coding helper for substantial edits. "
                "It may edit files under cwd, never commits or pushes, and still "
                "runs through Ouroboros runtime-mode and review protections."
            ),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string", "description": "Precise coding task and constraints."},
	                "cwd": {
	                    "type": "string",
	                    "default": "",
	                    "description": (
	                        "Working directory under the active repo/workspace, task_drive, artifact_store, "
	                        "an external absolute/~/ user_files path, or an explicit "
	                        "data/skills/<bucket>/<skill> payload path for skill repair."
	                    ),
	                },
                "budget": {"type": "number", "default": 5.0},
                "validate": {"type": "boolean", "default": False},
                "bucket": {"type": "string", "default": ""},
                "skill_name": {"type": "string", "default": ""},
                "outputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                    "description": "Generated file paths to copy/register into the task artifact store after a successful edit.",
                },
            }, "required": ["prompt"]},
        }, _claude_code_edit, is_code_tool=True, timeout_sec=1200),
        ToolEntry("run_script", {
            "name": "run_script",
            "description": (
                "Run a short task-scoped temporary script with a declared interpreter. "
                "Use for multi-line diagnostics or harness helpers; generated script files live under the task drive. "
                "The underlying command result echoes the resolved cwd."
            ),
            "parameters": {"type": "object", "properties": {
                "script": {"type": "string"},
	                "interpreter": {"type": "string", "enum": ["python", "python3", "bash", "sh", "node", "ruby"], "default": "python3"},
	                "args": {"type": "array", "items": {"type": "string"}, "default": []},
	                "cwd": {"type": "string", "default": ""},
	                "outputs": {
	                    "type": "array",
	                    "items": {"type": "string"},
	                    "default": [],
	                    "description": "Generated file paths to copy/register into the task artifact store after success.",
	                },
	            }, "required": ["script"]},
        }, _run_script, is_code_tool=True, timeout_sec=_RUN_SHELL_DEFAULT_TIMEOUT_SEC),
    ]

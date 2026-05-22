"""Tool registry SSOT: load tool modules, expose schemas, execute safely."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ouroboros.runtime_mode_policy import (
    FROZEN_CONTRACT_PATH_PREFIXES,
    PROTECTED_RUNTIME_PATHS,
    core_patch_notice,
    is_protected_runtime_path,
    mode_allows_protected_write,
    protected_paths_in,
    protected_write_block_message,
)
from ouroboros.tool_capabilities import CORE_TOOL_NAMES
from ouroboros.utils import safe_relpath
from ouroboros.contracts.task_constraint import TaskConstraint, normalize_task_constraint, resolve_payload_path
from ouroboros.contracts.skill_payload_policy import (
    SKILL_OWNER_STATE_FILENAMES,
    SKILL_OWNER_STATE_STEMS,
    SKILL_PAYLOAD_CONTROL_DIRNAMES,
    SKILL_PAYLOAD_CONTROL_FILENAMES,
    cross_skill_redirect_error,
    decide_payload_short_form,
    is_skill_payload_control_filename,
    is_skill_payload_path,
)

log = logging.getLogger(__name__)

_PROTECTED_RUNTIME_PATHS_LOWER = frozenset(
    p.lower() for p in PROTECTED_RUNTIME_PATHS
) | frozenset(prefix.lower() for prefix in FROZEN_CONTRACT_PATH_PREFIXES)

_SHELL_WRITE_INDICATORS = (
    "rm ", "rm\t", ">", "sed -i", "tee ", "truncate",
    "mv ", "cp ", "chmod ", "chown ", "unlink ", "delete", "trash",
    "rsync ", "write_text", "open(", ".write(", ".writelines(",
    "os.remove(", "os.unlink(", "os.mkdir(", "os.makedirs(", "sort -o",
)

_LIGHT_SHELL_WRITER_COMMANDS = frozenset({
    "chmod", "chown", "cp", "gunzip", "gzip", "ln", "mkdir", "mv",
    "perl", "rm", "ruby", "sed", "sort", "tar", "touch", "truncate", "uniq", "unzip",
})


def _coerce_real_path(value: Any) -> pathlib.Path | None:
    if value is None or value.__class__.__module__.startswith("unittest.mock"):
        return None
    try:
        return pathlib.Path(os.fspath(value))
    except TypeError:
        return None


def active_repo_dir_for(ctx: Any) -> pathlib.Path:
    """Return the active repo/workspace root for real and lightweight test contexts."""
    active = getattr(ctx, "active_repo_dir", None)
    if callable(active):
        try:
            candidate = active()
        except Exception:
            candidate = None
        path = _coerce_real_path(candidate)
        if path is not None:
            return path

    workspace_root = getattr(ctx, "workspace_root", None)
    workspace_path = _coerce_real_path(workspace_root)
    if workspace_path is not None:
        workspace_mode = str(getattr(ctx, "workspace_mode", "") or "").strip()
        if workspace_mode and workspace_mode != "self":
            return workspace_path

    return pathlib.Path(getattr(ctx, "repo_dir"))


def _detect_runtime_mode_elevation(text_lower: str) -> bool:
    """Detect shell/script attempts to change ``OUROBOROS_RUNTIME_MODE``."""
    has_save = "save_settings" in text_lower
    has_mode_key = "ouroboros_runtime_mode" in text_lower
    has_dotted_path = "ouroboros.config.save_settings" in text_lower
    return (has_save and has_mode_key) or has_dotted_path


def _shell_argv(raw_cmd: Any) -> List[str]:
    if isinstance(raw_cmd, list):
        return [str(x) for x in raw_cmd if str(x).strip()]
    try:
        return [str(x) for x in shlex.split(str(raw_cmd or "")) if str(x).strip()]
    except ValueError:
        return [str(x) for x in str(raw_cmd or "").split() if str(x).strip()]


def _unwrap_env_argv(argv: List[str]) -> List[str]:
    if not argv or pathlib.PurePath(argv[0]).name.lower() != "env":
        return argv
    idx = 1
    options_with_arg = {"-u", "--unset", "-C", "--chdir", "--argv0"}
    while idx < len(argv):
        token = argv[idx]
        if token == "--":
            idx += 1
            break
        if token == "-S" and idx + 1 < len(argv):
            return _shell_argv(argv[idx + 1])
        if token.startswith("--split-string="):
            return _shell_argv(token.split("=", 1)[1])
        if token in options_with_arg:
            idx += 2; continue
        if (
            any(token.startswith(prefix + "=") for prefix in ("--unset", "--chdir", "--argv0"))
            or token.startswith("-")
            or ("=" in token and not token.startswith("="))
        ):
            idx += 1; continue
        break
    return argv[idx:] if idx < len(argv) else []


def _strip_leading_env_assignments(argv: List[str]) -> List[str]:
    idx = 0
    while idx < len(argv) and "=" in argv[idx] and not argv[idx].startswith("="):
        idx += 1
    return argv[idx:]


def _shell_command_string(argv: List[str]) -> str:
    for idx, arg in enumerate(argv[1:], start=1):
        if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
            return argv[idx + 1] if idx + 1 < len(argv) else ""
    return ""


def _candidate_path_inside(root: pathlib.Path, work_dir: pathlib.Path, path_text: str) -> bool:
    """Return whether ``path_text`` resolves inside ``root``."""
    text = str(path_text or "").strip()
    if not text or text in {"-", "--"}:
        return False
    # Skip obvious non-path fragments and shell/control tokens.
    if text.startswith(("-", "$")) or text in {"|", "&&", "||", ";", ">", ">>"}:
        return False
    try:
        root_resolved = pathlib.Path(root).resolve()
        base = pathlib.Path(text)
        if not base.is_absolute():
            base = work_dir / base
        candidate = base.expanduser().resolve(strict=False)
        candidate.relative_to(root_resolved)
        return True
    except (OSError, ValueError):
        return False


def _repo_target_mentioned(argv: List[str], *, repo_dir: pathlib.Path, cwd: str = "") -> bool:
    work_dir = pathlib.Path(repo_dir)
    if cwd and str(cwd).strip() not in ("", ".", "./"):
        try:
            candidate = (pathlib.Path(repo_dir) / str(cwd)).resolve(strict=False)
            work_dir = candidate
        except OSError:
            pass
    return any(
        _candidate_path_inside(pathlib.Path(repo_dir), work_dir, token)
        for token in argv[1:]
    )


def _writer_target_tokens(argv: List[str]) -> List[str]:
    if not argv:
        return []
    cmd = pathlib.PurePath(argv[0]).name.lower()
    operands = [arg for arg in argv[1:] if arg and not arg.startswith("-")]
    if cmd == "cp":
        return operands[-1:] if len(operands) >= 2 else []
    if cmd in {"chmod", "chown"}:
        return operands[1:] if len(operands) >= 2 else []
    if cmd == "sed":
        return operands[1:] if len(operands) >= 2 else operands
    if cmd == "sort":
        for idx, arg in enumerate(argv[1:], start=1):
            if arg == "-o" and idx + 1 < len(argv):
                return [argv[idx + 1]]
            if arg.startswith("--output="):
                return [arg.split("=", 1)[1]]
        return []
    if cmd == "uniq":
        return operands[1:2] if len(operands) >= 2 else []
    return operands


def _writer_targets_repo(argv: List[str], *, repo_dir: pathlib.Path, cwd: str = "") -> bool:
    return _repo_target_mentioned([argv[0], *_writer_target_tokens(argv)], repo_dir=repo_dir, cwd=cwd)


def _shell_writer_targets_protected(raw_cmd: Any) -> bool:
    argv = _strip_leading_env_assignments(_unwrap_env_argv(_shell_argv(raw_cmd)))
    if not argv:
        return False
    executable = pathlib.PurePath(argv[0]).name.lower()
    if executable in {"bash", "sh", "zsh"}:
        inline = _shell_command_string(argv)
        return bool(inline and _shell_writer_targets_protected(inline))
    if executable not in _LIGHT_SHELL_WRITER_COMMANDS:
        return False
    target_text = " ".join(_writer_target_tokens(argv)).replace("\\", "/").lower()
    return bool(target_text and any(cf in target_text for cf in _PROTECTED_RUNTIME_PATHS_LOWER))


def _light_shell_repo_mutation(raw_cmd: Any, *, repo_dir: pathlib.Path, cwd: str = "") -> bool:
    """Detect simple shell writer commands that target the repo in light mode."""
    argv = _shell_argv(raw_cmd)
    if not argv:
        return False
    cmd_lower = " ".join(argv).lower()

    unwrapped = _unwrap_env_argv(argv)
    if unwrapped != argv:
        return _light_shell_repo_mutation(unwrapped, repo_dir=repo_dir, cwd=cwd)
    argv = _strip_leading_env_assignments(argv)
    if not argv:
        return False
    executable = pathlib.PurePath(argv[0]).name.lower()

    if executable in {"bash", "sh", "zsh"}:
        inline = _shell_command_string(argv)
        if inline:
            return _light_shell_repo_mutation(inline, repo_dir=repo_dir, cwd=cwd)

    if executable in _LIGHT_SHELL_WRITER_COMMANDS and _writer_targets_repo(argv, repo_dir=repo_dir, cwd=cwd):
        return True

    # Redirection/tee only count when the same argv mentions a repo path.
    if any(ind in cmd_lower for ind in (" > ", " >> ", " | tee ")):
        return _repo_target_mentioned(argv, repo_dir=repo_dir, cwd=cwd)

    return False



def _task_constraint_path_allowed(path_text: str, constraint: Optional[TaskConstraint], drive_root: pathlib.Path) -> bool:
    return is_skill_payload_path(
        drive_root,
        path_text or "",
        constraint=constraint,
        allow_short_relative=True,
        allow_control_plane=True,
    )


_HEAL_MODE_ALLOWED_TOOLS = frozenset({
    "claude_code_edit",
    "data_read",
    "data_list",
    "data_write",
    "list_skills",
    "review_skill",
    "str_replace_editor",
    # Read-only payload syntax validator; no review/enabled/grant mutation.
    "skill_preflight",
})

_HEAL_PROTECTED_PAYLOAD_FILENAMES = SKILL_PAYLOAD_CONTROL_FILENAMES


_SKILL_OWNER_STATE_STEMS = SKILL_OWNER_STATE_STEMS
_DETACHED_PROCESS_MARKERS = (
    "start_new_session",
    "new_session",
    "setsid",
    "preexec_fn",
    "nohup",
)


def _mentions_skill_owner_state(text_lower: str) -> bool:
    if "state" not in text_lower or "skills" not in text_lower:
        return False
    for stem in _SKILL_OWNER_STATE_STEMS:
        if f"{stem}.json" in text_lower:
            return True
        if stem in text_lower and ".json" in text_lower:
            return True
    return False


def _mentions_detached_process(text_lower: str) -> bool:
    return any(marker in text_lower for marker in _DETACHED_PROCESS_MARKERS)


def _heal_protected_payload_sidecar(path_text: str) -> bool:
    return is_skill_payload_control_filename(path_text)


def _skill_payload_cwd_allowed(cwd_text: str, drive_root: pathlib.Path) -> bool:
    return is_skill_payload_path(drive_root, cwd_text, allow_control_plane=False)


# Git via run_shell: only truly read-only subcommands allowed.
_GIT_READONLY_SUBCOMMANDS = frozenset([
    "status", "diff", "log", "show", "ls-files",
    "describe", "rev-parse", "cat-file",
    "shortlog", "version", "help", "blame",
    "grep", "reflog",
])

_WORKSPACE_ALLOWED_TOOLS = frozenset({
    "repo_read",
    "repo_list",
    "repo_write",
    "str_replace_editor",
    "code_search",
    "codebase_digest",
    "run_shell",
    "git_status",
    "git_diff",
    "data_read",
    "data_list",
    "data_write",
    "chat_history",
    "recent_tasks",
    "web_search",
    "browse_page",
    "browser_action",
    "analyze_screenshot",
    "list_available_tools",
    "enable_tools",
})

def _parse_porcelain_paths(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in str(output or "").splitlines():
        # Porcelain v1 has two status columns; keep leading status spaces.
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if " -> " in path_text:
            old_path, new_path = path_text.rsplit(" -> ", 1)
            paths.extend([old_path.strip(), new_path.strip()])
        else:
            paths.append(path_text)
    return sorted({p for p in paths if p})


def _light_repo_snapshot(repo_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    """Worktree tripwire for light-mode shell writes, not rollback machinery."""
    try:
        repo = pathlib.Path(repo_dir)
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        if status.returncode != 0:
            return None
        unstaged = subprocess.run(
            ["git", "diff", "--binary", "--no-ext-diff"],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
        staged = subprocess.run(
            ["git", "diff", "--cached", "--binary", "--no-ext-diff"],
            cwd=str(repo), capture_output=True, text=True, timeout=10,
        )
        paths = _parse_porcelain_paths(status.stdout)
        digest = hashlib.sha256()
        digest.update((status.stdout or "").encode("utf-8", errors="replace"))
        digest.update((unstaged.stdout if unstaged.returncode == 0 else "").encode("utf-8", errors="replace"))
        digest.update((staged.stdout if staged.returncode == 0 else "").encode("utf-8", errors="replace"))
        for rel in paths:
            try:
                target = (repo / safe_relpath(rel)).resolve(strict=False)
                target.relative_to(repo.resolve(strict=False))
                if target.is_file() and rel in (status.stdout or ""):
                    stat = target.stat()
                    digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}".encode("utf-8"))
            except Exception:
                continue
        return {"digest": digest.hexdigest(), "paths": paths}
    except Exception:
        return None


def _format_light_repo_write_block(before: Dict[str, Any], after: Dict[str, Any], result: str) -> str:
    before_paths = set(before.get("paths") or [])
    after_paths = set(after.get("paths") or [])
    touched = sorted(after_paths | before_paths)
    listed = ", ".join(touched[:30]) if touched else "(status changed; no paths parsed)"
    if len(touched) > 30:
        listed += f", ... (+{len(touched) - 30} more)"
    return (
        "⚠️ LIGHT_MODE_REPO_WRITE_BLOCKED: runtime_mode=light detected "
        "a mutation of the Ouroboros repository after run_shell. "
        "The command result is blocked and no automatic rollback was attempted "
        "to avoid overwriting concurrent human edits. "
        f"Affected/dirty paths: {listed}. Switch to advanced/pro for repo writes.\n\n"
        "Original command output:\n"
        f"{result}"
    )


def _git_ref_snapshot(repo_dir: pathlib.Path) -> Optional[Dict[str, str]]:
    try:
        repo = pathlib.Path(repo_dir)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        refs = subprocess.run(
            ["git", "show-ref", "--head", "--dereference"],
            cwd=str(repo), capture_output=True, text=True, timeout=5,
        )
        if head.returncode != 0 or refs.returncode not in (0, 1):
            return None
        digest = hashlib.sha256()
        digest.update((head.stdout or "").encode("utf-8", errors="replace"))
        digest.update((refs.stdout or "").encode("utf-8", errors="replace"))
        return {"head": (head.stdout or "").strip(), "digest": digest.hexdigest()}
    except Exception:
        return None


def _revert_protected_files(repo_dir, *, runtime_mode: str = "advanced") -> list:
    """Revert protected files after claude_code_edit unless pro mode is active."""
    if mode_allows_protected_write(runtime_mode):
        return []
    try:
        unstaged_diff = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        staged_diff = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if unstaged_diff.returncode != 0 and staged_diff.returncode != 0:
            return []
        modified = set()
        if unstaged_diff.returncode == 0:
            modified.update(unstaged_diff.stdout.strip().splitlines())
        if staged_diff.returncode == 0:
            modified.update(staged_diff.stdout.strip().splitlines())
        reverted = []
        for rel in sorted(modified):
            if is_protected_runtime_path(rel):
                subprocess.run(
                    ["git", "reset", "HEAD", "--", rel],
                    cwd=str(repo_dir), capture_output=True, timeout=5,
                )
                subprocess.run(
                    ["git", "checkout", "--", rel],
                    cwd=str(repo_dir), capture_output=True, timeout=5,
                )
                reverted.append(rel)
        return reverted
    except Exception:
        return []


def _extract_git_subcommand(cmd_parts: list) -> str:
    """Extract the git subcommand after global git options."""
    if not cmd_parts:
        return ""
    parts = _strip_leading_env_assignments([str(p) for p in cmd_parts])
    if not parts or pathlib.PurePath(parts[0]).name.lower() != "git":
        return ""
    i = 1
    while i < len(parts):
        p = parts[i]
        if p.startswith("-"):
            if p in ("-C", "-c", "--git-dir", "--work-tree"):
                i += 2
            else:
                i += 1
        else:
            return p
    return ""


def _extract_run_shell_git_subcommand(raw_cmd: Any) -> str:
    parts = _strip_leading_env_assignments(_unwrap_env_argv(_shell_argv(raw_cmd)))
    if not parts:
        return ""
    first = pathlib.PurePath(parts[0]).name.lower()
    if first == "git":
        return _extract_git_subcommand(parts)
    if first in {"bash", "sh", "zsh"}:
        inline = _shell_command_string(parts)
        if inline:
            return _extract_run_shell_git_subcommand(inline)
    return ""


def _workspace_git_safety_violation(raw_cmd: Any, *, active_root: pathlib.Path, cwd: str = "") -> str:
    root = pathlib.Path(active_root).resolve(strict=False)
    base = root
    if cwd and str(cwd).strip() not in ("", ".", "./"):
        try:
            base = (root / safe_relpath(str(cwd))).resolve(strict=False)
            base.relative_to(root)
        except Exception:
            base = root
    argv = _strip_leading_env_assignments(_unwrap_env_argv(_shell_argv(raw_cmd)))
    if not argv:
        return ""
    first = pathlib.PurePath(argv[0]).name.lower()
    if first in {"bash", "sh", "zsh"}:
        inline = _shell_command_string(argv)
        return _workspace_git_safety_violation(inline, active_root=root, cwd=str(base.relative_to(root)) if inline else "") if inline else ""
    for idx, token in enumerate(argv):
        if pathlib.PurePath(str(token)).name.lower() != "git":
            continue
        parts = argv[idx:]
        subcmd = ""
        j = 1
        while j < len(parts):
            part = parts[j]
            if part in {"-C", "--git-dir", "--work-tree"} and j + 1 < len(parts):
                try:
                    target = pathlib.Path(parts[j + 1])
                    if not target.is_absolute():
                        target = base / target
                    target.resolve(strict=False).relative_to(root)
                except Exception:
                    return f"git {part} escapes the active workspace"
                j += 2
                continue
            if part.startswith("--git-dir=") or part.startswith("--work-tree="):
                value = part.split("=", 1)[1]
                try:
                    target = pathlib.Path(value)
                    if not target.is_absolute():
                        target = base / target
                    target.resolve(strict=False).relative_to(root)
                except Exception:
                    return "git root selector escapes the active workspace"
                j += 1
                continue
            if part == "-c":
                j += 2
                continue
            if part.startswith("-"):
                j += 1
                continue
            subcmd = part
            break
        if subcmd and subcmd.lower() not in _GIT_READONLY_SUBCOMMANDS:
            return f"git {subcmd}"
    return ""


@dataclass
class BrowserState:
    """Per-task Playwright lifecycle state."""

    pw_instance: Any = None
    browser: Any = None
    page: Any = None
    last_screenshot_b64: Optional[str] = None


@dataclass
class ToolContext:
    """Tool execution context passed from the agent."""

    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"
    system_repo_dir: Optional[pathlib.Path] = None
    workspace_root: Optional[pathlib.Path] = None
    workspace_mode: str = ""
    memory_mode: str = ""
    task_metadata: Dict[str, Any] = field(default_factory=dict)
    pending_events: List[Dict[str, Any]] = field(default_factory=list)
    current_chat_id: Optional[int] = None
    current_task_type: Optional[str] = None
    pending_restart_reason: Optional[str] = None
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # LLM-driven model/effort switch.
    active_model_override: Optional[str] = None
    active_effort_override: Optional[str] = None
    active_use_local_override: Optional[bool] = None

    # Per-task browser state.
    browser_state: BrowserState = field(default_factory=BrowserState)

    # Budget tracking for usage events.
    event_queue: Optional[Any] = None
    task_id: Optional[str] = None

    # Conversation messages for safety checks.
    messages: Optional[List[Dict[str, Any]]] = None

    # Structured task constraints, e.g. skill repair payload confinement.
    task_constraint: Optional[TaskConstraint] = None

    # Task depth for fork-bomb protection.
    task_depth: int = 0

    # True inside handle_chat_direct, not a queued worker task.
    is_direct_chat: bool = False

    # Pre-commit review state.
    _review_advisory: List[Any] = field(default_factory=list)
    _review_iteration_count: int = 0
    _review_history: list = field(default_factory=list)

    def active_repo_dir(self) -> pathlib.Path:
        if self.workspace_root is not None and str(self.workspace_mode or "").strip():
            return pathlib.Path(self.workspace_root)
        return pathlib.Path(self.repo_dir)

    def is_workspace_mode(self) -> bool:
        return self.workspace_root is not None and bool(str(self.workspace_mode or "").strip())

    def repo_path(self, rel: str) -> pathlib.Path:
        root = self.active_repo_dir()
        resolved = (root / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(root.resolve())
        except ValueError:
            raise ValueError(f"Path escapes repo_dir boundary: {rel}")
        return resolved

    def drive_path(self, rel: str) -> pathlib.Path:
        resolved = (self.drive_root / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(self.drive_root.resolve())
        except ValueError:
            raise ValueError(f"Path escapes drive_root boundary: {rel}")
        return resolved

    def drive_logs(self) -> pathlib.Path:
        return (self.drive_root / "logs").resolve()


@dataclass
class ToolEntry:
    """Single tool descriptor."""

    name: str
    schema: Dict[str, Any]
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 360


class ToolRegistry:
    """Tool registry; modules export ``get_tools()``."""

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self._entries: Dict[str, ToolEntry] = {}
        self._ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
        self._load_modules()

    _FROZEN_TOOL_MODULES = [
        "browser", "ci", "claude_advisory_review", "compact_context", "control",
        "core", "evolution_stats", "git", "git_rollback", "github", "health",
        "knowledge", "memory_tools", "plan_review", "recent_tasks", "review", "search", "shell",
        # External skill surface.
        "skill_exec",
        "skill_publish",
        # Read-only payload validator for heal mode.
        "skill_preflight",
        "tool_discovery", "vision",
    ]

    def _load_modules(self) -> None:
        """Load frozen or package-discovered tool modules."""
        import importlib
        import logging
        import sys

        if getattr(sys, 'frozen', False):
            module_names = self._FROZEN_TOOL_MODULES
        else:
            import pkgutil
            import ouroboros.tools as tools_pkg
            module_names = [
                m for _, m, _ in pkgutil.iter_modules(tools_pkg.__path__)
                if not m.startswith("_") and m != "registry"
            ]

        for modname in module_names:
            try:
                mod = importlib.import_module(f"ouroboros.tools.{modname}")
                if hasattr(mod, "get_tools"):
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to load tool module %s", modname, exc_info=True)

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def register(self, entry: ToolEntry) -> None:
        """Register a new tool entry."""
        self._entries[entry.name] = entry

    # Contract.

    def available_tools(self) -> List[str]:
        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        return [
            e.name
            for e in self._entries.values()
            if not workspace_mode or e.name in _WORKSPACE_ALLOWED_TOOLS
        ]

    def _schema_for_entry(self, entry: ToolEntry) -> Dict[str, Any]:
        return {"type": "function", "function": entry.schema}

    def _schemas_for_entry(self, entry: ToolEntry) -> List[Dict[str, Any]]:
        return [self._schema_for_entry(entry)]

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        built_in = [
            schema
            for entry in self._entries.values()
            if not workspace_mode or entry.name in _WORKSPACE_ALLOWED_TOOLS
            for schema in self._schemas_for_entry(entry)
        ]
        # Include live extension tool schemas in normal tool discovery.
        try:
            from ouroboros.extension_loader import (
                _tools as _ext_tools,
                _lock as _ext_lock,
                is_extension_live as _ext_is_live,
            )
            with _ext_lock:
                extension_schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("schema", {"type": "object", "properties": {}}),
                        },
                    }
                    for tool in _ext_tools.values()
                    if _ext_is_live(str(tool.get("skill") or ""), pathlib.Path(self._ctx.drive_root))
                ]
            if workspace_mode:
                extension_schemas = []
        except Exception:
            extension_schemas = []

        if not core_only:
            try:
                from ouroboros.mcp_client import (
                    ensure_configured_from_settings as _mcp_ensure_configured,
                    get_manager as _mcp_get_manager,
                )
                _mcp_ensure_configured(refresh=True)
                mcp_schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("schema", {"type": "object", "properties": {}}),
                        },
                    }
                    for tool in _mcp_get_manager().list_tools_for_registry()
                ]
                if workspace_mode:
                    mcp_schemas = []
            except Exception:
                mcp_schemas = []
            return built_in + extension_schemas + mcp_schemas
        # Core tools plus meta-tools for enabling extended tools.
        result = []
        for e in self._entries.values():
            if workspace_mode and not e.name in _WORKSPACE_ALLOWED_TOOLS:
                continue
            if e.name in CORE_TOOL_NAMES or e.name in ("list_available_tools", "enable_tools"):
                result.extend(self._schemas_for_entry(e))
        # Extension tools are discoverable in core-mode; MCP stays opt-in.
        return result + extension_schemas

    def get_schema_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full schema for a specific tool."""
        requested = str(name or "").strip()
        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        if workspace_mode and not requested in _WORKSPACE_ALLOWED_TOOLS:
            return None
        entry = self._entries.get(requested)
        if entry:
            return self._schema_for_entry(entry)
        try:
            from ouroboros.extension_loader import parse_extension_surface_name as _ext_parse_name
        except Exception:
            _ext_parse_name = None
        if _ext_parse_name and _ext_parse_name(name):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool, is_extension_live as _ext_is_live
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None
            if ext_tool and not workspace_mode and _ext_is_live(str(ext_tool.get("skill") or ""), pathlib.Path(self._ctx.drive_root)):
                return {
                    "type": "function",
                    "function": {
                        "name": ext_tool["name"],
                        "description": ext_tool.get("description", ""),
                        "parameters": ext_tool.get("schema", {"type": "object", "properties": {}}),
                    },
                }
        try:
            from ouroboros.mcp_client import (
                ensure_configured_from_settings as _mcp_ensure_configured,
                get_manager as _mcp_get_manager,
                is_mcp_tool_name as _mcp_is_name,
            )
            _mcp_ensure_configured(refresh=False)
        except Exception:
            _mcp_get_manager = None
            _mcp_is_name = None
        if not workspace_mode and _mcp_get_manager and _mcp_is_name and _mcp_is_name(requested):
            mcp_tool = _mcp_get_manager().get_tool(requested)
            if mcp_tool:
                return {
                    "type": "function",
                    "function": {
                        "name": mcp_tool["name"],
                        "description": mcp_tool.get("description", ""),
                        "parameters": mcp_tool.get("schema", {"type": "object", "properties": {}}),
                    },
                }
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool (default 360)."""
        entry = self._entries.get(str(name or "").strip())
        if entry is not None:
            return entry.timeout_sec
        # Extension tools carry timeout_sec in the loader descriptor.
        try:
            from ouroboros.extension_loader import parse_extension_surface_name as _ext_parse_name
        except Exception:
            _ext_parse_name = None
        if _ext_parse_name and _ext_parse_name(name):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None
            if ext_tool:
                # Add cleanup grace around the inner async wait_for.
                return int(ext_tool.get("timeout_sec") or 60) + 3
        try:
            from ouroboros.mcp_client import (
                ensure_configured_from_settings as _mcp_ensure_configured,
                get_manager as _mcp_get_manager,
                is_mcp_tool_name as _mcp_is_name,
            )
            _mcp_ensure_configured(refresh=False)
        except Exception:
            _mcp_get_manager = None
            _mcp_is_name = None
        if _mcp_get_manager and _mcp_is_name and _mcp_is_name(name):
            try:
                return int(_mcp_get_manager().tool_timeout_sec()) + 3
            except Exception:
                return 63
        return 360

    def _dispatch_extension_tool(self, name: str, ext_tool: Dict[str, Any], args: Optional[Dict[str, Any]]) -> str:
        """Dispatch live extension tools through the same safety gate as built-ins."""
        try:
            from ouroboros.extension_loader import (
                is_extension_live as _ext_is_live,
                unload_extension as _ext_unload,
            )
        except Exception:
            _ext_is_live = None
            _ext_unload = None
        skill_name = str(ext_tool.get("skill") or "")
        if skill_name and callable(_ext_is_live) and not _ext_is_live(skill_name, pathlib.Path(self._ctx.drive_root)):
            if callable(_ext_unload):
                _ext_unload(skill_name)
            return (
                f"⚠️ EXTENSION_NOT_LIVE: extension {skill_name!r} is "
                "not allowed to dispatch right now."
            )
        from ouroboros.safety import check_safety as _ext_check_safety
        _ext_safe, _ext_safety_msg = _ext_check_safety(
            name,
            args or {},
            messages=getattr(self._ctx, "messages", None),
            ctx=self._ctx,
        )
        if not _ext_safe:
            return _ext_safety_msg
        handler = ext_tool["handler"]
        try:
            result = handler(self._ctx, **(args or {}))
        except TypeError:
            result = handler(**(args or {}))
        except Exception as exc:
            return (
                f"⚠️ extension tool {name!r} failed: "
                f"{type(exc).__name__}: {exc}"
            )
        # Async extension handlers run on a helper thread to avoid same-loop deadlocks.
        import asyncio as _asyncio
        import inspect as _inspect
        import threading as _threading
        if _inspect.iscoroutine(result):
            box: Dict[str, Any] = {}
            timeout = max(1, int(ext_tool.get("timeout_sec") or 60))
            def _runner() -> None:
                try:
                    async def _bounded():
                        return await _asyncio.wait_for(result, timeout=timeout)
                    box["value"] = _asyncio.run(_bounded())
                except Exception as exc:
                    box["error"] = exc

            thread = _threading.Thread(
                target=_runner,
                name=f"ext-tool-{name}-async",
                daemon=True,
            )
            thread.start()
            thread.join(timeout=timeout + 2)
            if thread.is_alive():
                return (
                    f"⚠️ extension tool {name!r} async handler failed: "
                    "TimeoutError: handler exceeded timeout"
                )
            if "error" in box:
                exc = box["error"]
                return (
                    f"⚠️ extension tool {name!r} async handler failed: "
                    f"{type(exc).__name__}: {exc}"
                )
            result = box.get("value", "")
        result_str = result if isinstance(result, str) else str(result)
        if _ext_safety_msg:
            return f"{_ext_safety_msg}\n\n---\n{result_str}"
        return result_str

    def _dispatch_mcp_tool(self, name: str, args: Dict[str, Any]) -> str:
        """Run a provider-safe MCP tool after the normal safety supervisor."""
        from ouroboros.safety import check_safety as _mcp_check_safety
        is_safe, safety_msg = _mcp_check_safety(
            name,
            args,
            messages=getattr(self._ctx, "messages", None),
            ctx=self._ctx,
        )
        if not is_safe:
            return safety_msg
        try:
            from ouroboros.mcp_client import call_mcp_tool as _mcp_call
            result = _mcp_call(name, args or {})
        except Exception as exc:
            return f"⚠️ TOOL_ERROR ({name}): {exc}"
        return f"{safety_msg}\n\n---\n{result}" if safety_msg else result

    def _run_shell_safety_check(self, args: Dict[str, Any], runtime_mode: str) -> Optional[str]:
        """Pre-execution run_shell filter; returns a block message or ``None``."""
        raw_cmd = args.get("cmd", args.get("command", ""))
        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        if isinstance(raw_cmd, list):
            cmd_lower = " ".join(str(x) for x in raw_cmd).lower()
        else:
            cmd_lower = str(raw_cmd).lower()
        cmd_path_lower = cmd_lower.replace("\\", "/")
        while "//" in cmd_path_lower:
            cmd_path_lower = cmd_path_lower.replace("//", "/")
        argv_for_write = _strip_leading_env_assignments(_unwrap_env_argv(_shell_argv(raw_cmd)))
        writeish = any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS) or (
            bool(argv_for_write) and pathlib.PurePath(argv_for_write[0]).name.lower() in _LIGHT_SHELL_WRITER_COMMANDS
        )
        if workspace_mode and writeish:
            active_root = active_repo_dir_for(self._ctx).resolve(strict=False)
            if "../" in cmd_path_lower or cmd_path_lower.startswith(".."):
                return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell commands may not target paths outside the active workspace."
            protected_roots = [getattr(self._ctx, "system_repo_dir", None) or getattr(self._ctx, "repo_dir", None)]
            try:
                from ouroboros.config import DATA_DIR as _PARENT_DATA_DIR
                protected_roots.append(_PARENT_DATA_DIR)
            except Exception:
                pass
            for root_value in protected_roots:
                try:
                    root_path = pathlib.Path(root_value).resolve(strict=False)
                except Exception:
                    continue
                try:
                    root_path.relative_to(active_root)
                    continue
                except Exception:
                    pass
                root_text = str(root_path).replace("\\", "/").lower()
                if root_text and root_text in cmd_path_lower:
                    return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell command mentions Ouroboros system/data paths."
            for token in _shell_argv(raw_cmd):
                candidates = [str(token)] if str(token).startswith("/") else []
                candidates.extend(re.findall(r"/[^\s'\"\\),;\]]+", str(token)))
                for candidate in candidates:
                    try:
                        pathlib.Path(candidate).resolve(strict=False).relative_to(active_root)
                    except Exception:
                        return "⚠️ WORKSPACE_SHELL_BLOCKED: write-like shell commands may not target absolute paths outside the active workspace."

        # Elevation pattern: blocked in all modes.
        if _detect_runtime_mode_elevation(cmd_lower):
            return (
                "⚠️ ELEVATION_BLOCKED: shell command pattern looks "
                "like an OUROBOROS_RUNTIME_MODE elevation attempt "
                "(mentions ``save_settings`` together with "
                "``OUROBOROS_RUNTIME_MODE``, or invokes "
                "``ouroboros.config.save_settings`` directly). "
                "Runtime mode is owner-controlled — change it by "
                "stopping the agent and editing settings.json "
                "directly, then restart."
            )
        if _mentions_skill_owner_state(cmd_lower):
            return (
                "⚠️ SKILL_STATE_WRITE_BLOCKED: skill review, enablement, "
                "grants, and marketplace provenance are owner/review "
                "controlled state. Use review_skill, toggle_skill/the Skills "
                "UI, or the desktop launcher confirmation flow."
            )
        if "state" in cmd_lower and "skills" in cmd_lower and _mentions_detached_process(cmd_lower):
            return (
                "⚠️ SKILL_STATE_WRITE_BLOCKED: detached shell processes must "
                "not target skill state directories. Use the reviewed skill "
                "lifecycle tools instead."
            )

        # Light-mode repo-mutation indicators.
        if runtime_mode == "light" and not workspace_mode:
            if _light_shell_repo_mutation(
                raw_cmd,
                repo_dir=pathlib.Path(self._ctx.active_repo_dir()),
                cwd=str(args.get("cwd") or ""),
            ):
                return (
                    "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light refuses "
                    "shell commands that mutate the Ouroboros repository. "
                    "Switch to 'advanced' or 'pro' in Settings → "
                    "Behavior → Runtime Mode for write access."
                )

        # Lexical defense for skill control-plane sidecar writes via shell.
        if not workspace_mode and any(
            name in cmd_path_lower
            for name in (
                *SKILL_PAYLOAD_CONTROL_FILENAMES,
                *(SKILL_PAYLOAD_CONTROL_DIRNAMES - {"__pycache__"}),
            )
        ) and any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS):
            return (
                "⚠️ SAFETY_VIOLATION: Shell command would modify a skill "
                "provenance / launcher seed / dependency marker (.clawhub.json, "
                ".ouroboroshub.json, .self_authored.json, SKILL.openclaw.md, .seed-origin, "
                ".ouroboros_env, node_modules). "
                "Use marketplace lifecycle flows or edit user-authored "
                "payload files instead."
            )

        # Protected runtime path writes.
        if not workspace_mode and _shell_writer_targets_protected(raw_cmd):
            return (
                "⚠️ CRITICAL SAFETY_VIOLATION: Shell command would modify "
                "a protected core/contract/release file. Protected: "
                + ", ".join(sorted(PROTECTED_RUNTIME_PATHS))
            )
        if not workspace_mode:
            for cf in _PROTECTED_RUNTIME_PATHS_LOWER:
                if cf in cmd_path_lower and any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS):
                    return (
                        "⚠️ CRITICAL SAFETY_VIOLATION: Shell command would modify "
                        "a protected core/contract/release file. Protected: "
                        + ", ".join(sorted(PROTECTED_RUNTIME_PATHS))
                    )

        # GitHub repo create/delete/auth.
        if "gh repo create" in cmd_lower or "gh repo delete" in cmd_lower:
            return "⚠️ SAFETY_VIOLATION: Creating/deleting GitHub repositories requires admin approval."
        if "gh auth" in cmd_lower:
            return "⚠️ SAFETY_VIOLATION: Modifying GitHub authentication is not permitted."

        # Direct git mutative ban via shell.
        if workspace_mode:
            git_violation = _workspace_git_safety_violation(
                raw_cmd,
                active_root=active_repo_dir_for(self._ctx),
                cwd=str(args.get("cwd") or ""),
            )
            if git_violation:
                return (
                    "⚠️ WORKSPACE_GIT_BLOCKED: run_shell may only use read-only git "
                    f"operations inside the active workspace; blocked {git_violation}."
                )
        subcmd = _extract_run_shell_git_subcommand(raw_cmd)
        if subcmd and subcmd.lower() not in _GIT_READONLY_SUBCOMMANDS:
            return (
                f"⚠️ GIT_VIA_SHELL_BLOCKED: `git {subcmd}` must go through "
                "repo_commit which enforces pre-commit "
                "checks. For read-only git: git_status, git_diff tools, or "
                "run_shell with git log/show/diff/status."
            )
        return None

    def _snapshot_owner_files(self) -> Dict[pathlib.Path, Optional[str]]:
        from ouroboros import config as _cfg
        out: Dict[pathlib.Path, Optional[str]] = {}
        settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
        try:
            out[settings_path] = settings_path.read_text(encoding="utf-8") if settings_path.is_file() else None
        except OSError:
            out[settings_path] = None
        root = pathlib.Path(self._ctx.drive_root) / "state" / "skills"
        if not root.is_dir():
            return out
        for path in root.glob("*/*"):
            if path.name.lower() not in SKILL_OWNER_STATE_FILENAMES:
                continue
            try:
                out[path] = path.read_text(encoding="utf-8")
            except OSError:
                out[path] = None
        return out

    def _restore_owner_files(self, before: Dict[pathlib.Path, Optional[str]]) -> bool:
        from ouroboros import config as _cfg
        root = pathlib.Path(self._ctx.drive_root) / "state" / "skills"
        current = set()
        if root.is_dir():
            current.update(
                path for path in root.glob("*/*")
                if path.name.lower() in SKILL_OWNER_STATE_FILENAMES
            )
        settings_path = pathlib.Path(_cfg.SETTINGS_PATH)
        current.add(settings_path)
        changed = False
        for path in current - set(before):
            try:
                path.unlink()
                changed = True
            except OSError:
                pass
        for path, content in before.items():
            try:
                if content is None:
                    if path.exists():
                        path.unlink()
                        changed = True
                    continue
                if not path.exists() or path.read_text(encoding="utf-8") != content:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content, encoding="utf-8")
                    changed = True
            except OSError:
                pass
        return changed

    def _run_shell_post_checks(
        self,
        result: str,
        *,
        owner_snapshot: Dict[pathlib.Path, Optional[str]],
        light_repo_before: Optional[Dict[str, Any]],
        workspace_refs_before: Optional[Dict[str, str]],
    ) -> str:
        import time

        restored_owner_state = False
        for _ in range(4):
            time.sleep(0.3)
            restored_owner_state = self._restore_owner_files(owner_snapshot) or restored_owner_state
        if restored_owner_state:
            result = (
                f"{result}\n\n⚠️ OWNER_STATE_RESTORED: run_shell attempted to "
                "change owner-only settings or skill trust state; protected files were restored."
            )
        if light_repo_before is not None:
            light_repo_after = _light_repo_snapshot(active_repo_dir_for(self._ctx))
            if (
                light_repo_after is not None
                and light_repo_after.get("digest") != light_repo_before.get("digest")
            ):
                result = _format_light_repo_write_block(light_repo_before, light_repo_after, result)
        if workspace_refs_before is not None:
            workspace_refs_after = _git_ref_snapshot(active_repo_dir_for(self._ctx))
            if (
                workspace_refs_after is not None
                and workspace_refs_after.get("digest") != workspace_refs_before.get("digest")
            ):
                result = (
                    "⚠️ WORKSPACE_GIT_REF_CHANGED: run_shell changed git HEAD or refs "
                    "inside the external workspace. External workspace runs must leave "
                    "changes as files/patch artifacts, not commits/tags/resets.\n\n"
                    "Original command output:\n"
                    f"{result}"
                )
        return result

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        name = str(name or "").strip()
        args = dict(args or {})
        entry = self._entries.get(name)
        ext_tool = None
        try:
            from ouroboros.extension_loader import parse_extension_surface_name as _ext_parse_name
        except Exception:
            _ext_parse_name = None
        if entry is None and _ext_parse_name and _ext_parse_name(name):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None

        try:
            from ouroboros.mcp_client import (
                ensure_configured_from_settings as _mcp_ensure_configured,
                is_mcp_tool_name as _mcp_is_name,
            )
            _mcp_ensure_configured(refresh=False)
        except Exception:
            _mcp_is_name = None
        is_mcp = bool(_mcp_is_name and _mcp_is_name(name))

        workspace_mode = bool(getattr(self._ctx, "is_workspace_mode", lambda: False)())
        if workspace_mode and (is_mcp or ext_tool or not name in _WORKSPACE_ALLOWED_TOOLS):
            workspace = str(getattr(self._ctx, "workspace_root", "") or "")
            return (
                "⚠️ WORKSPACE_MODE_BLOCKED: this task is running against an external "
                f"workspace ({workspace}). Tool {name!r} is outside the v5.29 "
                "workspace allowlist. Leave workspace changes as files or a patch artifact."
            )

        # Hardcoded sandbox: light blocks repo mutation; advanced protects
        # core/contracts/release; pro still relies on commit review.
        try:
            from ouroboros.config import get_runtime_mode as _get_runtime_mode
            _runtime_mode = _get_runtime_mode()
        except Exception:
            _runtime_mode = "advanced"

        task_constraint = normalize_task_constraint(getattr(self._ctx, "task_constraint", None))
        heal_no_enable = bool(task_constraint and task_constraint.mode == "skill_repair")
        if heal_no_enable:
            heal_skill = task_constraint.skill_name if task_constraint else ""
            if name in {"data_read", "data_write"}:
                data_path = str(args.get("path", "") or "")
                if not _task_constraint_path_allowed(data_path, task_constraint, pathlib.Path(self._ctx.drive_root)):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Repair data access is limited "
                        "to the selected skill payload under data/skills/external "
                        "data/skills/clawhub, or data/skills/ouroboroshub."
                    )
                if name == "data_write" and _heal_protected_payload_sidecar(data_path):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Repair may not edit marketplace "
                        "or official provenance sidecars (.clawhub.json, "
                        ".ouroboroshub.json, SKILL.openclaw.md, .seed-origin). "
                        "Edit the user-authored payload files instead."
                    )
            if name == "data_list":
                data_dir = str(args.get("dir", args.get("path", "")) or "")
                if not _task_constraint_path_allowed(data_dir, task_constraint, pathlib.Path(self._ctx.drive_root)):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Repair data listing is limited "
                        "to the selected skill payload under data/skills/external "
                        "data/skills/clawhub, or data/skills/ouroboroshub."
                    )
            if name == "str_replace_editor":
                edit_path = str(args.get("path", "") or "")
                if not _task_constraint_path_allowed(edit_path, task_constraint, pathlib.Path(self._ctx.drive_root)):
                    return "⚠️ HEAL_MODE_BLOCKED: Repair str_replace_editor is limited to the selected skill payload."
                if _heal_protected_payload_sidecar(edit_path):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Repair may not edit marketplace "
                        "or official provenance sidecars (.clawhub.json, "
                        ".ouroboroshub.json, SKILL.openclaw.md, .seed-origin). "
                        "Edit the user-authored payload files instead."
                    )
            if name == "claude_code_edit":
                cwd_text = str(args.get("cwd", "") or "")
                if not _task_constraint_path_allowed(cwd_text, task_constraint, pathlib.Path(self._ctx.drive_root)):
                    return "⚠️ HEAL_MODE_BLOCKED: Repair claude_code_edit cwd must be the selected skill payload."
            if name == "review_skill" and str(args.get("skill", "") or "").strip() != heal_skill:
                return "⚠️ HEAL_MODE_BLOCKED: Repair may only review the selected skill."
            if name == "skill_preflight" and str(args.get("skill", "") or "").strip() != heal_skill:
                return "⚠️ HEAL_MODE_BLOCKED: Repair may only preflight the selected skill."
            if ext_tool or is_mcp or name not in _HEAL_MODE_ALLOWED_TOOLS:
                return (
                    "⚠️ HEAL_MODE_BLOCKED: Repair tasks may inspect/edit skill "
                    "payloads and run review_skill only. Shell, browser automation, "
                    "repo mutation, skill execution, extension tools, MCP tools, "
                    "delegation, and enable/disable flows are unavailable. Use "
                    "the Skills UI after a fresh executable review."
                )
        if is_mcp:
            return self._dispatch_mcp_tool(name, args)
        if entry is None:
            if ext_tool and callable(ext_tool.get("handler")):
                return self._dispatch_extension_tool(name, ext_tool, args)
            return f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._entries.keys()))}"
        _REPO_MUTATION_TOOLS = frozenset(
            {
                "repo_write",
                "repo_commit",
                "str_replace_editor",
                "claude_code_edit",
                "revert_commit",
                "pull_from_remote",
                "restore_to_head",
                "rollback_to_target",
                "promote_to_stable",
                # PR integration tools mutate the local worktree/refs.
                "fetch_pr_ref",
                "create_integration_branch",
                "cherry_pick_pr_commits",
                "stage_adaptations",
                "stage_pr_merge",
            }
        )
        raw_bucket = str(args.get("bucket", "") or "")
        raw_skill_name = str(args.get("skill_name", "") or "")
        short_path_text = str(args.get("cwd") if name == "claude_code_edit" else args.get("path", "") or "")
        short_form_decision = decide_payload_short_form(
            bucket=raw_bucket,
            skill_name=raw_skill_name,
            path_text=short_path_text or ".",
            repo_dir=pathlib.Path(self._ctx.repo_dir),
            drive_root=pathlib.Path(self._ctx.drive_root),
        )
        synth_constraint = short_form_decision.constraint
        # Prefer specific skill payload arg errors over generic light-mode block.
        if (
            (raw_bucket or raw_skill_name)
            and short_form_decision.error
            and name in (
                "data_write",
                "str_replace_editor",
                "claude_code_edit",
            )
        ):
            return f"⚠️ SKILL_PAYLOAD_ARG_ERROR: {short_form_decision.error}"
        # Real skill_repair constraints beat synthesized short-form constraints.
        redirect_err = cross_skill_redirect_error(task_constraint, synth_constraint)
        if redirect_err and name in (
            "data_write",
            "str_replace_editor",
            "claude_code_edit",
        ):
            return f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}"
        # Existing skill_repair constraint remains authoritative.
        if task_constraint and task_constraint.mode == "skill_repair":
            effective_constraint = task_constraint
        else:
            effective_constraint = synth_constraint or task_constraint
        allow_short_relative = bool(
            effective_constraint and effective_constraint.mode == "skill_repair"
        )
        light_skill_scoped_claude = (
            _runtime_mode == "light"
            and name == "claude_code_edit"
            and is_skill_payload_path(
                pathlib.Path(self._ctx.drive_root),
                str(args.get("cwd") or "."),
                constraint=effective_constraint,
                allow_short_relative=allow_short_relative,
                allow_control_plane=False,
            )
        )
        light_skill_scoped_str_replace = (
            _runtime_mode == "light"
            and name == "str_replace_editor"
            and is_skill_payload_path(
                pathlib.Path(self._ctx.drive_root),
                str(args.get("path", "") or ""),
                constraint=effective_constraint,
                allow_short_relative=allow_short_relative,
                allow_control_plane=False,
            )
        )
        if (
            _runtime_mode == "light"
            and name in _REPO_MUTATION_TOOLS
            and not workspace_mode
            and not light_skill_scoped_claude
            and not light_skill_scoped_str_replace
        ):
            return (
                "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light disables "
                "repo self-modification. Tool "
                f"{name!r} would mutate the Ouroboros repository. "
                "Payload edits under data/skills/{external,clawhub,ouroboroshub}/<skill>/ "
                "are allowed via three paths: "
                "(1) task_constraint.mode='skill_repair'; "
                "(2) an explicit cwd/path under data/skills/<bucket>/<skill>/...; "
                "(3) supplying bucket and skill_name args (a short relative cwd/path "
                "then resolves under the skill payload). "
                "Switch to 'advanced' or 'pro' in Settings → Behavior "
                "→ Runtime Mode to re-enable self-modification."
            )

        protected_write_paths = []
        if name in ("repo_write", "str_replace_editor"):
            if name == "repo_write":
                maybe_path = str(args.get("path", "") or "")
                if maybe_path:
                    protected_write_paths.append(maybe_path)
                for f_entry in args.get("files") or []:
                    if isinstance(f_entry, dict):
                        protected_write_paths.append(str(f_entry.get("path", "") or ""))
            elif name == "str_replace_editor":
                protected_write_paths.append(str(args.get("path", "") or ""))
            protected_matches = [] if workspace_mode else protected_paths_in(protected_write_paths)
            if protected_matches and not mode_allows_protected_write(_runtime_mode):
                first = protected_matches[0]
                return protected_write_block_message(
                    path=first.path,
                    runtime_mode=_runtime_mode,
                    action=f"run tool {name!r} against",
                )

        if name == "run_shell":
            block_msg = self._run_shell_safety_check(args, _runtime_mode)
            if block_msg:
                return block_msg

        # LLM safety supervisor.
        from ouroboros.safety import check_safety
        is_safe, safety_msg = check_safety(
            name,
            args,
            messages=getattr(self._ctx, "messages", None),
            ctx=self._ctx,
        )
        if not is_safe:
            return safety_msg

        owner_snapshot = self._snapshot_owner_files() if name == "run_shell" else {}
        light_repo_before = (
            _light_repo_snapshot(active_repo_dir_for(self._ctx))
            if name == "run_shell" and _runtime_mode == "light"
            else None
        )
        workspace_refs_before = (
            _git_ref_snapshot(active_repo_dir_for(self._ctx))
            if name == "run_shell" and workspace_mode
            else None
        )
        try:
            result = entry.handler(self._ctx, **args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"
        if name == "run_shell":
            result = self._run_shell_post_checks(
                result,
                owner_snapshot=owner_snapshot,
                light_repo_before=light_repo_before,
                workspace_refs_before=workspace_refs_before,
            )

        # Pro can touch protected files, but commit review still gates landing.
        if name == "claude_code_edit":
            reverted = _revert_protected_files(self._ctx.repo_dir, runtime_mode=_runtime_mode)
            if reverted:
                result += (
                    "\n\n⚠️ SAFETY: Reverted modifications to protected files: "
                    + ", ".join(reverted)
                )
            elif mode_allows_protected_write(_runtime_mode):
                try:
                    diff = subprocess.run(
                        ["git", "diff", "--name-only"],
                        cwd=str(self._ctx.repo_dir), capture_output=True, text=True, timeout=5,
                    )
                    protected_matches = protected_paths_in(diff.stdout.splitlines() if diff.returncode == 0 else [])
                except Exception:
                    protected_matches = []
                if protected_matches:
                    result += "\n\n" + core_patch_notice(protected_matches)

        if safety_msg:
            return f"{safety_msg}\n\n---\n{result}"
        return result

    def override_handler(self, name: str, handler) -> None:
        """Override the handler for a registered tool (used for closure injection)."""
        entry = self._entries.get(name)
        if entry:
            self._entries[name] = ToolEntry(
                name=entry.name,
                schema=entry.schema,
                handler=handler,
                timeout_sec=entry.timeout_sec,
            )

    @property
    def CODE_TOOLS(self) -> frozenset:
        return frozenset(e.name for e in self._entries.values() if e.is_code_tool)

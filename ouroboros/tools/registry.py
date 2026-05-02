"""
Ouroboros — Tool registry (SSOT).

Plugin architecture: each module in tools/ exports get_tools().
ToolRegistry collects all tools, provides schemas() and execute().
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
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
from ouroboros.tool_aliases import (
    adapt_tool_args,
    alias_schema,
    aliases_for_canonical,
    canonical_tool_name,
)
from ouroboros.utils import safe_relpath

log = logging.getLogger(__name__)

_PROTECTED_RUNTIME_PATHS_LOWER = frozenset(
    p.lower() for p in PROTECTED_RUNTIME_PATHS
) | frozenset(prefix.lower() for prefix in FROZEN_CONTRACT_PATH_PREFIXES)

_SHELL_WRITE_INDICATORS = (
    "rm ", "rm\t", ">", "sed -i", "tee ", "truncate",
    "mv ", "cp ", "chmod ", "chown ", "unlink ", "delete", "trash",
    "rsync ", "write_text", "open(", ".write(", ".writelines(",
)

# v5.1.2 elevation ratchet: indicators reused by the run_shell argv
# check AND by the run_shell file-content scan (subprocess invocation
# `python helper.py` where helper.py contains the dangerous code).
# Splitting these into module-level constants lets ``execute`` apply the
# same check at both layers without duplicating the tuple.
#
# ``_LIGHT_MUTATION_INDICATORS`` fires only when ``runtime_mode == light``
# and matches obvious repo-mutation patterns. ``_ELEVATION_PROBES``
# captures the conjunctive elevation pattern (``save_settings`` together
# with ``OUROBOROS_RUNTIME_MODE``, OR the dotted ``ouroboros.config.save_settings``
# attribute path) — used in ALL modes because elevation ``advanced→pro``
# is also out of scope for the agent.
_LIGHT_MUTATION_INDICATORS = (
    "git commit", "git add", "git push", "git rebase", "git reset",
    "git checkout", "git merge", "git pull", "git stash drop",
    "git revert", "git cherry-pick",
    " > ", " >> ", " | tee ",
    "rm -", "mkdir ", "mv ", "cp ", "touch ",
    # In-place file mutation via common Unix tools.
    "sed -i", "perl -i", "ruby -i",
    "truncate ", "chmod ", "chown ", "ln -",
    "tar -x", "unzip ", "gzip ", "gunzip ",
    # Python / JS in-place writers.
    "open(", ".write(", ".writelines(",
    # ``Path.write_text`` / ``Path.write_bytes`` are not substrings of
    # ``.write(`` because of the ``_text`` / ``_bytes`` suffix between
    # ``write`` and ``(``.
    ".write_text(", ".write_bytes(",
    # OS-level rename / replace primitives commonly used to atomically
    # clobber a file.
    "os.replace(", "os.rename(",
)


def _detect_runtime_mode_elevation(text_lower: str) -> bool:
    """Return True when ``text_lower`` (a lowercased shell argv string OR
    a script file's lowercased content) matches the v5.1.2 elevation
    pattern: BOTH ``save_settings`` AND ``ouroboros_runtime_mode`` are
    present, OR the dotted attribute path ``ouroboros.config.save_settings``
    appears verbatim. The conjunctive form keeps the false-positive rate
    low for legitimate diagnostics (``echo $OUROBOROS_RUNTIME_MODE``,
    ``grep save_settings ouroboros/config.py``)."""
    has_save = "save_settings" in text_lower
    has_mode_key = "ouroboros_runtime_mode" in text_lower
    has_dotted_path = "ouroboros.config.save_settings" in text_lower
    return (has_save and has_mode_key) or has_dotted_path


def _is_heal_no_enable_context(messages: Optional[List[Dict[str, Any]]]) -> bool:
    for message in messages or []:
        content = message.get("content")
        if isinstance(content, str) and "HEAL_MODE_NO_ENABLE" in content:
            return True
    return False


def _heal_skill_name(messages: Optional[List[Dict[str, Any]]]) -> str:
    return _heal_marker_value(messages, "HEAL_SKILL_NAME_JSON=")


def _heal_payload_root(messages: Optional[List[Dict[str, Any]]]) -> str:
    raw = _raw_heal_marker_value(messages, "HEAL_SKILL_PAYLOAD_ROOT_JSON=")
    path = raw.replace("\\", "/").strip("/")
    if ".." in path:
        return ""
    parts = pathlib.PurePosixPath(path).parts
    if len(parts) < 3 or parts[0] != "skills" or parts[1] not in {"external", "clawhub", "ouroboroshub"}:
        return ""
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    return "/".join(parts)


def _raw_heal_marker_value(messages: Optional[List[Dict[str, Any]]], marker: str) -> str:
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, str) or marker not in content:
            continue
        raw = content.split(marker, 1)[1].splitlines()[0].strip()
        try:
            value = json.loads(raw)
        except Exception:
            value = raw.strip('"')
        return str(value or "").strip()
    return ""


def _heal_marker_value(messages: Optional[List[Dict[str, Any]]], marker: str) -> str:
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, str) or marker not in content:
            continue
        raw = content.split(marker, 1)[1].splitlines()[0].strip()
        try:
            value = json.loads(raw)
        except Exception:
            value = raw.strip('"')
        text = str(value or "").strip()
        if (
            not text
            or "/" in text
            or "\\" in text
            or text in {".", ".."}
            or any(part in {".", ".."} for part in pathlib.PurePosixPath(text).parts)
        ):
            return ""
        return text
    return ""


def _heal_data_path_allowed(path_text: str, payload_root: str, drive_root: pathlib.Path) -> bool:
    if not payload_root:
        return False
    try:
        drive = pathlib.Path(drive_root).resolve(strict=False)
        allowed_root = pathlib.Path(os.path.realpath(drive / safe_relpath(payload_root)))
        target = pathlib.Path(os.path.realpath(drive / safe_relpath(path_text or "")))
        target.relative_to(allowed_root)
        return True
    except (OSError, ValueError):
        return False


_HEAL_MODE_ALLOWED_TOOLS = frozenset({
    "data_read",
    "data_list",
    "data_write",
    "list_skills",
    "review_skill",
    # v5.7.0: skill_preflight is a read-only syntax validator
    # (Python compile() / node --check / bash -n + manifest parse). Heal mode
    # agents use it to catch silly typos before spending money on a
    # tri-model ``review_skill`` round. It NEVER mutates review state,
    # NEVER touches enabled.json / grants.json, and NEVER spawns shell
    # strings (no run_shell escape).
    "skill_preflight",
})

_HEAL_PROTECTED_PAYLOAD_FILENAMES = frozenset({
    ".clawhub.json",
    ".ouroboroshub.json",
    # v5.7.0: extend heal-mode payload sidecar protection in lockstep with
    # the central ``is_skill_control_plane_path`` guard in ``tools/core.py``.
    # Without these the launcher-seeded ``.seed-origin`` markers and the
    # original OpenClaw-publisher ``SKILL.openclaw.md`` could be silently
    # rewritten by a heal task — which would either disconnect the skill
    # from its update lane (.seed-origin) or launder the provenance the
    # reviewer cross-checks against (SKILL.openclaw.md).
    "skill.openclaw.md",
    ".seed-origin",
})


_SKILL_OWNER_STATE_STEMS = ("grants", "review", "enabled", "clawhub", "deps")
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
    name = pathlib.PurePosixPath(str(path_text or "").replace("\\", "/")).name
    return name.lower() in _HEAL_PROTECTED_PAYLOAD_FILENAMES


_INTERPRETER_BASENAMES = frozenset({
    "python", "python2", "python3",
    "bash", "sh", "zsh",
    "node", "nodejs",
})


def _extract_script_file_args(raw_cmd: Any) -> List[str]:
    """Return script file paths an interpreter is asked to execute.

    Recognises ``python``/``python3``/``bash``/``sh``/``zsh``/``node``
    invocations in argv form (list of strings) or shell-string form,
    and returns the first non-flag positional argument(s) following an
    interpreter token. Skips ``-c`` (inline code), ``-m`` (module
    name), ``-`` (stdin) and standard interpreter flags. Returns an
    empty list when no file argument is found, the cmd is unparseable,
    or the interpreter has no script file (e.g. ``python -c "..."``).

    Used by ``ToolRegistry.execute`` for ``run_shell`` to detect the
    file-based subprocess elevation bypass pattern: ``python evil.py``
    where ``evil.py`` was just written by ``data_write`` and contains
    code that imports ``save_settings`` or writes ``settings.json``.
    The argv-level substring check cannot see file content; the caller
    reads each returned path's content and re-runs the indicator
    checks against that content.
    """
    import shlex
    if isinstance(raw_cmd, list):
        argv = [str(x) for x in raw_cmd]
    else:
        try:
            argv = shlex.split(str(raw_cmd or ""))
        except ValueError:
            return []
    if not argv:
        return []

    def _option_arity(interpreter: str, option: str) -> int:
        """Return how many following argv tokens this interpreter option consumes."""
        if interpreter.startswith("python"):
            if "=" in option:
                return 0
            # Common CPython options with a following argument:
            # -W action, -X opt, -m module, -c command.
            if option in {"-c", "-m"}:
                return -1  # inline/module modes: no script file follows for this invocation
            if option in {"-W", "-X", "-Q"}:
                return 1
            return 0
        if interpreter in {"node", "nodejs"}:
            if option.startswith(("--require=", "--import=", "--loader=", "--experimental-loader=")):
                return 0
            if "=" in option:
                return 0
            # Node options with following values. For --require/-r and
            # --import, the consumed value is ALSO code that Node loads before
            # the main script, so scan it too when it looks like a file. The
            # main loop keeps scanning later positional args after consuming.
            if option in {"-e", "--eval"}:
                return -1
            if option in {"-r", "--require", "--import", "--loader", "--experimental-loader"}:
                return 1
            return 0
        # Shells: -c consumes a command string; otherwise flags generally don't
        # consume file-like args before the script.
        if option == "-c":
            return -1
        return 0

    files: List[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        # Strip leading paths so ``/usr/bin/python3`` and ``python3``
        # both match. Handle both POSIX and Windows separators because
        # the agent's argv may originate from either.
        basename = token.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        is_interpreter = (
            basename in _INTERPRETER_BASENAMES
            or any(basename.startswith(p + ".") for p in _INTERPRETER_BASENAMES)
        )
        if not is_interpreter:
            i += 1
            continue
        # Walk forward to find every plausible script file argument. Pre-v5.7
        # code returned the first non-flag after the interpreter; that missed
        # common arity options like `python -W ignore evil.py` by scanning
        # `ignore` instead of `evil.py`.
        j = i + 1
        while j < len(argv):
            arg = argv[j]
            if arg == "-":
                # Stdin marker.
                break
            if arg.startswith("-"):
                # Node supports --require=preload.js / --import=module.mjs
                # equals-form preload options. These values execute code
                # before the main script or eval string, so scan them too.
                if basename in {"node", "nodejs"} and arg.startswith(("--require=", "--import=", "--loader=", "--experimental-loader=")):
                    value = arg.split("=", 1)[1]
                    if value and any(value.endswith(ext) for ext in (".js", ".mjs", ".cjs")):
                        files.append(value)
                    j += 1
                    continue
                arity = _option_arity(basename, arg)
                if arity < 0:
                    break
                if arity > 0:
                    # If the option value itself is a preload/module path
                    # (Node --require/--import), scan it too.
                    if j + 1 < len(argv):
                        value = argv[j + 1]
                        if value and not value.startswith("-") and any(
                            value.endswith(ext) for ext in (".py", ".js", ".mjs", ".cjs", ".sh", ".bash")
                        ):
                            files.append(value)
                    j += 1 + arity
                    continue
                j += 1
                continue
            files.append(arg)
            # Keep scanning: e.g. `node --require preload.js evil.js` should
            # scan both preload.js and evil.js.
            j += 1
            continue
        i = j + 1 if j < len(argv) else i + 1
    return files


# Bound for file-content scans. 256 KB is enough for any realistic
# helper script the agent might ask ``run_shell`` to execute; bigger
# files are skipped (the scan is best-effort defense in depth — the
# authoritative gate is the ``save_settings`` chokepoint).
_RUN_SHELL_SCAN_BYTES = 256 * 1024

# Git via run_shell: only truly read-only subcommands allowed
_GIT_READONLY_SUBCOMMANDS = frozenset([
    "status", "diff", "log", "show", "ls-files",
    "describe", "rev-parse", "cat-file",
    "shortlog", "version", "help", "blame",
    "grep", "reflog", "fetch",
])

_SHELL_WRAPPERS = frozenset(["bash", "sh", "dash", "zsh", "env"])

def _revert_protected_files(repo_dir, *, runtime_mode: str = "advanced") -> list:
    """After claude_code_edit, revert protected files unless pro mode is active."""
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
    """Extract the git subcommand from a parsed command list.

    Handles: git status, git -C /path status, git --no-pager log, etc.
    """
    if not cmd_parts:
        return ""
    parts = [str(p) for p in cmd_parts]
    if parts[0] != "git":
        return ""
    i = 1
    while i < len(parts):
        p = parts[i]
        if p.startswith("-"):
            if p in ("-C", "--git-dir", "--work-tree"):
                i += 2
            else:
                i += 1
        else:
            return p
    return ""


@dataclass
class BrowserState:
    """Per-task browser lifecycle state (Playwright). Isolated from generic ToolContext."""

    pw_instance: Any = None
    browser: Any = None
    page: Any = None
    last_screenshot_b64: Optional[str] = None


@dataclass
class ToolContext:
    """Tool execution context — passed from the agent before each task."""

    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"
    pending_events: List[Dict[str, Any]] = field(default_factory=list)
    current_chat_id: Optional[int] = None
    current_task_type: Optional[str] = None
    pending_restart_reason: Optional[str] = None
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # LLM-driven model/effort switch (set by switch_model tool, read by loop.py)
    active_model_override: Optional[str] = None
    active_effort_override: Optional[str] = None
    active_use_local_override: Optional[bool] = None

    # Per-task browser state
    browser_state: BrowserState = field(default_factory=BrowserState)

    # Budget tracking (set by loop.py for real-time usage events)
    event_queue: Optional[Any] = None
    task_id: Optional[str] = None

    # Conversation messages (set by loop.py so safety checks have context)
    messages: Optional[List[Dict[str, Any]]] = None

    # Task depth for fork bomb protection
    task_depth: int = 0

    # True when running inside handle_chat_direct (not a queued worker task)
    is_direct_chat: bool = False

    # Pre-commit review state (reset per-commit, carried across review rounds)
    _review_advisory: List[Any] = field(default_factory=list)
    _review_iteration_count: int = 0
    _review_history: list = field(default_factory=list)

    # Compatibility profile (ouroboros/compat.py) — captured once per task in
    # agent.py::run_task, read by loop.py / loop_tool_execution.py to gate
    # local-LLM-friendly behaviour. ``None`` = legacy / dormant default.
    compat_profile: Optional[Any] = None
    # Per-task counters for tool-call dedup and gate-retry caps.
    # Reset in agent.py::run_task at the top of each task.
    _dedup_counts: Dict[str, int] = field(default_factory=dict)
    _gate_failure_counts: Dict[Any, int] = field(default_factory=dict)
    # Phase 8 escalation state (set by ouroboros/escalation.py).
    _dedup_ping_counts: Dict[str, int] = field(default_factory=dict)
    _wall_budget_breached: bool = False
    _wall_budget_extensions: int = 0
    # Resume-layer Phase 1 state — flag for at-most-once auto-snapshot per task.
    _task_checkpoint_written: bool = False

    def repo_path(self, rel: str) -> pathlib.Path:
        resolved = (self.repo_dir / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(self.repo_dir.resolve())
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
    """Single tool descriptor: name, schema, handler, metadata."""

    name: str
    schema: Dict[str, Any]
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 360


CORE_TOOL_NAMES = {
    "repo_read", "repo_list", "repo_write", "repo_write_commit", "repo_commit",
    "data_read", "data_list", "data_write",
    "run_shell", "claude_code_edit",
    "ensure_claude_cli",
    "git_status", "git_diff",
    "pull_from_remote", "restore_to_head", "revert_commit",
    "schedule_task", "wait_for_task", "get_task_result",
    "set_tool_timeout",
    "update_scratchpad", "update_identity",
    "chat_history", "web_search",
    "send_user_message", "switch_model",
    "request_restart", "promote_to_stable",
    "knowledge_read", "knowledge_write", "knowledge_list",
    "browse_page", "browser_action", "analyze_screenshot",
    # v5.7.0: keep this frozen fallback copy aligned with
    # tool_capabilities.CORE_TOOL_NAMES. ToolPolicy is the runtime SSOT, but
    # some schemas(core_only=True) callers still use this local set.
    "review_skill", "skill_preflight",
}


class ToolRegistry:
    """Ouroboros tool registry (SSOT).

    To add a tool: create a module in ouroboros/tools/,
    export get_tools() -> List[ToolEntry].
    """

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self._entries: Dict[str, ToolEntry] = {}
        self._ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
        self._load_modules()

    _FROZEN_TOOL_MODULES = [
        "a2a", "browser", "ci", "claude_advisory_review", "compact_context", "control",
        "core", "evolution_stats", "git", "git_rollback", "github", "health",
        "knowledge", "memory_tools", "plan_review", "review", "search", "shell",
        # Phase 3 three-layer refactor: external skill surface
        # (list_skills / review_skill / skill_exec / toggle_skill).
        "skill_exec",
        # v5.7.0: skill_preflight — read-only payload validator for heal mode.
        "skill_preflight",
        "tool_discovery", "vision",
    ]

    def _load_modules(self) -> None:
        """Auto-discover tool modules in ouroboros/tools/ that export get_tools()."""
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
        """Register a new tool (for extension by Ouroboros)."""
        self._entries[entry.name] = entry

    # --- Contract ---

    def available_tools(self) -> List[str]:
        return [e.name for e in self._entries.values()]

    def _schema_for_entry(self, entry: ToolEntry, *, alias: str = "") -> Dict[str, Any]:
        schema = alias_schema(alias, entry.schema) if alias else entry.schema
        return {"type": "function", "function": schema}

    def _schemas_for_entry(self, entry: ToolEntry) -> List[Dict[str, Any]]:
        schemas = [self._schema_for_entry(entry)]
        schemas.extend(
            self._schema_for_entry(entry, alias=alias)
            for alias in aliases_for_canonical(entry.name)
        )
        return schemas

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        built_in = [
            schema
            for entry in self._entries.values()
            for schema in self._schemas_for_entry(entry)
        ]
        # Include live extension-registered tool schemas so the normal
        # tool-policy/enable_tools path can surface provider-safe extension
        # tool entries instead of leaving them manually dispatch-only.
        # entries instead of leaving them manually dispatch-only.
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
        except Exception:
            extension_schemas = []

        if not core_only:
            return built_in + extension_schemas
        # Core tools + meta-tools for discovering/enabling extended tools
        result = []
        for e in self._entries.values():
            if e.name in CORE_TOOL_NAMES or e.name in ("list_available_tools", "enable_tools"):
                result.extend(self._schemas_for_entry(e))
        # Keep live extension tools enumerable in core-mode too so the
        # loop can discover them through the standard registry surface.
        return result + extension_schemas

    def list_non_core_tools(self) -> List[Dict[str, str]]:
        """Return name+description of all non-core tools."""
        result = []
        for e in self._entries.values():
            if e.name not in CORE_TOOL_NAMES:
                desc = e.schema.get("description", "No description")
                result.append({"name": e.name, "description": desc})
        try:
            from ouroboros.extension_loader import (
                _tools as _ext_tools,
                _lock as _ext_lock,
                is_extension_live as _ext_is_live,
            )
            with _ext_lock:
                for tool in _ext_tools.values():
                    skill_name = str(tool.get("skill") or "")
                    if not skill_name or not _ext_is_live(skill_name, pathlib.Path(self._ctx.drive_root)):
                        continue
                    result.append(
                        {
                            "name": str(tool.get("name") or ""),
                            "description": str(tool.get("description") or "No description"),
                        }
                    )
        except Exception:
            pass
        return result

    def get_schema_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full schema for a specific tool."""
        requested = str(name or "").strip()
        canonical = canonical_tool_name(requested)
        entry = self._entries.get(canonical)
        if entry:
            alias = requested if requested != canonical else ""
            return self._schema_for_entry(entry, alias=alias)
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
            if ext_tool and _ext_is_live(str(ext_tool.get("skill") or ""), pathlib.Path(self._ctx.drive_root)):
                return {
                    "type": "function",
                    "function": {
                        "name": ext_tool["name"],
                        "description": ext_tool.get("description", ""),
                        "parameters": ext_tool.get("schema", {"type": "object", "properties": {}}),
                    },
                }
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool (default 360)."""
        entry = self._entries.get(canonical_tool_name(name))
        if entry is not None:
            return entry.timeout_sec
        # Phase 5: extension-registered tools carry their own timeout_sec
        # in the loader's tool descriptor.
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
                # Extension async handlers enforce their own ``timeout_sec``
                # via ``asyncio.wait_for`` inside _dispatch_extension_tool.
                # Give the outer tool executor a small cleanup grace so it
                # does not return first while the inner coroutine is still
                # being cancelled.
                return int(ext_tool.get("timeout_sec") or 60) + 3
        return 360

    def _dispatch_extension_tool(self, name: str, ext_tool: Dict[str, Any], args: Optional[Dict[str, Any]]) -> str:
        """Run a provider-safe extension handler with the same safety gates
        the built-in tool path uses.

        v5.1.2 Frame A: extension dispatch is allowed in ``light`` (skills
        carry their own independent review + content-hash + sandbox
        stack); the ``light`` mode block previously here was removed.
        v5.1.2 iter-2 real triad finding TR1 (gpt-5.5 critical):
        extension dispatch previously short-circuited to the handler
        without reaching ``check_safety``, so removing the light-mode
        gate left extension tools unsupervised in light. Route through
        the same supervisor the built-in path uses so the per-call
        safety check applies uniformly.
        """
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
        # v5.7.0: extension authors writing async handlers used to silently
        # fail — register_tool typed handlers as ``Callable[..., str]``
        # but extension authors regularly registered ``async def`` tools.
        # ``handler(...)`` returns a coroutine object; ``str(coroutine)``
        # rendered ``<coroutine object … at 0x…>`` and the agent never saw
        # the real result (and the coroutine warned about never being
        # awaited). Detect coroutines and run them on a helper thread with
        # a fresh event loop. We intentionally do NOT use
        # ``run_coroutine_threadsafe(get_event_loop()).result()`` here:
        # if ToolRegistry.execute() is ever called from the same thread as
        # that running loop, blocking on ``future.result()`` deadlocks the
        # loop. Helper-thread execution is a little heavier but works in
        # both normal worker-thread dispatch and same-loop test/API calls.
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

    def _run_shell_safety_check(self, args: Dict[str, Any], runtime_mode: str) -> Optional[str]:
        """Pre-execution safety filter for ``run_shell``.

        Returns a block message string when the command should be
        refused, or ``None`` to let it proceed to the LLM safety
        supervisor + handler. Extracted from ``execute`` so the
        method itself stays under the 300-line hard gate; the checks
        themselves are unchanged.

        Layered checks (in order):
          1. Argv-level elevation pattern (``save_settings`` AND
             ``OUROBOROS_RUNTIME_MODE``, or dotted attribute path) —
             blocks in ALL modes.
          2. Light-mode argv repo-mutation indicators (git writes,
             redirection, ``rm -``, ``sed -i``, ``.write_text(`` …).
          3. v5.1.2 iter-3 file-content scan: for each interpreter
             invocation (``python evil.py`` / ``bash evil.sh`` / etc.)
             where the script file resolves inside the agent-writable
             area, read the content (bounded) and re-run the same
             elevation + light-mutation indicators against it.
          4. Protected runtime path writes (``BIBLE.md`` etc.) outside
             ``runtime_mode=pro``.
          5. ``gh repo create/delete/auth`` blanket block.
          6. Git mutative subcommand ban — write ops must go through
             ``repo_commit`` tools, never ``run_shell``.
        """
        raw_cmd = args.get("cmd", args.get("command", ""))
        if isinstance(raw_cmd, list):
            cmd_lower = " ".join(str(x) for x in raw_cmd).lower()
        else:
            cmd_lower = str(raw_cmd).lower()
        cmd_path_lower = cmd_lower.replace("\\", "/")
        while "//" in cmd_path_lower:
            cmd_path_lower = cmd_path_lower.replace("//", "/")

        # 1. Elevation pattern (all modes).
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

        # 2. Light-mode repo-mutation indicators (argv).
        if runtime_mode == "light":
            if any(ind in cmd_lower for ind in _LIGHT_MUTATION_INDICATORS):
                return (
                    "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light refuses "
                    "shell commands that look like repo mutations. "
                    "Switch to 'advanced' or 'pro' in Settings → "
                    "Behavior → Runtime Mode for write access."
                )

        # 3. File-content scan (v5.1.2 iter-3 file-based subprocess
        # bypass fix). The argv-level checks only see the literal
        # cmd; a ``python evil.py`` call has dangerous code INSIDE
        # the file, invisible to the substring filter.
        block_msg = self._scan_script_files(raw_cmd, runtime_mode, cwd=str(args.get("cwd") or ""))
        if block_msg:
            return block_msg

        # 4. Skill payload control-plane sidecar writes. This is a lexical
        # defense-in-depth layer for run_shell (the lower-level data_write /
        # file_browser guards do inode-aware checks). Shell commands are free
        # form, so we conservatively block when a write-like verb appears with
        # a protected sidecar path/name.
        if any(name in cmd_path_lower for name in (
            ".clawhub.json",
            ".ouroboroshub.json",
            "skill.openclaw.md",
            ".seed-origin",
            ".ouroboros_env",
            "node_modules",
        )) and any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS):
            return (
                "⚠️ SAFETY_VIOLATION: Shell command would modify a skill "
                "provenance / launcher seed / dependency marker (.clawhub.json, "
                ".ouroboroshub.json, SKILL.openclaw.md, .seed-origin, "
                ".ouroboros_env, node_modules). "
                "Use marketplace lifecycle flows or edit user-authored "
                "payload files instead."
            )

        # 5. Protected runtime path writes.
        for cf in _PROTECTED_RUNTIME_PATHS_LOWER:
            if cf in cmd_path_lower and any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS):
                return (
                    "⚠️ CRITICAL SAFETY_VIOLATION: Shell command would modify "
                    "a protected core/contract/release file. Protected: "
                    + ", ".join(sorted(PROTECTED_RUNTIME_PATHS))
                )

        # 6. GitHub repo create/delete/auth.
        if "gh repo create" in cmd_lower or "gh repo delete" in cmd_lower:
            return "⚠️ SAFETY_VIOLATION: Creating/deleting GitHub repositories requires admin approval."
        if "gh auth" in cmd_lower:
            return "⚠️ SAFETY_VIOLATION: Modifying GitHub authentication is not permitted."

        # 7. Git mutative ban via shell.
        if isinstance(raw_cmd, list):
            cmd_parts_for_git = [str(x) for x in raw_cmd]
        else:
            cmd_parts_for_git = cmd_lower.split()
        first_word = cmd_parts_for_git[0] if cmd_parts_for_git else ""
        is_direct_git = (first_word == "git")
        is_wrapped_git = (first_word in _SHELL_WRAPPERS and "git " in cmd_lower)
        if is_direct_git:
            subcmd = _extract_git_subcommand(cmd_parts_for_git)
            if subcmd and subcmd.lower() not in _GIT_READONLY_SUBCOMMANDS:
                return (
                    f"⚠️ GIT_VIA_SHELL_BLOCKED: `git {subcmd}` must go through "
                    "repo_commit / repo_write_commit tools which enforce pre-commit "
                    "checks. For read-only git: git_status, git_diff tools, or "
                    "run_shell with git log/show/diff/status."
                )
        if is_wrapped_git:
            _git_banned = (
                "git commit", "git push", "git add ", "git add\t",
                "git init", "git reset", "git rebase", "git merge",
                "git cherry-pick", "git branch", "git tag", "git remote",
                "git config", "git stash", "git clean", "git checkout",
                "git switch",
            )
            for banned in _git_banned:
                if banned in cmd_lower:
                    return (
                        "⚠️ GIT_VIA_SHELL_BLOCKED: git mutative commands in shell "
                        "wrappers must go through repo_commit / repo_write_commit tools."
                    )
        return None

    def _scan_script_files(self, raw_cmd: Any, runtime_mode: str, cwd: str = "") -> Optional[str]:
        """v5.1.2 iter-3 file-content scan for ``run_shell``.

        For each interpreter invocation in ``raw_cmd``, find the script
        file argument, resolve it, and check if it lives inside the
        agent-writable area (``repo_dir`` or ``drive_root``). If so,
        read content (bounded by ``_RUN_SHELL_SCAN_BYTES``) and run
        the same elevation + light-mutation indicator checks against
        it. Files outside the agent-writable area are skipped — the
        agent cannot produce them via ``data_write`` / ``repo_write``,
        so scanning would only create false positives on system
        helper scripts.
        """
        script_files = _extract_script_file_args(raw_cmd)
        if not script_files:
            return None
        try:
            repo_root_real = pathlib.Path(self._ctx.repo_dir).resolve()
        except OSError:
            repo_root_real = None
        try:
            drive_root_real = pathlib.Path(self._ctx.drive_root).resolve()
        except OSError:
            drive_root_real = None
        work_dir = pathlib.Path(self._ctx.repo_dir)
        if cwd and str(cwd).strip() not in ("", ".", "./"):
            candidate = (pathlib.Path(self._ctx.repo_dir) / str(cwd)).resolve()
            if candidate.exists() and candidate.is_dir():
                work_dir = candidate
        for script_path_str in script_files:
            try:
                raw_script_path = pathlib.Path(script_path_str)
                if not raw_script_path.is_absolute():
                    raw_script_path = work_dir / raw_script_path
                script_path = raw_script_path.resolve()
            except (OSError, ValueError):
                continue
            inside_repo = False
            inside_drive = False
            if repo_root_real is not None:
                try:
                    script_path.relative_to(repo_root_real)
                    inside_repo = True
                except ValueError:
                    pass
            if drive_root_real is not None:
                try:
                    script_path.relative_to(drive_root_real)
                    inside_drive = True
                except ValueError:
                    pass
            if not (inside_repo or inside_drive):
                continue
            try:
                if not script_path.is_file():
                    continue
                if script_path.stat().st_size > _RUN_SHELL_SCAN_BYTES:
                    continue
                content = script_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            content_lower = content.lower()
            if _detect_runtime_mode_elevation(content_lower):
                return (
                    f"⚠️ ELEVATION_BLOCKED: script file "
                    f"{script_path_str!r} (invoked via run_shell) "
                    "contains code that looks like an "
                    "OUROBOROS_RUNTIME_MODE elevation attempt "
                    "(mentions ``save_settings`` together with "
                    "``OUROBOROS_RUNTIME_MODE``, or "
                    "``ouroboros.config.save_settings`` directly). "
                    "Runtime mode is owner-controlled — change it by "
                    "stopping the agent and editing settings.json "
                    "directly, then restart."
                )
            if _mentions_skill_owner_state(content_lower):
                return (
                    f"⚠️ SKILL_STATE_WRITE_BLOCKED: script file "
                    f"{script_path_str!r} targets skill owner/review state. "
                    "Use review_skill, toggle_skill/the Skills UI, or the "
                    "desktop launcher confirmation flow."
                )
            if any(name in content_lower for name in (
                ".clawhub.json",
                ".ouroboroshub.json",
                "skill.openclaw.md",
                ".seed-origin",
                ".ouroboros_env",
                "node_modules",
            )) and any(w in content_lower for w in _SHELL_WRITE_INDICATORS):
                return (
                    f"⚠️ SKILL_STATE_WRITE_BLOCKED: script file "
                    f"{script_path_str!r} targets skill provenance / launcher "
                    "seed / dependency sidecars (.clawhub.json, .ouroboroshub.json, "
                    "SKILL.openclaw.md, .seed-origin, .ouroboros_env, node_modules). "
                    "Use marketplace lifecycle flows or edit "
                    "user-authored payload files instead."
                )
            if "state" in content_lower and "skills" in content_lower and _mentions_detached_process(content_lower):
                return (
                    f"⚠️ SKILL_STATE_WRITE_BLOCKED: script file "
                    f"{script_path_str!r} starts detached processes that target "
                    "skill state directories."
                )
            if runtime_mode == "light" and any(
                ind in content_lower for ind in _LIGHT_MUTATION_INDICATORS
            ):
                return (
                    f"⚠️ LIGHT_MODE_BLOCKED: script file "
                    f"{script_path_str!r} (invoked via run_shell) "
                    "contains repo-mutation patterns "
                    "(``.write_text(``/``.write_bytes(``/git writes/"
                    "``sed -i``/etc.). Switch to 'advanced' or 'pro' "
                    "in Settings → Behavior → Runtime Mode for write "
                    "access."
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
        protected_skill_state = {"grants.json", "review.json", "enabled.json", "clawhub.json", "deps.json"}
        for path in root.glob("*/*"):
            if path.name.lower() not in protected_skill_state:
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
            protected_skill_state = {"grants.json", "review.json", "enabled.json", "clawhub.json", "deps.json"}
            current.update(
                path for path in root.glob("*/*")
                if path.name.lower() in protected_skill_state
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

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        requested_name = str(name or "").strip()
        name = canonical_tool_name(requested_name)
        args = adapt_tool_args(requested_name, args)
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

        # --- Hardcoded Sandbox Protections ---

        # Runtime-mode gating:
        # - light blocks repo self-modification entirely;
        # - advanced may evolve the application layer but cannot edit protected
        #   core/contracts/release surfaces;
        # - pro may touch those surfaces, but the git commit path must pass the
        #   normal triad + scope review before the commit lands.
        try:
            from ouroboros.config import get_runtime_mode as _get_runtime_mode
            _runtime_mode = _get_runtime_mode()
        except Exception:
            _runtime_mode = "advanced"

        heal_no_enable = _is_heal_no_enable_context(getattr(self._ctx, "messages", None))
        if heal_no_enable:
            heal_skill = _heal_skill_name(getattr(self._ctx, "messages", None))
            heal_payload_root = _heal_payload_root(getattr(self._ctx, "messages", None))
            if name in {"data_read", "data_write"}:
                data_path = str(args.get("path", "") or "")
                if not _heal_data_path_allowed(data_path, heal_payload_root, pathlib.Path(self._ctx.drive_root)):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Heal/Fix data access is limited "
                        "to the selected skill payload under data/skills/external "
                        "data/skills/clawhub, or data/skills/ouroboroshub."
                    )
                if name == "data_write" and _heal_protected_payload_sidecar(data_path):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Heal/Fix may not edit marketplace "
                        "or official provenance sidecars (.clawhub.json, "
                        ".ouroboroshub.json, SKILL.openclaw.md, .seed-origin). "
                        "Edit the user-authored payload files instead."
                    )
            if name == "data_list":
                data_dir = str(args.get("dir", args.get("path", "")) or "")
                if not _heal_data_path_allowed(data_dir, heal_payload_root, pathlib.Path(self._ctx.drive_root)):
                    return (
                        "⚠️ HEAL_MODE_BLOCKED: Heal/Fix data listing is limited "
                        "to the selected skill payload under data/skills/external "
                        "data/skills/clawhub, or data/skills/ouroboroshub."
                    )
            if name == "review_skill" and str(args.get("skill", "") or "").strip() != heal_skill:
                return "⚠️ HEAL_MODE_BLOCKED: Heal/Fix may only review the selected skill."
            if name == "skill_preflight" and str(args.get("skill", "") or "").strip() != heal_skill:
                return "⚠️ HEAL_MODE_BLOCKED: Heal/Fix may only preflight the selected skill."
            if ext_tool or name not in _HEAL_MODE_ALLOWED_TOOLS:
                return (
                    "⚠️ HEAL_MODE_BLOCKED: Heal/Fix tasks may inspect/edit skill "
                    "payloads and run review_skill only. Shell, browser automation, "
                    "repo mutation, skill execution, extension tools, delegation, "
                    "and enable/disable flows are unavailable. Use the Skills UI "
                    "after a fresh PASS review."
                )
        if entry is None:
            if ext_tool and callable(ext_tool.get("handler")):
                return self._dispatch_extension_tool(name, ext_tool, args)
            return f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._entries.keys()))}"
        _REPO_MUTATION_TOOLS = frozenset(
            {
                "repo_write",
                "repo_write_commit",
                "repo_commit",
                "str_replace_editor",
                "claude_code_edit",
                "revert_commit",
                "pull_from_remote",
                "restore_to_head",
                "rollback_to_target",
                "promote_to_stable",
                # PR integration tools — they check out branches,
                # cherry-pick, and stage merges. All of them mutate
                # the local working tree / refs and must not run
                # when ``runtime_mode=light``.
                "fetch_pr_ref",
                "create_integration_branch",
                "cherry_pick_pr_commits",
                "stage_adaptations",
                "stage_pr_merge",
            }
        )
        if _runtime_mode == "light" and name in _REPO_MUTATION_TOOLS:
            return (
                "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light disables "
                "repo self-modification. Tool "
                f"{name!r} would mutate the Ouroboros repository. "
                "Switch to 'advanced' or 'pro' in Settings → Behavior "
                "→ Runtime Mode to re-enable self-modification."
            )

        protected_write_paths = []
        if name in ("repo_write_commit", "repo_write", "str_replace_editor"):
            if name in ("repo_write_commit", "repo_write"):
                maybe_path = str(args.get("path", "") or "")
                if maybe_path:
                    protected_write_paths.append(maybe_path)
                for f_entry in args.get("files") or []:
                    if isinstance(f_entry, dict):
                        protected_write_paths.append(str(f_entry.get("path", "") or ""))
            elif name == "str_replace_editor":
                protected_write_paths.append(str(args.get("path", "") or ""))
            protected_matches = protected_paths_in(protected_write_paths)
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

        # --- LLM Safety Supervisor ---
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
        try:
            result = entry.handler(self._ctx, **args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"
        if name == "run_shell":
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

        # Revert protected files after claude_code_edit unless pro mode is
        # active; pro-mode commits still require the normal commit review later.
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

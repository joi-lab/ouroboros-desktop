"""Shared shell guard helpers for process tools."""

from __future__ import annotations

import pathlib
import re
from typing import Any, Dict, List

from ouroboros.runtime_mode_policy import FROZEN_CONTRACT_PATH_PREFIXES, PROTECTED_RUNTIME_PATHS
from ouroboros.tools.shell_parse import (
    EMBEDDED_ABSOLUTE_PATH_RE,
    shell_argv,
    shell_argv_with_inline,
    shell_command_string,
    strip_leading_env_assignments,
    unwrap_env_argv,
)

PROTECTED_RUNTIME_PATHS_LOWER = frozenset(
    p.lower() for p in PROTECTED_RUNTIME_PATHS
) | frozenset(prefix.lower() for prefix in FROZEN_CONTRACT_PATH_PREFIXES)

SHELL_WRITE_INDICATORS = (
    "rm ", "rm\t", ">", "sed -i", "tee ", "truncate",
    "mv ", "cp ", "chmod ", "chown ", "unlink ", "delete", "trash",
    "rsync ", "write_text", "open(", ".write(", ".writelines(",
    "os.remove(", "os.unlink(", "os.mkdir(", "os.makedirs(", "sort -o",
)

LIGHT_SHELL_WRITER_COMMANDS = frozenset({
    "chmod", "chown", "cp", "gunzip", "gzip", "ln", "mkdir", "mv",
    "perl", "rm", "ruby", "sed", "sort", "tar", "touch", "truncate", "uniq", "unzip",
})

INTERPRETER_WRITE_RE = re.compile(
    r"""(?is)(?:\.write\(|write_text\(|write_bytes\(|fs\.write|fs\.append|"""
    r"""createwritestream|unlink\(|rename\(|mkdir\(|rmtree\(|remove\(|"""
    r"""open\s*\([^)]*,\s*['"][^'"]*[wax+])"""
)
EMBEDDED_RELATIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\.\.?/)+[^\s'\"\\),;\]]+")
EMBEDDED_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[A-Za-z]:[\\/][^\s'\"),;\]]+|\\\\[^\s'\"),;\]]+)"
)


def _path_inside(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        pathlib.Path(path).resolve(strict=False).relative_to(pathlib.Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def runtime_data_write_targets(
    raw_cmd: Any,
    *,
    drive_root: pathlib.Path,
    work_dir: pathlib.Path,
    allowed_roots: List[pathlib.Path],
) -> List[str]:
    """Find write-like path mentions under runtime data but outside task artifact roots."""

    try:
        drive = pathlib.Path(drive_root).resolve(strict=False)
        cwd = pathlib.Path(work_dir).resolve(strict=False)
    except Exception:
        return []
    allowed = [pathlib.Path(root).resolve(strict=False) for root in allowed_roots]
    try:
        home = pathlib.Path.home().resolve(strict=False)
    except Exception:
        home = pathlib.Path("~").expanduser()
    blocked: List[str] = []
    for token in shell_argv_with_inline(raw_cmd):
        text = str(token or "")
        expanded_texts = {
            text,
            text.replace("$OUROBOROS_DATA_DIR", str(drive))
            .replace("${OUROBOROS_DATA_DIR}", str(drive))
            .replace("%OUROBOROS_DATA_DIR%", str(drive)),
            text.replace("$HOME", str(home)).replace("${HOME}", str(home)).replace("%USERPROFILE%", str(home)),
            text.replace("~/", f"{home}/"),
        }
        candidates: List[str] = []
        for expanded in expanded_texts:
            if expanded.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", expanded):
                candidates.append(expanded)
            candidates.extend(EMBEDDED_ABSOLUTE_PATH_RE.findall(expanded))
            candidates.extend(EMBEDDED_WINDOWS_ABSOLUTE_PATH_RE.findall(expanded))
            candidates.extend(EMBEDDED_RELATIVE_PATH_RE.findall(expanded))
        for candidate in candidates:
            candidate_variants = {candidate}
            if "\\\\" in candidate:
                candidate_variants.add(candidate.replace("\\\\", "\\"))
            for candidate_text in candidate_variants:
                try:
                    raw_path = pathlib.Path(candidate_text).expanduser()
                    path = raw_path.resolve(strict=False) if raw_path.is_absolute() else (cwd / raw_path).resolve(strict=False)
                except Exception:
                    continue
                if not _path_inside(path, drive) or any(_path_inside(path, root) for root in allowed):
                    continue
                rendered = str(path)
                if rendered not in blocked:
                    blocked.append(rendered)
    return blocked


def process_shell_guard_args(name: str, args: Dict[str, Any], *, ctx: Any = None, runtime_mode: str = "") -> Dict[str, Any]:
    """Normalize process-tool arguments into the command shape inspected by shell guards."""

    if name == "run_script":
        interpreter = str(args.get("interpreter") or "python3").strip() or "python3"
        script = str(args.get("script") or "")
        cwd = args.get("cwd", "")
        if (
            not str(cwd or "").strip()
            and ctx is not None
            and str(runtime_mode or "").strip() == "light"
            and not bool(getattr(ctx, "is_workspace_mode", lambda: False)())
        ):
            try:
                cwd = str(ctx.task_drive_root())
            except Exception:
                cwd = ""
        return {
            "cmd": [interpreter, "-c", script],
            "cwd": cwd,
            "__tool_name": name,
        }
    return {**args, "__tool_name": name}


def parse_porcelain_paths(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in str(output or "").splitlines():
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


def _candidate_path_inside(root: pathlib.Path, work_dir: pathlib.Path, path_text: str) -> bool:
    text = str(path_text or "").strip()
    if not text or text in {"-", "--"}:
        return False
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


def repo_target_mentioned(argv: List[str], *, repo_dir: pathlib.Path, cwd: str = "") -> bool:
    work_dir = pathlib.Path(repo_dir)
    if cwd and str(cwd).strip() not in ("", ".", "./"):
        try:
            work_dir = (pathlib.Path(repo_dir) / str(cwd)).resolve(strict=False)
        except OSError:
            pass
    return any(_candidate_path_inside(pathlib.Path(repo_dir), work_dir, token) for token in argv[1:])


def writer_target_tokens(argv: List[str]) -> List[str]:
    if not argv:
        return []
    cmd = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")
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


def writer_targets_repo(argv: List[str], *, repo_dir: pathlib.Path, cwd: str = "") -> bool:
    return repo_target_mentioned([argv[0], *writer_target_tokens(argv)], repo_dir=repo_dir, cwd=cwd)


def shell_writer_targets_protected(raw_cmd: Any) -> bool:
    argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
    if not argv:
        return False
    executable = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")
    if executable in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        return bool(inline and shell_writer_targets_protected(inline))
    if executable not in LIGHT_SHELL_WRITER_COMMANDS:
        return False
    target_text = " ".join(writer_target_tokens(argv)).replace("\\", "/").lower()
    return bool(target_text and any(cf in target_text for cf in PROTECTED_RUNTIME_PATHS_LOWER))


def light_shell_repo_mutation(
    raw_cmd: Any,
    *,
    repo_dir: pathlib.Path,
    cwd: str = "",
    detect_interpreter_inline: bool = False,
) -> bool:
    """Detect simple shell writer commands that target the repo in light mode."""
    argv = shell_argv(raw_cmd)
    if not argv:
        return False
    cmd_lower = " ".join(argv).lower()

    unwrapped = unwrap_env_argv(argv)
    if unwrapped != argv:
        return light_shell_repo_mutation(
            unwrapped,
            repo_dir=repo_dir,
            cwd=cwd,
            detect_interpreter_inline=detect_interpreter_inline,
        )
    argv = strip_leading_env_assignments(argv)
    if not argv:
        return False
    executable = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")

    if executable in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        if inline:
            return light_shell_repo_mutation(
                inline,
                repo_dir=repo_dir,
                cwd=cwd,
                detect_interpreter_inline=detect_interpreter_inline,
            )

    if executable in LIGHT_SHELL_WRITER_COMMANDS and writer_targets_repo(argv, repo_dir=repo_dir, cwd=cwd):
        return True

    if detect_interpreter_inline and executable in {"python", "python3", "node", "ruby", "perl", "php"}:
        work_dir = pathlib.Path(cwd).expanduser() if str(cwd or "").strip() else pathlib.Path(repo_dir)
        if not work_dir.is_absolute():
            work_dir = pathlib.Path(repo_dir) / work_dir
        try:
            work_dir = work_dir.resolve(strict=False)
            work_dir.relative_to(pathlib.Path(repo_dir).resolve(strict=False))
            work_dir_inside_repo = True
        except (OSError, ValueError):
            work_dir_inside_repo = False
        inline = shell_command_string(argv) or " ".join(argv[1:])
        if INTERPRETER_WRITE_RE.search(inline):
            if work_dir_inside_repo:
                return True
            repo_text = str(pathlib.Path(repo_dir).resolve(strict=False)).replace("\\", "/")
            if repo_text and repo_text in inline.replace("\\", "/"):
                return True
        return False

    if any(ind in cmd_lower for ind in (" > ", " >> ", " | tee ")):
        return repo_target_mentioned(argv, repo_dir=repo_dir, cwd=cwd)
    return False

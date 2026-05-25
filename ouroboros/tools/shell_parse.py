"""Small shell argv parsing helpers shared by tool guardrails."""

from __future__ import annotations

import pathlib
import re
import shlex
from typing import Any, List


EMBEDDED_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])/[^\s'\"\\),;\]]+")
_SHELLS = {"sh", "bash", "zsh"}


def shell_argv(raw_cmd: Any) -> List[str]:
    if isinstance(raw_cmd, list):
        return [str(x) for x in raw_cmd if str(x).strip()]
    try:
        return [str(x) for x in shlex.split(str(raw_cmd or "")) if str(x).strip()]
    except ValueError:
        return [str(x) for x in str(raw_cmd or "").split() if str(x).strip()]


def unwrap_env_argv(argv: List[str]) -> List[str]:
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
            return shell_argv(argv[idx + 1])
        if token.startswith("--split-string="):
            return shell_argv(token.split("=", 1)[1])
        if token in options_with_arg:
            idx += 2
            continue
        if (
            any(token.startswith(prefix + "=") for prefix in ("--unset", "--chdir", "--argv0"))
            or token.startswith("-")
            or ("=" in token and not token.startswith("="))
        ):
            idx += 1
            continue
        break
    return argv[idx:] if idx < len(argv) else []


def strip_leading_env_assignments(argv: List[str]) -> List[str]:
    idx = 0
    while idx < len(argv) and "=" in argv[idx] and not argv[idx].startswith("="):
        idx += 1
    return argv[idx:]


def sudo_noninteractive_violation(argv: List[str]) -> bool:
    if argv and pathlib.PurePath(argv[0]).name.lower() in _SHELLS:
        inline = shell_command_string(argv)
        if inline:
            return sudo_noninteractive_violation(shell_argv(inline))
    for idx, token in enumerate(argv):
        command_name = pathlib.PurePath(token).name.lower()
        if command_name == "sudoedit":
            return True
        if command_name != "sudo":
            continue
        has_noninteractive = False
        for option in _sudo_option_tokens(argv[idx + 1 :]):
            if option == "-S" or (option.startswith("-") and not option.startswith("--") and "S" in option[1:]):
                return True
            if option == "-n" or (option.startswith("-") and not option.startswith("--") and "n" in option[1:]):
                has_noninteractive = True
            if option.startswith("--non-interactive"):
                has_noninteractive = True
        if not has_noninteractive:
            return True
    return False


def shell_command_string(argv: List[str]) -> str:
    for idx, arg in enumerate(argv[1:], start=1):
        if arg == "-c" or (arg.startswith("-") and not arg.startswith("--") and "c" in arg[1:]):
            return argv[idx + 1] if idx + 1 < len(argv) else ""
    return ""


def shell_argv_with_inline(raw_cmd: Any) -> List[str]:
    argv = shell_argv(raw_cmd)
    if argv and pathlib.PurePath(argv[0]).name.lower() in _SHELLS:
        inline = shell_command_string(argv)
        if inline:
            return argv + shell_argv(inline)
    return argv


def _sudo_option_tokens(rest: List[str]) -> List[str]:
    options: List[str] = []
    options_with_arg = {
        "-A", "-a", "-b", "-C", "-c", "-D", "-g", "-h", "-p", "-R", "-r", "-T", "-t", "-U", "-u",
        "--askpass", "--auth-type", "--background", "--chdir", "--close-from", "--command-timeout",
        "--context", "--group", "--host", "--login-class", "--prompt", "--role", "--type", "--user",
        "--other-user",
    }
    idx = 0
    while idx < len(rest):
        token = rest[idx]
        if token == "--":
            break
        if not token.startswith("-") or token == "-":
            break
        options.append(token)
        idx += 2 if token in options_with_arg else 1
    return options

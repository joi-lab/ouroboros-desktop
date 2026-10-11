"""Small subprocess helpers for benchmark adapters."""

from __future__ import annotations

import json
import pathlib
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandResult:
    cmd: list[str]
    returncode: int
    stdout: str
    stderr: str


def run_logged(cmd: list[str], *, cwd: pathlib.Path | None = None, timeout: int | None = None) -> CommandResult:
    proc = subprocess.run(
        [str(part) for part in cmd],
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return CommandResult([str(part) for part in cmd], proc.returncode, proc.stdout or "", proc.stderr or "")


def write_command_result(path: pathlib.Path, result: CommandResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "cmd": result.cmd,
                "returncode": result.returncode,
                "stdout_chars": len(result.stdout),
                "stderr_chars": len(result.stderr),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

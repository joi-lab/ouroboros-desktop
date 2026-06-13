"""Terminal-Bench custom agent bridge for Ouroboros CLI."""

from __future__ import annotations

import os
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # Terminal-Bench is an optional benchmark dependency.
    from terminal_bench.agents.base_agent import AgentResult, BaseAgent
    from terminal_bench.agents.failure_mode import FailureMode
except Exception:  # pragma: no cover - exercised only when tbench is installed.
    AgentResult = None  # type: ignore[assignment]
    BaseAgent = object  # type: ignore[assignment]
    FailureMode = None  # type: ignore[assignment]


def _git_dirty_reason(workspace: Path) -> str:
    """Return a failure reason for dirty git workspaces; allow non-git mounts."""

    probe = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip().lower() != "true":
        return ""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        capture_output=True,
        text=True,
    )
    if status.returncode != 0:
        return "git_status_failed"
    if status.stdout.strip():
        return "dirty_git_workspace"
    return ""


class OuroborosTerminalBenchAgent(BaseAgent):  # type: ignore[misc, valid-type]
    """Bridge Terminal-Bench to a mounted workspace served by Ouroboros CLI."""

    def __init__(
        self,
        workspace_root: str = "",
        model_name: str = "ouroboros-cli",
        timeout_sec: int = 7200,
        cli: str = "",
        **kwargs: Any,
    ) -> None:
        try:
            super().__init__(**kwargs)
        except TypeError:
            super().__init__()
        self.workspace_root = workspace_root or os.environ.get("OUROBOROS_TBENCH_WORKSPACE_ROOT", "")
        self.model_name = model_name
        self.timeout_sec = int(timeout_sec)
        self.cli = cli or os.environ.get("OUROBOROS_CLI", "")

    @staticmethod
    def name() -> str:
        return "Ouroboros CLI"

    def perform_task(self, task_description: str, session: Any, logging_dir: Path | None = None) -> Any:
        workspace = Path(self.workspace_root).expanduser().resolve(strict=False) if self.workspace_root else None
        if workspace is None or not workspace.is_dir():
            if AgentResult is None or FailureMode is None:
                return {"success": False, "output": "workspace_root must point to the mounted Terminal-Bench task workspace"}
            return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
        dirty_reason = _git_dirty_reason(workspace)
        if dirty_reason:
            summary = {
                "cmd": [],
                "workspace_root": str(workspace),
                "returncode": None,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "timeout_sec": self.timeout_sec,
                "failure_mode": dirty_reason,
            }
            if logging_dir is not None:
                Path(logging_dir).mkdir(parents=True, exist_ok=True)
                (Path(logging_dir) / "ouroboros-agent-result.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            if AgentResult is None or FailureMode is None:
                return {"success": False, "output": f"refusing dirty git workspace: {dirty_reason}"}
            return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
        prompt = self._render_instruction(task_description) if hasattr(self, "_render_instruction") else task_description
        cli_prefix = shlex.split(self.cli) if self.cli else [sys.executable, "-m", "ouroboros.cli"]
        cmd = [
            *cli_prefix,
            "run",
            "--workspace",
            str(workspace),
            "--memory-mode",
            "empty",
            "--timeout",
            str(self.timeout_sec),
            prompt,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_sec + 60)
            final = result.stdout.strip()
            summary = {
                "cmd": cmd,
                "workspace_root": str(workspace),
                "returncode": result.returncode,
                "stdout_chars": len(result.stdout or ""),
                "stderr_chars": len(result.stderr or ""),
                "final_text_empty": not bool(final),
                "timeout_sec": self.timeout_sec,
                "failure_mode": "",
            }
            if logging_dir is not None:
                Path(logging_dir).mkdir(parents=True, exist_ok=True)
                (Path(logging_dir) / "ouroboros.stdout").write_text(result.stdout, encoding="utf-8")
                (Path(logging_dir) / "ouroboros.stderr").write_text(result.stderr, encoding="utf-8")
            if result.returncode != 0:
                summary["failure_mode"] = "non_zero_exit"
                if logging_dir is not None:
                    (Path(logging_dir) / "ouroboros-agent-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                if AgentResult is None or FailureMode is None:
                    return {"success": False, "output": result.stderr or result.stdout or f"exit {result.returncode}"}
                return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
            if not final:
                summary["failure_mode"] = "empty_final_text"
                if logging_dir is not None:
                    (Path(logging_dir) / "ouroboros-agent-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                if AgentResult is None or FailureMode is None:
                    return {"success": False, "output": "ouroboros run produced empty final text"}
                return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
            if final:
                try:
                    submit_command = f"/submit {shlex.quote(final)}"
                    if hasattr(session, "send_keys"):
                        session.send_keys([submit_command, "Enter"], block=True)
                    elif hasattr(session, "send_command"):
                        session.send_command(submit_command)
                    elif hasattr(session, "run"):
                        session.run(submit_command)
                    else:
                        raise RuntimeError("Terminal-Bench session has no supported submit method")
                except Exception as exc:
                    summary["failure_mode"] = "submit_failed"
                    summary["submit_error"] = f"{type(exc).__name__}: {exc}"
                    if logging_dir is not None:
                        (Path(logging_dir) / "ouroboros-agent-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                    if AgentResult is None or FailureMode is None:
                        return {"success": False, "output": summary["submit_error"]}
                    return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
            summary["failure_mode"] = "none"
            if logging_dir is not None:
                (Path(logging_dir) / "ouroboros-agent-result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
            if AgentResult is None or FailureMode is None:
                return {"success": True, "output": final}
            return AgentResult(failure_mode=FailureMode.NONE)
        except subprocess.TimeoutExpired as exc:
            stdout = (exc.stdout or "").decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else str(exc.stdout or "")
            stderr = (exc.stderr or "").decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else str(exc.stderr or "")
            if logging_dir is not None:
                Path(logging_dir).mkdir(parents=True, exist_ok=True)
                (Path(logging_dir) / "ouroboros.stdout").write_text(stdout, encoding="utf-8")
                (Path(logging_dir) / "ouroboros.stderr").write_text(stderr, encoding="utf-8")
                (Path(logging_dir) / "ouroboros-agent-result.json").write_text(json.dumps({
                    "cmd": cmd,
                    "workspace_root": str(workspace),
                    "returncode": 124,
                    "stdout_chars": len(stdout),
                    "stderr_chars": len(stderr),
                    "timeout_sec": self.timeout_sec,
                    "failure_mode": "timeout",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            if AgentResult is None or FailureMode is None:
                return {"success": False, "output": f"ouroboros cli timed out after {self.timeout_sec}s", "timeout": True}
            return AgentResult(failure_mode=FailureMode.AGENT_TIMEOUT)


__all__ = ["OuroborosTerminalBenchAgent"]

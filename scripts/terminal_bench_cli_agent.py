"""Terminal-Bench custom agent bridge for Ouroboros CLI."""

from __future__ import annotations

import os
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


class OuroborosTerminalBenchAgent(BaseAgent):  # type: ignore[misc, valid-type]
    """Bridge Terminal-Bench to a mounted workspace served by Ouroboros CLI."""

    def __init__(
        self,
        workspace_root: str = "",
        model_name: str = "ouroboros-cli",
        timeout_sec: int = 7200,
        **kwargs: Any,
    ) -> None:
        try:
            super().__init__(**kwargs)
        except TypeError:
            super().__init__()
        self.workspace_root = workspace_root or os.environ.get("OUROBOROS_TBENCH_WORKSPACE_ROOT", "")
        self.model_name = model_name
        self.timeout_sec = int(timeout_sec)

    @staticmethod
    def name() -> str:
        return "Ouroboros CLI"

    def perform_task(self, task_description: str, session: Any, logging_dir: Path | None = None) -> Any:
        workspace = Path(self.workspace_root).expanduser().resolve(strict=False) if self.workspace_root else None
        if workspace is None or not workspace.is_dir():
            if AgentResult is None or FailureMode is None:
                return {"success": False, "output": "workspace_root must point to the mounted Terminal-Bench task workspace"}
            return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
        git_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True, timeout=10)
        git_status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if git_head.returncode != 0 or git_status.returncode != 0 or git_status.stdout.strip():
            if AgentResult is None or FailureMode is None:
                return {"success": False, "output": "workspace_root must be a clean git checkout"}
            return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
        prompt = self._render_instruction(task_description) if hasattr(self, "_render_instruction") else task_description
        cmd = [
            sys.executable,
            "-m",
            "ouroboros.cli",
            "run",
            "--workspace",
            str(workspace),
            "--memory-mode",
            "empty",
            prompt,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_sec)
            final = result.stdout.strip()
            if logging_dir is not None:
                Path(logging_dir).mkdir(parents=True, exist_ok=True)
                (Path(logging_dir) / "ouroboros.stdout").write_text(result.stdout, encoding="utf-8")
                (Path(logging_dir) / "ouroboros.stderr").write_text(result.stderr, encoding="utf-8")
            if result.returncode != 0:
                if AgentResult is None or FailureMode is None:
                    return {"success": False, "output": result.stderr or result.stdout or f"exit {result.returncode}"}
                return AgentResult(failure_mode=FailureMode.UNKNOWN_AGENT_ERROR)
            if final:
                try:
                    session.run(f"/submit {final}")
                except Exception:
                    pass
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
            if AgentResult is None or FailureMode is None:
                return {"success": False, "output": f"ouroboros cli timed out after {self.timeout_sec}s", "timeout": True}
            return AgentResult(failure_mode=FailureMode.AGENT_TIMEOUT)


__all__ = ["OuroborosTerminalBenchAgent"]

"""
A2A — File-based TaskStore.

Stores each A2A task as a JSON file in ~/Ouroboros/data/a2a_tasks/.
Uses atomic writes (write -> rename) consistent with supervisor/state.py.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from a2a.server.tasks import TaskStore
from a2a.types import Task

log = logging.getLogger("a2a-server")


class FileTaskStore(TaskStore):
    """File-based A2A task persistence."""

    def __init__(self, data_dir: pathlib.Path, ttl_hours: int = 24):
        self._dir = data_dir / "a2a_tasks"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._ttl_hours = ttl_hours

    def _task_path(self, task_id: str) -> pathlib.Path:
        safe_id = task_id.replace("/", "_").replace("..", "_")
        return self._dir / f"{safe_id}.json"

    async def get(self, task_id: str, **kwargs) -> Optional[Task]:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            return Task.model_validate(data)
        except Exception:
            log.warning("Failed to read task %s", task_id, exc_info=True)
            return None

    async def save(self, task: Task, **kwargs) -> None:
        path = self._task_path(task.id)
        data = task.model_dump(mode="json", exclude_none=True)
        content = json.dumps(data, ensure_ascii=False, indent=2)
        # Atomic write: tmp -> rename
        tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex[:8]}")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(str(tmp), str(path))

    async def delete(self, task_id: str, **kwargs) -> None:
        path = self._task_path(task_id)
        try:
            path.unlink(missing_ok=True)
        except Exception:
            log.warning("Failed to delete task %s", task_id, exc_info=True)

    async def cleanup_expired(self) -> int:
        """Remove tasks in terminal states older than TTL. Returns count removed."""
        terminal = {"completed", "failed", "canceled", "rejected"}
        cutoff = time.time() - self._ttl_hours * 3600
        removed = 0
        try:
            for path in self._dir.glob("*.json"):
                if path.stat().st_mtime > cutoff:
                    continue
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    state = (data.get("status") or {}).get("state", "")
                    if state in terminal:
                        path.unlink(missing_ok=True)
                        removed += 1
                except Exception:
                    continue
        except Exception:
            log.warning("Task cleanup error", exc_info=True)
        return removed

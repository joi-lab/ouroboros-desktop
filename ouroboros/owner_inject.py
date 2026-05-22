"""Per-task owner-message mailboxes for running worker tasks."""
import json
import logging
import pathlib
import uuid
from typing import List, Optional

from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

_MAILBOX_DIR = "memory/owner_mailbox"


def _mailbox_path(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return drive_root / _MAILBOX_DIR / f"{task_id}.jsonl"


def write_owner_message(
    drive_root: pathlib.Path,
    text: str,
    task_id: str,
    msg_id: Optional[str] = None,
) -> None:
    """Write an owner message to a specific task's mailbox."""
    path = _mailbox_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "msg_id": msg_id or uuid.uuid4().hex,
        "ts": utc_now_iso(),
        "text": text,
    }, ensure_ascii=False)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        log.debug("Failed to write owner message for task %s", task_id, exc_info=True)


def drain_owner_messages(
    drive_root: pathlib.Path,
    task_id: str,
    seen_ids: Optional[set] = None,
) -> List[str]:
    """Read unseen message texts for one task, updating seen_ids for dedup."""
    path = _mailbox_path(drive_root, task_id)
    if not path.exists():
        return []
    if seen_ids is None:
        seen_ids = set()
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        messages = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                mid = entry.get("msg_id", "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                text = entry.get("text", "")
                if text:
                    messages.append(text)
            except Exception:
                log.debug("Malformed mailbox line for task %s", task_id, exc_info=True)
        return messages
    except Exception:
        log.debug("Failed to read mailbox for task %s", task_id, exc_info=True)
        return []


def cleanup_task_mailbox(drive_root: pathlib.Path, task_id: str) -> None:
    """Remove a task's mailbox file after task completes."""
    path = _mailbox_path(drive_root, task_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        log.debug("Failed to cleanup mailbox for task %s", task_id, exc_info=True)

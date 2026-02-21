"""
Supervisor — Message Bus & Formatting.

Replaces TelegramClient with a local Queue-based message bus that connects 
the Flet UI with the Agent Supervisor.
"""

from __future__ import annotations

import datetime
import logging
import queue
import re
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import load_state, save_state, append_jsonl

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DRIVE_ROOT = None  # pathlib.Path
TOTAL_BUDGET_LIMIT: float = 0.0
BUDGET_REPORT_EVERY_MESSAGES: int = 10
_TG: Optional["LocalChatBridge"] = None


def init(drive_root, total_budget_limit: float, budget_report_every: int,
         tg_client: "LocalChatBridge") -> None:
    global DRIVE_ROOT, TOTAL_BUDGET_LIMIT, BUDGET_REPORT_EVERY_MESSAGES, _TG
    DRIVE_ROOT = drive_root
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BUDGET_REPORT_EVERY_MESSAGES = budget_report_every
    _TG = tg_client


def get_tg() -> "LocalChatBridge":
    assert _TG is not None, "telegram.init() not called"
    return _TG


# ---------------------------------------------------------------------------
# LocalChatBridge
# ---------------------------------------------------------------------------

class LocalChatBridge:
    """Local drop-in replacement for TelegramClient using queue.Queue."""
    
    def __init__(self, token: str = "local"):
        self._inbox = queue.Queue()   # user -> agent
        self._outbox = queue.Queue()  # agent -> UI
        self._log_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._update_counter = 0

    def get_updates(self, offset: int, timeout: int = 10) -> List[Dict[str, Any]]:
        """Simulate Telegram long-polling by blocking on the inbox queue."""
        try:
            # Block until message or timeout
            msg_text = self._inbox.get(timeout=timeout)
            
            # Pack it like a Telegram update so the rest of the code doesn't break
            self._update_counter = max(offset, self._update_counter + 1)
            return [{
                "update_id": self._update_counter,
                "message": {
                    "chat": {"id": 1},  # Hardcoded chat_id for local
                    "from": {"id": 1},  # Hardcoded user_id for local
                    "text": msg_text,
                }
            }]
        except queue.Empty:
            return []

    def send_message(self, chat_id: int, text: str, parse_mode: str = "") -> Tuple[bool, str]:
        """Put a message in the outbox for the UI to consume."""
        # Clean up some markdown for the simple UI
        clean_text = _strip_markdown(text) if not parse_mode else text
        
        self._outbox.put({
            "type": "text",
            "content": clean_text,
            "markdown": bool(parse_mode)
        })
        return True, "ok"

    def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send typing indicator to UI."""
        self._outbox.put({
            "type": "action",
            "content": action
        })
        return True

    def send_photo(self, chat_id: int, photo_bytes: bytes,
                   caption: str = "") -> Tuple[bool, str]:
        """Send photo to UI."""
        self._outbox.put({
            "type": "photo",
            "content": photo_bytes,
            "caption": caption
        })
        return True, "ok"

    def download_file_base64(self, file_id: str, max_bytes: int = 10_000_000) -> Tuple[Optional[str], str]:
        """Local app doesn't currently support file uploads from UI to agent."""
        return None, ""
        
    # Log streaming
    def push_log(self, event: dict):
        """Called by append_jsonl hook to stream log events to the UI."""
        try:
            self._log_queue.put_nowait(event)
        except queue.Full:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._log_queue.put_nowait(event)
            except queue.Full:
                pass

    def ui_poll_logs(self) -> list:
        """Called by the Flet UI to drain pending log events."""
        batch = []
        for _ in range(50):
            try:
                batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        return batch

    # UI hooks
    def ui_send(self, text: str):
        """Called by the Flet UI to send a message to the agent."""
        self._inbox.put(text)
        
    def ui_receive(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        """Called by the Flet UI to check for new messages from the agent."""
        try:
            return self._outbox.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def split_telegram(text: str, limit: int = 3800) -> List[str]:
    chunks: List[str] = []
    s = text
    while len(s) > limit:
        cut = s.rfind("\n", 0, limit)
        if cut < 100:
            cut = limit
        chunks.append(s[:cut])
        s = s[cut:]
    chunks.append(s)
    return chunks


def _strip_markdown(text: str) -> str:
    """Strip all markdown formatting markers, leaving only plain text."""
    # Fenced code blocks (keep content)
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    # Inline code (keep content)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Bold+italic (***text***)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    # Bold (**text**)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    # Italic (*text* or _text_)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    # Strikethrough (~~text~~)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    # Links [text](url) -> text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Headers (# text -> text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # List markers (- or * at start of line, keep bullet but remove markdown)
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)
    # Clean up any remaining stray markdown markers
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = text.replace("`", "")
    return text


def _send_markdown_telegram(chat_id: int, text: str) -> Tuple[bool, str]:
    """Send markdown text (for local UI, we just pass the text and flag it)."""
    tg = get_tg()
    if not text:
        return False, "empty"
    return tg.send_message(chat_id, text, parse_mode="markdown")


# ---------------------------------------------------------------------------
# Budget + logging
# ---------------------------------------------------------------------------

def _format_budget_line(st: Dict[str, Any]) -> str:
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    pct = (spent / total * 100.0) if total > 0 else 0.0
    sha = (st.get("current_sha") or "")[:8]
    branch = st.get("current_branch") or "?"
    return f"—\nBudget: ${spent:.4f} / ${total:.2f} ({pct:.2f}%) | {branch}@{sha}"


def budget_line(force: bool = False) -> str:
    try:
        st = load_state()
        every = max(1, int(BUDGET_REPORT_EVERY_MESSAGES))
        if force:
            st["budget_messages_since_report"] = 0
            save_state(st)
            return _format_budget_line(st)

        counter = int(st.get("budget_messages_since_report") or 0) + 1
        if counter < every:
            st["budget_messages_since_report"] = counter
            save_state(st)
            return ""

        st["budget_messages_since_report"] = 0
        save_state(st)
        return _format_budget_line(st)
    except Exception:
        log.debug("Suppressed exception in budget_line", exc_info=True)
        return ""


def log_chat(direction: str, chat_id: int, user_id: int, text: str) -> None:
    if DRIVE_ROOT:
        append_jsonl(DRIVE_ROOT / "logs" / "chat.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": load_state().get("session_id"),
            "direction": direction,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": text,
        })


def send_with_budget(chat_id: int, text: str, log_text: Optional[str] = None,
                     force_budget: bool = False, fmt: str = "",
                     is_progress: bool = False) -> None:
    st = load_state()
    owner_id = int(st.get("owner_id") or 0)
    
    if is_progress and DRIVE_ROOT:
        append_jsonl(DRIVE_ROOT / "logs" / "progress.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "direction": "out", "chat_id": chat_id, "user_id": owner_id,
            "text": text if log_text is None else log_text,
        })
    else:
        log_chat("out", chat_id, owner_id, text if log_text is None else log_text)
        
    budget = budget_line(force=force_budget)
    _text = str(text or "")
    if not budget:
        if _text.strip() in ("", "\u200b"):
            return
        full = _text
    else:
        base = _text.rstrip()
        if base in ("", "\u200b"):
            full = budget
        else:
            full = base + "\n\n" + budget

    if fmt == "markdown":
        ok, err = _send_markdown_telegram(chat_id, full)
        return

    tg = get_tg()
    for part in split_telegram(full):
        tg.send_message(chat_id, part)

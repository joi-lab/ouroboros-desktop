"""
Supervisor — Message Bus & Formatting.

Queue-based message bus that connects the Web UI, Telegram, and the Agent
Supervisor.
"""

from __future__ import annotations

import base64
import datetime
import logging
import mimetypes
import queue
import re
import threading
from typing import Any, Dict, List, Optional, Tuple

import requests

from supervisor.state import append_jsonl, load_state, save_state

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DATA_DIR = None  # pathlib.Path
TOTAL_BUDGET_LIMIT: float = 0.0
BUDGET_REPORT_EVERY_MESSAGES: int = 10
_BRIDGE: Optional["LocalChatBridge"] = None


def init(
    drive_root,
    total_budget_limit: float,
    budget_report_every: int,
    chat_bridge: "LocalChatBridge",
) -> None:
    global DATA_DIR, TOTAL_BUDGET_LIMIT, BUDGET_REPORT_EVERY_MESSAGES, _BRIDGE
    DATA_DIR = drive_root
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BUDGET_REPORT_EVERY_MESSAGES = budget_report_every
    _BRIDGE = chat_bridge


def get_bridge() -> "LocalChatBridge":
    assert _BRIDGE is not None, "message_bus.init() not called"
    return _BRIDGE


# ---------------------------------------------------------------------------
# LocalChatBridge
# ---------------------------------------------------------------------------

class LocalChatBridge:
    """Local message bus using queue.Queue."""

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self._inbox = queue.Queue()   # user -> agent
        self._outbox = queue.Queue()  # agent -> UI
        self._log_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._update_counter = 0
        self._broadcast_fn = None  # set by server.py for WebSocket streaming
        # A2A response subscriptions: {subscription_id: (chat_id, callback)}
        self._response_subs: Dict[str, tuple] = {}
        self._response_subs_lock = threading.Lock()
        self._telegram_bot_token = ""
        self._telegram_chat_id: int = 0
        self._telegram_active_chat_id: int = 0
        self._telegram_poll_thread: Optional[threading.Thread] = None
        self._telegram_stop = threading.Event()
        if settings:
            self.configure_from_settings(settings)

    def get_updates(self, offset: int, timeout: int = 10) -> List[Dict[str, Any]]:
        """Block on the inbox queue and return updates."""
        try:
            raw_msg = self._inbox.get(timeout=timeout)
            if isinstance(raw_msg, str):
                msg = {
                    "chat_id": 1,
                    "user_id": 1,
                    "text": raw_msg,
                    "source": "web",
                    "sender_label": "",
                }
            else:
                msg = dict(raw_msg or {})

            message = {
                "chat": {"id": int(msg.get("chat_id") or 1)},
                "from": {"id": int(msg.get("user_id") or 1)},
                "text": str(msg.get("text") or ""),
                "source": str(msg.get("source") or "web"),
            }
            for key in (
                "sender_label",
                "sender_session_id",
                "client_message_id",
                "telegram_chat_id",
                "image_base64",
                "image_mime",
                "image_caption",
            ):
                value = msg.get(key)
                if value not in (None, "", 0):
                    message[key] = value

            self._update_counter = max(offset, self._update_counter + 1)
            return [{
                "update_id": self._update_counter,
                "message": message,
            }]
        except queue.Empty:
            return []

    def configure_from_settings(self, settings: Dict[str, Any]) -> None:
        token = str(settings.get("TELEGRAM_BOT_TOKEN", "") or "").strip()
        chat_id = self._parse_single_chat_id(
            str(settings.get("TELEGRAM_CHAT_ID", "") or "").strip(),
        )
        token_changed = token != self._telegram_bot_token
        chat_id_changed = chat_id != self._telegram_chat_id

        self._telegram_bot_token = token
        self._telegram_chat_id = chat_id
        if chat_id:
            self._telegram_active_chat_id = chat_id
        elif token_changed:
            self._telegram_active_chat_id = 0

        if token_changed or chat_id_changed:
            self._restart_telegram_polling()

    def subscribe_response(self, chat_id: int, callback) -> str:
        """Subscribe to agent responses for a given chat_id. Returns subscription_id."""
        import uuid as _uuid
        sub_id = _uuid.uuid4().hex
        with self._response_subs_lock:
            self._response_subs[sub_id] = (chat_id, callback)
        return sub_id

    def unsubscribe_response(self, subscription_id: str) -> None:
        """Remove a response subscription."""
        with self._response_subs_lock:
            self._response_subs.pop(subscription_id, None)

    def shutdown(self) -> None:
        self._stop_telegram_polling()

    def _parse_single_chat_id(self, raw: str) -> int:
        text = str(raw or "").strip()
        if not text:
            return 0
        try:
            return int(text)
        except ValueError:
            return 0

    def _restart_telegram_polling(self) -> None:
        self._stop_telegram_polling()
        if not self._telegram_bot_token:
            return
        self._telegram_stop.clear()
        self._telegram_poll_thread = threading.Thread(
            target=self._telegram_poll_loop,
            daemon=True,
            name="telegram-poll",
        )
        self._telegram_poll_thread.start()

    def _stop_telegram_polling(self) -> None:
        self._telegram_stop.set()
        thread = self._telegram_poll_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)
        if not (thread and thread.is_alive()):
            self._telegram_poll_thread = None

    def _telegram_api(
        self,
        method: str,
        *,
        params: Optional[dict] = None,
        files: Optional[dict] = None,
        timeout: int = 35,
    ) -> dict:
        if not self._telegram_bot_token:
            raise RuntimeError("Telegram bot token is not configured")
        url = f"https://api.telegram.org/bot{self._telegram_bot_token}/{method}"
        response = requests.post(url, data=params, files=files, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description") or f"Telegram API error for {method}")
        return payload

    def _telegram_download_file(self, file_id: str, timeout: int = 30) -> tuple[bytes, str]:
        payload = self._telegram_api(
            "getFile",
            params={"file_id": str(file_id or "")},
            timeout=20,
        )
        file_path = str((payload.get("result") or {}).get("file_path") or "").strip()
        if not file_path:
            raise RuntimeError("Telegram file path is missing")
        url = f"https://api.telegram.org/file/bot{self._telegram_bot_token}/{file_path}"
        response = requests.get(url, timeout=timeout)
        response.raise_for_status()
        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        return response.content, mime

    def _telegram_target(self, preferred_chat_id: int = 0) -> int:
        if self._telegram_chat_id:
            return self._telegram_chat_id
        if preferred_chat_id and int(preferred_chat_id) > 1:
            return int(preferred_chat_id)
        return int(self._telegram_active_chat_id or 0)

    def _register_telegram_chat(self, chat_id: int) -> None:
        if not chat_id:
            return
        if self._telegram_chat_id and int(chat_id) != self._telegram_chat_id:
            return
        if not self._telegram_active_chat_id:
            self._telegram_active_chat_id = int(chat_id)

    def _telegram_poll_loop(self) -> None:
        offset = 0
        while not self._telegram_stop.is_set():
            try:
                payload = self._telegram_api(
                    "getUpdates",
                    params={"timeout": 20, "offset": offset},
                    timeout=25,
                )
                for update in payload.get("result", []):
                    update_id = int(update.get("update_id") or 0)
                    if update_id >= offset:
                        offset = update_id + 1
                    message = update.get("message") or {}
                    text = str(message.get("text") or "").strip()
                    caption = str(message.get("caption") or "").strip()
                    photos = message.get("photo") or []
                    if not text and not photos:
                        continue
                    chat = message.get("chat") or {}
                    sender = message.get("from") or {}
                    chat_id = int(chat.get("id") or 0)
                    if self._telegram_chat_id and chat_id != self._telegram_chat_id:
                        continue
                    if (
                        not self._telegram_chat_id
                        and self._telegram_active_chat_id
                        and chat_id != self._telegram_active_chat_id
                    ):
                        continue
                    user_id = int(sender.get("id") or chat_id or 0)
                    sender_name = (
                        str(sender.get("username") or "").strip()
                        or " ".join(
                            str(part).strip()
                            for part in (sender.get("first_name"), sender.get("last_name"))
                            if part
                        )
                        or f"Telegram {user_id}"
                    )
                    image_base64 = ""
                    image_mime = ""
                    if photos:
                        file_id = str((photos[-1] or {}).get("file_id") or "").strip()
                        if file_id:
                            try:
                                photo_bytes, image_mime = self._telegram_download_file(file_id)
                                image_base64 = base64.b64encode(photo_bytes).decode("ascii")
                            except Exception as exc:
                                log.warning("Telegram photo download failed: %s", exc)
                    clean_text = text or caption
                    if not clean_text and not image_base64:
                        continue
                    self._register_telegram_chat(chat_id)
                    self.enqueue_local_message(
                        clean_text,
                        chat_id=chat_id,
                        user_id=user_id,
                        source="telegram",
                        sender_label=f"Telegram ({sender_name})",
                        telegram_chat_id=chat_id,
                        image_base64=image_base64,
                        image_mime=image_mime,
                        image_caption=caption,
                    )
                    if self._broadcast_fn:
                        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        if image_base64:
                            self._broadcast_fn({
                                "type": "photo",
                                "role": "user",
                                "image_base64": image_base64,
                                "mime": image_mime or "image/jpeg",
                                "caption": caption,
                                "ts": ts,
                                "source": "telegram",
                                "sender_label": f"Telegram ({sender_name})",
                                "telegram_chat_id": chat_id,
                            })
                        else:
                            self._broadcast_fn({
                                "type": "chat",
                                "role": "user",
                                "content": clean_text,
                                "ts": ts,
                                "source": "telegram",
                                "sender_label": f"Telegram ({sender_name})",
                                "telegram_chat_id": chat_id,
                            })
            except Exception as exc:
                log.warning("Telegram polling error: %s", exc)
                self._telegram_stop.wait(5)

    def _send_telegram_text(self, text: str, preferred_chat_id: int = 0) -> None:
        clean_text = str(text or "").strip()
        if not clean_text or not self._telegram_bot_token:
            return
        chat_id = self._telegram_target(preferred_chat_id)
        if not chat_id:
            return
        try:
            self._telegram_api(
                "sendMessage",
                params={"chat_id": str(chat_id), "text": clean_text},
                timeout=20,
            )
        except Exception:
            log.debug("Failed to send Telegram message to chat %s", chat_id, exc_info=True)

    def _send_telegram_action(self, action: str, preferred_chat_id: int = 0) -> None:
        if not self._telegram_bot_token:
            return
        chat_id = self._telegram_target(preferred_chat_id)
        if not chat_id:
            return
        try:
            self._telegram_api(
                "sendChatAction",
                params={"chat_id": str(chat_id), "action": action},
                timeout=10,
            )
        except Exception:
            log.debug("Failed to send Telegram chat action to chat %s", chat_id, exc_info=True)

    def _send_telegram_photo(
        self,
        photo_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
        preferred_chat_id: int = 0,
    ) -> None:
        if not self._telegram_bot_token:
            return
        filename = "image.png" if mime == "image/png" else "image.jpg"
        chat_id = self._telegram_target(preferred_chat_id)
        if not chat_id:
            return
        try:
            self._telegram_api(
                "sendPhoto",
                params={"chat_id": str(chat_id), "caption": str(caption or "")},
                files={"photo": (filename, photo_bytes, mime)},
                timeout=30,
            )
        except Exception:
            log.debug("Failed to send Telegram photo to chat %s", chat_id, exc_info=True)

    def handle_web_message(
        self,
        text: str,
        *,
        sender_session_id: str = "",
        client_message_id: str = "",
    ) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        preferred_chat_id = self._telegram_target()
        if self._broadcast_fn:
            self._broadcast_fn({
                "type": "chat",
                "role": "user",
                "content": clean_text,
                "ts": ts,
                "source": "web",
                "sender_session_id": sender_session_id,
                "client_message_id": client_message_id,
            })
        if preferred_chat_id:
            sender = sender_session_id[:8] if sender_session_id else "web"
            self._send_telegram_text(
                f"WebUI ({sender}):\n{clean_text}",
                preferred_chat_id=preferred_chat_id,
            )
        self.enqueue_local_message(
            clean_text,
            chat_id=preferred_chat_id or 1,
            user_id=1,
            source="web",
            sender_label="",
            sender_session_id=sender_session_id,
            client_message_id=client_message_id,
            telegram_chat_id=preferred_chat_id or 0,
        )

    def enqueue_local_message(
        self,
        text: str,
        *,
        chat_id: int = 1,
        user_id: int = 1,
        source: str = "web",
        sender_label: str = "",
        sender_session_id: str = "",
        client_message_id: str = "",
        telegram_chat_id: int = 0,
        image_base64: str = "",
        image_mime: str = "",
        image_caption: str = "",
    ) -> None:
        clean_text = str(text or "").strip()
        caption_text = str(image_caption or "").strip()
        image_b64 = str(image_base64 or "").strip()
        if not clean_text and caption_text:
            clean_text = caption_text
        if not clean_text and not image_b64:
            return
        self._inbox.put({
            "chat_id": int(chat_id or 1),
            "user_id": int(user_id or 1),
            "text": clean_text,
            "source": str(source or "web"),
            "sender_label": str(sender_label or ""),
            "sender_session_id": str(sender_session_id or ""),
            "client_message_id": str(client_message_id or ""),
            "telegram_chat_id": int(telegram_chat_id or 0),
            "image_base64": image_b64,
            "image_mime": str(image_mime or ""),
            "image_caption": caption_text,
        })

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "",
        ts: Optional[str] = None,
        is_progress: bool = False,
        task_id: str = "",
    ) -> Tuple[bool, str]:
        """Put a message in the outbox for the UI to consume."""
        clean_text = _strip_markdown(text) if not parse_mode else text
        message_ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat()
        msg = {
            "type": "text",
            "content": clean_text,
            "markdown": bool(parse_mode),
            "is_progress": bool(is_progress),
            "ts": message_ts,
            "task_id": str(task_id or ""),
        }
        self._outbox.put(msg)
        # Notify A2A response subscribers
        with self._response_subs_lock:
            subs = [(sid, cb) for sid, (cid, cb) in self._response_subs.items()
                    if cid == chat_id and not is_progress]
        for sid, cb in subs:
            try:
                cb(clean_text)
            except Exception:
                log.debug("A2A response callback error for sub %s", sid, exc_info=True)
        if self._broadcast_fn:
            self._broadcast_fn({
                "type": "chat",
                "role": "assistant",
                "content": clean_text,
                "markdown": bool(parse_mode),
                "is_progress": bool(is_progress),
                "ts": message_ts,
                "task_id": str(task_id or ""),
            })
        self._send_telegram_text(_strip_markdown(clean_text), preferred_chat_id=chat_id)
        return True, "ok"

    def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send typing indicator to UI via WebSocket broadcast."""
        self._outbox.put({
            "type": "action",
            "content": action,
        })
        if self._broadcast_fn:
            self._broadcast_fn({"type": "typing", "action": action})
        if action == "typing":
            self._send_telegram_action("typing", preferred_chat_id=chat_id)
        return True

    def send_photo(
        self,
        chat_id: int,
        photo_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
    ) -> Tuple[bool, str]:
        """Send photo to UI and Telegram."""
        b64_str = base64.b64encode(photo_bytes).decode("ascii")
        msg = {
            "type": "photo",
            "role": "assistant",
            "image_base64": b64_str,
            "mime": mime,
            "caption": caption,
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
        self._outbox.put(msg)
        if self._broadcast_fn:
            self._broadcast_fn(msg)
        self._send_telegram_photo(photo_bytes, caption=caption, mime=mime, preferred_chat_id=chat_id)
        return True, "ok"

    def download_file_base64(
        self,
        file_id: str,
        max_bytes: int = 10_000_000,
    ) -> Tuple[Optional[str], str]:
        """Placeholder for future web UI file upload support."""
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
        if self._broadcast_fn:
            self._broadcast_fn({"type": "log", "data": event})

    def ui_poll_logs(self) -> list:
        """Called by the web UI to drain pending log events."""
        batch = []
        for _ in range(50):
            try:
                batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        return batch

    # UI hooks
    def ui_send(
        self,
        text: str,
        *,
        broadcast: bool = True,
        sender_session_id: str = "",
        client_message_id: str = "",
    ):
        """Called by the web UI to send a message to the agent."""
        if broadcast:
            self.handle_web_message(
                text,
                sender_session_id=sender_session_id,
                client_message_id=client_message_id,
            )
            return
        self.enqueue_local_message(text)

    def ui_receive(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        """Called by the web UI to check for new messages from the agent."""
        try:
            return self._outbox.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def split_message(text: str, limit: int = 4000) -> List[str]:
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
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = text.replace("`", "")
    return text


def _send_markdown(
    chat_id: int,
    text: str,
    ts: Optional[str] = None,
    is_progress: bool = False,
    task_id: str = "",
) -> Tuple[bool, str]:
    """Send markdown text to the UI."""
    bridge = get_bridge()
    if not text:
        return False, "empty"
    return bridge.send_message(
        chat_id,
        text,
        parse_mode="markdown",
        ts=ts,
        is_progress=is_progress,
        task_id=task_id,
    )


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


def log_chat(
    direction: str,
    chat_id: int,
    user_id: int,
    text: str,
    ts: Optional[str] = None,
    fmt: str = "",
    source: str = "",
    sender_label: str = "",
    sender_session_id: str = "",
    client_message_id: str = "",
    telegram_chat_id: int = 0,
    task_id: str = "",
) -> None:
    if DATA_DIR:
        append_jsonl(DATA_DIR / "logs" / "chat.jsonl", {
            "ts": ts or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "session_id": load_state().get("session_id"),
            "direction": direction,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": text,
            "format": fmt,
            "source": source,
            "sender_label": sender_label,
            "sender_session_id": sender_session_id,
            "client_message_id": client_message_id,
            "telegram_chat_id": int(telegram_chat_id or 0),
            "task_id": str(task_id or ""),
        })


def send_with_budget(chat_id: int, text: str, log_text: Optional[str] = None,
                     force_budget: bool = False, fmt: str = "",
                     is_progress: bool = False, task_id: str = "",
                     ts: Optional[str] = None) -> None:
    # force_budget kept in signature for caller compat but is a no-op since 3.3.0
    st = load_state()
    owner_id = int(st.get("owner_id") or 0)
    _text = str(text or "")
    msg_ts = ts or datetime.datetime.now(datetime.timezone.utc).isoformat()

    if is_progress and DATA_DIR:
        append_jsonl(DATA_DIR / "logs" / "progress.jsonl", {
            "ts": msg_ts,
            "type": "send_message",
            "task_id": task_id,
            "is_progress": True,
            "direction": "out", "chat_id": chat_id, "user_id": owner_id,
            "text": text if log_text is None else log_text,
            "content": _text,
            "format": fmt,
        })
    else:
        log_chat(
            "out",
            chat_id,
            owner_id,
            text if log_text is None else log_text,
            ts=msg_ts,
            fmt=fmt,
            task_id=task_id,
        )

    if _text.strip() in ("", "\u200b"):
        return
    # Budget footers are now shown in dashboard/status flows, not auto-appended
    # to every outgoing chat message.
    full = _text

    if fmt == "markdown":
        ok, err = _send_markdown(
            chat_id,
            full,
            ts=msg_ts,
            is_progress=is_progress,
            task_id=task_id,
        )
        return

    bridge = get_bridge()
    bridge.send_message(chat_id, full, ts=msg_ts, is_progress=is_progress, task_id=task_id)

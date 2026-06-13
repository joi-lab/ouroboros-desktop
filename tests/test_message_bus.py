import supervisor.message_bus as message_bus
import ouroboros.event_bus as event_bus


def _make_bridge(monkeypatch, settings=None):
    return message_bus.LocalChatBridge(settings or {})


def test_configure_from_settings_without_legacy_field(monkeypatch):
    """configure_from_settings remains a no-op compatibility path."""
    bridge = _make_bridge(monkeypatch)
    bridge.configure_from_settings({
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "999",
    })
    assert bridge.get_updates(offset=0, timeout=0) == []


def test_ui_send_enqueues_structured_message_and_broadcasts(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    broadcasts = []
    bridge._broadcast_fn = broadcasts.append

    bridge.ui_send("hello", sender_session_id="sess-1", client_message_id="c-1")
    updates = bridge.get_updates(offset=0, timeout=1)

    assert broadcasts[0]["role"] == "user"
    assert broadcasts[0]["sender_session_id"] == "sess-1"
    assert broadcasts[0]["client_message_id"] == "c-1"
    assert updates[0]["message"]["text"] == "hello"
    assert updates[0]["message"]["source"] == "web"
    assert updates[0]["message"]["sender_session_id"] == "sess-1"
    assert updates[0]["message"]["client_message_id"] == "c-1"


def test_ui_send_preserves_suppress_chat_log_flag(monkeypatch):
    bridge = _make_bridge(monkeypatch)

    bridge.ui_send("FULL_PROMPT", broadcast=False, suppress_chat_log=True)
    updates = bridge.get_updates(offset=0, timeout=1)

    assert updates[0]["message"]["text"] == "FULL_PROMPT"
    assert updates[0]["message"]["suppress_chat_log"] is True


def test_send_photo_publishes_transport_event_with_payload(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_photo(123, b"img", caption="caption", mime="image/png")

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_PHOTO
    assert payload["image_base64"]
    assert payload["caption"] == "caption"
    assert payload["mime"] == "image/png"


def test_send_video_publishes_transport_event_with_payload(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_video(123, b"vid", caption="trailer", mime="video/mp4")

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_VIDEO
    assert payload["video_base64"]
    assert payload["caption"] == "trailer"
    assert payload["mime"] == "video/mp4"


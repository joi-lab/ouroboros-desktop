"""Regression checks for restart/reconnect client behavior."""

import os
import pathlib

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_ws_has_error_handler_and_reconnect_timer():
    source = _read("web/modules/ws.js")
    assert "socket.onerror" in source
    assert "_scheduleReconnect" in source
    assert "_scheduleUiRecovery" in source
    assert "_uiRecoveryTimer" in source
    assert "_watchdogTimer" in source
    assert "_lastMessageAt" in source
    assert "_startWatchdog" in source
    assert "window.location.replace" in source
    assert "location.reload()" not in source


def test_ws_queues_outbound_messages_when_disconnected():
    source = _read("web/modules/ws.js")
    assert "_pendingMessages" in source
    assert "status: 'queued'" in source
    assert "outbound_sent" in source


def test_chat_marks_pending_messages_until_reconnect():
    source = _read("web/modules/chat.js")
    assert "pendingUserBubbles" in source
    assert "Queued until reconnect" in source
    assert "result?.status === 'queued'" in source


def test_chat_resyncs_history_after_reconnect():
    source = _read("web/modules/chat.js")
    assert "async function syncHistory" in source
    assert "/api/chat/history?limit=1000" in source
    assert "cache: 'no-store'" in source
    assert "syncHistory({ includeUser: !historyLoaded, fromReconnect: isReconnect })" in source
    assert "const expectedDisconnect = socketState !== WebSocket.OPEN" in source
    assert "if (expectedDisconnect && err instanceof TypeError)" in source


def test_server_enables_ws_ping_and_heartbeat():
    server_source = _read("server.py")
    helper_source = _read("ouroboros/server_runtime.py")
    assert "ws_heartbeat_loop" in server_source or "ws_heartbeat_loop" in helper_source
    assert '"type": "heartbeat"' in server_source or '"type": "heartbeat"' in helper_source
    assert "ws_ping_interval=20" in server_source
    assert "ws_ping_timeout=20" in server_source


def test_index_includes_reconnect_overlay():
    source = _read("web/index.html")
    assert 'id="reconnect-overlay"' in source


def test_index_page_disables_cache():
    server_source = _read("server.py")
    helper_source = _read("ouroboros/server_web.py")
    assert "cache-control" in server_source.lower() or "cache-control" in helper_source.lower()


def test_find_free_port_waits_for_preferred_port():
    """find_free_port should retry the preferred port before falling back."""
    import socket
    import threading
    from ouroboros.server_entrypoint import find_free_port

    # Grab any available port for isolation
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        preferred = s.getsockname()[1]

    # Block the preferred port with a listener that releases after a short delay
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", preferred))
    blocker.listen(1)

    def _release():
        import time
        time.sleep(1.0)
        blocker.close()

    threading.Thread(target=_release, daemon=True).start()

    # With wait_retries=6 × 0.5s = 3s budget, it should get the preferred port
    result = find_free_port("127.0.0.1", preferred, wait_retries=6, wait_interval=0.5)
    assert result == preferred, f"Expected preferred port {preferred}, got {result}"


def test_find_free_port_falls_back_when_stuck():
    """find_free_port should fall back to a nearby port if preferred stays busy.

    On Windows, SO_REUSEADDR allows binding to a port already in LISTEN state,
    so the real-socket blocking approach does not reliably prevent rebind.
    We use monkeypatching to guarantee the preferred port stays unavailable.
    """
    from ouroboros import server_entrypoint

    preferred = 51234  # arbitrary high port unlikely to collide

    _original = getattr(server_entrypoint, "_can_bind_port", None)

    def _fake_can_bind(host: str, port: int) -> bool:
        if port == preferred:
            return False  # always blocked
        # For fallback ports, allow the first one
        return True

    server_entrypoint._can_bind_port = _fake_can_bind
    try:
        result = server_entrypoint.find_free_port(
            "127.0.0.1", preferred, max_tries=10,
            wait_retries=2, wait_interval=0.05,
        )
        assert result != preferred, f"Should have fallen back, but got preferred {preferred}"
    finally:
        if _original is not None:
            server_entrypoint._can_bind_port = _original


def test_find_free_port_retries_fallback_range_until_port_frees(monkeypatch):
    """Fallback scanning should keep retrying instead of returning the busy preferred port."""
    from ouroboros import server_entrypoint

    preferred = 41000
    attempts = {preferred: 0, preferred + 1: 0, preferred + 2: 0, preferred + 3: 0}

    def fake_can_bind(_host: str, port: int) -> bool:
        attempts[port] += 1
        if port == preferred:
            return False
        if port == preferred + 1:
            return attempts[port] >= 3
        return False

    monkeypatch.setattr(server_entrypoint, "_can_bind_port", fake_can_bind)

    result = server_entrypoint.find_free_port(
        "127.0.0.1",
        preferred,
        max_tries=4,
        wait_retries=3,
        wait_interval=0,
    )

    assert result == preferred + 1
    assert attempts[preferred] == 3
    assert attempts[preferred + 1] == 3


def test_find_free_port_raises_when_range_stays_busy(monkeypatch):
    """find_free_port should fail clearly instead of returning a known-busy port."""
    from ouroboros import server_entrypoint

    monkeypatch.setattr(server_entrypoint, "_can_bind_port", lambda _host, _port: False)

    try:
        server_entrypoint.find_free_port(
            "127.0.0.1",
            42000,
            max_tries=3,
            wait_retries=2,
            wait_interval=0,
        )
    except OSError as exc:
        assert "42000-42002" in str(exc)
    else:
        raise AssertionError("Expected find_free_port to raise when every candidate stays busy")


def test_chat_shows_reconnect_banner_after_reconnect_and_reload():
    """chat.js should show reconnect status after both soft reconnect and restart reload."""
    source = _read("web/modules/chat.js")
    assert "wsHasConnectedOnce" in source, "Missing reconnect-once tracking flag"
    assert "Reconnected" in source, "Missing reconnect banner text"
    assert "Restart complete" in source, "Missing restart-complete banner text"
    assert "_ouro_reason" in source, "Missing restart reload reason handling"
    assert "history.replaceState" in source, "Reconnect params should be cleared after showing banner"
    # Ensure banner is ephemeral (not persisted to history)
    assert "ephemeral: true" in source


def test_progress_bubbles_have_subdued_styling():
    """Progress/reasoning bubbles should be visually muted compared to regular ones."""
    css = _read("web/style.css")
    assert ".chat-bubble.progress .message" in css, "Missing progress-specific message style"
    assert "font-size: 13px" in css, "Progress font should be smaller than default 15.5px"


def test_working_live_cards_are_subdued_and_expandable():
    """Visible working/thinking cards should stay compact but support expanding per block."""
    css = _read("web/style.css")
    assert ".chat-live-title" in css, "Missing live-card title styling"
    assert ".chat-live-line-toggle" in css, "Missing clickable live-line toggle"
    assert ".chat-live-line-expand-label" in css, "Missing expand/collapse label"
    assert '.chat-live-line[data-expanded="1"] .chat-live-line-body' in css


def test_live_card_blocks_can_expand_to_full_text():
    """chat.js should preserve expansion state and render per-block toggles."""
    source = _read("web/modules/chat.js")
    assert "expandedLineKeys" in source, "Missing per-line expansion state"
    assert "data-live-line-toggle" in source, "Missing per-block toggle markup"
    assert "fullHeadline" in source, "Missing full headline preservation"
    assert "fullBody" in source, "Missing full body preservation"


def test_live_event_summaries_preserve_full_text_for_expansion():
    """log event summaries should keep full text so expanded live blocks can reveal it."""
    source = _read("web/modules/log_events.js")
    assert "describeText" in source
    assert "fullHeadline" in source
    assert "fullBody" in source


def test_task_done_live_summary_distinguishes_typed_failure():
    source = _read("web/modules/log_events.js")
    assert "function taskDoneFailure" in source
    assert "['failed', 'infra_failed'].includes(resultStatus)" in source
    assert "phase: failed ? 'error' : 'done'" in source


def test_chat_scrolls_to_bottom_after_first_history_load():
    """syncHistory must scroll to bottom on first load (restart/open) but
    respect user scroll position on subsequent reconnect syncs."""
    source = _read("web/modules/chat.js")
    # First-load guard: wasFirstLoad captures pre-call state
    assert "wasFirstLoad = !historyLoaded" in source, \
        "Missing first-load detection before setting historyLoaded"
    # Conditional scroll: first load always scrolls, reconnect only when near bottom
    assert "if (wasFirstLoad || isNearBottom())" in source, \
        "Missing conditional scroll-to-bottom after history sync"
    assert "messagesDiv.scrollTop = messagesDiv.scrollHeight" in source


def test_restart_watchdog_waits_for_uvicorn_exit():
    source = _read("server.py")
    assert "_uvicorn_exited = threading.Event()" in source
    assert "_uvicorn_exited.wait(timeout=force_exit_timeout_sec)" in source
    assert "_uvicorn_exited.set()" in source


def test_owner_restart_copy_is_explicit_about_stopped_task():
    source = _read("server.py")
    assert 'ctx.send_with_budget(chat_id, "♻️ Restarting.")' in source
    assert "Stopping active task. New settings apply to the next message." in source
    assert "owner_restart_no_resume.flag" in source
    assert "owner_restart_no_resume" in source
    assert "panic_stop.flag" in source
    assert "owner_restart_flag.unlink(missing_ok=True)" in source
    assert "stable_skip_flag.unlink(missing_ok=True)" in source
    assert source.index("safe_restart(reason=\"owner_restart\"") < source.index("Stopping active task. New settings apply to the next message.")
    assert source.index("owner_restart_no_resume.flag") < source.index("Stopping active task. New settings apply to the next message.")


def test_auto_resume_skips_owner_restart_no_resume_flag(tmp_path, monkeypatch):
    import supervisor.workers as workers

    original_drive = workers.DRIVE_ROOT
    workers.DRIVE_ROOT = tmp_path
    flag = tmp_path / "state" / "owner_restart_no_resume.flag"
    flag.parent.mkdir(parents=True)
    flag.write_text("owner_restart", encoding="utf-8")
    compat_flag = tmp_path / "state" / "panic_stop.flag"
    compat_flag.write_text("owner_restart_no_resume", encoding="utf-8")

    def fail_load_state():
        raise AssertionError("owner restart flag should be checked before load_state")

    monkeypatch.setattr(workers, "load_state", fail_load_state)
    try:
        workers.auto_resume_after_restart()
    finally:
        workers.DRIVE_ROOT = original_drive

    assert not flag.exists()
    assert not compat_flag.exists()


def test_owner_restart_cleans_flags_when_worker_shutdown_fails(tmp_path, monkeypatch):
    import server
    import supervisor.message_bus as message_bus

    messages = []

    class Bridge:
        def get_updates(self, offset=0, timeout=1):
            return [{
                "update_id": offset,
                "message": {
                    "chat": {"id": 1},
                    "from": {"id": 1},
                    "text": "/restart",
                },
            }]

    class Ctx:
        consciousness = None

        def load_state(self):
            return {}

        def save_state(self, _state):
            return None

        def send_with_budget(self, _chat_id, text):
            messages.append(text)

        def safe_restart(self, **_kwargs):
            return True, "OK"

        def kill_workers(self, force=True, **kwargs):
            assert kwargs["result_status"] == "cancelled"
            assert kwargs["result_reason"] == "Owner restart stopped this task before process restart."
            raise RuntimeError("shutdown failed")

    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        server,
        "_request_restart_exit",
        lambda: (_ for _ in ()).throw(AssertionError("restart exit should not be requested")),
    )

    server._process_bridge_updates(Bridge(), 0, Ctx())

    assert not (tmp_path / "state" / "owner_restart_no_resume.flag").exists()
    assert not (tmp_path / "state" / "panic_stop.flag").exists()
    assert "Stopping active task. New settings apply to the next message." not in messages
    assert "⚠️ Restart cancelled: failed to stop workers." in messages


def test_ws_reloads_when_sha_unknown_after_reconnect():
    """ws.js must reload the page when _lastSha is null after reconnect (PyWebView loses JS state).

    Previously the guard was: `previouslyConnected && this._lastSha && d.sha && d.sha !== this._lastSha`
    which silently skipped the reload when _lastSha was null.
    Now: any reconnect with previouslyConnected=true where SHA is unknown or changed triggers reload.
    """
    source = _read("web/modules/ws.js")
    # New guard: reload if lastSha unknown OR sha changed
    assert "!this._lastSha || this._lastSha !== newSha" in source, (
        "_refreshStateAfterOpen must reload when _lastSha is null or SHA changed"
    )
    # Must still be guarded by previouslyConnected to avoid spurious reload on first connect
    assert "previouslyConnected && newSha" in source, (
        "Reload must only fire when previouslyConnected=true and server returns a non-empty SHA"
    )

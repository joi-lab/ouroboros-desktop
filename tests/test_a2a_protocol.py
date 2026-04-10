"""Tests for the A2A (Agent-to-Agent) protocol integration.

Covers: FileTaskStore, A2A Executor, A2A Server (Agent Card, JSON-RPC),
response subscriptions on LocalChatBridge, and client tools.
"""

import asyncio
import json
import pathlib
import threading
import uuid

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_data_dir(tmp_path):
    """Return a temporary data directory mimicking ~/Ouroboros/data."""
    d = tmp_path / "data"
    d.mkdir()
    return d


# ===========================================================================
# 1. FileTaskStore
# ===========================================================================


class TestFileTaskStore:
    """File-based task persistence."""

    def _make_store(self, tmp_path, ttl_hours=24):
        from ouroboros.a2a_task_store import FileTaskStore
        return FileTaskStore(_tmp_data_dir(tmp_path), ttl_hours=ttl_hours)

    def _make_task(self, task_id="task-1", state="completed"):
        from a2a.types import Task, TaskStatus
        return Task(
            id=task_id,
            contextId="ctx-1",
            status=TaskStatus(state=state, timestamp="2026-04-10T12:00:00Z"),
        )

    def test_save_and_get(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task))
        loaded = asyncio.run(store.get("task-1"))
        assert loaded is not None
        assert loaded.id == "task-1"
        assert loaded.status.state.value == "completed"

    def test_get_nonexistent_returns_none(self, tmp_path):
        store = self._make_store(tmp_path)
        assert asyncio.run(store.get("does-not-exist")) is None

    def test_delete(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task))
        asyncio.run(store.delete("task-1"))
        assert asyncio.run(store.get("task-1")) is None

    def test_delete_nonexistent_no_error(self, tmp_path):
        store = self._make_store(tmp_path)
        asyncio.run(store.delete("nope"))  # should not raise

    def test_save_overwrites(self, tmp_path):
        store = self._make_store(tmp_path)
        task1 = self._make_task(state="working")
        asyncio.run(store.save(task1))
        task2 = self._make_task(state="completed")
        asyncio.run(store.save(task2))
        loaded = asyncio.run(store.get("task-1"))
        assert loaded.status.state.value == "completed"

    def test_atomic_write_creates_valid_json(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task))
        task_file = store._dir / "task-1.json"
        assert task_file.exists()
        data = json.loads(task_file.read_text())
        assert data["id"] == "task-1"

    def test_safe_id_sanitization(self, tmp_path):
        store = self._make_store(tmp_path)
        task = self._make_task(task_id="../../etc/passwd")
        asyncio.run(store.save(task))
        # Should not create files outside the task dir
        assert not (tmp_path / "etc").exists()
        path = store._task_path("../../etc/passwd")
        assert str(path).startswith(str(store._dir))

    def test_cleanup_expired_removes_old_terminal(self, tmp_path):
        import os
        import time
        store = self._make_store(tmp_path, ttl_hours=0)  # 0 = expire immediately
        task = self._make_task(state="completed")
        asyncio.run(store.save(task))
        # Backdate the file mtime
        task_file = store._dir / "task-1.json"
        old_time = time.time() - 3600
        os.utime(task_file, (old_time, old_time))
        removed = asyncio.run(store.cleanup_expired())
        assert removed == 1
        assert asyncio.run(store.get("task-1")) is None

    def test_cleanup_keeps_non_terminal(self, tmp_path):
        import os
        import time
        store = self._make_store(tmp_path, ttl_hours=0)
        task = self._make_task(state="working")
        asyncio.run(store.save(task))
        task_file = store._dir / "task-1.json"
        old_time = time.time() - 3600
        os.utime(task_file, (old_time, old_time))
        removed = asyncio.run(store.cleanup_expired())
        assert removed == 0
        assert asyncio.run(store.get("task-1")) is not None

    def test_cleanup_keeps_recent_terminal(self, tmp_path):
        store = self._make_store(tmp_path, ttl_hours=24)
        task = self._make_task(state="completed")
        asyncio.run(store.save(task))
        removed = asyncio.run(store.cleanup_expired())
        assert removed == 0

    def test_context_parameter_accepted(self, tmp_path):
        """TaskStore interface passes context as positional arg."""
        store = self._make_store(tmp_path)
        task = self._make_task()
        asyncio.run(store.save(task, None))
        loaded = asyncio.run(store.get("task-1", None))
        assert loaded is not None
        asyncio.run(store.delete("task-1", None))
        assert asyncio.run(store.get("task-1")) is None


# ===========================================================================
# 2. LocalChatBridge response subscriptions
# ===========================================================================


class TestBridgeSubscriptions:
    """Response subscription mechanism on LocalChatBridge."""

    def _make_bridge(self, monkeypatch):
        import supervisor.message_bus as mb
        monkeypatch.setattr(mb.LocalChatBridge, "_restart_telegram_polling", lambda self: None)
        return mb.LocalChatBridge({})

    def test_subscribe_and_receive(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        sub_id = bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(42, "hello world")
        assert received == ["hello world"]
        bridge.unsubscribe_response(sub_id)

    def test_unsubscribe_stops_delivery(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        sub_id = bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.unsubscribe_response(sub_id)
        bridge.send_message(42, "should not arrive")
        assert received == []

    def test_subscription_filters_by_chat_id(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(99, "wrong chat_id")
        assert received == []

    def test_progress_messages_not_delivered(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []
        bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(42, "progress update", is_progress=True)
        assert received == []

    def test_multiple_subscribers(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        r1, r2 = [], []
        bridge.subscribe_response(42, lambda text: r1.append(text))
        bridge.subscribe_response(42, lambda text: r2.append(text))
        bridge.send_message(42, "broadcast")
        assert r1 == ["broadcast"]
        assert r2 == ["broadcast"]

    def test_callback_error_does_not_break_other_subscribers(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        received = []

        def bad_callback(text):
            raise RuntimeError("boom")

        bridge.subscribe_response(42, bad_callback)
        bridge.subscribe_response(42, lambda text: received.append(text))
        bridge.send_message(42, "test")
        assert received == ["test"]

    def test_subscribe_returns_unique_ids(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        ids = set()
        for _ in range(100):
            ids.add(bridge.subscribe_response(1, lambda t: None))
        assert len(ids) == 100

    def test_unsubscribe_nonexistent_no_error(self, monkeypatch):
        bridge = self._make_bridge(monkeypatch)
        bridge.unsubscribe_response("does-not-exist")  # should not raise

    def test_negative_chat_id_works(self, monkeypatch):
        """A2A uses negative virtual chat_ids."""
        bridge = self._make_bridge(monkeypatch)
        received = []
        bridge.subscribe_response(-1001, lambda text: received.append(text))
        bridge.send_message(-1001, "a2a response")
        assert received == ["a2a response"]


# ===========================================================================
# 3. A2A Executor
# ===========================================================================


class TestA2AExecutor:
    """OuroborosA2AExecutor unit tests."""

    def test_extract_text_from_text_part(self):
        from ouroboros.a2a_executor import OuroborosA2AExecutor
        from a2a.types import Message, Part, TextPart, Role

        msg = Message(
            messageId="m1",
            role=Role.user,
            parts=[Part(root=TextPart(text="hello"))],
        )
        assert OuroborosA2AExecutor._extract_text(msg) == "hello"

    def test_extract_text_multiple_parts(self):
        from ouroboros.a2a_executor import OuroborosA2AExecutor
        from a2a.types import Message, Part, TextPart, Role

        msg = Message(
            messageId="m1",
            role=Role.user,
            parts=[
                Part(root=TextPart(text="line1")),
                Part(root=TextPart(text="line2")),
            ],
        )
        assert OuroborosA2AExecutor._extract_text(msg) == "line1\nline2"

    def test_extract_text_none_message(self):
        from ouroboros.a2a_executor import OuroborosA2AExecutor
        assert OuroborosA2AExecutor._extract_text(None) == ""

    def test_extract_text_empty_parts(self):
        from ouroboros.a2a_executor import OuroborosA2AExecutor
        from a2a.types import Message, Role

        msg = Message(messageId="m1", role=Role.user, parts=[])
        assert OuroborosA2AExecutor._extract_text(msg) == ""

    def test_concurrency_semaphore(self):
        from ouroboros.a2a_executor import OuroborosA2AExecutor
        executor = OuroborosA2AExecutor(max_concurrent=2)
        assert executor._semaphore.acquire(blocking=False)
        assert executor._semaphore.acquire(blocking=False)
        assert not executor._semaphore.acquire(blocking=False)
        executor._semaphore.release()
        assert executor._semaphore.acquire(blocking=False)

    def test_virtual_chat_id_sequence(self):
        from ouroboros.a2a_executor import _next_a2a_chat_id
        id1 = _next_a2a_chat_id()
        id2 = _next_a2a_chat_id()
        assert id1 < -1000
        assert id2 < id1  # decreasing sequence


# ===========================================================================
# 4. A2A Server — Agent Card
# ===========================================================================


class TestAgentCard:
    """Dynamic Agent Card building."""

    def test_parse_identity_i_am_heading(self, tmp_path):
        from ouroboros.a2a_server import _parse_identity
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "identity.md").write_text("# I Am TestBot\n\nI do great things.\n")
        name, desc = _parse_identity(tmp_path)
        assert name == "TestBot"
        assert "great things" in desc

    def test_parse_identity_generic_heading(self, tmp_path):
        from ouroboros.a2a_server import _parse_identity
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "identity.md").write_text(
            "# Who I Am\n\nI'm Ouroboros. I rewrite myself.\n"
        )
        name, desc = _parse_identity(tmp_path)
        assert name == "Ouroboros"
        assert "rewrite" in desc

    def test_parse_identity_missing_file(self, tmp_path):
        from ouroboros.a2a_server import _parse_identity
        name, desc = _parse_identity(tmp_path)
        assert name == ""
        assert desc == ""

    def test_parse_identity_empty_file(self, tmp_path):
        from ouroboros.a2a_server import _parse_identity
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "identity.md").write_text("")
        name, desc = _parse_identity(tmp_path)
        assert name == ""
        assert desc == ""

    def test_parse_identity_stops_at_hr(self, tmp_path):
        from ouroboros.a2a_server import _parse_identity
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "identity.md").write_text(
            "# I Am Bot\n\nFirst paragraph.\n\n---\n\nShould not appear.\n"
        )
        name, desc = _parse_identity(tmp_path)
        assert "Should not appear" not in desc

    def test_parse_identity_stops_at_h2(self, tmp_path):
        from ouroboros.a2a_server import _parse_identity
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "identity.md").write_text(
            "# I Am Bot\n\nFirst paragraph.\n\n## Section\n\nHidden.\n"
        )
        name, desc = _parse_identity(tmp_path)
        assert "Hidden" not in desc

    def test_resolve_host_localhost(self):
        from ouroboros.a2a_server import _resolve_host
        assert _resolve_host("127.0.0.1") == "127.0.0.1"
        assert _resolve_host("192.168.1.1") == "192.168.1.1"

    def test_resolve_host_wildcard(self):
        from ouroboros.a2a_server import _resolve_host
        resolved = _resolve_host("0.0.0.0")
        assert resolved != "0.0.0.0"
        assert len(resolved) > 0

    def test_build_agent_card(self, tmp_path, monkeypatch):
        from ouroboros.a2a_server import _build_agent_card
        import ouroboros.config as config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)
        mem = tmp_path / "memory"
        mem.mkdir()
        (mem / "identity.md").write_text("# I Am TestAgent\n\nTest description.\n")

        settings = {"A2A_AGENT_NAME": "", "A2A_AGENT_DESCRIPTION": ""}
        card = _build_agent_card(settings, "127.0.0.1", 18800)

        assert card.name == "TestAgent"
        assert "Test description" in card.description
        assert card.url == "http://127.0.0.1:18800/"
        assert card.capabilities.streaming is True
        assert len(card.skills) >= 1

    def test_build_agent_card_settings_override(self, tmp_path, monkeypatch):
        from ouroboros.a2a_server import _build_agent_card
        import ouroboros.config as config

        monkeypatch.setattr(config, "DATA_DIR", tmp_path)

        settings = {
            "A2A_AGENT_NAME": "CustomName",
            "A2A_AGENT_DESCRIPTION": "Custom description",
        }
        card = _build_agent_card(settings, "127.0.0.1", 18800)
        assert card.name == "CustomName"
        assert card.description == "Custom description"


# ===========================================================================
# 5. Client tools
# ===========================================================================


class TestClientTools:
    """A2A client tools: a2a_discover, a2a_send, a2a_status."""

    def _ctx(self, tmp_path):
        from ouroboros.tools.registry import ToolContext
        return ToolContext(repo_dir=tmp_path, drive_root=tmp_path)

    def test_get_tools_returns_three(self):
        from ouroboros.tools.a2a import get_tools
        tools = get_tools()
        names = [t.name for t in tools]
        assert names == ["a2a_discover", "a2a_send", "a2a_status"]

    def test_tools_have_required_schema_fields(self):
        from ouroboros.tools.a2a import get_tools
        for tool in get_tools():
            schema = tool.schema
            assert "name" in schema
            assert "description" in schema
            assert "parameters" in schema
            assert schema["parameters"]["type"] == "object"
            assert "required" in schema["parameters"]

    def test_discover_bad_url(self, tmp_path):
        from ouroboros.tools.a2a import _a2a_discover
        result = json.loads(_a2a_discover(self._ctx(tmp_path), "http://127.0.0.1:1"))
        assert "error" in result

    def test_send_bad_url(self, tmp_path):
        from ouroboros.tools.a2a import _a2a_send
        result = json.loads(_a2a_send(
            self._ctx(tmp_path), "http://127.0.0.1:1", "hello"
        ))
        assert "error" in result

    def test_status_bad_url(self, tmp_path):
        from ouroboros.tools.a2a import _a2a_status
        result = json.loads(_a2a_status(
            self._ctx(tmp_path), "http://127.0.0.1:1", "task-1"
        ))
        assert "error" in result

    def test_discover_parses_agent_card(self, tmp_path, monkeypatch):
        """Mock httpx to return a fake Agent Card."""
        import httpx
        from ouroboros.tools.a2a import _a2a_discover

        fake_card = {
            "name": "RemoteBot",
            "description": "A remote agent",
            "version": "1.0.0",
            "url": "http://remote:9000/",
            "capabilities": {"streaming": True},
            "skills": [
                {"id": "s1", "name": "skill1", "description": "Does stuff"}
            ],
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
        }

        class FakeResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return fake_card

        class FakeClient:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def get(self, url): return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        result = json.loads(_a2a_discover(self._ctx(tmp_path), "http://remote:9000"))
        assert result["name"] == "RemoteBot"
        assert len(result["skills"]) == 1
        assert result["skills"][0]["name"] == "skill1"

    def test_send_parses_completed_task(self, tmp_path, monkeypatch):
        """Mock httpx to return a completed task."""
        import httpx
        from ouroboros.tools.a2a import _a2a_send

        fake_response = {
            "jsonrpc": "2.0",
            "id": "test",
            "result": {
                "id": "task-abc",
                "contextId": "ctx-1",
                "status": {"state": "completed"},
                "artifacts": [
                    {"artifactId": "a1", "parts": [{"text": "Four."}]}
                ],
            },
        }

        class FakeResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return fake_response

        class FakeClient:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, url, **kw): return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        result = json.loads(_a2a_send(
            self._ctx(tmp_path), "http://remote:9000", "2+2?"
        ))
        assert result["task_id"] == "task-abc"
        assert result["status"] == "completed"
        assert result["response"] == "Four."

    def test_status_parses_working_task(self, tmp_path, monkeypatch):
        """Mock httpx to return a working task."""
        import httpx
        from ouroboros.tools.a2a import _a2a_status

        fake_response = {
            "jsonrpc": "2.0",
            "id": "test",
            "result": {
                "id": "task-xyz",
                "status": {"state": "working"},
                "artifacts": [],
            },
        }

        class FakeResponse:
            status_code = 200
            def raise_for_status(self): pass
            def json(self): return fake_response

        class FakeClient:
            def __init__(self, **kw): pass
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def post(self, url, **kw): return FakeResponse()

        monkeypatch.setattr(httpx, "Client", FakeClient)
        result = json.loads(_a2a_status(
            self._ctx(tmp_path), "http://remote:9000", "task-xyz"
        ))
        assert result["task_id"] == "task-xyz"
        assert result["status"] == "working"
        assert result["response"] is None


# ===========================================================================
# 6. A2A Config
# ===========================================================================


class TestA2AConfig:
    """A2A settings in SETTINGS_DEFAULTS."""

    def test_settings_defaults_include_a2a(self):
        from ouroboros.config import SETTINGS_DEFAULTS
        assert "A2A_ENABLED" in SETTINGS_DEFAULTS
        assert "A2A_PORT" in SETTINGS_DEFAULTS
        assert "A2A_HOST" in SETTINGS_DEFAULTS
        assert "A2A_AGENT_NAME" in SETTINGS_DEFAULTS
        assert "A2A_AGENT_DESCRIPTION" in SETTINGS_DEFAULTS
        assert "A2A_MAX_CONCURRENT" in SETTINGS_DEFAULTS
        assert "A2A_TASK_TTL_HOURS" in SETTINGS_DEFAULTS

    def test_default_values(self):
        from ouroboros.config import SETTINGS_DEFAULTS
        assert SETTINGS_DEFAULTS["A2A_ENABLED"] is True
        assert SETTINGS_DEFAULTS["A2A_PORT"] == 18800
        assert SETTINGS_DEFAULTS["A2A_HOST"] == "127.0.0.1"
        assert SETTINGS_DEFAULTS["A2A_MAX_CONCURRENT"] == 3
        assert SETTINGS_DEFAULTS["A2A_TASK_TTL_HOURS"] == 24

    def test_frozen_tool_modules_includes_a2a(self):
        from ouroboros.tools.registry import ToolRegistry
        assert "a2a" in ToolRegistry._FROZEN_TOOL_MODULES

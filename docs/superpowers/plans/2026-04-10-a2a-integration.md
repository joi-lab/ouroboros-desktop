# A2A Protocol Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ouroboros Desktop both an A2A server (discoverable and callable by other agents) and an A2A client (able to discover/call other A2A agents via tools).

**Architecture:** Separate Starlette app on port 18800 using `a2a-sdk[http-server]`. Incoming A2A messages bridge to the existing supervisor via `handle_chat_direct()`. Outgoing A2A calls exposed as three tools in ToolRegistry.

**Tech Stack:** `a2a-sdk[http-server]`, `httpx`, Starlette, uvicorn, asyncio

**Spec:** `docs/superpowers/specs/2026-04-10-a2a-integration-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `requirements.txt` | Modify | Add `a2a-sdk[http-server]` and `httpx` |
| `ouroboros/config.py` | Modify | Add A2A settings to `SETTINGS_DEFAULTS` |
| `supervisor/message_bus.py` | Modify | Add `subscribe_response` / `unsubscribe_response` to `LocalChatBridge` |
| `ouroboros/a2a_task_store.py` | Create | File-based `TaskStore` implementation |
| `ouroboros/a2a_executor.py` | Create | `AgentExecutor` bridge to supervisor |
| `ouroboros/a2a_server.py` | Create | A2A Starlette app, dynamic Agent Card, server lifecycle |
| `ouroboros/tools/a2a.py` | Create | Three client tools: `a2a_discover`, `a2a_send`, `a2a_status` |
| `ouroboros/tools/registry.py` | Modify | Add `"a2a"` to `_FROZEN_TOOL_MODULES` |
| `server.py` | Modify | Launch/stop A2A server in lifespan |

---

### Task 1: Add Dependencies and Configuration

**Files:**
- Modify: `requirements.txt`
- Modify: `ouroboros/config.py:41-97`

- [ ] **Step 1: Add dependencies to requirements.txt**

Add these two lines at the end of `requirements.txt` (before any comments):

```
a2a-sdk[http-server]>=0.3.20
httpx>=0.27.0
```

- [ ] **Step 2: Install the new dependencies**

Run: `cd /home/mr8bit/Project/ouroboros-desktop && pip install -r requirements.txt`
Expected: Successful install of `a2a-sdk`, `httpx`, and their transitive deps.

- [ ] **Step 3: Verify a2a-sdk is importable**

Run: `python -c "from a2a.types import AgentCard; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Add A2A settings to SETTINGS_DEFAULTS**

In `ouroboros/config.py`, add these entries to the `SETTINGS_DEFAULTS` dict, after the `OUROBOROS_FILE_BROWSER_DEFAULT` line (before the closing `}`):

```python
    # A2A (Agent-to-Agent) protocol
    "A2A_ENABLED": True,
    "A2A_PORT": 18800,
    "A2A_HOST": "0.0.0.0",
    "A2A_AGENT_NAME": "",
    "A2A_AGENT_DESCRIPTION": "",
    "A2A_MAX_CONCURRENT": 3,
    "A2A_TASK_TTL_HOURS": 24,
```

- [ ] **Step 5: Commit**

```bash
git add requirements.txt ouroboros/config.py
git commit -m "feat(a2a): add a2a-sdk dependency and A2A settings"
```

---

### Task 2: File-Based Task Store (`ouroboros/a2a_task_store.py`)

**Files:**
- Create: `ouroboros/a2a_task_store.py`

- [ ] **Step 1: Create the file task store**

Create `ouroboros/a2a_task_store.py`:

```python
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
```

- [ ] **Step 2: Verify import**

Run: `python -c "from ouroboros.a2a_task_store import FileTaskStore; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ouroboros/a2a_task_store.py
git commit -m "feat(a2a): file-based TaskStore implementation"
```

---

### Task 3: Response Subscription on LocalChatBridge

**Files:**
- Modify: `supervisor/message_bus.py:57-68` (constructor) and `supervisor/message_bus.py:436-468` (send_message)

- [ ] **Step 1: Add subscription state to LocalChatBridge.__init__**

In `supervisor/message_bus.py`, in the `LocalChatBridge.__init__` method, add after the `self._broadcast_fn = None` line:

```python
        # A2A response subscriptions: {subscription_id: (chat_id, callback)}
        self._response_subs: Dict[str, tuple] = {}
        self._response_subs_lock = threading.Lock()
```

- [ ] **Step 2: Add subscribe_response and unsubscribe_response methods**

In `supervisor/message_bus.py`, add these methods to `LocalChatBridge`, after the `configure_from_settings` method (around line 116):

```python
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
```

- [ ] **Step 3: Hook subscriptions into send_message**

In `supervisor/message_bus.py`, in the `send_message` method, add this block right after the `self._outbox.put(msg)` line (line 456) and before the `if self._broadcast_fn:` block:

```python
        # Notify A2A response subscribers
        with self._response_subs_lock:
            subs = [(sid, cb) for sid, (cid, cb) in self._response_subs.items()
                    if cid == chat_id and not is_progress]
        for sid, cb in subs:
            try:
                cb(clean_text)
            except Exception:
                log.debug("A2A response callback error for sub %s", sid, exc_info=True)
```

- [ ] **Step 4: Verify no import errors**

Run: `python -c "from supervisor.message_bus import LocalChatBridge; b = LocalChatBridge(); print('OK')"`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add supervisor/message_bus.py
git commit -m "feat(a2a): add response subscription to LocalChatBridge"
```

---

### Task 4: A2A Executor (`ouroboros/a2a_executor.py`)

**Files:**
- Create: `ouroboros/a2a_executor.py`

- [ ] **Step 1: Create the executor**

Create `ouroboros/a2a_executor.py`:

```python
"""
A2A — Agent Executor.

Bridges incoming A2A messages to the Ouroboros supervisor via
handle_chat_direct(). Collects responses through LocalChatBridge
subscription mechanism.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.types import (
    Artifact,
    Message,
    Part,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    TextPart,
    Role,
)

log = logging.getLogger("a2a-server")

# Virtual chat_id range for A2A (negative, avoids collision with web=1, telegram=positive)
_A2A_CHAT_ID_BASE = -1000
_a2a_seq = 0
_a2a_seq_lock = threading.Lock()


def _next_a2a_chat_id() -> int:
    global _a2a_seq
    with _a2a_seq_lock:
        _a2a_seq += 1
        return _A2A_CHAT_ID_BASE - _a2a_seq


class OuroborosA2AExecutor(AgentExecutor):
    """Bridges A2A protocol to Ouroboros supervisor."""

    def __init__(self, max_concurrent: int = 3):
        self._semaphore = threading.Semaphore(max_concurrent)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id

        # Extract text from incoming message
        text = self._extract_text(context.message)
        if not text:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.failed,
                        timestamp=datetime.now(timezone.utc),
                        message=Message(
                            messageId=uuid.uuid4().hex,
                            role=Role.agent,
                            parts=[Part(root=TextPart(text="Empty message received"))],
                        ),
                    ),
                )
            )
            return

        # Check concurrency
        if not self._semaphore.acquire(blocking=False):
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.rejected,
                        timestamp=datetime.now(timezone.utc),
                        message=Message(
                            messageId=uuid.uuid4().hex,
                            role=Role.agent,
                            parts=[Part(root=TextPart(text="Too many concurrent tasks"))],
                        ),
                    ),
                )
            )
            return

        try:
            # Signal working
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=False,
                    status=TaskStatus(
                        state=TaskState.working,
                        timestamp=datetime.now(timezone.utc),
                    ),
                )
            )

            # Dispatch to supervisor and wait for response
            response_text = await self._dispatch_to_supervisor(text, event_queue, task_id, context_id)

            # Publish result
            artifact = Artifact(
                artifactId=uuid.uuid4().hex[:12],
                parts=[Part(root=TextPart(text=response_text))],
            )
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    artifact=artifact,
                )
            )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.completed,
                        timestamp=datetime.now(timezone.utc),
                    ),
                )
            )
        except Exception as exc:
            log.error("A2A task %s failed: %s", task_id, exc, exc_info=True)
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.failed,
                        timestamp=datetime.now(timezone.utc),
                        message=Message(
                            messageId=uuid.uuid4().hex,
                            role=Role.agent,
                            parts=[Part(root=TextPart(text=f"Task failed: {exc}"))],
                        ),
                    ),
                )
            )
        finally:
            self._semaphore.release()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                taskId=context.task_id,
                contextId=context.context_id,
                final=True,
                status=TaskStatus(
                    state=TaskState.canceled,
                    timestamp=datetime.now(timezone.utc),
                ),
            )
        )

    async def _dispatch_to_supervisor(
        self,
        text: str,
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
    ) -> str:
        """Send message to Ouroboros agent and wait for response."""
        from supervisor.message_bus import get_bridge
        from supervisor.workers import handle_chat_direct

        bridge = get_bridge()
        chat_id = _next_a2a_chat_id()
        response_event = asyncio.Event()
        response_holder: dict = {}
        loop = asyncio.get_running_loop()

        def on_response(resp_text: str) -> None:
            response_holder["text"] = resp_text
            loop.call_soon_threadsafe(response_event.set)

        sub_id = bridge.subscribe_response(chat_id, on_response)
        try:
            # Run handle_chat_direct in a thread (it's blocking)
            await asyncio.to_thread(handle_chat_direct, chat_id, text, None)

            # Wait for response with timeout (use hard timeout from config)
            timeout_sec = int(
                os.environ.get("OUROBOROS_HARD_TIMEOUT_SEC", "1800")
            )
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Agent did not respond within {timeout_sec}s")

            return response_holder.get("text", "(no response)")
        finally:
            bridge.unsubscribe_response(sub_id)

    @staticmethod
    def _extract_text(message: Optional[Message]) -> str:
        if not message or not message.parts:
            return ""
        texts = []
        for part in message.parts:
            inner = part.root if hasattr(part, "root") else part
            if isinstance(inner, TextPart):
                texts.append(inner.text)
            elif hasattr(inner, "text"):
                texts.append(str(inner.text))
        return "\n".join(texts).strip()
```

Add the missing import at the top:

```python
import os
```

- [ ] **Step 2: Verify import**

Run: `python -c "from ouroboros.a2a_executor import OuroborosA2AExecutor; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ouroboros/a2a_executor.py
git commit -m "feat(a2a): A2A executor bridging to supervisor"
```

---

### Task 5: A2A Server with Dynamic Agent Card (`ouroboros/a2a_server.py`)

**Files:**
- Create: `ouroboros/a2a_server.py`

- [ ] **Step 1: Create the A2A server module**

Create `ouroboros/a2a_server.py`:

```python
"""
A2A — Server module.

Runs a separate Starlette/uvicorn server on A2A_PORT (default 18800).
Serves the dynamic Agent Card and handles A2A JSON-RPC requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import pathlib
import re
import socket
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, List, Optional

import uvicorn
from a2a.server.apps import A2AStarletteApplication
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import AgentCard, AgentCapabilities, AgentSkill

from ouroboros.a2a_executor import OuroborosA2AExecutor
from ouroboros.a2a_task_store import FileTaskStore

log = logging.getLogger("a2a-server")

# Module-level server reference for lifecycle management
_server: Optional[uvicorn.Server] = None
_cleanup_task: Optional[asyncio.Task] = None

# Cached agent card
_card_cache: Dict[str, Any] = {}


def _setup_logging(data_dir: pathlib.Path) -> None:
    """Configure A2A server logging."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "a2a.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    a2a_log = logging.getLogger("a2a-server")
    if not a2a_log.handlers:
        a2a_log.addHandler(handler)
        a2a_log.addHandler(logging.StreamHandler())
        a2a_log.setLevel(logging.INFO)


def _resolve_host(configured_host: str) -> str:
    """Resolve 0.0.0.0 to an actual hostname for the Agent Card URL."""
    if configured_host in ("0.0.0.0", "::"):
        try:
            return socket.getfqdn() or socket.gethostname() or "localhost"
        except Exception:
            return "localhost"
    return configured_host


def _parse_identity(data_dir: pathlib.Path) -> tuple:
    """Extract name and description from identity.md. Returns (name, description)."""
    identity_path = data_dir / "memory" / "identity.md"
    if not identity_path.exists():
        return "", ""
    try:
        content = identity_path.read_text(encoding="utf-8")
        lines = content.strip().splitlines()
        name = ""
        desc_lines = []
        for i, line in enumerate(lines):
            if not name and line.startswith("# "):
                # Parse "# I Am Ouroboros" -> "Ouroboros"
                raw = line.lstrip("# ").strip()
                name = re.sub(r"^I\s+Am\s+", "", raw, flags=re.IGNORECASE).strip()
                continue
            if name and not desc_lines and not line.strip():
                continue  # skip blank lines after heading
            if name and line.startswith("---"):
                break  # stop at first horizontal rule
            if name and line.startswith("## "):
                break  # stop at next heading
            if name and line.strip():
                desc_lines.append(line.strip())
                if len(desc_lines) >= 3:
                    break
        desc = " ".join(desc_lines)
        return name, desc
    except Exception:
        return "", ""


def _build_skills_from_registry() -> List[AgentSkill]:
    """Build A2A skills from the ToolRegistry."""
    try:
        from supervisor.workers import _get_chat_agent
        agent = _get_chat_agent()
        registry = agent.registry
        skills = []
        for entry_name in registry.available_tools():
            schemas = registry.schemas()
            for schema_item in schemas:
                func = schema_item.get("function", {})
                if func.get("name") == entry_name:
                    # Derive tag from prefix
                    prefix = entry_name.split("_")[0] if "_" in entry_name else "tool"
                    skills.append(AgentSkill(
                        id=entry_name,
                        name=entry_name,
                        description=func.get("description", ""),
                        tags=[prefix],
                    ))
                    break
        return skills
    except Exception:
        log.debug("ToolRegistry not available yet, using fallback skills", exc_info=True)
        return [
            AgentSkill(
                id="general",
                name="General Assistant",
                description="Code editing, analysis, git operations, web search, file management",
                tags=["general"],
            )
        ]


def _build_agent_card(settings: Dict[str, Any], host: str, port: int) -> AgentCard:
    """Build a dynamic AgentCard from settings, identity.md, and ToolRegistry."""
    from ouroboros import get_version
    from ouroboros.config import DATA_DIR

    # Name and description
    id_name, id_desc = _parse_identity(DATA_DIR)
    name = settings.get("A2A_AGENT_NAME", "").strip() or id_name or "Ouroboros"
    description = (
        settings.get("A2A_AGENT_DESCRIPTION", "").strip()
        or id_desc
        or "Self-modifying AI agent"
    )

    resolved_host = _resolve_host(host)
    url = f"http://{resolved_host}:{port}/"

    skills = _build_skills_from_registry()

    return AgentCard(
        name=name,
        description=description,
        url=url,
        version=get_version(),
        skills=skills,
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )


async def _task_cleanup_loop(store: FileTaskStore, interval_sec: int = 3600) -> None:
    """Periodically clean up expired tasks."""
    while True:
        await asyncio.sleep(interval_sec)
        try:
            removed = await store.cleanup_expired()
            if removed:
                log.info("A2A task cleanup: removed %d expired tasks", removed)
        except Exception:
            log.warning("A2A task cleanup error", exc_info=True)


async def start_a2a_server(settings: Dict[str, Any]) -> None:
    """Start the A2A server as an async task."""
    global _server, _cleanup_task

    from ouroboros.config import DATA_DIR

    host = str(settings.get("A2A_HOST", "0.0.0.0")).strip()
    port = int(settings.get("A2A_PORT", 18800))
    max_concurrent = int(settings.get("A2A_MAX_CONCURRENT", 3))
    ttl_hours = int(settings.get("A2A_TASK_TTL_HOURS", 24))

    _setup_logging(DATA_DIR)
    log.info("Starting A2A server on %s:%d", host, port)

    # Build components
    task_store = FileTaskStore(DATA_DIR, ttl_hours=ttl_hours)
    executor = OuroborosA2AExecutor(max_concurrent=max_concurrent)
    agent_card = _build_agent_card(settings, host, port)

    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    a2a_app = A2AStarletteApplication(
        agent_card=agent_card,
        http_handler=handler,
    )
    starlette_app = a2a_app.build()

    # Start cleanup loop
    _cleanup_task = asyncio.create_task(
        _task_cleanup_loop(task_store), name="a2a-task-cleanup"
    )

    # Run uvicorn
    config = uvicorn.Config(
        starlette_app,
        host=host,
        port=port,
        log_level="warning",
    )
    _server = uvicorn.Server(config)
    await _server.serve()


def stop_a2a_server() -> None:
    """Signal the A2A server to shut down."""
    global _server, _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        _cleanup_task = None
    if _server:
        _server.should_exit = True
        _server = None
    log.info("A2A server shutdown requested")
```

- [ ] **Step 2: Verify import**

Run: `python -c "from ouroboros.a2a_server import start_a2a_server, stop_a2a_server; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add ouroboros/a2a_server.py
git commit -m "feat(a2a): A2A Starlette server with dynamic Agent Card"
```

---

### Task 6: Client Tools (`ouroboros/tools/a2a.py`)

**Files:**
- Create: `ouroboros/tools/a2a.py`
- Modify: `ouroboros/tools/registry.py:211-216`

- [ ] **Step 1: Create the client tools module**

Create `ouroboros/tools/a2a.py`:

```python
"""A2A client tools: discover, send, check status of other A2A agents."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger("a2a-server")


def _a2a_discover(ctx: ToolContext, url: str) -> str:
    """Fetch and summarize an A2A agent's Agent Card."""
    import httpx

    base = url.rstrip("/")
    card_url = f"{base}/.well-known/agent.json"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(card_url)
            resp.raise_for_status()
            card = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code} from {card_url}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch agent card: {e}"})

    skills = card.get("skills", [])
    skill_list = []
    for s in skills:
        name = s.get("name", s.get("id", "unknown"))
        desc = s.get("description", "")
        skill_list.append({"name": name, "description": desc})

    result = {
        "name": card.get("name", ""),
        "description": card.get("description", ""),
        "version": card.get("version", ""),
        "url": card.get("url", base),
        "capabilities": card.get("capabilities", {}),
        "skills": skill_list,
        "input_modes": card.get("defaultInputModes", []),
        "output_modes": card.get("defaultOutputModes", []),
    }
    return json.dumps(result, ensure_ascii=False, indent=2)


def _a2a_send(
    ctx: ToolContext,
    url: str,
    message: str,
    task_id: str = "",
    context_id: str = "",
) -> str:
    """Send a message to an A2A agent via JSON-RPC SendMessage."""
    import httpx

    base = url.rstrip("/")
    msg_id = uuid.uuid4().hex

    # Build the JSON-RPC request
    params: Dict[str, Any] = {
        "message": {
            "messageId": msg_id,
            "role": "user",
            "parts": [{"kind": "text", "text": message}],
        },
    }
    if task_id:
        params["message"]["taskId"] = task_id
    if context_id:
        params["message"]["contextId"] = context_id

    payload = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": "message/send",
        "params": params,
    }

    try:
        with httpx.Client(timeout=120) as client:
            resp = client.post(base + "/", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"Request failed: {e}"})

    if "error" in data:
        return json.dumps({"error": data["error"]}, ensure_ascii=False, indent=2)

    result = data.get("result", {})

    # Result can be a Task or a Message
    task_id_out = result.get("id", "")
    status = result.get("status", {})
    state = status.get("state", "")

    # Extract response text from artifacts or status message
    response_text = ""
    artifacts = result.get("artifacts", [])
    for art in artifacts:
        for part in art.get("parts", []):
            if "text" in part:
                response_text += part["text"]

    # If no artifacts, check if it's a direct message response
    if not response_text and "parts" in result:
        for part in result.get("parts", []):
            if "text" in part:
                response_text += part["text"]

    output = {
        "task_id": task_id_out,
        "status": state,
        "response": response_text or None,
    }
    if result.get("contextId"):
        output["context_id"] = result["contextId"]

    return json.dumps(output, ensure_ascii=False, indent=2)


def _a2a_status(ctx: ToolContext, url: str, task_id: str) -> str:
    """Check the status of an A2A task via JSON-RPC GetTask."""
    import httpx

    base = url.rstrip("/")
    req_id = uuid.uuid4().hex

    payload = {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tasks/get",
        "params": {"id": task_id},
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(base + "/", json=payload)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return json.dumps({"error": f"HTTP {e.response.status_code}"})
    except Exception as e:
        return json.dumps({"error": f"Request failed: {e}"})

    if "error" in data:
        return json.dumps({"error": data["error"]}, ensure_ascii=False, indent=2)

    result = data.get("result", {})
    status = result.get("status", {})
    state = status.get("state", "")

    # Extract response text from artifacts
    response_text = ""
    for art in result.get("artifacts", []):
        for part in art.get("parts", []):
            if "text" in part:
                response_text += part["text"]

    # Extract status message
    status_message = ""
    status_msg = status.get("message", {})
    if status_msg:
        for part in status_msg.get("parts", []):
            if "text" in part:
                status_message += part["text"]

    output = {
        "task_id": result.get("id", task_id),
        "status": state,
        "response": response_text or None,
        "status_message": status_message or None,
    }
    if result.get("contextId"):
        output["context_id"] = result["contextId"]

    return json.dumps(output, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("a2a_discover", {
            "name": "a2a_discover",
            "description": (
                "Discover an A2A (Agent-to-Agent) agent by fetching its Agent Card. "
                "Returns the agent's name, description, capabilities, and available skills. "
                "Use this to learn what another agent can do before sending it a task."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Base URL of the A2A agent (e.g. 'http://localhost:18800')",
                    },
                },
                "required": ["url"],
            },
        }, _a2a_discover),

        ToolEntry("a2a_send", {
            "name": "a2a_send",
            "description": (
                "Send a message to another A2A agent. Creates a task on the remote agent. "
                "Returns the task ID, status, and response (if the task completed immediately). "
                "For long-running tasks, use a2a_status to check progress later."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Base URL of the A2A agent",
                    },
                    "message": {
                        "type": "string",
                        "description": "The message text to send to the agent",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Optional: ID of existing task to continue a dialogue",
                    },
                    "context_id": {
                        "type": "string",
                        "description": "Optional: context ID for grouping related tasks",
                    },
                },
                "required": ["url", "message"],
            },
        }, _a2a_send),

        ToolEntry("a2a_status", {
            "name": "a2a_status",
            "description": (
                "Check the status of a task on a remote A2A agent. "
                "Returns current state (working/completed/failed/etc), "
                "response text if completed, and any status messages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Base URL of the A2A agent",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "The task ID returned by a2a_send",
                    },
                },
                "required": ["url", "task_id"],
            },
        }, _a2a_status),
    ]
```

- [ ] **Step 2: Add "a2a" to _FROZEN_TOOL_MODULES**

In `ouroboros/tools/registry.py`, in the `_FROZEN_TOOL_MODULES` list, add `"a2a"` at the beginning (alphabetical order):

Change:
```python
    _FROZEN_TOOL_MODULES = [
        "browser", "ci", "claude_advisory_review", "compact_context", "control",
```

To:
```python
    _FROZEN_TOOL_MODULES = [
        "a2a", "browser", "ci", "claude_advisory_review", "compact_context", "control",
```

- [ ] **Step 3: Verify import**

Run: `python -c "from ouroboros.tools.a2a import get_tools; tools = get_tools(); print([t.name for t in tools])"`
Expected: `['a2a_discover', 'a2a_send', 'a2a_status']`

- [ ] **Step 4: Commit**

```bash
git add ouroboros/tools/a2a.py ouroboros/tools/registry.py
git commit -m "feat(a2a): client tools - a2a_discover, a2a_send, a2a_status"
```

---

### Task 7: Integrate A2A Server into server.py Lifespan

**Files:**
- Modify: `server.py:1035-1090` (lifespan function)

- [ ] **Step 1: Add A2A server start to lifespan**

In `server.py`, in the `lifespan` function, add after the local model autostart block (after line 1060, before `try: yield`):

```python
    # A2A server
    a2a_server_task = None
    if settings.get("A2A_ENABLED", True):
        try:
            from ouroboros.a2a_server import start_a2a_server
            a2a_port = int(settings.get("A2A_PORT", 18800))
            a2a_server_task = asyncio.create_task(
                start_a2a_server(settings), name="a2a-server"
            )
            log.info("A2A server task created on port %d", a2a_port)
        except Exception:
            log.warning("Failed to start A2A server", exc_info=True)
```

- [ ] **Step 2: Add A2A server stop to lifespan finally block**

In `server.py`, in the `finally` block of `lifespan`, add at the beginning (before the existing cleanup code, after `yield` / `finally:`):

```python
        # Stop A2A server
        if a2a_server_task:
            try:
                from ouroboros.a2a_server import stop_a2a_server
                stop_a2a_server()
                a2a_server_task.cancel()
            except Exception:
                pass
```

- [ ] **Step 3: Verify server starts without errors**

Run: `cd /home/mr8bit/Project/ouroboros-desktop && timeout 5 python server.py --host 127.0.0.1 --port 0 2>&1 || true`
Expected: Server starts, logs show "A2A server task created on port 18800" (may timeout, that's fine — we just want to see it starts).

- [ ] **Step 4: Commit**

```bash
git add server.py
git commit -m "feat(a2a): launch A2A server from main server lifespan"
```

---

### Task 8: End-to-End Smoke Test

- [ ] **Step 1: Start the server in background**

```bash
cd /home/mr8bit/Project/ouroboros-desktop
python server.py --host 127.0.0.1 --port 8765 &
SERVER_PID=$!
sleep 5
```

- [ ] **Step 2: Test Agent Card endpoint**

```bash
curl -s http://localhost:18800/.well-known/agent.json | python -m json.tool
```

Expected: JSON with `name`, `description`, `version`, `skills`, `capabilities.streaming: true`.

- [ ] **Step 3: Test JSON-RPC SendMessage**

```bash
curl -s -X POST http://localhost:18800/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-1",
    "method": "message/send",
    "params": {
      "message": {
        "messageId": "msg-test-1",
        "role": "user",
        "parts": [{"kind": "text", "text": "What is 2+2?"}]
      }
    }
  }' | python -m json.tool
```

Expected: JSON-RPC response with a Task object containing status and/or artifacts.

- [ ] **Step 4: Test GetTask**

```bash
# Use the task_id from step 3
curl -s -X POST http://localhost:18800/ \
  -H "Content-Type: application/json" \
  -d '{
    "jsonrpc": "2.0",
    "id": "test-2",
    "method": "tasks/get",
    "params": {"id": "TASK_ID_FROM_STEP_3"}
  }' | python -m json.tool
```

Expected: Task with status `completed` and artifacts containing the response.

- [ ] **Step 5: Stop server and commit**

```bash
kill $SERVER_PID 2>/dev/null || true
```

Final commit:

```bash
git add -A
git commit -m "feat(a2a): A2A protocol integration complete

Ouroboros Desktop now supports the Google Agent2Agent (A2A) protocol:
- A2A server on port 18800 with dynamic Agent Card
- JSON-RPC endpoints: SendMessage, GetTask, CancelTask
- SSE streaming support
- File-based task persistence
- Client tools: a2a_discover, a2a_send, a2a_status
- Configurable via settings (A2A_ENABLED, A2A_PORT, etc.)"
```

# A2A Protocol Integration for Ouroboros Desktop

**Date:** 2026-04-10
**Status:** Approved
**Approach:** Python A2A SDK (`a2a-sdk[http-server]`)

## Overview

Integrate the Google Agent2Agent (A2A) protocol into Ouroboros Desktop, making the agent both a **server** (discoverable and callable by other A2A agents) and a **client** (able to discover and call other A2A agents via tools).

Key parameters:
- SSE streaming: yes
- Authentication: none (open access, can be added later)
- Port: separate, default 18800 (`OUROBOROS_A2A_PORT`)
- Client exposure: three tools (`a2a_discover`, `a2a_send`, `a2a_status`)
- SDK: `a2a-sdk[http-server]` (official Google SDK, v0.3.x)

## Architecture

```
                Exernal A2A Agents
                      |
                +-----v------+
                | A2A Server |  <- separate uvicorn on :18800
                | (Starlette)|
                |            |
                | /.well-known/agent.json  (Agent Card)
                | /            (JSON-RPC POST)
                | /stream      (SSE streaming)
                +-----+------+
                      |
               +------v-------+
               | A2AExecutor   |  <- bridge: A2A Task -> Ouroboros
               |               |     handle_chat_direct()
               | TaskStore     |  <- file-based task persistence
               +------+-------+
                      |
          +-----------v-----------+
          |  Existing Supervisor   |
          |  handle_chat_direct()  |
          |  LocalChatBridge       |
          +-----------------------+

    Ouroboros Agent (LLM)
         |
    +----v-----+
    | Tools    |
    | a2a_discover  <- fetch Agent Card
    | a2a_send      <- send task to agent
    | a2a_status    <- check task status
    +----------+
         |
    Other A2A Agents (client)
```

### New Files

| File | Responsibility |
|------|---------------|
| `ouroboros/a2a_server.py` | A2A Starlette app, JSON-RPC dispatcher, SSE, Agent Card endpoint |
| `ouroboros/a2a_executor.py` | Bridge between A2A protocol and supervisor (`handle_chat_direct`) |
| `ouroboros/a2a_task_store.py` | File-based task persistence |
| `ouroboros/tools/a2a.py` | Three client tools for ToolRegistry |

### Modified Files

| File | Change |
|------|--------|
| `server.py` | Launch/stop A2A server in lifespan |
| `ouroboros/config.py` | Add A2A settings to SETTINGS_DEFAULTS |
| `supervisor/message_bus.py` | Add `subscribe_response` / `unsubscribe_response` to LocalChatBridge |
| `ouroboros/tools/registry.py` | Add `"a2a"` to `_FROZEN_TOOL_MODULES` |

### New Dependency

`a2a-sdk[http-server]` added to `requirements.txt`.

## Dynamic Agent Card

Served at `GET /.well-known/agent.json`. Built dynamically, cached in memory with ETag.

### Name and Description

- `name`: from `settings["A2A_AGENT_NAME"]`, fallback -> parse first heading of `~/Ouroboros/data/memory/identity.md` (e.g. `# I Am Ouroboros` -> `"Ouroboros"`)
- `description`: from `settings["A2A_AGENT_DESCRIPTION"]`, fallback -> first paragraph of `identity.md` after the heading
- `version`: from `ouroboros.get_version()`

### Skills from ToolRegistry

Each `ToolEntry` in the registry is mapped to an `AgentSkill`:

```
ToolEntry(name="web_search", schema={"name": "web_search", "description": "Search the web..."})
->
AgentSkill(id="web_search", name="web_search", description="Search the web...", tags=["tool"])
```

Grouping: tools with a common prefix (git_*, repo_*, a2a_*) share a tag based on the prefix.

If supervisor is not ready yet, a hardcoded fallback with a minimal skill set is returned.

The result is cached in memory and rebuilt when tools or identity change.

### URL

Auto-formed: `http://{hostname}:{a2a_port}/`
- hostname via `socket.getfqdn()`, fallback to `OUROBOROS_A2A_HOST`
- If host is `0.0.0.0`, resolved to actual IP via `socket.gethostname()`

### HTTP Headers

`Cache-Control: max-age=300, public` and `ETag` based on content hash.

### Static Fields

```json
{
  "protocolVersion": "0.3.0",
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"]
}
```

## A2A Server (`ouroboros/a2a_server.py`)

### Routes

| Route | Method | Handler |
|-------|--------|---------|
| `/.well-known/agent.json` | GET | Dynamic Agent Card |
| `/` | POST | JSON-RPC 2.0 dispatcher (SendMessage, GetTask, CancelTask) |
| `/stream` | POST | SendStreamingMessage -> SSE via StreamingResponse |

### JSON-RPC Methods

Uses `A2AStarletteApplication` from `a2a-sdk` which routes JSON-RPC methods to our `AgentExecutor` implementation.

Supported methods:
- `SendMessage` -> create task, dispatch to agent, return result
- `SendStreamingMessage` -> same but streams status/artifact updates via SSE
- `GetTask` -> retrieve task from TaskStore
- `CancelTask` -> cancel running task

### Server Lifecycle

Started from `server.py` lifespan as an `asyncio.create_task`:

```python
if settings.get("A2A_ENABLED", True):
    from ouroboros.a2a_server import start_a2a_server, stop_a2a_server
    a2a_server_task = asyncio.create_task(
        start_a2a_server(settings, tool_registry_ref, a2a_port)
    )
```

Stopped in lifespan `finally` block via `stop_a2a_server()`.

Separate uvicorn instance running in the same event loop.

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `A2A_ENABLED` | `true` | Enable/disable A2A server |
| `A2A_PORT` | `18800` | A2A server port |
| `A2A_HOST` | `"0.0.0.0"` | A2A server bind address |
| `A2A_AGENT_NAME` | `""` | Override agent name (empty = auto from identity.md) |
| `A2A_AGENT_DESCRIPTION` | `""` | Override description (empty = auto from identity.md) |
| `A2A_MAX_CONCURRENT` | `3` | Max concurrent A2A tasks |
| `A2A_TASK_TTL_HOURS` | `24` | Hours before completed tasks are cleaned up |

## A2A Executor (`ouroboros/a2a_executor.py`)

Implements `AgentExecutor` interface from `a2a-sdk`.

### Inbound Flow

1. A2A `SendMessage` arrives with `Message(role="user", parts=[TextPart(...)])`
2. Executor extracts text from message parts
3. Creates entry in TaskStore with status `working`
4. Subscribes to response via `bridge.subscribe_response(chat_id, callback)`
5. Calls `handle_chat_direct(chat_id=A2A_VIRTUAL_CHAT_ID, text=...)` using a virtual chat_id. Virtual IDs use the range `-1000 - seq` (e.g. -1001, -1002, ...) to avoid collision with real chat_ids (web UI = 1, Telegram = positive integers).
6. Callback receives agent response, writes result to TaskStore
7. Unsubscribes

### Response Interception

New methods on `LocalChatBridge`:

```python
def subscribe_response(self, chat_id: int, callback: Callable[[str], None]) -> str:
    """Subscribe to next agent response for this chat_id. Returns subscription_id."""

def unsubscribe_response(self, subscription_id: str) -> None:
    """Remove subscription."""
```

In `send_message()`, before broadcasting, check for active subscriptions matching the chat_id and invoke callbacks.

### SSE Streaming

- Executor publishes intermediate messages (progress) as `StatusUpdate` events via an `asyncio.Queue`
- `SendStreamingMessage` endpoint reads from this queue and emits SSE
- Final response -> `TaskArtifactUpdate` + `TaskStatusUpdate(completed)`

### Concurrency

- Each A2A request = separate `handle_chat_direct` call in a new thread (existing supervisor behavior)
- Limit: `A2A_MAX_CONCURRENT` (default 3), enforced by a `threading.Semaphore`
- Over limit: task created with status `rejected`, message "Too many concurrent tasks"

### Error Handling

| Condition | A2A Response |
|-----------|-------------|
| Supervisor not ready | Task status `failed`, message "Agent not ready" |
| Budget exhausted | Task status `rejected`, message "Budget exhausted" |
| handle_chat_direct timeout | Task status `failed`, message "Task timed out" |
| Invalid message format | JSON-RPC error -32602 (invalid params) |
| Task not found | JSON-RPC error -32001 (TaskNotFoundError) |
| Task not cancelable | JSON-RPC error -32002 (TaskNotCancelableError) |

## Task Store (`ouroboros/a2a_task_store.py`)

File-based task persistence, consistent with the project's existing patterns.

### Storage

- Directory: `~/Ouroboros/data/a2a_tasks/`
- One file per task: `{task_id}.json`
- Atomic writes via `write -> rename` (same pattern as `supervisor/state.py`)

### Task File Structure

```json
{
  "id": "a2a-xxxxxxxxxxxx",
  "contextId": "ctx-xxxxxxxxxxxx",
  "status": {
    "state": "completed",
    "timestamp": "2026-04-10T12:00:00Z"
  },
  "history": [
    {"role": "user", "parts": [{"text": "..."}]},
    {"role": "agent", "parts": [{"text": "..."}]}
  ],
  "artifacts": [
    {"artifactId": "art-1", "parts": [{"text": "response text"}]}
  ],
  "metadata": {
    "source": "a2a",
    "created_at": "2026-04-10T12:00:00Z"
  }
}
```

### Interface

Implements `TaskStore` from `a2a-sdk`:
- `get(task_id)` -> Task or None
- `save(task)` -> write/update
- `delete(task_id)` -> remove file
- `list(context_id?, state?, limit?, offset?)` -> filtered pagination

### TTL Cleanup

- Tasks in terminal states (completed, failed, canceled, rejected) deleted after `A2A_TASK_TTL_HOURS` (default 24)
- Cleanup runs hourly as `asyncio.create_task` from lifespan
- Reads file mtime to determine age

## Client Tools (`ouroboros/tools/a2a.py`)

Three tools registered in ToolRegistry via `get_tools() -> List[ToolEntry]`.

### a2a_discover

```
Parameters: url (string) - base URL of A2A agent (e.g. "http://localhost:18800")
Returns: JSON summary of Agent Card - name, description, skills list, capabilities
```

- GET `{url}/.well-known/agent.json`
- Parses response, returns readable summary
- Timeout: 10 seconds
- Uses `httpx.AsyncClient` for async HTTP

### a2a_send

```
Parameters:
  url (string) - base URL of A2A agent
  message (string) - message text
  task_id (string, optional) - existing task ID to continue dialogue
  context_id (string, optional) - context ID for task grouping
Returns: JSON with task_id, status, and response text (if completed)
```

- Forms JSON-RPC `SendMessage` request via `A2AClient` from `a2a-sdk`
- If agent returns completed task: returns result immediately
- If working/submitted: returns task_id and status for later polling
- If input-required: returns request text and task_id for continuation
- Timeout: 120 seconds

### a2a_status

```
Parameters:
  url (string) - base URL of A2A agent
  task_id (string) - task ID
Returns: JSON with task status, artifacts (if completed), message history
```

- Forms JSON-RPC `GetTask` request via `A2AClient`
- Returns current status and result if task is done
- Timeout: 10 seconds

### Registration

Module exports `get_tools()`, auto-discovered by `ToolRegistry._load_modules()`.
`"a2a"` added to `_FROZEN_TOOL_MODULES` for PyInstaller builds.

## Logging

- Separate logger: `a2a-server`
- Log file: `~/Ouroboros/data/logs/a2a.log` (RotatingFileHandler, 2MB, 3 backups)
- Same format as main server: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`

## Integration with server.py

### Lifespan Changes

In `lifespan()` after supervisor start:
```python
a2a_server_task = None
if settings.get("A2A_ENABLED", True):
    from ouroboros.a2a_server import start_a2a_server, stop_a2a_server
    a2a_port = int(settings.get("A2A_PORT", 18800))
    a2a_server_task = asyncio.create_task(
        start_a2a_server(settings, tool_registry_ref, a2a_port)
    )
```

In `finally`:
```python
if a2a_server_task:
    stop_a2a_server()
    a2a_server_task.cancel()
```

### ToolRegistry Reference

A2A server needs access to ToolRegistry for dynamic Agent Card. Uses lazy reference: stores `Optional[ToolRegistry]`, on first Agent Card request attempts to get via `_get_chat_agent().registry`. Falls back to hardcoded minimal skills if supervisor not ready.

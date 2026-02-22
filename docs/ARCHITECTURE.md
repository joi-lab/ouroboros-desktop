# Ouroboros v2.0.0 — Architecture & Reference

This document describes every component, page, button, API endpoint, and data flow.
It is the single source of truth for how the system works. Keep it updated.

---

## 1. High-Level Architecture

```
User
  │
  ▼
launcher.py (PyWebView)       ← macOS desktop window, immutable process manager
  │
  │  spawns subprocess
  ▼
server.py (Starlette+uvicorn) ← HTTP + WebSocket on localhost:8765
  │
  ├── web/                     ← Static HTML/JS/CSS UI (served by Starlette)
  │
  ├── supervisor/              ← Background thread inside server.py
  │   ├── message_bus.py       ← Queue-based message bus (LocalChatBridge)
  │   ├── workers.py           ← Multiprocessing worker pool (fork)
  │   ├── state.py             ← Persistent state (state.json) with file locking
  │   ├── queue.py             ← Task queue management (PENDING/RUNNING lists)
  │   ├── events.py            ← Event dispatcher (worker→supervisor events)
  │   └── git_ops.py           ← Git operations (clone, checkout, rescue, rollback)
  │
  └── ouroboros/               ← Agent core (runs inside worker processes)
      ├── config.py            ← SSOT: paths, settings defaults, load/save, PID lock
      ├── agent.py             ← Task orchestrator
      ├── loop.py              ← LLM tool loop (send→call tools→repeat)
      ├── llm.py               ← OpenRouter API client
      ├── safety.py            ← Dual-layer LLM security supervisor
      ├── consciousness.py     ← Background thinking loop
      ├── memory.py            ← Scratchpad, identity, chat history
      ├── context.py           ← LLM context builder
      ├── tools/               ← Auto-discovered tool plugins
      └── ...
```

### Two-process model

1. **launcher.py** — immutable outer shell. Never self-modifies. Handles:
   - PID lock (single instance)
   - Bootstrap: copies workspace to `~/Ouroboros/repo/` on first run
   - Core file sync: overwrites safety-critical files on every launch
   - Starts `server.py` as a subprocess via embedded Python
   - Shows PyWebView window pointed at `http://127.0.0.1:8765`
   - Monitors subprocess; restarts on exit code 42 (restart signal)
   - First-run wizard (PyWebView HTML page for API key entry)
   - **Graceful shutdown with orphan cleanup** (see Shutdown section below)

2. **server.py** — self-editable inner server. Can be modified by the agent.
   - Starlette app with HTTP API + WebSocket
   - Runs supervisor in a background thread
   - Supervisor manages worker pool, task queue, message routing

### Data layout (`~/Ouroboros/`)

```
~/Ouroboros/
├── repo/              ← Agent's self-modifying git repository
│   ├── server.py      ← The running server (copied from workspace)
│   ├── ouroboros/      ← Agent core package
│   ├── supervisor/     ← Supervisor package
│   ├── web/            ← Web UI files
│   └── prompts/        ← System prompts (SYSTEM.md, SAFETY.md, CONSCIOUSNESS.md)
├── data/
│   ├── settings.json   ← User settings (API keys, models, budget)
│   ├── state/
│   │   ├── state.json  ← Runtime state (spent_usd, session_id, branch, etc.)
│   │   └── queue_snapshot.json
│   ├── memory/
│   │   ├── identity.md     ← Agent's self-description (persistent)
│   │   ├── scratchpad.md   ← Working memory (persistent)
│   │   ├── WORLD.md        ← System profile (generated on first run)
│   │   └── knowledge/      ← Structured knowledge base files
│   ├── logs/
│   │   ├── chat.jsonl      ← Chat message log
│   │   ├── events.jsonl    ← LLM rounds, task lifecycle, errors
│   │   ├── tools.jsonl     ← Tool call log with args/results
│   │   └── supervisor.jsonl ← Supervisor-level events
│   └── archive/            ← Rotated logs, rescue snapshots
└── ouroboros.pid           ← PID lock file (fcntl.flock — auto-released on crash)
```

---

## 2. Startup / Onboarding Flow

```
launcher.py main()
  │
  ├── acquire_pid_lock()        → Show "already running" if locked
  ├── check_git()               → Show "install git" wizard if missing
  ├── bootstrap_repo()          → Copy workspace to ~/Ouroboros/repo/ (first run)
  │                               OR sync core files (subsequent runs)
  ├── _run_first_run_wizard()   → Show API key wizard if no settings.json
  │                               (PyWebView HTML page with key + budget + model fields)
  │                               Saves to ~/Ouroboros/data/settings.json
  ├── agent_lifecycle_loop()    → Background thread: start/monitor server.py
  └── webview.start()           → Open PyWebView window at http://127.0.0.1:8765
```

### First-run wizard

Shown when `settings.json` does not exist or has no `OPENROUTER_API_KEY`.
Fields: OpenRouter API Key (required), Total Budget ($), Main Model.
On save: writes `settings.json`, closes wizard, proceeds to main app.

### Core file sync (`_sync_core_files`)

On every launch (not just first run), these files are copied from the workspace
bundle to `~/Ouroboros/repo/`, ensuring safety-critical code cannot be permanently
corrupted by agent self-modification:

- `prompts/SAFETY.md`, `prompts/SYSTEM.md`
- `ouroboros/config.py`, `ouroboros/safety.py`, `ouroboros/local_model.py`, `ouroboros/tools/registry.py`, `ouroboros/loop.py`
- `supervisor/message_bus.py`
- `server.py`

---

## 3. Web UI Pages & Buttons

The web UI is a single-page app (`web/index.html` + `web/app.js` + `web/style.css`).
Navigation is a left sidebar with 7 pages.

### 3.1 Chat

- **Status badge** (top-right): "Online" (green) / "Thinking..." (amber pulse) / "Reconnecting..." (red).
  Driven by WebSocket connection state and typing events.
- **Message input**: textarea + send button. Shift+Enter for newline, Enter to send.
- **Messages**: user bubbles (right, blue) and assistant bubbles (left, crimson). Assistant messages render markdown.
- **Typing indicator**: animated "thinking dots" bubble appears when the agent is processing.
- Messages sent via WebSocket `{type: "chat", content: text}`.
- Responses arrive via WebSocket `{type: "chat", role: "assistant", content: text}`.
- Supports slash commands: `/status`, `/evolve`, `/review`, `/bg`, `/restart`, `/panic`.

### 3.2 Dashboard

- **Stat cards**: Uptime, Workers (alive/total + progress bar), Budget (spent/limit + bar), Branch@SHA.
- **Toggles**: Evolution Mode (on/off), Background Consciousness (on/off).
  Send `/evolve start|stop` and `/bg start|stop` via WebSocket command.
- **Buttons**:
  - **Force Review** → sends `/review` command. Queues a deep code review task.
  - **Restart Agent** → sends `/restart` command. Graceful restart (save state, kill workers, exit 42).
  - **Panic Stop** → sends `/panic` command (with confirm dialog). Kills all workers immediately.
- Dashboard polls `/api/state` every 3 seconds.

### 3.3 Settings

- **API Keys**: OpenRouter (required), OpenAI (optional, for web search), Anthropic (optional).
  Keys are displayed as masked values (e.g., `sk-or-v1...`).
  Only overwritten on save if user enters a new value (not containing `...`).
- **Models**: Main, Code, Light, Fallback.
- **Runtime**: Max Workers, Budget ($), Soft/Hard Timeout.
- **GitHub**: Token + Repo (for remote sync).
- **Save Settings** button → POST `/api/settings`. Applies to env immediately.
  Budget changes take effect immediately; model/worker changes need restart.
- **Reset All Data** button (Danger Zone) → POST `/api/reset`.
  Deletes: state/, memory/, logs/, archive/, settings.json.
  Keeps: repo/ (agent code).
  Triggers server restart. On next launch, onboarding wizard appears.

### 3.4 Logs

- **Filter chips**: Tools, LLM, Errors, Tasks, System, Consciousness.
  Toggle on/off to filter log entries.
- **Clear** button: clears the in-memory log view (not files on disk).
- Log entries arrive via WebSocket `{type: "log", data: event}`.
- Each entry shows: timestamp, event type (color-coded), message preview.
- Click to expand long entries.
- Max 500 entries in view (oldest removed).

### 3.5 Versions

### 3.6 About

- Logo (large, centered)
- "A self-creating AI agent" description
- Created by Anton Razzhigaev & Andrew Kaznacheev
- Links: @abstractDL (Telegram), GitHub repo
- "Joi Lab" footer

### (previous 3.5) Versions

- **Current branch + SHA** displayed at top.
- **Recent Commits** list with SHA, date, message, and "Restore" button.
- **Tags** list with tag name, date, message, and "Restore" button.
- **Restore** button → POST `/api/git/rollback` with target SHA/tag.
  Creates rescue snapshot, resets to target, restarts server.
- **Promote to Stable** button → POST `/api/git/promote`.
  Updates `ouroboros-stable` branch to match `ouroboros`.
- **Refresh** button → reloads commit/tag lists.

---

## 4. Server API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves `web/index.html` |
| GET | `/api/health` | `{status, version}` |
| GET | `/api/state` | Dashboard data: uptime, workers, budget, branch, etc. |
| GET | `/api/settings` | Current settings with masked API keys |
| POST | `/api/settings` | Update settings (partial update, only provided keys) |
| POST | `/api/command` | Send a slash command `{cmd: "/status"}` |
| POST | `/api/reset` | Delete all runtime data, restart for fresh onboarding |
| GET | `/api/git/log` | Recent commits + tags + current branch/sha |
| POST | `/api/git/rollback` | Rollback to a specific commit/tag `{target: "sha"}` |
| POST | `/api/git/promote` | Promote ouroboros → ouroboros-stable |
| WS | `/ws` | WebSocket: chat messages, commands, log streaming |
| GET | `/static/*` | Static files from `web/` directory |

### WebSocket protocol

**Client → Server:**
- `{type: "chat", content: "text"}` — send chat message
- `{type: "command", cmd: "/status"}` — send slash command

**Server → Client:**
- `{type: "chat", role: "assistant", content: "text"}` — agent response
- `{type: "log", data: {type, ts, ...}}` — real-time log event
- `{type: "typing", action: "typing"}` — typing indicator (show animation)

---

## 5. Supervisor Loop

Runs in a background thread inside `server.py:_run_supervisor()`.

Each iteration (0.5s sleep):
1. `rotate_chat_log_if_needed()` — archive chat.jsonl if > 800KB
2. `ensure_workers_healthy()` — respawn dead workers, detect crash storms
3. Drain event queue (worker→supervisor events via multiprocessing.Queue)
4. `enforce_task_timeouts()` — soft/hard timeout handling
5. `enqueue_evolution_task_if_needed()` — auto-queue evolution if enabled
6. `assign_tasks()` — match pending tasks to free workers
7. `persist_queue_snapshot()` — save queue state for crash recovery
8. Poll `LocalChatBridge` inbox for user messages
9. Route messages: slash commands → supervisor handlers; text → agent

### Slash command handling (server.py main loop)

| Command | Action |
|---------|--------|
| `/panic` | Kill workers (force), request restart exit |
| `/restart` | Save state, safe_restart (git), kill workers, exit 42 |
| `/review` | Queue a review task |
| `/evolve on\|off` | Toggle evolution mode in state, prune evolution tasks if off |
| `/bg start\|stop\|status` | Control background consciousness |
| `/status` | Send status text with budget breakdown |
| (anything else) | Route to agent via `handle_chat_direct()` |

---

## 6. Agent Core

### Task lifecycle

1. Message arrives → `handle_chat_direct(chat_id, text, image_data)`
2. Creates task dict `{id, type, chat_id, text}`
3. `OuroborosAgent.handle_task(task)` →
   a. Build context (`context.py`): system prompt + bible + identity + scratchpad + runtime info
   b. `run_llm_loop()`: LLM call → tool execution → repeat until final text response
   c. Emit events: send_message, task_metrics, task_done
4. Events flow back to supervisor via event queue

### Tool execution (loop.py)

- Core tools always available; extra tools discoverable via `list_available_tools`/`enable_tools`
- Read-only tools can run in parallel (ThreadPoolExecutor)
- Browser tools use thread-sticky executor (Playwright greenlet affinity)
- All tools have hard timeout (default 120s)
- Safety check on mutative tools (run_shell, repo_write_commit, etc.)
- Tool results truncated to 15000 chars
- Context compaction kicks in after round 8 (summarizes old tool results)

### Safety system (safety.py)

Two-layer LLM security:
1. **Layer 1 (fast)**: Light model checks if tool call is SAFE/SUSPICIOUS/DANGEROUS
2. **Layer 2 (deep)**: If flagged, heavy model re-evaluates with "are you sure?" nudge

Hardcoded sandbox in `registry.py`: blocks deletion of BIBLE.md and safety.py regardless of LLM verdict.

### Background consciousness (consciousness.py)

- Daemon thread, sleeps between wakeups (interval controlled by LLM via `set_next_wakeup`)
- Loads: identity, scratchpad, bible, recent observations, runtime state
- Calls LLM with lightweight introspection prompt
- Has limited tool access (memory, messaging, scheduling, read-only)
- Pauses when regular task is running; resumes after
- Budget-capped (default 10% of total)

---

## 7. Configuration (ouroboros/config.py)

Single source of truth for:
- **Paths**: HOME, APP_ROOT, REPO_DIR, DATA_DIR, SETTINGS_PATH, PID_FILE, PORT_FILE
- **Constants**: RESTART_EXIT_CODE (42), AGENT_SERVER_PORT (8765)
- **Settings defaults**: all model names, budget, timeouts, worker count
- **Functions**: `read_version()`, `load_settings()`, `save_settings()`,
  `apply_settings_to_env()`, `acquire_pid_lock()`, `release_pid_lock()`

Settings file: `~/Ouroboros/data/settings.json`. File-locked for concurrent access.

### Default settings

| Key | Default | Description |
|-----|---------|-------------|
| OPENROUTER_API_KEY | "" | Required. Main LLM API key |
| OPENAI_API_KEY | "" | Optional. For web_search tool |
| ANTHROPIC_API_KEY | "" | Optional. For Claude Code CLI |
| OUROBOROS_MODEL | anthropic/claude-sonnet-4.6 | Main reasoning model |
| OUROBOROS_MODEL_CODE | anthropic/claude-sonnet-4.6 | Code editing model |
| OUROBOROS_MODEL_LIGHT | google/gemini-3-flash-preview | Fast/cheap model (safety, consciousness) |
| OUROBOROS_MODEL_FALLBACK | google/gemini-3-flash-preview | Fallback when primary fails |
| CLAUDE_CODE_MODEL | sonnet | Anthropic model for Claude Code CLI (sonnet, opus, or full name) |
| OUROBOROS_MAX_WORKERS | 5 | Worker process pool size |
| TOTAL_BUDGET | 10.0 | Total budget in USD |
| OUROBOROS_SOFT_TIMEOUT_SEC | 600 | Soft timeout warning (10 min) |
| OUROBOROS_HARD_TIMEOUT_SEC | 1800 | Hard timeout kill (30 min) |

---

## 8. Git Branching Model

- **ouroboros** — development branch. Agent commits here.
- **ouroboros-stable** — promoted stable version. Updated via "Promote to Stable" button.
- **main** — belongs to the creator. Agent never touches it.

| LOCAL_MODEL_ENABLED | false | Enable local model support |
`safe_restart()` does `git checkout -f ouroboros` + `git reset --hard` on the repo.
Uncommitted changes are rescued to `~/Ouroboros/data/archive/rescue/` before reset.

---

## 9. Shutdown & Process Cleanup

**Requirement: closing the window (X button or Cmd+Q) MUST leave zero orphan
processes. No zombies, no workers lingering in background.**

### 9.1 Normal Shutdown (window close)

```
1. _shutdown_event.set()           ← signal lifecycle loop to exit
2. stop_agent()
   a. SIGTERM → server.py          ← server runs its lifespan shutdown:
      │                                kill_workers(force=True) → SIGTERM+SIGKILL all workers
      │                                then server exits cleanly
   b. wait 10s for exit
   c. if still alive → SIGKILL     ← hard kill (workers may orphan)
3. _kill_orphaned_children()        ← SAFETY NET
   a. _kill_stale_on_port(8765)    ← lsof port, SIGKILL any survivors
   b. multiprocessing.active_children() → SIGKILL each
4. release_pid_lock()               ← delete ~/Ouroboros/ouroboros.pid
```

This three-layer approach (graceful → force-kill server → sweep port/children)
guarantees no orphans even if the server hangs or workers resist SIGTERM.

### 9.2 Panic Stop (`/panic` command or Panic Stop button)

**Panic is a full emergency stop. Not a restart — a complete shutdown.**

The panic sequence (in `server.py:_execute_panic_stop()`):

```
1. consciousness.stop()             ← stop background consciousness thread
2. Save state: evolution_mode_enabled=False, bg_consciousness_enabled=False
3. Write ~/Ouroboros/data/state/panic_stop.flag
4. LocalModelManager.stop_server()   ← kill local model server if running
5. kill_all_tracked_subprocesses()   ← os.killpg(SIGKILL) every tracked
   │                                    subprocess process group (claude CLI,
   │                                    shell commands, and ALL their children)
6. kill_workers(force=True)          ← SIGTERM+SIGKILL all multiprocessing workers
7. os._exit(99)                      ← immediate hard exit, kills daemon threads
```

Launcher handles exit code 99:

```
7. Launcher detects exit_code == PANIC_EXIT_CODE (99)
8. _shutdown_event.set()
9. Kill orphaned children (port sweep + multiprocessing sweep)
10. _webview_window.destroy()        ← closes PyWebView, app exits
```

On next manual launch:

```
11. auto_resume_after_restart() checks for panic_stop.flag
12. Flag found → skip auto-resume, delete flag
13. Agent waits for owner interaction (no automatic work)
```

### 9.3 Subprocess Process Group Management

All subprocesses spawned by agent tools (`run_shell`, `claude_code_edit`)
use `start_new_session=True` (via `_tracked_subprocess_run()` in
`ouroboros/tools/shell.py`). This creates a separate process group for each
subprocess and all its children.

On panic or timeout, the entire process tree is killed via
`os.killpg(pgid, SIGKILL)` — no orphans possible, even for deeply nested
subprocess trees (e.g., Claude CLI spawning node processes).

Active subprocesses are tracked in a thread-safe global set and cleaned up
automatically on completion or via `kill_all_tracked_subprocesses()` on panic.

---

## 10. Key Invariants

1. **Never delete BIBLE.md or identity.md** (hardcoded + LLM safety)
2. **VERSION == pyproject.toml version == latest git tag == README version**
3. **Config SSOT**: all settings defaults and paths live in `ouroboros/config.py`
4. **Message bus SSOT**: all messaging goes through `supervisor/message_bus.py`
5. **State locking**: `state.json` uses file locks for concurrent read-modify-write
6. **Budget tracking**: per-LLM-call cost events with model/key/category breakdown
7. **Core file sync**: safety-critical files are overwritten from bundle on every launch
8. **Zero orphans on close**: shutdown MUST kill all child processes (see Section 9)
9. **Panic MUST kill everything**: all processes (workers, subprocesses, subprocess
   trees, consciousness, evolution) are killed and the application exits completely.
   No agent code may prevent or delay panic. See BIBLE.md Emergency Stop Invariant.

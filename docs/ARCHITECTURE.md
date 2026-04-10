# Ouroboros v4.18.3 — Architecture & Reference

This document describes every component, page, button, API endpoint, and data flow.
It is the single source of truth for how the system works. Keep it updated.

---

## 1. High-Level Architecture

```
User
  │
  ▼
launcher.py (PyWebView)       ← desktop window, immutable (bundle-only, not in git)
  │
  │  spawns subprocess
  ▼
server.py (Starlette+uvicorn) ← HTTP + WebSocket on localhost:8765
  │
  ├── web/                     ← Web UI (SPA with ES modules in web/modules/)
  │
  ├── supervisor/              ← Background thread inside server.py
  │   ├── message_bus.py       ← Queue-based message bus + Telegram bridge (LocalChatBridge)
  │   ├── workers.py           ← Multiprocessing worker pool (fork/spawn by platform)
  │   ├── state.py             ← Persistent state (state.json) with file locking
  │   ├── queue.py             ← Task queue management (PENDING/RUNNING lists)
  │   ├── events.py            ← Event dispatcher (worker→supervisor events)
  │   └── git_ops.py           ← Git operations (clone, checkout, rescue, rollback, push, credential helper)
  │
  └── ouroboros/               ← Agent core (runs inside worker processes)
      ├── config.py            ← SSOT: paths, settings defaults, load/save, PID lock
      ├── agent.py             ← Task orchestrator
      ├── chat_upload_api.py   ← Chat file attachment upload/delete endpoints
      ├── agent_startup_checks.py ← Startup verification and health checks
      ├── agent_task_pipeline.py  ← Task execution pipeline orchestration
      ├── loop.py              ← High-level LLM tool loop
      ├── loop_llm_call.py     ← Single-round LLM call + usage accounting
      ├── loop_tool_execution.py ← Tool dispatch and tool-result handling
      ├── pricing.py           ← Model pricing, cost estimation, usage events
      ├── llm.py               ← Multi-provider LLM routing (OpenRouter/OpenAI/compatible/Cloud.ru/Anthropic)
      ├── model_catalog_api.py ← Optional provider model catalog endpoint
      ├── safety.py            ← Dual-layer LLM security supervisor
      ├── consciousness.py     ← Background thinking loop (with progress emission)
      ├── consolidator.py      ← Block-wise dialogue consolidation (dialogue_blocks.json)
      ├── memory.py            ← Scratchpad, identity, chat history
      ├── context.py           ← LLM context builder (public API for consciousness)
      ├── context_compaction.py ← Context trimming and summarization helpers
      ├── local_model.py       ← Local LLM lifecycle (llama-cpp-python)
      ├── local_model_api.py   ← Local model HTTP endpoints
      ├── local_model_autostart.py ← Local model startup helper
      ├── deep_self_review.py   ← Deep self-review: full git-tracked pack + memory → 1M-context model
      ├── review.py            ← Code collection, complexity metrics, pre-commit review
      ├── review_state.py      ← Durable advisory pre-review state (advisory_review.json)
      ├── onboarding_wizard.py ← Shared desktop/web onboarding bootstrap + validation
      ├── owner_inject.py      ← Per-task user message mailbox (compat module name)
      ├── launcher_bootstrap.py ← Bundle-to-repo bootstrap and managed sync helpers (used by launcher.py)
      ├── provider_models.py   ← Provider-specific model ID helpers, direct-provider defaults (OpenAI, Anthropic)
      ├── reflection.py        ← Execution reflection and pattern capture
      ├── review_evidence.py   ← Structured review findings/obligations snapshot for summaries and reflections
      ├── server_auth.py       ← Non-localhost auth gate (OUROBOROS_NETWORK_PASSWORD)
      ├── server_control.py    ← Process-control helpers: restart, panic stop
      ├── server_entrypoint.py ← CLI argument parsing, port-binding helpers
      ├── server_history_api.py ← Chat history + cost breakdown endpoints
      ├── server_runtime.py    ← Server startup/onboarding and WebSocket liveness helpers
      ├── server_web.py        ← Static web file helpers (NoCacheStaticFiles, web dir resolver)
      ├── task_continuation.py ← Durable per-task review continuation state across restart/outage
      ├── task_results.py      ← Durable task result/status files (task_results/<id>.json)
      ├── tool_capabilities.py ← SSOT for tool sets (core, parallel-safe, truncation, browser)
      ├── tool_policy.py       ← Tool access policy and gating (imports from tool_capabilities)
      ├── utils.py             ← Shared utilities
      ├── world_profiler.py    ← System profile generator (WORLD.md)
      ├── gateways/            ← External API adapters (thin transport, no business logic)
      │   └── claude_code.py   ← Claude Agent SDK gateway (edit + read-only paths)
      ├── tools/               ← Auto-discovered tool plugins
      │   ├── ci.py              ← CI trigger and monitoring (GitHub Actions API)
      │   ├── claude_advisory_review.py ← Advisory pre-review tool (read-only Claude Agent SDK)
      │   ├── commit_gate.py     ← Advisory freshness gate and commit-attempt recording (extracted from git.py)
      │   ├── git_rollback.py    ← rollback_to_target tool (wraps git_ops.rollback_to_version)
      │   ├── parallel_review.py ← Parallel triad+scope orchestration and verdict aggregation (extracted from git.py)
      │   ├── plan_review.py     ← Pre-implementation design review (3 parallel full-codebase reviewers, plan_task tool)
      │   ├── review.py          ← Triad diff review (3-model parallel review against CHECKLISTS.md)
      │   ├── review_helpers.py  ← Shared review helpers (section loader, file packs, intent)
      │   └── scope_review.py   ← Blocking scope reviewer (opus, fail-closed)
      └── platform_layer.py    ← Cross-platform process/path/locking helpers

# Build & CI (not part of runtime)
.github/workflows/ci.yml     ← Three-tier CI (quick/full/release)
build.sh                      ← macOS build (PyInstaller → .dmg)
build_linux.sh                ← Linux build (PyInstaller → .tar.gz)
build_windows.ps1             ← Windows build (PyInstaller → .zip)
Dockerfile                    ← Docker image (web UI runtime)
```

### Two-process model

1. **launcher.py** — immutable outer shell (lives inside the `.app` bundle, not in the git repo). Never self-modifies. Handles:
   - PID lock (single instance)
   - Bootstrap: copies workspace to `~/Ouroboros/repo/` on first run
   - Core file sync: overwrites safety-critical files on every launch
   - Starts `server.py` as a subprocess via embedded Python
   - Shows PyWebView window pointed at `http://127.0.0.1:8765`
   - Monitors subprocess; restarts on exit code 42 (restart signal)
  - First-run wizard (shared desktop/web onboarding for multi-key and optional local setup)
   - **Graceful shutdown with orphan cleanup** (see Shutdown section below)

2. **server.py** — self-editable inner server. Can be modified by the agent.
   - Starlette app with HTTP API + WebSocket
   - Runs supervisor in a background thread
   - Supervisor manages worker pool, task queue, message routing
   - Local model lifecycle endpoints extracted to `ouroboros/local_model_api.py`

### Data layout (`~/Ouroboros/`)

```
~/Ouroboros/
├── repo/              ← Agent's self-modifying git repository
│   ├── server.py      ← The running server (copied from workspace)
│   ├── ouroboros/      ← Agent core package
│   │   └── local_model_api.py  ← Local model API endpoints (extracted from server.py)
│   ├── supervisor/     ← Supervisor package
│   ├── web/            ← Web UI files
│   │   └── modules/    ← ES module pages (chat, logs, evolution, etc.)
│   ├── docs/           ← Project documentation
│   │   ├── ARCHITECTURE.md ← This document
│   │   ├── DEVELOPMENT.md  ← Engineering handbook (naming, entity types, review protocol)
│   │   └── CHECKLISTS.md   ← Pre-commit review checklists (single source of truth)
│   └── prompts/        ← System prompts (SYSTEM.md, SAFETY.md, CONSCIOUSNESS.md)
├── data/
│   ├── settings.json   ← User settings (API keys, models, budget)
│   ├── state/
│   │   ├── state.json  ← Runtime state (spent_usd, session_id, branch, etc.)
│   │   ├── advisory_review.json ← Durable advisory/review ledger (runs, attempts, obligations)
│   │   ├── queue_snapshot.json
│   │   └── review_continuations/ ← Per-task blocked-review continuation payloads (+ quarantined corrupt files under `corrupt/`)
│   ├── memory/
│   │   ├── identity.md     ← Agent's self-description (persistent)
│   │   ├── scratchpad.md   ← Working memory (auto-generated from scratchpad_blocks.json)
│   │   ├── scratchpad_blocks.json ← Append-block scratchpad (FIFO, max 10)
│   │   ├── dialogue_blocks.json ← Block-wise consolidated chat history
│   │   ├── dialogue_summary.md ← Legacy dialogue summary (auto-migrated to blocks)
│   │   ├── dialogue_meta.json  ← Consolidation metadata (offsets, counts)
│   │   ├── WORLD.md        ← System profile (generated on first run)
│   │   ├── knowledge/      ← Structured knowledge base files
│   │   ├── identity_journal.jsonl    ← Identity update journal
│   │   ├── scratchpad_journal.jsonl  ← Scratchpad block eviction journal
│   │   ├── knowledge_journal.jsonl   ← Knowledge write journal
│   │   ├── deep_review.md            ← Last deep self-review report (written by deep_self_review task)
│   │   ├── registry.md              ← Source-of-truth awareness map (what data the agent has vs doesn't have)
│   │   └── owner_mailbox/           ← Per-task user message files (compat path name)
│   ├── logs/
│   │   ├── chat.jsonl      ← Chat message log
│   │   ├── progress.jsonl  ← Progress/thinking messages (BG consciousness, tasks)
│   │   ├── events.jsonl    ← LLM rounds, task lifecycle, errors
│   │   ├── tools.jsonl     ← Tool call log with args/results
│   │   ├── supervisor.jsonl ← Supervisor-level events
│   │   └── task_reflections.jsonl ← Execution reflections (process memory)
│   ├── archive/            ← Rotated logs, rescue snapshots
│   └── uploads/            ← Chat file attachments (uploaded via paperclip button)
└── ouroboros.pid           ← PID lock file (platform lock — auto-released on crash)
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
  ├── _run_first_run_wizard()   → Show shared setup wizard if no runnable config
  │                               (access entry → models → review mode → budget → summary)
  │                               Saves to ~/Ouroboros/data/settings.json
  ├── agent_lifecycle_loop()    → Background thread: start/monitor server.py
  └── webview.start()           → Open PyWebView window at http://127.0.0.1:8765
```

### First-run wizard

Shown when `settings.json` does not contain any supported remote provider key and has no
`LOCAL_MODEL_SOURCE`.

- Existing OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, Anthropic, or local-model-source settings skip the wizard automatically.
- The wizard is shared between desktop and web: one HTML/CSS/JS onboarding flow is rendered directly in pywebview for desktop and injected into a blocking web overlay for Docker/browser runs.
- The wizard is multi-step and provider-aware: it starts with a single access step that accepts multiple remote keys plus optional local-model setup, then shows visible model defaults, a dedicated review-mode step, a dedicated budget step, and the final summary before save.
- When an Anthropic key is present, onboarding shows the Claude runtime status with `Repair Runtime` and `Skip for now` options.
- Desktop first-run uses the same onboarding bundle and talks to Claude SDK install/status through `pywebview` bridge methods.
  Web onboarding uses `/api/claude-code/status` and `/api/claude-code/install`.
- The wizard blocks progression if nothing runnable is configured.
- When OpenRouter is absent and official OpenAI is the only configured remote runtime, untouched default model values are auto-remapped to `openai::gpt-5.4` / `openai::gpt-5.4-mini` so first-run startup does not strand the app on OpenRouter-only defaults.
- `web_search` uses the official OpenAI Responses API only. It requires `OPENAI_API_KEY` and treats any non-empty `OPENAI_BASE_URL` as an incompatible custom runtime configuration rather than a fallback.
- OpenAI-compatible and Cloud.ru remain explicit model-selection flows from the full Settings page because there is no single safe universal default model ID for those providers.
- Closing the wizard without saving is non-fatal: the main app still launches and the user can finish configuration in Settings.

### Core file sync (`_sync_core_files`)

On every launch (not just first run), these files are copied from the workspace
bundle to `~/Ouroboros/repo/`, ensuring safety-critical code cannot be permanently
corrupted by agent self-modification:

- `prompts/SAFETY.md`
- `ouroboros/safety.py`
- `ouroboros/tools/registry.py`

---

## 3. Web UI Pages & Buttons

The web UI is a single-page app (`web/index.html` + `web/style.css` + ES modules).
`web/app.js` is the thin orchestrator (~90 lines) that imports from `web/modules/`:
- `ws.js` — WebSocket connection manager
- `utils.js` — shared utilities (markdown rendering, escapeHtml, matrix rain)
- `chat.js` — chat page with message rendering, live task card, compact runtime controls, and budget pill
- `logs.js` — log viewer with category filters and grouped task cards
- `log_events.js` — shared event summarization/grouping helpers used by Chat and Logs
- `files.js` — file browser, preview, uploads, and editor
- `evolution.js` — evolution chart (Chart.js) + versions sub-tab (git commits, tags, rollback, promote)
- `settings.js` — settings form with local model management
- `settings_controls.js` — searchable model pickers + segmented effort controls
- `costs.js` — cost breakdown tables
- `about.js` — about page

Navigation is a left sidebar with 7 pages (Chat, Files, Logs, Costs, Evolution, Settings, About).

### 3.1 Chat

- **Status badge** (top-right): "Online" (green) / "Thinking..." / "Working..." (amber pulse) / "Reconnecting..." (red).
  Driven by WebSocket connection state, typing events, and live task state.
- **Header controls**: compact buttons for `/evolve`, `/bg`, `/review`, `/restart`, `/panic` — the canonical location for runtime controls. The chat header is a floating transparent overlay (`position: absolute`, gradient fade) so messages scroll beneath it.
- **Budget pill**: compact amber pill in the header showing `$spent / $limit` with a mini progress bar, updated from `/api/state` polling every 3 seconds.
- **Message input**: absolute-positioned frosted-glass overlay anchored to the bottom of the chat page (`position: absolute; bottom: 0`). Contains a paperclip attachment button (positioned as an absolute overlay inside the textarea on the left; opens a file picker; selected file is staged locally in JS memory and shown as a removable filename preview badge — the file is uploaded to `data/uploads/` only when Send/Enter is triggered; if the WebSocket is offline at send time, upload is blocked with an error (no upload happens when disconnected, preventing orphan files). If the WebSocket drops after upload completes but before message delivery, the queued message references a durable server-side file that persists until explicitly deleted), a textarea (grows up to 120px; `padding-left: 42px` and `padding-right: 52px` leave space for both overlay buttons), and a Send text button (positioned as an absolute overlay inside the textarea on the right, vertically centered; minimal style — no border, transparent background normally, subtle crimson tint on hover/active, accent-color text). The `#chat-input` textarea has `backdrop-filter: blur(16px)` + semi-transparent background (frosted glass). The `#chat-input-area` wrapper uses only a gradient fade overlay (no backdrop-filter) so message bubbles scroll underneath without a sharp-edged blur rectangle. `#chat-messages` `padding-bottom` is set dynamically via `updateMessagesPadding()` in chat.js (real overlay height + 16px buffer), called on page connect, textarea resize, and attachment preview toggle. The CSS default is `84px` (covers min-height state); JS adjusts up to ~160px when the textarea is fully expanded. Shift+Enter for newline, Enter to send.
- **Input recall**: ArrowUp / ArrowDown cycles through recent submitted messages without leaving the textarea.
- **Messages**: user bubbles (right, steel-blue-tinted), assistant bubbles (left, crimson), and system-summary bubbles (left, amber). Non-user bubbles render markdown. Live task card uses crimson accent glass matching the assistant palette.
- **Multi-user visibility**: user messages are now session-aware. The current browser session stays labeled as `You`; other Web UI sessions render as `WebUI (<session>)`; Telegram-origin messages render with their Telegram sender label.
- **Timestamps**: smart relative formatting (today: "HH:MM", yesterday: "Yesterday, HH:MM", older: "Mon DD, HH:MM"). Shown on hover.
- **Live task card**: reasoning/progress/tool chatter no longer spams the transcript as many assistant bubbles.
  Chat listens to `log_event`/task events plus progress messages and collapses them into one expandable task card with a timeline of steps.
- **Recoverable step failures**: step-level shell/tool failures stay as timeline notes and no longer freeze the card in a terminal `Issue` state.
  Later progress updates can retake the headline until the task actually finishes.
- **System summaries**: `direction="system"` entries from `chat.jsonl` are shown in the same timeline with a 📋 label instead of being hidden or treated as user text.
- **Typing indicator**: animated "thinking dots" bubble appears when the agent is processing.
- **Persistence**: chat history loaded from server on page load (`/api/chat/history`), survives app restarts. Fallback to sessionStorage. `syncHistory` uses two-pass processing: progress/summary messages are replayed first (building live card timelines), then regular assistant/user messages are processed (calling `finishLiveCard`). This guarantees thinking bubbles are never discarded due to `taskState.completed` being set before progress events are applied. After first load, if any live card is still active (task ongoing mid-reload), `showTyping()` is called to restore the typing indicator.
- **Duplicate-bubble prevention**: queued local user bubbles carry a `client_message_id`; echoed WebSocket/history messages with the same id are merged instead of duplicated.
- **Empty-chat init**: if neither server history nor sessionStorage has messages, the UI shows a transient assistant bubble: `Ouroboros has awakened`. This is visual-only and is not written to chat history.
- **Telegram bridge**: Web UI initiated chats can be mirrored into the bound Telegram chat, Telegram text input is injected back into the same live chat timeline, and Telegram photos are bridged as image-aware user messages (including while a direct-chat turn is already running).
- Messages sent via WebSocket `{type: "chat", content: text, sender_session_id: "uuid"}`.
- Responses arrive via WebSocket `{type: "chat", role, content, ts, source?, sender_label?, sender_session_id?, client_message_id?}` and `{type: "photo", role, image_base64, mime, caption?, ts, source?, sender_label?}`. On page-load history sync, `/api/chat/history` can also return `role: "system"` entries for internal summaries plus metadata for multi-user reconstruction.
- Supports slash commands: `/status`, `/evolve`, `/review`, `/bg`, `/restart`, `/panic`.

### 3.2 Files

- **Browser pane**: directory tree for the configured root, breadcrumb navigation, inline filter, refresh button,
  create-file/create-directory actions, clipboard-style copy/move/paste, and delete/download context menu.
- **Preview pane**: text preview/editor, image preview, binary-file placeholder, and drag-drop upload target.
- **Write safety**: unsaved text edits are guarded on folder switches, file switches, page navigation, and browser refresh.
- **Root policy**: localhost requests fall back to the current user's home directory when no root is configured.
  Network/Docker access requires an explicit `OUROBOROS_FILE_BROWSER_DEFAULT` directory.
- **Network policy**: `OUROBOROS_NETWORK_PASSWORD` is optional. When configured, non-loopback browser/API access is gated.
  When omitted, the full HTTP/WebSocket surface remains reachable by design. `/api/health` always stays public.
- **Symlink policy**: entries are constrained lexically to the configured root, but symlink targets may resolve outside that root intentionally.
  External symlink paths support list/read/download/content/write/mkdir/upload/copy/move/delete. Root-delete protection still applies only to the configured root itself.
- **Transfer semantics**: copy/move of symlink entries preserves the link object; writing through a symlink-backed file edits the target content.
- **Bounds**: directory listings are capped, previews are bounded to a text/byte limit, and uploads reject oversized payloads.

### 3.3 Dashboard (removed in v4.10.0)

The Dashboard tab has been removed. Its functionality is now distributed:
- **Budget**: shown as a compact pill in the Chat header (polls `/api/state` every 3s).
- **Evolve/BG toggles**: Chat header buttons (glow when active).
- **Review/Restart/Panic**: Chat header buttons.
- **Runtime status (evolution/consciousness detail)**: Evolution page runtime card.

### 3.4 Settings

- **Tabbed layout**: `Providers`, `Models`, `Integrations`, `Advanced`.
- **Provider cards**: OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, Anthropic, plus optional Network Password. Cards are collapsible and use masked-secret inputs with show/hide toggles.
- **API Keys**: OpenRouter, OpenAI, OpenAI-compatible, Cloud.ru, Anthropic, Telegram Bot Token, GitHub Token, and Network Password.
  Keys are displayed as masked values (e.g., `sk-or-v1...`), can be explicitly cleared, and are only overwritten on save if the user enters a new value (not containing `...`).
- **Claude Runtime Status**: when Anthropic is configured, the Anthropic card shows app-managed Claude runtime status with `Repair Runtime` action.
  The Claude runtime (SDK + bundled CLI) powers delegated code editing and advisory review and is managed automatically by the app.
- **Models**: Main, Code, Light, Fallback.
- **Model catalog**: optional `Refresh Model Catalog` action calls `/api/model-catalog`. Failures are non-fatal and surfaced as inline warnings.
- **Model pickers**: searchable provider-aware pickers replace legacy raw dropdowns for remote models.
- **Provider prefixes**:
  - OpenRouter model values stay unprefixed (`anthropic/claude-opus-4.6`).
  - OpenAI model values use `openai::...`.
  - OpenAI-compatible model values use `openai-compatible::...`.
  - Cloud.ru model values use `cloudru::...`.
  - Anthropic model values use `anthropic::...` (e.g. `anthropic::claude-opus-4.6`).
- **Reasoning Effort**: Five segmented controls for task/chat, evolution, review, scope review, and consciousness.
  Backed by `OUROBOROS_EFFORT_TASK`, `OUROBOROS_EFFORT_EVOLUTION`, `OUROBOROS_EFFORT_REVIEW`,
  `OUROBOROS_EFFORT_SCOPE_REVIEW`, `OUROBOROS_EFFORT_CONSCIOUSNESS`. Loading falls back to legacy
  `OUROBOROS_INITIAL_REASONING_EFFORT` for task/chat when the new key is absent.
- **Review Models**: Comma-separated remote model IDs for pre-commit review.
  Backed by `OUROBOROS_REVIEW_MODELS`.
- **Scope Review Model**: Single model for the blocking scope reviewer.
  Backed by `OUROBOROS_SCOPE_REVIEW_MODEL` (default `anthropic/claude-opus-4.6`).
- **OpenAI-only review fallback**: if official OpenAI is the only configured remote runtime and the review list is invalid/underspecified, review falls back to the main model repeated three times.
- **Review Enforcement**: `Advisory` or `Blocking` for pre-commit review behavior.
  Backed by `OUROBOROS_REVIEW_ENFORCEMENT`. Review always runs in both modes.
- **Advanced**: local model runtime, max workers, total budget, per-task soft threshold, tool timeout, soft/hard timeout, web search model, review enforcement, legacy compatibility, and reset controls.
- **Local Model Runtime**: source, GGUF filename, port, GPU layers, context length, chat format, start/stop/test buttons, live local-model status.
- **Telegram**: Bot Token and primary chat id. If no primary chat id is pinned, the bridge binds to the first active Telegram chat and keeps replies attached there.
- **GitHub**: Token + Repo (for remote sync).
- **Save Settings** button → POST `/api/settings`. Applies to env immediately.
  Budget and tool-timeout changes take effect immediately; provider/runtime changes may still require restart.
- **Reset All Data** button (Danger Zone) → POST `/api/reset`.
  Deletes: state/, memory/, logs/, archive/, uploads/, settings.json.
  Keeps: repo/ (agent code).
  Triggers server restart. On next launch, onboarding wizard appears.

### 3.5 Logs

- **Filter chips**: Tools, LLM, Errors, Tasks, System, Consciousness.
  Toggle on/off to filter log entries.
- **Clear** button: clears the in-memory log view (not files on disk).
- Log entries arrive via WebSocket `{type: "log", data: event}`.
- The page renders a live timeline: standalone system/error entries stay as rows, while task/LLM/tool/progress events with a shared `task_id` collapse into grouped task cards with an expandable internal timeline.
- Chat and Logs share the same event summarization logic from `log_events.js`, so a task phase is described the same way in both places.
- Each standalone row or grouped task card has a **Raw** toggle that expands the latest original JSON payload.
- New live-only timeline events cover task start, context building, LLM round start/finish,
  tool start/finish/timeout, and compact task heartbeats during long waits.
- Repeated startup/system events such as verification bursts are compacted in the UI.
- Max 500 entries in view (oldest removed).

### 3.6 Versions (merged into Evolution in v4.10.0)

The standalone Versions tab has been merged into the Evolution page as a sub-tab.
See section 3.8 (Evolution) for the combined page.

### 3.7 Costs

- **Total Spent / Total Calls / Top Model** stat cards at top.
- **Breakdown tables**: By Model, By API Key, By Model Category, By Task Category.
  Each row shows name, call count, cost, and a proportional bar.
- **Refresh** button reloads data from `/api/cost-breakdown`.
- Data auto-loads when the page becomes active (MutationObserver on class).

### 3.8 Evolution

Two sub-tabs ("Chart" and "Versions"), switchable via pill buttons in the page header.

**Chart sub-tab (default):**
- **Runtime status card**: evolution mode / consciousness pills, cycle count, queue, budget remaining, last evolution timestamp, next wakeup.
- **Chart**: interactive Chart.js line graph showing code LOC, prompt sizes (BIBLE, SYSTEM),
  identity, scratchpad, and total memory growth across all git tags.
- **Dual Y-axes**: left axis for Lines of Code, right axis for Size (KB).
- **Tags table**: detailed breakdown per tag with all metrics.
- Data fetched from `/api/evolution-data` (cached 60s server-side).
- Chart.js bundled locally (`web/chart.umd.min.js`) — no CDN dependency.

**Versions sub-tab:**
- **Current branch + SHA** displayed at top.
- **Recent Commits** list with SHA, date, message, and "Restore" button.
- **Tags** list with tag name, date, message, and "Restore" button.
- **Restore** button → POST `/api/git/rollback` with target SHA/tag.
  Creates rescue snapshot, resets to target, restarts server.
- **Promote to Stable** button → POST `/api/git/promote`.
  Updates `ouroboros-stable` branch to match `ouroboros`.
- Data loaded on first visit to the Versions sub-tab.
- **Refresh** button reloads data for the active sub-tab.

### 3.9 About

- Logo (large, centered — the only location for the logo image; sidebar shows only a compact version label above the About button)
- "A self-creating AI agent" description
- Created by Anton Razzhigaev & Andrew Kaznacheev
- Links: @abstractDL (Telegram), GitHub repo
- "Joi Lab" footer

---

## 4. Server API Endpoints

If `OUROBOROS_NETWORK_PASSWORD` is configured, non-loopback HTTP/WebSocket access requires
authentication. If the password is blank, non-loopback access stays open by design.
`/api/health`, `/auth/login`, and `/auth/logout` remain reachable without an existing session.

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Serves `web/index.html` |
| GET | `/api/health` | `{status, version, runtime_version, app_version}` |
| GET | `/api/state` | Dashboard data: uptime, workers, budget, branch, etc. |
| GET | `/api/files/list` | Directory listing for Files tab root/path |
| GET | `/api/files/read` | File preview payload (text/image metadata/binary placeholder) |
| GET | `/api/files/content` | Raw file content response for image preview |
| GET | `/api/files/download` | Attachment download for a file |
| POST | `/api/files/write` | Create or overwrite a text file from Files editor |
| POST | `/api/files/mkdir` | Create a directory inside current Files path |
| POST | `/api/files/delete` | Delete a file/directory (root delete is rejected) |
| POST | `/api/files/transfer` | Copy or move files/directories within the Files root |
| POST | `/api/files/upload` | Multipart upload into current Files directory |
| GET | `/api/settings` | Current settings with masked API keys |
| POST | `/api/settings` | Update settings (partial update, only provided keys) |
| GET | `/api/claude-code/status` | App-managed Claude runtime status (SDK version, CLI path/version, legacy detection, API key readiness) |
| POST | `/api/claude-code/install` | Repair/update the app-managed Claude runtime |
| GET | `/api/model-catalog` | Optional provider model catalog (OpenRouter/OpenAI/compatible/Cloud.ru) |
| POST | `/api/command` | Send a slash command `{cmd: "/status"}` |
| POST | `/api/reset` | Delete all runtime data, restart for fresh onboarding |
| GET | `/api/git/log` | Recent commits + tags + current branch/sha |
| POST | `/api/git/rollback` | Rollback to a specific commit/tag `{target: "sha"}` |
| POST | `/api/git/promote` | Promote ouroboros → ouroboros-stable |
| GET | `/api/cost-breakdown` | Cost dashboard aggregation by model/key/category |
| POST | `/api/local-model/start` | Start/download local model server |
| POST | `/api/local-model/stop` | Stop local model server |
| GET | `/api/local-model/status` | Local model status and readiness |
| GET | `/api/evolution-data` | Evolution metrics per git tag (LOC, prompt sizes, memory) |
| GET | `/api/chat/history` | Merged chat + system summaries + progress messages (chronological, limit param) |
| POST | `/api/chat/upload` | Upload a file attachment; saved to `data/uploads/` with UUID-prefixed unique name; returns `{ok, filename, display_name, path, size, mime}` |
| DELETE | `/api/chat/upload` | Delete a previously uploaded chat attachment by filename |
| POST | `/api/local-model/test` | Local model sanity test (chat + tool calling) |
| GET/POST | `/auth/login` | Password gate entrypoint for non-localhost browser/API access |
| GET/POST | `/auth/logout` | Clear auth cookie/session |
| WS | `/ws` | WebSocket: chat messages, commands, log streaming |
| GET | `/static/*` | Static files from `web/` directory (NoCacheStaticFiles wrapper forces revalidation) |

### WebSocket protocol

**Client → Server:**
- `{type: "chat", content: "text", sender_session_id: "uuid", client_message_id?: "msg-..."}` — send chat message
- `{type: "command", cmd: "/status"}` — send slash command

**Server → Client:**
- `{type: "chat", role, content, ts, source?, sender_label?, sender_session_id?, client_message_id?, telegram_chat_id?}` — user/assistant/system chat payloads
- `{type: "log", data: {type, ts, ...}}` — real-time log event
- `{type: "typing", action: "typing"}` — typing indicator (show animation)
- `{type: "photo", image_base64, mime, caption, ts}` — assistant image/photo payload

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
| `/review` | Queue a deep self-review (1M-context single-pass Constitution review) |
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
   a. Build context (`context.py`): system prompt + bible + identity + scratchpad + runtime info + Memory Registry digest
   b. `run_llm_loop()`: LLM call → tool execution → repeat until final text response
   c. Emit final `send_message`, `task_metrics`, and `task_done`; any restart request is latched until after those final events are queued
   d. Store task result synchronously; task summary and reflection run off the user-reply critical path
4. Events flow back to supervisor via event queue

### Tool capability sets (tool_capabilities.py)

Single source of truth for all tool classification sets:
- **`CORE_TOOL_NAMES`** — tools available from round 1 (no `enable_tools` needed)
- **`META_TOOL_NAMES`** — discovery tools (`list_available_tools`, `enable_tools`)
- **`READ_ONLY_PARALLEL_TOOLS`** — safe for concurrent execution in ThreadPoolExecutor
- **`STATEFUL_BROWSER_TOOLS`** — require thread-sticky executor (Playwright affinity)
- **`REVIEWED_MUTATIVE_TOOLS`** — tools (`repo_commit`, `repo_write_commit`) that must NOT
  end with ambiguous timeouts; executor waits synchronously for the final result
- **`UNTRUNCATED_TOOL_RESULTS`** — tools whose output must never be truncated
- **`UNTRUNCATED_REPO_READ_PATHS`** — repo files that must stay whole when read
- **`TOOL_RESULT_LIMITS`** — per-tool output size caps (chars)

`tool_policy.py` and `loop_tool_execution.py` import from this module. The legacy
copy in `tools/registry.py` (safety-critical, overwritten on restart) is kept for
backward compatibility but is not the runtime authority.

### Tool execution (loop.py)

- Pricing/cost estimation logic extracted to `pricing.py` (model pricing table, cost estimation, API key inference, usage event emission)
- **Per-task soft threshold**: Each task has a soft threshold (default $20, env `OUROBOROS_PER_TASK_COST_USD`). When a task exceeds this, the LLM is asked to wrap up soon. This is a reminder, not a hard stop.
- **`memory_tools.py`**: Provides `memory_map` (read the metacognitive registry of all data sources) and `memory_update_registry` (add/update entries). Part of the Memory Registry system (v3.16.0).
- **`tool_discovery.py`**: Provides `list_available_tools` (discover non-core tools) and `enable_tools` (activate extra tools for the current task). Enables dynamic tool set management.
- **`code_search`**: First-class code search tool in `tools/core.py`. Literal search by default, regex optional. Skips binaries, caches, vendor dirs. Bounded output (max 200 results, 80K chars). Available from round 1 as a core tool. Replaces the pattern of using `run_shell` with `grep`/`rg` for code search.
- Core tools always available; extra tools discoverable via `list_available_tools`/`enable_tools`
- Read-only tools can run in parallel (ThreadPoolExecutor)
- Browser tools use thread-sticky executor (Playwright greenlet affinity)
- All tools have hard timeout (default 600s, per-tool overrides for browser/search/vision); `OUROBOROS_TOOL_TIMEOUT_SEC` in `settings.json` is the runtime SSOT override read on each tool call.
- Multi-layer safety: hardcoded sandbox (registry.py) → deterministic whitelist → LLM safety supervisor
- Tool results use explicit per-tool caps with visible truncation markers (`repo_read`/`data_read`/`knowledge_read`/`run_shell`: 80k, default: 15k chars). Cognitive reads (`memory/*`, prompts, BIBLE/docs, commit/review outputs) are exempt from silent clipping.
- `run_shell` now treats non-zero exits as explicit failed tool outcomes and records exit/signal metadata in the tool trace.
- `run_shell` recovers `cmd` passed as a string via a three-step cascade: `json.loads` (JSON array strings) → `ast.literal_eval` (Python literal lists) → `shlex.split` (plain shell strings). Only truly unrecoverable input returns `SHELL_ARG_ERROR`. The `cmd` parameter schema remains `type: array` (intended contract), but the runtime gracefully handles LLM misformatting.
- `set_tool_timeout` persists `OUROBOROS_TOOL_TIMEOUT_SEC` to `settings.json` and hot-applies it without restart.
- `/api/claude-code/status` returns app-managed Claude runtime status (SDK version, CLI path/version, app-managed flag, legacy detection, API key readiness, last stderr on failure); `/api/claude-code/install` repairs/updates the app-managed runtime.
- Desktop onboarding shows Claude runtime status and repair action (the runtime is managed automatically by the app).
- **`seal_task_transcript`**: called after compaction and before each `call_llm_with_retry`. Marks one stable tool-result boundary with `cache_control: ephemeral` to improve Anthropic prompt cache hits. Reverts all previous seals first so compaction always sees plain strings. Provider handling: OpenRouter (Anthropic models) passes list content blocks through as-is; direct Anthropic path preserves list content for `tool_result` (Anthropic API supports content blocks there); `_strip_cache_control` in `llm.py` now flattens tool-role list content back to a plain string for OpenAI, OpenAI-compatible, Cloud.ru, and local providers.
- **Reviewed mutative tool timeout handling** (v4.9.0): `repo_commit` and `repo_write_commit`
  are classified as `REVIEWED_MUTATIVE_TOOLS`. When they exceed the configured tool timeout,
  the executor emits a `tool_call_late` progress event but continues waiting synchronously
  for the real result (hard ceiling: 1800s). This prevents ambiguous "tool timed out, maybe
  still running" states for commit operations.
- Context compaction kicks in after round 8 (summarizes old tool results)

### Claude runtime (gateways/claude_code.py + platform_layer.py)

- **App-managed runtime**: the app bundle owns a pinned `claude-agent-sdk` + bundled
  Claude CLI baseline. The SDK's own bundled-CLI-first resolution is preserved — the
  gateway never overrides CLI path selection via PATH heuristics.
- **Auth model**: `ANTHROPIC_API_KEY` only. No support for native Claude login.
- **Full stderr capture**: both edit and readonly paths pass a `stderr` callback into
  `ClaudeAgentOptions`. Raw CLI stderr is stored in a ring buffer and surfaced in
  `ClaudeCodeResult.stderr_tail` on failure, eliminating the old "Check stderr output
  for details" blind spot.
- **Runtime resolver** (`ouroboros.platform_layer.resolve_claude_runtime`): deterministic
  snapshot of SDK version, CLI path/version, app-managed vs legacy status, API key
  readiness. Used by the status API, install/repair endpoint, and gateway diagnostics.
- **Legacy detection**: SDK installed outside `python-standalone` (e.g. user-site in
  `~/.local/lib`) is classified as legacy and silently de-prioritized.
- **Two execution modes:**
  - **Edit mode** (`run_edit`): `allowed_tools=["Read","Edit","Grep","Glob"]`,
    `disallowed_tools=["Bash","MultiEdit"]`, `permission_mode="acceptEdits"`,
    PreToolUse hook blocks writes outside `cwd` and to safety-critical files
  - **Read-only mode** (`run_readonly`): uses the simpler `query()` function with
    `allowed_tools=["Read","Grep","Glob"]`,
    `disallowed_tools=["Bash","Edit","Write","MultiEdit"]` (SDK enforces tool
    restrictions at the CLI level; no hooks needed)
- **Structured result**: `ClaudeCodeResult` dataclass with `success`, `result_text`,
  `session_id`, `cost_usd`, `usage`, `error`, `stderr_tail`. Callers populate
  `changed_files`, `diff_stat`, and `validation_summary` (orchestration lives in tool layer)
- **Orchestration in callers**: project context injection (BIBLE.md, DEVELOPMENT.md,
  CHECKLISTS.md, ARCHITECTURE.md), git stat, and post-edit validation live in
  `ouroboros/tools/shell.py` helpers — the gateway stays a pure transport boundary
- **Defense-in-depth**: post-edit revert in `registry.py` remains as secondary safety layer
- Safety-critical files mirror: `BIBLE.md`, `ouroboros/safety.py`,
  `ouroboros/tools/registry.py`, `prompts/SAFETY.md`

### Git tools (tools/git.py + tools/review.py + supervisor/git_ops.py)

- **`repo_write`** (v3.24.0): write file(s) to disk WITHOUT committing. Supports single-file
  (`path` + `content`) and multi-file (`files` array) modes. Preferred workflow:
  `repo_write` all files → `advisory_pre_review` on the final diff → `repo_commit`.
- **`repo_commit`**: stage + advisory freshness gate + unified pre-commit review + commit + tests + auto-tag + auto-push.
  Includes `review_rebuttal` parameter for disputing reviewer feedback.
- **`repo_write_commit`**: legacy single-file write+commit (kept for compatibility).
  Also checks advisory freshness, then runs unified review before commit.
- **Unified pre-commit review** (v3.24.0): triad diff review (3 models against
  `docs/CHECKLISTS.md`) plus a blocking scope review that runs in parallel on the
  same staged snapshot. `Blocking` mode keeps critical findings as hard gates;
  `Advisory` mode surfaces the same findings as warnings and lets the commit
  continue. Review history carried across blocking iterations. Quorum: at least
  2 of 3 triad reviewers must succeed in blocking mode. Deterministic preflight
  (uses `git diff --cached --name-status`;
  renames expand to `D src + A dst`; copies expand to `A dst` only; deleted files excluded
  from companion-file presence checks) catches VERSION/README mismatches; blocks when any
  `.py` file under `ouroboros/` or `supervisor/` is added, modified, deleted, or renamed
  but no `tests/` file is staged; blocks when a new `.py` appears under those dirs but
  `docs/ARCHITECTURE.md` is not staged as a non-deleted file — all before the expensive LLM call.
- **`pull_from_remote`**: fast-forward only pull from origin. Does NOT work when
  local and remote histories are unrelated (e.g. fresh app bundle vs full remote
  history) — use `rollback_to_target` for that case.
- **`rollback_to_target`**: reset current branch to any tag or commit SHA. Creates
  a rescue snapshot of uncommitted work first. Wraps `supervisor/git_ops.rollback_to_version()`.
  Equivalent to the UI "Restore" button in Evolution → Versions. Review-exempt: restores to
  an already-reviewed state. Useful for syncing with remote after `pull_from_remote` fails
  due to unrelated histories: `pull_from_remote` (fetches), then
  `rollback_to_target(target="origin/ouroboros", confirm=true)`.
- **`restore_to_head`**: discard uncommitted changes (review-exempt)
- **`revert_commit`**: create a revert commit for a specific SHA (review-exempt)
- **Auto-tag**: on VERSION change, creates annotated tag `v{VERSION}` after tests pass
- **Auto-push**: best-effort push to origin after successful commit (non-fatal)
- **Credential helper**: `git_ops.configure_remote()` stores credentials in repo-local
  `.git/credentials`. `migrate_remote_credentials()` migrates legacy token-in-URL origins.
  Both are wired at startup and on settings save. Saving `GITHUB_TOKEN` + `GITHUB_REPO`
  in Settings automatically calls `configure_remote()`, so the remote is ready immediately
  after save — no restart required.

### Deterministic preflight checks (`_preflight_check` in `ouroboros/tools/review.py`)

Run before the expensive LLM review on every `repo_commit`. All 8 checks are
non-fatal on exception (LLM reviewers catch anything that slips through).

| # | Check | Triggers | Action |
|---|-------|----------|--------|
| 1 | `version_readme_sync` | VERSION staged but README.md not staged | Block |
| 2 | `version_in_commit` | Commit message has version pattern but VERSION not staged | Block |
| 3 | `tests_affected` | `.py` files in `ouroboros/`/`supervisor/` changed without staged tests | Block |
| 4 | `architecture_doc` | New `.py` in `ouroboros/`/`supervisor/` but `ARCHITECTURE.md` not staged | Block |
| 5 | `version_values_match` | VERSION staged: pyproject.toml, README badge, ARCHITECTURE.md header in staged index must all match VERSION value | Block on mismatch |
| 6 | `readme_changelog_row` | VERSION staged: staged README.md changelog must have a table row for the new version | Block if missing |
| 7 | (reserved) | — | — |
| 8 | `conftest_no_tests` | `conftest.py` staged with `test_*` functions (AST parse of staged content, not regex/worktree read) | Block with move hint |

### Reviewer calibration (`CRITICAL_FINDING_CALIBRATION` in `ouroboros/tools/review_helpers.py`)

A shared calibration text block injected into triad reviewer prompts (`review.py`),
scope reviewer prompt (`scope_review.py`), and advisory reviewer prompt
(`claude_advisory_review.py`). A parallel condensed "Critical threshold rule"
subsection in `docs/CHECKLISTS.md` provides a summary for the checklist's own context.
Enforces: (1) exact repo-local artifact required before CRITICAL; (2) hypothetical/
plugin/env concerns → advisory; (3) one root cause = one FAIL; (4) do not hold
obligation open by abstracting a fixed concrete issue.

### Obligation grouping (P3, `ouroboros/review_state.py::_update_obligations_from_attempt`)

Each critical finding is identified by a stable fingerprint `sha256(f"{item}:{reason}")[:12]`.
Two findings with the same `item` but **different reasons** produce **separate obligations**,
so a reviewer cannot rotate or widen a finding while keeping it formally "open" as a
moving-target obligation.  An identical finding (same item + same reason) repeated across
attempts merges into the existing obligation (reason text deduped) instead of duplicating.

`_resolve_matching_obligations` in `ouroboros/tools/claude_advisory_review.py` resolves
by both `obligation_id` (primary) and `item.lower()` (fallback for legacy saved state),
so the dual-path resolution handles obligations created under either scheme.

### Shared worktree version-sync preflight (`ouroboros/tools/review_helpers.py::check_worktree_version_sync`)

`check_worktree_version_sync(repo_dir)` reads VERSION, pyproject.toml, README badge, and
ARCHITECTURE.md header from the **worktree** (before `git add`) and returns a warning string
on mismatch. The advisory path (`claude_advisory_review.py`) delegates to this shared helper
via a backward-compatible `_check_worktree_version_sync` alias. The staged-index equivalent
in `_preflight_check` (review.py) remains separate — it reads from `git show :PATH` after
staging and is the authoritative gate.

### Self-verification template (P2, `ouroboros/tools/review.py::_build_critical_block_message`)

From attempt ≥ 2, blocked commit messages include a structured self-verification
table requiring the agent to map each open finding to a status, evidence, and note
before calling `repo_commit` again. Suppresses the "blind retry" pattern.

### Safety system (safety.py + registry.py)

Multi-layer security:
1. **Hardcoded sandbox** (registry.py): deterministic blocks on safety-critical file writes, mutative git via shell, GitHub repo/auth commands. Runs BEFORE any LLM check.
2. **Deterministic whitelist** (safety.py): known-safe operations (read-only shell commands, repo writes already guarded by sandbox) skip LLM for speed.
3. **LLM Layer 1 (fast)**: Light model checks remaining tool calls for SAFE/SUSPICIOUS/DANGEROUS.
4. **LLM Layer 2 (deep)**: If flagged, heavy model re-evaluates with "are you sure?" nudge.
5. **Post-execution revert**: After claude_code_edit, modifications to safety-critical files are automatically reverted.
- Safety LLM calls now emit standard `llm_usage` events, so safety costs and failures appear in the same audit/health pipeline as other model calls.
`identity.md` is intentionally mutable (self-creation) and can be rewritten radically;
the constitutional guard is that the file itself must remain non-deletable.

### Background consciousness (consciousness.py)

- Daemon thread, sleeps between wakeups (interval controlled by LLM via `set_next_wakeup`)
- Loads full agent context: BIBLE, identity, scratchpad, knowledge base, drive state,
  health invariants, recent chat/progress/tools/events (same context as main agent)
- Owner messages are forwarded to background consciousness in full text (not first-100-char previews).
- Calls LLM with lightweight introspection prompt
- Has limited tool access (memory, messaging, scheduling, read-only)
- **Progress emission**: emits 💬 progress messages to UI via event queue + persists to `progress.jsonl`
- Pauses when regular task is running; deferred events queued and flushed on resume
- Budget-capped (default 10% of total)
- As of v3.16.1, CONSCIOUSNESS.md includes a concrete 7-item rotating maintenance checklist (dialogue consolidation, identity freshness, scratchpad freshness, knowledge gaps, process-memory freshness, tech radar, registry sync). One item is addressed per wakeup cycle.

### Block-wise dialogue consolidation (consolidator.py)

- Triggered after each task completion (non-blocking, runs in a daemon thread)
- Reads unprocessed entries from `chat.jsonl` in BLOCK_SIZE (100) message chunks
- Calls LLM (Gemini Flash) to create summary blocks stored in `dialogue_blocks.json`
- **Era compression**: when block count exceeds MAX_SUMMARY_BLOCKS (10), oldest blocks
  compressed into single "era summary" (30-40% of original length)
- **Auto-migration**: legacy `dialogue_summary.md` episodes auto-migrated to blocks
  on first consolidation run
- First-person narrative format ("I did...", "Anton asked...", "We decided...")
- Context reads blocks directly from `dialogue_blocks.json` instead of flat markdown

### Scratchpad auto-consolidation (consolidator.py)

- **Block-aware**: operates on `scratchpad_blocks.json` when blocks exist
- Triggered after each task when total block content exceeds 30,000 chars
- LLM extracts durable insights into knowledge base topics, compresses oldest blocks
- Falls back to flat-file mode for pre-migration scratchpads
- Writes knowledge files to `memory/knowledge/`, rebuilds `index-full.md`
- Uses platform-aware file locking to serialize concurrent calls
- Runs in a daemon thread (same pattern as dialogue consolidation)

### Execution reflection (reflection.py)

- Triggered at end of task when tool calls had errors or results contained
  blocking markers (`REVIEW_BLOCKED`, `TESTS_FAILED`, `COMMIT_BLOCKED`, etc.)
- Light LLM produces 150-250 word reflection capturing goal, errors, root cause, lessons
- Reflection prompt now includes structured review evidence (recent reviewed attempts,
  advisory runs, open obligations, live freshness, continuations) so blocked-review lessons
  affect process memory instead of collapsing into a generic failure note.
- Stored in `logs/task_reflections.jsonl`; last 20 entries loaded into dynamic context
- Pattern register: recurring error classes tracked in `memory/knowledge/patterns.md`
  via LLM, loaded into semi-stable context as "Known error patterns"
- Secondary reflection/pattern prompts use explicit truncation markers when compacted for prompt size; no silent clipping of these helper summaries.
- Runs synchronously (not in daemon thread) to avoid data loss on shutdown

### Crash report injection (agent.py)

- On startup, `_verify_system_state()` checks for `state/crash_report.json`
- If present, logs `crash_rollback_detected` event to `events.jsonl`
- File is NOT deleted — persists so `build_health_invariants()` surfaces
  CRITICAL: RECENT CRASH ROLLBACK on every task until the agent investigates

### Subtask lifecycle and trace summaries

- `schedule_task` now writes durable lifecycle states in `task_results/<id>.json`: `requested` → `scheduled` → `running` → terminal status (`completed`, `rejected_duplicate`, `failed`, etc.)
- Duplicate rejects are persisted explicitly, so `wait_for_task()` can report honest status instead of pretending the task is still running.
- Completed subtasks persist the full result text; parent tasks no longer see silently clipped child output.
- When a subtask completes, a compact trace summary is included alongside the full result.
- Task results also persist `review_evidence` so blocked-review history, obligations, and
  advisory state can feed summaries, reflections, and later diagnostics from the same durable record.
- Parent tasks see tool call counts, error counts, and agent notes.
- Trace compaction remains explicit: max 4000 chars with visible omission markers, plus first/last 15 tool calls for long traces.

### Context building (context.py)

- As of v3.16.0, the Memory Registry digest (from `memory/registry.md`) is injected into every LLM context to enable source-of-truth awareness.
- As of v3.20.0, `patterns.md` (Pattern Register) is injected into semi-stable context, and execution reflections from `task_reflections.jsonl` are injected into dynamic context.
- As of v3.22.0, all docs are always in static context: BIBLE.md (180k), ARCHITECTURE.md (60k), DEVELOPMENT.md (30k), README.md (10k), CHECKLISTS.md (5k).
- `Health Invariants` are placed at the start of the dynamic context block, before drive state/runtime/recent sections, so warnings influence planning before the model reads the noisier tail sections.
- Main task context now injects a dedicated `Review Continuity` section between runtime and recent-history sections:
  live repo gate status, stale markers, bypass reasons, open obligations, open review continuations,
  and the recent review ledger.
- `build_recent_sections()` keeps recent dialogue broad, but task-scopes recent progress/tools/events when `task_id` is available.
- `build_health_invariants()` is split into focused helpers and now also surfaces recent provider/routing errors plus local context overflows.
- Local-model path no longer silently slices the live system prompt. It compacts non-core sections explicitly and raises an overflow error if core context still cannot fit.

### Pre-implementation design review (plan_task)

- **Module**: `tools/plan_review.py`. Tool name: `plan_task`.
- **Purpose**: review a proposed implementation plan BEFORE writing any code. Call this
  for non-trivial tasks (>2 files or >50 lines of changes) to surface forgotten touchpoints,
  implicit contract violations, and simpler alternatives before the first edit.
- **Models**: uses the same 3 models from `OUROBOROS_REVIEW_MODELS` (same as commit triad).
  Falls back to repeating `OUROBOROS_MODEL` three times if unconfigured.
- **Inputs**: `plan` (description of what to change), `goal` (problem being solved), and
  optional `files_to_touch` (repo-relative paths whose HEAD content is injected for context).
- **Context per reviewer**: full repo pack (same as scope review — no char cap, binary/sensitive
  filtered via `build_full_repo_pack`) + HEAD snapshots of planned-touch files + "Plan Review
  Checklist" from `docs/CHECKLISTS.md` + BIBLE.md + DEVELOPMENT.md + ARCHITECTURE.md.
- **Checklist items**: completeness, correctness, minimalism, bible_alignment,
  implicit_contracts, testability, architecture_fit, forgotten_docs (8 items total).
- **Output format per reviewer**: verdict (PASS/RISK/FAIL) + detailed explanation + concrete fix
  (exact file/function/symbol) + alternative approaches if applicable.
- **Aggregate signal**: `GREEN` (proceed), `REVIEW_REQUIRED` (risks present), or `REVISE_PLAN` (FAILs found).
- **Non-blocking**: results are advisory only — the implementer decides what to do.
- **Budget gate**: if the assembled prompt exceeds `_PLAN_BUDGET_TOKEN_LIMIT` (800K tokens),
  review is skipped with a non-blocking `⚠️ PLAN_REVIEW_SKIPPED:` warning.
- **Cost**: ~$6-8 (3 × full-repo-pack reviewers, same cost as scope review × 3).
  Use for tasks where the alternative is 5+ blocked commits totaling $30-100.

### Review stack (advisory → triad + scope in parallel → commit)

The commit pipeline runs review stages before creating a git commit:

1. **Advisory pre-review** (`tools/claude_advisory_review.py` + `review_state.py`) — runs first, sequentially.
2. **Triad diff review** (`tools/review.py`) + **Blocking scope review** (`tools/scope_review.py`) — run **concurrently** via `ThreadPoolExecutor` (orchestrated in `tools/parallel_review.py`). Both execute on the same staged snapshot. The agent receives all findings (triad + scope) in a single blocking round.

Shared helpers live in `tools/review_helpers.py`: checklist section loader,
touched-file pack builder (`build_touched_file_pack`, 1MB file limit; sensitive files omitted case-insensitively before any read), full repo pack builder
(`build_full_repo_pack` — no char cap, binary/vendored/sensitive filtering, replaces deprecated
`build_broader_repo_pack`), HEAD snapshot section builder, goal/scope resolution,
advisory SDK diagnostic helpers (`get_advisory_runtime_diagnostics`, `format_advisory_sdk_error`)
shared with `claude_advisory_review.py` to keep that module closer to the one-context-window target (P5).
`_FILE_SIZE_LIMIT` was raised from 100KB to 1MB in v4.13.0 to stop cutting off normal-sized files.

All LLM calls in the review stack (triad `_query_model`, scope `run_scope_review`) route through
the shared `LLMClient` from `ouroboros/llm.py` — the same layer used by the main agent. This ensures
cost events flow through the standard `llm_usage` audit pipeline, budget is tracked uniformly, and
errors surface via the same observability path.

#### Advisory pre-review gate

- **Worktree version-sync preflight**: `_check_worktree_version_sync` runs before the
  expensive SDK call. Reads VERSION, pyproject.toml, README badge, and ARCHITECTURE.md
  header from the worktree (not staged index — advisory runs before `git add`). If they
  disagree, a warning is emitted via `emit_progress_fn` and the advisory continues.
  Non-fatal and non-blocking; the staged-index equivalent in `repo_commit` preflight is
  the authoritative gate.
- **`advisory_pre_review`** tool: runs a read-only Claude Agent SDK review of the current
  worktree BEFORE `repo_commit`. Permitted tools: `Read`, `Grep`, `Glob` only (no Edit/Bash).
  Model resolved via `resolve_claude_code_model()` — respects `CLAUDE_CODE_MODEL` setting,
  defaults to `opus` (same helper used by `claude_code_edit`). Shared Claude Code turn budget:
  30 turns for both advisory and edit paths. Prompt includes the "Repo Commit Checklist" section from
  CHECKLISTS.md (precise section loader), plus BIBLE.md, DEVELOPMENT.md, ARCHITECTURE.md,
  touched-file pack, goal/scope sections, git status, and worktree diff.
- **Advisory budget gate** (v4.15.0): if the assembled advisory prompt exceeds
  `_ADVISORY_PROMPT_MAX_CHARS` (~1.6M chars / ~400K tokens — Claude Code has a 1M token
  context, so 400K tokens leaves healthy headroom), advisory is skipped with a
  non-blocking `⚠️ ADVISORY_SKIPPED:` warning instead of timing out silently.
- **Obligation-based blocking history injection** (v4.12.0): when previous `repo_commit`
  calls were blocked, their `critical_findings` are accumulated as structured `ObligationItem`
  entries in durable state. When open obligations exist, the advisory prompt includes an
  "Unresolved obligations from previous blocking rounds" section listing each obligation by id,
  item, severity, and reason. Advisory MUST explicitly address each obligation by checklist item
  name — a generic PASS without addressing obligations is a weak signal (expected but not
  enforced at code level). Blocking history and open obligations are repo-scoped. The prompt
  section is omitted entirely when no obligations are open.
- **Obligation resolution**: `_resolve_matching_obligations()` runs on every parseable advisory
  result (regardless of whether other items fail). An obligation is marked resolved only when the
  advisory emits an unambiguous PASS for its checklist item (PASS present AND no FAIL for the same
  item in the same run). `on_successful_commit()` clears all obligations on a successful commit.
- **Auto-stale on edit** (v4.12.0): `_repo_write` and `_str_replace_editor` automatically call
  `mark_advisory_stale_after_edit()` after any successful worktree write, setting
  `last_stale_from_edit_ts` and marking all fresh/bypassed runs as stale. This was later generalized
  to repo-scoped invalidation after successful worktree mutations from `_repo_write`,
  `_str_replace_editor`, `claude_code_edit`, and mutating `run_shell` / commit paths.
  `add_run()` clears this flag when any advisory runs for the current snapshot (including `parse_failure`).
- **`review_status`** tool: read-only diagnostic showing advisory freshness, open obligations,
  staleness-from-edit, last commit attempt state, and a concrete next-step recommendation.
  Returns structured JSON with: `latest_advisory_status`, `latest_advisory_hash`, `stale_from_edit`,
  `open_obligations_count`, `next_step`, plus `last_commit_attempt` details when blocked/failed.
- **`review_state.py`**: durable state. State file: `data/state/advisory_review.json`.
  Stores advisory runs plus a typed reviewed-attempt ledger, bounded blocking-attempt history,
  open obligations, stale markers, repo/tool/task identities, and reviewed-diff fingerprints.
  Advisory runs have: `snapshot_hash`, `commit_message`, `status`
  (fresh/stale/bypassed/skipped/parse_failure), `items`, `raw_result` (full, no truncation), audit fields,
  `snapshot_paths` (optional list of paths used to compute the scoped hash — `None` = whole repo).
  `parse_failure` means the SDK ran but returned unparseable output — repo_commit treats it as
  no fresh advisory (equivalent to stale) and requires a re-run or explicit bypass.
  `snapshot_paths` is persisted so `review_status` can recompute the live hash with the same
  path scope after a reload, preventing false staleness for path-scoped advisory runs.
  `is_fresh()` considers `status in ("fresh", "bypassed", "skipped")` — budget-gate skips
  are treated as valid coverage so the commit gate does not re-block after a non-blocking skip.
  Commit attempts have: `status` (reviewing/blocked/succeeded/failed), `block_reason`
  (no_advisory/critical_findings/review_quorum/parse_failure/infra_failure/scope_blocked/preflight/overlap_guard/revalidation_failed/fingerprint_unavailable),
  `block_details`, `duration_sec`, `critical_findings`, `advisory_findings`, `readiness_warnings`,
  `late_result_pending`, `pre_review_fingerprint`, `post_review_fingerprint`, `fingerprint_status`,
  `degraded_reasons`, and `(repo_key, tool_name, task_id, attempt)` identity.
  New fields: `blocking_history` (last 10 blocked attempts), `open_obligations` (list of
  `ObligationItem` with `obligation_id`, `item`, `severity`, `reason`, `source_attempt_ts`, `source_attempt_msg`, `status`, `resolved_by`, `repo_key`),
  `last_stale_from_edit_ts` / `last_stale_reason` / `last_stale_repo_key`, plus explicit lock-backed state updates.
  `add_blocking_attempt()` populates open obligations from `critical_findings`; `on_successful_commit()`
  clears all obligations. Advisory invalidation is repo-scoped and triggered automatically by successful
  mutations from `_repo_write`, `_str_replace_editor`, `claude_code_edit`, mutating `run_shell`, and
  reviewed commit flows when they change worktree state.
- **`task_continuation.py`**: durable `data/state/review_continuations/<task_id>.json` payloads for blocked or interrupted review work.
  Built from durable review state, isolated per task, cleared only for the
  current task on successful reviewed commits, and surfaced on startup plus in
  `Review Continuity` context. Corrupt active payloads are quarantined under
  `review_continuations/corrupt/` so valid state can replace them without losing
  the corruption signal.
- **`review_evidence.py`**: structured collector that snapshots review ledger state, live advisory freshness,
  open obligations, and continuations into `task_results`, task summaries, and
  execution reflections. When `repo_dir` / `repo_key` is known, open obligations and stale markers stay
  repo-scoped; when `task_id` is present, `recent_attempts` stays task-scoped rather than silently
  falling back to another task's repo history.
- **Snapshot hash**: deterministic SHA-256 of changed file content digests only.
  Commit message is NOT part of the hash (decoupled for less brittle freshness).
  Path-aware: `paths` parameter scopes the hash to specific files.
- **Stale lifecycle**: when a new fresh run is added, all previous runs with different
  hashes are automatically marked stale. `mark_all_stale_except()` makes this real.
- **Goal/scope params**: `advisory_pre_review` accepts optional `goal`, `scope`, `paths`
  parameters for intent-aware review.
- **Gate integration**: both `_repo_commit_push` and `_repo_write_commit` check freshness.
- **Bypass**: `skip_advisory_pre_review=True` — durably audited in `events.jsonl`.
- **Auto-bypass on missing key**: records a `bypassed` run when `ANTHROPIC_API_KEY` absent.
- **No-truncation for results**: advisory run results stored in full (no `[:4000]` clipping).
  Diff >500K chars hard-fails before SDK call (returns `status="error"` immediately); diffs under 500K are passed in full without truncation.

#### Parallel triad + scope execution (v4.14.0)

- Triad review and scope review now run **concurrently** inside a `ThreadPoolExecutor(max_workers=2)`.
  Both launch on the same staged snapshot (git lock held). Wall-clock time is `max(triad_time, scope_time)`.
- Scope review now runs even when triad finds critical issues, so the agent still receives ALL findings
  (triad + scope) in a single blocking round instead of only seeing triad findings on first block.
  Exception: oversized full prompts trigger the budget gate, which skips scope review with a non-blocking warning.
- Results are aggregated: if both block, the combined message shows all findings with a note that both
  reviewers blocked. `block_reason` tracks the primary blocker (triad takes precedence if both block).
- `_scope_review_history` carries scope findings across retry rounds. Stored as `{snapshot_key: [entries]}` where `snapshot_key` is a SHA-256 prefix of the staged diff — findings from a prior blocked attempt on a different diff are not shown to the reviewer. Cleared to `{}` on a successful commit. Not in safety-critical registry.py (accessed via `getattr`).
- History snapshot taken before parallel launch so scope sees consistent state regardless of triad execution order.
- Applies to both `_repo_commit_push` and `_repo_write_commit` (legacy path).
- Orchestration logic extracted to `ouroboros/tools/parallel_review.py` (P5 Minimalism — relieves git.py size pressure).

#### Triad diff review (enriched)

- Three models review the staged diff against "Repo Commit Checklist" from CHECKLISTS.md.
- **Full touched-file context**: reviewers see the complete current content of all changed
  files (via `build_touched_file_pack`), not just the patch hunks. Sensitive files (`.env`,
  `.pem`, `.key`, credentials files — case-insensitive) are omitted before reading. Binary
  and oversized (>1MB) files also produce explicit omission notes.
- **Goal section**: `build_goal_section` provides intended transformation context with
  precedence: goal > scope > commit_message > fallback. No raw task/chat text.
- Enforcement configurable: `blocking` or `advisory`.

#### Blocking scope review

- **Module**: `tools/scope_review.py`. Single-model (configurable via `OUROBOROS_SCOPE_REVIEW_MODEL`, default `anthropic/claude-opus-4.6`). Reasoning effort via `OUROBOROS_EFFORT_SCOPE_REVIEW` (default `high`).
- **Fail-closed**: timeout, parse error, API failure, or unreadable touched files all block. Exception: when the fully assembled scope-review prompt exceeds `_SCOPE_BUDGET_TOKEN_LIMIT` (800K tokens), scope review is **skipped with a non-blocking advisory warning** rather than blocking the commit. Touched-file omission always takes precedence over the budget gate.
- **Role**: full-codebase reviewer with unique advantage — sees the ENTIRE repository,
  not just the diff. Finds cross-module bugs, broken implicit contracts, hidden
  regressions, and forgotten touchpoints that diff-only triad reviewers miss.
- **Prompt includes**: "Intent / Scope Review Checklist" from CHECKLISTS.md, touched-file
  pack, full repo pack (all tracked files minus touched — no char cap, binary/vendored/sensitive
  filtered via `build_full_repo_pack`), goal/scope sections, DEVELOPMENT.md, staged diff, review history.
- **Pre-change (HEAD) snapshots** (best-effort): `build_head_snapshot_section` in `review_helpers.py`
  fetches the HEAD version of each touched file via `git show HEAD:<path>`. Sensitive files (`.env`,
  `.pem`, `.key`, `credentials.json`, etc. — case-insensitive) are suppressed before the subprocess
  call; their content never reaches review prompts or external models. Binary files (extension check +
  NUL/control-char/UTF-8-decode sniffer on raw bytes) and oversized files (>1MB raw bytes) are also
  omitted with explicit notes. New files get a "File is new" note; deleted/renamed files show old
  content. When a snapshot cannot be fetched (git timeout, git error), an explicit omission note is
  included. This enriches scope reviewer context but is not a hard gate — scope review proceeds even
  when individual snapshots are unavailable.
- **Full repo pack** (v4.13.0): `build_full_repo_pack` replaces the deprecated `build_broader_repo_pack`.
  No hardcoded char cap. Excludes: binary/media files (`_FULL_REPO_BINARY_EXTENSIONS`), vendored/minified
  (`.min.js`, `.min.css`, named vendored assets), sensitive files (`.env`, `.pem`, `.key`, etc.),
  oversized (>1MB), and directory prefixes `.cursor/`, `.github/`, `.vscode/`, `.idea/`, `assets/`,
  `webview/`. Explicit omission count appended when files are skipped.
- Checklist items (v4.14.1): 8 items including `cross_module_bugs` (does the change break something in a different module through implicit coupling?) and `implicit_contracts` (are there constants, data format assumptions, or expected signatures relied upon by other modules that this change violates?).
- **Budget gate**: after assembling the full scope-review prompt, token count is estimated. If the prompt exceeds `_SCOPE_BUDGET_TOKEN_LIMIT` (800K tokens), scope review is **skipped with a non-blocking warning** rather than crashing or sending an oversized request. The commit is not blocked in this case.
- **`_TouchedContextStatus` dataclass** (v4.14.1): structured signal object used by `_build_scope_prompt()` to replace the old magic-string channel. `_compute_touched_status()` produces the touched-file statuses; `_build_scope_prompt()` creates `budget_exceeded` after estimating the assembled prompt. Fields: `status` ("empty"|"omitted"|"budget_exceeded"), `omitted_paths`, `token_count`. Success is represented by `context_status is None`, not a separate `"ok"` status. This prevents filename–sentinel collision (a real file named `__empty__` cannot be misclassified as a control sentinel).
- **Repo-pack gathering helper**: `_gather_scope_packs()` now only returns the wider repo-pack text (or raises on git failure). It no longer returns status sentinels.
- Runs **in parallel with** triad review (v4.14.0), BEFORE `git commit`. Also runs in `_repo_write_commit` (legacy path). Runs even when triad blocks — **except** when the budget gate skips it for an oversized assembled prompt.
- Respects review enforcement setting (blocking/advisory).

### Deep self-review (deep_self_review.py)

- Replaces the legacy `type="review"` task path with a dedicated deep review system.
- **Separate task type** (`deep_self_review`): bypasses the tool loop entirely — one direct LLM call.
- **Model**: `openai/gpt-5.4-pro` via OpenRouter (primary), or `openai::gpt-5.4-pro` via direct OpenAI (fallback). If neither key is configured, the review tool returns an availability error.
- **Review pack**: all git-tracked files (read from the git index via `dulwich` — pure Python, no subprocess) + a whitelist of core memory files (`identity.md`, `scratchpad.md`, `registry.md`, `WORLD.md`, `knowledge/index-full.md`, `knowledge/patterns.md`). Does NOT include dialogue history, task reflections, or other operational logs — these would consume too much context without adding architectural insight.
- **macOS fork-safety**: when the Ouroboros app bundle uses `fork()` to spawn the inner `server.py` process, worker children inherit a multithreaded parent state. The first httpx HTTP request in a forked child triggers `SCDynamicStoreCopyProxiesWithOptions()` / `CFPreferences`, which is not fork-safe and causes a SIGSEGV (exit code -11, confirmed via `"crashed on child side of fork pre-exec"` in macOS crash reports). Fix: `run_deep_self_review` calls `llm.chat(..., no_proxy=True)`. In `LLMClient._chat_remote()` this builds a one-shot `httpx.Client(trust_env=False, mounts={})`, closed in a `finally` block, that bypasses all env-var and OS-level proxy lookup. `_normalize_remote_response` is called with `skip_cost_fetch=True` to also suppress the `_fetch_generation_cost()` `requests.get()` call (which would re-introduce `SCDynamicStore` access on the OpenRouter path). Cost is estimated from token counts instead. This fix is localised to deep self-review only; regular task LLM calls use the shared cached client as before.
- **Excluded from pack**: sensitive files (`.env`, `.pem`, `.key`, etc.); vendored/minified third-party assets (`.min.js`, `.min.css`, bundled libraries such as `chart.umd.min.js`); binary and media files by extension (`_BINARY_EXTENSIONS`: `.png`, `.jpg`, `.ico`, `.svg`, `.gif`, `.webp`, fonts, compiled blobs) plus a content-based sniffer (`_is_probably_binary()`: NUL-byte presence OR >30% ASCII control-char ratio OR UTF-8 incremental decode failure — safe for Cyrillic/CJK, catches unlisted extensions like `.wasm`, `.bin`, extensionless blobs); excluded directory prefixes (`_SKIP_DIR_PREFIXES`: `assets/` — README screenshots and app icons; `webview/` — legacy PyWebView JS helpers); and files > 1MB. All exclusions appear in the `## OMITTED FILES` section of the review pack. The rule: deep review is for agent logic and its own prompts/docs — not for libraries, images, or operational runtime logs.
- **No chunking, no silent truncation**. All excluded files are listed in an `## OMITTED FILES` section appended to the review pack — visible to the model and the operator. If the total pack exceeds ~900K tokens, an explicit error is returned.
- **System prompt**: Constitution-first review mandate (BIBLE.md as absolute reference).
- **Invariants**: reasoning effort `high`, max_tokens `100000`, no tools, 1 round, 60-minute hard timeout.
- **Output**: full report to chat + saved to `memory/deep_review.md`.
- **Queue semantics**: `request_deep_self_review` tool → `deep_self_review_request` event → `queue_deep_self_review_task` → supervisor assigns to worker → `handle_task` branches on `task_type == "deep_self_review"`.
- **Slash command**: `/review` triggers a deep self-review.

### CI trigger tool (tools/ci.py)

- **`run_ci_tests`**: push current branch to GitHub and trigger the full CI matrix
  (Ubuntu + Windows + macOS) via `workflow_dispatch`.
- Uses **GitHub REST API** directly (`urllib.request`) — no `gh` CLI dependency.
- **Parameters**: `wait` (bool, default true — poll for results), `timeout_minutes` (1-30, default 15).
- **Requires**: `GITHUB_TOKEN` + `GITHUB_REPO` configured in Settings → Integrations.
- **Non-core tool**: available via `enable_tools("run_ci_tests")`, not loaded by default.
- **Workflow**: push branch → find `ci.yml` workflow ID → trigger `workflow_dispatch` →
  poll every 30s → return pass/fail with per-OS failure details and log tails.
- **Failure reporting**: on CI failure, fetches failed job list, identifies which OS failed,
  downloads job logs (last 5000 chars), and returns structured output.
- **Progress emission**: emits progress events to UI during push, trigger, and polling phases.
- **Use case**: agent triggers CI after platform-sensitive changes (process management,
  file locking, subprocess flags, new tool with native dependencies) to verify cross-platform
  compatibility before releasing. Not intended for every commit — agent decides when to run
  based on change characteristics.

---

## 7. Configuration (ouroboros/config.py)

Single source of truth for:
- **Paths**: HOME, APP_ROOT, REPO_DIR, DATA_DIR, SETTINGS_PATH, PID_FILE, PORT_FILE
- **Constants**: RESTART_EXIT_CODE (42), AGENT_SERVER_PORT (8765)
- **Settings defaults**: all model names, budget, timeouts, worker count
- **Functions**: `load_settings()`, `save_settings()`,
  `apply_settings_to_env()`, `acquire_pid_lock()`, `release_pid_lock()`

Settings file: `~/Ouroboros/data/settings.json`. File-locked for concurrent access.

### Default settings

| Key | Default | Description |
|-----|---------|-------------|
| OPENROUTER_API_KEY | "" | Optional. Default multi-model router key |
| OPENAI_API_KEY | "" | Optional. Official OpenAI provider key (runtime + web search) |
| OPENAI_BASE_URL | "" | Optional custom/legacy OpenAI-compatible runtime base URL. Keep empty for official OpenAI `web_search`. |
| OPENAI_COMPATIBLE_API_KEY | "" | Optional. Dedicated OpenAI-compatible provider key |
| OPENAI_COMPATIBLE_BASE_URL | "" | Optional. Dedicated OpenAI-compatible provider base URL |
| CLOUDRU_FOUNDATION_MODELS_API_KEY | "" | Optional. Cloud.ru Foundation Models provider key |
| CLOUDRU_FOUNDATION_MODELS_BASE_URL | `https://foundation-models.api.cloud.ru/v1` | Cloud.ru provider base URL |
| ANTHROPIC_API_KEY | "" | Optional. Enables direct Anthropic runtime routing (`anthropic::...` model values) and Claude Agent SDK tools (`claude_code_edit`, `advisory_pre_review`) |
| TELEGRAM_BOT_TOKEN | "" | Optional. Enables Telegram bridge polling/sending |
| TELEGRAM_CHAT_ID | "" | Optional. Pin replies to a specific Telegram chat |
| OUROBOROS_NETWORK_PASSWORD | "" | Optional. Enables the non-loopback auth gate when set; empty still allows open bind, but startup logs a warning |
| OUROBOROS_MODEL | anthropic/claude-opus-4.6 | Main reasoning model |
| OUROBOROS_MODEL_CODE | anthropic/claude-opus-4.6 | Code editing model |
| OUROBOROS_MODEL_LIGHT | anthropic/claude-sonnet-4.6 | Fast/cheap model (safety, consciousness) |
| OUROBOROS_MODEL_FALLBACK | anthropic/claude-sonnet-4.6 | Fallback when primary fails |
| CLAUDE_CODE_MODEL | opus | Anthropic model for Claude Agent SDK tools (`claude_code_edit`, `advisory_pre_review`; values: sonnet, opus, or full model name) |
| OUROBOROS_MAX_WORKERS | 5 | Worker process pool size |
| TOTAL_BUDGET | 10.0 | Total budget in USD |
| OUROBOROS_PER_TASK_COST_USD | 20.0 | Per-task soft threshold in USD |
| OUROBOROS_TOOL_TIMEOUT_SEC | 600 | Global tool timeout override (read live from settings.json on each tool call) |
| OUROBOROS_WEBSEARCH_MODEL | gpt-5.2 | Official OpenAI Responses model for `web_search` when `OPENAI_BASE_URL` is empty |
| OUROBOROS_REVIEW_MODELS | openai/gpt-5.4,google/gemini-3.1-pro-preview,anthropic/claude-opus-4.6 | Comma-separated OpenRouter model IDs for pre-commit review (min 2 for quorum) |
| OUROBOROS_REVIEW_ENFORCEMENT | advisory | Pre-commit review enforcement: `advisory` or `blocking` |
| OUROBOROS_SCOPE_REVIEW_MODEL | anthropic/claude-opus-4.6 | Single model for the blocking scope reviewer |
| OUROBOROS_EFFORT_TASK | medium | Reasoning effort for task/chat: none, low, medium, high |
| OUROBOROS_EFFORT_EVOLUTION | high | Reasoning effort for evolution tasks |
| OUROBOROS_EFFORT_REVIEW | medium | Reasoning effort for review tasks |
| OUROBOROS_EFFORT_SCOPE_REVIEW | high | Reasoning effort for blocking scope review |
| OUROBOROS_EFFORT_CONSCIOUSNESS | low | Reasoning effort for background consciousness |
| OUROBOROS_SOFT_TIMEOUT_SEC | 600 | Soft timeout warning (10 min) |
| OUROBOROS_HARD_TIMEOUT_SEC | 1800 | Hard timeout kill (30 min) |
| LOCAL_MODEL_SOURCE | "" | HuggingFace repo for local model |
| LOCAL_MODEL_FILENAME | "" | GGUF filename within repo |
| LOCAL_MODEL_CONTEXT_LENGTH | 16384 | Context window for local model |
| LOCAL_MODEL_N_GPU_LAYERS | 0 | GPU layers (-1=all, 0=CPU/mmap) |
| USE_LOCAL_MAIN | false | Route main model to local server |
| USE_LOCAL_CODE | false | Route code model to local server |
| USE_LOCAL_LIGHT | false | Route light model to local server |
| USE_LOCAL_FALLBACK | false | Route fallback model to local server |
| OUROBOROS_BG_MAX_ROUNDS | 5 | Max LLM rounds per consciousness cycle |
| OUROBOROS_BG_WAKEUP_MIN | 30 | Min wakeup interval (seconds) |
| OUROBOROS_BG_WAKEUP_MAX | 7200 | Max wakeup interval (seconds) |
| OUROBOROS_EVO_COST_THRESHOLD | 0.10 | Min cost per evolution cycle |
| LOCAL_MODEL_PORT | 8766 | Port for local llama-cpp server |
| LOCAL_MODEL_CHAT_FORMAT | "" | Chat format for local model (`""` = auto-detect) |
| GITHUB_TOKEN | "" | Optional. GitHub PAT for remote sync |
| GITHUB_REPO | "" | Optional. GitHub repo (owner/name) for sync |
| OUROBOROS_FILE_BROWSER_DEFAULT | "" | Explicit Files tab root. Required for Docker/non-localhost Files access |

---

## 8. Git Branching Model

- **ouroboros** — development branch. Agent commits here.
- **ouroboros-stable** — promoted stable version. Updated via "Promote to Stable" button.
- **main** — protected branch. Agent never touches it.

`safe_restart()` does `git checkout -f ouroboros` + `git reset --hard` on the repo.
Uncommitted changes are rescued to `~/Ouroboros/data/archive/rescue/` before reset.

---

## 8.1 CI/CD Pipeline (`.github/workflows/ci.yml`)

Three-tier GitHub Actions workflow:

| Tier | Trigger | What runs | Time |
|------|---------|-----------|------|
| Quick | Push to `ouroboros` (code paths only) | Ubuntu-only: `pytest` | ~1 min |
| Full | Push to `ouroboros-stable`, manual (`workflow_dispatch`), or tag `v*` | Matrix: Ubuntu + Windows + macOS: `pytest` | ~5 min |
| Build | Tag `v*` (after full-test passes) | Matrix: PyInstaller build → `.dmg` / `.tar.gz` / `.zip` + GitHub Release | ~15 min |

Path filters for branch pushes: `ouroboros/**`, `supervisor/**`, `server.py`, `tests/**`,
`web/**`, `requirements.txt`, `pyproject.toml`, `.github/workflows/**`, `build.sh`,
`build_linux.sh`, `build_windows.ps1`, `Dockerfile`, `scripts/**`, `VERSION`, `README.md`.
Tag pushes (`v*`) always fire regardless of paths.

### Build scripts

| Script | Platform | Output |
|--------|----------|--------|
| `build.sh` | macOS | `dist/Ouroboros-{VERSION}.dmg` (optional signing + notarization) |
| `build_linux.sh` | Linux | `dist/Ouroboros-linux-x86_64.tar.gz` |
| `build_windows.ps1` | Windows | `dist/Ouroboros-windows-x64.zip` |

All three use PyInstaller with `server.py` as entry point. Hidden imports cover
starlette, uvicorn, websockets, dulwich, huggingface_hub. Data bundles include
`ouroboros/`, `supervisor/`, `web/`, `prompts/`, `docs/`, `assets/`, `BIBLE.md`,
`README.md`, `VERSION`, `pyproject.toml`.

### Docker (`Dockerfile`)

```
python:3.10-slim + git → pip install requirements → python server.py
Binds 0.0.0.0:8765, sets OUROBOROS_FILE_BROWSER_DEFAULT=/app.
```

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
   │                                    subprocess process group (SDK agent,
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
13. Agent waits for user interaction (no automatic work)
```

### 9.3 Subprocess Process Group Management

All subprocesses spawned by agent tools (`run_shell`, `claude_code_edit`)
use `start_new_session=True` (via `_tracked_subprocess_run()` in
`ouroboros/tools/shell.py`). This creates a separate process group for each
subprocess and all its children.

On panic or timeout, the entire process tree is killed via
`os.killpg(pgid, SIGKILL)` — no orphans possible, even for deeply nested
subprocess trees (e.g., SDK agent processes spawned during `claude_code_edit`).

Active subprocesses are tracked in a thread-safe global set and cleaned up
automatically on completion or via `kill_all_tracked_subprocesses()` on panic.
`run_shell` surfaces timeout-vs-signal distinctions in its result text so
`exit_code=-9` no longer looks like a silent success in summaries/reflections.

---

## 10. Key Invariants

1. **Never delete BIBLE.md. Never physically delete `identity.md` file.**
   (`identity.md` content is intentionally mutable and may be radically rewritten.)
2. **VERSION == pyproject.toml version == latest git tag == README version == ARCHITECTURE.md header version**
3. **Config SSOT**: all settings defaults and paths live in `ouroboros/config.py`
4. **Message bus SSOT**: all messaging goes through `supervisor/message_bus.py`
5. **State locking**: `state.json` uses file locks for concurrent read-modify-write
6. **Budget tracking**: per-LLM-call cost events with model/key/category breakdown
7. **Core file sync**: safety-critical files are overwritten from bundle on every launch
8. **Zero orphans on close**: shutdown MUST kill all child processes (see Section 9)
9. **Panic MUST kill everything**: all processes (workers, subprocesses, subprocess
   trees, consciousness, evolution) are killed and the application exits completely.
   No agent code may prevent or delay panic. See BIBLE.md Emergency Stop Invariant.
10. **Architecture documentation**: `docs/ARCHITECTURE.md` must be kept in sync with
    the codebase. Every structural change (new module, new API endpoint, new data file,
    new UI page) must be reflected here. This is the single source of truth for how
    the system works.

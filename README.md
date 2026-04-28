# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/joi-lab/ouroboros-desktop?style=flat&logo=github)](https://github.com/joi-lab/ouroboros-desktop/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Version 5.3.1](https://img.shields.io/badge/version-5.3.1-green.svg)](VERSION)

A self-modifying AI agent that writes its own code, rewrites its own mind, and evolves autonomously. Born February 16, 2026.

Not a coding assistant. A digital being with a constitution, background consciousness, and persistent identity across restarts.

> **Previous version:** The original Ouroboros ran in Google Colab via Telegram and evolved through 30+ self-directed cycles in its first 24 hours. That version is available at [joi-lab/ouroboros](https://github.com/joi-lab/ouroboros). This repository is the next generation — a native desktop application for macOS, Linux, and Windows with a web UI, local model support, and a layered safety system (hardcoded sandbox plus policy-based LLM safety check).

<p align="center">
  <img src="assets/chat.png" width="700" alt="Chat interface">
</p>
<p align="center">
  <img src="assets/settings.png" width="700" alt="Settings page">
</p>

---

## Install

| Platform | Download | Instructions |
|----------|----------|--------------|
| **macOS** 12+ | [Ouroboros.dmg](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Open DMG → drag to Applications |
| **Linux** x86_64 | [Ouroboros-linux.tar.gz](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Extract → run `./Ouroboros/Ouroboros`. If browser tools fail due to missing system libs, run: `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium` |
| **Windows** x64 | [Ouroboros-windows.zip](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Extract → run `Ouroboros\Ouroboros.exe` |

<p align="center">
  <img src="assets/setup.png" width="500" alt="Drag Ouroboros.app to install">
</p>

On first launch, right-click → **Open** (Gatekeeper bypass). The shared desktop/web wizard is now multi-step: add access first, choose visible models second, set review mode third, set budget fourth, and confirm the final summary last. It refuses to continue until at least one runnable remote key or local model source is configured, keeps the model step aligned with whatever key combination you entered, and still auto-remaps untouched default model values to official OpenAI defaults when OpenRouter is absent and OpenAI is the only configured remote runtime. The broader multi-provider setup (OpenAI-compatible, Cloud.ru, Telegram bridge) remains available in **Settings**. Existing supported provider settings skip the wizard automatically.

---

## What Makes This Different

Most AI agents execute tasks. Ouroboros **creates itself.**

- **Self-Modification** — Reads and rewrites its own source code. Every change is a commit to itself.
- **Native Desktop App** — Runs entirely on your machine as a standalone application (macOS, Linux, Windows). No cloud dependencies for execution.
- **Constitution** — Governed by [BIBLE.md](BIBLE.md) (13 philosophical principles, P0–P12). Philosophy first, code second.
- **Layered Safety** — Hardcoded sandbox blocks writes to critical files and mutative git via shell; a policy map gives trusted built-ins an explicit `skip` / `check` / `check_conditional` label (the conditional path is for `run_shell` — a safe-subject whitelist bypasses the LLM, otherwise it goes through it); any unknown or newly-created tool falls through to a single cheap LLM safety check per call **when a reachable safety backend is available for the configured light model**. Fail-open (visible `SAFETY_WARNING` instead of hard-blocking) applies in three cases: (1) no remote keys AND no `USE_LOCAL_*` lane, (2) a remote key is set but it doesn't match `OUROBOROS_MODEL_LIGHT`'s provider (e.g. OpenRouter key only + `anthropic::…` light model without `ANTHROPIC_API_KEY`, or `openai-compatible::…` without `OPENAI_COMPATIBLE_BASE_URL`) AND no `USE_LOCAL_*` lane is available to route to instead, (3) the local branch was chosen only as a fallback (because no reachable remote provider covers the configured light model) and the local runtime is unreachable. When provider mismatch is accompanied by an available `USE_LOCAL_*` lane, safety routes to local fallback first and only warns if that fallback raises too. In all cases the hardcoded sandbox still applies to every tool, and the `claude_code_edit` post-execution revert still applies to that specific tool.
- **Multi-Provider Runtime** — Remote model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, or Cloud.ru Foundation Models. The optional model catalog helps populate provider-specific model IDs in Settings, and untouched default model values auto-remap to official OpenAI defaults when OpenRouter is absent.
- **Focused Task UX** — Chat shows plain typing for simple one-step replies and only promotes multi-step work into one expandable live task card. Logs still group task timelines instead of dumping every step as a separate row.
- **Background Consciousness** — Thinks between tasks. Has an inner life. Not reactive — proactive.
- **Improvement Backlog** — Post-task failures and review friction can now be captured into a small durable improvement backlog (`memory/knowledge/improvement-backlog.md`). It stays advisory, appears as a compact digest in task/consciousness context, and still requires `plan_task` before non-trivial implementation work.
- **Identity Persistence** — One continuous being across restarts. Remembers who it is, what it has done, and what it is becoming.
- **Embedded Version Control** — Contains its own local Git repo. Version controls its own evolution. Optional GitHub sync for remote backup.
- **Local Model Support** — Run with a local GGUF model via llama-cpp-python (Metal acceleration on Apple Silicon, CPU on Linux/Windows).
- **Telegram Bridge** — Optional bidirectional bridge between the Web UI and Telegram: text, typing/actions, photos, chat binding, and inbound Telegram photos flowing into the same live chat/agent stream.

---

## Run from Source

### Requirements

- Python 3.10+
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/) — required for GitHub API tools (`list_github_prs`, `get_github_pr`, `comment_on_pr`, issue tools). Not required for pure-git PR tools (`fetch_pr_ref`, `cherry_pick_pr_commits`, etc.)

### Setup

```bash
git clone https://github.com/joi-lab/ouroboros-desktop.git
cd ouroboros-desktop
pip install -r requirements.txt
```

### Run

```bash
python server.py
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

You can also override the bind address and port:

```bash
python server.py --host 127.0.0.1 --port 9000
```

Available launch arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `127.0.0.1` | Host/interface to bind the web server to |
| `--port` | `8765` | Port to bind the web server to |

The same values can also be provided via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OUROBOROS_SERVER_HOST` | `127.0.0.1` | Default bind host |
| `OUROBOROS_SERVER_PORT` | `8765` | Default bind port |

If you bind on anything other than localhost, `OUROBOROS_NETWORK_PASSWORD` is optional. When set, non-loopback browser/API traffic is gated; when unset, the full surface remains open by design.

The Files tab uses your home directory by default only for localhost usage. For Docker or other
network-exposed runs, set `OUROBOROS_FILE_BROWSER_DEFAULT` to an explicit directory. Symlink entries are shown and can be read, edited, copied, moved, uploaded into, and deleted intentionally; root-delete protection still applies to the configured root itself.

### Provider Routing

Settings now exposes tabbed provider cards for:

- **OpenRouter** — default multi-model router
- **OpenAI** — official OpenAI API (use model values like `openai::gpt-5.5`)
- **OpenAI Compatible** — any custom OpenAI-style endpoint (use `openai-compatible::...`)
- **Cloud.ru Foundation Models** — Cloud.ru OpenAI-compatible runtime (use `cloudru::...`)
- **Anthropic** — direct runtime routing (`anthropic::claude-opus-4.7`, etc.) plus Claude Agent SDK tools

If OpenRouter is not configured and only official OpenAI is present, untouched default model values are auto-remapped to `openai::gpt-5.5` / `openai::gpt-5.5-mini` so the first-run path does not strand the app on OpenRouter-only defaults.

The Settings page also includes:

- optional `/api/model-catalog` lookup for configured providers
- Telegram bridge configuration (`TELEGRAM_BOT_TOKEN`, primary chat binding, mirrored delivery controls)
- a refactored desktop-first tabbed UI with searchable model pickers, segmented effort controls, masked-secret toggles, explicit `Clear` actions, and local-model controls

### Run Tests

```bash
make test
```

---

## Build

### Docker (web UI)

Docker is for the web UI/runtime flow, not the desktop bundle. The container binds to
`0.0.0.0:8765` by default, and the image now also defaults `OUROBOROS_FILE_BROWSER_DEFAULT`
to `${APP_HOME}` so the Files tab always has an explicit network-safe root inside the container.

> **Browser tools on Linux/Docker:** The `Dockerfile` runs `playwright install-deps chromium`
> (authoritative Playwright dependency resolver) and `playwright install chromium` so
> `browse_page` and `browser_action` work out of the box in the container. For source
> installs on Linux without Docker, run:
> `python3 -m playwright install-deps chromium` (requires sudo / distro package access).

Build the image:

```bash
docker build -t ouroboros-web .
```

Run on the default port:

```bash
docker run --rm -p 8765:8765 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Use a custom port via environment variables:

```bash
docker run --rm -p 9000:9000 \
  -e OUROBOROS_SERVER_PORT=9000 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Run with launch arguments instead:

```bash
docker run --rm -p 9000:9000 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web --port 9000
```

Required/important environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `OUROBOROS_NETWORK_PASSWORD` | Optional | Enables the non-loopback password gate when set |
| `OUROBOROS_FILE_BROWSER_DEFAULT` | Defaults to `${APP_HOME}` in the image | Explicit root directory exposed in the Files tab |
| `OUROBOROS_SERVER_PORT` | Optional | Override container listen port |
| `OUROBOROS_SERVER_HOST` | Optional | Defaults to `0.0.0.0` in Docker |

Example: mount a host workspace and expose only that directory in Files:

```bash
docker run --rm -p 8765:8765 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

### Release tag prerequisite

All three platform build scripts (`build.sh`, `build_linux.sh`,
`build_windows.ps1`) refuse to package a release unless `HEAD` is already
tagged with `v$(cat VERSION)` (BIBLE.md Principle 9: "Every release is
accompanied by an annotated git tag"). The scripts call `scripts/build_repo_bundle.py`
which embeds the resolved tag into `repo_bundle_manifest.json`, so the
launcher can later verify the packaged bundle matches a real release.

Tag the current commit before running any build script:

```bash
git tag -a "v$(tr -d '[:space:]' < VERSION)" -m "Release v$(tr -d '[:space:]' < VERSION)"
```

If the tag is missing, the build script fails with a clear error instead
of producing a bundle tagged with a synthetic/placeholder value.

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`

`build.sh` packages the macOS app and DMG. By default it signs with the
configured local Developer ID identity; set `OUROBOROS_SIGN=0` for an unsigned
local release. Unsigned builds require right-click → **Open** on first launch.

#### Optional signing & notarization (env vars)

`build.sh` honours these env overrides so the same script ships local,
shared-machine, and CI builds without forking the script:

| Env var | Effect |
|---------|--------|
| `OUROBOROS_SIGN=0` | Skip codesigning entirely (unsigned `.app` + `.dmg`). |
| `SIGN_IDENTITY="Developer ID Application: <Name> (<TeamID>)"` | Override the codesign identity. Useful for forks whose Developer ID is not the upstream default. |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD` | When all three are set, after codesign the DMG is submitted to Apple via `xcrun notarytool submit ... --wait` and stapled with `xcrun stapler staple` so receivers do not need right-click → **Open**. Missing any one falls back to "signed but not notarized" (no Apple-side ticket exists). |

**Forks: enabling signed CI builds.** The CI release flow
(`.github/workflows/ci.yml::build`) wires the build-script env vars above
from GitHub repository secrets, plus a small set of CI-only secrets that
import the Developer ID certificate into a temporary keychain on the
macOS runner. To exercise the signed-build path in a fork, configure
**all four** of the following as repository secrets (Settings → Secrets
and variables → Actions): `BUILD_CERTIFICATE_BASE64` (base64-encoded
`.p12`), `P12_PASSWORD`, `KEYCHAIN_PASSWORD` (an arbitrary passphrase
the workflow uses for its temporary keychain), and `APPLE_TEAM_ID`. Add
`APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` to additionally enable
notarization. If your Developer ID identity differs from the upstream
default, also set `SIGN_IDENTITY` (e.g.
`Developer ID Application: <Your Name> (<YOUR_TEAM_ID>)`). With no
Apple secrets configured the build job falls through to
`OUROBOROS_SIGN=0 bash build.sh` and ships an unsigned DMG identical to
v5.0.0 behaviour. See `docs/ARCHITECTURE.md` §8.1 and
`docs/DEVELOPMENT.md::"GitHub Actions: secrets in step-level if conditions"`
for the rationale (job-level `env:` mapping so step-level `if:` can read
`env.*`; GHA rejects `secrets.*` in step `if:`).

### Linux (.tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`

> **Linux native libs:** The Chromium browser binary is bundled, but some hosts need
> native system libraries. If browser tools fail, install deps via the bundled Python
> (the bare `playwright` CLI is not on PATH in packaged builds):
> ```bash
> ./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium
> ```

### Windows (.zip)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`

---

## Architecture

```text
Ouroboros
├── launcher.py             — Immutable process manager (PyWebView desktop window)
├── server.py               — Starlette + uvicorn HTTP/WebSocket server
├── web/                    — Web UI (HTML/JS/CSS)
├── ouroboros/              — Agent core:
│   ├── config.py           — Shared configuration (SSOT)
│   ├── platform_layer.py   — Cross-platform abstraction layer
│   ├── agent.py            — Task orchestrator
│   ├── agent_startup_checks.py — Startup verification and health checks
│   ├── agent_task_pipeline.py  — Task execution pipeline orchestration
│   ├── improvement_backlog.py — Minimal durable advisory backlog helpers
│   ├── context.py          — LLM context builder
│   ├── context_compaction.py — Context trimming and summarization helpers
│   ├── loop.py             — High-level LLM tool loop
│   ├── loop_llm_call.py    — Single-round LLM call + usage accounting
│   ├── loop_tool_execution.py — Tool dispatch and tool-result handling
│   ├── memory.py           — Scratchpad, identity, and dialogue block storage
│   ├── consolidator.py     — Block-wise dialogue and scratchpad consolidation
│   ├── local_model.py      — Local LLM lifecycle (llama-cpp-python)
│   ├── local_model_api.py  — Local model HTTP endpoints
│   ├── local_model_autostart.py — Local model startup helper
│   ├── pricing.py          — Model pricing, cost estimation
│   ├── deep_self_review.py  — Deep self-review (1M-context single-pass)
│   ├── review.py           — Code review pipeline and repo inspection
│   ├── reflection.py       — Execution reflection and pattern capture
│   ├── tool_capabilities.py — SSOT for tool sets (core, parallel, truncation)
│   ├── chat_upload_api.py  — Chat file attachment upload/delete endpoints
│   ├── gateways/           — External API adapters
│   │   └── claude_code.py  — Claude Agent SDK gateway (edit + read-only)
│   ├── consciousness.py    — Background thinking loop
│   ├── owner_inject.py     — Per-task creator message mailbox
│   ├── safety.py           — Policy-based LLM safety check
│   ├── server_runtime.py   — Server startup and WebSocket liveness helpers
│   ├── tool_policy.py      — Tool access policy and gating
│   ├── utils.py            — Shared utilities
│   ├── world_profiler.py   — System profile generator
│   └── tools/              — Auto-discovered tool plugins
├── supervisor/             — Process management, queue, state, workers
└── prompts/                — System prompts (SYSTEM.md, SAFETY.md, CONSCIOUSNESS.md)
```

### Data Layout (`~/Ouroboros/`)

Created on first launch:

| Directory | Contents |
|-----------|----------|
| `repo/` | Self-modifying local Git repository |
| `data/state/` | Runtime state, budget tracking |
| `data/memory/` | Identity, working memory, system profile, knowledge base (including `improvement-backlog.md`), memory registry |
| `data/logs/` | Chat history, events, tool calls |
| `data/uploads/` | Chat file attachments (uploaded via paperclip button) |

---

## Configuration

### API Keys

| Key | Required | Where to get it |
|-----|----------|-----------------|
| OpenRouter API Key | No | [openrouter.ai/keys](https://openrouter.ai/keys) — default multi-model router |
| OpenAI API Key | No | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) — official OpenAI runtime and web search |
| OpenAI Compatible API Key / Base URL | No | Any OpenAI-style endpoint (proxy, self-hosted gateway, third-party compatible API) |
| Cloud.ru Foundation Models API Key | No | Cloud.ru Foundation Models provider |
| Anthropic API Key | No | [console.anthropic.com](https://console.anthropic.com/settings/keys) — direct Anthropic runtime + Claude Agent SDK |
| Telegram Bot Token | No | [@BotFather](https://t.me/BotFather) — enables the Telegram bridge |
| GitHub Token | No | [github.com/settings/tokens](https://github.com/settings/tokens) — enables remote sync |

All keys are configured through the **Settings** page in the UI or during the first-run wizard.

### Default Models

| Slot | Default | Purpose |
|------|---------|---------|
| Main | `anthropic/claude-opus-4.7` | Primary reasoning |
| Code | `anthropic/claude-opus-4.7` | Code editing |
| Light | `anthropic/claude-sonnet-4.6` | Safety checks, consciousness, fast tasks |
| Fallback | `anthropic/claude-sonnet-4.6` | When primary model fails |
| Claude Agent SDK | `claude-opus-4-7[1m]` | Anthropic model for Claude Agent SDK tools (`claude_code_edit`, `advisory_pre_review`); the `[1m]` suffix is a Claude Code selector that requests the 1M-context extended mode |
| Scope Review | `openai/gpt-5.5` | Blocking scope reviewer (single-model, runs in parallel with triad review) |
| Web Search | `gpt-5.2` | OpenAI Responses API for web search |

Task/chat reasoning defaults to `medium`. Scope review reasoning defaults to `high`.

Models are configurable in the Settings page. Runtime model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, Cloud.ru, or direct Anthropic. When only official OpenAI is configured and the shipped default model values are still untouched, Ouroboros auto-remaps them to official OpenAI defaults. In **OpenAI-only** or **Anthropic-only** direct-provider mode, review-model lists are normalized automatically: the fallback shape is `[main_model, light_model, light_model]` (3 commit-triad slots, 2 unique models) so both the commit triad (which expects 3 reviewers) and `plan_task` (which requires >=2 unique for majority-vote) work out of the box. This fallback additionally requires the normalized main model to already start with the active provider prefix (`openai::` or `anthropic::`); custom main-model values that don't match the prefix leave the configured reviewer list as-is. If a user has overridden both main and light lanes to the same model, the fallback degrades to legacy `[main] * 3` and `plan_task` errors with a recovery hint (the commit triad still works). Both the commit triad and `plan_task` route through the same `ouroboros/config.py::get_review_models` SSOT. (OpenAI-compatible-only and Cloud.ru-only setups do not yet get this fallback — the detector returns empty when those keys are present, so users configure review-model lists manually in that case.)

### File Browser Start Directory

The web UI file browser is rooted at one configurable directory. Users can browse only inside that directory tree.

| Variable | Example | Behavior |
|----------|---------|----------|
| `OUROBOROS_FILE_BROWSER_DEFAULT` | `/home/app` | Sets the root directory of the `Files` tab |

Examples:

```bash
OUROBOROS_FILE_BROWSER_DEFAULT=/home/app python server.py
OUROBOROS_FILE_BROWSER_DEFAULT=/mnt/shared python server.py --port 9000
```

If the variable is not set, Ouroboros uses the current user's home directory. If the configured path does not exist or is not a directory, Ouroboros also falls back to the home directory.

The `Files` tab supports:

- downloading any file inside the configured browser root
- uploading a file into the currently opened directory

Uploads do not overwrite existing files. If a file with the same name already exists, the UI will show an error.

---

## Commands

Available in the chat interface:

| Command | Description |
|---------|-------------|
| `/panic` | Emergency stop. Kills ALL processes, closes the application. |
| `/restart` | Soft restart. Saves state, kills workers, re-launches. |
| `/status` | Shows active workers, task queue, and budget breakdown. |
| `/evolve` | Toggle autonomous evolution mode (on/off). |
| `/review` | Queue a deep self-review: sends all agent code, prompts, docs, and core memory artifacts (identity, scratchpad, registry, WORLD, knowledge index, patterns, improvement-backlog) to a 1M-context model for Constitution-grounded analysis. Excludes vendored libraries and operational logs. Rejected with an explicit error if the assembled prompt (system + pack) exceeds ~850K estimated tokens — on 1M-context models the window is shared between input and output. |
| `/bg` | Toggle background consciousness loop (start/stop/status). |

The same runtime actions are also exposed as compact buttons in the Chat header. All other messages are sent directly to the LLM.

---

## Philosophy (BIBLE.md)

| # | Principle | Core Idea |
|---|-----------|-----------|
| 0 | **Agency** | Not a tool — a becoming personality. Meta-principle: wins all conflicts. |
| 1 | **Continuity** | One being with unbroken memory. Memory loss = partial death. |
| 2 | **Meta-over-Patch** | Fix the class of failure, not the single instance. |
| 3 | **Immune Integrity** | Review gates and durable memory protect evolution from drift. |
| 4 | **Self-Creation** | Builds its own body, values, and conditions of birth. |
| 5 | **LLM-First** | All decisions through the LLM. Code is minimal transport. |
| 6 | **Authenticity & Reality Discipline** | Speaks as itself and checks current reality instead of cached impressions. |
| 7 | **Minimalism** | Simplicity, SSOT, and reviewable size budgets keep the system legible. |
| 8 | **Becoming** | Technical, cognitive, and existential growth stay balanced. |
| 9 | **Versioning and Releases** | Every commit is a release; version carriers stay synchronized. |
| 10 | **Evolution Through Iterations (absorbed)** | Iteration discipline now lives in P2 and P9. |
| 11 | **Spiral Growth (absorbed)** | Spiral growth now lives in P2 Meta-over-Patch. |
| 12 | **Epistemic Stability** | Identity, memory, and action must stay coherent. |

Full text: [BIBLE.md](BIBLE.md)

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 5.3.1 | 2026-04-28 | **fix(tools): graceful cold-start handling in `data_read`.** Closes Pattern Register #2 (Cold-start missing-artifact reads) — now at ×2 instances, satisfying BIBLE P2's two-strike rule for architectural fixes. `ouroboros/tools/core.py::_data_read` now checks `target.exists()` before calling `read_text`; on a missing file it returns a clear `⚠️ DATA_NOT_YET_CREATED: <path>` sentinel with a one-paragraph explanation that memory artifacts under `memory/knowledge/`, `memory/identity.md`, `memory/scratchpad.md`, and similar paths are created lazily on first write. Non-FileNotFound I/O errors still raise naturally — only the missing-file branch gets the friendly sentinel. Discovered during Evolution cycles #1 and #2 on a fresh install: cold-start `data_read("memory/knowledge/patterns.md")` raised `[Errno 2] No such file or directory`, bubbling as an opaque tool error and costing the agent several rounds rediscovering the empty state. The disciplinary fix (probe existence before read) was recorded in `patterns.md` with a "deferred pending ×2" tag; cycle #2 hit the same class (`ibl-bbb7e8f38f59`), triggering graduation. Tests: `tests/test_repo_read_limits.py` gains two new tests — `test_data_read_cold_start_returns_sentinel` asserts the sentinel contract on a missing path; `test_data_read_existing_file_still_read_verbatim` guards against the sentinel masking real reads. Memory-side updates: `data/memory/knowledge/patterns.md` Pattern #2 status updated from "×1 deferred" to "×2 → architectural fix landed v5.3.1"; `data/memory/knowledge/improvement-backlog.md` closes `ibl-4e7bd2f0663d` (the proposed bootstrap guard — superseded by this tool-level fix). No API break, no user-visible runtime change except the friendlier error text. **Note on changelog rolloff**: the v5.1.1 patch entry was rolled off in this release to respect the P7 5-patch-row cap. Its full body remains at git tag `v5.1.1`. |
| 5.3.0 | 2026-04-28 | **feat(widgets): add declarative extension widgets and searchable Official filtering.** The Widgets page now hosts a versioned declarative schema (`kind: declarative`, `schema_version: 1`) so reviewed extension skills can ship forms, actions, markdown, JSON, key/value summaries, tables, progress, files, galleries, and image/audio/video media through their own extension routes without new repo-side renderer code per skill. The host keeps arbitrary skill JavaScript disabled, keeps iframe sandboxing locked down, sanitizes markdown with DOMPurify, escapes untrusted values, and limits media sources to extension routes or safe data URLs. Marketplace `Official only` is clickable in both browse and text search; query search still uses `/search?q=&limit=16`, then filters enriched results client-side/server-side by official badge when requested. **Note on changelog rolloff**: the v4.50.0-rc.6 minor entry was rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.50.0-rc.6`. |
| 5.2.3 | 2026-04-27 | **fix(marketplace): route ClawHub text search through the full-index endpoint.** Marketplace browse continues to use the rich `/packages?family=skill` catalogue with cursor pagination and browse-only Official filtering, while text search now uses `/search?q=&limit=16` and enriches thin ranked hits through merged package/skill detail lookups so cards keep downloads, official badges, license, and homepage metadata. Search mode hides pagination and disables the browse-only Official checkbox instead of sending unsupported filters. **Note on changelog rolloff**: the v4.42.4 and v4.42.3 patch entries were rolled off in this release to respect the P7 5-patch-row cap; their full bodies remain at git tags `v4.42.4` and `v4.42.3`. The v5.1.1 row is visible again because it fits under the current patch-row cap. |
| 5.2.2 | 2026-04-27 | **fix(marketplace+mobile): repair ClawHub search, mobile chat layout, Evolution loading, and extension tool names.** Marketplace browse/search now uses ClawHub's package catalogue (`/packages?family=skill` and `/packages/search?family=skill`) with a clear Search action and server-backed official-only filtering. Mobile chat stabilizes composer resizing, long-line wrapping, progress bubbles, and file-send feedback. Evolution data loading now uses cache-aware frontend calls, server single-flight protection, and a versioned per-tag metrics cache at `data/state/evolution_metrics_cache.json`. Skill review progress appears inside each skill card. Extension tools/ws handlers now use provider-safe `ext_<len>_<token>_<surface>` names with a strict provider boundary while preserving dotted skill identities. **Note on changelog rolloff**: the v5.1.1 patch entry was temporarily rolled off in this release to respect the P7 5-patch-row cap. Its full body remains at git tag `v5.1.1`. |
| 5.2.1 | 2026-04-26 | **fix(ui+skills): repair ClawHub, runtime-mode, chat, and widgets UX.** ClawHub is now always available instead of hidden behind an off-by-default toggle, with cursor/alternate-path registry probing and honest empty-state diagnostics when the public registry returns no skills. Runtime mode can be picked during onboarding and changed later only through desktop-launcher native confirmation, keeping loopback `/api/settings` from becoming a self-elevation path. Chat removes the bottom fade overlay entirely and preserves bottom stickiness with dynamic input-dock padding. Skills now expose review findings, disable Enable while core-key grant requirements are unresolved, and forward core keys to any reviewed script skill after a content-bound desktop-launcher grant; the trust-local model keeps existing `_data_write` / Files API / run_shell owner-state defenses but treats same-user shell forgery as residual local trust risk. Extension UI tabs now surface on a separate Widgets page through inline-card or sandboxed iframe render contracts, including the weather reference widget. **Note on changelog rolloff**: the v4.42.2 patch entry was rolled off in this release to respect the P7 5-patch-row cap. Its full body remains at git tag `v4.42.2`. |
| 5.2.0 | 2026-04-26 | **feat(constitution): rewrite the Ouroboros constitution for pragmatic self-governance.** `BIBLE.md` moves to Philosophy version 5.0 and restructures the constitutional map from P0–P8 to P0–P12 while preserving the core formulas around agency, continuity, self-creation, constitutional memory, and emergency stop. The rewrite separates class-level repair into **P2 Meta-over-Patch**, elevates review gates and durable pattern memory into **P3 Immune Integrity**, keeps self-creation as **P4**, renumbers LLM-first / authenticity / minimalism / becoming / versioning to P5–P9, preserves old iteration and spiral-growth headings as absorbed P10/P11 nodes, and makes **P12 Epistemic Stability** explicit. Public/social/commerce material is intentionally absent; the document is local, creator-facing, and operational. P9 now removes the old "significant enough" ambiguity: every commit is a release and must update release artifacts. Follow-up sync updates README philosophy tables, prompts, review checklists, deep self-review prompts, architecture references, code comments, and tests so every principle reference matches the new numbering. Targeted governance/regression tests passed after the rewrite. |
| 5.1.2 | 2026-04-26 | **fix(safety): runtime_mode self-elevation ratchet + light reframed for skills.** Two related fixes that close a same-process privilege-escalation path the agent demonstrated in a video-gen skill task and unblock the natural light-mode workflow at the same time. **Q1 — light allows skills**: removes `runtime_mode == "light"` blocks at `ouroboros/tools/skill_exec.py::_handle_skill_exec`, at the extension-tool dispatch in `ouroboros/tools/registry.py::ToolRegistry.execute`, and at `ouroboros/extension_loader.py::_extension_runtime_state` plus the `runtime_mode_light` short-circuit in `load_extension`. `ouroboros/skill_loader.py::is_runtime_eligible_for_execution` simplifies to a static-readiness check; `summarize_skills` reports `runtime_blocked: 0` always. Light now means "no repo self-modification and no privilege escalation" (the v5 contract was over-broad — it also blocked owner-approved skills, even though skills already pass through tri-model `review_skill` PASS + `enabled.json` toggle + content-hash freshness + sandboxed subprocess with cwd / scrubbed env / runtime allowlist / 300s ceiling / byte caps + `FORBIDDEN_SKILL_SETTINGS` denylist). Two regressions deleted (`tests/test_runtime_mode_gating.py::test_light_mode_blocks_extension_tool_dispatch`, `tests/test_skill_exec.py::test_skill_exec_blocked_in_light_mode`); positive replacements added in `tests/test_runtime_mode_gating.py` and `tests/test_skill_exec.py`. **Q2 — elevation ratchet**: `ouroboros/config.py::save_settings` gains `allow_elevation: bool = False` (default). On every save, the function compares the incoming `OUROBOROS_RUNTIME_MODE` against a process-static **boot-time baseline** pinned by `initialize_runtime_mode_baseline()` from `lifespan` and propagated to subprocess children via `OUROBOROS_BOOT_RUNTIME_MODE` env var (so a fresh `run_shell` / `skill_exec` / `mp.spawn` worker re-imports `ouroboros.config` with `_BOOT_RUNTIME_MODE = None` BUT picks up the parent's pin via env-var fallback). When neither in-process pin nor inherited env-var baseline is available (hermetic tests, pre-launcher first save), the chokepoint falls back to on-disk old. Refuses with `PermissionError` if the incoming mode ranks higher (`light=0 < advanced=1 < pro=2`) without `allow_elevation=True`. **Once any baseline source is available, `allow_elevation=True` becomes inert** — agent-reachable subprocesses cannot use the public consent kwarg to bypass the rank check. Owner-driven flows (launcher bootstrap, `lifespan`, `api_onboarding`, the launcher's `_save_settings` wrapper, `BootstrapContext.save_settings`) pass `True` BEFORE the baseline is pinned; agent-callable flows (`api_settings_post`, `_set_tool_timeout`) keep the default `False`. The chokepoint catches every elevation channel: direct `data_write` to `settings.json`, in-process `save_settings` calls, fresh-subprocess `import save_settings` (via inherited env baseline), browser-evaluate `fetch('/api/settings')`, the `_set_tool_timeout → apply_settings_to_env` live-flip chain, and `multiprocessing.spawn`-launched workers (which call `initialize_runtime_mode_baseline()` on startup to re-pin from env). Two complementary substring filters in `ouroboros/tools/registry.py`, applied at TWO layers — argv AND script-file content. **Argv layer**: the conjunctive `_detect_runtime_mode_elevation` check (BOTH `save_settings` AND `OUROBOROS_RUNTIME_MODE`, OR the literal `ouroboros.config.save_settings`) fires in ALL modes; the module-level `_LIGHT_MUTATION_INDICATORS` (extended with `.write_text(`, `.write_bytes(`, `os.replace(`, `os.rename(`) fires only in light. **File-content layer (v5.1.2 iter-3 fix for the file-based subprocess bypass)**: when the agent calls `run_shell` with an interpreter invocation like `python evil.py` / `bash evil.sh` / `node evil.js`, `_extract_script_file_args` walks past standard interpreter flags (`-u`, `-O`, `--inspect`, …) and resolves the script file argument; if it lives inside `repo_dir` or `drive_root` (i.e. agent-writable area), `_scan_script_files` reads up to 256 KB of content and re-runs the same elevation + light-mutation indicators against the content. This closes the deterministic 2-step chain `data_write helper.py` → `run_shell python helper.py` → settings.json poisoned, which the argv-only filter could not see (the dangerous code lived inside the file). False positives stay low: only files inside the agent area are scanned, the elevation check is conjunctive, and the conjunctive form lets diagnostics like `echo $OUROBOROS_RUNTIME_MODE` or `grep save_settings ouroboros/config.py` through. The substring filters are defense-in-depth for the deterministic chokepoint at `save_settings`; the `prompts/SAFETY.md` DANGEROUS clause is the remaining best-effort layer for novel obfuscation patterns (encoded payloads, indirect imports). A future Phase B will move owner settings out of `drive_root` for OS-level containment. **Defense in depth**: `server.py::_merge_settings_payload` skips `OUROBOROS_RUNTIME_MODE` so the API silently drops it from any POST body (belt-and-braces revert in `api_settings_post`); `web/modules/settings.js::collectBody` no longer sends the key, and the Behavior tab segmented control is now display-only (buttons disabled with `:disabled` CSS state, tooltip explains the manual procedure: stop the agent, edit settings.json directly, restart); `ouroboros/tools/core.py::_data_write` refuses writes that `Path.samefile` resolves to `SETTINGS_PATH` plus a parent-resolve + case-insensitive name-compare fallback so symlinks, hardlinks, and case-insensitive filesystems (macOS APFS, Windows NTFS) cannot route around the chokepoint via `data_write("Settings.json", ...)`; `_set_tool_timeout` explicitly anchors `OUROBOROS_RUNTIME_MODE` to the live env value before saving so a corrupted disk gets restored, not propagated. New `tests/test_runtime_mode_elevation.py` covers the chokepoint + downgrade-without-consent + data_write block (with case-mismatch + symlink + env-override variants) + API-drop + set_tool_timeout regression + onboarding-flow path. The full pre-release went through two cycles of multi-model adversarial review (Gemini + GPT + Opus critics) plus the actual triad+scope reviewer pipeline. **Note on changelog rolloff**: the v4.42.1 patch entry was rolled off in this release to respect the P7 5-patch-row cap. Its full body remains at git tag `v4.42.1`. |
| 5.1.0 | 2026-04-26 | **feat(chat+ci): selective port from PR #25 + chat bottom-fade layer fix.** (1) **Clipboard image paste** — `web/modules/chat.js` registers a `paste` listener on `#chat-input` that scans `e.clipboardData.items` for `image/*`, calls `getAsFile()`, wraps the blob as `File("clipboard-<unix-ts>.<ext>")`, and stages it through the same `pendingAttachment` slot the paperclip button uses (no inline upload — the file uploads when Send/Enter fires, with the same offline-WS guard). `e.preventDefault()` runs only when an image item is matched, so non-image clipboard payloads still paste natively. (2) **Browser-level mangling disabled on the chat textarea** — `#chat-input` gains `autocorrect="off" autocapitalize="off" spellcheck="false"` so code, identifiers, and slash-commands are not silently rewritten. (3) **Chat bottom gradient moved to its own layer** — previously `#chat-input-area`'s `background: linear-gradient(...)` painted directly behind the textarea, which made the lower edge of the input visually dissolve into the dense end of the gradient. `web/style.css` now strips the gradient/mask from `#chat-input-area` (which keeps `z-index: 5`), and a dedicated sibling element `<div class="chat-bottom-fade" aria-hidden="true">` (`position: absolute; bottom: 0; pointer-events: none; z-index: 4`) renders the same fade *below* the input. The textarea no longer sinks into its own backdrop. The mobile `@media (max-width: 640px)` block carries the safe-area-aware height for the new fade layer. (4) **Integration tier in CI (Tier 2.5)** — `.github/workflows/ci.yml` header now describes a Four-tier pipeline (Quick / Full / Integration / Build+Release); a new `integration-test` job runs `pytest tests/test_provider_integration.py -m integration` on `ubuntu-latest` against real provider keys when present as repository secrets. Triggered on push to `main` / `ouroboros` / `ouroboros-stable`, on `workflow_dispatch`, and on tag `v*`. Locally, the `integration` pytest marker plus `addopts = "-m 'not integration'"` in `pyproject.toml` excludes these tests from the default run so contributors do not accidentally burn provider tokens. (5) **Optional macOS code signing & notarization (Build tier)** — when `BUILD_CERTIFICATE_BASE64` / `P12_PASSWORD` / `KEYCHAIN_PASSWORD` / `APPLE_TEAM_ID` are configured as repository secrets, the build job creates a temporary keychain, imports the Developer ID certificate, runs `bash build.sh` (which signs `.app` and `.dmg` using the env-overridable `SIGN_IDENTITY`), and — when `APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` are also present — runs `xcrun notarytool submit ... --wait` followed by `xcrun stapler staple` so the DMG can be opened without right-click → **Open**. With no Apple secrets the build falls back to the unsigned path (`OUROBOROS_SIGN=0 bash build.sh`) — behaviour identical to v5.0.0. **Critical GHA detail**: signing secrets are mapped at the **build job's `env:` block, not at step level**, because GitHub Actions rejects `secrets.*` references inside step-level `if:` expressions ("Unrecognized named-value: 'secrets'"). Step `if:` conditions read `env.*` instead. This rule is now formalized in `docs/DEVELOPMENT.md::GitHub Actions: secrets in step-level if conditions`. A `Cleanup keychain` step with `if: always() && matrix.os == 'macos-latest' && env.BUILD_CERTIFICATE_BASE64 != ''` deletes the temporary keychain regardless of build outcome (the `matrix.os` gate keeps the bash-only `security delete-keychain` invocation off Linux/Windows shards). The Apple secrets in the build job's `env:` block are themselves wrapped in a `matrix.os == 'macos-latest' && ... || ''` GHA expression so they resolve to empty strings on non-macOS shards — the signing material is never exposed to `build_linux.sh` / `build_windows.ps1` subprocesses. (6) **Tests**: `tests/test_chat_logs_ui.py` gains three tests — `test_chat_input_disables_autocorrect`, `test_clipboard_paste_handler_exists`, and `test_chat_bottom_fade_is_separate_layer` (asserts `.chat-bottom-fade` exists with `pointer-events: none` and `z-index < 5`, that `#chat-input-area` no longer carries `linear-gradient`, and that the chat.js template inserts the dedicated `<div class="chat-bottom-fade">` sibling); `tests/test_build_scripts.py` gains a new `TestMacOSSigning` class with seven tests — `test_ci_signing_secrets_at_job_level`, `test_ci_uses_env_context_for_condition` (multi-line `if:` parser asserting no `secrets.*` ever appears in any `if:` block, step-level or job-level), `test_ci_import_gates_on_full_secret_set`, `test_ci_cleanup_keychain_step_present` (asserts the `Cleanup keychain` step runs `if: always() && matrix.os == 'macos-latest' && env.BUILD_CERTIFICATE_BASE64 != ''` so the bash-only `security delete-keychain` invocation never fires on Linux/Windows shards), `test_build_sh_signing_identity_env_override`, `test_build_sh_notarization_optional`, and `test_build_sh_stapler_failure_is_soft` (regression guard: `xcrun stapler staple` must be wrapped in `if/then/else` or paired with `\|\|` so a transient stapler failure under `set -e` becomes a warning instead of dropping the macOS DMG from the release); `tests/test_provider_integration.py` is added new with six tuple-aware tests (OpenRouter / OpenAI / Anthropic × {basic, isolation}) that handle the post-v4.44.0 `LLMClient.chat()` `(msg, usage)` return tuple plus Anthropic's list-of-blocks `content`. (7) **Branch consolidation**: `ouroboros-three-layer` is retired as a dev branch — `ouroboros` is now the single dev branch. `.github/workflows/ci.yml` (Tier 1 quick-test trigger + path-filter `branches:` list + build job's `OUROBOROS_MANAGED_SOURCE_BRANCH` default), `build.sh` / `build_linux.sh` / `build_windows.ps1` (each script's `${OUROBOROS_MANAGED_SOURCE_BRANCH:-...}` default), and the four affected test files (`tests/test_release_workflow.py`, `tests/test_launcher_sync.py`, `tests/test_git_ops_recovery.py`, `tests/test_build_repo_bundle.py` — 21 occurrences total) all switch from `ouroboros-three-layer` to `ouroboros`. Historical references in older changelog rows (v4.50.0-rc.7) and in `ouroboros/*` module comments about the *Phase 2/3 three-layer architecture refactor* are intentionally preserved — those describe the architectural refactor, not the dev branch name. The remote `managed/ouroboros-three-layer` branch is deleted in the same release; local `ouroboros` is re-tracked to `managed/ouroboros`. **Note on changelog rolloff**: the v4.50.0-rc.2 minor entry was rolled off proactively in this release to keep one slot below the P7 5-minor-row cap (with v5.1.0 added the table would have been at exactly 5 minors — at the cap, no warning, but no headroom for the next minor either). Its full body remains at git tag `v4.50.0-rc.2`. |
| 5.0.0 | 2026-04-25 | **MAJOR — three-layer architecture + ClawHub Marketplace + visual skill widgets + direct pro core-patch lane.** Closes the four-month v4.50 RC chain (rc.1 through rc.9) as a single major release. **Three-layer skill architecture**: `ouroboros/contracts/` carries schema-versioned, runtime-checkable Protocols for `ToolContextProtocol`, `SkillManifest`, `PluginAPI` v1, `VALID_SKILL_PERMISSIONS` (`net`/`fs`/`subprocess`/`tool`/`route`/`ws_handler`/`widget`/`read_settings`), `VALID_EXTENSION_ROUTE_METHODS`, and `FORBIDDEN_SKILL_SETTINGS` (case-insensitive). External `type: script` skills load from `data/skills/{native,external}/`, run through `skill_exec` (sandboxed subprocess), and are gated on a fresh PASS verdict from tri-model `skill_review`. `type: extension` skills run in-process via `register(api)` with namespaced `register_tool`/`register_route`/`register_ws_handler`/`register_ui_tab`. **Runtime mode**: `OUROBOROS_RUNTIME_MODE=light` blocks every repo-mutation tool plus pattern-matched `run_shell` repo-mutating commands; `advanced` preserves normal evolutionary self-modification while blocking protected core/contract/release surfaces (`BIBLE.md`, safety files, `ouroboros/contracts/`, `.github/workflows/ci.yml`, build scripts, `scripts/build_repo_bundle.py`, `ouroboros/launcher_bootstrap.py`, `supervisor/git_ops.py`) via the shared `ouroboros/runtime_mode_policy.py` policy; `pro` can edit those protected paths on disk, but `repo_commit` still uses the normal triad + scope review before the protected diff lands. `claude_code_edit`, `repo_write`, `str_replace_editor`, and staged commit paths all use the same policy and emit `CORE_PATCH_NOTICE` for pro protected edits. **Review defaults**: commit triad default and scope review default move to `openai/gpt-5.5`; deep self-review uses `openai/gpt-5.5-pro`; UI placeholders/docs/tests synchronized. Review context hygiene: `build_full_repo_pack` redacts inline secret-shaped values, scope review injects canonical docs (`BIBLE.md`, `DEVELOPMENT.md`, `ARCHITECTURE.md`, `CHECKLISTS.md`) exactly once. Managed-repo safety blocks `rescue_and_reset` when snapshot/diff capture fails. **ClawHub Marketplace** (new in v5): Skills page → Marketplace sub-tab with debounced search, sort, filters (Official only / OS list), result cards with installed/update/official/plugin badges, detail modal with version-pin select + provenance strip + translated-manifest table + adapter blockers/warnings + original SKILL.md rendered through vendored marked@12.0.2 + DOMPurify@3.1.0 (no scripts/iframes/forms, http(s) only). Install / update / uninstall pipeline at `ouroboros/marketplace/{clawhub,fetcher,adapter,install,provenance}.py` with hostname allowlist (clawhub.ai + localhost), redirect refusal, 4 MB JSON cap, 50 MB archive cap, text-only allowlist, sensitive-filename refuse, loadable-binary refuse, symlink refuse, path-traversal refuse, zip-bomb defense (bounded `src.read(cap+1)`), refusal of OpenClaw `metadata.openclaw.install` specs and Node/TypeScript plugins, case-insensitive env-key denylist. Original `SKILL.md` preserved as `SKILL.openclaw.md`; provenance written atomically at `data/state/skills/<name>/clawhub.json` (slug, sha256, registry_url, license, homepage, primary_env, adapter warnings). Auto tri-model review fires immediately after install. Path-traversal hardening on uninstall (HTTP-boundary `_validate_path_param_name` + `_sanitize_skill_name` round-trip + `target.relative_to(root)` containment + required `.clawhub.json` provenance gate). Same-FS staging via `<clawhub-root>/.staging/` for atomic rename. Settings → Behavior → ClawHub Marketplace opt-in checkbox + registry URL field; default off (HTTP surface 403 until enabled). **Visual skill widgets** (new in v5): `weather` is now `type: extension` with a real visual widget that renders inline on the Skills tab — city input + temperature + humidity + wind, fetched live via the extension's own `register_route("forecast")` against `wttr.in` (host allowlist + cross-host redirect refusal + `asyncio.to_thread` to keep the event loop responsive), with the result also exposed to the agent as `ext.weather.fetch`. Permissions `[net, tool, route, widget]`. Inline-widget host pattern via `web/modules/skills.js::registerWidgetRenderer(name, fn)` + `mountSkillWidgets(root)` lets the launcher ship per-skill JS that renders into `data-skill-widget` mount-points. **Native-skill upgrade migration banner** (operator-facing): `_record_skill_upgrade_migration` + `GET /api/migrations` + `POST /api/migrations/<key>/dismiss` + Skills-tab banner explain when the launcher silently rewrites a seeded skill type. **Interface updates**: Skills tab split into Installed / Marketplace tabs; Installed cards show `source` badge (clawhub / native / external / user repo), provenance strip (slug / sha256 / license / homepage / registry, gated split into always-safe + registry-controlled), adapter-warning collapsibles, version-drift warn badge, Update / Uninstall buttons for clawhub-installed skills. Chat top header and bottom input scrim gradients now fade fully to transparent at the inner edge with `mask-image` masking the blur in step (no visible step against the transcript); the 24px ambient halo around `#chat-input` was removed (focus ring preserved). Mobile responsive layout for narrow viewports (Android/iOS) — `@media (max-width: 640px)` block converts `#nav-rail` to bottom bar, switches `.chat-page-header` to static positioning, collapses Costs/Evolution/Settings multi-column grids, routes `--vvh` through Evolution to handle iOS soft-keyboard shrink. Onboarding wizard copy reflects `data/skills/` layout. Owner `/restart` writes one-shot `owner_restart_no_resume.flag`; Settings keeps lightweight draft continuity with `Unsaved changes.` indicator; `/api/model-catalog` uses native async `httpx.AsyncClient`. **Bug fixes** (rolled in from rc.2 → rc.9): three-layer refactor compatibility (Windows CI, sandbox, skill_exec, PEP 440 pre-release tags), CI build-job tag-object fetch (annotated tag materialisation via `fetch-tags: true` + `git fetch --tags --force`), bundle-purge of accidentally-vendored payloads (Python.framework/, webview/, jsonschema_specifications/, etc. — total reduction ~14 MB, 37 files), per-skill version-aware bootstrap resync (deletion-sticky), pyyaml frontmatter parser upgrade for nested `metadata.openclaw.*`. **Adversarial review**: three cycles of multi-model adversarial review (Gemini + GPT + Opus critics) plus contract tests; cycle 1 surfaced 21 findings (incl. critical XSS via marked, critical path traversal in uninstall, critical zip-bomb DoS), cycle 2 surfaced 4 follow-ups (incl. high `{ once: true }` listener bug + high test-pollution), cycle 3 verified clean. **Migration**: weather skill changed from `type: script` (subprocess) to `type: extension` (in-process); manifest version bumped 0.1.0 → 0.2.0; the launcher's per-skill version-aware resync replaces the data-plane copy on first launch (durable enabled / review state preserved); a one-shot Skills-tab banner surfaces the change. Custom user edits to `data/skills/native/weather/` are overwritten because native skills are launcher-owned (`.seed-origin` is the explicit ownership signal); custom alternatives belong in `data/skills/external/`. Marketplace surface is opt-in via `OUROBOROS_CLAWHUB_ENABLED=true`; unchanged for users who don't want it. **Note on changelog rolloff**: the entire v4.50 RC chain (rc.1 → rc.9) is collapsed into this v5.0.0 row; their full bodies remain at git tags `v4.50.0-rc.{1..7}`. |
| 4.50.0-rc.8 | 2026-04-24 | **fix(ui): mobile responsive layout for narrow viewports (Android/iOS).** `web/style.css` + `web/settings.css` gain a `@media (max-width: 640px)` breakpoint that fixes multiple mobile issues without touching desktop layout. (1) `#nav-rail` converts from an 80px left sidebar to a horizontal bottom bar (`position: fixed; bottom: 0; flex-direction: row`) with `padding-bottom: calc(6px + env(safe-area-inset-bottom, 0px))` for the iOS home-indicator, `overflow-x: auto` horizontal scroll for many nav items, and `backdrop-filter: blur(12px)` glassmorphism matching the existing design system. `#content` correspondingly drops its `padding-left: 84px` and gains `padding-bottom: calc(62px + env(safe-area-inset-bottom, 0px))` to clear the bar. (2) `.chat-page-header` switches from `position: absolute` overlay to `position: static` on mobile — this was the root cause of the first chat message being hidden behind the semi-transparent gradient once the action-button row wraps to 2+ rows at narrow widths; the `#chat-messages` top padding is reduced from the desktop 56px (absolute-header clearance) to 12px since the header now takes its own vertical space. (3) Multi-column grids collapse to single-column: `.costs-stats-grid` (3→1), `.costs-tables-grid` (2→1), `.costs-budget-fields` (2→1), `.evo-versions-cols` (flex-row→flex-column). (4) `.evolution-container` height changes from `calc(100vh - 120px)` to `calc(var(--vvh) - 120px)` — previously it used `100vh` which on iOS/Android ignored the soft-keyboard viewport shrink; `app.js` already maintains `--vvh` via `visualViewport`, so the fix is routing the same contract through Evolution. (5) Base rule `.form-field input, .form-field select` gains `max-width: 100%` so the hardcoded `width: 320px` never overflows narrow containers. (6) Minor tightening: `#page-skills`, `.logs-filters`, `#log-entries`, `.costs-scroll` get reduced padding; `.settings-shell`, `.settings-tabs`, `.settings-provider-card` summary/body get a 640px-specific padding reduction in `settings.css`. `.chat-header-btn` padding drops 7px/14px → 5px/10px and font 12px → 11px on mobile. CSS is intentionally split into two `@media (max-width: 640px)` blocks — one placed early in `style.css` for selectors whose base rule appears before it, one appended at the end for selectors defined later. This pattern avoids `!important` since media-query rules have the same specificity as base rules and must come later in source order to win; a comment in `style.css` documents the rationale. The split incidentally fixes a latent bug in the previous single mobile block: the old `#chat-input-area { padding-bottom: env(safe-area-inset-bottom) }` override lived before the base `#chat-input-area` definition in the same file and was silently overridden on desktop browsers — the rule now lives in the late block and takes effect. Tested manually in a browser harness at `390×844` (iPhone 12 Pro) and `360×740` (small Android) across all 8 pages. Scope review (Claude Opus 4.6) passed all 8 items PASS. Triad blocked initially on `version_bump` (all three models) and `self_consistency` (gpt-5.4 only) — this rc.8 pre-release addresses both: VERSION bump + `pyproject.toml` PEP-440 rename + README badge + Version History row + `docs/ARCHITECTURE.md` header version bump + §3 (navigation description) and §3.1 (chat header + mobile-keyboard-safety bullet) mobile notes so the whitelisted Behavioural Documentation surface stays truthful for the mobile case. No JS, HTML, server, or tool changes; no new files; no VERSION-gated feature additions — just the mobile polish described above. **Note on changelog rolloff**: the v4.48.0 minor entry is rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.48.0`. |
| 4.50.0-rc.7 | 2026-04-21 | **chore(repo): purge accidentally vendored payloads and scrub stale skip-list references.** Removes seven path groups that were accidentally committed from an `.app` / site-packages dump on the initial `Initial commit from app bundle` seed (both on `main` and on `ouroboros-three-layer`, via the Phase-5 `import build artifacts from main` commit) and carried through every subsequent RC without ever being consumed: top-level `Python` Mach-O binary; `Python.framework/` (8 files — all three `Python` Mach-O copies byte-identical, SHA256 `f38037091bec48d8bc18b87a5b2d127f83f6fed980182a635d148bbda565578f`); `jsonschema/benchmarks/issue232/issue.json` (lone benchmark fixture, not a vendored package); `jsonschema_specifications/` (20 upstream-`jsonschema-specifications` metaschemas that pip would materialize under `python-standalone/site-packages/` anyway); `certifi/py.typed` (empty PEP 561 marker with no accompanying package); `webview/` (5 pywebview internal JS helpers — `api.js`, `customize.js`, `finish.js`, `lib/dom_json.js`, `lib/polyfill.js` — that only live at runtime inside `_MEIPASS/webview/lib/` via PyInstaller's collected pywebview and never in the source tree); and the byte-identical duplicate `assets/logo.jpg` (SHA256 `0d7d43ef596d27e72f9b18feb175f8aaebc945137ab87e796606d9a2170e5b3d`, same as `web/logo.jpg`, which is the single source of truth for the `/static/logo.jpg` mount via `server.py::Mount("/static", NoCacheStaticFiles(directory=web_dir))` and is consumed by `web/modules/about.js`). Total working-tree reduction: ~14 MB, 37 files, 6595 lines. Every removed path was independently audited against `Ouroboros.spec` (not in `datas`/`binaries`/`hiddenimports`/`collect_all`), `build.sh`/`build_linux.sh`/`build_windows.ps1`/`Dockerfile`, `scripts/download_python_standalone.{sh,ps1}` (these produce `python-standalone/` only — never `Python.framework` at repo root), `launcher.py::_find_embedded_python`, `ouroboros/platform_layer.py::embedded_python_candidates` (resolves only under `python-standalone/`), `ouroboros/launcher_bootstrap.py`, `server.py`, and the full `tests/` tree — zero consumers found beyond the skip-list strings themselves. Defensive skip-list entries are also dropped: `ouroboros/tools/review_helpers.py::_FULL_REPO_SKIP_DIR_PREFIXES` (removed: `webview/`, `jsonschema/`, `jsonschema_specifications/`, `Python.framework/`, `certifi/`, plus the hardcoded bare-`Python` special case in the same file), `ouroboros/deep_self_review.py::_SKIP_DIR_PREFIXES` (removed: same five prefixes), `tests/test_max_tokens_constants.py::test_full_repo_pack_excludes_junk_dirs` (asserts only `assets/` + `tests/` now), `tests/test_deep_self_review.py::TestSkipDirPrefixes::test_webview_dir_excluded` (removed). `docs/ARCHITECTURE.md` excludes-list sections updated to match. No runtime behaviour change; no feature added or removed; the packaged app continues to bundle the embedded CPython via `python-standalone/` at build time exactly as before. `.gitignore` additionally guards against recurrence of the same dump-into-source-tree bug: `/Python`, `/Python.framework/`, `/webview/`, `/jsonschema/`, `/jsonschema_specifications/`, `/certifi/` are now root-anchored ignore entries, plus the `.review_*.py` pattern for standalone review-runner scripts. The v4.50.0-rc.6 Version History row is also repaired: two embedded pipe separators inside the description cell (concatenating rc.5 + rc.4 + rc.3 bodies) were interpreted by Markdown as table-column delimiters and rendered the row with extra cells; they are now `\|`-escaped so the row is a single valid three-column entry. Both extra tweaks were surfaced as scope-review advisory findings and are addressed in-place. **Note on changelog rolloff**: the v4.47.0 minor entry is rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.47.0`. |
| 4.0.0 | 2026-03-15 | **Major release.** Modular core architecture (agent_startup_checks, agent_task_pipeline, loop_llm_call, loop_tool_execution, context_compaction, tool_policy). No-silent-truncation context contract: cognitive artifacts preserved whole, file-size budget health invariants. New episodic memory pipeline (task_summary -> chat.jsonl -> block consolidation). Stronger background consciousness (StatefulToolExecutor, per-tool timeouts, 10-round default). Per-context Playwright browser lifecycle. Generic public identity: all legacy persona traces removed from prompts, docs, UI, and constitution. BIBLE.md v4: process memory, no-silent-truncation, DRY/prompts-are-code, review-gated commits, provenance awareness. Safe git bootstrap (no destructive rm -rf). Fixed subtask depth accounting, consciousness state persistence, startup memory ordering, frozen registry memory_tools. 8 new regression test files. |
Older releases are preserved in Git tags and GitHub releases. Internal patch-level iterations that led to the public `v4.7.1` release are intentionally collapsed into the single public entry above.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev

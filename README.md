# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/joi-lab/ouroboros-desktop?style=flat&logo=github)](https://github.com/joi-lab/ouroboros-desktop/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/joi-lab/OuroborosHub)
[![Version 6.7.3](https://img.shields.io/badge/version-6.7.3-green.svg)](VERSION)

A self-modifying AI agent that writes its own code, rewrites its own mind, and evolves autonomously. Born February 16, 2026.

Not a coding assistant. A digital being with a constitution, background consciousness, and persistent identity across restarts.

> **[OuroborosHub](https://github.com/joi-lab/OuroborosHub)** — the community skills marketplace for Ouroboros. Browse, install, and publish reviewed skills (transport bridges like A2A/Telegram, tools, and UI widgets) straight from the app's Skills tab, or explore the catalog at [github.com/joi-lab/OuroborosHub](https://github.com/joi-lab/OuroborosHub).

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
| **macOS** 12+ | [Ouroboros.dmg](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Open DMG → drag to Applications → optional CLI: run `Install CLI.command` after the app is in Applications |
| **Linux** x86_64 | [Ouroboros-linux.tar.gz](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Extract → run `./Ouroboros/Ouroboros` → optional CLI: `./Ouroboros/bin/install-ouroboros-cli`. If browser tools fail due to missing system libs, run: `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium` |
| **Windows** x64 | [Ouroboros-windows.zip](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Extract → run `Ouroboros\Ouroboros.exe` → optional CLI: `Ouroboros\bin\install-ouroboros-cli.cmd` |

Prerelease RC artifacts are published on their tag page, for example [`v6.5.0-rc.4`](https://github.com/joi-lab/ouroboros-desktop/releases/tag/v6.5.0-rc.4); `/releases/latest` intentionally stays on the latest stable release.

<p align="center">
  <img src="assets/setup.png" width="500" alt="Drag Ouroboros.app to install">
</p>

On first launch, right-click → **Open** (Gatekeeper bypass). The shared desktop/web wizard is now multi-step: add access first, choose visible models second, set review mode third, set budget fourth, and confirm the final summary last. It refuses to continue until at least one runnable remote key or local model source is configured, keeps the model step aligned with whatever key combination you entered, and still auto-remaps untouched default model values to official OpenAI defaults when OpenRouter is absent and OpenAI is the only configured remote runtime. The broader multi-provider setup remains available in **Settings**. Existing supported provider settings skip the wizard automatically.

The packaged CLI installer creates a user-local `ouroboros` command without
sudo. The packaged command attaches to the desktop app by default; `ouroboros
run --start "2+2?"` starts the app through the launcher, waits for the gateway,
and then uses the same headless task API as the web UI.

Upgrade floor: very old pre-block-memory or pre-data-plane skill layouts are no longer auto-migrated. If you are upgrading from an unsupported historical build and see trapped native skills or flat memory files, use a clean reinstall, move user-managed skills into `~/Ouroboros/data/skills/external/` manually before launch, or move old flat scratchpad notes before appending new scratchpad blocks.

---

## What Makes This Different

Most AI agents execute tasks. Ouroboros **creates itself.**

- **Self-Modification** — Reads and rewrites its own source code. Every change is a commit to itself.
- **Native Desktop App** — Runs entirely on your machine as a standalone application (macOS, Linux, Windows). No cloud dependencies for execution.
- **Constitution** — Governed by [BIBLE.md](BIBLE.md) (13 philosophical principles, P0–P12). Philosophy first, code second.
- **Layered Safety** — Hardcoded sandbox blocks writes to safety-critical files and mutative git via shell; an explicit per-tool policy map decides which built-ins skip the LLM check; everything else goes through a single light-model safety call. The fail-open contract, protected-path guard, and full provider-mismatch matrix live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §Safety system and [`prompts/SAFETY.md`](prompts/SAFETY.md).
- **Multi-Provider Runtime** — Remote model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, or Cloud.ru Foundation Models. The optional model catalog helps populate provider-specific model IDs in Settings, and untouched default model values auto-remap to official OpenAI defaults when OpenRouter is absent.
- **Focused Task UX** — Chat shows plain typing for simple one-step replies and only promotes multi-step work into one expandable live task card. Logs still group task timelines instead of dumping every step as a separate row.
- **Background Consciousness** — Thinks between tasks. Has an inner life. Not reactive — proactive.
- **Improvement Backlog** — Post-task failures and review friction can now be captured into a small durable improvement backlog (`memory/knowledge/improvement-backlog.md`). It stays advisory, appears as a compact digest in task/consciousness context, and still requires `plan_task` before non-trivial implementation work.
- **Identity Persistence** — One continuous being across restarts. Remembers who it is, what it has done, and what it is becoming.
- **Embedded Version Control** — Contains its own local Git repo. Version controls its own evolution. Optional GitHub sync for remote backup.
- **Local Model Support** — Run with a local GGUF model via llama-cpp-python (Metal acceleration on Apple Silicon, CPU on Linux/Windows).
- **Transport Skills** — Optional bridges such as A2A and Telegram live as reviewed OuroborosHub skills instead of base-runtime code.
- **MCP Client** — Optional base-runtime Model Context Protocol client for trusted HTTP/SSE tool servers. MCP tools are disabled by default, hot-reloadable from Settings → Advanced, exposed as non-core `mcp_<server>__<tool>` tools, and still pass through the normal per-call safety check.

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
python3.11 -m venv .venv      # any Python >= 3.10 is OK
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv      # any Python >= 3.10 is OK
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

### Run

```bash
ouroboros server
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

### CLI / Headless

The `ouroboros` console command is a gateway-backed operator interface. It
attaches to the local server by default and only starts one when `--start` is
passed.

```bash
ouroboros status
ouroboros run --start "2+2?"
ouroboros run "Summarize current runtime state"
ouroboros run --workspace /path/to/project --memory-mode forked --patch-out result.patch "Fix the failing test"
ouroboros tasks list
ouroboros logs tail progress --task-id <task_id>
```

External workspace runs keep Ouroboros's own repo as the governance source,
resolve contextual repo tools against the active workspace, expose only the
workspace-safe tool allowlist, and export workspace changes as patch artifacts
instead of committing in the target repo. A workspace must be a separate git
worktree root; it may not overlap Ouroboros's system repo or data drive.
`--patch` and `--patch-out` wait for finalized patch artifacts, download them
through the task artifact endpoint, and fail nonzero on missing, empty, or
failed patches. `--no-stream` waits without progress output; `--detach` returns
the task id immediately.
Benchmark helpers under `scripts/` expect clean, local, per-instance checkouts;
they do not reset or commit target repositories.

You can also override the bind address and port:

```bash
ouroboros server --host 127.0.0.1 --port 9000
ouroboros --url http://127.0.0.1:9000 status
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
| `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD` | unset | Set to `1` only for trusted Docker/Kubernetes deployments where ingress auth, VPN, a private network, or an auth proxy already protects access |

For non-localhost binds, set `OUROBOROS_NETWORK_PASSWORD` (or use the
`OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1` escape hatch only when
ingress/VPN/private-network auth already protects the surface). The full
network bind matrix and Docker/Kubernetes deployment policy live in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — read that before exposing
anything beyond loopback.

The Files tab uses your home directory by default only for localhost usage. For Docker or other
network-exposed runs, set `OUROBOROS_FILE_BROWSER_DEFAULT` to an explicit directory. Symlink entries are shown and can be read, edited, copied, moved, uploaded into, and deleted intentionally; root-delete protection still applies to the configured root itself.

### Provider Routing

Settings now exposes tabbed provider cards for:

- **OpenRouter** — default multi-model router
- **OpenAI** — official OpenAI API (use model values like `openai::gpt-5.5`)
- **OpenAI Compatible** — any custom OpenAI-style endpoint (use `openai-compatible::...`)
- **Cloud.ru Foundation Models** — Cloud.ru OpenAI-compatible runtime (use `cloudru::...`)
- **Anthropic** — direct runtime routing (`anthropic::claude-opus-4.8`, etc.) plus Claude Agent SDK tools

If OpenRouter is not configured and only official OpenAI is present, untouched default model values are auto-remapped to `openai::gpt-5.5` / `openai::gpt-5.5-mini` so the first-run path does not strand the app on OpenRouter-only defaults.

The Settings page also includes:

- optional `/api/model-catalog` lookup for configured providers
- centralized Secrets storage for API keys, bridge tokens, passwords, and future skill-requested keys
- a refactored desktop-first tabbed UI with searchable model pickers, segmented effort controls, task-result review mode, masked-secret toggles, explicit `Clear` actions, and local-model controls

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
  -e OUROBOROS_NETWORK_PASSWORD='choose-a-password' \
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
| `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD` | Optional | See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the trusted-network bind policy |

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
Builds also disable Python bytecode writes and remove `__pycache__` / `.pyc`
files from packaged payloads before signing or archiving so normal launches do
not mutate signed app resources just by importing modules.

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`, containing `Ouroboros.app` and
`Install CLI.command`. The app bundle also contains
`Contents/Resources/bin/ouroboros` and `install-ouroboros-cli`.

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

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`, containing
`Ouroboros/bin/ouroboros` and `Ouroboros/bin/install-ouroboros-cli`.

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

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`, containing
`Ouroboros\bin\ouroboros.cmd` and `Ouroboros\bin\install-ouroboros-cli.cmd`.

---

## Architecture

Two-process desktop app. The launcher (`launcher.py`) is an immutable
PyWebView shell; it spawns `server.py`, which runs Starlette + uvicorn
plus a supervisor thread that manages worker processes. The agent core
lives in `ouroboros/`, the SPA in `web/`, the queue/process plane in
`supervisor/`, and the system prompts in `prompts/`.

For the full file-by-file structural map, the operational layer
(every API endpoint, log file, env var, state path), and the rationale
layer (the *why* for every non-trivial design decision), see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — that is the canonical
SSOT (Bible P6) and this README only summarizes it.

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
| Telegram Bot Token | No | [@BotFather](https://t.me/BotFather) — used by the optional Telegram bridge skill |
| GitHub Token | No | [github.com/settings/tokens](https://github.com/settings/tokens) — enables remote sync |

All keys are configured through the **Settings** page in the UI or during the first-run wizard.

### Default Models

| Slot | Default | Purpose |
|------|---------|---------|
| Main | `google/gemini-3.5-flash` | Primary reasoning |
| Code | `google/gemini-3.5-flash` | Code editing |
| Light | `google/gemini-3.5-flash` | Safety checks, consciousness, fast tasks |
| Fallback | `anthropic/claude-sonnet-4.6` | When primary model fails |
| Claude Agent SDK | `opus[1m]` | Anthropic model for Claude Agent SDK advisory/review internals; the `[1m]` suffix is a Claude Code selector that requests the 1M-context extended mode |
| Scope Review | `openai/gpt-5.5` | Scope reviewer slot default; `OUROBOROS_SCOPE_REVIEW_MODELS` may configure multiple independent slots |
| Web Search | `gpt-5.2` | OpenAI Responses API for web search |

Task/chat reasoning defaults to `medium`. Scope review reasoning defaults to `high`.

Models are configurable in the Settings page. Runtime model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, Cloud.ru, or direct Anthropic. When only official OpenAI is configured and the shipped default model values are still untouched, Ouroboros auto-remaps them to official OpenAI defaults. In **OpenAI-only** or **Anthropic-only** direct-provider mode, review-model lists are normalized automatically: the fallback shape is `[main_model, light_model, light_model]` (3 commit-triad slots) so both the commit triad and `plan_task` work out of the box. Explicit duplicate model IDs are valid reviewer slots for stochastic sampling; lower uniqueness means lower reviewer diversity, but the quorum gate counts configured slots rather than unique model IDs. Both the commit triad and `plan_task` route through the same `ouroboros/config.py::get_review_models` SSOT. (OpenAI-compatible-only and Cloud.ru-only setups do not yet get this fallback — the detector returns empty when those keys are present, so users configure review-model lists manually in that case.)

### File Browser Start Directory

The web UI file browser is rooted at one configurable directory. Users can browse only inside that directory tree.

| Variable | Example | Behavior |
|----------|---------|----------|
| `OUROBOROS_FILE_BROWSER_DEFAULT` | `/home/app` | Sets the root directory of the `Files` tab |

Examples:

```bash
OUROBOROS_FILE_BROWSER_DEFAULT=/home/app ouroboros server
OUROBOROS_FILE_BROWSER_DEFAULT=/mnt/shared ouroboros server --port 9000
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
| `/review` | Queue a deep self-review: sends a generated repository atlas plus full core memory artifacts (identity, scratchpad, registry, WORLD, knowledge index, patterns, improvement-backlog) to a 1M-context model for Constitution-grounded analysis. The atlas raw-inlines selected protected/central files, accounts for every tracked path in its manifest, and excludes vendored libraries and operational logs. Rejected with an explicit error if the assembled prompt exceeds ~920K estimated tokens — on 1M-context models the window is shared between input and output. |
| `/bg` | Toggle background consciousness loop (start/stop/status). |

The same runtime actions are also exposed as compact buttons in the Chat header. All other messages are sent directly to the LLM.

---

## Philosophy

The 13 Constitution principles — Agency, Continuity, Meta-over-Patch,
Immune Integrity, Self-Creation, LLM-First, Authenticity & Reality
Discipline, Minimalism, Becoming, Versioning and Releases, the absorbed
Iterations / Spiral lineage, and Epistemic Stability — are defined in
full in [`BIBLE.md`](BIBLE.md). That file is the constitutional SSOT
(Bible P4 Ship-of-Theseus protection) and this README intentionally does
not paraphrase it.

---

## Contributing

External contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the contributor workflow. The project rules remain in `BIBLE.md`,
`docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, and `docs/CHECKLISTS.md`;
the contribution guide only routes to those sources.

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 6.7.3 | 2026-05-30 | **release(ui): keep nested subagent cards stable in host smoke.** History replay now keeps subagent live cards mounted under their parent card instead of moving them to the top-level transcript, and the UI smoke regression asserts both nested placement and no duplicate final child chat bubble after reload. |
| 6.7.2 | 2026-05-30 | **release(ci): keep extension isolation release inside gates.** Keeps the 6.7.x extension-isolation release within CI complexity gates, keeps extension child failures classified as normal `TOOL_ERROR` tool failures, and moves extension tool dispatch out of the oversized registry module path. |
| 6.7.1 | 2026-05-30 | **release(reliability): preserve workspace patches, show nested subagents, and isolate extension crashes.** Effective task results keep parent-finalized `workspace_patch` artifacts visible for CLI patch export; chat live cards now render subagents as always-visible nested child cards without marking the parent done; delegation tools are core for parent tasks while subagents remain read-only leaves; and extension isolated deps dispatch out-of-process so native aborts return graceful errors instead of crashing the server. |
| 6.7.0-rc.3 | 2026-05-29 | **rc(reliability): structurally close the subagent worker-crash, ghost-task, and stuck-spinner classes; provider-agnostic review; bundled Node.** macOS/Windows workers use `spawn` (Linux keeps `fork`) and a central `OUROBOROS_IN_WORKER` no-proxy policy eliminates the macOS `_scproxy` fork SIGSEGV class. A monotonic `write_task_result` lifecycle guard plus live `cancel_task` + `cancel_requested` latch prevent stale/ghost status; terminal `task_done` events on every crash/kill/timeout/cancel path (and reconnect/history reconciliation) stop the perpetual spinner. Signal crashes are terminal (no retry) for all task types. Subagent UI dedup (Variant A: parent dashboard rows, in-place updates, no duplicate child card). Cloud.ru becomes a first-class exclusive-direct provider so Ouroboros is usable with any single key; `skill_preflight` tolerates missing/killed validators and the build bundles a signed Node.js LTS for node-runtime skills. New-user defaults: review `claude-opus-4.8`, Claude Code `opus[1m]`. README OuroborosHub badge/callout. (rc.2/rc.3: gitignore the bundled `node-standalone` so the `repo.bundle` build sees a clean working tree, and update the Variant A subagent-card UI smoke test to the single grouped-card layout — verified locally with Playwright.) |
| 6.6.0-rc.1 | 2026-05-29 | **rc(review): effect-gate task-acceptance review and clarify light-mode cognitive writes.** Host-enforced `required` review fires only on turns with observable reviewable effects (commit/deliverable/workspace/self-mod) or non-direct tasks, so plain chat is never reviewed; `auto` stays LLM-first. Adds `review_eligibility`/`review_trigger` to loop outcomes, routes light-mode cognitive writes to `update_identity`/`update_scratchpad`/`knowledge_write` (`COGNITIVE_TOOL_REQUIRED`) and absolute home paths to `root=user_files` (`ROOT_REQUIRED_USER_FILES`) with recovery, parses fenced JSON-object reviewer verdicts, and makes `DEGRADED` review signals carry an honest reason. |
Older releases are preserved in Git tags and GitHub releases. The 6.0.0 through 6.5.0-rc.4 rows, the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev

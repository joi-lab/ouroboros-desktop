# DEVELOPMENT.md — Development Principles & Module Guide

## What This File Is

This is Ouroboros's **engineering handbook** — the bridge between philosophy (BIBLE.md) and architecture (ARCHITECTURE.md).

**BIBLE.md** answers *why* and *what matters*.
**ARCHITECTURE.md** describes *what exists right now*.
**DEVELOPMENT.md** answers *how to build* — the concrete principles, patterns, and checklists for writing, modifying, and reviewing code in this project.

## Scope

- **Code style & structure:** naming, file layout, module boundaries, error handling patterns.
- **Module lifecycle:** how to create a new module, what it must include, how it integrates.
- **Review & commit protocol:** what happens before code lands — gates, checks, invariants.
- **Testing standards:** what gets tested, how, minimum expectations.
- **Prompt engineering:** standards for writing and modifying LLM prompts (SYSTEM.md, CONSCIOUSNESS.md, etc.).
- **Integration patterns:** how modules communicate, data flows, shared state.

## What It Is NOT

- Not philosophy — that's BIBLE.md.
- Not an architecture map — that's ARCHITECTURE.md.
- Not a changelog — that's README.md + git log.
- Not aspirational — every rule here must reflect current practice or an immediately enforced standard.

## Relationship to Other Documents

```
BIBLE.md (soul — principles, constraints, identity)
    ↓ informs
DEVELOPMENT.md (hands — how to build, concretely)
    ↓ produces
ARCHITECTURE.md (mirror — what currently exists)
```

Rules in this file must not contradict BIBLE.md.

---

## Naming Convention

### General Rules

- **Language:** All code identifiers, comments, docstrings, and commit messages are in English.
- **Style:** Python PEP 8. Modules and variables — `snake_case`. Classes — `PascalCase`. Constants — `UPPER_SNAKE_CASE`.
- **Self-explanatory names** over abbreviations. A name should tell you what the thing *does*, not just what it *is*. Derived from P4 (Authenticity).

### Entity Types

| Entity Type | Purpose | Naming Pattern | Contains Business Logic? | Example |
|-------------|---------|----------------|--------------------------|---------|
| **Gateway** | Thin adapter to an external API. Wraps third-party SDK/HTTP calls into clean Python functions. | `{Platform}Gateway` | No. Pure I/O — translate calls in, translate responses out. | `BrowserGateway` |
| **Service** | Orchestrates a domain concern. May use one or more Gateways, manage state, apply business rules. | `{Domain}Service` | Yes. Coordinates, decides, transforms. | — |
| **Tool** | An LLM-callable function exposed to the agent. Thin wrapper that connects the agent to a Gateway or Service. | `{verb}_{noun}` (snake_case function) | Minimal. Validates input, calls Gateway/Service, formats output. | `repo_read`, `browse_page`, `web_search` |

### Gateway Rules (recommended pattern, not enforced)

When adding a new external API integration, the recommended pattern is a **Gateway** class that isolates transport from business logic. The `ouroboros/gateways/` directory houses external API adapters. As the codebase grows, extract Gateways as needed.

When a Gateway exists, it should follow these guidelines:
- No business logic: no routing, no decisions. Just transport.
- Input/output: takes Python primitives, returns Python primitives.
- Error handling: translates platform-specific errors into consistent return values.
- Stateless where possible.

**Existing Gateways:**
- `ouroboros/gateways/claude_code.py` — Claude Agent SDK gateway. Two paths: `run_edit`
  (edit mode with PreToolUse safety hooks) and `run_readonly` (advisory review, no
  mutating tools). Structured `ClaudeCodeResult` output.

### Relationship Between Entities

```
LLM Agent
    |  calls
Tool (repo_read, web_search, browse_page)
    |  delegates to
Gateway or direct implementation
    |  calls
External API / filesystem / subprocess
```

Not every layer is required for every operation. Simple cases (e.g., `repo_read`) go Tool → filesystem directly.

---

## Module Size & Complexity

Derived from P5 (Minimalism): entire codebase fits in one context window.

- Module target: ~1000 lines. Crossing that line is P5 pressure and should trigger extraction or an explicit justification.
- Module hard gate: 1250 lines for non-grandfathered modules in `tests/test_smoke.py`.
- Method target: <150 lines. Crossing that line is a decomposition signal, not an automatic failure by itself.
- Method hard gate: 250 lines in `tests/test_smoke.py`.
- Codebase-wide function-count hard gate: 1100 Python functions/methods in `tests/test_smoke.py`.
- Function parameters: <8.
- Net complexity growth per cycle approaches zero.
- If a feature is not used in the current cycle — it is premature.

---

## Review & Commit Protocol

Reviewed commits now have an explicit **two-step gate**:

1. **Advisory freshness gate**: finish all edits, then run `advisory_pre_review`.
   `repo_commit` / `repo_write_commit` require a fresh matching advisory run (or an
   explicit audited bypass) and no open obligations from earlier blocked rounds.
   Any edit after advisory makes it stale and requires a re-run.
2. **Unified pre-commit review**: once advisory is fresh, the reviewed commit path
   runs two reviewers in parallel on the exact staged snapshot:
   - **Triad review** (`ouroboros/tools/review.py`): three models review the staged
     diff against `docs/CHECKLISTS.md`.
   - **Scope review** (`ouroboros/tools/scope_review.py`): one model reviews
     completeness and cross-module consistency with full-repo context
     (`build_full_repo_pack`).

Both blocking reviewers always run concurrently via `concurrent.futures.ThreadPoolExecutor`
(orchestrated in `ouroboros/tools/parallel_review.py`). The caller receives one
combined verdict with all findings in a single round. Scope review still runs even
when triad blocks, **except** when the fully assembled scope-review prompt exceeds
the model context budget (`_SCOPE_BUDGET_TOKEN_LIMIT`), in which case scope review
is skipped with a non-blocking advisory warning. `docs/CHECKLISTS.md` remains the
single source of truth for review items; do not duplicate or fork checklist policy here.

Preferred workflow for multi-file changes: `repo_write` all files first, then
`advisory_pre_review`, then `repo_commit` immediately on the final diff.

The full pre-commit review checklists live in **`docs/CHECKLISTS.md`** —
the single source of truth (Bible P5: DRY).

This section defines what "DEVELOPMENT.md compliance" means in practice — it is the
detailed expansion of the `development_compliance` item in `docs/CHECKLISTS.md`.

### DEVELOPMENT.md Compliance Checklist

Before every commit, verify the following:

#### Naming Conventions
- [ ] Modules and variables use `snake_case`
- [ ] Classes use `PascalCase`
- [ ] Constants use `UPPER_SNAKE_CASE`
- [ ] Names are self-explanatory

#### Entity Type Rules
- [ ] **Gateway** (if present): contains ONLY transport. No business logic, no routing.
- [ ] **Tool** (`{verb}_{noun}`): thin LLM-callable wrapper. Validates input, formats output.

#### Module Size & Complexity
- [ ] Module stays near one context window (~1000 lines target; 1250 hard gate unless explicitly grandfathered debt)
- [ ] No method exceeds the practical target (150 lines) or the hard gate (250 lines)
- [ ] Total Python function count stays under the current smoke hard gate (1100)
- [ ] No function has more than 8 parameters
- [ ] No gratuitous abstract layers (Bible P5)

#### Structural Rules
- [ ] New Tool? `get_tools()` exports it using the `ToolEntry` pattern from `registry.py`.
- [ ] New Gateway (if extracted)? Contains no business logic, only transport.
- [ ] New memory/data files? Should they appear in LLM context (`context.py`)?

#### LLM Call Rules
- [ ] New LLM calls go through the shared `LLMClient` / `llm.py` layer — no ad-hoc HTTP clients or direct provider SDKs outside that layer.

#### Cognitive Artifact Integrity
- [ ] Cognitive artifacts (identity.md, scratchpad, task reflections, review outputs, pattern register) must NOT use hardcoded `[:N]` truncation. If content must be shortened, include an explicit omission note (e.g. `⚠️ OMISSION NOTE: truncated at N chars`).

---

*This section is the authoritative definition of "DEVELOPMENT.md compliance" referenced in the `development_compliance` item in `docs/CHECKLISTS.md`.*

---

## Platform Abstraction Rule

All platform-specific code **MUST** go through `ouroboros/platform_layer.py`.

### What counts as platform-specific

- Direct use of: `os.kill`, `os.setsid`, `os.killpg`, `os.getpgid`, `signal.SIGKILL`, `signal.SIGTERM`
- Unix-only modules: `fcntl`, `resource`, `grp`, `pwd`
- Windows-only modules: `msvcrt`, `winreg`, `ctypes.windll`
- `subprocess` with platform-conditional flags: `start_new_session`, `creationflags`
- Hardcoded path separators (`/` or `\\`) in filesystem logic (use `pathlib` instead)

### Rules

1. **All platform-specific calls live in `platform_layer.py`** — the rest of the codebase imports cross-platform wrappers from there.
2. **Platform-specific modules are imported inside `platform_layer.py` only**, guarded by `IS_WINDOWS` / `IS_MACOS` / `IS_LINUX` checks.
3. **No top-level imports of Unix-only or Windows-only modules** outside `platform_layer.py`. If you need `fcntl` — you're in the wrong file.
4. **Use `pathlib.Path`** for filesystem paths. Never construct paths with string concatenation using `/` or `\\`.

### Enforcement

- **AST-based test** (`tests/test_platform_guard.py`): scans `.py` files under `ouroboros/`, `supervisor/`, and `server.py` for:
  - Top-level imports of platform-specific modules (`fcntl`, `msvcrt`, `winreg`, `resource`)
  - Direct `os.kill`, `os.killpg`, `os.setsid`, `os.getpgid` attribute access
  - Direct `signal.SIGKILL`, `signal.SIGTERM` attribute access
  
  Not scanned by the AST guard: `launcher.py` (immutable outer shell, intentionally excluded) and subprocess flag patterns (`creationflags`, `start_new_session`). For subprocess isolation, use `subprocess_new_group_kwargs()` and `subprocess_hidden_kwargs()` from `platform_layer.py` — enforced by code review and the `cross_platform` checklist item.
- **Pre-commit review**: checklist item `cross_platform` (#14) catches violations during code review.
- **CI matrix**: tests run on Ubuntu, Windows, and macOS to catch runtime failures.

### Adding new platform-specific code

1. Add the cross-platform wrapper to `platform_layer.py`.
2. Import and use the wrapper in callers.
3. Add platform-conditional tests if behavior differs across OSes.

---

## Design System

Ouroboros uses **glassmorphism** as its visual language. All interactive surfaces follow this pattern:

```css
background: rgba(26, 21, 32, 0.62–0.88);
backdrop-filter: blur(8–16px);
border: 1px solid rgba(255, 255, 255, 0.06–0.12);
```

### Accent colors

| Role | Value | Usage |
|------|-------|-------|
| Primary | `rgba(201, 53, 69, ...)` = `#c93545` | Nav buttons, chat cards, borders |
| Hover/focus | `rgba(232, 93, 111, ...)` = `#e85d6f` | Focus glow, settings hover |

Use the primary accent for new features. Avoid introducing additional red/crimson shades.

### Border radius scale

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-xs` | `3px` | Micro accents (progress bars) |
| `--radius-sm` | `8px` | Small controls, filter chips |
| `--radius` | `12px` | Inputs, inner cards |
| `--radius-lg` | `16px` | Nav buttons, chat/live cards |
| `--radius-xl` | `20px` | Logo images, large media |
| *(no token)* | `18px` | Section cards (settings, form panels) |
| *(no token)* | `24px` | Modal/wizard shells, chat input |

Use CSS variables where possible. Do not introduce new hardcoded radius values.
When a new radius value is needed, add it to `:root` in `web/style.css` first.

### Interactive states

```css
hover:  transform: scale(1.02–1.04) + border-color +1 step brightness
active: background rgba(201,53,69, 0.12) + crimson glow
focus:  border-color rgba(232,93,111,0.4) + box-shadow 0 0 0 3px rgba(201,53,69,0.10)
```

### "Working" phase color

Use **crimson** (`rgba(248, 130, 140, ...)`) for active/working states everywhere — not blue.
The Logs page phase badges now match Chat live card colors.

### No inline styles in JS

JS modules that generate HTML must use CSS class names, not `style=""` attributes.
Existing classes (`.stat-card`, `.page-header`, `.about-*`, `.costs-*`) cover common layouts.
Add new classes to `web/style.css` when needed.

"""Tool capability sets — single source of truth.

Every hardcoded set that classifies tool behavior (core visibility,
parallelism, truncation policy, result size limits) lives HERE and
only here.  Other modules import from this file.

Why this exists: before the tool-capabilities consolidation, CORE_TOOL_NAMES was duplicated between
``ouroboros/tools/registry.py`` and ``ouroboros/tool_policy.py``, while
``READ_ONLY_PARALLEL_TOOLS``, ``_UNTRUNCATED_TOOL_RESULTS``, and
``_UNTRUNCATED_REPO_READ_PATHS`` were hardcoded in
``ouroboros/loop_tool_execution.py``.  Drift between the copies was a
recurring source of subtle bugs.

``ouroboros/tools/registry.py`` imports ``CORE_TOOL_NAMES`` from here for
``schemas(core_only=True)`` so registry visibility, tool policy, and loop
execution share one canonical set.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Core tools — available from round 1 without enable_tools
# ---------------------------------------------------------------------------

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    # File I/O
    "repo_read", "repo_list", "repo_write", "repo_commit",
    "str_replace_editor",
    "data_read", "data_list", "data_write",
    # Code search
    "code_search",
    # Shell / CLI
    "run_shell", "claude_code_edit",
    # Git
    "git_status", "git_diff",
    "restore_to_head", "revert_commit",
    "pull_from_remote", "rollback_to_target",
    # Task decomposition (non-core: use enable_tools("schedule_task") to activate)
    # schedule_task, wait_for_task, get_task_result are available but not auto-loaded
    # Memory / identity
    "update_scratchpad", "update_identity",
    "chat_history", "recent_tasks",
    # Knowledge base
    "knowledge_read", "knowledge_write", "knowledge_list",
    # Web
    "web_search",
    "browse_page", "browser_action", "analyze_screenshot",
    # Communication
    "send_user_message", "send_photo",
    # Control
    "switch_model",
    "request_restart", "promote_to_stable",
    # Advisory pre-review gate
    "advisory_pre_review", "review_status",
    # v5.7.0: ``review_skill`` and ``skill_preflight`` are the heal-mode
    # skill repair lane. They used to be discoverable only via
    # ``enable_tools`` which made heal prompts impossible to satisfy:
    # heal blocks ``enable_tools`` so the agent could never reach the very
    # tools the prompt asked it to call. Promoting both to core makes the
    # everyday "fix this skill" workflow a one-shot — no enable round-trip.
    "list_skills", "review_skill", "skill_preflight",
    "submit_skill_to_hub",
})

# Meta-tools: always visible alongside core tools
META_TOOL_NAMES: frozenset[str] = frozenset({
    "list_available_tools", "enable_tools",
})

# ---------------------------------------------------------------------------
# Read-only parallel-safe tools — can run concurrently in a ThreadPool
# ---------------------------------------------------------------------------

READ_ONLY_PARALLEL_TOOLS: frozenset[str] = frozenset({
    "repo_read", "repo_list",
    "data_read", "data_list",
    "code_search", "recent_tasks",
    "web_search", "codebase_digest", "chat_history",
})

# ---------------------------------------------------------------------------
# Stateful browser tools — require thread-sticky executor
# ---------------------------------------------------------------------------

STATEFUL_BROWSER_TOOLS: frozenset[str] = frozenset({
    "browse_page", "browser_action",
})

# ---------------------------------------------------------------------------
# Tool result truncation policy
# ---------------------------------------------------------------------------

# Tools whose results must NEVER be truncated (full output is semantically
# important — commit review verdicts, advisory findings, etc.)
UNTRUNCATED_TOOL_RESULTS: frozenset[str] = frozenset({
    "repo_commit",
    "multi_model_review",
    "advisory_pre_review",
    "review_skill",
    "review_status",
})

# repo_read paths that must NEVER be truncated (cognitive artifacts)
UNTRUNCATED_REPO_READ_PATHS: frozenset[str] = frozenset({
    "BIBLE.md",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/CHECKLISTS.md",
    "docs/DEVELOPMENT.md",
})

# Per-tool result size caps (chars). Tools not listed use DEFAULT_TOOL_RESULT_LIMIT.
TOOL_RESULT_LIMITS: dict[str, int] = {
    "repo_read": 80_000,
    "data_read": 80_000,
    "recent_tasks": 80_000,
    "knowledge_read": 80_000,
    "run_shell": 80_000,
    "code_search": 80_000,
    # v5.7.0: ``skill_exec`` returns a JSON wrapper with stdout (256KB cap)
    # and stderr (128KB cap) plus a small metadata header. With the bumped
    # caps the wrapped result can reach ~300KB; without an explicit per-
    # tool limit it would silently fall back to ``DEFAULT_TOOL_RESULT_LIMIT``
    # (15KB) and the agent would only see the first ~5% of the output.
    "skill_exec": 300_000,
}

DEFAULT_TOOL_RESULT_LIMIT: int = 15_000

# ---------------------------------------------------------------------------
# Reviewed mutative tools — special timeout handling
# ---------------------------------------------------------------------------

# Tools that perform reviewed mutative operations (commits, etc.)
# These tools MUST NOT end with an ambiguous timeout — the executor
# waits synchronously for the final result even if the soft timeout fires.
REVIEWED_MUTATIVE_TOOLS: frozenset[str] = frozenset({
    "repo_commit",
})

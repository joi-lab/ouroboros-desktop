"""Single source of truth for tool visibility, parallelism, and result limits."""

from __future__ import annotations

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "repo_read", "repo_list", "repo_write", "repo_commit",
    "str_replace_editor",
    "data_read", "data_list", "data_write",
    "code_search",
    "run_shell", "claude_code_edit",
    "git_status", "git_diff",
    "restore_to_head", "revert_commit",
    "pull_from_remote", "rollback_to_target",
    # schedule_task/wait/get_result remain opt-in via enable_tools.
    "update_scratchpad", "update_identity",
    "chat_history", "recent_tasks",
    "knowledge_read", "knowledge_write", "knowledge_list",
    "web_search",
    "browse_page", "browser_action", "analyze_screenshot",
    "send_user_message", "send_photo",
    "switch_model",
    "request_restart", "promote_to_stable",
    "advisory_pre_review", "review_status",
    # Heal mode blocks enable_tools, so repair/review tools must be core.
    "list_skills", "review_skill", "skill_preflight",
    "submit_skill_to_hub",
})

# Meta-tools: always visible alongside core tools
META_TOOL_NAMES: frozenset[str] = frozenset({
    "list_available_tools", "enable_tools",
})

READ_ONLY_PARALLEL_TOOLS: frozenset[str] = frozenset({
    "repo_read", "repo_list",
    "data_read", "data_list",
    "code_search", "recent_tasks",
    "web_search", "codebase_digest", "chat_history",
})

# Stateful browser tools need the thread-sticky executor.
STATEFUL_BROWSER_TOOLS: frozenset[str] = frozenset({
    "browse_page", "browser_action",
})

# Full outputs are semantic (review verdicts, advisory findings, status).
UNTRUNCATED_TOOL_RESULTS: frozenset[str] = frozenset({
    "repo_commit",
    "multi_model_review",
    "advisory_pre_review",
    "review_skill",
    "review_status",
})

# Cognitive artifacts must not be truncated.
UNTRUNCATED_REPO_READ_PATHS: frozenset[str] = frozenset({
    "BIBLE.md",
    "README.md",
    "docs/ARCHITECTURE.md",
    "docs/CHECKLISTS.md",
    "docs/DEVELOPMENT.md",
})

# Per-tool char caps; omitted tools use DEFAULT_TOOL_RESULT_LIMIT.
TOOL_RESULT_LIMITS: dict[str, int] = {
    "repo_read": 80_000,
    "data_read": 80_000,
    "recent_tasks": 80_000,
    "knowledge_read": 80_000,
    "run_shell": 80_000,
    "code_search": 80_000,
    # skill_exec wraps stdout/stderr; keep the full capped payload visible.
    "skill_exec": 300_000,
}

DEFAULT_TOOL_RESULT_LIMIT: int = 15_000

# Reviewed mutative tools must not end with ambiguous executor timeouts.
REVIEWED_MUTATIVE_TOOLS: frozenset[str] = frozenset({
    "repo_commit",
})

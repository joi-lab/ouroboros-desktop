"""Single source of truth for tool visibility, parallelism, and result limits."""

from __future__ import annotations

CORE_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "write_file", "edit_text",
    "search_code",
    "run_command", "claude_code_edit", "run_script",
    "start_service", "service_status", "service_logs", "stop_service",
    "vcs_status", "vcs_diff", "vcs_commit_reviewed", "commit_reviewed",
    "vcs_restore", "vcs_revert", "vcs_pull_ff", "vcs_rollback",
    "schedule_subagent", "wait_task", "wait_tasks", "get_task_result",
    "update_scratchpad", "update_identity",
    "chat_history", "recent_tasks",
    "knowledge_read", "knowledge_write", "knowledge_list",
    "web_search",
    "browse_page", "browser_action", "analyze_screenshot",
    "send_user_message", "send_photo", "send_video",
    "switch_model",
    "request_restart", "promote_to_stable",
    "advisory_review", "review_status", "task_acceptance_review",
    # Heal mode blocks enable_tools, so repair/review tools must be core.
    "list_skills", "skill_review", "skill_preflight",
    "submit_skill_to_hub",
})

# Meta-tools: always visible alongside core tools
META_TOOL_NAMES: frozenset[str] = frozenset({
    "list_available_tools", "enable_tools",
})

LOCAL_READONLY_SUBAGENT_MODE: str = "local_readonly_subagent"
MAX_SUBTASK_DEPTH: int = 2

# V1 subagents are read-only against local Ouroboros state. Browser interaction
# remains available by explicit product decision, so this mode is not a remote
# website sandbox.
LOCAL_READONLY_SUBAGENT_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "search_code", "codebase_digest",
    "vcs_status", "vcs_diff",
    "chat_history", "recent_tasks", "get_task_result", "wait_task", "wait_tasks",
    "web_search", "browse_page", "browser_action", "analyze_screenshot",
})

READ_ONLY_PARALLEL_TOOLS: frozenset[str] = frozenset({
    "read_file", "list_files",
    "search_code", "recent_tasks",
    "web_search", "codebase_digest", "chat_history",
    "vcs_status", "vcs_diff", "service_status", "service_logs",
})

# Stateful browser tools need the thread-sticky executor.
STATEFUL_BROWSER_TOOLS: frozenset[str] = frozenset({
    "browse_page", "browser_action",
})

# Full outputs are semantic (review verdicts, advisory findings, status).
UNTRUNCATED_TOOL_RESULTS: frozenset[str] = frozenset({
    "commit_reviewed",
    "vcs_commit_reviewed",
    "plan_task",
    "task_acceptance_review",
    "advisory_review",
    "skill_review",
    "review_status",
    "get_task_result",
    "wait_task",
    "wait_tasks",
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
    "read_file": 80_000,
    "recent_tasks": 80_000,
    "knowledge_read": 80_000,
    "claude_code_edit": 80_000,
    "run_command": 80_000,
    "run_script": 80_000,
    "search_code": 80_000,
    "service_logs": 80_000,
    # skill_exec wraps stdout/stderr; keep the full capped payload visible.
    "skill_exec": 300_000,
}

DEFAULT_TOOL_RESULT_LIMIT: int = 15_000

# Reviewed mutative tools must not end with ambiguous executor timeouts.
REVIEWED_MUTATIVE_TOOLS: frozenset[str] = frozenset({
    "commit_reviewed",
    "vcs_commit_reviewed",
})

# Foreground mutative tools may keep editing files after Python future timeout;
# the loop must wait for terminal completion instead of returning while they run.
FOREGROUND_MUTATIVE_TOOLS: frozenset[str] = frozenset({
    "claude_code_edit",
})

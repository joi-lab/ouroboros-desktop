"""Structured log event formatting for the Logs tab."""

import flet as ft

LOG_CATEGORIES = {
    "tools":         {"types": set(), "has_tool_field": True,
                      "color": ft.Colors.TEAL_200,   "label": "Tools"},
    "llm":           {"types": {"llm_round", "llm_empty_response"},
                      "color": ft.Colors.BLUE_300,   "label": "LLM"},
    "errors":        {"types": {"tool_error", "tool_timeout", "task_error",
                                "llm_api_error", "consciousness_tool_error",
                                "consciousness_llm_error", "consciousness_error"},
                      "color": ft.Colors.RED_300,    "label": "Errors"},
    "tasks":         {"types": {"task_received", "task_done", "task_eval"},
                      "color": ft.Colors.GREEN_300,  "label": "Tasks"},
    "system":        {"types": {"worker_boot", "worker_spawn_start", "deps_sync_ok",
                                "startup_verification", "reset_fetch_failed",
                                "safe_restart_dev_import_failed", "restart_verify"},
                      "color": ft.Colors.AMBER_300,  "label": "System"},
    "consciousness": {"types": {"consciousness_thought", "bg_budget_exceeded_mid_cycle"},
                      "color": ft.Colors.PURPLE_200, "label": "BG"},
}


def categorize_event(evt: dict) -> tuple:
    """Return (category_key, color) for an event dict."""
    typ = evt.get("type", "")
    if not typ and evt.get("tool"):
        return "tools", LOG_CATEGORIES["tools"]["color"]
    for key, cat in LOG_CATEGORIES.items():
        if typ in cat.get("types", set()):
            return key, cat["color"]
    return "system", ft.Colors.WHITE24


def _extract_tool_detail(tool: str, args: dict) -> str:
    if tool == "run_shell":
        cmd = args.get("cmd", [])
        return f"cmd={cmd}" if len(str(cmd)) < 80 else f"cmd={str(cmd)[:77]}..."
    if tool in ("repo_read", "repo_write_commit", "drive_read", "drive_write"):
        return f"path={args.get('path', '?')}"
    if tool == "web_search":
        return f"q={args.get('query', '?')[:60]}"
    if tool == "browse_page":
        return f"url={args.get('url', '?')[:60]}"
    if tool in ("update_scratchpad", "update_identity"):
        content = args.get("content", "")
        return f"len={len(content)}"
    if tool == "repo_commit":
        return f"msg={args.get('commit_message', '?')[:50]}"
    if tool == "git_status":
        return ""
    if tool == "schedule_task":
        return f"type={args.get('type', '?')}"
    if tool == "set_next_wakeup":
        return f"{args.get('seconds', '?')}s"
    for k, v in args.items():
        sv = str(v)
        return f"{k}={sv[:50]}" if len(sv) > 50 else f"{k}={sv}"
    return ""


def format_log_line(evt: dict) -> str:
    """Build a compact, readable one-liner from a log event."""
    ts = (evt.get("ts") or "")[11:19]
    typ = evt.get("type", "")

    if not typ and evt.get("tool"):
        tool = evt["tool"]
        args = evt.get("args", {})
        detail = _extract_tool_detail(tool, args)
        preview = evt.get("result_preview", "")
        if preview and "\u26a0\ufe0f" in preview:
            return f"{ts}  [{tool}] {detail} -> ERROR"
        return f"{ts}  [{tool}] {detail}"

    if typ == "llm_round":
        r = evt.get("round", "?")
        model = (evt.get("model") or "?").split("/")[-1]
        cost = evt.get("cost_usd", 0)
        return f"{ts}  [llm] r={r} {model} ${cost:.3f}"

    if typ == "task_received":
        task = evt.get("task", {})
        text = (task.get("text") or "")[:60]
        return f'{ts}  [task] "{text}"'

    if typ == "task_done":
        tid = (evt.get("task_id") or "")[:8]
        cost = evt.get("cost_usd", 0)
        rounds = evt.get("total_rounds", "?")
        return f"{ts}  [done] id={tid} ${cost:.3f} {rounds} rounds"

    if typ == "task_eval":
        tid = (evt.get("task_id") or "")[:8]
        ok = evt.get("ok", "?")
        errs = evt.get("tool_errors", 0)
        calls = evt.get("tool_calls", 0)
        return f"{ts}  [eval] id={tid} ok={ok} tools={calls} errors={errs}"

    if typ in ("tool_error", "tool_timeout", "consciousness_tool_error"):
        tool = evt.get("tool", "?")
        err = (evt.get("error") or "")[:80]
        return f"{ts}  [error] {tool}: {err}"

    if typ == "llm_api_error":
        err = (evt.get("error") or "")[:80]
        return f"{ts}  [llm_error] {err}"

    if typ == "consciousness_thought":
        rounds = evt.get("rounds_used", "?")
        return f"{ts}  [thought] {rounds} rounds"

    if typ == "worker_boot":
        pid = evt.get("pid", "?")
        branch = evt.get("git_branch", "?")
        return f"{ts}  [boot] pid={pid} branch={branch}"

    if typ == "worker_spawn_start":
        count = evt.get("count", "?")
        return f"{ts}  [spawn] {count} workers"

    if typ == "startup_verification":
        checks = evt.get("checks", {})
        budget = checks.get("budget", {})
        remaining = budget.get("remaining_usd", "?")
        return f"{ts}  [startup] budget=${remaining}"

    short = str(evt)[:100]
    return f"{ts}  [{typ or 'event'}] {short}"


def format_log_entry(evt: dict) -> ft.Text:
    """Create a colored ft.Text control from a log event."""
    _, color = categorize_event(evt)
    line = format_log_line(evt)
    return ft.Text(line, size=11, color=color, font_family="monospace", selectable=True)

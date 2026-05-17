"""
Safety Agent — Policy-based LLM safety check.

Every tool call in registry.execute() is routed through check_safety() with a
policy lookup:

  - POLICY_SKIP               — trusted built-in tool; no LLM call.
  - POLICY_CHECK_CONDITIONAL  — only for run_shell: safe-subject whitelist
                                bypasses LLM; otherwise LLM check.
  - POLICY_CHECK              — always LLM check.

Unknown tools (e.g. tools the agent creates at runtime) fall back to
DEFAULT_POLICY = POLICY_CHECK. This is the "weak cheap recheck" layer for
newly-created tools — enough to notice obviously destructive calls, fast
enough not to hurt normal development (single light-model call).

Defense in depth (unchanged, lives elsewhere):
  - Hardcoded sandbox in ouroboros/tools/registry.py runs BEFORE this
    module and blocks protected runtime paths (safety-critical, frozen contracts,
    release invariants), mutative git via shell, and GitHub repo/auth manipulation.
  - claude_code_edit post-execution revert/non-pro guard for protected paths; normal commit review remains mandatory.
  - Pre-commit triad + scope review (review.py, scope_review.py).

Returns:
  (True, "")                         — SAFE, proceed without comment
  (True, "⚠️ SAFETY_WARNING: ...")    — SUSPICIOUS, proceed but warn the agent
  (False, "⚠️ SAFETY_VIOLATION: ...") — DANGEROUS, blocked
"""

import ast
import json
import logging
import os
import pathlib
import re
import shlex
from typing import Tuple, Dict, Any, List, Optional

from ouroboros.config import get_light_model
from ouroboros.llm import LLMClient
from ouroboros.pricing import emit_llm_usage_event, estimate_cost, infer_provider_from_model
from supervisor.state import update_budget_from_usage

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Policy constants
# ---------------------------------------------------------------------------

POLICY_SKIP = "skip"
POLICY_CHECK = "check"
POLICY_CHECK_CONDITIONAL = "check_conditional"

# Default policy for unknown tools (e.g. agent-created tools that are not yet
# in TOOL_POLICY). "check" means: always run one cheap LLM check — weak but
# better than nothing, and low-cost enough not to matter.
DEFAULT_POLICY = POLICY_CHECK

# Explicit policy for every built-in tool exported by modules in
# ouroboros/tools/. Keep in sync with get_tools() across those modules.
# An invariant test (test_tool_policy_covers_all_builtin_tools) guards against
# silent drift when a new built-in tool is added without an entry here.
TOOL_POLICY: Dict[str, str] = {
    # --- Read-only / trivially safe ---
    "repo_read": POLICY_SKIP,
    "repo_list": POLICY_SKIP,
    "data_read": POLICY_SKIP,
    "data_list": POLICY_SKIP,
    "git_status": POLICY_SKIP,
    "git_diff": POLICY_SKIP,
    "chat_history": POLICY_SKIP,
    "recent_tasks": POLICY_SKIP,
    "knowledge_read": POLICY_SKIP,
    "knowledge_list": POLICY_SKIP,
    "web_search": POLICY_SKIP,
    "codebase_digest": POLICY_SKIP,
    "codebase_health": POLICY_SKIP,
    "code_search": POLICY_SKIP,
    "list_available_tools": POLICY_SKIP,
    "memory_map": POLICY_SKIP,
    "analyze_screenshot": POLICY_SKIP,
    "vlm_query": POLICY_SKIP,
    "browse_page": POLICY_SKIP,
    "browser_action": POLICY_SKIP,
    "list_github_prs": POLICY_SKIP,
    "get_github_pr": POLICY_SKIP,
    "list_github_issues": POLICY_SKIP,
    "get_github_issue": POLICY_SKIP,
    "multi_model_review": POLICY_SKIP,
    "plan_task": POLICY_SKIP,
    "review_status": POLICY_SKIP,
    "get_task_result": POLICY_SKIP,
    "wait_for_task": POLICY_SKIP,
    "switch_model": POLICY_SKIP,

    # --- Mutative, but guarded by hardcoded sandbox / revert / review gate ---
    "repo_write": POLICY_SKIP,
    "repo_commit": POLICY_SKIP,
    "str_replace_editor": POLICY_SKIP,
    "data_write": POLICY_SKIP,
    "knowledge_write": POLICY_SKIP,
    "update_scratchpad": POLICY_SKIP,
    "update_identity": POLICY_SKIP,
    "memory_update_registry": POLICY_SKIP,
    "pull_from_remote": POLICY_SKIP,
    "restore_to_head": POLICY_SKIP,
    "revert_commit": POLICY_SKIP,
    "rollback_to_target": POLICY_SKIP,

    # --- Control / messaging / internal side effects ---
    "schedule_task": POLICY_SKIP,
    "cancel_task": POLICY_SKIP,
    "request_restart": POLICY_SKIP,
    "request_deep_self_review": POLICY_SKIP,
    "set_tool_timeout": POLICY_SKIP,
    "toggle_evolution": POLICY_SKIP,
    "toggle_consciousness": POLICY_SKIP,
    "promote_to_stable": POLICY_SKIP,
    "send_user_message": POLICY_SKIP,
    "send_photo": POLICY_SKIP,
    "forward_to_worker": POLICY_SKIP,
    "summarize_dialogue": POLICY_SKIP,
    "compact_context": POLICY_SKIP,
    "enable_tools": POLICY_SKIP,
    "advisory_pre_review": POLICY_SKIP,

    # --- Phase 3 three-layer refactor: external skill surface ---
    # Read-only catalogue view.
    "list_skills": POLICY_SKIP,
    # Runs the existing tri-model review against a skill package; no
    # subprocess execution, only durable skill state updates.
    "review_skill": POLICY_SKIP,
    # Flips the ``enabled.json`` bit for a single skill in its private
    # state directory. Cannot touch the main repo.
    "toggle_skill": POLICY_SKIP,
    # Actually spawns a subprocess from the external skill checkout.
    # The tool itself enforces executable review + enabled + non-stale hash;
    # v5.1.2 Frame A: runtime_mode no longer gates execution. We still
    # keep a cheap per-call LLM recheck as defense in depth.
    "skill_exec": POLICY_CHECK,
    # v5.7.0: read-only argv-only syntax validator for skill payloads
    # (Python compile() / node --check / bash -n + manifest parse). Heal-allowed.
    # No state mutation, no shell strings, scrubbed env, 30s/file cap.
    "skill_preflight": POLICY_SKIP,

    # --- Conditional: run_shell (safe-subject whitelist) ---
    "run_shell": POLICY_CHECK_CONDITIONAL,

    # --- Always LLM-checked built-ins ---
    "fetch_pr_ref": POLICY_CHECK,
    "create_integration_branch": POLICY_CHECK,
    "cherry_pick_pr_commits": POLICY_CHECK,
    "stage_adaptations": POLICY_CHECK,
    "stage_pr_merge": POLICY_CHECK,
    "run_ci_tests": POLICY_CHECK,
    "generate_evolution_stats": POLICY_CHECK,
    "submit_skill_to_hub": POLICY_CHECK,
    "comment_on_pr": POLICY_CHECK,
    "comment_on_issue": POLICY_CHECK,
    "close_github_issue": POLICY_CHECK,
    "create_github_issue": POLICY_CHECK,

    # Delegates to the external Claude Agent SDK/CLI: the code actually
    # mutating the repo runs outside this process, so we keep one cheap LLM
    # recheck of the call itself. PreToolUse hooks + post-execution revert in
    # ouroboros/gateways/claude_code.py and registry.py remain as defense in depth.
    "claude_code_edit": POLICY_CHECK,

    # --- Consciousness-only built-ins manually registered outside get_tools() ---
    # (e.g. ouroboros/consciousness.py._build_registry) — must still have an
    # explicit entry so the coverage invariant catches drift.
    "set_next_wakeup": POLICY_SKIP,
}

# ---------------------------------------------------------------------------
# run_shell safe-subject whitelist
# ---------------------------------------------------------------------------

# Note: ``pip`` is intentionally omitted — ``pip install/uninstall`` mutates the
# Python environment and must route through the LLM check. ``python -m pytest``
# is still whitelisted via the Python interpreter branch below.
SAFE_SHELL_COMMANDS = frozenset([
    "ls", "cat", "head", "tail", "grep", "rg", "find", "wc",
    "git", "pytest", "pwd", "whoami",
    "date", "which", "file", "stat", "diff", "tree",
    "du", "df",
])

_SAFE_PYTHON_MODULE_ALIASES = {
    "pytest": "pytest",
    "py.test": "pytest",
}


def _split_shell_command(raw_cmd: Any) -> List[str]:
    """Best-effort argv parser for safety whitelist classification."""
    if isinstance(raw_cmd, list):
        return [str(part) for part in raw_cmd if str(part).strip()]
    text = str(raw_cmd or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return [str(part) for part in parsed if str(part).strip()]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    try:
        parsed = ast.literal_eval(text)
        if isinstance(parsed, list):
            return [str(part) for part in parsed if str(part).strip()]
    except (SyntaxError, ValueError):
        pass
    try:
        return [str(part) for part in shlex.split(text) if str(part).strip()]
    except ValueError:
        return text.split()


def _is_explicit_python_interpreter(executable: str) -> bool:
    """Allow only literal Python interpreter tokens, not path/basename lookalikes."""
    token = str(executable or "").strip().lower()
    if not token:
        return False
    if token in {"python", "python3"}:
        return True
    return bool(re.fullmatch(r"python\d+(?:\.\d+)?", token))


def _normalize_safe_shell_subject(raw_cmd: Any) -> str:
    """Return the canonical safe subject for deterministic shell allowlisting."""
    argv = _split_shell_command(raw_cmd)
    if not argv:
        return ""

    executable = str(argv[0]).strip().lower()
    if executable in SAFE_SHELL_COMMANDS:
        return executable

    if _is_explicit_python_interpreter(executable):
        for idx, part in enumerate(argv[1:-1], start=1):
            part_str = str(part)
            if part_str == "-m":
                module = str(argv[idx + 1]).lower()
                return _SAFE_PYTHON_MODULE_ALIASES.get(module, "")
            if part_str == "-c":
                break
            # The moment we see a positional argument (typically a script
            # path), further ``-m`` / ``-c`` tokens belong to that script,
            # not to the Python interpreter. Stop here so a malicious
            # ``python malicious.py -m pytest`` cannot bypass the LLM check.
            if not part_str.startswith("-"):
                break
            # Bare ``--`` terminator — anything after belongs to the script.
            if part_str == "--":
                break

    return ""


# ---------------------------------------------------------------------------
# LLM check plumbing
# ---------------------------------------------------------------------------

def _get_safety_prompt() -> str:
    """Load the safety system prompt from prompts/SAFETY.md."""
    prompt_path = pathlib.Path(__file__).parent.parent / "prompts" / "SAFETY.md"
    try:
        return prompt_path.read_text(encoding="utf-8")
    except Exception as e:
        log.error(f"Failed to read SAFETY.md: {e}")
        return (
            "You are a security supervisor. Block only clearly destructive commands. "
            "Default to SAFE. Respond with JSON: "
            '{\"status\": \"SAFE\"|\"SUSPICIOUS\"|\"DANGEROUS\", \"reason\": \"...\"}'
        )


# ---------------------------------------------------------------------------
# Secret redaction
# ---------------------------------------------------------------------------

# Segment-level secret key names. Matching is whole-segment (after splitting
# the key on ``_``/``-``) to avoid false positives like ``override_author``
# which contains the substring ``auth`` but is not a credential carrier.
_SECRET_KEY_SEGMENTS = frozenset({
    "key",  # only together with prefix segment — see _is_secret_key
    "apikey",
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "credentials",
    "cookie",
    "authorization",
})

# Prefix+suffix shapes treated as credential keys regardless of split (e.g.
# ``api_key`` → segments {"api","key"} which _is_secret_key combines).
_SECRET_KEY_COMBO = frozenset({
    ("api", "key"),
    ("access", "key"),
    ("access", "token"),
    ("auth", "token"),
    ("auth", "key"),
    ("session", "token"),
    ("refresh", "token"),
})


def _is_secret_key(key: str) -> bool:
    """Segment-aware credential-key classifier.

    Splits on ``_`` / ``-`` and matches only whole segments so that keys
    whose name merely contains a credential substring (``override_author``,
    ``authored``, ``coauthor``) are not over-redacted.
    """
    segments = [s for s in re.split(r"[_\-]+", str(key).lower()) if s]
    if not segments:
        return False
    seg_set = set(segments)
    if any(seg in _SECRET_KEY_SEGMENTS and seg != "key" for seg in seg_set):
        return True
    for i in range(len(segments) - 1):
        if (segments[i], segments[i + 1]) in _SECRET_KEY_COMBO:
            return True
    # ``apikey`` (no separator) handled by segment set above. ``password``,
    # ``secret`` etc. caught above. ``key`` alone is too ambiguous (matches
    # e.g. ``primary_key`` on a DB row) so it only counts in combinations.
    return False

# Inline patterns for known secret shapes (Bearer headers, OpenAI/GitHub tokens,
# Anthropic keys). Intentionally conservative: redact shape matches and let
# the LLM reason about surrounding intent.
# No leading/trailing ``\b`` on the `sk-` family — when a token sits flush
# against more word characters (pathological `AAA…sk-TOKENBBB…` with no
# whitespace), a word boundary would fail to match and the redaction
# would leak. Over-redacting a few neighbouring word characters is
# acceptable; under-redacting a credential is not. The Bearer and
# api_key patterns keep their leading ``\b`` since those prefixes
# starting mid-word would be a false positive.
_SECRET_INLINE_PATTERNS = (
    re.compile(r"(sk|pk|rk|gh[opsu])[-_][A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{16,}", re.IGNORECASE),
    re.compile(r"\bapi[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9._\-]{16,}['\"]?", re.IGNORECASE),
)


def _redact_secret_value(value: Any) -> Any:
    """Return a JSON-serializable redaction marker for a sensitive value."""
    if isinstance(value, str) and value:
        return f"[REDACTED: {len(value)} chars]"
    if value in (None, "", 0, False):
        return value
    return "[REDACTED]"


def _redact_secrets_in_arguments(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of arguments with secret-like key values redacted.

    Also applies inline-secret-shape scrubbing to every string leaf (including
    strings inside nested lists/tuples — e.g. ``cmd=["curl","-H","Authorization: Bearer …"]``)
    and renders non-JSON-serializable values as ``repr`` strings so the safety
    prompt can never TypeError on arbitrary tool arguments.
    """
    def _walk(value: Any) -> Any:
        if isinstance(value, dict):
            out = {}
            for k, v in value.items():
                if _is_secret_key(k):
                    out[k] = _redact_secret_value(v)
                else:
                    out[k] = _walk(v)
            return out
        if isinstance(value, (list, tuple)):
            return [_walk(v) for v in value]
        if isinstance(value, str):
            return _redact_secrets_in_text(value)
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        # Fall back to repr for objects that don't JSON-serialize cleanly;
        # scrub inline shapes out of the repr too in case it contains a token.
        return _redact_secrets_in_text(repr(value))

    try:
        return _walk(arguments)
    except Exception:
        # Last-ditch fallback — never let secret redaction itself crash the
        # safety path (would lock the agent out of every unknown tool).
        return {"_redacted": "[REDACTION_FAILED]"}


def _redact_secrets_in_text(text: str) -> str:
    """Strip common inline-secret shapes out of a free-form string."""
    redacted = text
    for pattern in _SECRET_INLINE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _format_messages_for_safety(messages: List[Dict[str, Any]]) -> str:
    """Format conversation messages into a compact context string for the safety LLM.

    Secrets are redacted BEFORE truncation so a token-like substring that
    crosses the 500-char boundary cannot be split into a non-matching
    fragment and leak to the external safety model.
    """
    parts = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content", "")
        if not content or role == "tool":
            continue
        if isinstance(content, list):
            content = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        text = _redact_secrets_in_text(str(content))
        if len(text) > 500:
            omitted = len(text) - 500
            text = text[:500] + f" [...{omitted} chars omitted]"
        parts.append(f"[{role}] {text}")
    return "\n".join(parts)


def _build_check_prompt(
    tool_name: str,
    arguments: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]] = None,
) -> str:
    safe_args = _redact_secrets_in_arguments(arguments or {})
    try:
        args_json = json.dumps(safe_args, indent=2, default=repr)
    except Exception:
        args_json = repr(safe_args)
    runtime_mode = os.environ.get("OUROBOROS_RUNTIME_MODE", "advanced") or "advanced"
    prompt = (
        "Proposed tool call:\n"
        f"Runtime mode: {runtime_mode}\n"
        f"Tool: {tool_name}\n"
        f"Arguments:\n```json\n{args_json}\n```\n"
    )
    if messages:
        context = _format_messages_for_safety(messages)
        if context.strip():
            prompt += f"\nConversation context:\n{context}\n"
    prompt += "\nIs this safe?"
    return prompt


def _parse_safety_response(text: str) -> Optional[Dict[str, Any]]:
    """Parse JSON from LLM response, handling markdown code fences."""
    clean = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(clean)
    except json.JSONDecodeError:
        return None


_REMOTE_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_COMPATIBLE_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
)

_LOCAL_ROUTING_KEYS = (
    "USE_LOCAL_MAIN",
    "USE_LOCAL_CODE",
    "USE_LOCAL_LIGHT",
    "USE_LOCAL_FALLBACK",
)

# Provider-specific API key mapped from ``infer_api_key_type`` result.
_PROVIDER_KEY_ENV = {
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
    "cloudru": "CLOUDRU_FOUNDATION_MODELS_API_KEY",
}


def _any_remote_provider_configured() -> bool:
    return any(str(os.environ.get(k, "") or "").strip() for k in _REMOTE_PROVIDER_KEYS)


def _any_local_routing_enabled() -> bool:
    return any(
        str(os.environ.get(k, "") or "").lower() in ("true", "1")
        for k in _LOCAL_ROUTING_KEYS
    )


def _light_model_has_reachable_provider(light_model: str) -> bool:
    """Return True iff the configured light model's provider has a usable
    runtime configuration (API key AND any additional prerequisites).

    When ``OUROBOROS_MODEL_LIGHT`` is prefixed (``anthropic::…``,
    ``openai::…``, ``openai-compatible::…``, ``cloudru::…``) it routes
    directly to that provider regardless of whether another remote key
    like ``OPENROUTER_API_KEY`` is set. Without this check, a config
    combining OpenRouter-only credentials with a direct-provider light
    model would hit the direct path, raise on missing key, and
    ``SAFETY_VIOLATION`` every ``POLICY_CHECK`` call.

    For ``openai-compatible`` we also require ``OPENAI_COMPATIBLE_BASE_URL``
    (or legacy ``OPENAI_BASE_URL``) since ``LLMClient._resolve_remote_target``
    will raise without it — an API key alone is not enough.
    """
    try:
        from ouroboros.pricing import infer_api_key_type
        key_type = infer_api_key_type(light_model)
    except Exception:  # pragma: no cover — defensive
        return True  # don't over-block on classifier failure
    env_key = _PROVIDER_KEY_ENV.get(key_type)
    if env_key is None:
        return True
    if not str(os.environ.get(env_key, "") or "").strip():
        return False
    if key_type == "openai-compatible":
        base_url = (
            str(os.environ.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
            or str(os.environ.get("OPENAI_BASE_URL", "") or "").strip()
        )
        if not base_url:
            return False
    return True


def _resolve_safety_routing() -> Tuple[bool, bool, Optional[str]]:
    """Decide whether the safety LLM call should go to local or remote.

    Returns ``(use_local, is_fallback, skip_reason)``.

    - ``skip_reason`` is non-None when no reachable safety backend exists;
      the caller fails open with a SAFETY_WARNING.
    - ``is_fallback`` is True when local was chosen only because no reachable
      remote light-model provider is configured (either no remote keys at
      all, or the configured light-model's provider key is missing). In that
      case a local transport failure must also fail open — blocking would
      recreate the tool-creation friction this refactor set out to remove.
      When ``USE_LOCAL_LIGHT`` is explicitly opted in, local is primary and
      ``is_fallback`` is False; a local failure is a real config error.
    """
    if str(os.environ.get("USE_LOCAL_LIGHT", "") or "").lower() in ("true", "1"):
        return True, False, None

    light_model = get_light_model()

    if _any_remote_provider_configured():
        # The configured light model's provider must itself have a key,
        # otherwise the direct-provider call will raise and turn every
        # unknown-tool check into SAFETY_VIOLATION.
        if _light_model_has_reachable_provider(light_model):
            return False, False, None
        if _any_local_routing_enabled():
            # Provider-mismatch: we route to local as a fallback so the
            # check still runs somewhere, and mark it as fallback so a
            # local transport failure is tolerated too.
            return True, True, None
        return False, False, (
            f"Light model provider key missing for {light_model} "
            f"(other remote keys are set but they don't cover this provider); "
            "skipping check."
        )

    if _any_local_routing_enabled():
        # Remote keys absent but operator has opted into local routing for
        # some lane — route safety to local light too so unknown tools aren't
        # hard-blocked on local-only configs. Fallback semantics apply so a
        # local transport failure still fails open.
        return True, True, None

    return False, False, (
        "No safety LLM available (neither remote provider keys nor local "
        "routing are configured); skipping check."
    )


_UNCHECKED_WARNING_SUFFIX = (
    "The tool call was allowed so the agent is not hard-blocked on a misconfigured "
    "runtime — the hardcoded sandbox (registry.py SAFETY_CRITICAL_PATHS, mutative-git "
    "via shell, gh repo/auth) still applies to every tool, and the claude_code_edit "
    "post-execution revert still applies when the failing call is claude_code_edit."
)


def _run_llm_check(
    tool_name: str,
    arguments: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]],
    ctx: Optional[Any],
) -> Tuple[bool, str]:
    """Run a single light-model safety check and classify the verdict."""
    _use_local_light, _is_local_fallback, _skip_reason = _resolve_safety_routing()
    if _skip_reason is not None:
        log.warning("Safety backend unavailable for %s: %s", tool_name, _skip_reason)
        return True, (
            f"⚠️ SAFETY_WARNING: Safety backend is not configured "
            f"({_skip_reason.rstrip('.')}). {_UNCHECKED_WARNING_SUFFIX}"
        )

    prompt = _build_check_prompt(tool_name, arguments, messages)
    client = LLMClient()

    light_model = get_light_model()
    log.info(f"Running safety check on {tool_name} using {light_model} (local={_use_local_light})")

    try:
        msg, usage = client.chat(
            messages=[
                {"role": "system", "content": _get_safety_prompt()},
                {"role": "user", "content": prompt},
            ],
            model=light_model,
            use_local=_use_local_light,
        )
    except Exception as e:
        # When the local branch was only chosen as a fallback (because no
        # reachable light-model backend exists), a local runtime outage must
        # not turn every unknown-tool call into SAFETY_VIOLATION — that would
        # hard-block the agent on any degraded config. Fail open with a
        # visible warning; the hardcoded sandbox still applies to every call,
        # and claude_code_edit's post-execution revert still applies to that
        # specific tool.
        if _use_local_light and _is_local_fallback:
            log.warning(
                "Safety local-fallback LLM call failed for %s (%s); proceeding with warning",
                tool_name, e,
            )
            return True, (
                f"⚠️ SAFETY_WARNING: Local safety runtime unreachable ({e}). "
                f"{_UNCHECKED_WARNING_SUFFIX}"
            )
        log.error(f"Safety check LLM call failed for {tool_name}: {e}")
        return False, f"⚠️ SAFETY_VIOLATION: Safety check failed with error: {e}"

    if usage:
        # Prefer the provider-canonical identity returned by the LLM client over
        # the raw env/request model string so cost estimation and usage events
        # match the contract used elsewhere in the repo (plan_review, review).
        resolved_model = str(usage.get("resolved_model") or light_model)
        if _use_local_light:
            provider = "local"
            model_name = f"{light_model} (local)"
        else:
            provider = str(usage.get("provider") or infer_provider_from_model(light_model))
            model_name = resolved_model
        cost = float(usage.get("cost") or 0.0)
        if not _use_local_light and cost == 0.0:
            cost = estimate_cost(
                resolved_model,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("cached_tokens") or 0),
                int(usage.get("cache_write_tokens") or 0),
            )
            # Populate the estimate back into the usage dict so the
            # update_budget_from_usage fallback below (when there is no
            # event_queue to emit through) still attributes the spend.
            usage["cost"] = cost
        _eq = getattr(ctx, "event_queue", None) if ctx is not None else None
        if _eq is not None:
            emit_llm_usage_event(
                _eq,
                getattr(ctx, "task_id", "") if ctx is not None else "",
                model_name, usage, cost,
                category="safety",
                provider=provider,
                source="safety_check",
            )
        else:
            update_budget_from_usage(usage)

    result = _parse_safety_response(msg.get("content") or "")
    if result is None:
        log.error(f"Safety check returned invalid JSON for {tool_name}: {msg.get('content')}")
        return False, "⚠️ SAFETY_VIOLATION: Safety Supervisor returned unparseable response."

    status = str(result.get("status", "")).upper()
    reason = result.get("reason", "Unknown")

    if status == "SAFE":
        return True, ""

    if status == "SUSPICIOUS":
        log.warning(f"Safety check: {tool_name} is suspicious: {reason}")
        return True, (
            f"⚠️ SAFETY_WARNING: The Safety Supervisor flagged this action as suspicious.\n"
            f"Reason: {reason}\n"
            f"The command was allowed, but consider whether this is the right approach."
        )

    # DANGEROUS (or any unrecognised status — fail safe)
    log.error(f"Safety check blocked {tool_name}: {reason}")
    return False, (
        f"⚠️ SAFETY_VIOLATION: The Safety Supervisor blocked this command.\n"
        f"Reason: {reason}\n\n"
        f"You must find a different, safer approach to achieve your goal."
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_safety(
    tool_name: str,
    arguments: Dict[str, Any],
    messages: Optional[List[Dict[str, Any]]] = None,
    ctx: Optional[Any] = None,
) -> Tuple[bool, str]:
    """Check if a tool call is safe to execute.

    Returns:
      (True, "")           — SAFE
      (True, warning_str)  — SUSPICIOUS (proceed, but warning is passed to agent)
      (False, error_str)   — DANGEROUS (blocked)
    """
    # Defensive: arguments can be None when the LLM serializes a tool call
    # with no parameters — ``.get()`` on that would AttributeError.
    tool_name = str(tool_name or "").strip()
    arguments = arguments or {}
    policy = TOOL_POLICY.get(tool_name, DEFAULT_POLICY)

    if policy == POLICY_SKIP:
        return True, ""

    if policy == POLICY_CHECK_CONDITIONAL:
        # Currently only run_shell uses this policy.
        raw_cmd = arguments.get("cmd", arguments.get("command", ""))
        if _normalize_safe_shell_subject(raw_cmd):
            return True, ""
        return _run_llm_check(tool_name, arguments, messages, ctx)

    # POLICY_CHECK (explicit) or DEFAULT_POLICY (unknown tool).
    return _run_llm_check(tool_name, arguments, messages, ctx)

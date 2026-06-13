"""Regression tests: verify raised max_tokens / max_turns constants."""


def test_review_query_model_max_tokens():
    """review.py _query_model must use ≥65536 max_tokens."""
    import ast
    from pathlib import Path

    src = Path("ouroboros/tools/review.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "max_tokens":
            if isinstance(node.value, ast.Constant) and node.value.value >= 65536:
                return  # found
    raise AssertionError("Expected max_tokens>=65536 in review.py _query_model")


def test_scope_review_max_tokens():
    """scope_review.py _SCOPE_MAX_TOKENS must be ≥100000."""
    from ouroboros.tools.scope_review import _SCOPE_MAX_TOKENS
    assert _SCOPE_MAX_TOKENS >= 100_000


def test_llm_client_default_max_tokens():
    """Main remote chat defaults must leave enough output room for long tool plans."""
    import inspect
    from ouroboros.llm import LLMClient

    assert inspect.signature(LLMClient.chat).parameters["max_tokens"].default >= 65_536
    assert inspect.signature(LLMClient.chat_async).parameters["max_tokens"].default >= 65_536


def test_main_loop_explicit_max_tokens():
    """The task loop must pin the same 64K output budget even if client defaults move."""
    from ouroboros.loop_llm_call import MAIN_LOOP_MAX_TOKENS

    assert MAIN_LOOP_MAX_TOKENS >= 65_536


def test_vision_query_default_max_tokens():
    """VLM tools inherit the shared vision_query output budget."""
    import inspect
    from ouroboros.llm import LLMClient

    assert inspect.signature(LLMClient.vision_query).parameters["max_tokens"].default >= 32_768


def test_summary_and_background_token_budgets():
    """Summary/reflection/background paths must stay above the raised floors."""
    from pathlib import Path

    expectations = {
        "ouroboros/tools/review_synthesis.py": "max_tokens=16384",
        "ouroboros/consolidator.py": "max_tokens=16384",
        "ouroboros/reflection.py": "max_tokens=16384",
        "ouroboros/agent_task_pipeline.py": "max_tokens=16384",
        "ouroboros/context_compaction.py": "max_tokens=32768",
        "ouroboros/tools/skill_publish.py": "max_tokens=8192",
        "ouroboros/consciousness.py": "max_tokens=65536",
    }
    for path, needle in expectations.items():
        src = Path(path).read_text(encoding="utf-8").replace(" ", "")
        assert needle in src, f"{path} must contain {needle}"


def test_claude_code_edit_sdk_max_turns():
    """Edit and advisory paths must share the same default Claude Code turn budget (50)."""
    import ast
    from pathlib import Path

    # Verify the constant value via AST (works without claude_agent_sdk installed)
    gw_src = Path("ouroboros/gateways/claude_code.py").read_text(encoding="utf-8")
    tree = ast.parse(gw_src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_CLAUDE_CODE_MAX_TURNS":
                    assert isinstance(node.value, ast.Constant) and node.value.value == 50, (
                        f"DEFAULT_CLAUDE_CODE_MAX_TURNS should be 50, got {getattr(node.value, 'value', '?')}"
                    )
                    found = True
    assert found, "DEFAULT_CLAUDE_CODE_MAX_TURNS not found in claude_code.py"

    # Verify callers reference the shared constant
    shell_src = Path("ouroboros/tools/shell.py").read_text(encoding="utf-8")
    advisory_src = Path("ouroboros/tools/claude_advisory_review.py").read_text(encoding="utf-8")
    assert "DEFAULT_CLAUDE_CODE_MAX_TURNS" in shell_src
    assert "DEFAULT_CLAUDE_CODE_MAX_TURNS" in advisory_src
    assert "max_turns=25" not in shell_src
    assert "max_turns=8" not in advisory_src


def test_claude_code_sdk_only_no_cli_fallback():
    """shell.py must not contain legacy CLI subprocess fallback."""
    src = open("ouroboros/tools/shell.py", encoding="utf-8").read()
    assert "_run_claude_cli" not in src, "CLI fallback function should be gone"
    assert "ensure_claude_cli" not in src, "CLI install function should be gone"


def test_review_prompt_token_budget_is_ssot():
    """``review_helpers.REVIEW_PROMPT_TOKEN_BUDGET`` is the single source of
    truth for the unified scope/plan/deep-review input gate (920K). Bumping
    the constant must move all three call sites in lockstep so the skip
    threshold cannot silently desync between modules.

    Note: Claude Opus 4.6 has a 1M context window SHARED between input and
    output. ``estimate_tokens`` (chars/4) is approximate, so the 920K gate
    intentionally leaves limited output headroom and remains best-effort.
    """
    from ouroboros.tools.review_helpers import REVIEW_PROMPT_TOKEN_BUDGET
    from ouroboros.tools.scope_review import _SCOPE_BUDGET_TOKEN_LIMIT
    from ouroboros.tools.plan_review import _PLAN_BUDGET_TOKEN_LIMIT

    assert REVIEW_PROMPT_TOKEN_BUDGET == 920_000, (
        f"REVIEW_PROMPT_TOKEN_BUDGET drifted to {REVIEW_PROMPT_TOKEN_BUDGET}; "
        "see review_helpers.py docstring before changing — call sites do "
        "not silently re-pin to an old budget."
    )
    assert _SCOPE_BUDGET_TOKEN_LIMIT == REVIEW_PROMPT_TOKEN_BUDGET, (
        f"_SCOPE_BUDGET_TOKEN_LIMIT ({_SCOPE_BUDGET_TOKEN_LIMIT}) must equal "
        f"the SSOT REVIEW_PROMPT_TOKEN_BUDGET ({REVIEW_PROMPT_TOKEN_BUDGET})."
    )
    assert _PLAN_BUDGET_TOKEN_LIMIT == REVIEW_PROMPT_TOKEN_BUDGET, (
        f"_PLAN_BUDGET_TOKEN_LIMIT ({_PLAN_BUDGET_TOKEN_LIMIT}) must equal "
        f"the SSOT REVIEW_PROMPT_TOKEN_BUDGET ({REVIEW_PROMPT_TOKEN_BUDGET})."
    )


def test_deep_self_review_budget_uses_ssot():
    """``deep_self_review`` must gate on the SSOT constant (not a hardcoded
    literal) and must gate on the FULL assembled prompt (system + user)
    using the shared ``estimate_tokens(chars/4)`` helper, matching
    scope_review and plan_review.
    """
    import pathlib
    src = pathlib.Path("ouroboros/deep_self_review.py").read_text(encoding="utf-8")
    assert "REVIEW_PROMPT_TOKEN_BUDGET" in src, (
        "deep_self_review must import the SSOT constant from review_helpers"
    )
    assert "estimated_tokens > REVIEW_PROMPT_TOKEN_BUDGET" in src, (
        "deep_self_review must compare against the SSOT constant, not a literal"
    )
    assert "estimate_tokens(_SYSTEM_PROMPT + pack_text)" in src, (
        "deep_self_review must gate on the FULL assembled prompt "
        "(system + user) using the shared estimate_tokens(chars/4) helper."
    )
    # Old hardcoded literals must not survive — drift would silently desync.
    assert "estimated_tokens > 850_000" not in src, (
        "deep_self_review still has the old hardcoded literal; switch to the SSOT constant"
    )
    assert "estimated_tokens > 920_000" not in src, (
        "deep_self_review hardcodes the current budget; use the SSOT constant instead"
    )
    assert "int(stats[\"total_chars\"] / 3.5)" not in src, (
        "deep_self_review must not use its old chars/3.5 estimator"
    )


def test_tool_timeout_uses_max_of_settings_and_per_tool():
    """_get_tool_timeout must return max(settings, per_tool) not just settings."""
    import importlib
    from unittest.mock import patch
    import ouroboros.loop_tool_execution as mod

    class FakeTools:
        def get_timeout(self, name):
            return 1200  # per-tool declares 1200s

    # settings says 600, per-tool says 1200 → should return 1200
    with patch.object(mod, "load_settings", return_value={"OUROBOROS_TOOL_TIMEOUT_SEC": 600}):
        result = mod._get_tool_timeout(FakeTools(), "claude_code_edit")
    assert result == 1200, f"Expected 1200 (per-tool), got {result}"


def test_tool_timeout_settings_wins_when_higher():
    """_get_tool_timeout: if settings > per_tool, settings wins."""
    from unittest.mock import patch
    import ouroboros.loop_tool_execution as mod

    class FakeTools:
        def get_timeout(self, name):
            return 360  # default per-tool

    with patch.object(mod, "load_settings", return_value={"OUROBOROS_TOOL_TIMEOUT_SEC": 900}):
        result = mod._get_tool_timeout(FakeTools(), "run_command")
    assert result == 900, f"Expected 900 (settings), got {result}"


def test_review_evidence_no_truncation_by_default():
    """format_review_evidence_for_prompt must NOT truncate by default (max_chars=0)."""
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    big = {"has_evidence": True, "data": "x" * 10000}
    result = format_review_evidence_for_prompt(big)
    assert "truncated" not in result.lower()
    assert len(result) > 10000


def test_review_evidence_bounded_with_omission_note():
    """format_review_evidence_for_prompt truncates with explicit omission note when max_chars>0."""
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    big = {"has_evidence": True, "data": "x" * 10000}
    result = format_review_evidence_for_prompt(big, max_chars=500)
    assert "OMISSION NOTE" in result
    assert "truncated at 500 chars" in result


def test_review_evidence_no_obligation_cap():
    """collect_review_evidence default max_obligations must be None (no cap)."""
    import inspect
    from ouroboros.review_evidence import collect_review_evidence
    sig = inspect.signature(collect_review_evidence)
    default = sig.parameters["max_obligations"].default
    assert default is None, f"Expected None, got {default}"


def test_run_script_timeout_360():
    """run_script ToolEntry must stay foreground-bounded like run_command."""
    from ouroboros.tools.shell import get_tools
    entries = get_tools()
    rs = [e for e in entries if e.name == "run_script"]
    assert rs, "run_script not found in shell.get_tools()"
    assert rs[0].timeout_sec == 360


def test_advisory_pre_review_timeout_1200():
    """advisory_pre_review ToolEntry must declare timeout_sec=1200."""
    from ouroboros.tools.claude_advisory_review import get_tools
    entries = get_tools()
    apr = [e for e in entries if e.name == "advisory_review"]
    assert apr, "advisory_pre_review not found"
    assert apr[0].timeout_sec == 1200


def test_full_repo_pack_excludes_junk_dirs():
    """build_full_repo_pack must skip non-agent-logic directories (assets/, tests/)."""
    from ouroboros.tools.review_helpers import _FULL_REPO_SKIP_DIR_PREFIXES
    for prefix in ("assets/", "tests/"):
        assert prefix in _FULL_REPO_SKIP_DIR_PREFIXES, f"{prefix} not in skip list"


def test_summary_and_reflection_callers_use_bounded_evidence():
    """Summary and reflection prompt builders must call format_review_evidence_for_prompt with max_chars."""
    import ast
    from pathlib import Path

    for filename in ("ouroboros/agent_task_pipeline.py", "ouroboros/reflection.py"):
        src = Path(filename).read_text(encoding="utf-8")
        assert "format_review_evidence_for_prompt(" in src
        # Must pass max_chars argument (not rely on default 0)
        assert "max_chars=" in src, f"{filename} must call format_review_evidence_for_prompt with max_chars"


def test_obligation_context_shows_all():
    """build_review_context must not slice open_obligations."""
    src = open("ouroboros/agent_task_pipeline.py", encoding="utf-8").read()
    assert "open_obs[:4]" not in src, "open_obs[:4] cap should be removed"
    assert "obligation_ids[:4]" not in src, "obligation_ids[:4] cap should be removed"

"""Tests for advisory_pre_review observability, model-drift fix, and budget gate.

Split from test_commit_gate.py to keep each test module within the ~1000-line limit (P7).
"""
import importlib
import json
import os
import sys

import asyncio

import pytest

from tests._shared import ensure_claude_agent_sdk_mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ensure_claude_agent_sdk_mock()


def _get_advisory_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.tools.claude_advisory_review")


# ---------------------------------------------------------------------------
# Model-drift: resolve_claude_code_model
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "case_id,env_value,expected",
    [
        ("returns_env_value", "sonnet", "sonnet"),
        ("falls_back_to_shipped_default", None, "opus[1m]"),
        ("strips_whitespace", "  claude-opus-4.6  ", "claude-opus-4.6"),
    ],
)
def test_resolve_claude_code_model(monkeypatch, case_id, env_value, expected):
    sys.path.insert(0, REPO)
    gw = importlib.import_module("ouroboros.gateways.claude_code")
    if env_value is None:
        monkeypatch.delenv("CLAUDE_CODE_MODEL", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_MODEL", env_value)
    assert gw.resolve_claude_code_model() == expected


def test_shell_edit_uses_resolve_claude_code_model_helper():
    """claude_code_edit path must use resolve_claude_code_model(), not raw os.environ.get."""
    import inspect
    sys.path.insert(0, REPO)
    shell_mod = importlib.import_module("ouroboros.tools.shell")
    source = inspect.getsource(shell_mod._claude_code_edit)
    assert "resolve_claude_code_model" in source
    assert 'os.environ.get("CLAUDE_CODE_MODEL"' not in source


def test_advisory_uses_resolve_claude_code_model_helper():
    """_run_claude_advisory must call resolve_claude_code_model() — no hardcoded 'opus'."""
    import inspect
    adv_mod = _get_advisory_module()
    source = inspect.getsource(adv_mod._run_claude_advisory)
    assert "resolve_claude_code_model" in source


def test_advisory_passes_scope_review_effort_to_claude_code(monkeypatch, tmp_path):
    adv_mod = _get_advisory_module()
    from types import SimpleNamespace
    from ouroboros.gateways.claude_code import ClaudeCodeResult
    import ouroboros.gateways.claude_code as gw

    captured = {}

    def fake_run_readonly(**kwargs):
        captured.update(kwargs)
        return ClaudeCodeResult(
            success=True,
            result_text='[{"item":"bible_compliance","verdict":"PASS","reason":"ok","severity":"critical"}]',
            session_id="sess-effort",
            cost_usd=0,
            usage={},
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "low")
    monkeypatch.setattr(gw, "run_readonly", fake_run_readonly)
    monkeypatch.setattr(adv_mod, "_get_staged_diff", lambda *a, **kw: "diff")
    monkeypatch.setattr(adv_mod, "_get_changed_file_list", lambda *a, **kw: "M file.py")
    monkeypatch.setattr(adv_mod, "build_advisory_changed_context", lambda *a, **kw: (["file.py"], "pack", []))
    monkeypatch.setattr(adv_mod, "_build_advisory_prompt", lambda *a, **kw: "prompt")
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[], emit_progress_fn=lambda *_: None)

    adv_mod._run_claude_advisory(tmp_path, "msg", ctx)

    assert captured["effort"] == "low"


def test_paid_empty_advisory_result_is_error(monkeypatch, tmp_path):
    adv_mod = _get_advisory_module()
    from types import SimpleNamespace
    from ouroboros.gateways.claude_code import ClaudeCodeResult
    import ouroboros.gateways.claude_code as gw

    def fake_run_readonly(**kwargs):
        return ClaudeCodeResult(
            success=True,
            result_text="(no output)",
            session_id="sess-empty",
            cost_usd=1.23,
            usage={"prompt_tokens": 100, "completion_tokens": 0},
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(gw, "run_readonly", fake_run_readonly)
    monkeypatch.setattr(adv_mod, "_get_staged_diff", lambda *a, **kw: "diff")
    monkeypatch.setattr(adv_mod, "_get_changed_file_list", lambda *a, **kw: "M file.py")
    monkeypatch.setattr(adv_mod, "build_advisory_changed_context", lambda *a, **kw: (["file.py"], "pack", []))
    monkeypatch.setattr(adv_mod, "_build_advisory_prompt", lambda *a, **kw: "prompt")
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[], emit_progress_fn=lambda *_: None)

    items, raw, _model, _chars = adv_mod._run_claude_advisory(tmp_path, "msg", ctx)

    assert items == []
    assert raw.startswith("⚠️ ADVISORY_ERROR:")
    assert "paid empty output" in raw
    assert any(ev.get("type") == "advisory_sdk_suspect_result" for ev in ctx.pending_events)


def test_handle_advisory_error_persists_session_id(monkeypatch, tmp_path):
    adv_mod = _get_advisory_module()
    from types import SimpleNamespace
    from ouroboros.review_state import load_state

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(
        adv_mod,
        "_advisory_pre_sdk_gate",
        lambda **kwargs: ([], "M file.py", None),
    )

    def fake_run(*args, **kwargs):
        ctx = args[2]
        ctx._last_claude_advisory_meta = {"session_id": "sess-paid-empty"}
        return [], "⚠️ ADVISORY_ERROR: paid empty output", "claude-opus", 12345

    monkeypatch.setattr(adv_mod, "_run_claude_advisory", fake_run)
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[], emit_progress_fn=lambda *_: None, task_id="t")

    raw = adv_mod._handle_advisory_pre_review(ctx, commit_message="msg", skip_tests=True)
    result = json.loads(raw)

    assert result["status"] == "error"
    assert result["session_id"] == "sess-paid-empty"
    state = load_state(tmp_path)
    assert state.advisory_runs
    run = state.advisory_runs[-1]
    assert run.status == "error"
    assert run.session_id == "sess-paid-empty"
    assert run.prompt_chars == 12345


def test_skill_advisory_duplicate_expected_items_warn_not_error(monkeypatch, tmp_path):
    adv_mod = _get_advisory_module()
    from types import SimpleNamespace
    from ouroboros.gateways.claude_code import ClaudeCodeResult
    import ouroboros.gateways.claude_code as gw

    def fake_run_readonly(**kwargs):
        return ClaudeCodeResult(
            success=True,
            result_text=json.dumps([
                {"item": "manifest_schema", "verdict": "PASS", "reason": "ok", "severity": "critical"},
                {"item": "permissions_honesty", "verdict": "PASS", "reason": "ok", "severity": "critical"},
                {"item": "permissions_honesty", "verdict": "FAIL", "reason": "second issue", "severity": "critical"},
            ]),
            session_id="sess-duplicate",
            cost_usd=0.2,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(gw, "run_readonly", fake_run_readonly)
    monkeypatch.setattr(adv_mod, "_build_advisory_prompt", lambda *a, **kw: "prompt")
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[], emit_progress_fn=lambda *_: None)

    items, raw, _model, _chars = adv_mod._run_claude_advisory(
        tmp_path,
        "skill advisory",
        ctx,
        scope="plugin.py",
        options={
            "include_repo_diff": False,
            "review_surface": "skill",
            "expected_items": ["manifest_schema", "permissions_honesty"],
        },
    )

    assert len(items) == 3
    assert not raw.startswith("⚠️ ADVISORY_ERROR:")
    assert any(ev.get("type") == "advisory_contract_warning" for ev in ctx.pending_events)
    assert not any(ev.get("type") == "advisory_sdk_suspect_result" for ev in ctx.pending_events)


def test_skill_advisory_missing_expected_items_still_errors(monkeypatch, tmp_path):
    adv_mod = _get_advisory_module()
    from types import SimpleNamespace
    from ouroboros.gateways.claude_code import ClaudeCodeResult
    import ouroboros.gateways.claude_code as gw

    def fake_run_readonly(**kwargs):
        return ClaudeCodeResult(
            success=True,
            result_text='[{"item":"manifest_schema","verdict":"PASS","reason":"ok","severity":"critical"}]',
            session_id="sess-partial",
            cost_usd=0.2,
            usage={"prompt_tokens": 100, "completion_tokens": 20},
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(gw, "run_readonly", fake_run_readonly)
    monkeypatch.setattr(adv_mod, "_build_advisory_prompt", lambda *a, **kw: "prompt")
    ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, pending_events=[], emit_progress_fn=lambda *_: None)

    items, raw, _model, _chars = adv_mod._run_claude_advisory(
        tmp_path,
        "skill advisory",
        ctx,
        scope="plugin.py",
        options={
            "include_repo_diff": False,
            "review_surface": "skill",
            "expected_items": ["manifest_schema", "permissions_honesty"],
        },
    )

    assert items == []
    assert raw.startswith("⚠️ ADVISORY_ERROR:")
    assert "checklist contract mismatch" in raw
    assert any(ev.get("type") == "advisory_sdk_suspect_result" for ev in ctx.pending_events)


# ---------------------------------------------------------------------------
# Observability: _format_advisory_error / _get_runtime_diagnostics
# ---------------------------------------------------------------------------

def test_advisory_error_message_includes_diagnostic_fields():
    """_format_advisory_error must include all required diagnostic fields."""
    adv_mod = _get_advisory_module()
    diag = {
        "model": "opus",
        "sdk_version": "0.1.56",
        "cli_version": "2.1.92",
        "cli_path": "/app/claude",
        "python": "/usr/bin/python3",
        "prompt_chars": 120000,
        "prompt_tokens_approx": 30000,
        "touched_paths": ["ouroboros/tools/foo.py"],
    }
    msg = adv_mod._format_advisory_error(
        prefix="test failure",
        result_error="exit code 1",
        stderr_tail="some stderr line",
        session_id="sess-123",
        diag=diag,
    )
    assert "⚠️ ADVISORY_ERROR:" in msg
    assert "opus" in msg
    assert "0.1.56" in msg
    assert "2.1.92" in msg
    assert "/app/claude" in msg
    assert "120000" in msg
    assert "30000" in msg or "30,000" in msg
    assert "sess-123" in msg
    assert "some stderr line" in msg
    assert "ouroboros/tools/foo.py" in msg


def test_get_runtime_diagnostics_never_raises():
    """_get_runtime_diagnostics must return partial data on any error, never raise."""
    adv_mod = _get_advisory_module()
    diag = adv_mod._get_runtime_diagnostics("opus", 50000, ["file.py"])
    assert isinstance(diag, dict)
    assert diag["model"] == "opus"
    assert diag["prompt_chars"] == 50000
    assert diag["prompt_tokens_approx"] == 12500
    assert diag["touched_paths"] == ["file.py"]
    assert "sdk_version" in diag


def test_get_runtime_diagnostics_reads_runtime_state_attributes(monkeypatch):
    """Runtime diagnostics must read cli_path/cli_version from ClaudeRuntimeState attributes."""
    adv_mod = _get_advisory_module()
    from ouroboros.platform_layer import ClaudeRuntimeState

    monkeypatch.setattr(
        "ouroboros.platform_layer.resolve_claude_runtime",
        lambda: ClaudeRuntimeState(
            cli_path="/app/claude",
            cli_version="2.1.92",
        ),
    )
    diag = adv_mod._get_runtime_diagnostics("opus", 1234, ["file.py"])

    assert diag["cli_path"] == "/app/claude"
    assert diag["cli_version"] == "2.1.92"


# ---------------------------------------------------------------------------
# Budget gate: skip path and durable state
# ---------------------------------------------------------------------------

def _make_minimal_git_repo(tmp_path):
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "BIBLE.md").write_text("bible", encoding="utf-8")
    (tmp_path / "VERSION").write_text("5.99.0-rc.1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "5.99.0rc1"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[![Version 5.99.0-rc.1](https://img.shields.io/badge/version-5.99.0--rc.1-green.svg)](VERSION)\n\n"
        "## Version History\n\n| Version | Date | Description |\n|---------|------|-------------|\n"
        "| 5.99.0-rc.1 | 2026-05-16 | test row |\n",
        encoding="utf-8",
    )
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "CHECKLISTS.md").write_text("# Repo Commit Checklist\n", encoding="utf-8")
    (tmp_path / "docs" / "ARCHITECTURE.md").write_text(
        "# Ouroboros v5.99.0-rc.1 — Architecture & Reference\n",
        encoding="utf-8",
    )
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)


@pytest.fixture
def budget_gate_env(monkeypatch, tmp_path):
    """Pin the prompt budget to 10 chars and prepare a minimal env+repo.

    Yields ``(adv_mod, tmp_path)`` so callers can build their own ctx and
    invoke the advisory entrypoint they care about. Restores the original
    ``_ADVISORY_PROMPT_MAX_CHARS`` at teardown.
    """
    adv_mod = _get_advisory_module()
    _make_minimal_git_repo(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")
    original_limit = adv_mod._ADVISORY_PROMPT_MAX_CHARS
    adv_mod._ADVISORY_PROMPT_MAX_CHARS = 10
    try:
        yield adv_mod, tmp_path
    finally:
        adv_mod._ADVISORY_PROMPT_MAX_CHARS = original_limit


def _budget_ctx(tmp_path, *, task_id=None):
    from types import SimpleNamespace
    ctx_kwargs = {
        "repo_dir": tmp_path,
        "drive_root": tmp_path,
        "emit_progress_fn": lambda _: None,
        "pending_events": [],
    }
    if task_id is not None:
        ctx_kwargs["task_id"] = task_id
    return SimpleNamespace(**ctx_kwargs)


def test_advisory_budget_gate_returns_skipped_on_large_prompt(budget_gate_env):
    """_run_claude_advisory must return ADVISORY_SKIPPED when prompt exceeds budget gate."""
    adv_mod, tmp_path = budget_gate_env
    ctx = _budget_ctx(tmp_path)
    items, raw, _model, _chars = adv_mod._run_claude_advisory(tmp_path, "test commit", ctx)
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_SKIPPED:")
    assert "chars" in raw


def test_handle_advisory_pre_review_returns_skipped_status_on_budget_gate(budget_gate_env):
    """_handle_advisory_pre_review must surface ADVISORY_SKIPPED as status='skipped'."""
    adv_mod, tmp_path = budget_gate_env
    ctx = _budget_ctx(tmp_path, task_id="t-test")
    raw_json = adv_mod._handle_advisory_pre_review(ctx, commit_message="test commit")
    result = json.loads(raw_json)
    assert result["status"] == "skipped"
    assert "ADVISORY_SKIPPED" in result["message"]


def test_budget_gate_skip_persists_durable_state(budget_gate_env):
    """Budget-gate skip must write status='skipped' to state; is_fresh() must return True."""
    adv_mod, tmp_path = budget_gate_env
    ctx = _budget_ctx(tmp_path, task_id="t-bg")
    raw_json = adv_mod._handle_advisory_pre_review(ctx, commit_message="budget gate test")

    result = json.loads(raw_json)
    assert result["status"] == "skipped"
    snapshot_hash = result["snapshot_hash"]

    from ouroboros.review_state import load_state
    state = load_state(tmp_path)
    assert state.is_fresh(snapshot_hash), (
        "is_fresh() must be True after budget-gate skip so commit gate does not re-block"
    )
    run = state.find_by_hash(snapshot_hash)
    assert run is not None
    assert run.status == "skipped"


def test_next_step_guidance_for_skipped_advisory():
    """_next_step_guidance must return a distinct message for status='skipped' runs."""
    adv_mod = _get_advisory_module()
    from ouroboros.review_state import AdvisoryRunRecord, AdvisoryReviewState

    skipped_run = AdvisoryRunRecord(
        snapshot_hash="abc123",
        commit_message="test",
        status="skipped",
        ts="2026-01-01T00:00:00",
    )
    state = AdvisoryReviewState(advisory_runs=[skipped_run])
    msg = adv_mod._next_step_guidance(
        latest=skipped_run,
        state=state,
        stale_from_edit=False,
        stale_from_edit_ts=None,
        open_obs=[],
        open_debts=[],
        effective_is_fresh=True,
    )
    # Must NOT say "fresh" or "no critical findings" — that would mislead
    assert "skip" in msg.lower() or "budget" in msg.lower(), (
        "skipped advisory must produce a distinct message, not the generic fresh-pass message"
    )
    assert "commit_reviewed" in msg, "message should still indicate commit can proceed"


def test_next_step_guidance_requires_reaudit_when_obligations_remain():
    """Open obligations after a blocked review should trigger explicit re-audit guidance."""
    adv_mod = _get_advisory_module()
    from ouroboros.review_state import AdvisoryRunRecord, AdvisoryReviewState, ObligationItem

    fresh_run = AdvisoryRunRecord(
        snapshot_hash="abc123",
        commit_message="test",
        status="fresh",
        ts="2026-01-01T00:00:00",
    )
    state = AdvisoryReviewState(advisory_runs=[fresh_run])
    open_obs = [ObligationItem(
        obligation_id="ob-1",
        item="code_quality",
        severity="critical",
        reason="Need broader fix",
        source_attempt_ts="2026-01-01T00:00:01",
        source_attempt_msg="blocked",
        repo_key="repo",
    )]
    msg = adv_mod._next_step_guidance(
        latest=fresh_run,
        state=state,
        stale_from_edit=False,
        stale_from_edit_ts=None,
        open_obs=open_obs,
        open_debts=[],
        effective_is_fresh=True,
    )
    lowered = msg.lower()
    assert "re-read the full diff" in lowered
    assert "group obligations by root cause" in lowered
    assert "rewrite the plan" in lowered


def test_skipped_run_hash_mismatch_reported_as_stale(monkeypatch, tmp_path):
    """A skipped run with a different snapshot hash must be reported as stale (hash_mismatch path)."""
    adv_mod = _get_advisory_module()
    _make_minimal_git_repo(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")

    import subprocess
    # Commit BIBLE.md so git has a real HEAD
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test"], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"], capture_output=True)

    # Write a skipped run with a fake (stale) hash directly into state
    from ouroboros.review_state import (
        AdvisoryRunRecord, AdvisoryReviewState, load_state, save_state,
    )
    old_hash = "000000000000000000000000000000000000000000000000"
    run = AdvisoryRunRecord(
        snapshot_hash=old_hash,
        commit_message="skipped test",
        status="skipped",
        ts="2026-01-01T00:00:00",
    )
    state = AdvisoryReviewState(advisory_runs=[run])
    save_state(tmp_path, state)

    # Now add a file to the worktree so the real snapshot hash differs from old_hash
    (tmp_path / "new_file.py").write_text("x = 1\n", encoding="utf-8")

    # review_status must report stale (hash mismatch), not fresh
    raw_json = adv_mod._handle_review_status(
        ctx=__import__("types").SimpleNamespace(
            repo_dir=tmp_path, drive_root=tmp_path,
            emit_progress_fn=lambda _: None, pending_events=[],
        )
    )
    import json as _json
    result = _json.loads(raw_json)
    latest_status = result.get("latest_advisory_status", "")
    assert latest_status in ("stale", "no_advisory"), (
        f"Expected stale/no_advisory for skipped run with hash mismatch, got: {latest_status!r}\n"
        f"Full result: {result}"
    )


def test_advisory_context_build_failure_is_surfaced(monkeypatch, tmp_path):
    """Phase 4: changed-file context build failures must surface as explicit advisory errors."""
    adv_mod = _get_advisory_module()
    _make_minimal_git_repo(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")

    monkeypatch.setattr(adv_mod, "_get_staged_diff", lambda *args, **kwargs: "(no diff)")
    monkeypatch.setattr(adv_mod, "_get_changed_file_list", lambda *args, **kwargs: "M foo.py")
    monkeypatch.setattr(
        adv_mod,
        "build_advisory_changed_context",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("context pack exploded")),
    )

    from types import SimpleNamespace
    ctx = SimpleNamespace(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        emit_progress_fn=lambda _: None,
        pending_events=[],
        task_id="ctx-fail",
    )
    items, raw, _model, _chars = adv_mod._run_claude_advisory(tmp_path, "test commit", ctx)
    assert items == []
    assert raw.startswith("⚠️ ADVISORY_ERROR:")
    assert "failed to build advisory prompt" in raw


def test_budget_gate_skip_becomes_stale_after_edit(monkeypatch, tmp_path):
    """A budget-gate skip must be invalidated (marked stale) by a subsequent worktree edit."""
    adv_mod = _get_advisory_module()
    _make_minimal_git_repo(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "opus")

    original_limit = adv_mod._ADVISORY_PROMPT_MAX_CHARS
    try:
        adv_mod._ADVISORY_PROMPT_MAX_CHARS = 10
        from types import SimpleNamespace
        ctx = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path, task_id="t-stale",
                              emit_progress_fn=lambda _: None, pending_events=[])
        raw_json = adv_mod._handle_advisory_pre_review(ctx, commit_message="skip stale test")
    finally:
        adv_mod._ADVISORY_PROMPT_MAX_CHARS = original_limit

    result = json.loads(raw_json)
    assert result["status"] == "skipped"
    snapshot_hash = result["snapshot_hash"]

    # Simulate a worktree edit invalidating the advisory
    from ouroboros.review_state import load_state, mark_advisory_stale_after_edit
    mark_advisory_stale_after_edit(tmp_path)

    state = load_state(tmp_path)
    assert not state.is_fresh(snapshot_hash), (
        "is_fresh() must be False after mark_advisory_stale_after_edit() — edit invalidates skip"
    )
    run = state.find_by_hash(snapshot_hash)
    assert run is not None
    assert run.status == "stale"


# ---------------------------------------------------------------------------
# SDK break-after-ResultMessage fix (spurious exit code 1 prevention)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["readonly", "edit"])
def test_run_async_breaks_after_result_message(mode):
    """Both ``_run_readonly_async`` and ``_run_edit_async`` use
    ClaudeSDKClient.receive_response and must stop
    iterating after ResultMessage. Root cause: the SDK stream can raise
    when iterated past the ResultMessage because the CLI subprocess has
    exited and the message reader hits a closed pipe.

    The fix adds a ``break`` after processing ResultMessage. The test
    verifies that the break prevents the post-ResultMessage Exception
    from reaching the caller as a failure.

    Parametrized in v5.15.x — previously two near-identical tests
    test_run_{readonly,edit}_async_breaks_after_result_message, ~150 LOC
    of duplicated mock setup.
    """
    import sys

    sys.path.insert(0, REPO)

    # ---- Shared mock message types ------------------------------------
    AssistantMsg = type("AssistantMessage", (), {})
    ResultMsg = type("ResultMessage", (), {})

    class FakeTextBlock:
        def __init__(self, text):
            self.text = text

    text_payload = "Edit output" if mode == "edit" else "Hello"
    session_id = "edit-session-456" if mode == "edit" else "test-session-123"
    in_tokens = 20 if mode == "edit" else 10
    out_tokens = 10 if mode == "edit" else 5
    cost = 0.002 if mode == "edit" else 0.001

    class FakeAssistantMessage(AssistantMsg):
        def __init__(self):
            self.content = [FakeTextBlock(text_payload)]

    class FakeResultMessage(ResultMsg):
        pass

    FakeResultMessage.session_id = session_id
    FakeResultMessage.total_cost_usd = cost
    FakeResultMessage.usage = {"input_tokens": in_tokens, "output_tokens": out_tokens}
    FakeResultMessage.subtype = "success"

    import ouroboros.gateways.claude_code as gw

    class FakeClaudeAgentOptions:
        def __init__(self, **kwargs):
            pass

    class FakeHookMatcher:
        def __init__(self, **kwargs):
            pass

    orig_AssistantMessage = gw.AssistantMessage
    orig_ResultMessage = gw.ResultMessage
    orig_ClaudeAgentOptions = gw.ClaudeAgentOptions
    orig_ClaudeSDKClient = gw.ClaudeSDKClient
    orig_HookMatcher = gw.HookMatcher

    try:
        gw.AssistantMessage = FakeAssistantMessage
        gw.ResultMessage = FakeResultMessage
        gw.ClaudeAgentOptions = FakeClaudeAgentOptions
        gw.HookMatcher = FakeHookMatcher

        class FakeSDKClient:
            def __init__(self, options=None):
                self.options = options

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def query(self, prompt):
                pass

            async def receive_response(self):
                yield FakeAssistantMessage()
                yield FakeResultMessage()
                raise Exception(
                    "Command failed with exit code 1 (exit code: 1)\n"
                    "Error output: Check stderr output for details"
                )

        gw.ClaudeSDKClient = FakeSDKClient
        if mode == "readonly":
            result = asyncio.run(gw._run_readonly_async(
                prompt="test",
                cwd="/tmp",
                model="opus",
                max_turns=1,
                effort=None,
            ))
        else:
            result = asyncio.run(gw._run_edit_async(
                prompt="test edit",
                cwd="/tmp",
                model="opus",
                max_turns=1,
            ))
    finally:
        gw.AssistantMessage = orig_AssistantMessage
        gw.ResultMessage = orig_ResultMessage
        gw.ClaudeAgentOptions = orig_ClaudeAgentOptions
        gw.ClaudeSDKClient = orig_ClaudeSDKClient
        gw.HookMatcher = orig_HookMatcher

    assert result.success, f"Expected success but got error: {result.error}"
    assert result.session_id == session_id
    assert text_payload in result.result_text


@pytest.mark.parametrize(
    ("cwd", "expected_repo_name"),
    [
        ("", None),          # self repo root
        ("external", "external"),  # nested external git root
    ],
)
def test_claude_code_edit_invalidates_target_repo_root(monkeypatch, tmp_path, cwd, expected_repo_name):
    """Phase 3: claude_code_edit should invalidate advisory for the nearest git root."""
    from types import SimpleNamespace

    sys.path.insert(0, REPO)
    shell_mod = importlib.import_module("ouroboros.tools.shell")
    git_mod = importlib.import_module("ouroboros.tools.git")
    gw = importlib.import_module("ouroboros.gateways.claude_code")

    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    target_root = tmp_path
    if expected_repo_name:
        target_root = tmp_path / expected_repo_name
        (target_root / ".git").mkdir(parents=True, exist_ok=True)

    class FakeResult:
        def __init__(self):
            self.success = True
            self.result_text = "ok"
            self.session_id = "sess-1"
            self.cost_usd = 0.0
            self.usage = {}
            self.changed_files = []
            self.diff_stat = ""
            self.validation_summary = ""
            self.error = ""

        def to_tool_output(self):
            return json.dumps({"success": True})

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(gw, "resolve_claude_code_model", lambda: "opus")
    monkeypatch.setattr(gw, "run_edit", lambda **kwargs: FakeResult())
    monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: object())
    monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: None)
    monkeypatch.setattr(shell_mod, "_load_project_context", lambda repo_dir: "")
    monkeypatch.setattr(shell_mod, "_get_diff_stat", lambda repo_dir: "")
    monkeypatch.setattr(shell_mod, "run_cmd", lambda *args, **kwargs: "")

    change_calls = iter([[], ["foo.py"], ["foo.py"]])
    monkeypatch.setattr(shell_mod, "_get_changed_files", lambda repo_dir: next(change_calls))
    invalidate_calls = []
    monkeypatch.setattr(
        shell_mod,
        "_invalidate_advisory",
        lambda ctx, **kwargs: invalidate_calls.append(kwargs),
    )

    ctx = SimpleNamespace(
        repo_dir=tmp_path,
        drive_root=tmp_path,
        branch_dev="ouroboros",
        emit_progress_fn=lambda *_: None,
        pending_events=[],
    )

    raw = shell_mod._claude_code_edit(ctx, prompt="edit something", cwd=cwd)
    assert json.loads(raw)["success"] is True
    assert len(invalidate_calls) == 1
    mutation_root = invalidate_calls[0]["mutation_root"]
    assert mutation_root == target_root
    assert invalidate_calls[0]["source_tool"] == "claude_code_edit"
    assert invalidate_calls[0]["changed_paths"] == ["foo.py"]


# ---------------------------------------------------------------------------
# _parse_advisory_output — JSON array extraction heuristics
# ---------------------------------------------------------------------------

class TestIsChecklistArray:
    """Unit tests for the _is_checklist_array helper."""

    def setup_method(self, _=None):
        import importlib
        self.mod = importlib.import_module("ouroboros.tools.claude_advisory_review")
        self.fn = self.mod._is_checklist_array

    def test_empty_list_rejected(self):
        assert self.fn([]) is False

    def test_stray_int_array_rejected(self):
        assert self.fn([1, 2, 3]) is False

    def test_stray_string_array_rejected(self):
        assert self.fn(["a", "b"]) is False

    def test_dict_missing_item_rejected(self):
        assert self.fn([{"verdict": "PASS"}]) is False

    def test_dict_missing_verdict_rejected(self):
        assert self.fn([{"item": "bible_compliance"}]) is False

    def test_valid_single_item_accepted(self):
        assert self.fn([{"item": "bible_compliance", "verdict": "PASS"}]) is True

    def test_valid_multi_item_accepted(self):
        items = [
            {"item": "bible_compliance", "verdict": "PASS"},
            {"item": "code_quality", "verdict": "FAIL", "reason": "bug"},
        ]
        assert self.fn(items) is True

    def test_mixed_valid_invalid_rejected(self):
        # One bad element should disqualify the whole array
        items = [
            {"item": "bible_compliance", "verdict": "PASS"},
            {"not_item": "x"},
        ]
        assert self.fn(items) is False


class TestParseAdvisoryOutput:
    """Tests for the JSON array parser used to extract checklist items
    from advisory SDK output, including cases where code blocks contain
    brackets that could confuse a naïve find/rfind approach."""

    @pytest.fixture(autouse=True)
    def _import(self):
        ensure_claude_agent_sdk_mock()
        import importlib
        self.mod = importlib.import_module(
            "ouroboros.tools.claude_advisory_review"
        )

    def _parse(self, text: str) -> list:
        return self.mod._parse_advisory_output(text)

    def _item(self, item: str, verdict: str = "PASS") -> dict:
        return {"item": item, "verdict": verdict, "reason": "ok"}

    def test_plain_json_array(self):
        items = [self._item("bible_compliance")]
        text = json.dumps(items)
        assert self._parse(text) == items

    def test_json_after_prose(self):
        items = [self._item("secrets_check"), self._item("code_quality")]
        text = "Here is my analysis.\n\n" + json.dumps(items)
        assert self._parse(text) == items

    def test_json_after_code_block_with_brackets(self):
        """Code block containing '[' and ']' before the actual JSON array."""
        items = [self._item("bible_compliance", "PASS"), self._item("version_bump", "PASS")]
        code_block = (
            "```python\n"
            "result = [x for x in range(10)]\n"
            "nested = [[1, 2], [3, 4]]\n"
            "```\n\n"
        )
        text = "Let me think.\n" + code_block + "Final answer:\n" + json.dumps(items)
        result = self._parse(text)
        assert result == items

    def test_json_in_markdown_fence(self):
        items = [self._item("tests_affected")]
        text = "Review:\n```json\n" + json.dumps(items) + "\n```"
        assert self._parse(text) == items

    def test_empty_input(self):
        assert self._parse("") == []

    def test_no_json(self):
        assert self._parse("This is prose with no JSON.") == []

    def test_multiple_code_blocks_json_last(self):
        """Multiple code blocks followed by the JSON findings array — the
        real production scenario that caused parse_failure."""
        items = [
            self._item("bible_compliance"),
            self._item("code_quality"),
            self._item("version_bump"),
        ]
        text = (
            "Checking files...\n"
            "```python\n"
            "checks = [{'key': 'val'}, {'key2': [1, 2, 3]}]\n"
            "```\n"
            "More analysis with [inline] brackets and [another].\n"
            "Final findings:\n"
            + json.dumps(items)
        )
        result = self._parse(text)
        assert result == items

    def test_stray_array_after_real_checklist_returns_checklist(self):
        """When a real checklist array is followed by a stray unrelated array,
        the parser must return the checklist, not the stray array."""
        items = [
            self._item("bible_compliance"),
            self._item("code_quality"),
        ]
        # The stray [1,2,3] appears AFTER the real checklist — a bracket-scan
        # without shape validation would return [1,2,3] because rfind("]")
        # finds the last "]" which belongs to [1,2,3].
        text = json.dumps(items) + "\n\nSee also config option [1,2,3]."
        result = self._parse(text)
        assert result == items, (
            "Parser must prefer the checklist array over a later stray array"
        )

    def test_stray_int_array_alone_returns_empty(self):
        """A stray [1,2,3] with no real checklist must yield empty list (parse_failure)."""
        result = self._parse("some text [1,2,3] end")
        assert result == []


# ---------------------------------------------------------------------------
# LLM fallback extraction (_llm_extract_advisory_items)
# ---------------------------------------------------------------------------

class TestLLMFallbackExtraction:
    """Tests for the LLM-first parse-failure fallback in _run_claude_advisory."""

    @pytest.fixture(autouse=True)
    def _import(self):
        ensure_claude_agent_sdk_mock()
        import importlib
        self.mod = importlib.import_module("ouroboros.tools.claude_advisory_review")

    def _make_ctx(self):
        from types import SimpleNamespace
        return SimpleNamespace(
            repo_dir="/tmp",
            drive_root="/tmp",
            emit_progress_fn=lambda _: None,
            pending_events=[],
            task_id="fallback-test",
        )

    def _item(self, item: str, verdict: str = "PASS") -> dict:
        return {"item": item, "verdict": verdict, "reason": "ok"}

    def test_fallback_succeeds_when_direct_parse_fails(self, monkeypatch):
        """When _parse_advisory_output returns [] but raw_text has JSON,
        _llm_extract_advisory_items should return the checklist items."""
        expected = [self._item("bible_compliance"), self._item("code_quality")]

        # Patch LLMClient.chat to return the JSON as if the LLM extracted it
        def fake_chat(self_llm, messages, model, **kwargs):
            return {"content": json.dumps(expected)}, {"cost": 0.001}

        import ouroboros.llm as llm_mod
        monkeypatch.setattr(llm_mod.LLMClient, "chat", fake_chat)

        raw_text = (
            "I've reviewed the code carefully.\n"
            "Let me check each file...\n"
            "Here are my findings in JSON:\n"
            + json.dumps(expected)
        )
        result = self.mod._llm_extract_advisory_items(raw_text, self._make_ctx())
        assert result == expected

    def test_fallback_window_includes_tail_for_long_inputs(self, monkeypatch):
        """For inputs longer than head+tail budget, _build_fallback_window must preserve
        the TAIL (where JSON lives) and show a head excerpt + omission note — not a
        first-N truncation that would discard the JSON at the end."""
        head_chars = self.mod._FALLBACK_HEAD_CHARS
        tail_chars = self.mod._FALLBACK_TAIL_CHARS

        # Build a text that is bigger than the combined budget.
        # Head = 'A' * head_chars, middle filler, tail contains a distinct marker.
        tail_marker = "TAIL_JSON_MARKER"
        filler_size = head_chars + tail_chars + 10_000  # clearly over budget
        preamble = "A" * filler_size
        raw_text = preamble + tail_marker

        window = self.mod._build_fallback_window(raw_text)

        # Tail marker must be present
        assert tail_marker in window, "Tail (where JSON lives) must be included in the window"
        # Omission note must be present for middle-section awareness
        assert "OMISSION NOTE" in window, "Omission note must mark the skipped middle section"
        # Window must be shorter than full raw_text
        assert len(window) < len(raw_text)

    def test_resolve_fallback_model_no_env_uses_config_default(self, monkeypatch):
        """When OUROBOROS_MODEL_LIGHT is not set, fallback model comes from SETTINGS_DEFAULTS."""
        monkeypatch.delenv("OUROBOROS_MODEL_LIGHT", raising=False)

        model = self.mod._resolve_fallback_model()
        from ouroboros.config import SETTINGS_DEFAULTS
        expected = str(SETTINGS_DEFAULTS.get("OUROBOROS_MODEL_LIGHT", ""))
        assert model == expected, (
            f"Expected config default {expected!r}, got: {model!r}"
        )
        assert model, "Fallback model must be non-empty"

    def test_resolve_fallback_model_uses_env_var(self, monkeypatch):
        """OUROBOROS_MODEL_LIGHT must take priority over auto-detection."""
        monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-4o-mini")
        model = self.mod._resolve_fallback_model()
        assert model == "openai::gpt-4o-mini"

    def test_fallback_normalises_fail_without_severity_to_critical(self, monkeypatch):
        """FAIL items missing 'severity' from the LLM fallback must be normalised to 'critical'
        so _handle_advisory_pre_review() never silently downgrades blocking findings."""
        # Simulate LLM returning a FAIL item with no severity (schema-incomplete output)
        raw_items = [
            {"item": "code_quality", "verdict": "FAIL", "reason": "bug found"},
        ]

        def fake_chat(self_llm, messages, model, **kwargs):
            return {"content": json.dumps(raw_items)}, {"cost": 0.001}

        import ouroboros.llm as llm_mod
        monkeypatch.setattr(llm_mod.LLMClient, "chat", fake_chat)

        result = self.mod._llm_extract_advisory_items("narrative with no json", self._make_ctx())
        assert len(result) == 1
        assert result[0]["verdict"] == "FAIL"
        assert result[0]["severity"] == "critical", (
            "FAIL without severity must be normalised to 'critical' — not left empty"
        )

    def test_fallback_returns_empty_on_llm_failure(self, monkeypatch):
        """When the LLM call raises an exception, fallback must return [] gracefully."""
        import ouroboros.llm as llm_mod

        def fake_chat_raises(self_llm, messages, model, **kwargs):
            raise RuntimeError("API unavailable")

        monkeypatch.setattr(llm_mod.LLMClient, "chat", fake_chat_raises)

        result = self.mod._llm_extract_advisory_items("some narrative text", self._make_ctx())
        assert result == []

    def test_direct_parse_success_skips_fallback(self, monkeypatch):
        """When _parse_advisory_output succeeds, _llm_extract_advisory_items must NOT be called."""
        expected = [self._item("secrets_check"), self._item("version_bump")]

        fallback_called = []
        original_fallback = self.mod._llm_extract_advisory_items

        def tracking_fallback(raw_text, ctx):
            fallback_called.append(raw_text)
            return original_fallback(raw_text, ctx)

        monkeypatch.setattr(self.mod, "_llm_extract_advisory_items", tracking_fallback)

        # _run_claude_advisory calls _parse_advisory_output first; mock run_readonly
        # to return a clean JSON array (direct parse succeeds)
        import ouroboros.gateways.claude_code as gw
        from ouroboros.gateways.claude_code import ClaudeCodeResult

        def fake_run_readonly(**kwargs):
            return ClaudeCodeResult(
                success=True,
                result_text=json.dumps(expected),
                session_id="sess-test",
            )

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setattr(gw, "run_readonly", fake_run_readonly)
        monkeypatch.setattr(self.mod, "build_advisory_changed_context",
                            lambda *a, **kw: ([], "", set()))
        monkeypatch.setattr(self.mod, "_get_staged_diff",
                            lambda *a, **kw: "diff --git a/foo.py b/foo.py")
        monkeypatch.setattr(self.mod, "_get_changed_file_list",
                            lambda *a, **kw: "M foo.py")
        monkeypatch.setattr(self.mod, "_build_advisory_prompt",
                            lambda *a, **kw: "prompt text")

        import pathlib
        items, raw, _model, _chars = self.mod._run_claude_advisory(
            pathlib.Path("/tmp"), "test commit", self._make_ctx()
        )

        assert items == expected
        assert fallback_called == [], "Fallback must NOT be called when direct parse succeeds"

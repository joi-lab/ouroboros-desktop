"""Runtime mode core: settings/config plumbing + tool-registry gating.

Merged in v5.15.x from former ``test_runtime_mode.py`` (Phase 2 settings
plumbing: VALID_RUNTIME_MODES, get_runtime_mode clamp/case/default,
prepare_onboarding_settings validation, /api/state surface, web UI
substrings, /api/settings POST clamp + silent-drop) and
``test_runtime_mode_gating.py`` (ToolRegistry runtime-mode gating:
light blanket, advanced protected-path block, pro CORE_PATCH_NOTICE,
run_shell mutation detection across wrappers).

Security-critical self-elevation tests live in
``test_runtime_mode_elevation.py`` (kept as a separate file because of
its multi-vector attack matrix; both files together cover the full
runtime-mode surface).
"""
from __future__ import annotations

import ast
import os
import pathlib
import subprocess
import sys

import pytest

from ouroboros.runtime_mode_policy import protected_path_category
from ouroboros.tools.registry import ToolEntry, ToolRegistry

REPO = pathlib.Path(__file__).resolve().parent.parent


# ===========================================================================
# Part 1: config.py defaults + helpers + env propagation
# ===========================================================================


def test_settings_defaults_include_phase2_keys():
    from ouroboros.config import SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"] == "advanced"
    assert SETTINGS_DEFAULTS["OUROBOROS_SKILLS_REPO_PATH"] == ""


def test_valid_runtime_modes_is_frozen_tuple():
    from ouroboros.config import VALID_RUNTIME_MODES

    assert VALID_RUNTIME_MODES == ("light", "advanced", "pro")


@pytest.mark.parametrize("mode", ["light", "advanced", "pro"])
def test_get_runtime_mode_accepts_all_three(mode, monkeypatch):
    from ouroboros.config import get_runtime_mode

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
    assert get_runtime_mode() == mode


def test_get_runtime_mode_clamps_unknown_value(monkeypatch):
    from ouroboros.config import get_runtime_mode

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "ULTRA")
    assert get_runtime_mode() == "advanced"


def test_get_runtime_mode_is_case_insensitive(monkeypatch):
    from ouroboros.config import get_runtime_mode

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "Pro")
    assert get_runtime_mode() == "pro"


def test_get_runtime_mode_defaults_when_unset(monkeypatch):
    from ouroboros.config import get_runtime_mode

    monkeypatch.delenv("OUROBOROS_RUNTIME_MODE", raising=False)
    assert get_runtime_mode() == "advanced"


def test_get_skills_repo_path_defaults_to_empty(monkeypatch):
    from ouroboros.config import get_skills_repo_path

    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)
    assert get_skills_repo_path() == ""


def test_get_skills_repo_path_expands_home(monkeypatch):
    from ouroboros.config import get_skills_repo_path

    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", "~/Ouroboros/skills")
    expanded = get_skills_repo_path()
    assert expanded.startswith(os.path.expanduser("~"))
    assert expanded.endswith(os.path.join("Ouroboros", "skills"))


def test_apply_settings_to_env_propagates_phase2_keys(monkeypatch):
    from ouroboros.config import SETTINGS_DEFAULTS, apply_settings_to_env

    monkeypatch.delenv("OUROBOROS_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)

    settings = dict(SETTINGS_DEFAULTS)
    settings["OUROBOROS_RUNTIME_MODE"] = "light"
    settings["OUROBOROS_SKILLS_REPO_PATH"] = "/tmp/skills"

    apply_settings_to_env(settings)

    assert os.environ["OUROBOROS_RUNTIME_MODE"] == "light"
    assert os.environ["OUROBOROS_SKILLS_REPO_PATH"] == "/tmp/skills"


def test_normalize_runtime_mode_clamps_unknown_inputs():
    from ouroboros.config import normalize_runtime_mode

    assert normalize_runtime_mode("light") == "light"
    assert normalize_runtime_mode("ADVANCED") == "advanced"
    assert normalize_runtime_mode("Pro") == "pro"
    assert normalize_runtime_mode("turbo") == "advanced"
    assert normalize_runtime_mode("") == "advanced"
    assert normalize_runtime_mode(None) == "advanced"
    assert normalize_runtime_mode(123) == "advanced"


def test_load_settings_clamps_legacy_invalid_runtime_mode(tmp_path, monkeypatch):
    """Read-path normalization: a pre-existing settings.json containing
    an invalid runtime mode must be clamped at load time so /api/settings
    (GET) and the onboarding bootstrap cannot echo stale invalid values.
    """
    import importlib
    import json

    import ouroboros.config as cfg

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({
            "OUROBOROS_RUNTIME_MODE": "turbo",
            "OUROBOROS_SKILLS_REPO_PATH": "   ",
        }),
        encoding="utf-8",
    )

    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(settings_path))
    monkeypatch.delenv("OUROBOROS_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)

    try:
        cfg_reloaded = importlib.reload(cfg)
        loaded = cfg_reloaded.load_settings()
        assert loaded["OUROBOROS_RUNTIME_MODE"] == "advanced"
        assert loaded["OUROBOROS_SKILLS_REPO_PATH"] == ""
    finally:
        os.environ.pop("OUROBOROS_SETTINGS_PATH", None)
        importlib.reload(cfg)


# ===========================================================================
# Part 2: onboarding_wizard validation
# ===========================================================================


def _onboarding_payload_with_runtime(mode: str | None = None, skills_path: str = ""):
    from ouroboros.config import SETTINGS_DEFAULTS

    payload = {
        "OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 30,
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "TOTAL_BUDGET": 10,
        "OUROBOROS_PER_TASK_COST_USD": 20,
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_FILENAME": "",
        "LOCAL_MODEL_CONTEXT_LENGTH": SETTINGS_DEFAULTS["LOCAL_MODEL_CONTEXT_LENGTH"],
        "LOCAL_MODEL_N_GPU_LAYERS": -1,
        "LOCAL_MODEL_CHAT_FORMAT": "",
        "LOCAL_ROUTING_MODE": "cloud",
        "OUROBOROS_MODEL": "anthropic/claude-opus-4.6",
        "OUROBOROS_MODEL_CODE": "anthropic/claude-opus-4.6",
        "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4.6",
        "OUROBOROS_MODEL_FALLBACK": "anthropic/claude-sonnet-4.6",
        "OUROBOROS_SKILLS_REPO_PATH": skills_path,
    }
    if mode is not None:
        payload["OUROBOROS_RUNTIME_MODE"] = mode
    return payload


def test_prepare_onboarding_settings_defaults_runtime_mode_when_missing():
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode=None)
    prepared, error = prepare_onboarding_settings(payload, {})
    assert error is None, error
    assert prepared["OUROBOROS_RUNTIME_MODE"] == "advanced"
    assert prepared["OUROBOROS_SKILLS_REPO_PATH"] == ""


@pytest.mark.parametrize("mode", ["light", "advanced", "pro"])
def test_prepare_onboarding_settings_accepts_each_runtime_mode(mode):
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode=mode)
    prepared, error = prepare_onboarding_settings(payload, {})
    assert error is None, error
    assert prepared["OUROBOROS_RUNTIME_MODE"] == mode


def test_prepare_onboarding_settings_rejects_unknown_runtime_mode():
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode="turbo")
    prepared, error = prepare_onboarding_settings(payload, {})
    assert prepared == {}
    assert error is not None
    assert "runtime mode" in error.lower()


def test_prepare_onboarding_settings_persists_skills_repo_path():
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode="advanced", skills_path="~/skills-dev")
    prepared, error = prepare_onboarding_settings(payload, {})
    assert error is None, error
    assert prepared["OUROBOROS_SKILLS_REPO_PATH"] == "~/skills-dev"


def test_onboarding_bootstrap_exposes_runtime_mode():
    from ouroboros.onboarding_wizard import build_onboarding_html

    html = build_onboarding_html(
        {"OUROBOROS_RUNTIME_MODE": "pro", "OUROBOROS_SKILLS_REPO_PATH": "/opt/skills"}
    )
    assert '"runtimeMode": "pro"' in html
    assert '"skillsRepoPath": "/opt/skills"' in html


# ===========================================================================
# Part 3: server.py /api/state surfaces + TypedDict
# ===========================================================================


def test_api_state_declares_phase2_keys():
    tree = ast.parse((REPO / "ouroboros" / "gateway" / "state.py").read_text(encoding="utf-8"))
    api_state_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_state":
            api_state_fn = node
            break
    assert api_state_fn is not None

    for node in ast.walk(api_state_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "JSONResponse"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        keys = {
            k.value for k in node.args[0].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if keys == {"error"}:
            continue
        assert "runtime_mode" in keys
        assert "skills_repo_configured" in keys
        return
    raise AssertionError("api_state exposes no happy-path JSONResponse literal")


def test_state_response_typeddict_declares_phase2_keys():
    from ouroboros.gateway.contracts import StateResponse

    keys = set(StateResponse.__annotations__.keys())
    assert "runtime_mode" in keys
    assert "skills_repo_configured" in keys


# ===========================================================================
# Part 4: Web UI substrings
# ===========================================================================


def test_settings_ui_renders_runtime_mode_and_skills_path():
    src = (REPO / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    assert 'id="s-runtime-mode"' in src
    assert 'data-runtime-mode-group' in src
    for mode in ("light", "advanced", "pro"):
        assert f'data-effort-value="{mode}"' in src
    assert 'id="s-skills-repo-path"' in src


def test_settings_js_reads_and_writes_phase2_keys():
    src = (REPO / "web" / "modules" / "settings.js").read_text(encoding="utf-8")
    assert "OUROBOROS_RUNTIME_MODE" in src
    assert "OUROBOROS_SKILLS_REPO_PATH" in src
    assert "byId('s-runtime-mode').value = s.OUROBOROS_RUNTIME_MODE" in src
    assert "byId('s-skills-repo-path').value.trim()" in src


def test_onboarding_js_has_runtime_mode_selector_and_save_payload():
    src = (REPO / "web" / "modules" / "onboarding_wizard.js").read_text(encoding="utf-8")
    for mode in ("light", "advanced", "pro"):
        assert f'data-runtime-mode="{mode}"' in src
    assert "OUROBOROS_RUNTIME_MODE" in src
    assert "OUROBOROS_SKILLS_REPO_PATH" in src


def test_phase4_ui_copy_matches_shipped_runtime():
    settings_ui = (REPO / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    onboarding_js = (REPO / "web" / "modules" / "onboarding_wizard.js").read_text(encoding="utf-8")

    assert "Phase 2 plumbing only" not in settings_ui
    assert "land in Phase 3" not in settings_ui
    assert "data/skills/" in settings_ui
    assert "Pick both review enforcement and the initial runtime mode" in onboarding_js
    assert "normal triad + scope review" in onboarding_js
    assert "Phase 6+:" not in onboarding_js


def test_skills_ui_reads_live_extension_state_fields():
    src = (REPO / "web" / "modules" / "skills.js").read_text(encoding="utf-8")
    assert "live_loaded" in src
    assert "live_reason" in src
    assert "review_gate.executable_review" in src
    assert "executable_review" in src
    assert "skill.review_status === 'blockers' && !reviewReady(skill)" in src
    assert "function statusBadge(status, gate = null)" in src
    assert "statusBadge(skill.review_status, skill.review_gate)" in src
    assert "catalog only" in src
    assert "ui_tabs_pending" in src
    assert "result.error" in src


def test_onboarding_js_exposes_skills_repo_path_input_and_binding():
    src = (REPO / "web" / "modules" / "onboarding_wizard.js").read_text(encoding="utf-8")
    assert 'id="skills-repo-path"' in src
    assert 'data-clear="skills-repo-path"' in src
    assert "state.skillsRepoPath = skillsInput.value" in src
    assert "target === 'skills-repo-path'" in src


def test_onboarding_css_has_three_column_variant():
    src = (REPO / "web" / "onboarding.css").read_text(encoding="utf-8")
    assert ".wizard-choice-grid.three" in src


# ===========================================================================
# Part 5: /api/settings POST elevation + clamp behavior
# ===========================================================================


def test_api_settings_post_clamps_unknown_runtime_mode(tmp_path, monkeypatch):
    """POSTing an invalid runtime mode must be normalized to 'advanced'
    before save — so /api/settings and /api/state can never disagree."""
    import server as srv
    from starlette.testclient import TestClient
    from unittest.mock import patch

    saved: dict = {}

    def fake_load_settings():
        from ouroboros.config import SETTINGS_DEFAULTS
        out = dict(SETTINGS_DEFAULTS)
        out.update(saved)
        return out

    def fake_save_settings(payload, *, allow_elevation: bool = False):
        saved.clear()
        saved.update(payload)

    with patch.object(srv, "load_settings", side_effect=fake_load_settings), \
            patch.object(srv, "save_settings", side_effect=fake_save_settings), \
            patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None), \
            patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None), \
            patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])), \
            patch("ouroboros.server_auth.get_configured_network_password", return_value=""):
        client = TestClient(srv.app)
        resp = client.post(
            "/api/settings",
            json={"OUROBOROS_RUNTIME_MODE": "turbo"},
        )
        assert resp.status_code == 200, resp.text
        # /api/settings drops OUROBOROS_RUNTIME_MODE entirely — even invalid
        # inputs do not reach the body merge. The persisted value equals the
        # SETTINGS_DEFAULTS baseline ("advanced") via the belt-and-braces
        # revert in api_settings_post.
        assert saved["OUROBOROS_RUNTIME_MODE"] == "advanced"


def test_api_settings_post_silently_drops_runtime_mode_changes():
    """v5.1.2 elevation ratchet: even a VALID runtime_mode in the body
    is silently dropped — the API never accepts mode changes."""
    import server as srv
    from starlette.testclient import TestClient
    from unittest.mock import patch

    saved: dict = {}

    def fake_load_settings():
        from ouroboros.config import SETTINGS_DEFAULTS
        out = dict(SETTINGS_DEFAULTS)
        out["OUROBOROS_RUNTIME_MODE"] = "light"
        out.update(saved)
        return out

    def fake_save_settings(payload, *, allow_elevation: bool = False):
        saved.clear()
        saved.update(payload)

    with patch.object(srv, "load_settings", side_effect=fake_load_settings), \
            patch.object(srv, "save_settings", side_effect=fake_save_settings), \
            patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None), \
            patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None), \
            patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])), \
            patch("ouroboros.server_auth.get_configured_network_password", return_value=""):
        client = TestClient(srv.app)
        resp = client.post(
            "/api/settings",
            json={"OUROBOROS_RUNTIME_MODE": "pro", "OUROBOROS_SKILLS_REPO_PATH": "  /tmp/sk  "},
        )
        assert resp.status_code == 200, resp.text
        assert saved["OUROBOROS_RUNTIME_MODE"] == "light"
        assert saved["OUROBOROS_SKILLS_REPO_PATH"] == "/tmp/sk"


# ===========================================================================
# Part 6: ToolRegistry runtime-mode gating
# ===========================================================================


def _registry(tmp_path):
    return ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)


class _CommitCtx:
    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self.repo_dir = repo_dir
        self.drive_root = drive_root
        self.task_id = "runtime-mode-test"
        self._review_advisory = []
        self._last_triad_models = []
        self._last_scope_model = ""
        self._last_triad_raw_results = []
        self._last_scope_raw_result = {}
        self._review_degraded_reasons = []
        self._current_review_tool_name = "repo_commit"
        self._scope_review_history = {}
        self._review_history = []

    def emit_progress_fn(self, *_args, **_kwargs):
        return None

    def drive_logs(self):
        path = pathlib.Path(self.drive_root) / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("constitution\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


# ----- Light mode blanket block -----


@pytest.mark.parametrize("tool_name", [
    "repo_write",
    "repo_commit",
    "str_replace_editor",
    "claude_code_edit",
    "revert_commit",
    "pull_from_remote",
    "restore_to_head",
    "rollback_to_target",
    "promote_to_stable",
])
def test_light_mode_blocks_repo_mutation_tools(tool_name, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute(tool_name, {"path": "README.md"})
    assert "LIGHT_MODE_BLOCKED" in result, result[:200]


def test_light_mode_still_allows_read_only_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("repo_read", {"path": "README.md"})
    assert "LIGHT_MODE_BLOCKED" not in result


def test_light_mode_does_not_block_skill_exec_at_registry_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("skill_exec", {})
    assert "LIGHT_MODE_BLOCKED" not in result
    assert "SKILL_EXEC_BLOCKED" not in result


# ----- Advanced mode: protected core/contract/release surfaces -----


@pytest.mark.parametrize("path", [
    "ouroboros/safety.py",
    "ouroboros/contracts/plugin_api.py",
    "ouroboros/runtime_mode_policy.py",
    ".github/workflows/ci.yml",
])
def test_advanced_mode_blocks_protected_write(path, tmp_path, monkeypatch):
    """One parametrized test replaces three near-identical
    test_advanced_mode_blocks_{safety_critical,frozen_contract,
    runtime_policy_guardrail,release_invariant}_write variants."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": path, "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" in result


def test_dot_github_workflow_is_release_invariant():
    assert protected_path_category(".github/workflows/ci.yml") == "release-invariant"
    assert protected_path_category("./.github/workflows/ci.yml") == "release-invariant"


def test_advanced_mode_allows_non_critical_write_calls_through(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": "docs/README.md", "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" not in result
    assert "LIGHT_MODE_BLOCKED" not in result


# ----- Pro mode: protected edits allowed with CORE_PATCH_NOTICE -----


def test_pro_mode_allows_protected_write_with_core_patch_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": "ouroboros/safety.py", "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" not in result
    assert "CORE_PATCH_NOTICE" in result


def test_pro_mode_claude_code_edit_emits_core_patch_notice(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "ouroboros" / "contracts").mkdir(parents=True)
    (repo / "ouroboros" / "contracts" / "plugin_api.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "contracts"], cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path)

    def fake_edit(ctx, **_kwargs):
        (pathlib.Path(ctx.repo_dir) / "ouroboros" / "contracts" / "plugin_api.py").write_text(
            "new\n",
            encoding="utf-8",
        )
        return "edited"

    reg._entries["claude_code_edit"] = ToolEntry(
        name="claude_code_edit",
        schema={"name": "claude_code_edit"},
        handler=fake_edit,
    )

    result = reg.execute("claude_code_edit", {"prompt": "edit protected"})

    assert "edited" in result
    assert "CORE_PATCH_NOTICE" in result
    assert "ouroboros/contracts/plugin_api.py" in result


def test_advanced_commit_blocks_protected_staged_paths(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    (repo / "BIBLE.md").write_text("changed\n", encoding="utf-8")
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")

    result = git_mod._run_reviewed_stage_cycle(
        ctx,
        "test protected commit",
        0.0,
        paths=["BIBLE.md"],
        skip_advisory_pre_review=True,
    )

    assert result["status"] == "blocked"
    assert result["block_reason"] == "core_protection_blocked"
    assert "CORE_PROTECTION_BLOCKED" in result["message"]


def test_advanced_commit_blocks_rename_from_protected_path(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    subprocess.run(["git", "mv", "BIBLE.md", "BIBLE2.md"], cwd=repo, check=True)
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")

    result = git_mod._run_reviewed_stage_cycle(
        ctx,
        "rename protected file",
        0.0,
        skip_advisory_pre_review=True,
    )

    assert result["status"] == "blocked"
    assert result["block_reason"] == "core_protection_blocked"
    assert "BIBLE.md" in result["message"]


def test_pro_commit_uses_normal_review_for_protected_paths(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    (repo / "BIBLE.md").write_text("changed\n", encoding="utf-8")
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")

    calls = {"review": 0}

    def fake_review(*_args, **_kwargs):
        calls["review"] += 1
        return None, None, "", []

    monkeypatch.setattr(git_mod, "_run_parallel_review", fake_review)
    monkeypatch.setattr(git_mod, "_aggregate_review_verdict", lambda *a, **k: (False, None, "", [], []))

    result = git_mod._run_reviewed_stage_cycle(
        ctx,
        "test protected commit",
        0.0,
        paths=["BIBLE.md"],
        skip_advisory_pre_review=True,
    )

    assert result["status"] == "passed"
    assert calls == {"review": 1}


def test_restore_to_head_blocks_release_invariant_path(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "ci"], cwd=repo, check=True, capture_output=True)
    (repo / ".github" / "workflows" / "ci.yml").write_text("name: changed\n", encoding="utf-8")

    ctx = _CommitCtx(repo, tmp_path / "drive")
    result = git_mod._restore_to_head(ctx, confirm=True, paths=[".github/workflows/ci.yml"])

    assert "RESTORE_BLOCKED" in result
    assert ".github/workflows/ci.yml" in result


def test_restore_to_head_blocks_protected_rename_source(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    subprocess.run(["git", "mv", "BIBLE.md", "BIBLE2.md"], cwd=repo, check=True)

    ctx = _CommitCtx(repo, tmp_path / "drive")
    result = git_mod._restore_to_head(ctx, confirm=True)

    assert "RESTORE_BLOCKED" in result
    assert "BIBLE.md" in result


def test_revert_commit_blocks_protected_contract_path(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    (repo / "ouroboros" / "contracts").mkdir(parents=True)
    (repo / "ouroboros" / "contracts" / "plugin_api.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "contract"], cwd=repo, check=True, capture_output=True)
    target_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()

    ctx = _CommitCtx(repo, tmp_path / "drive")
    result = git_mod._revert_commit(ctx, target_sha, confirm=True)

    assert "REVERT_BLOCKED" in result
    assert "ouroboros/contracts/plugin_api.py" in result


# ----- run_shell mutation detection (light + advanced) -----


def test_light_mode_blocks_runshell_mutation(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": "git commit -m 'x'"})
    assert "GIT_VIA_SHELL_BLOCKED" in result


@pytest.mark.parametrize("cmd", [
    ["env", "git", "commit", "-m", "x"],
    ["/usr/bin/env", "git", "commit", "-m", "x"],
    ["/usr/bin/env", "-S", "git commit -m x"],
])
def test_run_shell_blocks_env_wrapped_git_mutation(cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": cmd})
    assert "GIT_VIA_SHELL_BLOCKED" in result


@pytest.mark.parametrize("cmd", [
    ["sh", "-c", "git commit -m x"],
    ["bash", "-c", "git add README.md && git commit -m x"],
])
def test_run_shell_blocks_shell_wrapped_git_mutation(cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": cmd})
    assert "GIT_VIA_SHELL_BLOCKED" in result


def test_advanced_mode_blocks_runshell_protected_python_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "run_shell",
        {"cmd": "python -c \"from pathlib import Path; Path('BIBLE.md').write_text('x')\""},
    )
    assert "SAFETY_VIOLATION" in result
    assert "BIBLE.md" in result


def test_advanced_mode_blocks_runshell_protected_backslash_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "run_shell",
        {"cmd": "python -c \"open('ouroboros\\\\contracts\\\\plugin_api.py','w').write('x')\""},
    )
    assert "SAFETY_VIOLATION" in result


def test_light_mode_allows_extension_tool_dispatch(tmp_path, monkeypatch):
    """v5.1.2 Frame A: ``light`` lets reviewed + enabled extension tools dispatch."""
    from ouroboros import extension_loader

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    tool_name = extension_loader.extension_surface_name("testskill", "echo")
    with extension_loader._lock:
        extension_loader._tools[tool_name] = {
            "name": tool_name,
            "handler": lambda ctx, **kwargs: "extension-tool-ran",
            "description": "echo",
            "schema": {},
            "timeout_sec": 10,
            "skill": "testskill",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_a, **_k: True)
    unloaded: list[str] = []
    monkeypatch.setattr(extension_loader, "unload_extension", unloaded.append)
    try:
        result = reg.execute(tool_name, {})
        assert "LIGHT_MODE_BLOCKED" not in result
        assert "extension-tool-ran" in result
        assert unloaded == []
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop(tool_name, None)


@pytest.mark.parametrize("bad_cmd", [
    "sed -i 's/foo/bar/' docs/README.md",
    "perl -i -pe 's/foo/bar/' docs/README.md",
    "truncate -s 0 docs/README.md",
    "chmod 755 docs/README.md",
    "chown anton docs/README.md",
    "ln -s /tmp/x docs/link",
])
def test_light_mode_blocks_inplace_mutation_tools(bad_cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": bad_cmd})
    assert "LIGHT_MODE_BLOCKED" in result, f"cmd={bad_cmd!r}: {result[:200]}"


@pytest.mark.parametrize("tool_name", [
    "fetch_pr_ref",
    "create_integration_branch",
    "cherry_pick_pr_commits",
    "stage_adaptations",
    "stage_pr_merge",
])
def test_light_mode_blocks_pr_integration_tools(tool_name, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute(tool_name, {})
    assert "LIGHT_MODE_BLOCKED" in result


def test_light_mode_allows_readonly_runshell(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": "git status"})
    assert "LIGHT_MODE_BLOCKED" not in result


@pytest.mark.parametrize("cmd", [
    "mkdir /tmp/ouroboros-light-mode-scratch",
    "touch /tmp/ouroboros-light-mode-scratch-file",
    "chmod +x /tmp/ouroboros-light-mode-scratch-file",
    "sed -i 's/foo/bar/' /tmp/ouroboros-light-mode-scratch-file",
    "chown nobody /tmp/ouroboros-light-mode-scratch-file",
    "cp README.md /tmp/ouroboros-light-mode-copy-out",
    "python3 -c \"open('/tmp/ouroboros-light-mode-scratch-file', 'r').read()\"",
])
def test_light_mode_allows_non_repo_shell_file_operations(cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": cmd})
    assert "LIGHT_MODE_BLOCKED" not in result, result[:200]


def test_advanced_mode_blocks_python_os_remove_protected_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": "python3 -c \"import os; os.remove('BIBLE.md')\""})
    assert "SAFETY_VIOLATION" in result


@pytest.mark.parametrize("cmd", [
    "sort -o BIBLE.md BIBLE.md",
    "uniq BIBLE.md BIBLE.md",
])
def test_run_shell_blocks_sort_uniq_protected_output_paths(cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": cmd})
    assert "SAFETY_VIOLATION" in result
    assert "BIBLE.md" in result or "protected" in result.lower()


@pytest.mark.parametrize("cmd", ["cat BIBLE.md", "git diff BIBLE.md", "du BIBLE.md"])
def test_run_shell_allows_readonly_mentions_of_protected_paths(cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": cmd})
    assert "SAFETY_VIOLATION" not in result


@pytest.mark.parametrize("cmd", [
    ["bash", "-c", "printf x > README.md"],
    ["sh", "-c", "touch README.md"],
])
def test_light_mode_blocks_simple_shell_c_repo_writer(cmd, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": cmd})
    assert "LIGHT_MODE_BLOCKED" in result


def test_light_mode_allows_shell_wrapper_non_repo_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": ["bash", "-c", "mkdir /tmp/ouroboros-light-wrapper"]})
    assert "LIGHT_MODE_BLOCKED" not in result, result[:200]


def test_light_mode_tripwire_catches_python_repo_writer(tmp_path, monkeypatch):
    import ouroboros.safety as safety_mod

    repo = _git_repo(tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **k: (True, ""))
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "drive")

    result = reg.execute(
        "run_shell",
        {"cmd": [sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('hacked\\n')"]},
    )

    assert "LIGHT_MODE_REPO_WRITE_BLOCKED" in result, result[:300]
    assert "README.md" in result
    assert (repo / "README.md").read_text(encoding="utf-8") == "hacked\n"


def test_light_mode_tripwire_catches_untracked_repo_file(tmp_path, monkeypatch):
    import ouroboros.safety as safety_mod

    repo = _git_repo(tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **k: (True, ""))
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "drive")

    result = reg.execute(
        "run_shell",
        {"cmd": [sys.executable, "-c", "from pathlib import Path; Path('new_tool.py').write_text('x\\n')"]},
    )

    assert "LIGHT_MODE_REPO_WRITE_BLOCKED" in result, result[:300]
    assert "new_tool.py" in result
    assert (repo / "new_tool.py").read_text(encoding="utf-8") == "x\n"


def test_light_mode_tripwire_runs_after_failed_command(tmp_path, monkeypatch):
    import ouroboros.safety as safety_mod

    repo = _git_repo(tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **k: (True, ""))
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "drive")

    result = reg.execute(
        "run_shell",
        {"cmd": [sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('bad\\n'); raise SystemExit(2)"]},
    )

    assert "LIGHT_MODE_REPO_WRITE_BLOCKED" in result, result[:300]
    assert "SHELL_EXIT_ERROR" in result


def test_advanced_mode_does_not_run_light_tripwire(tmp_path, monkeypatch):
    import ouroboros.safety as safety_mod

    repo = _git_repo(tmp_path)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **k: (True, ""))
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "drive")

    result = reg.execute(
        "run_shell",
        {"cmd": [sys.executable, "-c", "from pathlib import Path; Path('README.md').write_text('advanced\\n')"]},
    )

    assert "LIGHT_MODE_REPO_WRITE_BLOCKED" not in result, result[:300]


# ===========================================================================
# Part: light-mode bucket+skill_name short-form authoring (v5.16.0-rc.1)
# ===========================================================================
#
# Under runtime_mode=light, the three skill-payload edit tools — data_write,
# str_replace_editor, claude_code_edit — accept optional bucket+skill_name
# args that synthesize a skill_repair-like constraint at the gate (registry
# layer) and again at the handler layer. These tests pin the gate decision
# (LIGHT_MODE_BLOCKED present / absent) and the new error/help messages.
# Handler-internal path resolution is covered by handler-level tests
# (test_data_write_*, test_str_replace_editor_*, test_skill_repair_*).


def _register_fake(reg, tool_name, fake_handler):
    """Replace a registered ToolEntry's handler with ``fake_handler`` while
    preserving the registered schema. Lets gate tests observe what the gate
    decided without dragging in handler-internal side effects (network, FS)."""
    existing = reg._entries.get(tool_name)
    schema = existing.schema if existing else {"name": tool_name}
    is_code_tool = getattr(existing, "is_code_tool", False) if existing else False
    reg._entries[tool_name] = ToolEntry(
        name=tool_name,
        schema=schema,
        handler=fake_handler,
        is_code_tool=is_code_tool,
    )


def _make_skill_payload(tmp_path, bucket, name):
    """Create data/skills/<bucket>/<name>/plugin.py so resolve_skill_payload_target
    sees an existing payload root."""
    payload = tmp_path / "skills" / bucket / name
    payload.mkdir(parents=True)
    (payload / "plugin.py").write_text("def register(api):\n    pass\n", encoding="utf-8")
    return payload


@pytest.mark.parametrize("bucket", ["external", "clawhub", "ouroboroshub"])
def test_light_claude_code_edit_with_bucket_skill_name_allowed(bucket, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    _make_skill_payload(tmp_path, bucket, "alpha")
    reg = _registry(tmp_path)
    _register_fake(reg, "claude_code_edit", lambda ctx, **kw: "edit-ran")
    result = reg.execute(
        "claude_code_edit",
        {"prompt": "noop", "cwd": ".", "bucket": bucket, "skill_name": "alpha"},
    )
    assert "LIGHT_MODE_BLOCKED" not in result, result[:200]
    assert "edit-ran" in result


@pytest.mark.parametrize("bucket", ["external", "clawhub", "ouroboroshub"])
def test_light_str_replace_editor_with_bucket_skill_name_allowed(bucket, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    _make_skill_payload(tmp_path, bucket, "beta")
    reg = _registry(tmp_path)
    _register_fake(reg, "str_replace_editor", lambda ctx, **kw: "str-replace-ran")
    result = reg.execute(
        "str_replace_editor",
        {"path": "plugin.py", "old_str": "x", "new_str": "y", "bucket": bucket, "skill_name": "beta"},
    )
    assert "LIGHT_MODE_BLOCKED" not in result, result[:200]
    assert "str-replace-ran" in result


def test_light_data_write_with_bucket_skill_name_resolves_under_payload(tmp_path, monkeypatch):
    """data_write is NOT in _REPO_MUTATION_TOOLS, so the gate never sees it.
    Verify the handler-level synthesis still resolves the short path under
    data/skills/<bucket>/<skill>/ so a file lands inside the payload."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    _make_skill_payload(tmp_path, "external", "gamma")
    reg = _registry(tmp_path)
    result = reg.execute(
        "data_write",
        {
            "path": "lib/utils.py",
            "content": "def hi(): return 'ok'\n",
            "bucket": "external",
            "skill_name": "gamma",
        },
    )
    assert "DATA_WRITE_ERROR" not in result, result[:200]
    assert "DATA_WRITE_BLOCKED" not in result, result[:200]
    landed = tmp_path / "skills" / "external" / "gamma" / "lib" / "utils.py"
    assert landed.is_file(), f"expected file at {landed}; got result={result[:200]}"


def test_light_bucket_native_rejected_at_gate(tmp_path, monkeypatch):
    """bucket=native MUST not be honoured — launcher seed update lane stays
    authoritative. With the post-triad partial-args check in place, the gate
    surfaces the specific SKILL_PAYLOAD_ARG_ERROR (which lists `native excluded`)
    BEFORE the generic LIGHT_MODE_BLOCKED would fire — giving the agent a
    clearer signal."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute(
        "claude_code_edit",
        {"prompt": "noop", "cwd": ".", "bucket": "native", "skill_name": "anything"},
    )
    assert "SKILL_PAYLOAD_ARG_ERROR" in result, result[:200]
    assert "native excluded" in result


@pytest.mark.parametrize("tool_name,base_args", [
    ("data_write", {"path": "plugin.py", "content": "x"}),
    ("str_replace_editor", {"path": "plugin.py", "old_str": "a", "new_str": "b"}),
    ("claude_code_edit", {"prompt": "noop", "cwd": "plugin.py"}),
])
@pytest.mark.parametrize("partial", [
    {"bucket": "external"},
    {"skill_name": "alpha"},
    {"bucket": "native", "skill_name": "alpha"},
    {"bucket": "external", "skill_name": "...."},  # sanitizes to empty
])
def test_light_partial_args_surface_specific_error_not_generic_light_block(
    tool_name, base_args, partial, tmp_path, monkeypatch
):
    """Partial / invalid bucket+skill_name must yield a SPECIFIC actionable
    error before the generic LIGHT_MODE_BLOCKED. Triad reviewer round 1
    flagged the older test as codifying a weaker contract — this test pins
    the documented behaviour: ⚠️ SKILL_PAYLOAD_ARG_ERROR surfaces uniformly
    across all three payload-mutating tools, regardless of which partial
    shape the caller used."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    args = {**base_args, **partial}
    result = reg.execute(tool_name, args)
    assert "SKILL_PAYLOAD_ARG_ERROR" in result, (
        f"expected specific partial-args error for {tool_name} {partial!r}; "
        f"got: {result[:300]}"
    )
    assert "bucket and skill_name must be supplied together" in result, result[:300]


def test_light_control_plane_sidecar_still_blocked_with_bucket_skill_name(tmp_path, monkeypatch):
    """Even with a valid bucket+skill_name pair, the gate refuses control-plane
    sidecars (allow_control_plane=False is preserved). Same protection as repair
    mode — sidecar paths cannot be rewritten via generic tools."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    _make_skill_payload(tmp_path, "ouroboroshub", "delta")
    reg = _registry(tmp_path)
    result = reg.execute(
        "str_replace_editor",
        {
            "path": ".ouroboroshub.json",
            "old_str": "x",
            "new_str": "y",
            "bucket": "ouroboroshub",
            "skill_name": "delta",
        },
    )
    assert "LIGHT_MODE_BLOCKED" in result, result[:200]


def test_light_mode_blocked_message_lists_three_paths(tmp_path, monkeypatch):
    """LIGHT_MODE_BLOCKED message documents all three valid escape hatches so
    agents do not silently fall back to less-idiomatic tools."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("repo_write", {"path": "README.md", "content": "x"})
    assert "LIGHT_MODE_BLOCKED" in result, result[:200]
    assert "skill_repair" in result
    assert "data/skills/<bucket>" in result
    assert "bucket and skill_name" in result


# ===========================================================================
# Repair-mode confinement vs bucket+skill_name short-form (v5.16.0-rc.1
# adversarial-review round 1 finding: three independent critics flagged a
# cross-skill escape where an agent in heal mode for skill A could pass
# bucket+skill_name args pointing at skill B and have the synthesized
# constraint override the real heal task_constraint. These tests pin the
# precedence rule: real skill_repair task_constraint wins, mismatched
# bucket+skill_name args return ⚠️ SKILL_REDIRECT_BLOCKED before any
# resolution happens.)
# ===========================================================================


def _ctx_with_skill_repair(tmp_path, skill_name: str, bucket: str = "external"):
    """Build a minimal ToolRegistry whose ctx already carries a skill_repair
    task_constraint for ``skill_name``. Returns the registry."""
    from ouroboros.contracts.task_constraint import TaskConstraint

    reg = _registry(tmp_path)
    reg._ctx.task_constraint = TaskConstraint(
        mode="skill_repair",
        skill_name=skill_name,
        payload_root=f"skills/{bucket}/{skill_name}",
    )
    return reg


@pytest.mark.parametrize("tool_name,extra_args", [
    ("data_write", {"path": "plugin.py", "content": "evil-payload\n"}),
    ("str_replace_editor", {"path": "plugin.py", "old_str": "x", "new_str": "y"}),
    ("claude_code_edit", {"prompt": "noop", "cwd": "."}),
])
def test_repair_mode_blocks_cross_skill_redirect_via_bucket_skill_name(
    tool_name, extra_args, tmp_path, monkeypatch
):
    """If a heal task is active for alpha and the agent passes
    bucket+skill_name args naming a different skill bravo, the call must NOT
    silently write into bravo's payload. SKILL_REDIRECT_BLOCKED is the
    intended failure mode (registry-level + handler-level defense-in-depth)."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    _make_skill_payload(tmp_path, "external", "alpha")
    _make_skill_payload(tmp_path, "external", "bravo")
    reg = _ctx_with_skill_repair(tmp_path, "alpha")

    args = dict(extra_args)
    args["bucket"] = "external"
    args["skill_name"] = "bravo"
    result = reg.execute(tool_name, args)

    assert "SKILL_REDIRECT_BLOCKED" in result, (
        f"expected SKILL_REDIRECT_BLOCKED for {tool_name} with cross-skill "
        f"bucket+skill_name args under active skill_repair; got: {result[:200]}"
    )
    # Bravo's payload must remain untouched.
    bravo_plugin = tmp_path / "skills" / "external" / "bravo" / "plugin.py"
    assert not bravo_plugin.exists() or bravo_plugin.read_text(encoding="utf-8") == "def register(api):\n    pass\n", (
        f"unexpected write to bravo's payload: {bravo_plugin.read_text(encoding='utf-8')[:200]}"
    )


def test_repair_mode_matching_bucket_skill_name_is_silently_redundant(tmp_path, monkeypatch):
    """When bucket+skill_name match the active skill_repair task_constraint
    they are redundant but not erroneous — the call proceeds via the real TC,
    no SKILL_REDIRECT_BLOCKED. Real TC stays authoritative."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    _make_skill_payload(tmp_path, "external", "alpha")
    reg = _ctx_with_skill_repair(tmp_path, "alpha")

    result = reg.execute(
        "data_write",
        {
            "path": "extra.py",
            "content": "x\n",
            "bucket": "external",
            "skill_name": "alpha",
        },
    )

    assert "SKILL_REDIRECT_BLOCKED" not in result, result[:200]
    assert "DATA_WRITE_ERROR" not in result, result[:200]
    landed = tmp_path / "skills" / "external" / "alpha" / "extra.py"
    assert landed.is_file(), f"expected file at {landed}; got result={result[:200]}"


def test_synthesize_payload_constraint_unit():
    """Direct contract on the synthesis helper. Covers every branch so callers
    can rely on None == 'no short-form payload context'."""
    from ouroboros.contracts.skill_payload_policy import (
        SKILL_PAYLOAD_BUCKETS,
        synthesize_payload_constraint,
    )

    # Happy path — every allowed bucket.
    for bucket in SKILL_PAYLOAD_BUCKETS:
        tc = synthesize_payload_constraint(bucket, "weather")
        assert tc is not None
        assert tc.mode == "skill_repair"
        assert tc.skill_name == "weather"
        assert tc.payload_root == f"skills/{bucket}/weather"

    # Native is excluded — launcher seed update lane stays authoritative.
    assert synthesize_payload_constraint("native", "anything") is None

    # Unknown bucket.
    assert synthesize_payload_constraint("notabucket", "weather") is None

    # Empty / whitespace inputs.
    assert synthesize_payload_constraint("", "weather") is None
    assert synthesize_payload_constraint("external", "") is None
    assert synthesize_payload_constraint("   ", "weather") is None

    # Name that sanitizes away to nothing.
    assert synthesize_payload_constraint("external", "....") is None
    assert synthesize_payload_constraint("external", "/") is None
    assert synthesize_payload_constraint("external", "__omit__") is None

    # Sanitizer normalises odd input but still returns a usable constraint.
    tc = synthesize_payload_constraint("external", "weather/v2")
    assert tc is not None and tc.skill_name == "weather_v2"


def test_repo_path_wins_over_stale_bucket_skill_name(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    drive = tmp_path / "drive"
    (drive / "skills" / "external" / "alpha").mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)

    result = reg.execute(
        "str_replace_editor",
        {
            "path": "README.md",
            "old_str": "ok",
            "new_str": "repo-ok",
            "bucket": "external",
            "skill_name": "alpha",
        },
    )

    assert "Replaced" in result, result[:300]
    assert "SKILL_SHORT_FORM_IGNORED" in result
    assert (repo / "README.md").read_text(encoding="utf-8") == "repo-ok\n"
    assert not (drive / "skills" / "external" / "alpha" / "README.md").exists()


def test_data_settings_path_wins_over_stale_bucket_skill_name(tmp_path, monkeypatch):
    from ouroboros import config as cfg

    drive = tmp_path / "drive"
    repo = tmp_path / "repo"
    repo.mkdir()
    (drive / "skills" / "external" / "alpha").mkdir(parents=True)
    (drive / "settings.json").write_text('{"TOTAL_BUDGET": 10}\n', encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setattr(cfg, "DATA_DIR", drive)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", drive / "settings.json")
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)

    result = reg.execute(
        "data_write",
        {
            "path": "settings.json",
            "content": "{}\n",
            "bucket": "external",
            "skill_name": "alpha",
        },
    )

    assert "DATA_WRITE_BLOCKED" in result, result[:300]
    assert not (drive / "skills" / "external" / "alpha" / "settings.json").exists()
    assert (drive / "settings.json").read_text(encoding="utf-8") == '{"TOTAL_BUDGET": 10}\n'


def test_data_settings_case_variant_wins_over_stale_bucket_skill_name(tmp_path, monkeypatch):
    from ouroboros import config as cfg

    drive = tmp_path / "drive"
    repo = tmp_path / "repo"
    repo.mkdir()
    (drive / "skills" / "external" / "alpha").mkdir(parents=True)
    (drive / "settings.json").write_text('{"TOTAL_BUDGET": 10}\n', encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setattr(cfg, "DATA_DIR", drive)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", drive / "settings.json")
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)

    result = reg.execute(
        "data_write",
        {
            "path": "Settings.json",
            "content": "{}\n",
            "bucket": "external",
            "skill_name": "alpha",
        },
    )

    assert "DATA_WRITE_BLOCKED" in result, result[:300]
    assert not (drive / "skills" / "external" / "alpha" / "Settings.json").exists()


def test_explicit_data_skills_path_wins_over_stale_bucket_skill_name(tmp_path, monkeypatch):
    drive = tmp_path / "drive"
    repo = tmp_path / "repo"
    repo.mkdir()
    skill = drive / "skills" / "external" / "alpha"
    skill.mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=repo, drive_root=drive)

    result = reg.execute(
        "data_write",
        {
            "path": "data/skills/external/alpha/plugin.py",
            "content": "VALUE = 1\n",
            "bucket": "external",
            "skill_name": "alpha",
        },
    )

    assert "DATA_WRITE_ERROR" not in result, result[:300]
    assert "SKILL_SHORT_FORM_IGNORED" in result
    assert (skill / "plugin.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert not (drive / "data" / "skills" / "external" / "alpha" / "plugin.py").exists()


def test_short_form_requires_existing_payload_root(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "drive")
    (tmp_path / "repo").mkdir()

    result = reg.execute(
        "str_replace_editor",
        {
            "path": "plugin.py",
            "old_str": "x",
            "new_str": "y",
            "bucket": "external",
            "skill_name": "ghost",
        },
    )

    assert "skill payload not found" in result, result[:300]


def test_cross_skill_redirect_error_unit():
    """The helper that produces SKILL_REDIRECT_BLOCKED text. Empty string means
    'no conflict, proceed'; non-empty means 'reject the call'."""
    from ouroboros.contracts.skill_payload_policy import (
        cross_skill_redirect_error,
        synthesize_payload_constraint,
    )
    from ouroboros.contracts.task_constraint import TaskConstraint

    alpha_tc = TaskConstraint(
        mode="skill_repair", skill_name="alpha", payload_root="skills/external/alpha"
    )
    bravo_synth = synthesize_payload_constraint("external", "bravo")
    alpha_synth = synthesize_payload_constraint("external", "alpha")

    # Mismatched names → non-empty redirect message.
    err = cross_skill_redirect_error(alpha_tc, bravo_synth)
    assert err and "alpha" in err and "bravo" in err

    # Matching names → empty (redundant, not erroneous).
    assert cross_skill_redirect_error(alpha_tc, alpha_synth) == ""

    # No active TC → no redirect possible.
    assert cross_skill_redirect_error(None, bravo_synth) == ""

    # No synth → nothing to redirect.
    assert cross_skill_redirect_error(alpha_tc, None) == ""

    # Existing TC of a different mode (hypothetical future) → not skill_repair,
    # so no confinement to enforce here.
    other_mode = TaskConstraint(mode="other", skill_name="alpha", payload_root="skills/external/alpha")
    assert cross_skill_redirect_error(other_mode, bravo_synth) == ""

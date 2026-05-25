"""Tests for ouroboros.context health invariants."""

from __future__ import annotations

import json
import pathlib
import tempfile

from ouroboros.context import build_health_invariants, build_runtime_section


class TestCacheHitRateInvariant:
    def _make_env(self, tmp_path, events_lines):
        class FakeEnv:
            def drive_path(self, p):
                return tmp_path / p
            def repo_path(self, p):
                return tmp_path / "repo" / p
            @property
            def repo_dir(self):
                return tmp_path / "repo"
            @property
            def drive_root(self):
                return tmp_path

        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
        (tmp_path / "repo" / "web").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "web" / "package.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
        (tmp_path / "repo" / "README.md").write_text('version-1.2.3', encoding="utf-8")
        (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text('# Ouroboros v1.2.3', encoding="utf-8")
        (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text('# Dev', encoding="utf-8")
        (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
        (tmp_path / "memory" / "identity.md").write_text('x' * 300, encoding="utf-8")
        (tmp_path / "memory" / "scratchpad.md").write_text('x' * 300, encoding="utf-8")
        (tmp_path / "logs" / "events.jsonl").write_text("\n".join(events_lines) + "\n", encoding="utf-8")
        return FakeEnv()

    def test_cache_hit_rate_good(self, tmp_path):
        lines = [json.dumps({"type": "llm_round", "prompt_tokens": 1000, "cached_tokens": 600}) for _ in range(15)]
        env = self._make_env(tmp_path, lines)
        result = build_health_invariants(env)
        assert "cache hit rate" in result.lower()
        assert "60%" in result or "60.0%" in result

    def test_cache_hit_rate_warning_below_30(self, tmp_path):
        lines = [json.dumps({"type": "llm_round", "prompt_tokens": 1000, "cached_tokens": 200}) for _ in range(15)]
        env = self._make_env(tmp_path, lines)
        result = build_health_invariants(env)
        assert "LOW CACHE HIT RATE" in result


def _make_health_env(tmp_path, events_lines=None):
    class FakeEnv:
        def drive_path(self, p):
            return tmp_path / p

        def repo_path(self, p):
            return tmp_path / "repo" / p

        @property
        def repo_dir(self):
            return tmp_path / "repo"

        @property
        def drive_root(self):
            return tmp_path

    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "archive" / "rescue").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
    (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
    (tmp_path / "repo" / "web").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "web" / "package.json").write_text('{"version": "1.2.3"}', encoding="utf-8")
    (tmp_path / "repo" / "README.md").write_text('version-1.2.3', encoding="utf-8")
    (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text('# Ouroboros v1.2.3', encoding="utf-8")
    (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text('# Dev', encoding="utf-8")
    (tmp_path / "repo" / "prompts" / "CONSCIOUSNESS.md").write_text('Prompt text', encoding="utf-8")
    (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
    (tmp_path / "memory" / "identity.md").write_text('x' * 300, encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text('x' * 300, encoding="utf-8")
    event_lines = events_lines or []
    (tmp_path / "logs" / "events.jsonl").write_text("\n".join(event_lines) + ("\n" if event_lines else ""), encoding="utf-8")
    return FakeEnv()


def test_runtime_section_includes_light_runtime_mode_rule(tmp_path, monkeypatch):
    env = _make_health_env(tmp_path)
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "light")
    section = build_runtime_section(env, {"id": "task-1", "type": "task"})
    payload = json.loads(section.split("\n\n", 1)[1])

    assert payload["runtime_mode"] == "light"
    assert "forbids Ouroboros repo mutation" in payload["runtime_mode_rule"]


def test_runtime_section_omits_light_rule_for_advanced(tmp_path, monkeypatch):
    env = _make_health_env(tmp_path)
    monkeypatch.setattr("ouroboros.config.get_runtime_mode", lambda: "advanced")
    section = build_runtime_section(env, {"id": "task-1", "type": "task"})
    payload = json.loads(section.split("\n\n", 1)[1])

    assert payload["runtime_mode"] == "advanced"
    assert "runtime_mode_rule" not in payload


class TestFileSizeBudgetHealthInvariant:
    def _make_env(self, tmp_path, development_text: str):
        class FakeEnv:
            def drive_path(self, p):
                return tmp_path / p
            def repo_path(self, p):
                return tmp_path / "repo" / p
            @property
            def repo_dir(self):
                return tmp_path / "repo"
            @property
            def drive_root(self):
                return tmp_path

        (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text(development_text, encoding="utf-8")
        (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
        (tmp_path / "repo" / "README.md").write_text('version-1.2.3', encoding="utf-8")
        (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text('# Ouroboros v1.2.3', encoding="utf-8")
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
        (tmp_path / "memory" / "identity.md").write_text('x' * 300, encoding="utf-8")
        (tmp_path / "memory" / "scratchpad.md").write_text('x' * 300, encoding="utf-8")
        return FakeEnv()

    def test_warns_when_memory_file_nears_budget(self, tmp_path):
        dev = """
### File Size Budgets
| Path | Budget chars |
|------|--------------|
| memory/identity.md | 1000 |
### Next Section
"""
        env = self._make_env(tmp_path, dev)
        (tmp_path / "memory" / "identity.md").write_text("x" * 950, encoding="utf-8")
        result = build_health_invariants(env)
        assert "FILE SIZE NEAR BUDGET" in result
        assert "memory/identity.md" in result

    def test_warns_when_prompt_file_exceeds_budget(self, tmp_path):
        dev = """
### File Size Budgets
| Path | Budget chars |
|------|--------------|
| prompts/SYSTEM.md | 1000 |
### Next Section
"""
        env = self._make_env(tmp_path, dev)
        (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "prompts" / "SYSTEM.md").write_text("y" * 1200, encoding="utf-8")
        result = build_health_invariants(env)
        assert "FILE SIZE BUDGET EXCEEDED" in result
        assert "prompts/SYSTEM.md" in result


class TestAdditionalHealthInvariantCoverage:
    def test_version_desync_warning(self, tmp_path):
        env = _make_health_env(tmp_path)
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.4"', encoding="utf-8")

        result = build_health_invariants(env)
        assert "VERSION DESYNC" in result
        assert "pyproject.toml=1.2.4" in result

    def test_web_package_version_desync_warning(self, tmp_path):
        env = _make_health_env(tmp_path)
        (tmp_path / "repo" / "web" / "package.json").write_text('{"version": "1.2.4"}', encoding="utf-8")

        result = build_health_invariants(env)
        assert "VERSION DESYNC" in result
        assert "web/package.json=1.2.4" in result

    def test_rc_pep440_pyproject_does_not_warn(self, tmp_path):
        env = _make_health_env(tmp_path)
        (tmp_path / "repo" / "VERSION").write_text("4.50.0-rc.2", encoding="utf-8")
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "4.50.0rc2"', encoding="utf-8")
        (tmp_path / "repo" / "web" / "package.json").write_text('{"version": "4.50.0-rc.2"}', encoding="utf-8")
        (tmp_path / "repo" / "README.md").write_text(
            "[![Version 4.50.0-rc.2](https://img.shields.io/badge/version-4.50.0--rc.2-green.svg)](VERSION)",
            encoding="utf-8",
        )
        (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text(
            "# Ouroboros v4.50.0-rc.2",
            encoding="utf-8",
        )

        result = build_health_invariants(env)
        assert "VERSION DESYNC" not in result

    def test_rc_badge_url_mismatch_warns(self, tmp_path):
        env = _make_health_env(tmp_path)
        (tmp_path / "repo" / "VERSION").write_text("4.50.0-rc.2", encoding="utf-8")
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "4.50.0rc2"', encoding="utf-8")
        (tmp_path / "repo" / "web" / "package.json").write_text('{"version": "4.50.0-rc.2"}', encoding="utf-8")
        (tmp_path / "repo" / "README.md").write_text(
            "[![Version 4.50.0-rc.2](https://img.shields.io/badge/version-4.50.0-rc.2-green.svg)](VERSION)",
            encoding="utf-8",
        )
        (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text(
            "# Ouroboros v4.50.0-rc.2",
            encoding="utf-8",
        )

        result = build_health_invariants(env)
        assert "VERSION DESYNC" in result
        assert "README badge URL token" in result

    def test_duplicate_processing_warning(self, tmp_path):
        env = _make_health_env(tmp_path)
        (tmp_path / "logs" / "events.jsonl").write_text(
            json.dumps({
                "type": "owner_message_injected",
                "text": "same message",
                "task_id": "task-a",
            }) + "\n",
            encoding="utf-8",
        )
        (tmp_path / "logs" / "supervisor.jsonl").write_text(
            json.dumps({
                "event_type": "owner_message_injected",
                "text": "same message",
                "task_id": "task-b",
            }) + "\n",
            encoding="utf-8",
        )

        result = build_health_invariants(env)
        assert "DUPLICATE PROCESSING" in result
        assert "task-a" in result
        assert "task-b" in result

    def test_provider_and_overflow_warnings(self, tmp_path):
        env = _make_health_env(
            tmp_path,
            events_lines=[
                json.dumps({"type": "llm_api_error", "model": "openai/gpt-5.5"}),
                json.dumps({"type": "local_context_overflow", "model": "local/qwen"}),
            ],
        )

        result = build_health_invariants(env)
        assert "PROVIDER/ROUTING ERRORS" in result
        assert "openai/gpt-5.5 x1" in result
        assert "LOCAL CONTEXT OVERFLOW" in result
        assert "local/qwen x1" in result

    def test_rescue_snapshot_warning(self, tmp_path):
        env = _make_health_env(tmp_path)
        rescue_dir = tmp_path / "archive" / "rescue" / "2026-04-14-test"
        rescue_dir.mkdir(parents=True, exist_ok=True)
        (rescue_dir / "rescue_meta.json").write_text("{}", encoding="utf-8")
        (rescue_dir / "changes.diff").write_text("diff", encoding="utf-8")

        result = build_health_invariants(env)
        assert "RESCUE SNAPSHOT AVAILABLE" in result
        assert "2026-04-14-test" in result


class TestAdvisoryReviewStatusInContext:
    """Tests that advisory review status appears in LLM context when runs exist."""

    def _make_env(self, tmp_path):
        class FakeEnv:
            def drive_path(self, p):
                return tmp_path / p
            def repo_path(self, p):
                return tmp_path / "repo" / p
            @property
            def repo_dir(self):
                return tmp_path / "repo"
            @property
            def drive_root(self):
                return tmp_path

        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
        (tmp_path / "repo" / "README.md").write_text('version-1.2.3', encoding="utf-8")
        (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text('# Ouroboros v1.2.3', encoding="utf-8")
        (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text('# Dev', encoding="utf-8")
        (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
        (tmp_path / "memory" / "identity.md").write_text('x' * 300, encoding="utf-8")
        (tmp_path / "memory" / "scratchpad.md").write_text('x' * 300, encoding="utf-8")
        return FakeEnv()

    def test_advisory_status_in_build_llm_messages(self, tmp_path):
        """format_status_section returns non-empty string when runs exist."""
        from ouroboros.review_state import (
            AdvisoryReviewState, AdvisoryRunRecord, save_state, format_status_section
        )
        state = AdvisoryReviewState()
        state.add_run(AdvisoryRunRecord(
            snapshot_hash="abc123",
            commit_message="test commit",
            status="fresh",
            ts="2026-01-01T00:00:00",
            items=[{"item": "bible_compliance", "verdict": "PASS", "severity": "critical", "reason": "ok"}],
        ))
        save_state(tmp_path, state)

        loaded = __import__("ouroboros.review_state", fromlist=["load_state"]).load_state(tmp_path)
        section = format_status_section(loaded)
        assert "Advisory Pre-Review Status" in section
        assert "FRESH" in section
        assert "abc123" in section

    def test_advisory_status_empty_when_no_runs(self, tmp_path):
        """format_status_section returns 'No advisory runs' when state is empty."""
        from ouroboros.review_state import AdvisoryReviewState, format_status_section
        state = AdvisoryReviewState()
        section = format_status_section(state)
        assert "No advisory runs" in section

    def test_review_continuity_context_surfaces_live_gate_and_continuation(self, tmp_path):
        from ouroboros.agent_task_pipeline import build_review_context
        from ouroboros.context import build_llm_messages
        from ouroboros.memory import Memory
        from ouroboros.review_state import (
            AdvisoryReviewState,
            AdvisoryRunRecord,
            CommitAttemptRecord,
            compute_snapshot_hash,
            make_repo_key,
            save_state,
        )
        from ouroboros.task_continuation import ReviewContinuation, save_review_continuation
        from ouroboros.task_results import STATUS_COMPLETED, write_task_result

        env = self._make_env(tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "prompts" / "SYSTEM.md").write_text("System", encoding="utf-8")
        (tmp_path / "repo" / "BIBLE.md").write_text("Bible", encoding="utf-8")
        (tmp_path / "repo" / "docs" / "CHECKLISTS.md").write_text("Checklist", encoding="utf-8")
        (tmp_path / "repo" / "tracked.py").write_text("print('hi')\n", encoding="utf-8")

        repo_key = make_repo_key(tmp_path / "repo")
        snapshot_hash = compute_snapshot_hash(tmp_path / "repo")
        state = AdvisoryReviewState()
        state.add_run(AdvisoryRunRecord(
            snapshot_hash=snapshot_hash,
            commit_message="test commit",
            status="bypassed",
            ts="2026-04-07T09:59:00+00:00",
            repo_key=repo_key,
            bypass_reason="manual audit override",
        ))
        state.advisory_runs[-1].status = "stale"
        state.last_stale_from_edit_ts = "2026-04-07T10:00:00+00:00"
        state.last_stale_reason = "claude_code_edit mutated tracked.py"
        state.last_stale_repo_key = repo_key
        state.record_attempt(CommitAttemptRecord(
            ts="2026-04-07T10:01:00+00:00",
            commit_message="blocked commit",
            status="blocked",
            repo_key=repo_key,
            tool_name="repo_commit",
            task_id="task-old",
            attempt=1,
            critical_findings=[{
                "item": "tests_affected",
                "reason": "Fix the failing test before commit",
                "severity": "critical",
                "verdict": "FAIL",
            }],
            readiness_warnings=["Review was blocked and needs follow-up."],
        ))
        save_state(tmp_path, state)

        save_review_continuation(
            tmp_path,
            ReviewContinuation(
                task_id="task-old",
                source="blocked_review",
                stage="blocking_review",
                repo_key=repo_key,
                tool_name="repo_commit",
                attempt=1,
                block_reason="critical_findings",
                critical_findings=[{
                    "item": "tests_affected",
                    "reason": "Fix the failing test before commit",
                    "severity": "critical",
                    "verdict": "FAIL",
                }],
                readiness_warnings=["Review was blocked and needs follow-up."],
            ),
            expect_task_id="task-old",
        )
        write_task_result(
            tmp_path,
            "task-old",
            STATUS_COMPLETED,
            result="Commit blocked by review.",
        )

        messages, _ = build_llm_messages(
            env=env,
            memory=Memory(drive_root=tmp_path),
            task={"id": "task-new", "type": "task", "text": "continue"},
            review_context_builder=lambda: build_review_context(env),
        )
        dynamic_text = messages[0]["content"][2]["text"]

        assert "## Review Continuity" in dynamic_text
        assert "repo_commit_ready=no" in dynamic_text
        assert "retry_anchor=commit_readiness_debt" in dynamic_text
        assert "Commit-readiness debt" in dynamic_text
        assert "bypass_reason=manual audit override" in dynamic_text
        assert "stale_marker=2026-04-07T10:00:00" in dynamic_text
        assert "### Open review continuations" in dynamic_text
        assert "critical_finding=tests_affected: Fix the failing test before commit" in dynamic_text
        assert "### Historical review ledger" in dynamic_text
        assert "## Scratchpad" in dynamic_text
        assert dynamic_text.index("## Scratchpad") < dynamic_text.index("## Drive state")
        assert dynamic_text.index("## Runtime context") < dynamic_text.index("## Review Continuity")

    def test_review_continuity_context_ignores_foreign_repo_obligations(self, tmp_path):
        from ouroboros.agent_task_pipeline import build_review_context
        from ouroboros.review_state import (
            AdvisoryReviewState,
            AdvisoryRunRecord,
            CommitAttemptRecord,
            compute_snapshot_hash,
            make_repo_key,
            save_state,
        )

        env = self._make_env(tmp_path)
        repo_a = tmp_path / "repo"
        repo_b = tmp_path / "repo-other"
        (repo_a / ".git").mkdir(parents=True, exist_ok=True)
        (repo_b / ".git").mkdir(parents=True, exist_ok=True)
        (repo_a / "tracked.py").write_text("print('repo a')\n", encoding="utf-8")
        (repo_b / "tracked.py").write_text("print('repo b')\n", encoding="utf-8")

        repo_a_key = make_repo_key(repo_a)
        repo_b_key = make_repo_key(repo_b)
        state = AdvisoryReviewState()
        state.add_run(AdvisoryRunRecord(
            snapshot_hash=compute_snapshot_hash(repo_a),
            commit_message="repo a ready",
            status="fresh",
            ts="2026-04-07T10:00:00+00:00",
            repo_key=repo_a_key,
        ))
        state.record_attempt(CommitAttemptRecord(
            ts="2026-04-07T10:01:00+00:00",
            commit_message="repo b blocked",
            status="blocked",
            repo_key=repo_b_key,
            tool_name="repo_commit",
            task_id="task-b",
            attempt=1,
            block_reason="critical_findings",
            critical_findings=[{
                "item": "foreign_issue",
                "reason": "other repo only",
                "severity": "critical",
                "verdict": "FAIL",
            }],
        ))
        save_state(tmp_path, state)

        dynamic_text = build_review_context(env)
        assert "repo_commit_ready=yes" in dynamic_text
        assert "foreign_issue" not in dynamic_text
        assert "repo b blocked" not in dynamic_text

    def test_review_continuity_context_keeps_open_obligations_without_runs(self, tmp_path):
        from ouroboros.agent_task_pipeline import build_review_context
        from ouroboros.review_state import (
            AdvisoryReviewState,
            ObligationItem,
            make_repo_key,
            save_state,
        )

        env = self._make_env(tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "tracked.py").write_text("print('hi')\n", encoding="utf-8")

        repo_key = make_repo_key(tmp_path / "repo")
        state = AdvisoryReviewState(
            open_obligations=[
                ObligationItem(
                    obligation_id="obl-0001",
                    item="tests_affected",
                    severity="critical",
                    reason="Coverage still missing",
                    source_attempt_ts="2026-04-07T10:00:00+00:00",
                    source_attempt_msg="blocked commit",
                    repo_key=repo_key,
                    fingerprint="finding:tests_affected:abc123",
                )
            ]
        )
        save_state(tmp_path, state)

        dynamic_text = build_review_context(env)
        assert "## Review Continuity" in dynamic_text
        assert "open_obligations=1" in dynamic_text
        assert "[obl-0001] tests_affected: Coverage still missing" in dynamic_text

    def test_review_continuity_context_keeps_all_debt_evidence(self, tmp_path):
        from ouroboros.agent_task_pipeline import build_review_context
        from ouroboros.review_state import (
            AdvisoryReviewState,
            CommitReadinessDebtItem,
            make_repo_key,
            save_state,
        )

        env = self._make_env(tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repo" / "tracked.py").write_text("print('hi')\n", encoding="utf-8")

        repo_key = make_repo_key(tmp_path / "repo")
        state = AdvisoryReviewState(
            commit_readiness_debts=[
                CommitReadinessDebtItem(
                    debt_id="debt-0001",
                    category="repeated_obligation",
                    title="Commit readiness debt",
                    summary="Repeated tests blocker",
                    repo_key=repo_key,
                    source_obligation_ids=["obl-0001"],
                    evidence=[
                        "first evidence",
                        "second evidence",
                        "third evidence",
                    ],
                )
            ]
        )
        save_state(tmp_path, state)

        dynamic_text = build_review_context(env)
        assert "first evidence" in dynamic_text
        assert "second evidence" in dynamic_text
        assert "third evidence" in dynamic_text


def test_runtime_section_includes_improvement_backlog_digest(tmp_path):
    from ouroboros.context import build_llm_messages
    from ouroboros.memory import Memory

    class FakeEnv:
        def drive_path(self, p):
            return tmp_path / p

        def repo_path(self, p):
            return tmp_path / "repo" / p

        @property
        def repo_dir(self):
            return tmp_path / "repo"

        @property
        def drive_root(self):
            return tmp_path

    (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    (tmp_path / "repo" / "prompts" / "SYSTEM.md").write_text("System prompt", encoding="utf-8")
    (tmp_path / "repo" / "BIBLE.md").write_text("Bible", encoding="utf-8")
    (tmp_path / "repo" / "README.md").write_text("README", encoding="utf-8")
    (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text('# Ouroboros v1.2.3', encoding="utf-8")
    (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text('# Dev', encoding="utf-8")
    (tmp_path / "repo" / "docs" / "CHECKLISTS.md").write_text('Checklist', encoding="utf-8")
    (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
    (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
    (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (tmp_path / "memory" / "identity.md").write_text("I am Ouroboros", encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")
    (tmp_path / "memory" / "knowledge" / "improvement-backlog.md").write_text(
        "# Improvement Backlog\n\n### ibl-1\n- status: open\n- created_at: 2026-04-14T09:00:00+00:00\n- source: execution_reflection\n- category: process\n- task_id: task-1\n- requires_plan_review: yes\n- fingerprint: fp-1\n- summary: Reduce recurring task friction around REVIEW_BLOCKED\n",
        encoding="utf-8",
    )

    messages, _ = build_llm_messages(
        env=FakeEnv(),
        memory=Memory(drive_root=tmp_path),
        task={"id": "task-a", "type": "task", "text": "hello"},
    )
    dynamic_text = messages[0]["content"][2]["text"]
    assert "## Improvement Backlog" in dynamic_text
    assert "Reduce recurring task friction around REVIEW_BLOCKED" in dynamic_text


class TestRuntimeEnvSection:
    """build_runtime_section includes runtime_env with platform and is_desktop."""

    def _make_env(self, tmp_path):
        class FakeEnv:
            repo_dir = tmp_path / "repo"
            drive_root = tmp_path

            def drive_path(self, p):
                return tmp_path / p

        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state" / "state.json").write_text(
            '{"spent_usd": 0}', encoding="utf-8"
        )
        return FakeEnv()

    def test_runtime_env_present(self, tmp_path, monkeypatch):
        from ouroboros.context import build_runtime_section

        monkeypatch.delenv("OUROBOROS_DESKTOP_MODE", raising=False)
        env = self._make_env(tmp_path)
        section = build_runtime_section(env, {"id": "t1", "type": "task"})
        data = json.loads(section.split("## Runtime context\n\n", 1)[1])
        assert "runtime_env" in data
        assert "platform" in data["runtime_env"]
        assert isinstance(data["runtime_env"]["platform"], str)
        assert data["runtime_env"]["is_desktop"] is False

    def test_runtime_env_desktop_flag(self, tmp_path, monkeypatch):
        from ouroboros.context import build_runtime_section

        monkeypatch.setenv("OUROBOROS_DESKTOP_MODE", "1")
        env = self._make_env(tmp_path)
        section = build_runtime_section(env, {"id": "t2", "type": "task"})
        data = json.loads(section.split("## Runtime context\n\n", 1)[1])
        assert data["runtime_env"]["is_desktop"] is True


# ===========================================================================
# Memory / consolidation offset behavior (merged from former
# test_context_memory_overhaul.py).  Inspect-only `limit=50` / `limit=1000`
# source-string pins were dropped — behavioral coverage below already
# exercises the offset path.  test_no_identity_truncation_in_consolidator_
# prompts was also dropped (inspect-only); identity-truncation is covered
# behaviorally by consolidator tests.
# ===========================================================================


def test_recent_chat_starts_after_consolidated_offset(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory

    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    logs_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {"ts": f"2026-03-19T16:{i:02d}:00Z", "direction": "in", "username": "User", "text": f"msg-{i}"}
        for i in range(5)
    ]
    (logs_dir / "chat.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    memory = Memory(drive_root=tmp_path)
    (memory_dir / "dialogue_meta.json").write_text(
        json.dumps({
            "last_consolidated_offset": 3,
            "chat_log_signature": memory.jsonl_generation_signature("chat.jsonl"),
        }),
        encoding="utf-8",
    )

    sections = build_recent_sections(memory, env=None)
    combined = "\n\n".join(sections)

    assert "msg-0" not in combined
    assert "msg-1" not in combined
    assert "msg-2" not in combined
    assert "msg-3" in combined
    assert "msg-4" in combined


def test_recent_chat_offset_uses_filtered_dialogue_entries(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory

    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    logs_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    entries = [
        {"chat_id": 1, "direction": "in", "username": "User", "text": "consolidated-0"},
        {"chat_id": -1, "direction": "in", "username": "Agent", "text": "a2a-noise"},
        {"chat_id": 1, "direction": "in", "username": "User", "text": "consolidated-1"},
        {"chat_id": 1, "direction": "in", "username": "User", "text": "fresh"},
    ]
    (logs_dir / "chat.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )
    memory = Memory(drive_root=tmp_path)
    (memory_dir / "dialogue_meta.json").write_text(
        json.dumps({
            "last_consolidated_offset": 2,
            "chat_log_signature": memory.jsonl_generation_signature("chat.jsonl"),
        }),
        encoding="utf-8",
    )

    combined = "\n\n".join(build_recent_sections(memory, env=None))

    assert "consolidated-0" not in combined
    assert "consolidated-1" not in combined
    assert "a2a-noise" not in combined
    assert "fresh" in combined


def test_recent_chat_ignores_stale_consolidation_offset_after_rotation(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory

    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    logs_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    initial = [
        {"chat_id": 1, "direction": "in", "username": "User", "text": f"early-{i}"}
        for i in range(3)
    ]
    (logs_dir / "chat.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in initial) + "\n",
        encoding="utf-8",
    )
    memory = Memory(drive_root=tmp_path)
    stale_signature = memory.jsonl_generation_signature("chat.jsonl")
    (memory_dir / "dialogue_meta.json").write_text(
        json.dumps({
            "last_consolidated_offset": 3,
            "chat_log_signature": stale_signature,
        }),
        encoding="utf-8",
    )

    rotated = [
        {"chat_id": 1, "direction": "in", "username": "User", "text": f"post-rotate-{i}"}
        for i in range(2)
    ]
    (logs_dir / "chat.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in rotated) + "\n",
        encoding="utf-8",
    )

    combined = "\n\n".join(build_recent_sections(memory, env=None))

    # Rotation invalidates the stale offset; rotated entries appear.
    assert "post-rotate-0" in combined
    assert "post-rotate-1" in combined


def test_recent_chat_keeps_offset_when_same_log_gets_appended(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory

    logs_dir = tmp_path / "logs"
    memory_dir = tmp_path / "memory"
    logs_dir.mkdir(parents=True, exist_ok=True)
    memory_dir.mkdir(parents=True, exist_ok=True)
    initial = [
        {"chat_id": 1, "direction": "in", "username": "User", "text": f"old-{i}"}
        for i in range(3)
    ]
    (logs_dir / "chat.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in initial) + "\n",
        encoding="utf-8",
    )
    memory = Memory(drive_root=tmp_path)
    (memory_dir / "dialogue_meta.json").write_text(
        json.dumps({
            "last_consolidated_offset": 3,
            "chat_log_signature": memory.jsonl_generation_signature("chat.jsonl"),
        }),
        encoding="utf-8",
    )

    with open(logs_dir / "chat.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"chat_id": 1, "direction": "in", "username": "User", "text": "new"}) + "\n")

    combined = "\n\n".join(build_recent_sections(memory, env=None))

    assert "old-0" not in combined
    assert "new" in combined


def test_world_profile_is_loaded_with_stable_memory(tmp_path):
    from ouroboros.context import build_memory_sections
    from ouroboros.memory import Memory

    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory" / "WORLD.md").write_text("world-profile-data", encoding="utf-8")
    memory = Memory(drive_root=tmp_path)

    sections = build_memory_sections(memory)
    combined = "\n\n".join(sections)

    assert "world-profile-data" in combined


def test_retired_dialogue_summary_remains_visible_when_blocks_exist(tmp_path):
    from ouroboros.context import build_memory_sections
    from ouroboros.memory import Memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "dialogue_summary.md").write_text("legacy dialogue", encoding="utf-8")
    (memory_dir / "dialogue_blocks.json").write_text(
        json.dumps([{"content": "new dialogue block"}]),
        encoding="utf-8",
    )
    memory = Memory(drive_root=tmp_path)

    combined = "\n\n".join(build_memory_sections(memory, partition="volatile"))

    assert "## Dialogue History" in combined
    assert "new dialogue block" in combined
    assert "## Legacy Dialogue Summary (retired flat format, read-only fallback)" in combined
    assert "legacy dialogue" in combined


def test_retired_dialogue_summary_fallback_preserves_continuity_without_blocks(tmp_path):
    from ouroboros.context import build_memory_sections
    from ouroboros.memory import Memory

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / "dialogue_summary.md").write_text("legacy dialogue only", encoding="utf-8")
    memory = Memory(drive_root=tmp_path)

    combined = "\n\n".join(build_memory_sections(memory, partition="volatile"))

    assert "## Legacy Dialogue Summary (retired flat format, read-only fallback)" in combined
    assert "legacy dialogue only" in combined


def test_recent_sections_filter_process_logs_by_task_id(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "progress.jsonl").write_text(
        "\n".join([
            json.dumps({"task_id": "task-a", "text": "in-scope"}),
            json.dumps({"task_id": "task-b", "text": "out-of-scope"}),
        ]) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "tools.jsonl").write_text(
        "\n".join([
            json.dumps({"task_id": "task-a", "tool": "shell"}),
            json.dumps({"task_id": "task-b", "tool": "shell"}),
        ]) + "\n",
        encoding="utf-8",
    )

    memory = Memory(drive_root=tmp_path)
    sections = build_recent_sections(memory, env=None, task_id="task-a")
    combined = "\n\n".join(sections)
    assert "in-scope" in combined
    assert "out-of-scope" not in combined


def test_should_consolidate_chat_blocks(tmp_path):
    from ouroboros.consolidator import should_consolidate, BLOCK_SIZE
    chat_path = tmp_path / 'chat.jsonl'
    meta_path = tmp_path / 'dialogue_meta.json'
    entries = [
        json.dumps({"ts": f"2026-03-09T10:{i % 60:02d}:00Z", "direction": "in", "text": "msg"})
        for i in range(BLOCK_SIZE + 5)
    ]
    chat_path.write_text("\n".join(entries) + "\n", encoding='utf-8')
    assert should_consolidate(meta_path, chat_path) is True


def test_consolidate_chat_creates_block(tmp_path):
    from unittest.mock import MagicMock
    from ouroboros.consolidator import consolidate, _load_meta, _load_blocks, BLOCK_SIZE
    chat_path = tmp_path / 'chat.jsonl'
    blocks_path = tmp_path / 'dialogue_blocks.json'
    meta_path = tmp_path / 'dialogue_meta.json'
    entries = [
        json.dumps({"ts": f"2026-03-09T10:{i % 60:02d}:00Z", "direction": "in", "text": f"msg {i}"})
        for i in range(BLOCK_SIZE + 5)
    ]
    chat_path.write_text("\n".join(entries) + "\n", encoding='utf-8')
    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        {"content": "### Block: test\n\nSummary."},
        {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
    )
    usage = consolidate(chat_path, blocks_path, meta_path, mock_llm)
    assert usage is not None
    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == BLOCK_SIZE
    blocks = _load_blocks(blocks_path)
    assert len(blocks) == 1


def test_installed_skills_section_includes_warnings_verdict(tmp_path, monkeypatch):
    from ouroboros.context import _build_installed_skills_section

    class FakeEnv:
        drive_root = tmp_path

    monkeypatch.setattr(
        "ouroboros.skill_loader.summarize_skills",
        lambda _root: {
            "skills": [
                {
                    "name": "weather",
                    "type": "script",
                    "enabled": True,
                    "review_status": "warnings",
                    "executable_review": True,
                    "review_stale": False,
                    "description": "Weather helper",
                }
            ]
        },
    )

    section = _build_installed_skills_section(FakeEnv())

    assert "## Installed Skills" in section
    assert "weather" in section
    assert "warnings" in section


def test_health_invariants_come_first_in_dynamic_context(tmp_path):
    from ouroboros.context import build_llm_messages
    from ouroboros.memory import Memory

    class FakeEnv:
        def drive_path(self, p):
            return tmp_path / p

        def repo_path(self, p):
            return tmp_path / "repo" / p

        @property
        def repo_dir(self):
            return tmp_path / "repo"

        @property
        def drive_root(self):
            return tmp_path

    (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    (tmp_path / "repo" / "prompts" / "SYSTEM.md").write_text("System prompt", encoding="utf-8")
    (tmp_path / "repo" / "BIBLE.md").write_text("Bible", encoding="utf-8")
    (tmp_path / "repo" / "README.md").write_text("README", encoding="utf-8")
    (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v1.2.3", encoding="utf-8")
    (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text(
        "### File Size Budgets\n| Path | Budget chars |\n|------|--------------|\n| memory/identity.md | 1000 |\n",
        encoding="utf-8",
    )
    (tmp_path / "repo" / "docs" / "CHECKLISTS.md").write_text("Checklist", encoding="utf-8")
    (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
    (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
    (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
    (tmp_path / "memory" / "identity.md").write_text("x" * 950, encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")

    messages, _cap_info = build_llm_messages(
        env=FakeEnv(),
        memory=Memory(drive_root=tmp_path),
        task={"id": "task-a", "type": "task", "text": "hello"},
    )

    dynamic_text = messages[0]["content"][2]["text"]
    assert dynamic_text.startswith("## Health Invariants")
    assert dynamic_text.index("## Health Invariants") < dynamic_text.index("## Drive state")


def test_health_invariants_come_first_in_background_consciousness_context(tmp_path):
    from ouroboros.consciousness import BackgroundConsciousness

    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    (repo_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    (drive_root / "logs").mkdir(parents=True, exist_ok=True)
    (drive_root / "state").mkdir(parents=True, exist_ok=True)

    (repo_dir / "prompts" / "CONSCIOUSNESS.md").write_text("Consciousness prompt", encoding="utf-8")
    (repo_dir / "BIBLE.md").write_text("Bible", encoding="utf-8")
    (repo_dir / "VERSION").write_text("1.2.3", encoding="utf-8")
    (repo_dir / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
    (repo_dir / "README.md").write_text("README", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v1.2.3", encoding="utf-8")
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text(
        "### File Size Budgets\n| Path | Budget chars |\n|------|--------------|\n| memory/identity.md | 1000 |\n",
        encoding="utf-8",
    )
    (drive_root / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("x" * 950, encoding="utf-8")
    (drive_root / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")
    (drive_root / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "progress.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "events.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "supervisor.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "task_reflections.jsonl").write_text("", encoding="utf-8")

    bg = BackgroundConsciousness(
        drive_root=drive_root,
        repo_dir=repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )

    text = bg._build_context()
    assert text.index("## Health Invariants") < text.index("## Drive state")

"""Tests for ouroboros.context health invariants."""

from __future__ import annotations

import json
import pathlib
import tempfile

from ouroboros.context import build_health_invariants


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

    def test_rc_pep440_pyproject_does_not_warn(self, tmp_path):
        env = _make_health_env(tmp_path)
        (tmp_path / "repo" / "VERSION").write_text("4.50.0-rc.2", encoding="utf-8")
        (tmp_path / "repo" / "pyproject.toml").write_text('version = "4.50.0rc2"', encoding="utf-8")
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


class TestIdentityStaleThreshold:
    """Verify the identity-staleness threshold is configurable and defaults to 168h."""

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
        (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
        (tmp_path / "memory" / "identity.md").write_text("x" * 300, encoding="utf-8")
        (tmp_path / "memory" / "scratchpad.md").write_text("x" * 300, encoding="utf-8")
        (tmp_path / "logs" / "events.jsonl").write_text("", encoding="utf-8")
        return FakeEnv()

    def test_default_threshold_is_168h(self):
        from ouroboros.context import IDENTITY_STALE_HOURS_DEFAULT

        assert IDENTITY_STALE_HOURS_DEFAULT == 168

    def test_recent_identity_no_warning(self, tmp_path):
        from ouroboros.context import build_health_invariants

        env = self._make_env(tmp_path)
        # mtime is "now" — no warning expected
        result = build_health_invariants(env)
        assert "STALE IDENTITY" not in result

    def test_stale_identity_warning(self, tmp_path):
        import os
        import time

        from ouroboros.context import build_health_invariants

        env = self._make_env(tmp_path)
        identity_file = tmp_path / "memory" / "identity.md"
        # Set mtime to 200h ago (past default 168h threshold)
        old_mtime = time.time() - 200 * 3600
        os.utime(identity_file, (old_mtime, old_mtime))
        result = build_health_invariants(env)
        assert "STALE IDENTITY" in result

    def test_env_override_threshold(self, tmp_path, monkeypatch):
        import os
        import time

        from ouroboros.context import build_health_invariants

        env = self._make_env(tmp_path)
        identity_file = tmp_path / "memory" / "identity.md"
        # Set mtime to 200h ago — past default (168h) but within override (300h)
        old_mtime = time.time() - 200 * 3600
        os.utime(identity_file, (old_mtime, old_mtime))

        # Override threshold to 100h → should warn (200 > 100)
        monkeypatch.setenv("OUROBOROS_IDENTITY_STALE_HOURS", "100")
        result = build_health_invariants(env)
        assert "STALE IDENTITY" in result

        # Override threshold to 250h → should NOT warn (200 < 250)
        monkeypatch.setenv("OUROBOROS_IDENTITY_STALE_HOURS", "250")
        result = build_health_invariants(env)
        assert "STALE IDENTITY" not in result

    def test_invalid_env_value_falls_back_to_default(self, tmp_path, monkeypatch):
        import os
        import time

        from ouroboros.context import build_health_invariants

        env = self._make_env(tmp_path)
        identity_file = tmp_path / "memory" / "identity.md"
        # Set mtime to 200h ago — past default (168h)
        old_mtime = time.time() - 200 * 3600
        os.utime(identity_file, (old_mtime, old_mtime))

        monkeypatch.setenv("OUROBOROS_IDENTITY_STALE_HOURS", "not-a-number")
        result = build_health_invariants(env)
        # Falls back to default 168h → should warn (200 > 168)
        assert "STALE IDENTITY" in result

"""Tests for the plan_task (plan_review.py) pre-implementation design review tool.

Tests cover:
- Tool is registered and callable
- Input validation (missing plan, missing goal)
- Budget gate fires when prompt is oversized
- _get_review_models fallback when OUROBOROS_REVIEW_MODELS not set
- _load_plan_checklist returns non-empty text (section exists in CHECKLISTS.md)
- _format_output aggregate signal logic (GREEN / REVIEW_REQUIRED / REVISE_PLAN)
- Output structure: all reviewer sections present
"""

from __future__ import annotations

import os
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(tmp_path: pathlib.Path | None = None) -> MagicMock:
    root = tmp_path or pathlib.Path(".")
    ctx = MagicMock()
    ctx.repo_dir = root
    ctx.drive_root = root
    ctx.drive_logs.return_value = root / "logs"
    ctx.emit_progress_fn = MagicMock()
    return ctx


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestPlanReviewInputValidation(unittest.TestCase):
    def setUp(self):
        from ouroboros.tools.plan_review import _handle_plan_task
        self.handler = _handle_plan_task
        self.ctx = _make_ctx()

    def test_missing_plan_returns_error(self):
        result = self.handler(self.ctx, plan="", goal="some goal")
        self.assertIn("ERROR", result)
        self.assertIn("plan", result.lower())

    def test_missing_goal_returns_error(self):
        result = self.handler(self.ctx, plan="some plan", goal="")
        self.assertIn("ERROR", result)
        self.assertIn("goal", result.lower())

    def test_whitespace_plan_returns_error(self):
        result = self.handler(self.ctx, plan="   ", goal="some goal")
        self.assertIn("ERROR", result)

    def test_whitespace_goal_returns_error(self):
        result = self.handler(self.ctx, plan="some plan", goal="   ")
        self.assertIn("ERROR", result)


class TestPlanReviewModels(unittest.TestCase):
    def test_falls_back_to_config_default_when_env_is_empty(self):
        """Empty OUROBOROS_REVIEW_MODELS → use the shipped SETTINGS_DEFAULTS.

        Post-v4.33.1: plan_task delegates to ``config.get_review_models``,
        which returns the shipped triad default when the env is empty — the
        same behavior as the commit triad. This keeps plan_task and commit
        review in lockstep instead of plan_task silently collapsing to
        ``[main] * 3`` on an unconfigured instance.

        Hermetic: explicitly clears all provider env vars AND
        ``OPENAI_BASE_URL`` so this test does not depend on shell/CI
        environment. An ambient ANTHROPIC_API_KEY (or any direct-provider
        key) would flip ``config.get_review_models`` into the exclusive
        direct-provider fallback path and break the assertion, and a
        non-empty ``OPENAI_BASE_URL`` is treated as a custom runtime
        configuration by ``_exclusive_direct_remote_provider_env`` which
        also alters the code path.
        """
        from ouroboros.tools.plan_review import _get_review_models
        env = {
            "OUROBOROS_REVIEW_MODELS": "",
            "OUROBOROS_MODEL": "test/model-x",
            "OPENROUTER_API_KEY": "",
            "OPENAI_API_KEY": "",
            "OPENAI_BASE_URL": "",
            "OPENAI_COMPATIBLE_API_KEY": "",
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
        }
        with patch.dict(os.environ, env, clear=False):
            models = _get_review_models()
        self.assertEqual(len(models), 3)
        # The shipped default is the 3-model OpenRouter triad (GPT-5.4,
        # Gemini 3.1 Pro Preview, Claude Opus 4.7). Exact identities are
        # version-tracked in config.SETTINGS_DEFAULTS; we just assert the
        # size and that we did NOT silently collapse to [main] * 3.
        self.assertFalse(
            all(m == "test/model-x" for m in models),
            f"plan_task must not silently collapse to main × 3 when the default triad "
            f"is configured; got {models!r}",
        )

    def test_returns_configured_models(self):
        from ouroboros.tools.plan_review import _get_review_models
        configured = "openai/gpt-5.5,google/gemini-3.5-flash,anthropic/claude-opus-4.6"
        with patch.dict(os.environ, {"OUROBOROS_REVIEW_MODELS": configured}, clear=False):
            models = _get_review_models()
        self.assertEqual(models, [
            "openai/gpt-5.5",
            "google/gemini-3.5-flash",
            "anthropic/claude-opus-4.6",
        ])

    def test_capped_at_three_models(self):
        from ouroboros.tools.plan_review import _get_review_models
        too_many = "a/1,b/2,c/3,d/4,e/5"
        with patch.dict(os.environ, {"OUROBOROS_REVIEW_MODELS": too_many}, clear=False):
            models = _get_review_models()
        self.assertEqual(len(models), 3)

    def test_preserves_one_model_config_for_quorum_error(self):
        """One model configured stays one slot; caller returns quorum error."""
        from ouroboros.tools.plan_review import _get_review_models
        with patch.dict(os.environ, {"OUROBOROS_REVIEW_MODELS": "only/one"}, clear=False):
            models = _get_review_models()
        self.assertEqual(models, ["only/one"])

    def test_preserves_two_model_config(self):
        """Two configured models are two reviewer slots, not an implicit third."""
        from ouroboros.tools.plan_review import _get_review_models
        with patch.dict(os.environ, {"OUROBOROS_REVIEW_MODELS": "model/a,model/b"}, clear=False):
            models = _get_review_models()
        self.assertEqual(models, ["model/a", "model/b"])

    def test_delegates_to_config_get_review_models_for_direct_provider_fallback(self):
        """plan_task must use the same direct-provider fallback as the commit triad.

        Regression guard for v4.33.1 scope review finding
        ``plan_task_review_model_parity`` + v4.39.0 quorum-safe-fallback fix:
        ``config.get_review_models``'s OpenAI-only / Anthropic-only fallback
        now rewrites the list to ``[main, light, light]`` (3 slots)
        when the configured reviewers don't match the exclusive direct-
        provider prefix, and ``_get_review_models`` must see that shape
        unchanged. Duplicate model IDs are valid stochastic reviewer slots.
        """
        from ouroboros.tools.plan_review import _get_review_models
        # Simulate Anthropic-only direct setup: only ANTHROPIC key present,
        # main is anthropic::..., but the reviewer list is still the default
        # OpenRouter-style set (so none match the anthropic:: prefix).
        env = {
            "OUROBOROS_REVIEW_MODELS": "openai/gpt-5.5,google/gemini-3.5-flash,anthropic/claude-opus-4.6",
            "OUROBOROS_MODEL": "anthropic::claude-opus-4-6",
            "OUROBOROS_MODEL_LIGHT": "anthropic::claude-sonnet-4-6",
            "OPENROUTER_API_KEY": "",
            "OPENAI_API_KEY": "",
            "OPENAI_BASE_URL": "",
            "OPENAI_COMPATIBLE_API_KEY": "",
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": "",
            "ANTHROPIC_API_KEY": "sk-ant-test",
        }
        with patch.dict(os.environ, env, clear=False):
            models = _get_review_models()
        # Expect the Anthropic-only fallback: `[main, light, light]`.
        self.assertEqual(len(models), 3)
        self.assertEqual(
            models,
            [
                "anthropic::claude-opus-4-6",
                "anthropic::claude-sonnet-4-6",
                "anthropic::claude-sonnet-4-6",
            ],
            f"expected [main, light, light] direct-provider fallback, got {models!r}",
        )


class TestPlanReviewChecklist(unittest.TestCase):
    def test_checklist_section_exists_and_non_empty(self):
        """Plan Review Checklist section must exist in CHECKLISTS.md."""
        from ouroboros.tools.plan_review import _load_plan_checklist
        checklist = _load_plan_checklist()
        self.assertIsInstance(checklist, str)
        self.assertGreater(len(checklist), 100)
        # Verify key items are present
        self.assertIn("completeness", checklist)
        self.assertIn("correctness", checklist)
        self.assertIn("minimalism", checklist)
        self.assertIn("bible_alignment", checklist)


class TestPlanReviewSystemPrompt(unittest.TestCase):
    def test_system_prompt_frames_reviewer_as_candidate_validator(self):
        from ouroboros.tools.plan_review import _build_system_prompt
        prompt = _build_system_prompt("checklist", "", "", "")
        self.assertIn("validating a concrete candidate plan", prompt)
        self.assertIn("not brainstorming from zero", prompt)

    def test_system_prompt_declares_generative_stance(self):
        """Review stance must explicitly frame the reviewer as a generative partner."""
        from ouroboros.tools.plan_review import _build_system_prompt
        prompt = _build_system_prompt("checklist", "", "", "")
        self.assertIn("## Review stance", prompt)
        self.assertIn("GENERATIVE", prompt)
        # Design PARTNER framing — not auditor
        self.assertIn("PARTNER", prompt)

    def test_system_prompt_requires_own_approach_and_proposals_sections(self):
        """Required output structure must include 'Your own approach' and ## PROPOSALS."""
        from ouroboros.tools.plan_review import _build_system_prompt
        prompt = _build_system_prompt("checklist", "", "", "")
        self.assertIn("Required output structure", prompt)
        self.assertIn("Your own approach", prompt)
        self.assertIn("## PROPOSALS", prompt)

    def test_system_prompt_forbids_commit_hygiene_penalty(self):
        """Reviewers must not penalise missing tests/VERSION/README — plan has no code yet."""
        from ouroboros.tools.plan_review import _build_system_prompt
        prompt = _build_system_prompt("checklist", "", "", "")
        self.assertIn("Do NOT penalise missing tests", prompt)

    def test_system_prompt_explains_majority_vote_coordination(self):
        """The prompt must explain that REVISE_PLAN requires majority agreement
        across the configured reviewer slot count (2 or 3 slots)."""
        from ouroboros.tools.plan_review import _build_system_prompt
        prompt = _build_system_prompt("checklist", "", "", "")
        self.assertIn("majority-vote", prompt)
        self.assertIn("at least 2 reviewer slots", prompt)
        self.assertIn("2-3 reviewer slots", prompt)

    def test_system_prompt_preserves_aggregate_contract(self):
        from ouroboros.tools.plan_review import _build_system_prompt
        prompt = _build_system_prompt("checklist", "", "", "")
        self.assertIn("AGGREGATE: GREEN", prompt)
        self.assertIn("AGGREGATE: REVIEW_REQUIRED", prompt)
        self.assertIn("AGGREGATE: REVISE_PLAN", prompt)


class TestPlanReviewFormatOutput(unittest.TestCase):
    def _run(self, raw_results):
        from ouroboros.tools.plan_review import _format_output
        return _format_output(raw_results, ["model-a", "model-b", "model-c"], "test goal", 12345)

    def test_green_when_no_fails_or_risks(self):
        results = [
            {"model": "model-a", "text": "PASS on all items.\nAGGREGATE: GREEN", "error": None},
            {"model": "model-b", "text": "Everything looks good.\nAGGREGATE: GREEN", "error": None},
            {"model": "model-c", "text": "No issues found.\nAGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        aggregate_section = out.split("## Aggregate")[1]
        # Final verdict (bolded) must be GREEN, not REVISE_PLAN or REVIEW_REQUIRED
        self.assertIn("**GREEN**", aggregate_section)
        self.assertNotIn("**REVISE_PLAN**", aggregate_section)
        self.assertNotIn("**REVIEW_REQUIRED**", aggregate_section)

    def test_review_required_when_risk_present(self):
        results = [
            {"model": "model-a", "text": "Some RISK items.\nAGGREGATE: REVIEW_REQUIRED", "error": None},
            {"model": "model-b", "text": "AGGREGATE: GREEN", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        self.assertIn("REVIEW_REQUIRED", out)

    def test_minority_revise_plan_becomes_review_required(self):
        """One reviewer flagging REVISE_PLAN while the others do not → REVIEW_REQUIRED.

        Majority-vote coordination: a lone dissenting REVISE_PLAN surfaces as a
        strong coordination signal (REVIEW_REQUIRED with dissent noted), not an
        automatic REVISE_PLAN. Replaces the pre-majority-vote behavior where any
        single REVISE_PLAN escalated the final verdict.
        """
        results = [
            {"model": "model-a", "text": "Critical FAIL: missing tests.\nAGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-b", "text": "AGGREGATE: GREEN", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        aggregate_section = out.split("## Aggregate")[1]
        # Final verdict should be REVIEW_REQUIRED, not REVISE_PLAN
        self.assertIn("REVIEW_REQUIRED", aggregate_section)
        self.assertNotIn("**REVISE_PLAN**", aggregate_section)
        # Dissent must be explicitly noted in the aggregate reasoning
        self.assertIn("dissent", aggregate_section.lower())

    def test_majority_revise_plan_blocks(self):
        """Two reviewers flagging REVISE_PLAN → final verdict is REVISE_PLAN."""
        results = [
            {"model": "model-a", "text": "FAIL on correctness.\nAGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-b", "text": "FAIL on completeness.\nAGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        aggregate_section = out.split("## Aggregate")[1]
        self.assertIn("REVISE_PLAN", aggregate_section)

    def test_unanimous_revise_plan_is_revise_plan(self):
        """Three reviewers flagging REVISE_PLAN → final verdict is REVISE_PLAN."""
        results = [
            {"model": "model-a", "text": "FAIL.\nAGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-b", "text": "FAIL.\nAGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-c", "text": "FAIL.\nAGGREGATE: REVISE_PLAN", "error": None},
        ]
        out = self._run(results)
        aggregate_section = out.split("## Aggregate")[1]
        self.assertIn("REVISE_PLAN", aggregate_section)

    def test_error_result_does_not_crash(self):
        results = [
            {"model": "model-a", "text": "", "error": "Timeout after 120s"},
            {"model": "model-b", "text": "AGGREGATE: GREEN", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        self.assertIn("ERROR", out)

    def test_single_revise_plus_error_is_review_required(self):
        """One REVISE_PLAN + one error + one GREEN → REVIEW_REQUIRED (no majority FAIL).

        Replaces the pre-majority-vote test that asserted REVISE_PLAN stayed final
        when a later reviewer errored. Majority-vote coordination requires TWO
        agreeing REVISE_PLAN reviewers; a single dissent plus a degraded reviewer
        does not clear the bar.
        """
        results = [
            {"model": "model-a", "text": "Critical FAIL.\nAGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-b", "text": "", "error": "Timeout after 120s"},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        aggregate_section = out.split("## Aggregate")[1]
        self.assertIn("REVIEW_REQUIRED", aggregate_section)
        self.assertNotIn("**REVISE_PLAN**", aggregate_section)

    def test_aggregate_block_reports_per_reviewer_counts(self):
        """Aggregate block should surface per-reviewer signal counts for auditability."""
        results = [
            {"model": "model-a", "text": "AGGREGATE: REVISE_PLAN", "error": None},
            {"model": "model-b", "text": "AGGREGATE: REVIEW_REQUIRED", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        aggregate_section = out.split("## Aggregate")[1]
        self.assertIn("REVISE_PLAN=1", aggregate_section)
        self.assertIn("REVIEW_REQUIRED=1", aggregate_section)
        self.assertIn("GREEN=1", aggregate_section)

    def test_empty_reviewer_list_returns_explicit_review_required(self):
        """Empty per-reviewer list → explicit 'no responses' message, not misleading zero counts.

        Defensive path: `_run_plan_review_async` guarantees at least one reviewer,
        but if `_format_output` is ever called with an empty list the aggregate
        block must say so explicitly rather than rendering 'REVISE_PLAN=0, ...'
        which would read like a false clean-PASS aggregate.
        """
        out = self._run([])
        self.assertIn("## Aggregate", out)
        self.assertIn("REVIEW_REQUIRED", out)
        self.assertIn("No reviewer responses", out)
        # Must NOT render the zero-count line when there is no data at all
        self.assertNotIn("REVISE_PLAN=0", out)

    def test_missing_aggregate_line_yields_review_required(self):
        """A non-error response with no AGGREGATE: line → REVIEW_REQUIRED (not GREEN)."""
        results = [
            {"model": "model-a", "text": "Looks generally fine but some concerns.", "error": None},
            {"model": "model-b", "text": "AGGREGATE: GREEN", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        # model-a has no aggregate line → should pull aggregate down to REVIEW_REQUIRED
        self.assertIn("REVIEW_REQUIRED", out)
        self.assertNotIn("\n## Aggregate Signal: GREEN", out)

    def test_all_reviewer_sections_present(self):
        results = [
            {"model": "model-a", "text": "AGGREGATE: GREEN", "error": None},
            {"model": "model-b", "text": "AGGREGATE: GREEN", "error": None},
            {"model": "model-c", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        self.assertIn("Reviewer 1", out)
        self.assertIn("Reviewer 2", out)
        self.assertIn("Reviewer 3", out)

    def test_goal_and_token_estimate_in_output(self):
        results = [
            {"model": "model-a", "text": "AGGREGATE: GREEN", "error": None},
        ]
        out = self._run(results)
        self.assertIn("test goal", out)
        self.assertIn("12,345", out)


class TestPlanReviewBudgetGate(unittest.IsolatedAsyncioTestCase):
    async def test_budget_gate_skips_when_oversized(self):
        """When assembled prompt exceeds token limit, returns PLAN_REVIEW_SKIPPED."""
        from ouroboros.tools import plan_review as pr

        ctx = _make_ctx()
        ctx.repo_dir = pathlib.Path(".")
        atlas = SimpleNamespace(text="x" * 1_000_000, manifest={}, status="budget_constrained")

        with (
            patch.object(pr, "compile_review_context_atlas", return_value=atlas),
            patch.object(pr, "build_head_snapshot_section", return_value=""),
            patch.object(pr, "_load_plan_checklist", return_value="checklist"),
            patch.object(pr, "_load_bible", return_value=""),
            patch.object(pr, "_load_doc", return_value=""),
            # Two distinct models so the quorum gate (v4.39.0) passes and we
            # actually reach the budget check under test. Patch BOTH
            # `_cfg.get_review_models` (quorum gate reads this) and
            # `pr._get_review_models` (parallel-run reads this) so the test
            # is hermetic against developer `OUROBOROS_REVIEW_MODELS`.
            patch("ouroboros.config.get_review_models",
                  return_value=["model-a", "model-b"]),
            patch.object(pr, "_get_review_models", return_value=["model-a", "model-b"]),
            # estimate_tokens returns a large number
            patch("ouroboros.tools.plan_review.estimate_tokens", return_value=1_100_000),
        ):
            result = await pr._run_plan_review_async(ctx, "my plan", "my goal", [], context_level="constitutional")

        self.assertIn("PLAN_REVIEW_SKIPPED", result)

        atlas = SimpleNamespace(
            text="small atlas",
            manifest={"estimated_total_tokens": 950_000},
            status="budget_exceeded",
        )
        with (
            patch.object(pr, "compile_review_context_atlas", return_value=atlas),
            patch.object(pr, "build_head_snapshot_section", return_value=""),
            patch.object(pr, "_load_plan_checklist", return_value="checklist"),
            patch.object(pr, "_load_bible", return_value=""),
            patch.object(pr, "_load_doc", return_value=""),
            patch("ouroboros.config.get_review_models",
                  return_value=["model-a", "model-b"]),
            patch.object(pr, "_get_review_models", return_value=["model-a", "model-b"]),
            patch("ouroboros.tools.plan_review.estimate_tokens", return_value=10_000),
        ):
            result = await pr._run_plan_review_async(ctx, "my plan", "my goal", [], context_level="constitutional")

        self.assertIn("PLAN_REVIEW_SKIPPED", result)
        self.assertIn("generated repository atlas exceeded hard budget", result)

    async def test_proceeds_when_within_budget(self):
        """When prompt is within budget, reviewers are called."""
        from ouroboros.tools import plan_review as pr

        ctx = _make_ctx()
        ctx.repo_dir = pathlib.Path(".")

        mock_result = {
            "model": "model-a",
            "text": "All good.\nAGGREGATE: GREEN",
            "error": None,
            "tokens_in": 100,
            "tokens_out": 50,
        }
        atlas = SimpleNamespace(text="small atlas", manifest={}, status="ok")

        with (
            patch.object(pr, "compile_review_context_atlas", return_value=atlas),
            patch.object(pr, "build_head_snapshot_section", return_value=""),
            patch.object(pr, "_load_plan_checklist", return_value="checklist"),
            patch.object(pr, "_load_bible", return_value=""),
            patch.object(pr, "_load_doc", return_value=""),
            # Two distinct models so the quorum gate (v4.39.0) passes and we
            # actually reach the reviewer-call path under test. Patch both
            # `_cfg.get_review_models` and `pr._get_review_models` to stay
            # hermetic against developer `OUROBOROS_REVIEW_MODELS`.
            patch("ouroboros.config.get_review_models",
                  return_value=["model-a", "model-b"]),
            patch.object(pr, "_get_review_models", return_value=["model-a", "model-b"]),
            patch("ouroboros.tools.plan_review.estimate_tokens", return_value=10_000),
            patch.object(pr, "_run_plan_review_slots", new=AsyncMock(return_value=[mock_result, mock_result])),
        ):
            result = await pr._run_plan_review_async(ctx, "my plan", "my goal", [], context_level="localized")

        self.assertIn("Plan Review Results", result)
        self.assertIn("GREEN", result)

    async def test_context_level_must_be_agent_chosen_explicitly(self):
        """plan_task must not use host-side auto heuristics for context selection."""
        from ouroboros.tools import plan_review as pr

        ctx = _make_ctx()
        with (
            patch("ouroboros.config.get_review_models",
                  return_value=["model-a", "model-b"]),
            patch.object(pr, "_get_review_models", return_value=["model-a", "model-b"]),
        ):
            result = await pr._run_plan_review_async(ctx, "my plan", "my goal", [])

        self.assertIn("ERROR", result)
        self.assertIn("explicit context_level", result)
        self.assertIn("host-side auto", result)


class TestParseAggregateSignal(unittest.TestCase):
    def setUp(self):
        from ouroboros.tools.plan_review import _parse_aggregate_signal
        self.parse = _parse_aggregate_signal

    def test_detects_green(self):
        self.assertEqual(self.parse("AGGREGATE: GREEN"), "GREEN")

    def test_detects_review_required(self):
        self.assertEqual(self.parse("AGGREGATE: REVIEW_REQUIRED"), "REVIEW_REQUIRED")

    def test_detects_revise_plan(self):
        self.assertEqual(self.parse("AGGREGATE: REVISE_PLAN"), "REVISE_PLAN")

    def test_case_insensitive(self):
        self.assertEqual(self.parse("aggregate: green"), "GREEN")

    def test_allows_leading_whitespace(self):
        self.assertEqual(self.parse("  AGGREGATE: REVISE_PLAN"), "REVISE_PLAN")

    def test_returns_empty_when_no_aggregate_line(self):
        text = "This is not a REVISE_PLAN case — the situation is fine.\nLooks GREEN to me overall."
        self.assertEqual(self.parse(text), "")

    def test_body_text_does_not_false_positive(self):
        """Reviewer explaining 'This would be REVISE_PLAN if X' should not trigger signal."""
        text = "Normally this would be REVISE_PLAN but in this case it is acceptable.\nAGGREGATE: REVIEW_REQUIRED"
        self.assertEqual(self.parse(text), "REVIEW_REQUIRED")

    def test_last_valid_aggregate_line_wins(self):
        """When multiple AGGREGATE lines exist, LAST one wins (self-correction semantics)."""
        text = "AGGREGATE: GREEN\nAGGREGATE: REVISE_PLAN"
        self.assertEqual(self.parse(text), "REVISE_PLAN")

    def test_last_aggregate_line_wins_when_model_self_corrects(self):
        """Model says REVIEW_REQUIRED, then corrects to REVISE_PLAN — final verdict wins."""
        text = "Initial thought: AGGREGATE: REVIEW_REQUIRED\nAfter reconsideration:\nAGGREGATE: REVISE_PLAN"
        self.assertEqual(self.parse(text), "REVISE_PLAN")


class TestPlanReviewToolRegistration(unittest.TestCase):
    def test_get_tools_returns_plan_task(self):
        from ouroboros.tools.plan_review import get_tools
        tools = get_tools()
        names = [t.name for t in tools]
        self.assertIn("plan_task", names)

    def test_plan_task_schema_has_required_fields(self):
        from ouroboros.tools.plan_review import get_tools
        tool = next(t for t in get_tools() if t.name == "plan_task")
        params = tool.schema["parameters"]["properties"]
        self.assertIn("plan", params)
        self.assertIn("goal", params)
        self.assertIn("files_to_touch", params)
        self.assertIn("context_level", params)
        self.assertEqual(tool.schema["parameters"]["required"], ["plan", "goal", "context_level"])
        self.assertNotIn("auto", params["context_level"].get("enum", []))

    def test_plan_review_deduplicates_canonical_docs_from_repo_pack(self):
        import inspect
        import ouroboros.tools.plan_review as pr

        source = inspect.getsource(pr._run_plan_review_async)
        assert '"BIBLE.md"' in source
        assert '"docs/DEVELOPMENT.md"' in source
        assert '"docs/ARCHITECTURE.md"' in source
        assert '"docs/CHECKLISTS.md"' in source

    def test_plan_review_prompt_points_to_plan_checklist_section(self):
        from ouroboros.tools.plan_review import _build_system_prompt

        prompt = _build_system_prompt("", "", "", "", "## Plan Review Checklist\n\n- completeness\n")

        assert "Use the `## Plan Review Checklist` section" in prompt
        assert "## CHECKLISTS.md" in prompt

    def test_plan_task_description_mentions_pre_implementation(self):
        from ouroboros.tools.plan_review import get_tools
        tool = next(t for t in get_tools() if t.name == "plan_task")
        desc = tool.schema["description"].lower()
        self.assertIn("before", desc)
        self.assertIn("code", desc)


class TestClassifyReviewerError(unittest.TestCase):
    """Tests for _classify_reviewer_error — readable error messages for reviewer failures."""

    def setUp(self):
        from ouroboros.tools.plan_review import _classify_reviewer_error
        self.classify = _classify_reviewer_error

    def test_json_decode_error_mentions_oversized_prompt(self):
        """JSONDecodeError → message explains the likely oversized-prompt root cause."""
        import json
        exc = json.JSONDecodeError("Expecting value", "", 0)
        msg = self.classify(exc, "openai/gpt-5.5")
        self.assertIn("non-JSON response body", msg)
        self.assertIn("oversized prompt", msg)
        self.assertIn("openai/gpt-5.5", msg)

    def test_json_decode_error_does_not_say_json_formatting_problem(self):
        """The user should not think it's a JSON format issue in our code."""
        import json
        exc = json.JSONDecodeError("Expecting value", "doc", 902)
        msg = self.classify(exc, "google/gemini-3.5-flash")
        # Should NOT say things like "JSON format" or "checklist formatting"
        self.assertNotIn("format", msg.lower().replace("non-JSON", ""))

    def test_json_decode_error_realistic_message(self):
        """Reproduces the exact JSONDecodeError seen in production logs."""
        import json
        # Exact args from the production failure:
        # "Expecting value: line 165 column 1 (char 902)"
        exc = json.JSONDecodeError("Expecting value", "line 165 column 1 (char 902)", 0)
        msg = self.classify(exc, "openai/gpt-5.5")
        self.assertIn("openai/gpt-5.5", msg)
        self.assertIn("non-JSON", msg)
        self.assertIn("oversized", msg)
        # The raw JSONDecodeError text should not be the ONLY content
        self.assertNotEqual(msg, str(exc))

    def test_generic_exception_preserves_type_and_message(self):
        """Unknown exception types fall back to 'TypeName: message' format."""
        exc = ValueError("something went wrong")
        msg = self.classify(exc, "some/model")
        self.assertIn("ValueError", msg)
        self.assertIn("something went wrong", msg)

    def test_timeout_error_fallback(self):
        """TimeoutError (if not caught before) is reported with its type."""
        exc = TimeoutError("connection timed out")
        msg = self.classify(exc, "my/model")
        self.assertIn("TimeoutError", msg)
        self.assertIn("connection timed out", msg)

    def test_model_name_always_included(self):
        """Model name should always appear in error message for traceability."""
        import json
        exc = json.JSONDecodeError("Expecting value", "", 0)
        msg = self.classify(exc, "very-specific/model-id-xyz")
        self.assertIn("very-specific/model-id-xyz", msg)

    def test_empty_exception_message_does_not_crash(self):
        """Exception with empty message string should not crash the helper."""
        exc = Exception("")
        msg = self.classify(exc, "test/model")
        self.assertIsInstance(msg, str)
        self.assertGreater(len(msg), 0)


if __name__ == "__main__":
    unittest.main()

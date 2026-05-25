from __future__ import annotations

import pathlib
from types import SimpleNamespace

import ouroboros.skill_lifecycle_queue as lifecycle_queue
from ouroboros.skill_loader import (
    SkillReviewState,
    compute_content_hash,
    load_enabled,
    load_review_state,
    load_skill_grants,
    save_review_state,
)
from ouroboros.skill_review import SkillReviewOutcome
from ouroboros.skill_review_runner import _review_result_message, run_skill_review_lifecycle_blocking


def _reset_queue() -> None:
    lifecycle_queue._events.clear()
    lifecycle_queue._active = None
    lifecycle_queue._lock = None
    lifecycle_queue._dedupe_jobs.clear()


def _build_extension(skills_root: pathlib.Path, name: str) -> pathlib.Path:
    skill_dir = skills_root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Review runner test.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text("def register(api):\n    return None\n", encoding="utf-8")
    return skill_dir


def _build_keyed_extension(skills_root: pathlib.Path, name: str) -> pathlib.Path:
    skill_dir = _build_extension(skills_root, name)
    manifest = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    manifest = manifest.replace("permissions: []\n", "permissions: [read_settings]\nenv_from_settings: [OPENROUTER_API_KEY]\n")
    (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")
    return skill_dir


def _mark_self_authored(skill_dir: pathlib.Path) -> None:
    payload = {
        "schema_version": 1,
        "origin": "self_authored",
        "task_id": "task-1",
        "created_at": "2026-05-07T00:00:00+00:00",
    }
    (skill_dir / ".self_authored.json").write_text(
        __import__("json").dumps(payload) + "\n",
        encoding="utf-8",
    )
    state = skill_dir.parents[2] / "state" / "skills" / skill_dir.name
    state.mkdir(parents=True, exist_ok=True)
    (state / "self_authored.json").write_text(__import__("json").dumps(payload) + "\n", encoding="utf-8")


def test_blocking_review_lifecycle_uses_single_progress_card(tmp_path, monkeypatch):
    _reset_queue()
    sent = []
    reconcile_calls = []
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = tmp_path / "skills"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir()
    skill_dir = _build_extension(skills_root, "alpha")
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    def fake_review(_ctx, skill_name):
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="pass",
            content_hash=content_hash,
            reviewer_models=["fake/reviewer"],
            findings=[{"item": "manifest_schema", "verdict": "PASS"}],
            error="",
        )

    def fake_reconcile(_ctx, skill_name, **_kwargs):
        reconcile_calls.append(lifecycle_queue.queue_snapshot()["active"]["target"])
        return "extension_loaded", "review_passed"

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", fake_send)
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_deps_after_pass_review", lambda *_a, **_k: ("installed", ""))
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_extension_payload", fake_reconcile)

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(skills_root),
    )

    assert payload["status"] == "clean"
    assert payload["deps_status"] == "installed"
    assert payload["extension_action"] == "extension_loaded"
    assert reconcile_calls == ["alpha"]

    progress_messages = [
        args[1]
        for args, kwargs in sent
        if kwargs.get("is_progress")
        and str(kwargs.get("task_id") or "").startswith("skill_lifecycle_review_alpha_")
    ]
    assert any("Running tri-model review" in message for message in progress_messages)
    assert any("Installing dependencies" in message for message in progress_messages)
    assert any("Reloading extension" in message for message in progress_messages)
    assert any("completed" in message and "Review executable (clean): PASS manifest_schema" in message for message in progress_messages)
    assert not any(kwargs.get("task_id") in {"skill_lifecycle_review", "api_skill_review"} for _args, kwargs in sent)


def test_review_lifecycle_installs_deps_after_warnings(tmp_path, monkeypatch):
    _reset_queue()
    deps_calls = []
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = tmp_path / "skills"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir()
    skill_dir = _build_extension(skills_root, "alpha")
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    def fake_review(_ctx, skill_name):
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="warnings",
            content_hash=content_hash,
            reviewer_models=["fake/reviewer#1", "fake/reviewer#2"],
            findings=[{"item": "error_handling", "verdict": "FAIL", "severity": "advisory"}],
            error="",
        )

    def fake_deps(*_args, **_kwargs):
        deps_calls.append("alpha")
        return "installed", ""

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: None)
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_deps_after_pass_review", fake_deps)
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_extension_payload", lambda *_a, **_k: (None, None))

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(skills_root),
    )

    assert payload["status"] == "warnings"
    assert payload["executable_review"] is True
    assert payload["review_gate"]["blocking_reason"] == "warnings_do_not_block_execution"
    assert payload["deps_status"] == "installed"
    assert deps_calls == ["alpha"]


def test_review_result_message_prefers_non_pass_findings_and_marks_omissions(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    long_reason = "x" * 400
    outcome = SkillReviewOutcome(
        skill_name="alpha",
        status="blockers",
        findings=[
            {"item": "manifest_schema", "verdict": "PASS", "reason": "ok"},
            {"item": "extension_namespace_discipline", "verdict": "FAIL", "reason": long_reason},
        ],
    )

    message = _review_result_message(outcome)

    assert message.startswith("Review blocked: blocker findings (blockers): FAIL extension_namespace_discipline")
    assert "manifest_schema" not in message
    assert "[omitted " in message
    assert "full findings in Skills page" in message


def test_review_result_message_allows_warnings_status(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    outcome = SkillReviewOutcome(
        skill_name="alpha",
        status="warnings",
        findings=[{"item": "bug_hunting", "verdict": "FAIL", "reason": "soft"}],
    )

    assert _review_result_message(outcome).startswith(
        "Review executable with findings (warnings):"
    )


def test_review_result_message_includes_auto_granted_keys(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    outcome = SkillReviewOutcome(
        skill_name="alpha",
        status="pass",
        findings=[{"item": "manifest_schema", "verdict": "PASS", "reason": "ok"}],
        auto_granted_keys=["OPENROUTER_API_KEY"],
    )

    assert _review_result_message(outcome).endswith(
        "| auto-granted: OPENROUTER_API_KEY"
    )


def test_review_result_message_includes_auto_granted_permissions(monkeypatch):
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    outcome = SkillReviewOutcome(
        skill_name="alpha",
        status="pass",
        findings=[{"item": "manifest_schema", "verdict": "PASS", "reason": "ok"}],
        auto_granted_permissions=["inject_chat"],
    )

    assert _review_result_message(outcome).endswith(
        "| auto-granted: permissions: inject_chat"
    )


def test_self_authored_review_lifecycle_uses_triad(tmp_path, monkeypatch):
    _reset_queue()
    sent = []
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_keyed_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: sent.append((a, kw)))
    monkeypatch.setattr(
        "ouroboros.skill_review_runner.load_settings",
        lambda: {"OPENROUTER_API_KEY": "sk-test"},
    )
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_deps_after_pass_review",
        lambda *_a, **_k: ("not_required", ""),
    )
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_extension_payload",
        lambda *_a, **_k: ("extension_loaded", "ready"),
    )

    def fake_review(_ctx, _skill_name):
        outcome = SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=content_hash,
            reviewer_models=["reviewer-a", "reviewer-b", "reviewer-c"],
            findings=[],
        )
        save_review_state(
            drive_root,
            "alpha",
            SkillReviewState(
                status=outcome.status,
                content_hash=outcome.content_hash,
                findings=outcome.findings,
                reviewer_models=outcome.reviewer_models,
            ),
        )
        return outcome

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "clean"
    assert payload["auto_flow"] is False
    assert load_enabled(drive_root, "alpha") is False
    review = load_review_state(drive_root, "alpha")
    assert review.status == "clean"
    assert review.content_hash == content_hash
    assert review.reviewer_models == ["reviewer-a", "reviewer-b", "reviewer-c"]
    grants = load_skill_grants(drive_root, "alpha")
    assert grants["granted_keys"] == []


def test_review_lifecycle_payload_surfaces_auto_flow_grants(tmp_path, monkeypatch):
    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_keyed_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: None)
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_deps_after_pass_review",
        lambda *_a, **_k: ("not_required", ""),
    )
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_extension_payload",
        lambda *_a, **_k: ("extension_loaded", "ready"),
    )

    def fake_review(_ctx, _skill_name):
        return SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=content_hash,
            reviewer_models=["reviewer"],
            auto_flow=True,
            requested_keys=["OPENROUTER_API_KEY"],
            auto_granted_keys=["OPENROUTER_API_KEY"],
            requested_permissions=["inject_chat"],
            auto_granted_permissions=["inject_chat"],
        )

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "clean"
    assert payload["auto_flow"] is True
    assert payload["requested_keys"] == ["OPENROUTER_API_KEY"]
    assert payload["auto_granted_keys"] == ["OPENROUTER_API_KEY"]
    assert payload["requested_permissions"] == ["inject_chat"]
    assert payload["auto_granted_permissions"] == ["inject_chat"]
    assert load_enabled(drive_root, "alpha") is True


def test_self_authored_review_does_not_enable_when_deps_fail(tmp_path, monkeypatch):
    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: None)
    monkeypatch.setattr("ouroboros.skill_review_runner._reconcile_deps_after_pass_review", lambda *_a, **_k: ("failed", "pip exploded"))

    def fake_review(_ctx, _skill):
        outcome = SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py"),
            reviewer_models=["reviewer"],
        )
        outcome.auto_flow = True
        return outcome

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "pending"
    assert payload["deps_status"] == "failed"
    assert "pip exploded" in payload["deps_error"]
    assert load_enabled(drive_root, "alpha") is False


def test_lifecycle_finish_writes_full_markdown_to_chat_jsonl(tmp_path, monkeypatch):
    """v5.18 Skill Review Feedback Overhaul: the on_finished callback writes a
    full markdown render of the outcome to ``logs/chat.jsonl`` as
    ``direction:"system"`` ``type:"skill_review"`` so the foreground agent
    sees every reviewer's full findings, not just the 180-char headline.
    """
    import json

    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = tmp_path / "skills"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir()
    skill_dir = _build_extension(skills_root, "alpha")
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    long_reason = (
        "ffmpeg invocation in handler.py:42 spawns a subprocess that exits within "
        "the request scope. Not a long-lived companion process — this finding "
        "should be advisory at most, see CHECKLISTS.md item 11."
    )
    raw_failure = "partial reviewer output that failed JSON parsing"

    def fake_review(_ctx, skill_name):
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="fail",
            content_hash=content_hash,
            reviewer_models=["openai/gpt-5.5", "google/gemini-3.5-flash"],
            findings=[
                {
                    "item": "companion_process_safety",
                    "verdict": "FAIL",
                    "severity": "critical",
                    "reason": long_reason,
                    "model": "openai/gpt-5.5",
                },
                {
                    "item": "companion_process_safety",
                    "verdict": "PASS",
                    "severity": "critical",
                    "reason": "Transient subprocess in handler scope.",
                    "model": "google/gemini-3.5-flash",
                },
            ],
            raw_actor_records=[{
                "model_id": "anthropic/claude-opus-4.6",
                "status": "parse_failure",
                "raw_text": raw_failure,
            }],
            error="",
        )

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **k: None)
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_deps_after_pass_review",
        lambda *_a, **_k: ("not_required", ""),
    )
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_extension_payload",
        lambda *_a, **_k: ("noop", "review_failed"),
    )

    run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=fake_review,
        repo_path=str(skills_root),
    )

    chat_path = drive_root / "logs" / "chat.jsonl"
    assert chat_path.exists(), "Expected chat.jsonl to be created on lifecycle finish"
    lines = [line for line in chat_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    skill_rows = [
        json.loads(line) for line in lines
        if json.loads(line).get("type") == "skill_review"
    ]
    assert len(skill_rows) == 1
    row = skill_rows[0]
    assert row["direction"] == "system"
    assert row["skill"] == "alpha"
    assert row["status"] == "blockers"
    assert row["attempt"] >= 1
    # Full markdown — no per-row truncation; the long reason must appear verbatim.
    assert long_reason in row["text"]
    assert raw_failure in row["text"]
    assert "Reviewer: openai/gpt-5.5" in row["text"]
    assert "Reviewer: google/gemini-3.5-flash" in row["text"]


def test_lifecycle_finish_writes_raw_only_review_to_chat_jsonl(tmp_path, monkeypatch):
    import json

    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = tmp_path / "skills"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir()
    skill_dir = _build_extension(skills_root, "alpha")
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])
    raw_text = "raw reviewer text from a parse failure"

    def fake_review(_ctx, skill_name):
        return SkillReviewOutcome(
            skill_name=skill_name,
            status="pending",
            content_hash=content_hash,
            reviewer_models=["fake/reviewer"],
            findings=[],
            raw_actor_records=[{
                "model_id": "fake/reviewer",
                "status": "parse_failure",
                "raw_text": raw_text,
            }],
            error="quorum failure",
        )

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **k: None)
    monkeypatch.setattr(
        "ouroboros.skill_review_runner._reconcile_extension_payload",
        lambda *_a, **_k: ("noop", "review_pending"),
    )
    run_skill_review_lifecycle_blocking(
        ctx, "alpha", source="test", review_impl=fake_review, repo_path=str(skills_root),
    )

    rows = [
        json.loads(line)
        for line in (drive_root / "logs" / "chat.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert rows[-1]["type"] == "skill_review"
    assert raw_text in rows[-1]["text"]


def test_self_authored_review_requires_configured_requested_keys(tmp_path, monkeypatch):
    _reset_queue()
    drive_root = tmp_path / "drive"
    repo_dir = tmp_path / "repo"
    skills_root = drive_root / "skills" / "external"
    drive_root.mkdir()
    repo_dir.mkdir()
    skills_root.mkdir(parents=True)
    skill_dir = _build_keyed_extension(skills_root, "alpha")
    _mark_self_authored(skill_dir)
    ctx = SimpleNamespace(drive_root=drive_root, repo_dir=repo_dir, messages=[])

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **kw: None)
    monkeypatch.setattr("ouroboros.skill_review_runner.load_settings", lambda: {})

    payload = run_skill_review_lifecycle_blocking(
        ctx,
        "alpha",
        source="test",
        review_impl=lambda _ctx, _skill: SkillReviewOutcome(
            skill_name="alpha",
            status="pass",
            content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py"),
            reviewer_models=["reviewer"],
        ),
        repo_path=str(drive_root / "skills"),
    )

    assert payload["status"] == "clean"
    assert load_enabled(drive_root, "alpha") is False

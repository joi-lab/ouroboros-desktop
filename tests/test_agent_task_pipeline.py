import json
from types import SimpleNamespace

import ouroboros.agent_task_pipeline as pipeline


def test_task_summary_prefers_direct_model_when_openrouter_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-5.5-mini")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "openai::gpt-5.5-mini")
    monkeypatch.setenv("OUROBOROS_MODEL", "openai::gpt-5.5")
    monkeypatch.setenv("OUROBOROS_MODEL_CODE", "openai::gpt-5.5")

    captured = {}

    class FakeLlm:
        def chat(self, *, messages, model, reasoning_effort, max_tokens):
            captured["messages"] = messages
            captured["model"] = model
            captured["reasoning_effort"] = reasoning_effort
            captured["max_tokens"] = max_tokens
            return {"content": "direct summary ok"}, {"cost": 0}

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    # Use rounds > 1 so the task is non-trivial and the LLM summary path is taken
    pipeline._run_task_summary(
        env=None,
        llm=FakeLlm(),
        task={"id": "task-123", "type": "task", "text": "Reply with exactly OK."},
        usage={"rounds": 3, "cost": 0.01, "result_status": "failed", "reason_code": "empty_final_text"},
        llm_trace={"tool_calls": [{"tool": "read_file", "args": {}}], "reasoning_notes": []},
        drive_logs=drive_logs,
    )

    assert captured["model"] == "openai::gpt-5.5-mini"
    chat_lines = (drive_logs / "chat.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(chat_lines) == 1
    payload = json.loads(chat_lines[0])
    assert payload["type"] == "task_summary"
    assert payload["text"] == "direct summary ok"
    # Non-trivial task metadata is persisted
    assert payload["tool_calls"] == 1
    assert payload["rounds"] == 3
    assert payload["result_status"] == "failed"
    assert payload["reason_code"] == "empty_final_text"


def test_task_summary_keeps_openrouter_model_when_key_present(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-5.5-mini")

    assert (
        pipeline._resolve_task_summary_model("google/gemini-3.5-flash")
        == "google/gemini-3.5-flash"
    )


def test_task_summary_accepts_openai_compatible_when_legacy_base_url_is_present(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-openai-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "anthropic/claude-opus-4.6")
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "openai-compatible::custom-model")
    monkeypatch.setenv("OUROBOROS_MODEL", "anthropic/claude-opus-4.6")
    monkeypatch.setenv("OUROBOROS_MODEL_CODE", "anthropic/claude-opus-4.6")

    assert (
        pipeline._resolve_task_summary_model("anthropic/claude-sonnet-4.6")
        == "openai-compatible::custom-model"
    )


def test_emit_task_results_queues_restart_after_final_events(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "_store_task_result", lambda *args, **kwargs: None)
    memory_calls = []
    monkeypatch.setattr(pipeline, "_run_chat_consolidation", lambda *args, **kwargs: memory_calls.append("chat"))
    monkeypatch.setattr(pipeline, "_run_scratchpad_consolidation", lambda *args, **kwargs: memory_calls.append("scratchpad"))
    monkeypatch.setattr(pipeline, "_run_post_task_processing_async", lambda *args, **kwargs: memory_calls.append("post_task"))

    pending_events = []
    ctx = SimpleNamespace(pending_restart_reason="apply timeout fix")
    env = SimpleNamespace(drive_root=tmp_path)
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline.emit_task_results(
        env=env,
        memory=object(),
        llm=object(),
        pending_events=pending_events,
        task={"id": "task-1", "type": "task", "chat_id": 1, "text": "do it"},
        text="All done",
        usage={"rounds": 2, "cost": 0.2},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
        ctx=ctx,
    )

    assert [evt["type"] for evt in pending_events] == [
        "send_message",
        "task_metrics",
        "task_done",
        "restart_request",
    ]
    assert pending_events[-1]["reason"] == "apply timeout fix"
    assert ctx.pending_restart_reason is None
    assert memory_calls == ["chat", "scratchpad", "post_task"]

    pending_events.clear()
    memory_calls.clear()
    pipeline.emit_task_results(
        env=env,
        memory=object(),
        llm=object(),
        pending_events=pending_events,
        task={"id": "child-1", "type": "task", "chat_id": 1, "text": "inspect", "delegation_role": "subagent", "memory_mode": "shared"},
        text="summary",
        usage={"rounds": 2, "cost": 0.2},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        start_time=0.0,
        drive_logs=drive_logs,
        ctx=SimpleNamespace(pending_restart_reason=""),
    )
    assert [evt["type"] for evt in pending_events] == ["send_message", "task_metrics", "task_done"]
    assert memory_calls == []


def test_build_trace_summary_shows_structured_failure_facts():
    trace = {
        "tool_calls": [{
            "tool": "run_command",
            "args": {"cmd": ["npm", "install", "-g", "@anthropic-ai/claude-code"]},
            "result": "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=-9 (signal=SIGKILL).",
            "is_error": True,
            "status": "non_zero_exit",
            "exit_code": -9,
            "signal": "SIGKILL",
        }],
        "reasoning_notes": ["Thought this might still work."],
    }

    summary = pipeline.build_trace_summary(trace)

    assert "status=non_zero_exit" in summary
    assert "exit_code=-9" in summary
    assert "signal=SIGKILL" in summary
    assert "Agent notes (supplementary, not source of truth)" in summary

    long_trace = {
        "tool_calls": [
            {
                "tool": "run_command",
                "args": {"cmd": "x" * 5000},
                "is_error": False,
            }
            for _ in range(40)
        ],
        "reasoning_notes": ["note" * 2000],
    }
    assert "OMISSION NOTE" in pipeline.build_trace_summary(long_trace)


def test_task_summary_prompt_includes_review_evidence(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-5.5-mini")

    captured = {}

    class FakeLlm:
        def chat(self, *, messages, model, reasoning_effort, max_tokens):
            captured["prompt"] = messages[0]["content"]
            return {"content": "summary with review evidence"}, {"cost": 0}

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline._run_task_summary(
        env=None,
        llm=FakeLlm(),
        task={"id": "task-review", "type": "task", "text": "Fix commit flow"},
        usage={"rounds": 4, "cost": 0.02},
        llm_trace={"tool_calls": [{"tool": "commit_reviewed", "args": {}}], "reasoning_notes": []},
        drive_logs=drive_logs,
        review_evidence={
            "has_evidence": True,
            "recent_attempts": [{
                "status": "blocked",
                "critical_findings": [{
                    "severity": "critical",
                    "item": "tests_affected",
                    "reason": "broken",
                }],
            }],
        },
    )

    assert "Structured review evidence" in captured["prompt"]
    assert "tests_affected" in captured["prompt"]
    assert "critical" in captured["prompt"]
    assert "meta-reflection" in captured["prompt"].lower()
    assert "What friction, errors, or weak assumptions slowed the work?" in captured["prompt"]
    assert "What should Ouroboros change in its own process or prompts" in captured["prompt"]
    assert "keep it to 1-2 sentences and DO NOT add meta-reflection" in captured["prompt"]


def test_trivial_task_summary_bypasses_llm_and_uses_short_format(tmp_path):
    class FailIfCalledLlm:
        def chat(self, *args, **kwargs):  # pragma: no cover - should never be called
            raise AssertionError("LLM summary path must be skipped for trivial tasks")

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline._run_task_summary(
        env=None,
        llm=FailIfCalledLlm(),
        task={"id": "task-trivial", "type": "task", "text": "Say hi"},
        usage={"rounds": 1, "cost": 0.0, "result_status": "infra_failed", "reason_code": "llm_api_error"},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        drive_logs=drive_logs,
    )

    payload = json.loads((drive_logs / "chat.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert payload["type"] == "task_summary"
    assert payload["task_id"] == "task-trivial"
    assert payload["text"] == "Task task-trivial (task): Say hi. 1r, $0.00."
    assert payload["tool_calls"] == 0
    assert payload["rounds"] == 1
    assert payload["result_status"] == "infra_failed"
    assert payload["reason_code"] == "llm_api_error"


def test_multi_round_zero_tool_task_uses_llm_summary_prompt(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "openai::gpt-5.5-mini")

    captured = {}

    class FakeLlm:
        def chat(self, *, messages, model, reasoning_effort, max_tokens):
            captured["prompt"] = messages[0]["content"]
            return {"content": "multi-round summary"}, {"cost": 0}

    drive_logs = tmp_path / "logs"
    drive_logs.mkdir(parents=True)

    pipeline._run_task_summary(
        env=None,
        llm=FakeLlm(),
        task={"id": "task-zero-tool-multi-round", "type": "task", "text": "Think carefully"},
        usage={"rounds": 3, "cost": 0.01},
        llm_trace={"tool_calls": [], "reasoning_notes": ["note"]},
        drive_logs=drive_logs,
    )

    assert "0 tool calls and ≤1 round" in captured["prompt"]
    assert "DO NOT add meta-reflection" in captured["prompt"]
    payload = json.loads((drive_logs / "chat.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert payload["text"] == "multi-round summary"
    assert payload["tool_calls"] == 0
    assert payload["rounds"] == 3


def test_store_task_result_persists_review_evidence(tmp_path):
    env = SimpleNamespace(drive_root=tmp_path)

    pipeline._store_task_result(
        env=env,
        task={"id": "task-store", "type": "task", "text": "hi"},
        text="done",
        usage={"rounds": 2, "cost": 0.1},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        review_evidence={"has_evidence": True, "open_obligations": [{"item": "tests_affected"}]},
    )

    payload = json.loads((tmp_path / "task_results" / "task-store.json").read_text(encoding="utf-8"))
    assert payload["review_evidence"]["has_evidence"] is True
    assert payload["review_evidence"]["open_obligations"][0]["item"] == "tests_affected"


def test_store_task_result_preserves_failed_status(tmp_path):
    from ouroboros.task_results import STATUS_FAILED, write_task_result

    env = SimpleNamespace(drive_root=tmp_path)
    write_task_result(tmp_path, "task-failed", STATUS_FAILED, result="initial failure")

    pipeline._store_task_result(
        env=env,
        task={"id": "task-failed", "type": "task", "text": "hi"},
        text="final failure reply",
        usage={"rounds": 1, "cost": 0.0},
        llm_trace={"tool_calls": [], "reasoning_notes": []},
        review_evidence={},
    )

    payload = json.loads((tmp_path / "task_results" / "task-failed.json").read_text(encoding="utf-8"))
    assert payload["status"] == STATUS_FAILED
    assert payload["result"] == "final failure reply"


def test_store_task_result_marks_unresolved_tool_failure_failed(tmp_path):
    from ouroboros.task_results import STATUS_FAILED

    env = SimpleNamespace(drive_root=tmp_path)

    pipeline._store_task_result(
        env=env,
        task={"id": "task-tool-failed", "type": "task", "text": "make file"},
        text="Created the file.",
        usage={"rounds": 2, "cost": 0.0},
        llm_trace={
            "tool_calls": [{
                "tool": "run_command",
                "args": {"cmd": "python3 -c ..."},
                "result": "⚠️ ARTIFACT_OUTPUT_ERROR: undeclared output",
                "is_error": True,
                "status": "artifact_output_error",
            }],
            "reasoning_notes": [],
        },
        review_evidence={},
    )

    payload = json.loads((tmp_path / "task_results" / "task-tool-failed.json").read_text(encoding="utf-8"))
    assert payload["status"] == STATUS_FAILED
    assert payload["result_status"] == "failed"
    assert payload["reason_code"] == "tool_failure"
    assert payload["loop_outcome"]["failure"]["tool_errors"][0]["status"] == "artifact_output_error"


def test_store_task_result_allows_recovered_tool_failure_success(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED

    env = SimpleNamespace(drive_root=tmp_path)

    pipeline._store_task_result(
        env=env,
        task={"id": "task-tool-recovered", "type": "task", "text": "make file"},
        text="Created the file.",
        usage={"rounds": 3, "cost": 0.0},
        llm_trace={
            "tool_calls": [
                {
                    "tool": "edit_text",
                    "args": {"path": "Desktop/report.html"},
                    "result": "⚠️ EDIT_TEXT_ERROR: old_str matched 0 times",
                    "is_error": True,
                    "status": "edit_text_blocked",
                },
                {
                    "tool": "write_file",
                    "args": {"root": "user_files", "path": "Desktop/report.html"},
                    "result": "OK: wrote user_files:Desktop/report.html\nARTIFACT_OUTPUTS: registered user file -> artifact_store:report.html",
                    "is_error": False,
                    "status": "ok",
                },
            ],
            "reasoning_notes": [],
        },
        review_evidence={},
    )

    payload = json.loads((tmp_path / "task_results" / "task-tool-recovered.json").read_text(encoding="utf-8"))
    assert payload["status"] == STATUS_COMPLETED
    assert payload["result_status"] == "succeeded"
    assert payload["loop_outcome"]["failure"] is None


def test_collect_review_evidence_keeps_recent_attempts_task_scoped(tmp_path):
    from ouroboros.review_evidence import collect_review_evidence
    from ouroboros.review_state import AdvisoryReviewState, CommitAttemptRecord, make_repo_key, save_state

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()

    state = AdvisoryReviewState()
    state.record_attempt(CommitAttemptRecord(
        ts="2026-04-07T10:00:00+00:00",
        commit_message="other task attempt",
        status="blocked",
        repo_key=make_repo_key(repo_dir),
        tool_name="commit_reviewed",
        task_id="task-other",
        attempt=1,
        block_reason="critical_findings",
    ))
    save_state(tmp_path, state)

    evidence = collect_review_evidence(
        tmp_path,
        task_id="task-current",
        repo_dir=repo_dir,
    )

    assert evidence["recent_attempts"] == []


def test_update_improvement_backlog_appends_candidates(tmp_path):
    env = SimpleNamespace(drive_root=tmp_path)

    added = pipeline._update_improvement_backlog(
        env,
        {
            "backlog_candidates": [{
                "summary": "Reduce recurring task friction around REVIEW_BLOCKED",
                "category": "process",
                "source": "execution_reflection",
                "task_id": "task-backlog",
                "evidence": "REVIEW_BLOCKED",
                "context": "The task retried blocked review loops without narrowing scope.",
                "proposed_next_step": "Run plan_task before touching review prompts again.",
            }],
        },
    )

    assert added == 1
    backlog_path = tmp_path / "memory" / "knowledge" / "improvement-backlog.md"
    assert backlog_path.exists()
    text = backlog_path.read_text(encoding="utf-8")
    assert "Reduce recurring task friction around REVIEW_BLOCKED" in text


def test_run_reflection_returns_entry_when_generated(tmp_path):
    captured = {}

    class FakeLlm:
        def chat(self, *, messages, model, reasoning_effort, max_tokens):
            captured["prompt"] = messages[0]["content"]
            return {
                "content": (
                    "Reflection text.\n"
                    "BACKLOG_CANDIDATES_JSON: "
                    "[{\"summary\":\"Reduce recurring task friction around REVIEW_BLOCKED\"," 
                    "\"category\":\"process\"," 
                    "\"source\":\"execution_reflection\"," 
                    "\"evidence\":\"REVIEW_BLOCKED\"}]"
                )
            }, {"cost": 0}

    env = SimpleNamespace(drive_root=tmp_path)
    (tmp_path / "logs").mkdir(parents=True)

    entry = pipeline._run_reflection(
        env,
        FakeLlm(),
        {"id": "task-reflect", "type": "task", "text": "Fix it"},
        {"rounds": 2, "cost": 0.01},
        {"tool_calls": [{"tool": "commit_reviewed", "is_error": False, "result": "⚠️ REVIEW_BLOCKED"}]},
        {"recent_attempts": [], "open_obligations": [{"item": "tests_affected", "reason": "Fix the failing test before commit"}]},
    )

    assert entry is not None
    assert entry["task_id"] == "task-reflect"
    assert entry["reflection"] == "Reflection text."
    assert len(entry["backlog_candidates"]) == 1
    assert entry["backlog_candidates"][0]["summary"] == "Reduce recurring task friction around REVIEW_BLOCKED"


def test_collect_review_evidence_scopes_open_obligations_to_repo(tmp_path):
    from ouroboros.review_evidence import collect_review_evidence
    from ouroboros.review_state import (
        AdvisoryReviewState,
        AdvisoryRunRecord,
        CommitAttemptRecord,
        compute_snapshot_hash,
        make_repo_key,
        save_state,
    )

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    (repo_a / ".git").mkdir()
    (repo_b / ".git").mkdir()
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
        tool_name="commit_reviewed",
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
    state.last_stale_from_edit_ts = "2026-04-07T10:02:00+00:00"
    state.last_stale_reason = "repo-b mutation"
    state.last_stale_repo_key = repo_b_key
    save_state(tmp_path, state)

    evidence = collect_review_evidence(tmp_path, repo_dir=repo_a)

    assert evidence["current_repo"]["repo_commit_ready"] is True
    assert evidence["current_repo"]["stale_reason"] == ""
    assert evidence["current_repo"]["stale_ts"] == ""
    assert evidence["open_obligations"] == []
    assert evidence["commit_readiness_debts"] == []


def test_collect_review_evidence_includes_commit_readiness_debt(tmp_path):
    from ouroboros.review_evidence import collect_review_evidence
    from ouroboros.review_state import AdvisoryReviewState, CommitAttemptRecord, make_repo_key, save_state

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".git").mkdir()
    (repo_dir / "tracked.py").write_text("print('hi')\n", encoding="utf-8")

    repo_key = make_repo_key(repo_dir)
    state = AdvisoryReviewState()
    for idx, reason in enumerate(["missing tests", "coverage still missing"], start=1):
        state.record_attempt(CommitAttemptRecord(
            ts=f"2026-04-07T10:0{idx}:00+00:00",
            commit_message=f"blocked {idx}",
            status="blocked",
            repo_key=repo_key,
            tool_name="commit_reviewed",
            task_id=f"task-{idx}",
            attempt=idx,
            block_reason="critical_findings",
            critical_findings=[{
                "item": "tests_affected",
                "reason": reason,
                "severity": "critical",
                "verdict": "FAIL",
            }],
            readiness_warnings=["Start retry from review debt."],
        ))
    save_state(tmp_path, state)

    evidence = collect_review_evidence(tmp_path, repo_dir=repo_dir)

    assert evidence["current_repo"]["repo_commit_ready"] is False
    assert len(evidence["commit_readiness_debts"]) >= 1
    assert evidence["commit_readiness_debts"][0]["category"] in {"obligation_repeat", "readiness_warning"}


def test_truncate_with_notice_uses_utils_ssot():
    """_truncate_with_notice in agent_task_pipeline is now truncate_review_artifact from utils.
    Verify it truncates long strings and adds a visible omission note (no silent clipping)."""
    from ouroboros.utils import truncate_review_artifact
    # The alias in agent_task_pipeline should be the same object
    assert pipeline._truncate_with_notice is truncate_review_artifact

    short = "hello"
    assert pipeline._truncate_with_notice(short, 100) == short

    long_text = "x" * 200
    result = pipeline._truncate_with_notice(long_text, 50)
    assert result.startswith("x" * 50)
    assert "50" in result  # omission note mentions limit
    assert len(result) > 50  # note appended, not just raw slice

    # Handles None gracefully
    assert pipeline._truncate_with_notice(None, 10) == ""

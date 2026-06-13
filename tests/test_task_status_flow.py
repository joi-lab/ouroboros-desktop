import json
import pathlib
import time
from types import SimpleNamespace


class _FakeEventQueue:
    def __init__(self, fail=False, status_root=None):
        self.fail = fail
        self.status_root = status_root
        self.events = []

    def put_nowait(self, evt):
        if self.fail:
            raise RuntimeError("queue unavailable")
        if self.status_root is not None:
            path = pathlib.Path(self.status_root) / "task_results" / f"{evt['task_id']}.json"
            data = json.loads(path.read_text(encoding="utf-8"))
            assert data["status"] == "requested"
        self.events.append(dict(evt))


def test_schedule_task_live_emits_strict_contract_and_requested_status(tmp_path):
    from ouroboros.tools.control import _schedule_task
    from ouroboros.task_results import STATUS_REQUESTED

    event_queue = _FakeEventQueue(status_root=tmp_path)
    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        event_queue=event_queue,
        drive_root=tmp_path,
        task_id="parent123",
        task_metadata={"root_task_id": "root123", "session_id": "sess123"},
        current_chat_id=777,
        is_direct_chat=False,
        is_workspace_mode=lambda: False,
    )

    result = _schedule_task(
        ctx,
        objective="Do the thing",
        expected_output="A concise handoff",
        role="architecture",
        context="Model focus A",
    )

    assert "Subagent request queued" in result
    assert ctx.pending_events == []
    assert len(event_queue.events) == 1
    evt = event_queue.events[0]
    task_id = evt["task_id"]
    assert evt["description"] == "Do the thing"
    assert evt["expected_output"] == "A concise handoff"
    assert evt["role"] == "architecture"
    assert evt["parent_task_id"] == "parent123"
    assert evt["root_task_id"] == "root123"
    assert evt["session_id"] == "sess123"
    assert evt["chat_id"] == 777
    assert evt["delegation_role"] == "subagent"
    assert evt["memory_mode"] == "forked"
    assert pathlib.Path(evt["drive_root"]).parts[-3:] == ("headless_tasks", task_id, "data")
    assert evt["child_drive_root"] == evt["drive_root"]
    assert evt["budget_drive_root"] == str(tmp_path)
    assert evt["task_constraint"]["mode"] == "local_readonly_subagent"
    path = tmp_path / "task_results" / f"{task_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REQUESTED
    assert data["description"] == "Do the thing"
    assert data["expected_output"] == "A concise handoff"
    assert data["role"] == "architecture"
    assert data["context"] == "Model focus A"
    assert data["chat_id"] == 777
    assert data["memory_mode"] == "forked"
    assert data["child_drive_root"] == evt["drive_root"]


def test_schedule_task_falls_back_to_pending_events_when_live_queue_unavailable(tmp_path, monkeypatch):
    from ouroboros.tools import control as control_mod
    from ouroboros.tools.control import _schedule_task

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        event_queue=_FakeEventQueue(fail=True),
        drive_root=tmp_path,
        task_id="parent123",
        task_metadata={},
        is_direct_chat=False,
        is_workspace_mode=lambda: False,
    )

    result = _schedule_task(ctx, objective="Fallback child", expected_output="Result")

    assert "Subagent request queued" in result
    assert len(ctx.pending_events) == 1
    assert ctx.pending_events[0]["objective"] == "Fallback child"

    event_queue = _FakeEventQueue()
    ctx.pending_events = []
    ctx.event_queue = event_queue
    monkeypatch.setattr(control_mod, "write_task_result", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk full")))
    result = _schedule_task(ctx, objective="No status", expected_output="No child")
    assert "SUBTASK_STATUS_ERROR" in result
    assert ctx.pending_events == []
    assert event_queue.events == []


def test_cancel_task_latches_cancel_requested_and_emits_live(tmp_path):
    from ouroboros.tools.control import _cancel_task
    from ouroboros.task_results import (
        STATUS_CANCEL_REQUESTED, STATUS_RUNNING, load_task_result, write_task_result,
    )

    # Pre-existing running status: the latch must advance running -> cancel_requested.
    write_task_result(tmp_path, "child42", STATUS_RUNNING, result="working")
    event_queue = _FakeEventQueue()
    ctx = SimpleNamespace(
        task_depth=0, pending_events=[], event_queue=event_queue,
        drive_root=tmp_path, task_id="parent123", task_metadata={},
        is_direct_chat=False, is_workspace_mode=lambda: False,
    )

    result = _cancel_task(ctx, "child42")

    assert "Cancel requested" in result
    # The latch is actually written (a missing write_task_result import would
    # silently skip this and leave the status at running).
    assert load_task_result(tmp_path, "child42")["status"] == STATUS_CANCEL_REQUESTED
    # And the cancel is emitted live (not buffered to round end).
    assert any(e.get("type") == "cancel_task" and e.get("task_id") == "child42" for e in event_queue.events)


def test_schedule_task_memory_modes_prepare_declared_drive_shape(tmp_path):
    from ouroboros.tools.control import _schedule_task

    parent_memory = tmp_path / "memory"
    (parent_memory / "knowledge").mkdir(parents=True)
    (parent_memory / "identity.md").write_text("stable identity", encoding="utf-8")
    (parent_memory / "scratchpad.md").write_text("working scratch", encoding="utf-8")
    (parent_memory / "knowledge" / "pattern.md").write_text("stable pattern", encoding="utf-8")

    event_queue = _FakeEventQueue()
    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        event_queue=event_queue,
        drive_root=tmp_path,
        task_id="parent123",
        task_metadata={},
        is_direct_chat=False,
        is_workspace_mode=lambda: False,
    )

    _schedule_task(ctx, objective="Fork child", expected_output="Result", memory_mode="forked")
    forked_drive = tmp_path / "state" / "headless_tasks" / event_queue.events[-1]["task_id"] / "data"
    assert event_queue.events[-1]["drive_root"] == str(forked_drive)
    assert (forked_drive / "memory" / "identity.md").read_text(encoding="utf-8") == "stable identity"
    assert not (forked_drive / "memory" / "scratchpad.md").exists()
    assert (forked_drive / "memory" / "knowledge" / "pattern.md").is_file()

    _schedule_task(ctx, objective="Empty child", expected_output="Result", memory_mode="empty")
    empty_drive = tmp_path / "state" / "headless_tasks" / event_queue.events[-1]["task_id"] / "data"
    assert event_queue.events[-1]["drive_root"] == str(empty_drive)
    assert not (empty_drive / "memory" / "identity.md").exists()

    before_shared = len(event_queue.events)
    shared_result = _schedule_task(ctx, objective="Shared child", expected_output="Result", memory_mode="shared")
    assert "TOOL_ARG_ERROR" in shared_result
    assert "memory_mode=shared is disabled" in shared_result
    assert len(event_queue.events) == before_shared


def test_schedule_task_rejects_legacy_description_schema(tmp_path):
    from ouroboros.tools.control import _schedule_task

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        event_queue=None,
        drive_root=tmp_path,
        task_id="parent123",
        task_metadata={},
        is_direct_chat=False,
        is_workspace_mode=lambda: False,
    )

    result = _schedule_task(ctx, description="legacy", context="old", parent_task_id="p1")

    assert "TOOL_ARG_ERROR" in result
    assert "description" in result
    assert ctx.pending_events == []
    assert not (tmp_path / "task_results").exists()


def test_schedule_task_workspace_mode_blocked_does_not_enqueue(tmp_path):
    from ouroboros.tools.control import _schedule_task

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        event_queue=_FakeEventQueue(),
        drive_root=tmp_path,
        task_id="parent123",
        task_metadata={},
        is_direct_chat=False,
        is_workspace_mode=lambda: True,
    )

    result = _schedule_task(ctx, objective="Blocked", expected_output="Nothing")

    assert "WORKSPACE_MODE_BLOCKED" in result
    assert ctx.pending_events == []
    assert ctx.event_queue.events == []


def test_get_task_result_returns_full_completed_output(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.control import _get_task_result

    full_text = ("hello\n" * 1200) + "TAIL_MARKER"
    write_task_result(
        tmp_path,
        "abc123",
        STATUS_COMPLETED,
        result=full_text,
        cost_usd=1.23,
        trace_summary="trace",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _get_task_result(ctx, "abc123")

    assert "TAIL_MARKER" in output
    assert full_text in output
    assert "[BEGIN_SUBTASK_OUTPUT]" in output


def test_get_task_result_uses_child_terminal_over_stale_parent(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_SCHEDULED, write_task_result
    from ouroboros.tools.control import _get_task_result

    child_drive = tmp_path / "state" / "headless_tasks" / "child123" / "data"
    child_drive.mkdir(parents=True)
    write_task_result(
        tmp_path,
        "child123",
        STATUS_SCHEDULED,
        child_drive_root=str(child_drive),
        result="stale parent handoff",
    )
    write_task_result(
        child_drive,
        "child123",
        STATUS_COMPLETED,
        result="child terminal handoff",
        cost_usd=0.42,
        trace_summary="child trace",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _get_task_result(ctx, "child123")

    assert "child terminal handoff" in output
    assert "stale parent handoff" not in output
    assert "[SUBTASK_TRACE]" in output


def test_wait_for_tasks_returns_structured_effective_batch(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_SCHEDULED, write_task_result
    from ouroboros.tools.control import _wait_for_tasks

    child_drive = tmp_path / "state" / "headless_tasks" / "childdone" / "data"
    child_drive.mkdir(parents=True)
    write_task_result(tmp_path, "parentdone", STATUS_COMPLETED, result="parent finished")
    write_task_result(tmp_path, "childdone", STATUS_SCHEDULED, child_drive_root=str(child_drive), result="queued")
    write_task_result(child_drive, "childdone", STATUS_COMPLETED, result="child finished", trace_summary="trace")

    ctx = SimpleNamespace(drive_root=tmp_path)
    payload = json.loads(_wait_for_tasks(ctx, ["parentdone", "childdone"], timeout_sec=0))

    assert payload["all_terminal"] is True
    assert payload["timed_out"] is False
    assert payload["tasks"]["parentdone"]["result"] == "parent finished"
    assert payload["tasks"]["childdone"]["result"] == "child finished"
    assert payload["tasks"]["childdone"]["trace_summary"] == "trace"


def test_effective_status_keeps_workspace_finalization_nonterminal_without_child_drive(tmp_path):
    from ouroboros.headless import ARTIFACT_STATUS_FINALIZING
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, write_task_result
    from ouroboros.task_status import load_effective_task_result, wait_for_effective_tasks

    write_task_result(
        tmp_path,
        "workspace1",
        STATUS_COMPLETED,
        workspace_root=str(tmp_path / "workspace"),
        artifact_status=ARTIFACT_STATUS_FINALIZING,
        result="worker finished but artifacts are still pending",
    )

    effective = load_effective_task_result(tmp_path, "workspace1")
    waited = wait_for_effective_tasks(tmp_path, ["workspace1"], timeout_sec=0)

    assert effective["status"] == STATUS_RUNNING
    assert effective["child_status"] == STATUS_COMPLETED
    assert effective["artifact_status"] == ARTIFACT_STATUS_FINALIZING
    assert waited["all_terminal"] is False
    assert waited["timed_out"] is True


def test_effective_status_repairs_stale_running_infra_failure_when_queue_empty(tmp_path):
    from ouroboros.headless import ARTIFACT_STATUS_FINALIZING, ARTIFACT_STATUS_FAILED
    from ouroboros.task_results import STATUS_FAILED, STATUS_RUNNING, write_task_result
    from ouroboros.task_status import load_effective_task_result

    write_task_result(
        tmp_path,
        "providerfail",
        STATUS_RUNNING,
        workspace_root=str(tmp_path / "workspace"),
        artifact_status=ARTIFACT_STATUS_FINALIZING,
        result_status="infra_failed",
        reason_code="provider_failure",
        result="provider error",
        artifact_bundle={
            "status": ARTIFACT_STATUS_FINALIZING,
            "artifacts": [
                {"name": "deck.html", "status": ARTIFACT_STATUS_FINALIZING, "errors": []},
            ],
        },
    )
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "queue_snapshot.json").write_text('{"pending": [], "running": []}', encoding="utf-8")

    effective = load_effective_task_result(tmp_path, "providerfail")

    assert effective["status"] == STATUS_FAILED
    assert effective["status_reconciled_from"] == STATUS_RUNNING
    assert effective["artifact_status"] == ARTIFACT_STATUS_FAILED
    assert effective["artifact_bundle"]["status"] == ARTIFACT_STATUS_FAILED
    assert effective["artifact_bundle"]["artifacts"][0]["status"] == ARTIFACT_STATUS_FAILED
    assert "task ended before artifact finalization" in effective["artifact_bundle"]["artifacts"][0]["errors"]


def test_effective_status_does_not_repair_running_when_queue_snapshot_missing(tmp_path):
    from ouroboros.task_results import STATUS_RUNNING, write_task_result
    from ouroboros.task_status import load_effective_task_result

    write_task_result(
        tmp_path,
        "providerfail",
        STATUS_RUNNING,
        result_status="infra_failed",
        reason_code="provider_failure",
        result="provider error",
    )

    effective = load_effective_task_result(tmp_path, "providerfail")

    assert effective["status"] == STATUS_RUNNING
    assert effective["queue_reconciliation_warning"] == "queue snapshot missing or invalid"


def test_effective_status_repairs_orphan_running_after_worker_restart(tmp_path, monkeypatch):
    from ouroboros.headless import ARTIFACT_STATUS_FINALIZING, ARTIFACT_STATUS_FAILED
    from ouroboros.task_results import STATUS_FAILED, STATUS_RUNNING, write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.utils import append_jsonl

    monkeypatch.setattr(time, "time", lambda: 1_800_000_000.0)
    write_task_result(
        tmp_path,
        "cc4db6fa",
        STATUS_RUNNING,
        result="Task is running.",
        ts="2026-05-28T00:00:00+00:00",
        artifact_status=ARTIFACT_STATUS_FINALIZING,
        artifact_bundle={
            "status": ARTIFACT_STATUS_FINALIZING,
            "artifacts": [
                {"name": "presentation.html", "status": ARTIFACT_STATUS_FINALIZING, "errors": []},
            ],
        },
    )
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / "queue_snapshot.json").write_text('{"pending": [], "running": []}', encoding="utf-8")
    events = tmp_path / "logs" / "events.jsonl"
    append_jsonl(events, {"ts": "2026-05-28T00:00:01+00:00", "type": "llm_round", "task_id": "cc4db6fa"})
    append_jsonl(events, {"ts": "2026-05-28T00:00:02+00:00", "type": "worker_boot"})

    effective = load_effective_task_result(tmp_path, "cc4db6fa")

    assert effective["status"] == STATUS_FAILED
    assert effective["status_reconciled_from"] == STATUS_RUNNING
    assert effective["result_status"] == "infra_failed"
    assert effective["reason_code"] == "orphaned_running_after_worker_restart"
    assert "TASK_ORPHAN_RECONCILED" in effective["result"]
    assert effective["artifact_status"] == ARTIFACT_STATUS_FAILED
    assert effective["artifact_bundle"]["artifacts"][0]["status"] == ARTIFACT_STATUS_FAILED
    assert "task interrupted before artifact finalization" in effective["artifact_bundle"]["artifacts"][0]["errors"]


def test_find_child_tasks_does_not_regress_terminal_or_running_from_stale_queue_snapshot(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, write_task_result
    from ouroboros.task_status import find_child_tasks, load_effective_task_result

    write_task_result(
        tmp_path,
        "childdone",
        STATUS_COMPLETED,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        result="terminal handoff",
    )
    write_task_result(
        tmp_path,
        "childrun",
        STATUS_RUNNING,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        result="still working",
    )
    snapshot = {
        "pending": [
            {"id": "childdone", "task": {"id": "childdone", "parent_task_id": "parent1", "root_task_id": "parent1", "delegation_role": "subagent"}},
            {"id": "childrun", "task": {"id": "childrun", "parent_task_id": "parent1", "root_task_id": "parent1", "delegation_role": "subagent"}},
        ],
        "running": [],
    }
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "queue_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    effective_done = load_effective_task_result(tmp_path, "childdone")
    effective_running = load_effective_task_result(tmp_path, "childrun")
    children = {row["task_id"]: row for row in find_child_tasks(tmp_path, parent_task_id="parent1", root_task_id="parent1")}

    assert effective_done["status"] == STATUS_COMPLETED
    assert effective_running["status"] == STATUS_RUNNING
    assert children["childdone"]["status"] == STATUS_COMPLETED
    assert children["childrun"]["status"] == STATUS_RUNNING


def test_effective_status_preserves_parent_retry_status_over_stale_child_running(tmp_path):
    from ouroboros.task_results import STATUS_INTERRUPTED, STATUS_RUNNING, STATUS_SCHEDULED, write_task_result
    from ouroboros.task_status import load_effective_task_result

    child_drive = tmp_path / "state" / "headless_tasks" / "childretry" / "data"
    child_drive.mkdir(parents=True)
    write_task_result(
        tmp_path,
        "childretry",
        STATUS_INTERRUPTED,
        child_drive_root=str(child_drive),
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        result="parent marked retry",
        error="worker interrupted",
        ts="2026-01-01T00:00:02Z",
    )
    write_task_result(
        child_drive,
        "childretry",
        STATUS_RUNNING,
        result="stale child still running",
        error="",
        ts="2026-01-01T00:00:01Z",
    )
    snapshot = {
        "pending": [
            {
                "id": "childretry",
                "task": {
                    "id": "childretry",
                    "parent_task_id": "parent1",
                    "root_task_id": "parent1",
                    "delegation_role": "subagent",
                },
            }
        ],
        "running": [],
    }
    (tmp_path / "state" / "queue_snapshot.json").write_text(json.dumps(snapshot), encoding="utf-8")

    effective = load_effective_task_result(tmp_path, "childretry")

    assert effective["status"] == STATUS_SCHEDULED
    assert effective["result"] == "parent marked retry"
    assert effective["error"] == "worker interrupted"


def test_find_child_tasks_requires_subagent_role_and_can_exclude_current_task(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_RUNNING, write_task_result
    from ouroboros.task_status import find_child_tasks, format_handoff_message

    write_task_result(
        tmp_path,
        "forgedroot",
        STATUS_COMPLETED,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="root",
        result="should not be treated as child",
    )
    write_task_result(
        tmp_path,
        "child1",
        STATUS_RUNNING,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        role="reviewer",
        result="x" * 2000,
        trace_summary="trace" * 500,
    )

    children = find_child_tasks(tmp_path, parent_task_id="parent1", root_task_id="parent1")
    excluded = find_child_tasks(tmp_path, parent_task_id="parent1", root_task_id="parent1", exclude_task_id="child1")
    handoff = format_handoff_message(children)

    assert [row["task_id"] for row in children] == ["child1"]
    assert excluded == []
    assert "should not be treated as child" not in handoff
    assert len(handoff) < 1200
    assert "Use get_task_result" in handoff
    assert "result_chars" in handoff


def test_wait_for_task_times_out_when_child_is_not_terminal(tmp_path):
    from ouroboros.task_results import STATUS_RUNNING, write_task_result
    from ouroboros.tools.control import _wait_for_task

    write_task_result(tmp_path, "stillrunning", STATUS_RUNNING, result="working")

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _wait_for_task(ctx, "stillrunning", timeout_sec=0)

    assert "Task wait timed out" in output
    assert "stillrunning [running]" in output


def test_wait_tools_reject_invalid_ids_and_cap_batch(tmp_path):
    from ouroboros.tools.control import _wait_for_task, _wait_for_tasks

    ctx = SimpleNamespace(drive_root=tmp_path)

    assert "TOOL_ARG_ERROR" in _wait_for_task(ctx, "../settings", timeout_sec=0)
    assert "TOOL_ARG_ERROR" in _wait_for_tasks(ctx, ["ok123", "../bad"], timeout_sec=0)
    assert "capped at 50" in _wait_for_tasks(ctx, [f"task{i}" for i in range(51)], timeout_sec=0)


def test_wait_for_task_reports_rejected_duplicate(tmp_path):
    from ouroboros.task_results import STATUS_REJECTED_DUPLICATE, write_task_result
    from ouroboros.tools.control import _wait_for_task

    write_task_result(
        tmp_path,
        "dup123",
        STATUS_REJECTED_DUPLICATE,
        duplicate_of="orig999",
        result="Task was rejected as semantically similar to already active task orig999.",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _wait_for_task(ctx, "dup123")

    assert "rejected_duplicate" in output
    assert "duplicate_of=orig999" in output


def test_handle_schedule_task_duplicate_writes_rejected_status(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_REJECTED_DUPLICATE

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: "orig111")

    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "dup222",
            "objective": "Do the thing",
            "expected_output": "Duplicate verdict",
            "context": "Model focus B",
            "depth": 1,
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "dup222" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "dup222" / "data"),
        },
        FakeCtx(),
    )

    path = tmp_path / "task_results" / "dup222.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REJECTED_DUPLICATE
    assert data["duplicate_of"] == "orig111"
    assert sent and "semantically similar" in sent[0][1]
    assert sent[0][2]["is_progress"] is True
    assert sent[0][2]["progress_meta"]["delegation_role"] == "subagent"
    assert sent[0][2]["progress_meta"]["parent_task_id"] == ""
    assert sent[0][2]["progress_meta"]["status"] == STATUS_REJECTED_DUPLICATE


def test_find_duplicate_task_includes_subagent_handoff_fields(monkeypatch):
    from supervisor import events as ev_module
    import ouroboros.config as config_module
    import ouroboros.llm as llm_module

    captured = {}

    class FakeClient:
        def chat(self, messages, **kwargs):
            captured["prompt"] = messages[0]["content"]
            return {"content": "NONE"}, {}

    monkeypatch.setattr(config_module, "get_light_model", lambda: "test-light")
    monkeypatch.setattr(llm_module, "LLMClient", lambda: FakeClient())

    result = ev_module._find_duplicate_task(
        "Review shared surface",
        "same context",
        [
            {
                "id": "pending1",
                "description": "Review shared surface",
                "context": "same context",
                "expected_output": "Docs table",
                "constraints": "docs only",
                "role": "docs reviewer",
            }
        ],
        {},
        expected_output="Security table",
        constraints="security only",
        role="security reviewer",
    )

    assert result is None
    prompt = captured["prompt"]
    assert "Expected output:\nSecurity table" in prompt
    assert "Expected output:\nDocs table" in prompt
    assert "Constraints:\nsecurity only" in prompt
    assert "Constraints:\ndocs only" in prompt
    assert "Role:\nsecurity reviewer" in prompt
    assert "Role:\ndocs reviewer" in prompt


def test_handle_schedule_task_accepts_unique_subagent_with_lineage_and_constraint(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_SCHEDULED

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    enqueued = []
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            enqueued.append(task)

        def persist_queue_snapshot(self, reason=""):
            self.snapshot_reason = reason

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "child123",
            "objective": "Inspect scheduling",
            "expected_output": "Findings table",
            "constraints": "No writes",
            "role": "reviewer",
            "context": "Parent facts",
            "depth": 1,
            "parent_task_id": "parent123",
            "root_task_id": "root123",
            "session_id": "sess123",
            "actor_id": "subagent:reviewer",
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "child123" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "child123" / "data"),
            "budget_drive_root": str(tmp_path),
            "task_constraint": {"mode": "skill_repair", "allow_enable": True, "allow_review": True},
        },
        FakeCtx(),
    )

    assert len(enqueued) == 1
    task = enqueued[0]
    assert task["id"] == "child123"
    assert task["parent_task_id"] == "parent123"
    assert task["root_task_id"] == "root123"
    assert task["session_id"] == "sess123"
    assert task["role"] == "reviewer"
    assert task["memory_mode"] == "forked"
    assert task["child_drive_root"] == task["drive_root"]
    assert task["task_constraint"]["mode"] == "local_readonly_subagent"
    assert task["task_constraint"]["allow_enable"] is False
    assert task["task_constraint"]["allow_review"] is False
    assert "[EXPECTED_OUTPUT]" in task["text"]
    assert "[BEGIN_PARENT_CONTEXT" in task["text"]
    data = json.loads((tmp_path / "task_results" / "child123.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_SCHEDULED
    assert data["expected_output"] == "Findings table"
    assert data["child_drive_root"] == task["drive_root"]
    assert data["task_constraint"]["mode"] == "local_readonly_subagent"
    assert sent and sent[0][2].get("is_progress") is True


def test_handle_schedule_task_rejects_internal_subagent_without_child_drive_contract(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_FAILED

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            raise AssertionError("invalid internal subagent should not enqueue")

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "badchild",
            "objective": "Inspect invalid event",
            "expected_output": "Nothing",
            "depth": 1,
            "delegation_role": "subagent",
            "memory_mode": "shared",
        },
        FakeCtx(),
    )

    data = json.loads((tmp_path / "task_results" / "badchild.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert "memory_mode=forked or empty" in data["result"]
    assert sent and sent[0][2]["progress_meta"]["subagent_event"] == "rejected"
    assert sent[0][2]["progress_meta"]["delegation_role"] == "subagent"
    assert sent[0][2]["progress_meta"]["parent_task_id"] == ""
    assert sent[0][2]["progress_meta"]["status"] == STATUS_FAILED


def test_handle_schedule_task_uses_event_chat_id_without_owner(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_FAILED, STATUS_SCHEDULED

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    enqueued = []
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            enqueued.append(task)

        def persist_queue_snapshot(self, reason=""):
            self.snapshot_reason = reason

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "headless1",
            "objective": "Inspect no-owner path",
            "expected_output": "Findings",
            "depth": 1,
            "chat_id": 44,
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "headless1" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "headless1" / "data"),
        },
        FakeCtx(),
    )

    assert len(enqueued) == 1
    assert enqueued[0]["chat_id"] == 44
    scheduled = json.loads((tmp_path / "task_results" / "headless1.json").read_text(encoding="utf-8"))
    assert scheduled["status"] == STATUS_SCHEDULED
    assert scheduled["chat_id"] == 44
    assert sent and sent[0][0] == 44

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "headless2",
            "objective": "Inspect missing chat target",
            "expected_output": "Findings",
            "depth": 1,
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "headless2" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "headless2" / "data"),
        },
        FakeCtx(),
    )

    failed = json.loads((tmp_path / "task_results" / "headless2.json").read_text(encoding="utf-8"))
    assert failed["status"] == STATUS_FAILED
    assert "no chat target" in failed["result"]


def test_handle_schedule_task_depth_rejection_writes_failed_status(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_FAILED

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            raise AssertionError("depth-rejected task should not enqueue")

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "deep1",
            "objective": "Too deep",
            "expected_output": "Nothing",
            "depth": ev_module.MAX_SUBTASK_DEPTH + 1,
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "deep1" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "deep1" / "data"),
        },
        FakeCtx(),
    )

    data = json.loads((tmp_path / "task_results" / "deep1.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert "depth limit" in data["result"]
    assert sent and "depth limit" in sent[0][1]
    assert sent[0][2]["is_progress"] is True
    assert sent[0][2]["progress_meta"]["delegation_role"] == "subagent"
    assert sent[0][2]["progress_meta"]["status"] == STATUS_FAILED


def test_handle_schedule_task_rejects_legacy_subagent_event_schema(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_FAILED

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    enqueued = []
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            enqueued.append(task)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "legacy123",
            "description": "Old child form",
            "context": "old reference",
            "parent_task_id": "parent123",
            "delegation_role": "subagent",
        },
        FakeCtx(),
    )

    assert enqueued == []
    data = json.loads((tmp_path / "task_results" / "legacy123.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert "objective and expected_output" in data["result"]
    assert sent and "objective and expected_output" in sent[0][1]
    assert sent[0][2]["is_progress"] is True
    assert sent[0][2]["progress_meta"]["delegation_role"] == "subagent"
    assert sent[0][2]["progress_meta"]["parent_task_id"] == "parent123"
    assert sent[0][2]["progress_meta"]["status"] == STATUS_FAILED


def test_handle_schedule_task_rejects_fourth_active_subagent(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_FAILED, load_task_result, write_task_result

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = [{"id": f"p{i}", "root_task_id": "root123", "delegation_role": "subagent"} for i in range(2)]
        RUNNING = {"r1": {"task": {"id": "r1", "root_task_id": "root123", "delegation_role": "subagent"}}}
        WORKERS = {0: SimpleNamespace(busy_task_id=None)}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            raise AssertionError("should not enqueue")

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "child999",
            "objective": "Too many",
            "expected_output": "Nothing",
            "depth": 1,
            "root_task_id": "root123",
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "child999" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "child999" / "data"),
        },
        FakeCtx(),
    )

    data = json.loads((tmp_path / "task_results" / "child999.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert "active child limit" in data["result"]
    assert sent and "active child limit" in sent[0][1]
    assert sent[0][2]["is_progress"] is True
    assert sent[0][2]["progress_meta"]["delegation_role"] == "subagent"
    assert sent[0][2]["progress_meta"]["status"] == STATUS_FAILED

    child_drive = tmp_path / "state" / "headless_tasks" / "childdone" / "data"
    (child_drive / "memory").mkdir(parents=True)
    (child_drive / "memory" / "identity.md").write_text("child identity", encoding="utf-8")
    write_task_result(child_drive, "childdone", STATUS_COMPLETED, result="summary")

    sent = []
    worker = SimpleNamespace(busy_task_id="childdone")
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={
            "childdone": {
                "task": {
                    "id": "childdone",
                    "chat_id": 1,
                    "drive_root": str(child_drive),
                    "delegation_role": "subagent",
                    "role": "reviewer",
                    "root_task_id": "root123",
                    "parent_task_id": "parent123",
                    "task_constraint": {"mode": "local_readonly_subagent", "allow_enable": False},
                }
            }
        },
        WORKERS={7: worker},
        bridge=SimpleNamespace(push_log=lambda _payload: None),
        send_with_budget=lambda chat_id, text, **kwargs: sent.append((chat_id, text, kwargs)),
        persist_queue_snapshot=lambda reason="": None,
    )

    ev_module._handle_task_done({"task_id": "childdone", "worker_id": 7, "task_type": "task"}, ctx)

    assert load_task_result(tmp_path, "childdone")["result"] == "summary"
    assert not (tmp_path / "task_results" / "artifacts" / "childdone" / "memory_export.json").exists()
    assert sent and sent[-1][2]["progress_meta"]["subagent_role"] == "reviewer"

    failed_drive = tmp_path / "state" / "headless_tasks" / "childfail" / "data"
    (failed_drive / "task_results").mkdir(parents=True)
    write_task_result(failed_drive, "childfail", STATUS_FAILED, result="boom")
    sent = []
    worker = SimpleNamespace(busy_task_id="childfail")
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={
            "childfail": {
                "task": {
                    "id": "childfail",
                    "chat_id": 1,
                    "drive_root": str(failed_drive),
                    "delegation_role": "subagent",
                    "role": "reviewer",
                    "root_task_id": "root123",
                    "parent_task_id": "parent123",
                    "task_constraint": {"mode": "local_readonly_subagent", "allow_enable": False},
                }
            }
        },
        WORKERS={8: worker},
        bridge=SimpleNamespace(push_log=lambda _payload: None),
        send_with_budget=lambda chat_id, text, **kwargs: sent.append((chat_id, text, kwargs)),
        persist_queue_snapshot=lambda reason="": None,
    )

    ev_module._handle_task_done({"task_id": "childfail", "worker_id": 8, "task_type": "task"}, ctx)

    assert load_task_result(tmp_path, "childfail")["status"] == STATUS_FAILED
    assert sent and "failed" in sent[-1][1]
    assert sent[-1][2]["progress_meta"]["subagent_event"] == "failed"


def test_handle_schedule_task_fails_fast_when_worker_pool_unavailable(tmp_path, monkeypatch):
    """When the worker pool is empty (e.g. disabled after a crash storm), a
    schedule must NOT be left as a 'scheduled' ghost — it gets a terminal
    workers_unavailable result so the parent can act."""
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_FAILED

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}
        WORKERS = {}  # pool disabled / not available

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            raise AssertionError("must not enqueue when worker pool is unavailable")

        def persist_queue_snapshot(self, reason=""):
            pass

    ev_module._handle_schedule_task(
        {
            "type": "schedule_subagent",
            "task_id": "ghost1",
            "objective": "Work with no workers",
            "expected_output": "Nothing",
            "depth": 1,
            "root_task_id": "rootX",
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "drive_root": str(tmp_path / "state" / "headless_tasks" / "ghost1" / "data"),
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "ghost1" / "data"),
        },
        FakeCtx(),
    )

    data = json.loads((tmp_path / "task_results" / "ghost1.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert data.get("reason_code") == "workers_unavailable"


def test_handle_task_done_finalizes_workspace_subagent_artifacts(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    import ouroboros.headless as headless
    from ouroboros.task_results import STATUS_COMPLETED, write_task_result

    calls = []
    monkeypatch.setattr(headless, "copy_child_task_result", lambda root, task: calls.append(("copy", task["id"])))

    def fake_finalize(root, task):
        calls.append(("finalize", task["id"]))
        write_task_result(
            pathlib.Path(root),
            task["id"],
            STATUS_COMPLETED,
            result="done",
            artifact_status="failed",
            artifact_bundle={"status": "failed", "artifacts": []},
        )

    monkeypatch.setattr(headless, "finalize_task_artifacts", fake_finalize)
    pushed = []

    worker = SimpleNamespace(busy_task_id="workspace-child")
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={
            "workspace-child": {
                "task": {
                    "id": "workspace-child",
                    "chat_id": 1,
                    "delegation_role": "subagent",
                    "role": "workspace-reviewer",
                    "root_task_id": "root123",
                    "parent_task_id": "parent123",
                    "workspace_root": str(tmp_path / "workspace"),
                    "task_constraint": {"mode": "workspace"},
                }
            }
        },
        WORKERS={3: worker},
        bridge=SimpleNamespace(push_log=lambda payload: pushed.append(payload)),
        send_with_budget=lambda *args, **kwargs: None,
        persist_queue_snapshot=lambda reason="": None,
    )

    ev_module._handle_task_done({"task_id": "workspace-child", "worker_id": 3, "task_type": "task"}, ctx)

    assert ("copy", "workspace-child") in calls
    assert ("finalize", "workspace-child") in calls
    assert pushed[-1]["artifact_status"] == "failed"
    assert pushed[-1]["artifact_bundle"]["status"] == "failed"


def test_queue_snapshot_preserves_subagent_contract_fields(tmp_path, monkeypatch):
    from supervisor import queue as queue_module

    snapshot_path = tmp_path / "state" / "queue_snapshot.json"
    monkeypatch.setattr(queue_module, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_module, "QUEUE_SNAPSHOT_PATH", snapshot_path)
    monkeypatch.setattr(queue_module, "PENDING", [])
    monkeypatch.setattr(queue_module, "RUNNING", {})
    monkeypatch.setattr(queue_module, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    monkeypatch.setattr(queue_module, "append_jsonl", lambda *args, **kwargs: None)

    queue_module.PENDING.append(
        {
            "id": "sub1",
            "type": "task",
            "chat_id": 1,
            "text": "subagent prompt",
            "description": "Review shared surface",
            "objective": "Review shared surface",
            "expected_output": "Distinct handoff table",
            "constraints": "No writes",
            "role": "security reviewer",
            "context": "same context",
            "parent_task_id": "parent1",
            "root_task_id": "root1",
            "session_id": "sess1",
            "actor_id": "subagent:security",
            "delegation_role": "subagent",
            "memory_mode": "forked",
            "child_drive_root": str(tmp_path / "state" / "headless_tasks" / "sub1" / "data"),
            "task_constraint": {"mode": "local_readonly_subagent", "allow_enable": False},
        }
    )

    queue_module.persist_queue_snapshot(reason="test")
    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))["pending"][0]["task"]
    assert saved["objective"] == "Review shared surface"
    assert saved["expected_output"] == "Distinct handoff table"
    assert saved["constraints"] == "No writes"
    assert saved["role"] == "security reviewer"
    assert pathlib.Path(saved["child_drive_root"]).parts[-4:] == ("state", "headless_tasks", "sub1", "data")
    assert saved["task_constraint"]["mode"] == "local_readonly_subagent"

    queue_module.PENDING.clear()
    assert queue_module.restore_pending_from_snapshot(max_age_sec=900) == 1
    restored = queue_module.PENDING[0]
    assert restored["objective"] == "Review shared surface"
    assert restored["expected_output"] == "Distinct handoff table"
    assert restored["constraints"] == "No writes"
    assert restored["role"] == "security reviewer"
    assert pathlib.Path(restored["child_drive_root"]).parts[-4:] == ("state", "headless_tasks", "sub1", "data")
    assert restored["task_constraint"]["mode"] == "local_readonly_subagent"


def test_assign_tasks_mirrors_running_subagent_status_to_parent_drive(tmp_path, monkeypatch):
    from ouroboros.task_results import STATUS_RUNNING, load_task_result
    from supervisor import queue as queue_module
    from supervisor import state as state_module
    from supervisor import workers as workers_module

    child_drive = tmp_path / "state" / "headless_tasks" / "childrun" / "data"
    child_drive.mkdir(parents=True)
    delivered = []

    class FakeWorkerQueue:
        def put(self, task):
            delivered.append(dict(task))

    task = {
        "id": "childrun",
        "type": "task",
        "chat_id": 1,
        "description": "Inspect handoff",
        "objective": "Inspect handoff",
        "expected_output": "Findings",
        "parent_task_id": "parent123",
        "root_task_id": "root123",
        "session_id": "sess123",
        "actor_id": "subagent:reviewer",
        "delegation_role": "subagent",
        "role": "reviewer",
        "memory_mode": "forked",
        "drive_root": str(child_drive),
        "child_drive_root": str(child_drive),
        "budget_drive_root": str(tmp_path),
        "task_constraint": {"mode": "local_readonly_subagent", "allow_enable": False},
        "metadata": {"root_task_id": "root123"},
    }
    monkeypatch.setattr(workers_module, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers_module, "PENDING", [task])
    monkeypatch.setattr(workers_module, "RUNNING", {})
    monkeypatch.setattr(workers_module, "WORKERS", {1: SimpleNamespace(wid=1, busy_task_id=None, in_q=FakeWorkerQueue())})
    monkeypatch.setattr(workers_module, "load_state", lambda: {})
    monkeypatch.setattr(state_module, "budget_remaining", lambda _state: 100.0)
    monkeypatch.setattr(queue_module, "persist_queue_snapshot", lambda reason="": None)

    workers_module.assign_tasks()

    parent_result = load_task_result(tmp_path, "childrun")
    assert parent_result["status"] == STATUS_RUNNING
    assert parent_result["child_drive_root"] == str(child_drive)
    assert parent_result["result"] == "Subagent assigned to a worker."
    assert delivered and delivered[0]["id"] == "childrun"


def test_subagent_hard_timeout_retry_preserves_task_id(tmp_path, monkeypatch):
    from supervisor import queue as queue_module
    from supervisor import workers as workers_module
    from ouroboros.task_results import STATUS_INTERRUPTED, load_task_result

    class FakeProc:
        pid = 12345

        def is_alive(self):
            return False

        def terminate(self):
            raise AssertionError("already dead")

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(queue_module, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_module, "PENDING", [])
    monkeypatch.setattr(queue_module, "RUNNING", {})
    monkeypatch.setattr(queue_module, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    monkeypatch.setattr(queue_module, "HARD_TIMEOUT_SEC", 1)
    monkeypatch.setattr(queue_module, "SOFT_TIMEOUT_SEC", 1)
    monkeypatch.setattr(queue_module, "QUEUE_MAX_RETRIES", 1)
    monkeypatch.setattr(queue_module, "load_state", lambda: {})
    monkeypatch.setattr(queue_module, "append_jsonl", lambda *args, **kwargs: None)
    monkeypatch.setattr(queue_module, "persist_queue_snapshot", lambda reason="": None)
    worker = SimpleNamespace(busy_task_id="childtimeout", proc=FakeProc())
    monkeypatch.setattr(workers_module, "WORKERS", {9: worker})
    monkeypatch.setattr(workers_module, "respawn_worker", lambda worker_id: None)
    child_drive = tmp_path / "child-drive"
    service_dir = child_drive / "services" / "childtimeout"
    service_dir.mkdir(parents=True)
    (service_dir / "devserver.log").write_text("READY\n", encoding="utf-8")

    queue_module.RUNNING["childtimeout"] = {
        "task": {
            "id": "childtimeout",
            "type": "task",
            "chat_id": 1,
            "delegation_role": "subagent",
            "drive_root": str(child_drive),
            "child_drive_root": str(child_drive),
            "_attempt": 1,
        },
        "started_at": time.time() - 10,
        "last_heartbeat_at": time.time() - 10,
        "worker_id": 9,
        "attempt": 1,
    }

    queue_module.enforce_task_timeouts()

    assert queue_module.PENDING
    retried = queue_module.PENDING[0]
    assert retried["id"] == "childtimeout"
    assert retried["_attempt"] == 2
    assert retried["timeout_retry_from"] == "childtimeout"
    assert load_task_result(tmp_path, "childtimeout")["status"] == STATUS_INTERRUPTED
    assert "childtimeout" not in queue_module.RUNNING
    assert not service_dir.exists()


def test_handle_text_response_keeps_full_reasoning_note():
    from ouroboros.loop import _handle_text_response

    content = "A" * 500
    llm_trace = {"reasoning_notes": [], "tool_calls": []}
    _, _, updated = _handle_text_response(content, llm_trace, {})

    assert updated["reasoning_notes"] == [content]


def test_request_restart_latches_reason_until_task_end(tmp_path, monkeypatch):
    from ouroboros.tools import control as control_module

    monkeypatch.setattr(control_module, "run_cmd", lambda *args, **kwargs: "value")
    written = {}
    monkeypatch.setattr(
        control_module,
        "atomic_write_json",
        lambda path, payload: written.setdefault(str(path), payload),
    )

    class _Ctx:
        current_task_type = "task"
        last_push_succeeded = True
        pending_events = []
        pending_restart_reason = None
        repo_dir = tmp_path

        def drive_path(self, rel):
            return tmp_path / rel

    ctx = _Ctx()
    result = control_module._request_restart(ctx, "reload runtime")

    assert "Restart requested" in result
    assert ctx.pending_events == []
    assert ctx.pending_restart_reason == "reload runtime"
    assert written

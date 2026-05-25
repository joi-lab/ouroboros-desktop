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

    _schedule_task(ctx, objective="Shared child", expected_output="Result", memory_mode="shared")
    assert "drive_root" not in event_queue.events[-1]
    shared_id = event_queue.events[-1]["task_id"]
    shared_status = json.loads((tmp_path / "task_results" / f"{shared_id}.json").read_text(encoding="utf-8"))
    assert shared_status["memory_mode"] == "shared"
    assert shared_status["drive_root"] == ""


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

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "dup222",
            "objective": "Do the thing",
            "expected_output": "Duplicate verdict",
            "context": "Model focus B",
            "depth": 1,
        },
        FakeCtx(),
    )

    path = tmp_path / "task_results" / "dup222.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REJECTED_DUPLICATE
    assert data["duplicate_of"] == "orig111"
    assert sent and "Task rejected" in sent[0][1]


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
        WORKERS = {}

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
            "type": "schedule_task",
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
    assert task["task_constraint"]["mode"] == "local_readonly_subagent"
    assert task["task_constraint"]["allow_enable"] is False
    assert task["task_constraint"]["allow_review"] is False
    assert "[EXPECTED_OUTPUT]" in task["text"]
    assert "[BEGIN_PARENT_CONTEXT" in task["text"]
    data = json.loads((tmp_path / "task_results" / "child123.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_SCHEDULED
    assert data["expected_output"] == "Findings table"
    assert data["task_constraint"]["mode"] == "local_readonly_subagent"
    assert sent and sent[0][2].get("is_progress") is True


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
        WORKERS = {}

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
            "type": "schedule_task",
            "task_id": "headless1",
            "objective": "Inspect no-owner path",
            "expected_output": "Findings",
            "depth": 1,
            "chat_id": 44,
            "delegation_role": "subagent",
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
            "type": "schedule_task",
            "task_id": "headless2",
            "objective": "Inspect missing chat target",
            "expected_output": "Findings",
            "depth": 1,
            "delegation_role": "subagent",
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
        WORKERS = {}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            raise AssertionError("depth-rejected task should not enqueue")

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "deep1",
            "objective": "Too deep",
            "expected_output": "Nothing",
            "depth": ev_module.MAX_SUBTASK_DEPTH + 1,
            "delegation_role": "subagent",
        },
        FakeCtx(),
    )

    data = json.loads((tmp_path / "task_results" / "deep1.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert "depth limit" in data["result"]
    assert sent and "depth limit" in sent[0][1]


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
        WORKERS = {}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            enqueued.append(task)

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
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


def test_handle_schedule_task_rejects_fourth_active_subagent(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_COMPLETED, STATUS_FAILED, load_task_result, write_task_result

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: None)
    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = [{"id": f"p{i}", "root_task_id": "root123", "delegation_role": "subagent"} for i in range(2)]
        RUNNING = {"r1": {"task": {"id": "r1", "root_task_id": "root123", "delegation_role": "subagent"}}}
        WORKERS = {}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs))

        def enqueue_task(self, task):
            raise AssertionError("should not enqueue")

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "child999",
            "objective": "Too many",
            "expected_output": "Nothing",
            "depth": 1,
            "root_task_id": "root123",
            "delegation_role": "subagent",
        },
        FakeCtx(),
    )

    data = json.loads((tmp_path / "task_results" / "child999.json").read_text(encoding="utf-8"))
    assert data["status"] == STATUS_FAILED
    assert "active child limit" in data["result"]
    assert sent and "active child limit" in sent[0][1]

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


def test_handle_task_done_finalizes_workspace_subagent_artifacts(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    import ouroboros.headless as headless

    calls = []
    monkeypatch.setattr(headless, "copy_child_task_result", lambda root, task: calls.append(("copy", task["id"])))
    monkeypatch.setattr(headless, "finalize_task_artifacts", lambda root, task: calls.append(("finalize", task["id"])))

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
        bridge=SimpleNamespace(push_log=lambda _payload: None),
        send_with_budget=lambda *args, **kwargs: None,
        persist_queue_snapshot=lambda reason="": None,
    )

    ev_module._handle_task_done({"task_id": "workspace-child", "worker_id": 3, "task_type": "task"}, ctx)

    assert ("copy", "workspace-child") in calls
    assert ("finalize", "workspace-child") in calls


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
            "task_constraint": {"mode": "local_readonly_subagent", "allow_enable": False},
        }
    )

    queue_module.persist_queue_snapshot(reason="test")
    saved = json.loads(snapshot_path.read_text(encoding="utf-8"))["pending"][0]["task"]
    assert saved["objective"] == "Review shared surface"
    assert saved["expected_output"] == "Distinct handoff table"
    assert saved["constraints"] == "No writes"
    assert saved["role"] == "security reviewer"
    assert saved["task_constraint"]["mode"] == "local_readonly_subagent"

    queue_module.PENDING.clear()
    assert queue_module.restore_pending_from_snapshot(max_age_sec=900) == 1
    restored = queue_module.PENDING[0]
    assert restored["objective"] == "Review shared surface"
    assert restored["expected_output"] == "Distinct handoff table"
    assert restored["constraints"] == "No writes"
    assert restored["role"] == "security reviewer"
    assert restored["task_constraint"]["mode"] == "local_readonly_subagent"


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

    queue_module.RUNNING["childtimeout"] = {
        "task": {
            "id": "childtimeout",
            "type": "task",
            "chat_id": 1,
            "delegation_role": "subagent",
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

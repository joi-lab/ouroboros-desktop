from __future__ import annotations

import json
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from ouroboros.gateway.tasks import api_task_events, api_task_get, api_tasks_create, api_tasks_list, iter_task_events
from ouroboros.headless import build_memory_export, build_workspace_patch
from ouroboros.task_results import write_task_result
from ouroboros.tools.core import _repo_read
from ouroboros.tools.registry import ToolContext, ToolRegistry
from ouroboros.utils import utc_now_iso


def test_task_api_enqueue_workspace_creates_child_drive(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    data = tmp_path / "data"
    (data / "memory").mkdir(parents=True)
    (data / "memory" / "identity.md").write_text("seed identity", encoding="utf-8")

    captured = []

    def fake_enqueue(task):
        captured.append(dict(task))
        return task

    monkeypatch.setattr("supervisor.queue.enqueue_task", fake_enqueue)
    monkeypatch.setattr("supervisor.queue.persist_queue_snapshot", lambda reason="": None)

    app = Starlette(routes=[Route("/api/tasks", endpoint=api_tasks_create, methods=["POST"])])
    app.state.drive_root = data
    app.state.repo_dir = repo
    response = TestClient(app).post(
        "/api/tasks",
        json={
            "description": "fix it",
            "workspace_root": str(workspace),
            "memory_mode": "forked",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["task_id"]
    assert captured and captured[0]["workspace_root"] == str(workspace.resolve(strict=False))
    child_drive = captured[0]["drive_root"]
    assert child_drive
    assert (tmp_path / "data" / "task_results" / f"{payload['task_id']}.json").is_file()
    assert "seed identity" in (data / "state" / "headless_tasks" / payload["task_id"] / "data" / "memory" / "identity.md").read_text(encoding="utf-8")


def test_task_api_rejects_unsafe_task_id_and_system_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr("supervisor.queue.enqueue_task", lambda task: task)
    monkeypatch.setattr("supervisor.queue.persist_queue_snapshot", lambda reason="": None)

    app = Starlette(routes=[Route("/api/tasks", endpoint=api_tasks_create, methods=["POST"])])
    app.state.drive_root = data
    app.state.repo_dir = repo
    client = TestClient(app)

    bad_id = client.post("/api/tasks", json={"description": "x", "task_id": "../settings", "workspace_root": str(workspace)})
    assert bad_id.status_code == 400
    assert not (data / "settings.json").exists()

    system_repo = client.post("/api/tasks", json={"description": "x", "workspace_root": str(repo)})
    assert system_repo.status_code == 400
    assert "system repo" in system_repo.json()["error"]

    bad_numbers = client.post("/api/tasks", json={"description": "x", "chat_id": "not-int", "workspace_root": str(workspace)})
    assert bad_numbers.status_code == 400

    first = client.post("/api/tasks", json={"description": "x", "task_id": "fixed1", "workspace_root": str(workspace)})
    assert first.status_code == 200
    duplicate = client.post("/api/tasks", json={"description": "x", "task_id": "fixed1", "workspace_root": str(workspace)})
    assert duplicate.status_code == 409

    typed = client.post("/api/tasks", json={"description": "x", "type": "deep_self_review", "workspace_root": str(workspace)})
    assert typed.status_code == 400


def test_task_event_replay_uses_existing_logs_and_result(tmp_path):
    data = tmp_path / "data"
    logs = data / "logs"
    logs.mkdir(parents=True)
    task_id = "abc123"
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-01-01T00:00:00Z", "task_id": task_id, "content": "working"}) + "\n",
        encoding="utf-8",
    )
    result_dir = data / "task_results"
    result_dir.mkdir()
    (result_dir / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "status": "completed", "result": "done", "ts": "2026-01-01T00:00:01Z"}),
        encoding="utf-8",
    )

    events = iter_task_events(data, task_id)

    assert [event["type"] for event in events] == ["progress", "task_result"]
    assert events[0]["seq"] == 1
    assert events[1]["data"]["result"] == "done"


def test_task_sse_emits_final_result_after_cursor_saw_scheduled_result(tmp_path):
    data = tmp_path / "data"
    (data / "task_results").mkdir(parents=True)
    task_id = "abc123"
    (data / "task_results" / f"{task_id}.json").write_text(
        json.dumps({"task_id": task_id, "status": "completed", "result": "done", "ts": "2026-01-01T00:00:01Z"}),
        encoding="utf-8",
    )
    app = Starlette(routes=[Route("/api/tasks/{task_id}/events", endpoint=api_task_events, methods=["GET"])])
    app.state.drive_root = data

    response = TestClient(app).get(f"/api/tasks/{task_id}/events?cursor=1&wait=0")

    assert response.status_code == 200
    assert '"type": "task_result"' in response.text
    assert '"status": "completed"' in response.text


def test_task_list_filters_on_effective_child_status(tmp_path):
    data = tmp_path / "data"
    child_running = tmp_path / "child-running"
    child_done = tmp_path / "child-done"
    for root in (data, child_running, child_done):
        (root / "task_results").mkdir(parents=True)

    write_task_result(data, "task-running", "scheduled", child_drive_root=str(child_running), result="queued")
    write_task_result(child_running, "task-running", "running", result="working", ts="2026-01-01T00:00:01Z")
    write_task_result(data, "task-done", "scheduled", child_drive_root=str(child_done), result="queued")
    write_task_result(child_done, "task-done", "completed", result="done", ts="2026-01-01T00:00:02Z")

    app = Starlette(routes=[Route("/api/tasks", endpoint=api_tasks_list, methods=["GET"])])
    app.state.drive_root = data
    client = TestClient(app)

    running = client.get("/api/tasks?status=running").json()["tasks"]
    completed = client.get("/api/tasks?status=completed").json()["tasks"]

    assert [task["task_id"] for task in running] == ["task-running"]
    assert running[0]["result"] == "working"
    assert [task["task_id"] for task in completed] == ["task-done"]
    assert completed[0]["result"] == "done"


@pytest.mark.parametrize("status", ["cancelled", "failed"])
def test_effective_task_result_preserves_parent_terminal_status(tmp_path, status):
    data = tmp_path / "data"
    child = tmp_path / "child"
    for root in (data, child):
        (root / "task_results").mkdir(parents=True)
    write_task_result(
        data,
        "task-terminal",
        status,
        child_drive_root=str(child),
        result="parent terminal",
        ts="2026-01-01T00:00:02Z",
    )
    write_task_result(
        child,
        "task-terminal",
        "running",
        result="child stale",
        ts="2026-01-01T00:00:03Z",
    )

    app = Starlette(routes=[Route("/api/tasks/{task_id}", endpoint=api_task_get, methods=["GET"])])
    app.state.drive_root = data

    payload = TestClient(app).get("/api/tasks/task-terminal").json()

    assert payload["status"] == status
    assert payload["result"] == "parent terminal"
    assert payload["ts"] == "2026-01-01T00:00:02Z"


def test_workspace_context_routes_repo_tools_and_blocks_self_commit(tmp_path):
    system_repo = tmp_path / "system"
    workspace = tmp_path / "workspace"
    data = tmp_path / "data"
    system_repo.mkdir()
    workspace.mkdir()
    data.mkdir()
    (system_repo / "README.md").write_text("system", encoding="utf-8")
    (workspace / "README.md").write_text("workspace", encoding="utf-8")
    (workspace / "BIBLE.md").write_text("external bible", encoding="utf-8")

    ctx = ToolContext(
        repo_dir=system_repo,
        drive_root=data,
        workspace_root=workspace,
        workspace_mode="external",
    )

    assert "workspace" in _repo_read(ctx, "README.md")
    registry = ToolRegistry(repo_dir=system_repo, drive_root=data)
    registry.set_context(ctx)
    assert "WORKSPACE_MODE_BLOCKED" in registry.execute("repo_commit", {"commit_message": "nope"})
    assert registry.get_schema_by_name("repo_commit") is None
    assert registry.get_schema_by_name("request_restart") is None
    assert "WORKSPACE_MODE_BLOCKED" in registry.execute("request_restart", {"reason": "nope"})
    assert "Written" in registry.execute("repo_write", {"path": "BIBLE.md", "content": "external edit"})
    assert (workspace / "BIBLE.md").read_text(encoding="utf-8") == "external edit"
    replaced = registry.execute(
        "str_replace_editor",
        {"path": "README.md", "old_str": "workspace", "new_str": "workspace edited"},
    )
    assert "Replaced" in replaced
    assert (workspace / "README.md").read_text(encoding="utf-8") == "workspace edited"


def test_workspace_run_shell_blocks_escaping_cwd(tmp_path):
    system_repo = tmp_path / "system"
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    data = tmp_path / "data"
    for path in (system_repo, workspace, outside, data):
        path.mkdir()
    ctx = ToolContext(
        repo_dir=system_repo,
        drive_root=data,
        workspace_root=workspace,
        workspace_mode="external",
    )
    registry = ToolRegistry(repo_dir=system_repo, drive_root=data)
    registry.set_context(ctx)

    result = registry.execute("run_shell", {"cmd": ["pwd"], "cwd": str(outside)})

    assert "SHELL_CWD_BLOCKED" in result
    git_escape = registry.execute("run_shell", {"cmd": ["git", "-C", str(system_repo), "status"]})
    assert "WORKSPACE_GIT_BLOCKED" in git_escape
    git_chain = registry.execute("run_shell", {"cmd": ["sh", "-c", "true && git commit -m nope"]})
    assert "WORKSPACE_GIT_BLOCKED" in git_chain
    outside_write = registry.execute("run_shell", {"cmd": ["touch", str(system_repo / "README.md")]})
    assert "WORKSPACE_SHELL_BLOCKED" in outside_write
    embedded_outside_write = registry.execute(
        "run_shell",
        {"cmd": ["python", "-c", "open('/tmp/ouroboros-outside.txt','w').write('x')"]},
    )
    assert "WORKSPACE_SHELL_BLOCKED" in embedded_outside_write


def test_workspace_patch_includes_tracked_and_untracked_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    (repo / "tracked.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "tracked.txt").write_text("new\n", encoding="utf-8")
    (repo / "new.txt").write_text("hello\n", encoding="utf-8")

    patch = build_workspace_patch(repo)

    assert "diff --git a/tracked.txt b/tracked.txt" in patch
    assert "+new" in patch
    assert "diff --git" in patch and "new.txt" in patch


def test_memory_export_includes_nested_memory_files(tmp_path):
    drive = tmp_path / "child"
    memory = drive / "memory"
    nested = memory / "knowledge" / "patterns"
    nested.mkdir(parents=True)
    (memory / "identity.md").write_text("id\n", encoding="utf-8")
    (nested / "cli.md").write_text("pattern\n", encoding="utf-8")

    export = build_memory_export(drive, {"id": "task-1", "memory_mode": "forked"})

    assert export["files"]["identity.md"] == "id\n"
    assert export["files"]["knowledge/patterns/cli.md"] == "pattern\n"


def test_external_child_task_budget_uses_parent_drive_state(tmp_path, monkeypatch):
    from ouroboros.agent import Env, OuroborosAgent

    repo = tmp_path / "repo"
    parent = tmp_path / "parent-data"
    child = tmp_path / "child-data"
    for root in (repo, parent, child):
        root.mkdir()
    for drive in (parent, child):
        (drive / "state").mkdir()
        (drive / "logs").mkdir()
    (parent / "state" / "state.json").write_text('{"spent_usd": 9.0}\n', encoding="utf-8")
    (child / "state" / "state.json").write_text('{"spent_usd": 0.0}\n', encoding="utf-8")

    monkeypatch.setenv("TOTAL_BUDGET", "10")
    monkeypatch.setattr(OuroborosAgent, "_log_worker_boot_once", lambda self: None)
    monkeypatch.setattr("ouroboros.agent.build_llm_messages", lambda **kwargs: ([], {}))

    agent = OuroborosAgent(Env(repo_dir=repo, drive_root=child))
    ctx, _messages, cap_info = agent._prepare_task_context({
        "id": "budget-task",
        "type": "task",
        "text": "x",
        "budget_drive_root": str(parent),
    })

    assert cap_info["budget_remaining"] == 1.0
    assert ctx.task_metadata["budget_drive_root"] == str(parent)


def test_cli_patch_requires_accessible_server_local_artifact(tmp_path):
    from ouroboros.cli import CLIError, _patch_from_result

    missing = tmp_path / "missing.patch"
    result = {"artifacts": [{"kind": "workspace_patch", "path": str(missing)}]}

    with pytest.raises(CLIError, match="same-filesystem"):
        _patch_from_result(result)


def test_cli_has_no_file_or_review_commit_groups():
    from ouroboros.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["run", "hello"]).command == "run"
    with pytest.raises(SystemExit):
        parser.parse_args(["files"])
    with pytest.raises(SystemExit):
        parser.parse_args(["commit"])
    with pytest.raises(SystemExit):
        parser.parse_args(["review"])
    with pytest.raises(SystemExit):
        parser.parse_args(["skills", "review", "demo"])


def test_source_server_start_is_blocked_in_packaged_cli_env(monkeypatch):
    from ouroboros import cli

    monkeypatch.setenv("OUROBOROS_PACKAGED_CLI", "1")
    monkeypatch.setattr(cli.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("direct server start"))

    with pytest.raises(cli.CLIError, match="packaged CLI must launch the desktop app"):
        cli._start_local_server("http://127.0.0.1:8765")


def test_cli_run_no_stream_requires_jsonl_without_touching_server(capsys):
    from ouroboros.cli import main

    assert main(["run", "--no-stream", "hello"]) == 2
    captured = capsys.readouterr()
    assert "--no-stream requires --jsonl" in captured.err


def test_queue_restore_accepts_headless_chat_zero(tmp_path, monkeypatch):
    import supervisor.queue as queue

    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "QUEUE_SEQ_COUNTER_REF", {"value": 0})
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "QUEUE_SNAPSHOT_PATH", tmp_path / "queue_snapshot.json")
    monkeypatch.setattr(queue, "append_jsonl", lambda *args, **kwargs: None)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    (tmp_path / "queue_snapshot.json").write_text(
        json.dumps({
            "ts": utc_now_iso(),
            "pending": [{"task": {"id": "headless1", "type": "task", "chat_id": 0, "text": "x"}}],
        }),
        encoding="utf-8",
    )

    assert queue.restore_pending_from_snapshot(max_age_sec=900) == 1
    assert queue.PENDING[0]["id"] == "headless1"

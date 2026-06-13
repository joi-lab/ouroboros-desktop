"""Tool API v2 filesystem observability.

The public tools keep their existing root policies, but user-facing output must
name the resolved logical root so agents do not confuse workspace, runtime data,
task-drive, and skill-payload paths.
"""
from __future__ import annotations

import subprocess

from ouroboros.tools.core import _code_search, _edit_text, _read_file, _write_file
from ouroboros.tools.registry import ToolContext


def _make_ctx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    drive = tmp_path / "data"
    drive.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello workspace\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return ToolContext(repo_dir=repo, drive_root=drive)


def test_read_file_headers_are_root_qualified(tmp_path):
    ctx = _make_ctx(tmp_path)
    (ctx.drive_root / "notes.txt").write_text("hello data\n", encoding="utf-8")

    workspace = _read_file(ctx, "README.md", root="active_workspace")
    runtime = _read_file(ctx, "notes.txt", root="runtime_data")

    assert workspace.startswith("# active_workspace:README.md")
    assert runtime.startswith("# runtime_data:notes.txt")


def test_write_file_outputs_use_normalized_root_paths(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    workspace = _write_file(ctx, "/tmp/tool.py", "print('x')", root="active_workspace")
    runtime = _write_file(ctx, "/tmp/tool.txt", "x", root="runtime_data")
    task_drive = _write_file(ctx, "./artifact.txt", "x", root="task_drive")
    user_file = _write_file(ctx, "Desktop/tool.txt", "x", root="user_files")

    assert "active_workspace:tmp/tool.py" in workspace
    assert "runtime_data:tmp/tool.txt" in runtime
    assert "task_drive:artifact.txt" in task_drive
    assert "user_files:Desktop/tool.txt" in user_file


def test_edit_text_and_search_outputs_are_root_qualified(tmp_path):
    ctx = _make_ctx(tmp_path)

    edit_result = _edit_text(ctx, "README.md", "hello", "hi", root="active_workspace")
    search_result = _code_search(ctx, "hi workspace", path=".", root="active_workspace")
    missing_result = _code_search(ctx, "missing", path="/not-there", root="active_workspace")

    assert "Replaced in active_workspace:README.md" in edit_result
    assert "active_workspace:README.md:1: hi workspace" in search_result
    assert "path not found: active_workspace:not-there" in missing_result

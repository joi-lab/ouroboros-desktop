import os
import subprocess
import time

import supervisor.git_ops as git_ops


def test_git_capture_repairs_corrupt_index(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_text("broken", encoding="utf-8")
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)

    calls = {"status": 0, "rebuild": 0}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd == ["git", "status", "--porcelain"]:
            calls["status"] += 1
            if calls["status"] == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    128,
                    stdout="",
                    stderr="fatal: .git/index: index file smaller than expected\n",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=" M changed.py\n", stderr="")
        if cmd == ["git", "reset", "--mixed", "HEAD"]:
            calls["rebuild"] += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    rc, stdout, stderr = git_ops.git_capture(["git", "status", "--porcelain"])

    assert rc == 0
    assert stdout == "M changed.py"
    assert stderr == ""
    assert calls["status"] == 2
    assert calls["rebuild"] == 1
    assert any(path.name.startswith("index.corrupt.") for path in git_dir.iterdir())


def test_checkout_and_reset_removes_stale_index_lock(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    lock_path = git_dir / "index.lock"
    lock_path.write_text("lock", encoding="utf-8")
    stale_ts = time.time() - 60
    os.utime(lock_path, (stale_ts, stale_ts))

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: False)
    monkeypatch.setattr(git_ops, "load_state", lambda: {})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    calls = {"checkout": 0}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "checkout"]:
            calls["checkout"] += 1
            if calls["checkout"] == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    128,
                    stdout="",
                    stderr=f"fatal: Unable to create '{git_dir / 'index.lock'}': File exists.\n",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset("ouroboros", unsynced_policy="ignore")

    assert ok
    assert message == "ok"
    assert calls["checkout"] == 2
    assert not lock_path.exists()
    assert saved_state["current_branch"] == "ouroboros"
    assert saved_state["current_sha"] == "abc123"


def test_checkout_and_reset_continues_when_fetch_fails(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: name in (None, "origin"))
    monkeypatch.setattr(git_ops, "load_state", lambda: {})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    events = []
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: events.append(payload))

    def fake_git_capture(cmd):
        if cmd == ["git", "fetch", "origin"]:
            return 1, "", "network down"
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "checkout"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="def456\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset("ouroboros", reason="restart", unsynced_policy="ignore")

    assert ok
    assert message == "ok"
    assert saved_state["current_branch"] == "ouroboros"
    assert saved_state["current_sha"] == "def456"
    assert events
    assert events[0]["type"] == "reset_fetch_failed"
    assert events[0]["continuing_local_reset"] is True


def test_checkout_and_reset_blocks_when_rescue_snapshot_fails(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: False)
    monkeypatch.setattr(git_ops, "load_state", lambda: {})
    monkeypatch.setattr(
        git_ops,
        "_collect_repo_sync_state",
        lambda: {
            "current_branch": "ouroboros",
            "dirty_lines": [" M BIBLE.md"],
            "unpushed_lines": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_create_rescue_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("snapshot failed")),
    )
    events = []
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: events.append(payload))

    reset_calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd[:2] == ["git", "reset"]:
            reset_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="restart",
        unsynced_policy="rescue_and_reset",
    )

    assert ok is False
    assert "rescue snapshot failed" in message
    assert reset_calls == []
    assert events and events[-1]["type"] == "reset_blocked_rescue_failed"
    assert events[-1]["incomplete_reason"] == "snapshot_error"


def test_checkout_and_reset_blocks_when_untracked_rescue_is_truncated(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: False)
    monkeypatch.setattr(git_ops, "load_state", lambda: {})
    monkeypatch.setattr(
        git_ops,
        "_collect_repo_sync_state",
        lambda: {
            "current_branch": "ouroboros",
            "dirty_lines": ["?? large.bin"],
            "unpushed_lines": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_create_rescue_snapshot",
        lambda **_kwargs: {
            "path": str(tmp_path / "data" / "archive" / "rescue" / "x"),
            "untracked": {"copied_files": 0, "skipped_files": 0, "truncated": True},
        },
    )
    events = []
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: events.append(payload))
    reset_calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd[:2] == ["git", "reset"]:
            reset_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="restart",
        unsynced_policy="rescue_and_reset",
    )

    assert ok is False
    assert "untracked-file rescue was incomplete" in message
    assert reset_calls == []
    assert events and events[-1]["type"] == "reset_blocked_rescue_incomplete"
    assert events[-1]["incomplete_reason"] == "untracked_rescue"
    assert events[-1]["incomplete_detail"] == "untracked rescue copy was truncated"


def test_checkout_and_reset_preserves_local_head_on_managed_restart(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: name in (None, "managed"))
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
            "managed_remote_stable_branch": "ouroboros-stable",
        },
    )
    monkeypatch.setattr(git_ops, "load_state", lambda: {})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    def fake_git_capture(cmd):
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="local-sha\n", stderr="")
        if cmd == ["git", "checkout", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "reset", "--hard", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="local-sha\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset("ouroboros", reason="restart", unsynced_policy="ignore")

    assert ok
    assert message == "ok"
    assert ["git", "fetch", "managed"] not in calls
    assert ["git", "checkout", "-B", "ouroboros", "managed/ouroboros"] not in calls
    assert ["git", "checkout", "ouroboros"] in calls
    assert saved_state["current_branch"] == "ouroboros"
    assert saved_state["current_sha"] == "local-sha"


def test_checkout_and_reset_cleans_untracked_after_managed_restart_rescue(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
        },
    )
    monkeypatch.setattr(git_ops, "load_state", lambda: {})
    monkeypatch.setattr(git_ops, "save_state", lambda _state: None)
    monkeypatch.setattr(
        git_ops,
        "_collect_repo_sync_state",
        lambda: {
            "current_branch": "ouroboros",
            "dirty_lines": ["?? scratch.py"],
            "unpushed_lines": [],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_create_rescue_snapshot",
        lambda **_kwargs: {
            "path": str(tmp_path / "data" / "archive" / "rescue" / "x"),
            "untracked": {"copied_files": 1, "skipped_files": 0, "truncated": False},
        },
    )
    monkeypatch.setattr(git_ops, "append_jsonl", lambda _path, _payload: None)
    monkeypatch.setattr(git_ops, "git_capture", lambda cmd: (_ for _ in ()).throw(AssertionError(cmd)))

    calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="local-sha\n", stderr="")
        if cmd == ["git", "checkout", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "reset", "--hard", "HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "clean", "-fd"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="local-sha\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="restart",
        unsynced_policy="rescue_and_reset",
    )

    assert ok
    assert message == "ok"
    assert ["git", "clean", "-fd"] in calls
    assert calls.index(["git", "clean", "-fd"]) < calls.index(["git", "checkout", "ouroboros"])


def test_checkout_and_reset_does_not_rescue_for_only_managed_ahead_commits(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {"managed_remote_name": "managed", "managed_remote_branch": "ouroboros"},
    )
    monkeypatch.setattr(git_ops, "load_state", lambda: {})
    monkeypatch.setattr(git_ops, "save_state", lambda _state: None)
    monkeypatch.setattr(
        git_ops,
        "_collect_repo_sync_state",
        lambda: {
            "current_branch": "ouroboros",
            "dirty_lines": [],
            "unpushed_lines": ["abc123 local self-modification"],
            "warnings": [],
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_create_rescue_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("ahead-only restart should not rescue")),
    )
    monkeypatch.setattr(git_ops, "git_capture", lambda cmd: (_ for _ in ()).throw(AssertionError(cmd)))

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd == ["git", "rev-parse", "--verify", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="local-sha\n", stderr="")
        if cmd[:2] in (["git", "reset"], ["git", "clean"], ["git", "checkout"]):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="local-sha\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="restart",
        unsynced_policy="rescue_and_reset",
    )

    assert ok
    assert message == "ok"


def test_checkout_and_reset_applies_explicit_update_intent(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
            "managed_remote_stable_branch": "ouroboros-stable",
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_read_update_intent",
        lambda: {"branch": "ouroboros", "target_sha": "remote-sha"},
    )
    monkeypatch.setattr(git_ops, "load_state", lambda: {})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-parse", "--verify", "remote-sha"]:
            return 0, "remote-sha", ""
        if cmd == ["git", "rev-list", "--left-right", "--count", "ouroboros...remote-sha"]:
            return 0, "0 1", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "remote-sha"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="remote-sha\n", stderr="")
        if cmd[:4] == ["git", "checkout", "-B", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "clean"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="remote-sha\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="ui_update_apply",
        unsynced_policy="ignore",
    )

    assert ok
    assert message == "ok"
    assert ["git", "checkout", "-B", "ouroboros", "remote-sha"] in calls
    assert saved_state["current_branch"] == "ouroboros"
    assert saved_state["current_sha"] == "remote-sha"


def test_checkout_and_reset_preserves_ahead_head_before_update_intent(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_read_update_intent",
        lambda: {"branch": "ouroboros", "target_sha": "remote-sha"},
    )
    monkeypatch.setattr(git_ops, "load_state", lambda: {})
    monkeypatch.setattr(git_ops, "save_state", lambda _state: None)
    monkeypatch.setattr(git_ops, "append_jsonl", lambda _path, _payload: None)

    capture_calls = []

    def fake_git_capture(cmd):
        capture_calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "remote-sha"]:
            return 0, "remote-sha", ""
        if cmd == ["git", "rev-list", "--left-right", "--count", "ouroboros...remote-sha"]:
            return 0, "2 1", ""
        if cmd[:2] == ["git", "branch"] and cmd[-1] == "ouroboros":
            return 0, "", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd == ["git", "rev-parse", "--verify", "remote-sha"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="remote-sha\n", stderr="")
        if cmd[:2] in (["git", "reset"], ["git", "clean"], ["git", "checkout"]):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="remote-sha\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="ui_update_apply",
        unsynced_policy="ignore",
    )

    assert ok
    assert message == "ok"
    assert any(cmd[:2] == ["git", "branch"] and cmd[-1] == "ouroboros" for cmd in capture_calls)


def test_checkout_and_reset_blocks_when_update_ahead_check_fails(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {"managed_remote_name": "managed", "managed_remote_branch": "ouroboros"},
    )
    monkeypatch.setattr(
        git_ops,
        "_read_update_intent",
        lambda: {"branch": "ouroboros", "target_sha": "remote-sha"},
    )

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-parse", "--verify", "remote-sha"]:
            return 0, "remote-sha", ""
        if cmd == ["git", "rev-list", "--left-right", "--count", "ouroboros...remote-sha"]:
            return 128, "", "bad revision"
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    checkout_calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        checkout_calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "remote-sha"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="remote-sha\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset(
        "ouroboros",
        reason="ui_update_apply",
        unsynced_policy="ignore",
    )

    assert ok is False
    assert "Could not preserve local branch before official update" in message
    assert ["git", "checkout", "-B", "ouroboros", "remote-sha"] not in checkout_calls


def test_compute_managed_update_status_passive_does_not_ensure_remote(monkeypatch):
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
        },
    )
    monkeypatch.setattr(
        git_ops,
        "ensure_official_update_remote",
        lambda: (_ for _ in ()).throw(AssertionError("passive status mutated remotes")),
    )

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, "ouroboros", ""
        if cmd == ["git", "rev-parse", "HEAD"]:
            return 0, "abc123", ""
        if cmd == ["git", "status", "--porcelain"]:
            return 0, "", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    status = git_ops.compute_managed_update_status(fetch=False)

    assert status["managed"] is True
    assert "official_status_requires_check" in status["warnings"]


def test_prepare_managed_update_preserves_dev_branch_not_current_head(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(
        git_ops,
        "compute_managed_update_status",
        lambda fetch=False: {
            "managed": True,
            "available": True,
            "latest_sha": "remote-sha",
            "target_ref": "managed/ouroboros",
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_collect_repo_sync_state",
        lambda: {"current_branch": "ouroboros-stable", "dirty_lines": [], "unpushed_lines": [], "warnings": []},
    )
    monkeypatch.setattr(
        git_ops,
        "_create_rescue_snapshot",
        lambda **_kwargs: {
            "path": str(tmp_path / "data" / "archive" / "rescue" / "x"),
            "untracked": {"copied_files": 0, "skipped_files": 0, "truncated": False},
        },
    )
    monkeypatch.setattr(git_ops, "_write_update_intent", lambda _payload: None)
    monkeypatch.setattr(git_ops, "append_jsonl", lambda _path, _payload: None)

    capture_calls = []

    def fake_git_capture(cmd):
        capture_calls.append(cmd)
        if cmd == ["git", "rev-list", "--left-right", "--count", "ouroboros...remote-sha"]:
            return 0, "1 0", ""
        if cmd[:2] == ["git", "branch"] and cmd[-1] == "ouroboros":
            return 0, "", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    ok, payload = git_ops.prepare_managed_update("replace")

    assert ok is True
    assert payload["keep_branch"].startswith("local-keep-")
    assert any(cmd[:2] == ["git", "branch"] and cmd[-1] == "ouroboros" for cmd in capture_calls)


def test_prepare_managed_update_blocks_when_ahead_check_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(git_ops, "DRIVE_ROOT", tmp_path / "data")
    monkeypatch.setattr(
        git_ops,
        "compute_managed_update_status",
        lambda fetch=False: {
            "managed": True,
            "available": True,
            "latest_sha": "remote-sha",
            "target_ref": "managed/ouroboros",
        },
    )
    monkeypatch.setattr(
        git_ops,
        "_collect_repo_sync_state",
        lambda: {"current_branch": "ouroboros", "dirty_lines": [], "unpushed_lines": [], "warnings": []},
    )
    monkeypatch.setattr(
        git_ops,
        "_create_rescue_snapshot",
        lambda **_kwargs: {
            "path": str(tmp_path / "data" / "archive" / "rescue" / "x"),
            "untracked": {"copied_files": 0, "skipped_files": 0, "truncated": False},
        },
    )

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-list", "--left-right", "--count", "ouroboros...remote-sha"]:
            return 128, "", "bad revision"
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    ok, payload = git_ops.prepare_managed_update("replace")

    assert ok is False
    assert "Could not compare local branch with managed update target" in payload["error"]


def test_safe_restart_fallback_does_not_rewrite_dev_branch(monkeypatch):
    checkout_calls = []

    def fake_checkout(branch, reason="unspecified", unsynced_policy="ignore"):
        checkout_calls.append((branch, reason, unsynced_policy))
        return True, "ok"

    import_results = [
        {"ok": False, "stdout": "", "stderr": "broken dev", "returncode": 1},
        {"ok": True, "stdout": "import_ok", "stderr": "", "returncode": 0},
    ]

    monkeypatch.setattr(git_ops, "checkout_and_reset", fake_checkout)
    monkeypatch.setattr(git_ops, "sync_runtime_dependencies", lambda reason: (True, reason))
    monkeypatch.setattr(git_ops, "import_test", lambda: import_results.pop(0))
    monkeypatch.setattr(git_ops, "append_jsonl", lambda _path, _payload: None)

    ok, message = git_ops.safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")

    assert ok is True
    assert message == "OK: fell back to ouroboros-stable"
    assert checkout_calls == [
        ("ouroboros", "owner_restart", "rescue_and_reset"),
        ("ouroboros-stable", "owner_restart_fallback_stable", "rescue_and_reset"),
    ]


def test_configure_remote_adds_origin_even_when_managed_remote_exists(monkeypatch):
    calls = []

    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: name in (None, "managed"))
    monkeypatch.setattr(
        git_ops,
        "git_capture",
        lambda cmd: calls.append(cmd) or (0, "", ""),
    )
    monkeypatch.setattr(
        git_ops,
        "_configure_credential_helper",
        lambda repo_slug, token: calls.append(("helper", repo_slug, token)),
    )

    ok, message = git_ops.configure_remote("joi-lab/ouroboros-desktop", "ghp_test")

    assert ok
    assert message == "ok"
    assert ["git", "remote", "add", "origin", "https://github.com/joi-lab/ouroboros-desktop.git"] in calls


def test_collect_repo_sync_state_prefers_managed_remote(monkeypatch):
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
        },
    )
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: name in (None, "managed"))

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-parse", "--abbrev-ref", "HEAD"]:
            return 0, "ouroboros", ""
        if cmd == ["git", "status", "--porcelain"]:
            return 0, "", ""
        if cmd == ["git", "log", "--oneline", "managed/ouroboros..HEAD"]:
            return 0, "abc123 local commit\n", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    state = git_ops._collect_repo_sync_state()

    assert state["current_branch"] == "ouroboros"
    assert state["unpushed_lines"] == ["abc123 local commit"]


def test_checkout_and_reset_keeps_bundled_sha_on_first_managed_bootstrap(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / git_ops.BOOTSTRAP_PIN_MARKER_NAME).write_text("pending\n", encoding="utf-8")

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_has_remote", lambda name=None: name in (None, "managed"))
    monkeypatch.setattr(
        git_ops,
        "_read_managed_repo_meta",
        lambda: {
            "managed_remote_name": "managed",
            "managed_remote_branch": "ouroboros",
            "source_sha": "bundle123",
        },
    )
    monkeypatch.setattr(git_ops, "load_state", lambda: {"current_sha": "bundle123"})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    def fake_git_capture(cmd):
        if cmd == ["git", "rev-parse", "HEAD"]:
            return 0, "bundle123", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    calls = []

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        calls.append(cmd)
        if cmd == ["git", "rev-parse", "--verify", "ouroboros"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="bundle123\n", stderr="")
        if cmd[:2] == ["git", "checkout"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="bundle123\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset("ouroboros", reason="bootstrap", unsynced_policy="ignore")

    assert ok
    assert message == "ok"
    assert ["git", "fetch", "managed"] not in calls
    assert saved_state["current_sha"] == "bundle123"
    assert not (git_dir / git_ops.BOOTSTRAP_PIN_MARKER_NAME).exists()


def test_ensure_local_version_tag_accepts_rc_versions(monkeypatch, tmp_path):
    (tmp_path / "VERSION").write_text("4.50.0-rc.2\n", encoding="utf-8")
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_ensure_git_identity", lambda: None)

    calls = []

    def fake_git_capture(cmd):
        calls.append(cmd)
        if cmd == ["git", "tag", "-l", "v4.50.0-rc.2"]:
            return 0, "", ""
        if cmd == ["git", "tag", "-l"]:
            return 0, "", ""
        if cmd == ["git", "rev-parse", "HEAD"]:
            return 0, "abc123", ""
        if cmd == ["git", "tag", "-a", "v4.50.0-rc.2", "-m", "Release v4.50.0-rc.2"]:
            return 0, "", ""
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    git_ops._ensure_local_version_tag()

    assert ["git", "tag", "-a", "v4.50.0-rc.2", "-m", "Release v4.50.0-rc.2"] in calls

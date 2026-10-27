"""Finished records are compressed in place: the one floor (``artifact_storage.compression_outcome``) and the
maintenance child's paced ``record_compression`` job over terminal evidence and finished reviewer seat homes.

Real APFS proofs run on macOS and skip elsewhere; the job's choices (which files, when, where it resumes)
are proven on every platform through a recording outcome.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ouroboros import artifact_storage, maintenance, maintenance_jobs, record_compression as job

darwin = pytest.mark.skipif(sys.platform != "darwin", reason="real APFS transparent compression")
TEXT = b"The finished record keeps every byte; only its allocation changes.\n"


def _write(path: pathlib.Path, raw: bytes = TEXT * 4096) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _identity(path: pathlib.Path):
    raw = path.read_bytes()
    return len(raw), hashlib.sha256(raw).hexdigest()


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    return root


@pytest.fixture
def fake_darwin(monkeypatch):
    """The macOS path on any platform, with a ditto that must not run unless a test allows it."""
    calls = []

    def ditto(command, **_kwargs):
        calls.append(command)
        raise AssertionError("the floor should have kept this file before any rewrite")

    monkeypatch.setattr(artifact_storage, "sys", SimpleNamespace(platform="darwin"))
    monkeypatch.setattr(artifact_storage, "subprocess", SimpleNamespace(
        run=ditto, DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError))
    monkeypatch.setattr(artifact_storage, "_may_share", lambda path: False)
    return calls


# ---- the floor -------------------------------------------------------------------------------

@darwin
def test_real_apfs_keeps_size_bytes_and_sha_while_allocation_drops(tmp_path):
    path = _write(tmp_path / "record.log", TEXT * 16384)
    os.chmod(path, 0o640)
    before, identity = os.lstat(path), _identity(path)
    assert artifact_storage.compression_outcome(path) == "compressed"
    after = os.lstat(path)
    assert _identity(path) == identity and after.st_size == before.st_size
    assert after.st_flags & stat.UF_COMPRESSED and after.st_blocks < before.st_blocks // 2
    assert (after.st_mtime_ns, stat.S_IMODE(after.st_mode)) == (before.st_mtime_ns, 0o640)
    assert artifact_storage.compression_outcome(path) == "compressed_already", "a second pass rewrites nothing"
    assert sorted(item.name for item in tmp_path.iterdir()) == ["record.log"]


@darwin
def test_real_apfs_clone_is_kept_because_it_may_share_blocks(tmp_path):
    source = _write(tmp_path / "source.log")
    assert artifact_storage.clone_file(source, tmp_path / "clone.log")
    assert os.lstat(source).st_nlink == 1, "a clone is invisible to nlink"
    assert artifact_storage.compression_outcome(source) == "shared"
    assert artifact_storage.compression_outcome(_write(tmp_path / "private.log")) == "compressed"


def test_each_floor_guard_keeps_its_file_before_any_rewrite(tmp_path, fake_darwin, monkeypatch):
    small = _write(tmp_path / "small.log", b"x" * (artifact_storage.FLOOR_BYTES - 1))
    linked = _write(tmp_path / "linked.log")
    os.link(linked, tmp_path / "second-name.log")
    noise = _write(tmp_path / "noise.bin", os.urandom(4 * artifact_storage.FLOOR_BYTES))
    folder = tmp_path / "folder"
    folder.mkdir()
    assert artifact_storage.compression_outcome(small) == "small"
    assert artifact_storage.compression_outcome(linked) == "linked"
    assert artifact_storage.compression_outcome(noise) == "incompressible"
    assert artifact_storage.compression_outcome(folder) == "not_regular"
    plain = _write(tmp_path / "plain.log")
    monkeypatch.setattr(artifact_storage, "_may_share", lambda path: True)
    assert artifact_storage.compression_outcome(plain) == "shared"
    monkeypatch.setattr(artifact_storage, "_may_share", lambda path: None)
    assert artifact_storage.compression_outcome(plain) == "share_unknown", "unknown sharing is kept"
    monkeypatch.setattr(artifact_storage, "_may_share", lambda path: False)
    monkeypatch.setattr(artifact_storage.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024))
    assert artifact_storage.compression_outcome(plain) == "low_space"
    assert fake_darwin == [], "no guard reached ditto"
    assert {item.name for item in tmp_path.iterdir()} == {
        "small.log", "linked.log", "second-name.log", "noise.bin", "folder", "plain.log"}


def test_ntfs_keeps_a_read_only_file_and_linux_is_unsupported(tmp_path, monkeypatch):
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o444, st_size=1 << 20, st_nlink=1, st_flags=0,
                           st_file_attributes=stat.FILE_ATTRIBUTE_READONLY)
    monkeypatch.setattr(artifact_storage, "sys", SimpleNamespace(platform="win32"))
    assert artifact_storage._floor(_write(tmp_path / "evidence.log"), info) == "read_only"
    monkeypatch.setattr(artifact_storage, "sys", SimpleNamespace(platform="linux"))
    assert artifact_storage.compression_supported() is False
    assert artifact_storage.compression_outcome(tmp_path / "evidence.log") == "unsupported"


@darwin
def test_an_append_during_compression_keeps_the_writers_bytes(tmp_path, monkeypatch):
    path = _write(tmp_path / "growing.log")
    real = subprocess.run

    def ditto_then_append(command, **kwargs):
        result = real(command, **kwargs)
        with open(path, "ab") as writer:
            writer.write(b"appended while the sibling was written\n")
        return result

    monkeypatch.setattr(artifact_storage, "subprocess", SimpleNamespace(
        run=ditto_then_append, DEVNULL=subprocess.DEVNULL, SubprocessError=subprocess.SubprocessError))
    inode = os.lstat(path).st_ino
    assert artifact_storage.compression_outcome(path) == "changed"
    assert path.read_bytes().endswith(b"appended while the sibling was written\n") and os.lstat(path).st_ino == inode
    assert sorted(item.name for item in tmp_path.iterdir()) == ["growing.log"], "the sibling is gone"


@darwin
def test_ownership_is_asked_last_and_a_lost_owner_keeps_the_original(tmp_path):
    path = _write(tmp_path / "evidence.log")
    inode, asked = os.lstat(path).st_ino, []
    assert artifact_storage.compression_outcome(path, owned=lambda: asked.append(1) or False) == "not_owned"
    assert asked == [1] and os.lstat(path).st_ino == inode and not os.lstat(path).st_flags & stat.UF_COMPRESSED
    assert sorted(item.name for item in tmp_path.iterdir()) == ["evidence.log"]


# ---- the job --------------------------------------------------------------------------------

def _evidence(data, task_id, *, status="completed", live_files=True):
    from ouroboros.task_results import write_task_result

    write_task_result(data, task_id, status)
    base = data / "task_results" / "artifacts" / task_id
    files = {"evidence": _write(base / "delegated_runs" / "run-1" / "evidence" / "files" / "out.log"),
             "generation": _write(base / "delegated_runs" / "run-1" / "evidence-abc" / "summary.json"),
             "test": _write(base / "source_handles" / "test-evidence" / "run-1" / "stdout.bin")}
    if live_files:
        files.update(other=_write(base / "workspace.patch"),
                     lock=_write(base / "delegated_runs" / "run-1" / "disposition.lock"),
                     pending=_write(base / "delegated_runs" / "run-1" / ".evidence-x.pending" / "f.log"),
                     git=_write(base / "delegated_runs" / "run-1" / "evidence" / ".git" / "objects" / "pack"),
                     checkpoint=_write(base / "source_handles" / "context_checkpoints" / "c.json"))
    return files


def _seat(data, task_id, operation, *, area=False):
    from ouroboros import review_checking

    home = review_checking.seat_dir(data, task_id, operation)
    (home / "checking.json").parent.mkdir(parents=True, exist_ok=True)
    (home / "checking.json").write_text(json.dumps({"schema": 1, "task_id": task_id, "operation_id": operation}),
                                        encoding="utf-8")
    files = {"evidence": _write(home / "evidence" / "files" / "venv.bin"),
             "test": _write(home / ".test-evidence" / "run" / "out.log")}
    if area:
        files["area"] = _write(home / "area.noindex" / "big.log")
    return home, files


@pytest.fixture
def recorded(monkeypatch):
    """Every path offered to the floor, answered ``compressed`` without touching it."""
    seen = []
    monkeypatch.setattr(artifact_storage, "compression_supported", lambda: True)
    monkeypatch.setattr(artifact_storage, "compression_outcome",
                        lambda path, owned=None: seen.append((pathlib.Path(path), owned)) or "compressed")
    monkeypatch.setattr(job, "_queue_live", lambda root: (lambda task_id: task_id == "seat-live"))
    return seen


def _run(data):
    return maintenance.run_job(data, job.JOB, should_stop=lambda: False)


def test_the_job_offers_only_finished_records_never_a_copy_or_git(data, recorded):
    done = _evidence(data, "done")
    live = _evidence(data, "live", status="running", live_files=False)
    offered_home, offered = _seat(data, "done", "op-1")
    _copy_home, with_copy = _seat(data, "done", "op-2", area=True)
    _live_home, of_live = _seat(data, "seat-live", "op-1")
    assert _run(data)["state"] == "done"
    paths = {path for path, _owned in recorded}
    assert paths == {done["evidence"], done["generation"], done["test"], offered["evidence"], offered["test"]}
    assert not paths & {*live.values(), *with_copy.values(), *of_live.values()}
    owned = {path: check for path, check in recorded}
    assert owned[done["evidence"]] is not None and owned[done["evidence"]]() is True, "the row is asked again"
    assert owned[offered["evidence"]] is None, "a home is held by its seat lock instead"
    row = maintenance_jobs.job(data, job.JOB)
    assert row["waiting"] == sorted(home.name for home in (_copy_home, _live_home))
    assert _run(data) is None, "a second look at unchanged inputs is at rest"


def test_new_terminal_writes_make_the_job_due_and_only_their_records_are_read(data, recorded):
    from ouroboros.task_results import write_task_result

    _evidence(data, "first")
    late = _evidence(data, "late", status="running", live_files=False)
    _run(data)
    assert not job.due(data, maintenance_jobs.job(data, job.JOB), {}, False)
    recorded.clear()
    write_task_result(data, "late", "completed")
    assert job.due(data, maintenance_jobs.job(data, job.JOB), {}, False), "a terminal write arrived"
    _run(data)
    assert {path for path, _owned in recorded} == {late["evidence"], late["generation"], late["test"]}
    assert not job.due(data, maintenance_jobs.job(data, job.JOB), {}, False)


def test_the_budget_and_a_stop_leave_a_cursor_and_every_file_is_offered_once(data, recorded, monkeypatch):
    files = [*_evidence(data, "a", live_files=False).values(), *_evidence(data, "b", live_files=False).values()]
    monkeypatch.setattr(job, "FILES_PER_RUN", 2)
    outcomes = []
    while (outcome := _run(data)) is not None:
        outcomes.append(outcome["state"])
        row = maintenance_jobs.job(data, job.JOB)
        assert (row["cursor"] is None) == (outcome["state"] == "done")
    assert outcomes == ["pending", "pending", "done"]
    assert sorted(path for path, _owned in recorded) == sorted(files), "each file once, none skipped"
    recorded.clear()
    from ouroboros.task_results import write_task_result

    write_task_result(data, "c", "completed")  # a new pass; a stop after the first file keeps its place
    extra = _evidence(data, "c", live_files=False)
    calls = iter([False, True])
    assert maintenance.run_job(data, job.JOB, should_stop=lambda: next(calls, True))["state"] == "pending"
    assert maintenance_jobs.job(data, job.JOB)["owed"]["evidence"] == ["c"]
    monkeypatch.setattr(job, "FILES_PER_RUN", 100)
    assert _run(data)["state"] == "done"
    assert sorted(path for path, _owned in recorded) == sorted(extra.values())


def test_a_killed_runs_sibling_is_reaped_and_never_offered(data, recorded):
    files = _evidence(data, "done", live_files=False)
    home, _ = _seat(data, "done", "op-1")
    siblings = [_write(files["evidence"].with_name(".out.log.tmp.4242.17.0a1b2c3d")),
                _write(home / "evidence" / "files" / ".venv.bin.tmp.4242.17.0a1b2c3d")]
    keep = _write(files["evidence"].with_name(".notes.tmp.backup"))  # not the signature: an ordinary record
    outcome = _run(data)
    assert outcome["state"] == "done" and not any(path.exists() for path in siblings)
    assert not {path for path, _owned in recorded} & set(siblings)
    assert keep.exists() and keep in {path for path, _owned in recorded}
    assert maintenance_jobs.job(data, job.JOB)["last"]["reaped"] == 2


def test_a_held_seat_lock_keeps_the_home_waiting_until_it_is_free(data, recorded):
    from ouroboros.review_checking import _seat_locked
    from ouroboros.task_results import write_task_result

    home, files = _seat(data, "done", "op-1")
    write_task_result(data, "done", "completed")
    with _seat_locked(home) as held:
        assert held
        _run(data)
    assert not recorded and maintenance_jobs.job(data, job.JOB)["waiting"] == [home.name]
    write_task_result(data, "other", "completed")  # the next pass: the waiting home is looked at again
    _run(data)
    assert {path for path, _owned in recorded} == {files["evidence"], files["test"]}
    assert maintenance_jobs.job(data, job.JOB)["waiting"] == []


def test_an_unreadable_listing_fails_the_run_and_marks_nothing_covered(data, recorded, monkeypatch):
    _evidence(data, "done", live_files=False)
    real, refused = os.scandir, data / "task_results" / "artifacts"

    def scandir(path="."):
        if pathlib.Path(path) == refused:
            raise PermissionError(13, "listing refused")
        return real(path)

    monkeypatch.setattr(maintenance, "JOBS", {job.JOB: maintenance.JOBS[job.JOB]})
    monkeypatch.setattr(os, "scandir", scandir)
    assert maintenance.run(data)["status"] == "failed"
    row = maintenance_jobs.job(data, job.JOB)
    assert row.get("counter") is None and row.get("state") != "done", "the backlog is still owed"
    assert row["last_error"]["class"] == "PermissionError" and not recorded
    monkeypatch.setattr(os, "scandir", real)
    assert _run(data)["state"] == "done" and len(recorded) == 3


def test_unsupported_platforms_record_it_once_and_walk_nothing(data, monkeypatch):
    _evidence(data, "done", live_files=False)
    monkeypatch.setattr(artifact_storage, "compression_supported", lambda: False)
    monkeypatch.setattr(artifact_storage, "compression_outcome", lambda *a, **k: pytest.fail("walked"))
    assert job.JOB in maintenance.due(data)
    assert _run(data) == {"state": "done", "items": 0}
    assert maintenance_jobs.job(data, job.JOB)["unsupported"] == sys.platform
    assert job.JOB not in maintenance.due(data) and _run(data) is None


@darwin
def test_real_compression_of_terminal_evidence_and_a_finished_seat_home(data, monkeypatch):
    monkeypatch.setattr(job, "_queue_live", lambda root: (lambda task_id: False))
    done = _evidence(data, "done")
    home, seat = _seat(data, "done", "op-1")
    linked = _write(seat["evidence"].with_name("linked.bin"))
    os.link(linked, home / "outside-name.bin")
    identities = {path: _identity(path) for path in (*done.values(), *seat.values(), linked)}
    outcome = _run(data)
    assert outcome["state"] == "done" and outcome["items"] == 5
    for path, identity in identities.items():
        assert _identity(path) == identity, path
    compressed = {path for path in identities if os.lstat(path).st_flags & stat.UF_COMPRESSED}
    assert compressed == {done["evidence"], done["generation"], done["test"], seat["evidence"], seat["test"]}
    row = maintenance_jobs.job(data, job.JOB)
    assert row["compressed"] == 5 and row["saved_bytes"] > 0 and row["last"]["skipped"] == {}
    assert _run(data) is None

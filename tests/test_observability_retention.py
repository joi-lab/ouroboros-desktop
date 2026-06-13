import json
import os

from ouroboros.observability import persist_call, prune_observability_blobs, write_blob


def test_observability_retention_preserves_old_manifests_and_orphan_blobs(tmp_path):
    old = persist_call(
        tmp_path,
        task_id="old-task",
        call_id="old-call",
        call_type="llm_call",
        payload={"message": "old"},
    )
    keep = persist_call(
        tmp_path,
        task_id="new-task",
        call_id="new-call",
        call_type="llm_call",
        payload={"message": "new"},
    )
    orphan = write_blob(tmp_path, {"message": "orphan"})

    old_manifest = tmp_path / "observability" / "calls" / "old-task" / "old-call.json"
    new_manifest = tmp_path / "observability" / "calls" / "new-task" / "new-call.json"
    old_time = 1_000_000.0
    new_time = old_time + 10 * 86400
    os.utime(old_manifest, (old_time, old_time))
    os.utime(new_manifest, (new_time, new_time))
    os.utime(orphan["path"], (old_time, old_time))

    report = prune_observability_blobs(tmp_path, retention_days=7, now=new_time)

    assert report["preserved_indefinitely"] is True
    assert report["deleted_manifests"] == 0
    assert report["deleted_blobs"] == 0
    assert old_manifest.exists()
    assert new_manifest.exists()
    assert os.path.exists(orphan["path"])
    assert report["manifest_count"] == 2
    assert report["blob_count"] >= 3
    manifest = json.loads(new_manifest.read_text(encoding="utf-8"))
    assert os.path.exists(manifest["full_payload_ref"]["path"])
    assert os.path.exists(manifest["redacted_projection_ref"]["path"])
    assert keep["manifest_ref"]["path"] == str(new_manifest)


def test_observability_retention_preserves_fresh_orphan_blobs(tmp_path):
    orphan = write_blob(tmp_path, {"message": "fresh service log blob"})
    now = 1_000_000.0
    os.utime(orphan["path"], (now, now))

    report = prune_observability_blobs(tmp_path, retention_days=7, now=now)

    assert report["deleted_blobs"] == 0
    assert os.path.exists(orphan["path"])


def test_observability_retention_disabled_without_env(tmp_path, monkeypatch):
    orphan = write_blob(tmp_path, {"message": "kept"})
    monkeypatch.delenv("OUROBOROS_OBSERVABILITY_RETENTION_DAYS", raising=False)

    report = prune_observability_blobs(tmp_path)

    assert report["enabled"] is False
    assert report["preserved_indefinitely"] is True
    assert os.path.exists(orphan["path"])

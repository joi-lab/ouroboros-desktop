"""Chronicle reads: lock-free while the index is current, the publisher's locked catch-up otherwise."""
import json

from ouroboros import chronicle_store
from ouroboros.chronicle_store import ChronicleStore

MIND = {"kind": "mind", "task_id": "t1", "focus": {"role": "root", "task_id": "t1"}}


def _note(store, text, room="r"):
    result = store.write_note(room_id=room, task_id="t1", text=text, author=MIND)
    assert result.ok, result
    return result.record


def _counted_locks(monkeypatch):
    taken, real = [], chronicle_store.acquire_exclusive_file_lock
    monkeypatch.setattr(chronicle_store, "acquire_exclusive_file_lock",
                        lambda *args, **kwargs: taken.append(1) or real(*args, **kwargs))
    return taken


def _reads(store, record_id):
    """Every reader, once: what a capture asks the chronicle."""
    return (store.get(record_id)["text"], [r["id"] for r in store.room_records("r")], [r["id"] for r in store.records()],
            store.scan_state(), store.active_marks("r"), store.room_head("r"), store.sealed_row_refs("r"),
            store.pages_of_room("r"), store.observation_snapshot(None)[1]["record_id"])


def test_reads_take_no_lock_and_write_nothing_while_the_index_is_current(tmp_path, monkeypatch):
    store = ChronicleStore(tmp_path)
    kept = _note(store, "kept")
    index = store.index_path.stat()
    taken = _counted_locks(monkeypatch)
    assert _reads(store, kept["id"]) == ("kept", [kept["id"]], [kept["id"]], {}, [], kept["sequence"], set(), [],
                                         kept["id"])
    assert taken == [] and not store.lock_path.exists()
    assert store.index_path.stat().st_mtime_ns == index.st_mtime_ns
    _note(store, "published")  # a publication keeps its lock
    assert taken == [1]


def test_a_read_after_an_unprojected_append_catches_up_under_the_lock(tmp_path, monkeypatch):
    store = ChronicleStore(tmp_path)
    _note(store, "first")
    tx = {"kind": "transaction", "schema": 1, "scan_state": None,
          "records": [{"id": "late", "kind": "note", "room_id": "r", "task_id": "t1", "text": "appended",
                       "author": MIND, "ts": "2026-10-25T00:00:00Z"}]}
    with store.log_path.open("a", encoding="utf-8") as stream:  # a publisher stopped between append and projection
        stream.write(json.dumps(tx) + "\n")
    taken = _counted_locks(monkeypatch)
    assert store.get("late")["text"] == "appended" and taken == [1]
    assert store.get("late")["text"] == "appended" and taken == [1]  # caught up: the next read is lock-free again


def test_without_enforced_kernel_locks_every_read_keeps_the_lock(tmp_path, monkeypatch):
    store = ChronicleStore(tmp_path)
    kept = _note(store, "kept")
    monkeypatch.setitem(chronicle_store._KERNEL_LOCKS, str(store.directory), False)  # the name tier
    taken = _counted_locks(monkeypatch)
    assert store.get(kept["id"])["text"] == "kept" and store.room_records("r") and taken == [1, 1]


def test_a_publication_between_two_reads_is_visible_to_the_second(tmp_path):
    store = ChronicleStore(tmp_path)
    first = _note(store, "first")
    assert [r["id"] for r in store.room_records("r")] == [first["id"]]
    second = _note(ChronicleStore(tmp_path), "second")  # another writer instance, same authority
    assert [r["id"] for r in store.room_records("r")] == [first["id"], second["id"]]

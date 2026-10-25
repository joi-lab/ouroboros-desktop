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


# --- one capture: one state of the index for every reader --------------------------------------------

LIGHT = {"kind": "helper", "route": "configured-light"}
LEGACY = {"kind": "legacy_helper", "writer": "old_consolidator"}


def _page(store, *refs, at, author=MIND, **kwargs):
    result = store.publish_page(room_id="r", text=f"page over {refs}", author=author,
                                covers={"rows": list(refs), "stream_span": [at, at + len(refs) - 1]}, **kwargs)
    assert result.ok, result
    return result.record["id"]


def _part(store, *members):
    result = store.publish_part(room_id="r", text=f"part over {members}", member_ids=list(members), author=MIND,
                                expected_sequence=store.room_head("r"))
    assert result.ok, result
    return result.record["id"]


def _every_kind(store):
    """A chronicle with every record kind and every derived fact the readers interpret."""
    assert store.ensure_activated()["kind"] == "activation"
    legacy = store.publish([{"id": "legacy-b01-rr", "kind": "legacy", "room_id": "r", "text": "retelling",
                             "author": LEGACY, "metadata": {"legacy_type": "era", "legacy_block": 1}},
                            {"id": "gap-s", "kind": "gap", "room_id": "s", "author": {"kind": "host"},
                             "metadata": {"source_gap": "test"}}]).record["id"]
    pages = [_page(store, f"a{n}", at=n) for n in range(4)]
    nested = _part(store, _part(store, *pages[:2]), _part(store, *pages[2:]))
    for at, accepted in ((10, True), (11, False)):
        assert store.decide(_page(store, f"b{at}", at=at, author=LIGHT), accepted, MIND, "judged").ok
    _page(store, "c1", at=20, author=LIGHT, metadata={"candidate": True})  # pending
    assert store.decide(_page(store, "c2", at=21, author=LIGHT, metadata={"candidate": True}), True, MIND, "adopt").ok
    kept = _note(store, "sealed note")
    sealing = _page(store, "note:" + kept["id"], "a9", at=30)
    assert store.correct(sealing, "said better", MIND).ok
    account = store.publish_account(room_id="r", text="across", sources=[sealing, {"id": legacy}], author=MIND)
    assert store.select_account(account.record["id"], replaces=[sealing], author=MIND, reason="told").ok
    seen = store.mark({"kind": "chronicle", "id": sealing}, "keep", MIND, room_id="r").record["id"]
    store.mark({"kind": "task", "task_id": "t1"}, "for all", MIND, room_id="s", scope="global")
    gone = store.mark({"kind": "task", "task_id": "t1"}, "later", MIND, room_id="r").record["id"]
    assert store.set_mark_view(seen, "meaning", MIND, "enough").ok and store.release_mark(gone, MIND, "done").ok
    with store.log_path.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "transaction", "records": [\n')  # a torn transaction: an addressed journal gap
    assert store.publish([], scan_state={"probe": {"k": 1}}).ok
    store.mark({"kind": "task", "task_id": "t1"}, "the room's newest record", MIND, room_id="r")  # not a head kind
    return nested


def _readers(store, nested):
    ids = [record["id"] for record in store.records()] + ["missing"]
    return ([store.get(record_id) for record_id in ids], store.records(), store.records("r"),
            store.records(kinds=("page", "part")), store.records("r", kinds=("note", "mark"), after_seq=3),
            store.room_records("r"), store.room_records("r", after_seq=8), store.room_records("s"),
            store.pages_of_room("r"), store.pending_candidates(), store.pending_candidates("r"), store.accounts(),
            store.folded_members(nested), store.sealed_row_refs("r"), store.sealed_row_refs("x"),
            store.legacy_pointer_rows(), store.active_marks(None), store.active_marks("r"),
            store.active_marks("r", include_global=False), store.active_marks("x"), store.scan_state(),
            store.activation(), store.room_head("r"), store.room_head("s"), store.room_head("x"), store.journal_size())


def test_every_reader_inside_a_capture_answers_as_the_index_and_enters_it_once(tmp_path, monkeypatch):
    store = ChronicleStore(tmp_path)
    nested = _every_kind(store)
    outside = _readers(store, nested)
    assert outside[13] and outside[9] and outside[11] and outside[12] and any(r["kind"] == "gap" for r in outside[1])
    entered, real = [], ChronicleStore._index
    monkeypatch.setattr(ChronicleStore, "_index", lambda self: entered.append(1) or real(self))
    with chronicle_store.capture(tmp_path):
        inside = _readers(store, nested)
    assert entered == [1]  # the capture's own read; its readers ask the snapshot
    assert inside == outside


def test_a_capture_reads_one_state_and_holds_no_read_transaction_while_its_readers_run(tmp_path):
    store = ChronicleStore(tmp_path)
    first = _note(store, "first")
    with chronicle_store.capture(tmp_path):
        second = _note(ChronicleStore(tmp_path), "second")  # a publisher commits while the capture is pinned
        assert [r["id"] for r in store.room_records("r")] == [first["id"]] and store.get(second["id"]) is None
        assert store.room_head("r") == first["sequence"]  # every reader of the capture: the one state it read
    assert [r["id"] for r in store.room_records("r")] == [first["id"], second["id"]]

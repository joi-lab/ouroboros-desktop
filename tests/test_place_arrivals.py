"""Captured facts retain their exact source and their existing room lifetime."""
from dataclasses import replace
import json

import pytest

from ouroboros import dialogue_store, place_arrivals, presence_runner as runner
from tests.test_place_focus import focus as focus


@pytest.mark.parametrize("identity", [None, {}, {
    "sends_as": {"id": "writer", "label": "Writer café", "kind": "user"},
    "reads_as": [{"id": "reader", "label": "Reader", "kind": "user"}],
}])
def test_capture_identity_and_unchanged_legacy_row_bytes(focus, monkeypatch, identity):
    root, place, admission, event = focus
    # Works before and after F1A adds the optional frozen-dataclass field.
    object.__setattr__(event, "identity", identity)
    monkeypatch.setattr(runner, "utc_now_iso", lambda: "2026-10-26T12:00:00+00:00")
    monkeypatch.setattr(runner, "read_json_dict", lambda _: {"session_id": "fixed-session"})
    task = runner.capture_presence_event(admission, event, drive_root=root)
    expected = {
        "ts": "2026-10-26T12:00:00+00:00", "session_id": "fixed-session",
        "direction": "in", "type": "place_arrival", "chat_id": task["chat_id"],
        "user_id": runner._stable_numeric_id("presence-actor-log", str(task["actor_id"])),
        "text": "hello", "format": "", "source": "presence:chat",
        "sender_label": "", "sender_session_id": "person", "client_message_id": "event1",
        "presence_event_identity": task["metadata"]["presence_event_identity"],
        "transport": {"provider": "chat", "account_id": "a", "conversation_id": "r",
                      "thread_id": "", "conversation_key": "chat:a:r",
                      "actor": {"platform_actor_id": "person"}, "conversation": {}, "message": {}},
        "presence_provenance": {"place_id": place.place_id, "binding_id": place.place_id,
                                "transport_skill": "bridge", "behavior_skill": "",
                                "profile_fingerprint": "", "state_fingerprint": "", "selection_fingerprint": "",
                                "source_event_id": "event1", "conversation_key": "chat:a:r",
                                "provider": "chat", "account_id": "a", "conversation_id": "r",
                                "thread_id": "", "actor_id": "person"},
        "task_id": task["id"], "place_id": place.place_id,
    }
    if identity is not None:
        expected["transport"]["identity"] = identity
    with dialogue_store.transaction(root, write=False) as conn:
        row, = conn.execute("SELECT body FROM dialogue WHERE stream='chat'").fetchall()
    assert row["body"] == json.dumps(expected, ensure_ascii=False)


@pytest.mark.parametrize("declared", [None, (), ("mention", "signal.words")])
def test_heard_retains_declared_facts_without_inference_or_source_rewrite(focus, declared):
    root, _, admission, event = focus
    event = replace(event, facts=declared, actor={**event.actor, "is_bot": True})
    task = runner.capture_presence_event(admission, event, drive_root=root)
    source, = dialogue_store.iter_rows(root, "chat")
    result = place_arrivals.record_heard(root, task, event, "no_wake_match")
    with dialogue_store.transaction(root, write=False) as conn:
        row, = conn.execute("SELECT body,dispatch_input FROM dialogue WHERE stream='chat'").fetchall()
    assert json.loads(row["dispatch_input"])["place_attention"] == {
        "decision": "heard", "reason": "no_wake_match", "delivery_reporting_version": 0,
        "facts": list(declared or ()),
    }
    assert json.loads(row["body"]) == source.row
    assert place_arrivals.heard_replay(root, task["id"]) == result


def test_old_heard_decision_replays_without_backfilling_facts(focus):
    root, _, admission, event = focus
    task = runner.capture_presence_event(admission, event, drive_root=root)
    legacy = {"place_attention": {"decision": "heard", "reason": "off", "delivery_reporting_version": 0}}
    with dialogue_store.transaction(root, write=True) as conn:
        conn.execute("UPDATE dialogue SET dispatch_input=? WHERE task_id=?", (json.dumps(legacy), task["id"]))
    before = place_arrivals.heard_replay(root, task["id"])
    assert before.outcome == "silent"
    assert place_arrivals.record_heard(root, task, event, "paused") == before
    with dialogue_store.transaction(root, write=False) as conn:
        row, = conn.execute("SELECT dispatch_input FROM dialogue WHERE stream='chat'").fetchall()
    assert json.loads(row["dispatch_input"]) == legacy


def test_focus_source_retains_no_attention_facts(focus):
    root, _, admission, event = focus
    task = runner.capture_presence_event(admission, event, drive_root=root)
    with place_arrivals.intake_lock(root, event.conversation_key):
        place_arrivals.retain_focus_source(root, task["id"])
    with dialogue_store.transaction(root, write=False) as conn:
        row, = conn.execute("SELECT dispatch_input FROM dialogue WHERE stream='chat'").fetchall()
    assert set(json.loads(row["dispatch_input"])) == {"place_context_source_ref"}

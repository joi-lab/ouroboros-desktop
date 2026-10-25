"""The Swarm annotation is an acceptance fact, shared by live echo and history."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from ouroboros import config, projects_registry as projects
from ouroboros.composer_cognition import update_project_cognition
from ouroboros.gateway.history import make_chat_history_endpoint
from supervisor import message_ingress
from tests.test_task_settings_snapshot import document, isolated_runtime, save_owner  # noqa: F401
from tests.test_chat_attachments import _web_bridge


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("accepted,later", [("swarm", "off"), ("auto", "swarm"), ("off", "swarm")])
def test_acceptance_label_survives_room_switch_and_duplicate_echo(monkeypatch, project, accepted, later):
    values = {**document("old"), "OUROBOROS_SUBAGENTS_MODE": accepted}
    save_owner(values)
    room = projects.create_project(config.DATA_DIR, "alpha") if project else {"chat_id": 1, "id": ""}
    bridge, echoes = _web_bridge(config.DATA_DIR, monkeypatch)
    sent = dict(broadcast=True, sender_session_id="surface", client_message_id="accepted",
                chat_id=room["chat_id"], project_id=room["id"])
    bridge.ui_send("Owner words", **sent)
    row = message_ingress.accepted_chat_message(config.DATA_DIR, room["chat_id"], "accepted")
    if project:
        update_project_cognition(config.DATA_DIR, "alpha", subagents_mode=later)
    else:
        save_owner({**values, "OUROBOROS_SUBAGENTS_MODE": later})
    bridge.ui_send("Owner words", **sent)
    assert message_ingress.accepted_chat_message(config.DATA_DIR, room["chat_id"], "accepted") == row
    assert row.get("subagents_mode") == ("swarm" if accepted == "swarm" else None)
    assert len(echoes) == 2
    assert all(echo.get("subagents_mode") == row.get("subagents_mode") for echo in echoes)
    response = asyncio.run(make_chat_history_endpoint(config.DATA_DIR)(SimpleNamespace(
        query_params={"chat_id": str(room["chat_id"]), "limit": "10"})))
    rows = json.loads(response.body)["messages"]
    replay = next(item for item in rows if item.get("client_message_id") == "accepted")
    assert replay.get("subagents_mode") == row.get("subagents_mode")


def test_unannotated_legacy_history_is_readable_and_has_no_inferred_label():
    save_owner({**document("old"), "OUROBOROS_SUBAGENTS_MODE": "swarm"})
    path = config.DATA_DIR / "logs/chat.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"direction": "in", "chat_id": 1, "user_id": 1,
                               "text": "old row", "ts": "2026-10-01T01:00:00Z"}) + "\n")
    response = asyncio.run(make_chat_history_endpoint(config.DATA_DIR)(SimpleNamespace(query_params={})))
    replay = next(item for item in json.loads(response.body)["messages"] if item["text"] == "old row")
    assert "subagents_mode" not in replay


@pytest.mark.parametrize("mode", ["off", "auto", "swarm"])
def test_direct_owner_root_records_live_admission_not_client_metadata(monkeypatch, mode):
    from ouroboros import agent as agent_module
    from supervisor import workers
    from tests.test_native_conversation_activity import _lane

    save_owner({**document("old"), "OUROBOROS_SUBAGENTS_MODE": mode})
    _lane(monkeypatch, config.DATA_DIR)
    seen = []
    monkeypatch.setattr(agent_module, "make_agent", lambda **kwargs: SimpleNamespace(
        handle_task=lambda task: seen.append(task) or []))
    workers.handle_chat_direct(1, "Work", task_metadata={"subagents_mode": "forged", "force_plan": True})
    assert len(seen) == 1
    assert seen[0]["metadata"]["subagents_mode"] == mode
    assert "subagents_mode" not in seen[0]["task_contract"]


def test_legacy_force_plan_frame_is_accepted_and_ignored(monkeypatch):
    from starlette.websockets import WebSocketDisconnect
    from ouroboros.gateway import ws
    from supervisor import message_bus

    delivered, warnings = [], []
    monkeypatch.setattr(message_bus, "try_get_bridge", lambda: SimpleNamespace(
        ui_send=lambda text, **kwargs: delivered.append((text, kwargs))))

    class Socket:
        app = SimpleNamespace(state=SimpleNamespace(drive_root=config.DATA_DIR))
        first = True

        async def accept(self):
            pass

        async def receive_text(self):
            if self.first:
                self.first = False
                return json.dumps({"type": "chat", "content": "ordinary request", "force_plan": True})
            for _ in range(100):
                if delivered:
                    break
                await asyncio.sleep(0.01)
            raise WebSocketDisconnect()

        async def send_text(self, text):
            warnings.append(text)

    asyncio.run(ws.ws_endpoint(Socket()))
    assert len(delivered) == 1 and not warnings
    assert delivered[0][0] == "ordinary request"
    assert "force_plan" not in delivered[0][1]["task_metadata"]
    assert "force_plan_source" not in delivered[0][1]["task_metadata"]


def test_state_serves_stored_main_mode_with_settings_lock():
    from ouroboros.gateway.state import api_state

    save_owner({**document("old"), "OUROBOROS_SUBAGENTS_MODE": "swarm",
                "OUROBOROS_SUBAGENTS": '{"enabled":false,"items":[]}'})
    response = asyncio.run(api_state(SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(drive_root=config.DATA_DIR)))))
    assert response.status_code == 200, response.body
    payload = json.loads(response.body)
    assert payload["subagents_mode"] == "swarm"
    assert payload["subagents_availability"] == "disabled"

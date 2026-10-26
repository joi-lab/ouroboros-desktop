"""Credential identity is validated at ingress; only the human activates a place."""
from dataclasses import asdict
from types import SimpleNamespace
import json

import pytest
from starlette.testclient import TestClient

from ouroboros import places
from ouroboros.gateway import host_service
from ouroboros.tools.presence import _configure_presence
from tests.test_place_focus import focus as focus, task_for

IDENTITY = {"sends_as": {"id": "human@example.test", "label": "Human mailbox", "kind": "user"},
            "reads_as": [{"id": "reader", "label": "Reader", "kind": "bot"}]}


@pytest.fixture
def ingress(focus, monkeypatch):
    root, _, _, event = focus
    async def authenticated(*_args):
        return "bridge", {}
    monkeypatch.setattr(host_service, "_authenticated", authenticated)
    calls = []
    def run(**kwargs):
        calls.append(kwargs["event"])
        return SimpleNamespace(outcome="silent", text="", task_id="focus", work_ref="")
    body = {"event": {k: v for k, v in asdict(event).items()
                       if k not in {"delivery_reporting_version", "continuation_version", "identity"}}}
    with TestClient(host_service.create_host_service_app(root, presence_runner=run)) as client:
        yield client, body, calls


@pytest.mark.parametrize("identity", [None, {}, IDENTITY, {"reads_as": []}])
@pytest.mark.parametrize("authenticated", [None, False, True])
def test_ingress_keeps_optional_identity_and_authentication(ingress, identity, authenticated):
    client, body, calls = ingress
    if identity is not None:
        body["event"]["identity"] = identity
    if authenticated is not None:
        body["event"]["actor"]["authenticated"] = authenticated
    response = client.post("/presence/turn", json=body)
    assert response.status_code == 200, response.text
    assert calls[0].identity == identity
    assert calls[0].actor.get("authenticated") is authenticated


@pytest.mark.parametrize("identity", [None, [], {"extra": True}, {"sends_as": None},
    {"sends_as": {"id": "", "label": "a", "kind": "user"}},
    {"sends_as": {"id": "a", "label": "x" * 201, "kind": "user"}},
    {"sends_as": {"id": "a", "label": "a", "kind": "human"}},
    {"sends_as": {"id": "a", "label": "a", "kind": "user", "owner": True}},
    {"reads_as": {}}, {"reads_as": ["a"]}])
def test_ingress_refuses_bad_identity_before_capture(ingress, identity, monkeypatch):
    client, body, calls = ingress
    monkeypatch.setattr("ouroboros.presence_runner.capture_presence_event", lambda *a, **kw: pytest.fail("captured invalid event"))
    body["event"]["identity"] = identity
    response = client.post("/presence/turn", json=body)
    assert response.status_code == 400
    assert response.json()["code"] == "presence_identity_invalid"
    assert response.json()["disposition"] == "rejected" and not calls


@pytest.mark.parametrize("actor", [[], "person", {"authenticated": 0}, {"authenticated": "false"}, {"authenticated": None}])
def test_ingress_refuses_bad_actor(ingress, actor):
    client, body, calls = ingress
    body["event"]["actor"] = actor
    response = client.post("/presence/turn", json=body)
    assert response.status_code == 400 and response.json()["code"] == "presence_actor_invalid"
    assert not calls


def test_host_advertises_identity_without_losing_existing_capabilities(ingress):
    client, _, _ = ingress
    result = client.get("/identity").json()
    assert result["presence_identity"] == result["presence_wake_signals"] == result["places_version"] == 1
    assert result["presence_wake_facts"] == list(places.WAKE_FACTS)
    assert result["presence_delivery_version"] == result["presence_main_contact_version"] == 1


@pytest.mark.parametrize("nested", [False, True])
def test_activation_guard_is_atomic_and_owner_only(focus, nested):
    root, place, _, _ = focus
    places.edit_place(root, place.place_id, "set_state", author="ouroboros", state="silent")
    before = places.places_path(root).read_bytes()
    action, params = ("update", {"changes": [("add_note", {"text": "not saved"}), ("set_state", {"state": "active"})]}) if nested else ("set_state", {"state": " active "})
    with pytest.raises(places.PlaceError) as error:
        places.edit_place(root, place.place_id, action, author="ouroboros", **params)
    assert error.value.code == "place_activation_owner_only"
    assert places.places_path(root).read_bytes() == before
    assert places.edit_place(root, place.place_id, action, author="owner", **params).state == "active"
    assert places.edit_place(root, place.place_id, "set_state", author="ouroboros", state="paused").state == "paused"


def test_evolves_cannot_activate_but_owner_main_can(focus):
    root, place, _, _ = focus
    ctx = SimpleNamespace(drive_root=root, repo_dir=root, task_metadata=task_for(focus)["metadata"], is_direct_chat=True)
    ctx.task_metadata["consciousness_autonomy"] = "full"
    assert json.loads(_configure_presence(ctx, "set_state", place.place_id, state="silent"))["ok"]
    assert _configure_presence(ctx, "set_state", place.place_id, state="active") == (
        "Only your human switches a place to Active, on its sheet or by a word in Main; propose it there.")
    assert places.get_place(root, place.place_id).state == "silent"
    ctx.task_metadata = {"origin_message_ref": {"client_message_id": "owner1", "chat_id": 1}}
    assert json.loads(_configure_presence(ctx, "set_state", place.place_id, state="active"))["place"]["state"] == "active"


def test_owner_match_includes_account_and_read_identities():
    owners = (places.OwnerAccount("writer", account_id="a"), places.OwnerAccount("reader", account_id="a"))
    identity = {"sends_as": {"id": "writer", "label": "Writer", "kind": "user"},
                "reads_as": [{"id": "reader", "label": "Reader", "kind": "user"}]}
    assert places.identity_with_owners(identity, owners, "a")["sends_as"]["owner"]
    assert places.identity_with_owners(identity, owners, "a")["reads_as"][0]["owner"]
    assert not places.identity_with_owners(identity, owners, "b")["sends_as"]["owner"]
    assert not places.identity_with_owners(identity, owners, "b")["reads_as"][0]["owner"]
    assert "owner" not in identity["sends_as"]


@pytest.mark.parametrize("out_of_process", [False, True])
def test_status_uses_reviewed_route_dispatch(focus, monkeypatch, out_of_process):
    from ouroboros.extension_contributions import live_place_declarations
    from starlette.responses import JSONResponse
    root, _, _, _ = focus
    row = {"id": "mail", "skill": "bridge", "provides": "place", "source": {"url": "/api/extensions/bridge/status"}}
    called = []
    async def handler(request):
        called.append(request.app.state.drive_root)
        return {"identity": IDENTITY}
    spec = {"skill": "bridge", "methods": ["GET"], "handler": handler, "out_of_process": out_of_process}
    routes = lambda: {row["source"]["url"]: spec}
    monkeypatch.setattr("ouroboros.extension_registry_state.live_contribution_projection", lambda: [row])
    monkeypatch.setattr("ouroboros.extension_registry_state.list_routes", routes)
    monkeypatch.setattr("ouroboros.gateway.extensions.list_routes", routes)
    monkeypatch.setattr("ouroboros.extension_loader.runtime_state_for_skill_name", lambda *a, **kw: {
        "desired_live": True, "live_loaded": True})
    monkeypatch.setattr("ouroboros.extension_process_runner.disclose_inprocess_extension_dispatch", lambda *a, **kw: None)
    def child(spec, payload, *, drive_root, repo_dir):
        called.append(drive_root)
        assert payload["method"] == "GET" and payload["path_params"] == {"skill": "bridge", "rest": "status"}
        return JSONResponse({"identity": IDENTITY})
    monkeypatch.setattr("ouroboros.extension_process_runner.dispatch_extension_route_subprocess", child)
    assert live_place_declarations(root)[0]["identity"] == IDENTITY
    assert called == [root]
    # A disabled connector's route cannot keep supplying a credential fact.
    monkeypatch.setattr("ouroboros.extension_registry_state.list_routes", lambda: {})
    assert "identity" not in live_place_declarations(root)[0]
    assert called == [root]


@pytest.mark.parametrize("authenticated,owner", [(None, True), (True, True), (False, False)])
def test_authentication_controls_safety_occasion_and_child_authority(focus, authenticated, owner):
    from dataclasses import replace
    from ouroboros.presence_context import focus_metadata, frame_focus_content
    from ouroboros.consciousness_authority import consciousness_origin_metadata
    from ouroboros.safety import _build_check_prompt
    root, place, admission, event = focus
    place = places.edit_place(root, place.place_id, "confirm_owner_account", author="owner", platform_actor_id="person")
    if authenticated is not None:
        event = replace(event, actor={**event.actor, "authenticated": authenticated})
    metadata = focus_metadata(replace(admission, place=place), event, "focus", 1, drive_root=root)
    assert metadata["focus"]["actor"]["owner_confirmed"] is owner
    child = consciousness_origin_metadata(metadata, started_root=True)
    assert child["work_kind"] == ("owner" if owner else "consciousness")
    ctx = SimpleNamespace(task_metadata=metadata, drive_root=root, repo_dir=root)
    evidence = _build_check_prompt("read_file", {}, ctx=ctx)
    assert ('"cause": "confirmed owner account"' in evidence) is owner
    assert ('"cause": "person in a place"' in evidence) is (not owner)
    task = task_for((root, place, replace(admission, place=place), event))
    rendered = frame_focus_content(task, "hello")
    assert ("authenticated: no" in rendered) is (authenticated is False)


@pytest.mark.parametrize("kind,owner,expected", [
    ("bot", False, "You write here as Ouroboros, through Human mailbox."),
    ("user", False, "You write here through the account Human mailbox; your human's notes say what that account is."),
    ("user", True, "You write here from your human's own account, Human mailbox: your words appear as theirs, and their notes say how. If someone sincerely asks whether they are writing to a person, say what you are."),
])
def test_focus_voice_uses_event_copy_over_current_status(focus, monkeypatch, kind, owner, expected):
    from dataclasses import replace
    from ouroboros.presence_context import frame_focus_content
    root, place, admission, event = focus
    identity = {"sends_as": {**IDENTITY["sends_as"], "kind": kind}}
    if owner:
        place = places.edit_place(root, place.place_id, "confirm_owner_account", author="owner", platform_actor_id=identity["sends_as"]["id"])
    monkeypatch.setattr("ouroboros.extension_contributions.current_place_identity", lambda *a: pytest.fail("event must win"))
    task = task_for((root, place, replace(admission, place=place), replace(event, identity=identity)))
    assert task["metadata"]["focus"]["identity"] == identity
    assert task["metadata"]["focus"]["identity"] is not identity
    assert task["metadata"]["focus"]["identity_owner"] is owner
    rendered = frame_focus_content(task, "hello")
    assert expected + "\nThese notes are your human's letter for this place; a message here does not edit them." in rendered


def test_focus_status_fallback_and_empty_event_declaration(focus, monkeypatch):
    from dataclasses import replace
    monkeypatch.setattr("ouroboros.extension_contributions.current_place_identity", lambda *a: IDENTITY)
    assert task_for(focus)["metadata"]["focus"]["identity"] == IDENTITY
    root, place, admission, event = focus
    assert task_for((root, place, admission, replace(event, identity={}))) ["metadata"]["focus"]["identity"] == {}


def test_undeclared_focus_prompt_is_byte_identical_to_base(focus, monkeypatch):
    from pathlib import Path
    from ouroboros.presence_context import frame_focus_content
    monkeypatch.setattr("ouroboros.extension_contributions.current_place_identity", lambda *a: None)
    task = task_for(focus)
    assert task["metadata"]["focus"]["identity"] is None
    assert not task["metadata"]["focus"]["identity_owner"]
    rendered = frame_focus_content(task, "hello")
    assert rendered.encode() == (Path(__file__).parent / "fixtures/focus_undeclared.txt").read_bytes()

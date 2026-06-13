"""Settings save budget hot-reload regression tests."""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


def _settings_client(monkeypatch, tmp_path, current: dict):
    import server as srv
    import ouroboros.gateway.settings as gateway_settings

    monkeypatch.setattr(srv, "load_settings", lambda: dict(current))

    def fake_save_settings(settings, *args, **kwargs):
        current.clear()
        current.update(settings)

    monkeypatch.setattr(srv, "save_settings", fake_save_settings)
    monkeypatch.setattr(gateway_settings, "_owner_write_settings", fake_save_settings)
    monkeypatch.setattr(srv, "_apply_settings_to_env", lambda *_a, **_k: None)
    monkeypatch.setattr(srv, "_start_supervisor_if_needed", lambda *_a, **_k: False)
    monkeypatch.setattr(srv, "apply_runtime_provider_defaults", lambda s: (dict(s), False, []))
    monkeypatch.setattr(srv, "_mcp_reconfigure_startup", lambda *_a, **_k: None, raising=False)

    app = Starlette(routes=[Route("/api/settings", endpoint=srv.api_settings_post, methods=["POST"])])
    app.state.drive_root = tmp_path / "drive"
    app.state.repo_dir = tmp_path / "repo"
    return TestClient(app)


def test_settings_post_updates_budget_limits_and_per_task_threshold(monkeypatch, tmp_path):
    import server as srv
    import supervisor.message_bus as bus_mod
    import supervisor.state as state_mod

    current = dict(srv._SETTINGS_DEFAULTS)
    current["TOTAL_BUDGET"] = 10.0
    monkeypatch.setattr(state_mod, "TOTAL_BUDGET_LIMIT", 10.0)
    monkeypatch.setattr(bus_mod, "TOTAL_BUDGET_LIMIT", 10.0)

    client = _settings_client(monkeypatch, tmp_path, current)

    resp = client.post("/api/settings", json={"TOTAL_BUDGET": 25.0})

    assert resp.status_code == 200, resp.text
    assert resp.json().get("immediate_changed") is True
    assert state_mod.TOTAL_BUDGET_LIMIT == 25.0
    assert bus_mod.TOTAL_BUDGET_LIMIT == 25.0

    resp = client.post("/api/settings", json={"OUROBOROS_PER_TASK_COST_USD": "7.5"})

    assert resp.status_code == 200, resp.text
    assert resp.json().get("immediate_changed") is not True
    assert resp.json().get("next_task_changed") is True
    assert current["OUROBOROS_PER_TASK_COST_USD"] == 7.5

    invalid_cases = [
        ({"TOTAL_BUDGET": 0}, "greater than zero"),
        ({"TOTAL_BUDGET": 0.005}, "at least 0.01"),
        (["TOTAL_BUDGET", 25], "JSON body must be an object."),
        ({"OUROBOROS_PER_TASK_COST_USD": "nan"}, "must be a number"),
        ({"OUROBOROS_PER_TASK_COST_USD": "0.005"}, "at least 0.01"),
        ({"TOTAL_BUDGET": True}, "must be a number"),
    ]
    clean_budget_state = dict(current)
    clean_budget_state["TOTAL_BUDGET"] = 10.0
    clean_budget_state["OUROBOROS_PER_TASK_COST_USD"] = 20.0
    for payload, error in invalid_cases:
        current.clear()
        current.update(clean_budget_state)
        resp = client.post("/api/settings", json=payload)

        assert resp.status_code == 400
        assert error in resp.json()["error"]
        assert current["TOTAL_BUDGET"] == 10.0
        assert current["OUROBOROS_PER_TASK_COST_USD"] == 20.0

"""Tests for LAN access toggle feature (OUROBOROS_SERVER_HOST)."""
import importlib
import json
import os
import types


def test_server_host_in_settings_defaults():
    """OUROBOROS_SERVER_HOST must be present in SETTINGS_DEFAULTS."""
    from ouroboros.config import SETTINGS_DEFAULTS
    assert "OUROBOROS_SERVER_HOST" in SETTINGS_DEFAULTS
    assert SETTINGS_DEFAULTS["OUROBOROS_SERVER_HOST"] == "127.0.0.1"


def test_apply_settings_to_env_propagates_server_host(monkeypatch):
    """apply_settings_to_env must push OUROBOROS_SERVER_HOST into os.environ."""
    monkeypatch.delenv("OUROBOROS_SERVER_HOST", raising=False)
    from ouroboros.config import apply_settings_to_env, SETTINGS_DEFAULTS
    settings = {**SETTINGS_DEFAULTS, "OUROBOROS_SERVER_HOST": "0.0.0.0"}
    apply_settings_to_env(settings)
    assert os.environ.get("OUROBOROS_SERVER_HOST") == "0.0.0.0"


def test_apply_settings_to_env_clears_empty_host(monkeypatch):
    """When OUROBOROS_SERVER_HOST is empty, it should be removed from env."""
    monkeypatch.setenv("OUROBOROS_SERVER_HOST", "0.0.0.0")
    from ouroboros.config import apply_settings_to_env, SETTINGS_DEFAULTS
    settings = {**SETTINGS_DEFAULTS, "OUROBOROS_SERVER_HOST": ""}
    apply_settings_to_env(settings)
    assert "OUROBOROS_SERVER_HOST" not in os.environ


def test_server_host_in_restart_required_keys():
    """Changing OUROBOROS_SERVER_HOST requires a restart."""
    import importlib
    import server as srv_mod
    assert "OUROBOROS_SERVER_HOST" in srv_mod._RESTART_REQUIRED_KEYS


def test_main_reads_settings_before_bind(monkeypatch):
    """main() must call load_settings and apply_settings_to_env before parse_server_args."""
    call_order = []

    from ouroboros.config import SETTINGS_DEFAULTS
    fake_settings = {**SETTINGS_DEFAULTS, "OUROBOROS_SERVER_HOST": "0.0.0.0"}

    def fake_load():
        call_order.append("load_settings")
        return fake_settings

    def fake_apply(s):
        call_order.append("apply_settings")
        os.environ["OUROBOROS_SERVER_HOST"] = s.get("OUROBOROS_SERVER_HOST", "127.0.0.1")

    class FakeArgs:
        host = "0.0.0.0"
        port = 8765

    def fake_parse(host, port):
        call_order.append(f"parse_server_args:{host}")
        return FakeArgs()

    # Patch at server module level
    import server as srv_mod
    monkeypatch.setattr(srv_mod, "load_settings", fake_load)
    monkeypatch.setattr(srv_mod, "_apply_settings_to_env", fake_apply)
    monkeypatch.setattr(srv_mod, "parse_server_args", fake_parse)
    monkeypatch.setattr(srv_mod, "get_network_auth_startup_warning", lambda h: None)
    monkeypatch.setattr(srv_mod, "validate_network_auth_configuration", lambda h: "block")

    srv_mod.main()

    assert "load_settings" in call_order
    assert "apply_settings" in call_order
    # parse_server_args must be called AFTER apply_settings with the effective host
    load_idx = call_order.index("load_settings")
    apply_idx = call_order.index("apply_settings")
    parse_entries = [c for c in call_order if c.startswith("parse_server_args:")]
    assert parse_entries, "parse_server_args must be called"
    parse_idx = call_order.index(parse_entries[0])
    assert load_idx < apply_idx < parse_idx


def test_api_network_info_returns_ips(monkeypatch):
    """api_network_info must return a JSON response with ips and port."""
    import server as srv_mod
    import asyncio

    # Mock socket to return a known IP
    class FakeSocket:
        def connect(self, addr):
            pass
        def getsockname(self):
            return ("192.168.1.42", 0)
        def close(self):
            pass

    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: FakeSocket())
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", "9999")

    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    test_app = Starlette(routes=[Route("/api/network-info", endpoint=srv_mod.api_network_info)])
    client = TestClient(test_app)
    resp = client.get("/api/network-info")
    assert resp.status_code == 200
    data = resp.json()
    assert "ips" in data
    assert "port" in data
    assert data["port"] == 9999
    assert "192.168.1.42" in data["ips"]


def test_api_network_info_filters_link_local(monkeypatch):
    """api_network_info must filter out 169.254.x.x link-local addresses."""
    import server as srv_mod

    class FakeSocket:
        def connect(self, addr):
            pass
        def getsockname(self):
            return ("169.254.19.0", 0)
        def close(self):
            pass

    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **kw: FakeSocket())
    # Also mock getaddrinfo to return nothing useful
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [])
    monkeypatch.setattr(socket, "gethostname", lambda: "test-host")

    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    test_app = Starlette(routes=[Route("/api/network-info", endpoint=srv_mod.api_network_info)])
    client = TestClient(test_app)
    resp = client.get("/api/network-info")
    data = resp.json()
    assert "169.254.19.0" not in data["ips"]

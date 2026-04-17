"""Tests for LAN access toggle feature (OUROBOROS_SERVER_HOST)."""
import os


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


def test_discover_lan_ips_prefers_hostname_over_udp_trick(monkeypatch):
    """
    _discover_lan_ips must NOT invoke the UDP-connect fallback when
    gethostbyname_ex already returns a non-loopback IP. This is the
    critical fix for the VPN-interception bug: socket.connect('8.8.8.8')
    picks up VPN tunnel IPs (e.g. 172.16.x.x) when a VPN captures the
    default route, but the hostname resolver typically returns the real
    LAN interface.
    """
    import server as srv_mod
    import socket as _sock

    udp_called = {"n": 0}

    def fake_gethostbyname_ex(hostname):
        return (hostname, [], ["192.168.0.92"])

    class _ShouldNotBeUsed:
        def __init__(self, *a, **kw):
            udp_called["n"] += 1

        def connect(self, addr):
            raise AssertionError("UDP fallback should not run when primary succeeded")

        def getsockname(self):
            return ("172.16.9.1", 0)

        def close(self):
            pass

    monkeypatch.setattr(_sock, "gethostname", lambda: "test-host")
    monkeypatch.setattr(_sock, "gethostbyname_ex", fake_gethostbyname_ex)
    monkeypatch.setattr(_sock, "getaddrinfo", lambda *a, **kw: [])
    monkeypatch.setattr(_sock, "socket", _ShouldNotBeUsed)

    ips = srv_mod._discover_lan_ips()

    assert "192.168.0.92" in ips, f"Expected LAN IP in result, got {ips!r}"
    assert "172.16.9.1" not in ips, "VPN IP must not leak in when primary path succeeds"
    assert udp_called["n"] == 0, "UDP socket fallback should not have been instantiated"


def test_discover_lan_ips_filters_loopback_and_link_local(monkeypatch):
    """Loopback (127.*) and link-local (169.254.*) must never appear in results."""
    import server as srv_mod
    import socket as _sock

    def fake_gethostbyname_ex(hostname):
        return (hostname, [], ["127.0.0.1", "169.254.19.0", "192.168.0.92"])

    monkeypatch.setattr(_sock, "gethostname", lambda: "test-host")
    monkeypatch.setattr(_sock, "gethostbyname_ex", fake_gethostbyname_ex)
    monkeypatch.setattr(_sock, "getaddrinfo", lambda *a, **kw: [])

    ips = srv_mod._discover_lan_ips()

    assert ips == ["192.168.0.92"]


def test_discover_lan_ips_deduplicates(monkeypatch):
    """If the same IP is returned by multiple resolvers, it must appear only once."""
    import server as srv_mod
    import socket as _sock

    def fake_gethostbyname_ex(hostname):
        return (hostname, [], ["192.168.0.92"])

    def fake_getaddrinfo(host, port, family):
        # Duplicate of the primary result plus a new one
        return [
            (family, 0, 0, "", ("192.168.0.92", 0)),
            (family, 0, 0, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(_sock, "gethostname", lambda: "test-host")
    monkeypatch.setattr(_sock, "gethostbyname_ex", fake_gethostbyname_ex)
    monkeypatch.setattr(_sock, "getaddrinfo", fake_getaddrinfo)

    ips = srv_mod._discover_lan_ips()

    assert ips.count("192.168.0.92") == 1, f"Duplicates slipped through: {ips!r}"
    assert "10.0.0.5" in ips


def test_discover_lan_ips_udp_fallback_only_when_hostname_empty(monkeypatch):
    """When hostname resolution fails entirely, UDP-connect is the final fallback."""
    import server as srv_mod
    import socket as _sock

    def fake_gethostbyname_ex(hostname):
        raise OSError("name resolution failed")

    class FakeSocket:
        def __init__(self, *a, **kw):
            self.closed = False

        def connect(self, addr):
            pass

        def getsockname(self):
            return ("10.99.0.7", 0)

        def close(self):
            self.closed = True

    monkeypatch.setattr(_sock, "gethostname", lambda: "test-host")
    monkeypatch.setattr(_sock, "gethostbyname_ex", fake_gethostbyname_ex)
    monkeypatch.setattr(_sock, "getaddrinfo", lambda *a, **kw: [])
    monkeypatch.setattr(_sock, "socket", FakeSocket)

    ips = srv_mod._discover_lan_ips()

    assert ips == ["10.99.0.7"]


def test_api_network_info_returns_ips_and_port(monkeypatch):
    """api_network_info must return JSON {ips, port} using _discover_lan_ips."""
    import server as srv_mod

    monkeypatch.setattr(srv_mod, "_discover_lan_ips", lambda: ["192.168.0.92"])
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", "9999")

    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    test_app = Starlette(routes=[Route("/api/network-info", endpoint=srv_mod.api_network_info)])
    client = TestClient(test_app)
    resp = client.get("/api/network-info")
    assert resp.status_code == 200
    data = resp.json()
    assert data["port"] == 9999
    assert data["ips"] == ["192.168.0.92"]


def test_api_network_info_dispatches_to_thread(monkeypatch):
    """The async endpoint must not call _discover_lan_ips synchronously on the event loop."""
    import server as srv_mod
    import asyncio

    calls = {"direct": 0, "thread": 0}

    def fake_discover():
        calls["direct"] += 1
        return ["192.168.0.92"]

    real_to_thread = asyncio.to_thread

    async def spy_to_thread(fn, *a, **kw):
        calls["thread"] += 1
        return await real_to_thread(fn, *a, **kw)

    monkeypatch.setattr(srv_mod, "_discover_lan_ips", fake_discover)
    monkeypatch.setattr(asyncio, "to_thread", spy_to_thread)

    from starlette.testclient import TestClient
    from starlette.applications import Starlette
    from starlette.routing import Route

    test_app = Starlette(routes=[Route("/api/network-info", endpoint=srv_mod.api_network_info)])
    client = TestClient(test_app)
    resp = client.get("/api/network-info")
    assert resp.status_code == 200
    assert calls["thread"] == 1, "asyncio.to_thread should have been used to dispatch the blocking work"
    assert calls["direct"] == 1, "_discover_lan_ips should have been executed exactly once"

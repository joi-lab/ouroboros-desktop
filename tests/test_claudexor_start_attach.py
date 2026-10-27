"""START reuses the handshake that proved the daemon live; a silent listener is waited for, never respawned."""
import http.server
import json
import os
import pathlib
import sys
import threading
import time

import pytest

from ouroboros import claudexor_daemon as owned
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from ouroboros.model_wait import execution_deadline_scope, monotonic_now

pytestmark = pytest.mark.serial  # real loopback ports, as in test_claudexor_admission_wait.py


def _point_owned_home(monkeypatch, config_dir: pathlib.Path, data_dir: pathlib.Path) -> None:
    monkeypatch.setattr(owned, "owned_config_dir", lambda: config_dir)
    monkeypatch.setattr(owned, "owned_descriptor_path", lambda: config_dir / "daemon" / "control-api.json")
    monkeypatch.setattr(owned, "owned_daemon_provisioned",
                        lambda: (config_dir / "daemon" / "control-api.json").is_file())
    import ouroboros.config as config_mod
    monkeypatch.setattr(config_mod, "DATA_DIR", data_dir)


def _daemon(token: str, *, silent_for: int):
    """A daemon that accepts the socket but answers nothing for its first ``silent_for`` handshakes."""
    state = {"handshakes": 0, "silent_for": silent_for, "hold": threading.Event()}

    class _Daemon(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.headers.get("Authorization") != f"Bearer {token}":
                self._send(401, {})
                return
            if self.path != "/v2/handshake":
                self._send(200, {})
                return
            state["handshakes"] += 1
            if state["handshakes"] <= state["silent_for"]:
                state["hold"].wait(30)  # an accepted socket with no answer: a busy loop, not an absent daemon
                return
            self._send(200, {"compatible": True, "protocolMajor": 3, "servingMode": "normal",
                             "engine": {"version": "9.9.9", "sha": "live"}})

        def do_GET(self):
            self._send(200, {})

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Daemon)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, state


def _manager(monkeypatch, tmp_path, server, token, *, silent_wait: float):
    data_dir = tmp_path / "data"
    config_dir = data_dir / "claudexor"
    _point_owned_home(monkeypatch, config_dir, data_dir)
    daemon_dir = config_dir / "daemon"
    daemon_dir.mkdir(parents=True)
    (daemon_dir / "token").write_text(token, encoding="utf-8")
    (daemon_dir / "control-api.json").write_text(json.dumps({
        "host": "127.0.0.1", "port": server.server_address[1], "tokenPath": str(daemon_dir / "token")}),
        encoding="utf-8")
    monkeypatch.setattr(owned, "_SPAWN_WAIT_SEC", silent_wait)
    monkeypatch.setattr(owned, "_SPAWN_POLL_SEC", 0.05)
    monkeypatch.setattr(owned, "_ADMISSION_POLL_SEC", 0.05)
    manager = owned.OwnedClaudexorDaemon()
    monkeypatch.setattr(owned, "get_owned_daemon", lambda: manager)
    monkeypatch.setattr(owned, "verify_owned_home", lambda **_kw: "")
    monkeypatch.setattr(owned, "_write_ownership_marker", lambda: None)
    monkeypatch.setattr(manager, "_startup_pids", lambda: {os.getpid()})  # the daemon's pid is in custody
    monkeypatch.setattr(manager, "reconcile_rotation", lambda _gateway: None)

    def no_spawn(*_a, **_kw):
        raise AssertionError("a daemon that holds its socket must never be spawned twice")

    monkeypatch.setattr(manager, "_spawn", no_spawn)
    monkeypatch.setenv("OUROBOROS_CLAUDEXOR_BIN", sys.executable)  # no runtime download while attaching
    return manager


def _bounded_probes(monkeypatch):
    """Every handshake of these tests is read-bounded, so a silent accept costs seconds, not the 60 s default."""
    from ouroboros.gateways import claudexor as gateway_module
    real = gateway_module.ClaudexorGateway.handshake
    monkeypatch.setattr(gateway_module.ClaudexorGateway, "handshake",
                        lambda self, *, timeout_sec=None: real(self, timeout_sec=min(timeout_sec or 0.4, 0.4)))


def test_start_reuses_the_proving_handshake_without_a_second_one(monkeypatch, tmp_path):
    server, state = _daemon("tok", silent_for=0)
    try:
        _manager(monkeypatch, tmp_path, server, "tok", silent_wait=2.0)
        gateway = owned.ensure_owned_gateway()
        try:
            assert gateway.engine_version == "9.9.9"
            assert state["handshakes"] == 1, "the probe that proved the daemon live is the start's handshake"
        finally:
            gateway.close()
    finally:
        server.shutdown()


def test_a_silent_accepted_socket_is_waited_for_within_the_callers_allowance_never_respawned(monkeypatch, tmp_path):
    server, state = _daemon("tok", silent_for=2)
    try:
        manager = _manager(monkeypatch, tmp_path, server, "tok", silent_wait=0.5)
        _bounded_probes(monkeypatch)
        started = time.monotonic()
        with execution_deadline_scope(monotonic_now() + 20.0):  # the review slot's own window
            gateway = owned.ensure_owned_gateway()
        try:
            assert gateway.engine_version == "9.9.9"
            assert state["handshakes"] >= 3 and time.monotonic() - started < 15.0
            assert manager._proc is None
        finally:
            gateway.close()
            state["hold"].set()
    finally:
        server.shutdown()


def test_an_allowance_that_ends_unanswered_keeps_the_typed_custody_of_a_live_daemon(monkeypatch, tmp_path):
    server, state = _daemon("tok", silent_for=100)
    try:
        _manager(monkeypatch, tmp_path, server, "tok", silent_wait=0.3)
        _bounded_probes(monkeypatch)
        with execution_deadline_scope(monotonic_now() + 1.5):
            with pytest.raises(ClaudexorUnavailable) as caught:
                owned.ensure_owned_gateway()
        assert caught.value.code == "daemon_not_answering", "alive and silent, never daemon_unreachable"
        assert caught.value.observation_reason == "observation_read_timeout"
        assert state["handshakes"] >= 2, "the allowance was spent retrying the same daemon"
    finally:
        state["hold"].set()
        server.shutdown()


def test_a_passive_sweep_keeps_its_short_bound_and_does_not_retry(monkeypatch, tmp_path):
    server, state = _daemon("tok", silent_for=100)
    try:
        _manager(monkeypatch, tmp_path, server, "tok", silent_wait=0.3)
        from ouroboros.gateways import claudexor as gateway_module
        monkeypatch.setattr(gateway_module, "SHORT_POLL_TIMEOUT_SEC", 0.3)
        started = time.monotonic()
        with execution_deadline_scope(monotonic_now() + 60.0):
            with pytest.raises(ClaudexorUnavailable) as caught:
                owned.ensure_owned_gateway(admission_wait_sec=0, startup_wait_sec=0.3)
        assert caught.value.code == "daemon_not_answering"
        assert time.monotonic() - started < 5.0, "a sweep skips and comes back next tick"
    finally:
        state["hold"].set()
        server.shutdown()

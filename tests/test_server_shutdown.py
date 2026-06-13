from types import SimpleNamespace


def test_main_normal_exit_does_not_run_emergency_cleanup(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr(server, "load_settings", lambda: {"OUROBOROS_SERVER_HOST": "127.0.0.1"})
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "get_network_auth_startup_warning", lambda _host: "")
    monkeypatch.setattr(server, "validate_network_auth_configuration", lambda _host: "")
    monkeypatch.setattr(server, "find_free_port", lambda _host, port: port)
    monkeypatch.setattr(server, "write_port_file", lambda *_a, **_k: None)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_emergency_process_cleanup", lambda: cleanup_calls.append("cleanup"))
    server._restart_requested.clear()

    assert server.main() == 0
    assert cleanup_calls == []


def test_main_graceful_restart_cleanup_avoids_port_sweep(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            server._restart_requested.set()
            return None

    class ExitCalled(RuntimeError):
        pass

    monkeypatch.setattr(server, "load_settings", lambda: {"OUROBOROS_SERVER_HOST": "127.0.0.1"})
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "get_network_auth_startup_warning", lambda _host: "")
    monkeypatch.setattr(server, "validate_network_auth_configuration", lambda _host: "")
    monkeypatch.setattr(server, "find_free_port", lambda _host, port: port)
    monkeypatch.setattr(server, "write_port_file", lambda *_a, **_k: None)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_LAUNCHER_MANAGED", True)
    monkeypatch.setattr(server, "_emergency_process_cleanup", lambda **kw: cleanup_calls.append(kw))
    monkeypatch.setattr(server.os, "_exit", lambda code: (_ for _ in ()).throw(ExitCalled(code)))
    server._restart_requested.clear()

    try:
        server.main()
    except ExitCalled:
        pass
    finally:
        server._restart_requested.clear()

    assert cleanup_calls == [{"port_sweep": False}]


def test_emergency_cleanup_kills_services_without_log_finalization(monkeypatch):
    import server

    service_calls = []
    worker_calls = []

    monkeypatch.setattr("ouroboros.tools.shell.kill_all_tracked_subprocesses", lambda: None)
    monkeypatch.setattr("ouroboros.tools.services.kill_all_services", lambda *a, **k: service_calls.append((a, k)))
    monkeypatch.setattr("supervisor.workers.kill_workers", lambda **kw: worker_calls.append(kw))
    monkeypatch.setattr("multiprocessing.active_children", lambda: [])
    monkeypatch.setattr("ouroboros.platform_layer.kill_process_on_port", lambda _port: None)
    monkeypatch.setattr("ouroboros.extension_companion.panic_kill_all", lambda: None)
    monkeypatch.setattr("ouroboros.gateway.host_service.host_service_port", lambda: 8767)

    server._emergency_process_cleanup(port_sweep=False)

    assert service_calls == [((), {"wait": False})]
    assert worker_calls == [{"force": True, "archive_service_logs": False}]


def test_panic_stop_kills_services_without_log_finalization(monkeypatch, tmp_path):
    from ouroboros import server_control

    service_calls = []
    worker_calls = []

    class ExitCalled(RuntimeError):
        pass

    monkeypatch.setattr("ouroboros.tools.shell.kill_all_tracked_subprocesses", lambda: None)
    monkeypatch.setattr("ouroboros.tools.services.kill_all_services", lambda *a, **k: service_calls.append((a, k)))
    monkeypatch.setattr("ouroboros.local_model.get_manager", lambda: SimpleNamespace(stop_server=lambda: None))
    monkeypatch.setattr("supervisor.state.load_state", lambda: {})
    monkeypatch.setattr("supervisor.state.save_state", lambda _state: None)
    monkeypatch.setattr("ouroboros.extension_companion.panic_kill_all", lambda: None)
    monkeypatch.setattr("multiprocessing.active_children", lambda: [])
    monkeypatch.setattr("ouroboros.platform_layer.kill_process_on_port", lambda _port: None)
    monkeypatch.setattr("ouroboros.gateway.host_service.host_service_port", lambda: 8767)
    monkeypatch.setattr(server_control.os, "_exit", lambda code: (_ for _ in ()).throw(ExitCalled(code)))

    try:
        server_control.execute_panic_stop(
            consciousness=SimpleNamespace(stop=lambda: None),
            kill_workers_fn=lambda **kw: worker_calls.append(kw),
            data_dir=tmp_path,
            panic_exit_code=120,
            log=SimpleNamespace(critical=lambda *a, **k: None),
        )
    except ExitCalled:
        pass

    assert service_calls == [((), {"wait": False})]
    assert worker_calls == [{"force": True, "archive_service_logs": False}]

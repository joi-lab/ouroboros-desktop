"""A remover's last outcome is durable and read beside the place it serves: a maintenance job that throws
keeps its state and gains ``last_error`` until it returns normally, the engine place carries the engine's
last retention pass (unknown, never zero, when the engine cannot be asked or keeps no record), and the
Storage projection, the runtime ``storage`` fact and ``query_data(kind="storage")`` read the same facts."""
from __future__ import annotations

import pathlib

import pytest

from ouroboros import maintenance, maintenance_jobs, storage, storage_removers
from ouroboros.context_runtime_facts import storage_fact
from ouroboros.gateways import claudexor as gateway_module
from ouroboros.gateways.claudexor import ClaudexorUnavailable
from ouroboros.gateways.claudexor_maintenance import ClaudexorMaintenanceGateway

HELD = {"outcome": "held", "started_at": "2026-10-27T15:00:00Z", "finished_at": "2026-10-27T15:05:00Z",
        "freed_bytes": 2_000_000, "kept": {"active": 1, "recent": 2, "young": 3, "referenced": 4, "actionable": 0,
                                           "unknown_state": 0},
        "held": {"roots": 357, "native_homes": 2},
        "holds": [{"class": "missing_partition", "count": 357, "example": "/p", "since": "2026-10-18T09:00:00Z"},
                  {"class": "native_home_veto", "count": 2, "example": "/h", "since": "2026-10-20T09:00:00Z"}],
        "errors": {"count": 0, "first": None}}


@pytest.fixture
def data(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    return root


def _engine(data, monkeypatch, answer):
    """A provisioned engine home whose read answers ANSWER (a record, or an exception raised by the read)."""
    descriptor = data / storage.ENGINE_DIR / "daemon" / "control-api.json"
    descriptor.parent.mkdir(parents=True, exist_ok=True)
    descriptor.write_text("{}", encoding="utf-8")
    asked = []

    class Gateway:
        def __init__(self, endpoint):
            asked.append(endpoint)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def maintenance_gc_last(self, *, timeout_sec=None):
            asked.append(timeout_sec)
            if isinstance(answer, Exception):
                raise answer
            return {"last": answer}

    monkeypatch.setattr(gateway_module, "discover_daemon_at", lambda home: ("endpoint", pathlib.Path(home)))
    monkeypatch.setattr(gateway_module, "ClaudexorGateway", Gateway)
    return asked


def test_a_throwing_job_keeps_its_state_gains_last_error_and_a_normal_return_clears_it(data, monkeypatch):
    from ouroboros import store

    outcomes = iter([RuntimeError("disk said no"), store.StoreBusy("held"), None])

    def run(root, should_stop):
        outcome = next(outcomes)
        if outcome is not None:
            raise outcome
        return {"state": "done", "items": 0}

    monkeypatch.setattr(maintenance, "JOBS", {"custody_trash": (lambda *a: True, run)})
    maintenance_jobs.record(data, "custody_trash", state="pending", stuck={"x": [1]})
    assert maintenance.run(data)["status"] == "failed"
    row = maintenance_jobs.job(data, "custody_trash")
    assert row["state"] == "pending" and row["stuck"] == {"x": [1]}, "the state a due rule reads is untouched"
    assert row["last_error"]["class"] == "RuntimeError" and row["last_error"]["at"]
    [fact] = storage_removers.place_removers(data)["work_folders"]
    assert fact["state"] == "pending" and fact["last_error"]["class"] == "RuntimeError"
    assert storage_removers.not_ok({"work_folders": [fact]}) == [
        f"custody_trash: last run threw RuntimeError at {row['last_error']['at']}"]
    assert maintenance.run(data)["status"] == "completed", "contention is no failure"
    assert maintenance_jobs.job(data, "custody_trash")["last_error"]["class"] == "RuntimeError", "nor a normal return"
    maintenance.run(data)
    assert "last_error" not in maintenance_jobs.job(data, "custody_trash"), "the next normal return clears it"


def test_a_first_run_that_throws_leaves_a_never_finished_job_due(monkeypatch, tmp_path):
    from ouroboros import subagent_metrics

    monkeypatch.setattr(subagent_metrics, "_source_stamp", lambda root: ["events.jsonl", 1])
    assert subagent_metrics.job_due(tmp_path, {}, {}, False)
    assert subagent_metrics.job_due(tmp_path, {"last_error": {"class": "OSError", "at": "now"}}, {}, False)


def test_the_engine_place_carries_the_engines_last_pass(data, monkeypatch):
    asked = _engine(data, monkeypatch, HELD)
    [engine] = storage_removers.place_removers(data)["engine"]
    assert engine == {"id": "engine", "state": "held", "at": "2026-10-27T15:05:00Z", "freed_bytes": 2_000_000,
                      "held": 359, "held_since": "2026-10-18T09:00:00Z", "errors": 0, "first_error": None}
    assert asked[-1] == storage_removers.ENGINE_READ_SEC, "the read is bounded"
    assert storage_removers.not_ok({"engine": [engine]}) == [
        "engine retention: last pass held at 2026-10-27T15:05:00Z; holding 359 folders since 2026-10-18T09:00:00Z"]
    _engine(data, monkeypatch, {**HELD, "outcome": "completed", "held": {"roots": 0, "native_homes": 0}, "holds": []})
    assert storage_removers.not_ok(storage_removers.place_removers(data)) == [], "a clean pass is silent"


@pytest.mark.parametrize("answer,state", [
    (None, "none"),
    (ClaudexorUnavailable("maintenance_unavailable", "old engine", status_code=404), "unsupported"),
    (ClaudexorUnavailable("daemon_unreachable", "down"), "unavailable"),
    ({"outcome": "completed"}, "unavailable"),  # a record of another shape is no record
])
def test_an_engine_that_cannot_answer_is_unknown_never_zero(data, monkeypatch, answer, state):
    _engine(data, monkeypatch, answer)
    [engine] = storage_removers.place_removers(data)["engine"]
    assert engine == {"id": "engine", "state": state, "at": None}, "no freed or held count stands for unknown"
    assert storage_removers.not_ok({"engine": [engine]}) == [f"engine retention: {state}"]


def test_no_provisioned_engine_has_no_engine_remover(data):
    assert "engine" not in storage_removers.place_removers(data)


def test_the_gateway_reads_the_record_and_types_an_engine_without_the_route(monkeypatch):
    calls = []

    class Gateway(ClaudexorMaintenanceGateway):
        def __init__(self, answer):
            self.answer = answer

        def _request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("timeout_sec")))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    assert Gateway({"last": HELD}).maintenance_gc_last(timeout_sec=2.0) == {"last": HELD}
    assert calls == [("GET", "/v2/maintenance/gc", 2.0)], "a read, never the POST that starts a pass"
    for status in (404, 405):
        with pytest.raises(ClaudexorUnavailable) as raised:
            Gateway(ClaudexorUnavailable(f"http_{status}", "no route", status_code=status)).maintenance_gc_last()
        assert raised.value.code == "maintenance_unavailable"
    with pytest.raises(ClaudexorUnavailable) as raised:
        Gateway(ClaudexorUnavailable("daemon_unreachable", "down")).maintenance_gc_last()
    assert raised.value.code == "daemon_unreachable", "a down daemon stays unavailable, not unsupported"
    with pytest.raises(ClaudexorUnavailable):
        Gateway({"something": 1}).maintenance_gc_last()


def test_the_runtime_fact_is_silent_when_all_is_ok_and_present_before_the_first_count(data, monkeypatch):
    assert storage_fact(data) is None, "nothing counted, every remover clean"
    maintenance_jobs.record(data, "runtime_retirement", state="done", last_error={"class": "OSError", "at": "T"})
    assert storage_fact(data) == {"removers": "runtime_retirement: last run threw OSError at T"}
    _engine(data, monkeypatch, ClaudexorUnavailable("daemon_unreachable", "down"))
    assert storage_fact(data)["removers"] == "engine retention: unavailable; runtime_retirement: last run threw OSError at T"
    (data / "observability").mkdir()
    table = [{"id": "model_calls", "group": "records", "roots": [(str(data), ("observability",))]}]
    storage.save_count(data, storage.measure(data, places=table, sleep=lambda _s: None))
    fact = storage_fact(data)
    assert "counted_at" in fact and fact["removers"].startswith("engine retention: unavailable"), "beside a count too"


def test_the_projection_and_query_data_read_the_same_removers(data, monkeypatch):
    _engine(data, monkeypatch, HELD)
    maintenance_jobs.record(data, "record_compression", state="done", compressed=12, saved_bytes=4096)
    projected = storage.count.projection(data)["removers"]
    assert projected == storage_removers.place_removers(data)
    assert set(projected) <= {place_id for place_id, _group, _spec in storage.PLACES}
    [_retirement, compression, _trash] = projected["app_state"]
    assert compression == {"id": "record_compression", "state": "done", "at": compression["at"],
                           "compressed": 12, "saved_bytes": 4096}

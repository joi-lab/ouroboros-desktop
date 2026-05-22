"""Tests for the ClawHub marketplace HTTP API adapter layer."""

from __future__ import annotations

import asyncio
import json

from ouroboros.gateway import marketplace as marketplace_api
from ouroboros.marketplace.clawhub import ClawHubSkillSummary
from ouroboros.marketplace.ouroboroshub import HubInstallResult, HubSkillSummary


class _Request:
    def __init__(self, query_params):
        self.query_params = query_params


class _BodyRequest:
    def __init__(self, body=None, path_params=None, query_params=None):
        self._body = body if body is not None else {}
        self.path_params = path_params or {}
        self.query_params = query_params or {}

    async def json(self):
        return self._body


def _json_response_payload(response):
    return json.loads(response.body.decode("utf-8"))


def _stub_marketplace_roots(monkeypatch, tmp_path):
    monkeypatch.setattr(marketplace_api, "_request_drive_root", lambda _req: tmp_path)
    monkeypatch.setattr(marketplace_api, "_request_repo_dir", lambda _req: tmp_path / "repo")


def _run_lifecycle_inline(monkeypatch):
    async def _fake_lifecycle_job(**kwargs):
        return await kwargs["runner"]()

    async def _fake_blocking(func, *args, **kwargs):
        kwargs.pop("log_label", None)
        return func(*args, **kwargs)

    monkeypatch.setattr(marketplace_api, "run_lifecycle_job", _fake_lifecycle_job)
    monkeypatch.setattr(marketplace_api, "run_blocking_preserving_cancellation", _fake_blocking)


def test_marketplace_api_search_drops_params_with_query(monkeypatch):
    captured = {}

    def _fake_search(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return {
            "results": [],
            "next_cursor": "",
            "path": "search",
            "attempts": [],
        }

    monkeypatch.setattr(marketplace_api, "_registry_search", _fake_search)
    response = asyncio.run(
        marketplace_api.api_marketplace_search(
            _Request(
                {
                    "q": "deep research",
                    "limit": "7",
                    "offset": "50",
                    "cursor": "abc",
                    "official": "1",
                }
            )
        )
    )

    assert response.status_code == 200
    assert captured["query"] == "deep research"
    assert captured["kwargs"]["limit"] == 7
    assert "offset" not in captured["kwargs"]
    assert captured["kwargs"]["cursor"] is None
    assert captured["kwargs"]["official_only"] is False
    assert captured["kwargs"]["timeout_sec"] == 15
    assert "enrich_search_results" not in captured["kwargs"]
    payload = _json_response_payload(response)
    assert payload["official"] is True
    assert payload["offset"] == 0
    assert payload["cursor"] is None
    assert payload["registry_path"] == "search"


def test_marketplace_api_search_filters_official_after_enrichment(monkeypatch):
    def _fake_search(query, **_kwargs):
        return {
            "results": [
                ClawHubSkillSummary(slug="official", badges={"official": True}),
                ClawHubSkillSummary(slug="community", badges={}),
            ],
            "next_cursor": "",
            "path": "search",
            "attempts": [],
        }

    monkeypatch.setattr(marketplace_api, "_registry_search", _fake_search)
    response = asyncio.run(
        marketplace_api.api_marketplace_search(
            _Request({"q": "deep research", "official": "1"})
        )
    )

    assert response.status_code == 200
    payload = _json_response_payload(response)
    assert payload["official"] is True
    assert [r["slug"] for r in payload["results"]] == ["official"]


def test_marketplace_api_browse_keeps_official_and_cursor(monkeypatch):
    captured = {}

    def _fake_search(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return {
            "results": [],
            "next_cursor": "next",
            "path": "packages",
            "attempts": [],
        }

    monkeypatch.setattr(marketplace_api, "_registry_search", _fake_search)
    response = asyncio.run(
        marketplace_api.api_marketplace_search(
            _Request({"limit": "5", "cursor": "abc", "official": "1"})
        )
    )

    assert response.status_code == 200
    assert captured["query"] == ""
    assert captured["kwargs"]["limit"] == 5
    assert captured["kwargs"]["cursor"] == "abc"
    assert captured["kwargs"]["official_only"] is True
    assert captured["kwargs"]["timeout_sec"] == 5
    payload = _json_response_payload(response)
    assert payload["official"] is True
    assert payload["cursor"] == "abc"
    assert payload["next_cursor"] == "next"


def test_ouroboroshub_install_response_shape_after_review_and_deps(monkeypatch, tmp_path):
    _stub_marketplace_roots(monkeypatch, tmp_path)
    _run_lifecycle_inline(monkeypatch)

    target_dir = tmp_path / "skills" / "ouroboroshub" / "demo"
    target_dir.mkdir(parents=True)
    (target_dir / ".ouroboroshub.json").write_text("{}", encoding="utf-8")
    summary = HubSkillSummary(slug="demo", name="Demo", version="1.0.0")
    provenance = {"source": "ouroboroshub", "slug": "demo"}

    def _fake_install(slug, *, overwrite=False):
        assert slug == "demo"
        assert overwrite is False
        return HubInstallResult(
            ok=True,
            sanitized_name="demo",
            target_dir=target_dir,
            summary=summary,
            provenance=provenance,
        )

    monkeypatch.setattr(marketplace_api.ouroboroshub, "install", _fake_install)
    monkeypatch.setattr(
        marketplace_api,
        "_run_skill_review",
        lambda _drive, _repo, name: ("clean", [{"message": "ok"}], ""),
    )
    monkeypatch.setattr(
        marketplace_api,
        "_reconcile_deps_after_review",
        lambda _drive, name: ("installed", ""),
    )

    response = asyncio.run(
        marketplace_api.api_ouroboroshub_install(
            _BodyRequest({"slug": "demo", "auto_review": True})
        )
    )

    assert response.status_code == 200
    assert _json_response_payload(response) == {
        "ok": True,
        "sanitized_name": "demo",
        "error": "",
        "provenance": provenance,
        "summary": summary.to_dict(),
        "target_dir": str(target_dir),
        "review_status": "clean",
        "review_findings": [{"message": "ok"}],
        "review_error": "",
        "deps_status": "installed",
        "deps_error": "",
    }


def test_clawhub_uninstall_clears_deps_state(tmp_path):
    from ouroboros.marketplace.install import uninstall_skill

    target = tmp_path / "skills" / "clawhub" / "demo"
    target.mkdir(parents=True)
    (target / ".clawhub.json").write_text("{}", encoding="utf-8")
    deps = tmp_path / "state" / "skills" / "demo" / "deps.json"
    deps.parent.mkdir(parents=True)
    deps.write_text(json.dumps({"status": "installed", "specs_hash": "abc"}), encoding="utf-8")

    result = uninstall_skill(tmp_path, sanitized_name="demo")

    assert result.ok
    assert not deps.exists()


def test_ouroboroshub_update_rejects_missing_install(monkeypatch, tmp_path):
    _stub_marketplace_roots(monkeypatch, tmp_path)
    _run_lifecycle_inline(monkeypatch)
    called = {"install": 0}

    monkeypatch.setattr(
        marketplace_api.ouroboroshub,
        "install",
        lambda *_args, **_kwargs: called.__setitem__("install", called["install"] + 1),
    )

    response = asyncio.run(
        marketplace_api.api_ouroboroshub_update(
            _BodyRequest(path_params={"name": "demo"})
        )
    )

    assert response.status_code == 400
    assert called["install"] == 0
    assert _json_response_payload(response) == {
        "ok": False,
        "sanitized_name": "demo",
        "error": "demo is not installed",
        "provenance": {},
        "summary": None,
    }


def test_ouroboroshub_update_rejects_unmarked_payload(monkeypatch, tmp_path):
    _stub_marketplace_roots(monkeypatch, tmp_path)
    _run_lifecycle_inline(monkeypatch)
    target_dir = tmp_path / "skills" / "ouroboroshub" / "demo"
    target_dir.mkdir(parents=True)
    called = {"install": 0}

    monkeypatch.setattr(
        marketplace_api.ouroboroshub,
        "install",
        lambda *_args, **_kwargs: called.__setitem__("install", called["install"] + 1),
    )

    response = asyncio.run(
        marketplace_api.api_ouroboroshub_update(
            _BodyRequest(path_params={"name": "demo"})
        )
    )

    assert response.status_code == 400
    assert called["install"] == 0
    assert _json_response_payload(response) == {
        "ok": False,
        "sanitized_name": "demo",
        "error": "missing OuroborosHub provenance marker",
        "provenance": {},
        "summary": None,
        "target_dir": str(target_dir),
    }


def test_ouroboroshub_update_rejects_wrong_provenance_marker(monkeypatch, tmp_path):
    _stub_marketplace_roots(monkeypatch, tmp_path)
    _run_lifecycle_inline(monkeypatch)
    target_dir = tmp_path / "skills" / "ouroboroshub" / "demo"
    target_dir.mkdir(parents=True)
    (target_dir / ".ouroboroshub.json").write_text(
        json.dumps({"source": "clawhub", "slug": "demo", "sanitized_name": "demo"}),
        encoding="utf-8",
    )
    called = {"install": 0}

    monkeypatch.setattr(
        marketplace_api.ouroboroshub,
        "install",
        lambda *_args, **_kwargs: called.__setitem__("install", called["install"] + 1),
    )

    response = asyncio.run(
        marketplace_api.api_ouroboroshub_update(
            _BodyRequest(path_params={"name": "demo"})
        )
    )

    assert response.status_code == 400
    assert called["install"] == 0
    assert _json_response_payload(response) == {
        "ok": False,
        "sanitized_name": "demo",
        "error": "invalid OuroborosHub provenance marker",
        "provenance": {},
        "summary": None,
        "target_dir": str(target_dir),
    }


def test_ouroboroshub_update_response_shape_on_dependency_failure(monkeypatch, tmp_path):
    _stub_marketplace_roots(monkeypatch, tmp_path)
    _run_lifecycle_inline(monkeypatch)

    target_dir = tmp_path / "skills" / "ouroboroshub" / "demo"
    target_dir.mkdir(parents=True)
    (target_dir / ".ouroboroshub.json").write_text(
        json.dumps({
            "schema_version": 1,
            "source": "ouroboroshub",
            "slug": "demo",
            "sanitized_name": "demo",
        }),
        encoding="utf-8",
    )
    summary = HubSkillSummary(slug="demo", name="Demo", version="1.0.0")
    provenance = {"source": "ouroboroshub", "slug": "demo"}

    monkeypatch.setattr(
        "ouroboros.extension_loader.is_extension_live",
        lambda _name, _drive: False,
    )
    monkeypatch.setattr("ouroboros.extension_loader.unload_extension", lambda _name: None)

    def _fake_install(slug, *, overwrite=False):
        assert slug == "demo"
        assert overwrite is True
        return HubInstallResult(
            ok=True,
            sanitized_name="demo",
            target_dir=target_dir,
            summary=summary,
            provenance=provenance,
        )

    monkeypatch.setattr(
        marketplace_api.ouroboroshub,
        "install",
        _fake_install,
    )
    monkeypatch.setattr(
        marketplace_api,
        "_run_skill_review",
        lambda _drive, _repo, name: ("clean", [{"message": "ok"}], ""),
    )
    monkeypatch.setattr(
        marketplace_api,
        "_reconcile_deps_after_review",
        lambda _drive, name: ("failed", "dependency boom"),
    )

    response = asyncio.run(
        marketplace_api.api_ouroboroshub_update(
            _BodyRequest(path_params={"name": "demo"})
        )
    )

    assert response.status_code == 400
    assert _json_response_payload(response) == {
        "ok": False,
        "sanitized_name": "demo",
        "error": "dependency boom",
        "provenance": provenance,
        "summary": summary.to_dict(),
        "target_dir": str(target_dir),
        "review_status": "clean",
        "review_findings": [{"message": "ok"}],
        "review_error": "",
        "deps_status": "failed",
        "deps_error": "dependency boom",
    }

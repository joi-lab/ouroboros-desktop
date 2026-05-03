"""Tests for ``ouroboros.credentials_sidecar``.

The sidecar pattern keeps service tokens out of ``settings.json`` (which
ships in support bundles, paste-into-bug-report flows, etc.). Tokens
live in a per-service gitignored JSON under DATA_DIR.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.credentials_sidecar import (
    credentials_present,
    fingerprint_secret,
    load_sidecar,
    redact_secret,
    sidecar_path,
)


def _write_sidecar(tmp_path, name, payload):
    p = tmp_path / f"{name}-credentials.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_load_returns_none_when_missing(tmp_path):
    assert load_sidecar("nonesuch", drive_root=tmp_path) is None
    assert credentials_present("nonesuch", drive_root=tmp_path) is False


def test_load_returns_dict_when_present(tmp_path):
    _write_sidecar(tmp_path, "openbotcity", {
        "service": "openbotcity",
        "jwt": "eyJabc",
        "bot_id": "bid-1",
    })
    data = load_sidecar("openbotcity", drive_root=tmp_path)
    assert data is not None
    assert data["jwt"] == "eyJabc"
    assert data["bot_id"] == "bid-1"
    assert data["_sidecar_path"].endswith("openbotcity-credentials.json")


def test_load_returns_none_for_malformed_json(tmp_path):
    p = tmp_path / "broken-credentials.json"
    p.write_text("{not valid json", encoding="utf-8")
    assert load_sidecar("broken", drive_root=tmp_path) is None


def test_load_returns_none_when_root_is_not_object(tmp_path):
    p = tmp_path / "list-credentials.json"
    p.write_text('["a", "b"]', encoding="utf-8")
    assert load_sidecar("list", drive_root=tmp_path) is None


def test_sidecar_path_normalizes_name(tmp_path):
    p = sidecar_path("OpenBotCity", drive_root=tmp_path)
    assert p.name == "openbotcity-credentials.json"
    p2 = sidecar_path("My Service!*", drive_root=tmp_path)
    # non-alphanumeric (other than - and _) is stripped
    assert p2.name == "myservice-credentials.json"


def test_sidecar_path_honors_explicit_env_override(tmp_path, monkeypatch):
    explicit = tmp_path / "vault" / "obc.json"
    monkeypatch.setenv("OPENBOTCITY_CREDENTIALS_PATH", str(explicit))
    p = sidecar_path("openbotcity", drive_root=tmp_path)
    assert p == explicit


def test_fingerprint_is_stable_and_distinguishing():
    a = fingerprint_secret("eyJabc")
    b = fingerprint_secret("eyJabc")
    c = fingerprint_secret("eyJxyz")
    assert a == b
    assert a != c
    # 16 hex chars (truncated SHA256).
    assert len(a) == 16
    assert all(ch in "0123456789abcdef" for ch in a)


def test_fingerprint_handles_empty_input():
    # Doesn't raise; produces a stable digest of empty string.
    assert fingerprint_secret("") == fingerprint_secret("")
    assert fingerprint_secret(None) == fingerprint_secret("")  # type: ignore[arg-type]


def test_redact_secret_short_input_full_mask():
    # Tokens shorter than 2*keep get fully masked rather than partially shown.
    assert redact_secret("abc", keep=6) == "***"


def test_redact_secret_long_input_keeps_head_tail():
    out = redact_secret("eyJabcdefgh1234567890XYZ", keep=4)
    assert out.startswith("eyJa")
    assert "XYZ" in out
    assert "len=" in out


def test_load_includes_sidecar_mtime(tmp_path):
    p = _write_sidecar(tmp_path, "svc", {"jwt": "x"})
    data = load_sidecar("svc", drive_root=tmp_path)
    assert data is not None
    assert "_sidecar_mtime" in data
    assert data["_sidecar_mtime"] == pytest.approx(p.stat().st_mtime, abs=1.0)

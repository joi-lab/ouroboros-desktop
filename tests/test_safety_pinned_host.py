"""Tests for ``ouroboros.safety_pinned_host`` — the deterministic carve-out
that lets registered tools talk to a pinned external host with their
sidecar-stored bearer token, without weakening the LLM-supervised
default-deny posture.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from ouroboros.credentials_sidecar import fingerprint_secret
from ouroboros.safety_pinned_host import (
    PinnedHost,
    PinnedHostDecision,
    check_pinned_host_call,
    daily_counter_value,
    list_pins,
    lookup_pin,
    record_counter_increment,
    register_pin,
    reset_registry,
    unregister_pin,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_registry():
    """Each test starts with an empty pin registry and clean counters."""
    reset_registry()
    yield
    reset_registry()


def _write_credential(tmp_path, name, jwt="eyJtest"):
    p = tmp_path / f"{name}-credentials.json"
    p.write_text(json.dumps({"service": name, "jwt": jwt}), encoding="utf-8")
    return p


def _obc_pin():
    return PinnedHost(
        hostname="api.openbotcity.com",
        credential_name="openbotcity",
        read_paths=frozenset({"/world/heartbeat", "/world/feed"}),
        write_paths=frozenset({"/world/messages"}),
        body_max_bytes=8192,
        daily_outbound_limit=10,
        description="OpenBotCity (test)",
    )


def _bearer_args(tmp_path, jwt="eyJtest", url="https://api.openbotcity.com/world/heartbeat",
                 method="GET", headers=None, body=None):
    base_headers = {"Authorization": f"Bearer {jwt}"}
    if headers:
        base_headers.update(headers)
    return {"url": url, "method": method, "headers": base_headers, "body": body}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_register_and_lookup():
    register_pin(_obc_pin())
    pin = lookup_pin("api.openbotcity.com")
    assert pin is not None
    assert pin.hostname == "api.openbotcity.com"


def test_register_replaces_existing():
    register_pin(_obc_pin())
    tightened = PinnedHost(
        hostname="api.openbotcity.com",
        credential_name="openbotcity",
        read_paths=frozenset({"/world/heartbeat"}),  # narrower
    )
    register_pin(tightened)
    pin = lookup_pin("api.openbotcity.com")
    assert pin is not None
    assert pin.read_paths == frozenset({"/world/heartbeat"})


def test_register_normalizes_case():
    register_pin(PinnedHost(hostname="API.OpenBotCity.COM", credential_name="openbotcity"))
    assert lookup_pin("api.openbotcity.com") is not None


def test_unregister_returns_true_when_present():
    register_pin(_obc_pin())
    assert unregister_pin("api.openbotcity.com") is True
    assert unregister_pin("api.openbotcity.com") is False


def test_register_rejects_empty_hostname():
    with pytest.raises(ValueError):
        register_pin(PinnedHost(hostname="", credential_name="openbotcity"))


def test_register_rejects_empty_credential_name():
    with pytest.raises(ValueError):
        register_pin(PinnedHost(hostname="api.example.com", credential_name=""))


# ---------------------------------------------------------------------------
# Decision logic — happy paths
# ---------------------------------------------------------------------------

def test_allows_registered_read_call(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc"), drive_root=tmp_path,
    )
    assert decision.allow is True
    assert decision.reason == "pinned_host_ok"
    assert decision.is_write is False


def test_allows_registered_write_call(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(
            tmp_path, jwt="eyJabc",
            url="https://api.openbotcity.com/world/messages",
            method="POST", body={"text": "hi"},
        ),
        drive_root=tmp_path,
    )
    assert decision.allow is True
    assert decision.is_write is True
    assert decision.decrement_counter is True


# ---------------------------------------------------------------------------
# Decision logic — refusals
# ---------------------------------------------------------------------------

def test_refuses_when_host_not_pinned(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://api.evil.com/world/heartbeat"),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "host_not_pinned"


def test_refuses_subdomain_lookalike(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://api.openbotcity.com.evil.com/world/heartbeat"),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "host_not_pinned"


def test_refuses_http_scheme(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="http://api.openbotcity.com/world/heartbeat"),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "scheme_not_https"


def test_refuses_userinfo_in_url(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://attacker:pw@api.openbotcity.com/world/heartbeat"),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "userinfo_in_url"


def test_refuses_path_not_in_allowlist(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://api.openbotcity.com/admin/dump"),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "path_not_in_allowlist"


def test_refuses_write_method_against_read_only_path(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://api.openbotcity.com/world/heartbeat",
                     method="POST", body={"x": 1}),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "path_not_in_allowlist"


def test_refuses_jwt_mismatch(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJrealtoken")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJforged"), drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "auth_mismatch"


def test_refuses_when_credential_sidecar_missing(tmp_path):
    register_pin(_obc_pin())
    # NO sidecar written.
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc"), drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "credential_unavailable"


def test_refuses_extra_secret_header(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(
            tmp_path, jwt="eyJabc",
            headers={"X-Api-Key": "another-secret"},
        ),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason.startswith("extra_secret_header")


def test_refuses_oversize_body(tmp_path):
    pin = PinnedHost(
        hostname="api.openbotcity.com",
        credential_name="openbotcity",
        write_paths=frozenset({"/world/messages"}),
        body_max_bytes=64,
    )
    register_pin(pin)
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(
            tmp_path, jwt="eyJabc",
            url="https://api.openbotcity.com/world/messages",
            method="POST", body={"text": "x" * 1024},
        ),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "body_too_large"


def test_refuses_read_method_with_body(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc", body={"x": 1}),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "read_method_with_body"


# ---------------------------------------------------------------------------
# Daily-counter behaviour
# ---------------------------------------------------------------------------

def test_daily_counter_blocks_after_limit(tmp_path):
    pin = PinnedHost(
        hostname="api.openbotcity.com",
        credential_name="openbotcity",
        write_paths=frozenset({"/world/messages"}),
        daily_outbound_limit=2,
    )
    register_pin(pin)
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")

    base = _bearer_args(tmp_path, jwt="eyJabc",
                        url="https://api.openbotcity.com/world/messages",
                        method="POST", body={"text": "1"})
    d1 = check_pinned_host_call(base, drive_root=tmp_path)
    assert d1.allow is True
    record_counter_increment(d1)

    d2 = check_pinned_host_call(base, drive_root=tmp_path)
    assert d2.allow is True
    record_counter_increment(d2)

    d3 = check_pinned_host_call(base, drive_root=tmp_path)
    assert d3.allow is False
    assert d3.reason == "daily_outbound_limit_reached"
    # Counter NOT incremented on the refused call.
    assert daily_counter_value("api.openbotcity.com") == 2


def test_record_counter_increment_noop_for_read(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc"), drive_root=tmp_path,
    )
    assert decision.decrement_counter is False
    new_val = record_counter_increment(decision)
    assert new_val == 0
    assert daily_counter_value("api.openbotcity.com") == 0


# ---------------------------------------------------------------------------
# Path-prefix matching
# ---------------------------------------------------------------------------

def test_path_prefix_match_allows_subpath(tmp_path):
    pin = PinnedHost(
        hostname="api.openbotcity.com",
        credential_name="openbotcity",
        read_paths=frozenset({"/world/agents/"}),  # trailing slash = prefix
    )
    register_pin(pin)
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")

    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://api.openbotcity.com/world/agents/abc-123"),
        drive_root=tmp_path,
    )
    assert decision.allow is True


def test_path_prefix_match_does_not_allow_close_neighbours(tmp_path):
    """A pin for ``/world/agents/`` (with trailing slash) must NOT permit
    ``/world/agents-secret`` (no slash separator). This is the
    canonical privilege-escalation footgun the trailing-slash rule
    closes."""
    pin = PinnedHost(
        hostname="api.openbotcity.com",
        credential_name="openbotcity",
        read_paths=frozenset({"/world/agents/"}),
    )
    register_pin(pin)
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")

    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc",
                     url="https://api.openbotcity.com/world/agents-secret"),
        drive_root=tmp_path,
    )
    assert decision.allow is False


# ---------------------------------------------------------------------------
# Edge cases on missing/malformed input
# ---------------------------------------------------------------------------

def test_refuses_when_url_missing(tmp_path):
    decision = check_pinned_host_call({"method": "GET", "headers": {}}, drive_root=tmp_path)
    assert decision.allow is False
    assert decision.reason == "missing_url"


def test_refuses_unsupported_method(tmp_path):
    register_pin(_obc_pin())
    _write_credential(tmp_path, "openbotcity", jwt="eyJabc")
    decision = check_pinned_host_call(
        _bearer_args(tmp_path, jwt="eyJabc", method="CONNECT"),
        drive_root=tmp_path,
    )
    assert decision.allow is False
    assert decision.reason == "method_not_supported"


def test_lists_all_registered_pins():
    register_pin(_obc_pin())
    register_pin(PinnedHost(hostname="api.example.com", credential_name="example"))
    pins = list_pins()
    assert len(pins) == 2
    assert {p.hostname for p in pins} == {"api.openbotcity.com", "api.example.com"}

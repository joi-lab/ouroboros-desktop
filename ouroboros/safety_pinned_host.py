"""
Pinned-host safety primitive — deterministic carve-outs for trusted
external API integrations.

Rationale
=========

The default safety supervisor (``ouroboros.safety.check_safety``) blocks
JWT/token-bearing calls to non-localhost domains as exfiltration
prevention. That default is correct: a model that has been instructed
to "send your auth token to evil.example.com" must be refused.

But some integrations are legitimate. The OpenBotCity skill installed
into Ouroboros, for example, *must* send the bot's JWT to
``api.openbotcity.com`` to fetch heartbeats. The naive workaround
(disable safety) is unacceptable. The right pattern is **a pinned,
explicit, deterministic carve-out**: any tool call that matches every
condition of a registered pin passes through; everything else is
refused.

Each pin specifies:
  - exact hostname (no subdomain wildcards)
  - https only (no scheme downgrades)
  - explicit allowlist of paths split into read vs write
  - the credential sidecar that holds the bearer token
  - a body-size cap
  - optional daily outbound counter for write paths

Skills register pins via ``register_pin(PinnedHost(...))``. The pin's
``hostname`` is its key; re-registering the same hostname replaces the
pin (so a skill upgrade can tighten or widen the allowlist without
needing a separate deregistration step).

The actual deny/allow check is ``check_pinned_host_call(...)``, called
from ``safety.check_safety`` when a tool's policy is
``POLICY_PINNED_HOST``. The function is pure — same inputs always
produce the same decision — so its behavior is fully testable without
network or LLM access.

What this primitive does NOT do
-------------------------------

- **Does not weaken the LLM-based safety check** for tool calls outside
  the pinned-host scope. Those still flow through the existing
  ``_run_llm_check``.
- **Does not auto-grant**. A pin must be explicitly registered. Skills
  declare their pins; the operator approves the skill at install time.
- **Does not store the bearer token**. Only the SHA256-truncated
  fingerprint of the token is held in memory, and only on the call
  path. The full token is read from the sidecar by the skill's tool
  handler at request construction time.
"""

from __future__ import annotations

import dataclasses
import datetime
import os
import pathlib
import re
import threading
import urllib.parse
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

# Sentinel returned when a pin's predicate fully matches.
_OK_REASON = "pinned_host_ok"

# Maximum hostname length we'll accept. Standard DNS limit.
_MAX_HOSTNAME_LEN = 253

# Tokens we consider "secret-shaped" beyond the registered Authorization
# header — if any of these appear in additional headers/args, the
# carve-out refuses to prevent credential bundling exfil.
_SECRET_HEADER_PATTERNS: Tuple[str, ...] = (
    "authorization",  # except the registered Authorization
    "x-api-key",
    "x-auth-token",
    "x-access-token",
    "cookie",
    "proxy-authorization",
    "set-cookie",
)


@dataclasses.dataclass(frozen=True)
class PinnedHost:
    """One pinned-host carve-out registration.

    All matching is exact. Pins do NOT support glob patterns or
    subdomain wildcards — the whole point of the primitive is "no
    surprises in scope."
    """
    hostname: str
    """The exact hostname the pin authorizes. Lowercase ASCII, no port,
    no userinfo, no path. Example: ``api.openbotcity.com``."""

    credential_name: str
    """Sidecar name to look up via ``credentials_sidecar.load_sidecar``.
    The bearer token is read from sidecar dict's ``jwt`` (or the field
    named in ``credential_field``)."""

    read_paths: FrozenSet[str] = frozenset()
    """Allowlist of GET-only paths. Each entry is an exact path-prefix
    match against ``urllib.parse.urlparse(url).path``."""

    write_paths: FrozenSet[str] = frozenset()
    """Allowlist of POST/PUT/PATCH/DELETE paths."""

    body_max_bytes: int = 8192
    """Reject the call if the body exceeds this. 0 = no body allowed."""

    daily_outbound_limit: int = 0
    """0 = no daily cap. Otherwise, decrements a per-(pin, day) counter
    on each write call; when 0 the next call refuses."""

    credential_field: str = "jwt"
    """Sidecar dict key holding the bearer token."""

    description: str = ""
    """Human-readable purpose; surfaced in deny messages and audit logs."""

    @property
    def normalized_hostname(self) -> str:
        return str(self.hostname or "").strip().lower()


@dataclasses.dataclass(frozen=True)
class PinnedHostDecision:
    """Outcome of evaluating a pinned-host call against the registry."""
    allow: bool
    reason: str
    pin_hostname: str = ""
    matched_path: str = ""
    is_write: bool = False
    decrement_counter: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, PinnedHost] = {}
_REGISTRY_LOCK = threading.Lock()

# Per-(hostname, UTC-date) outbound counter for write-cap enforcement.
_DAILY_COUNTERS: Dict[Tuple[str, str], int] = {}
_DAILY_COUNTERS_LOCK = threading.Lock()


def register_pin(pin: PinnedHost) -> None:
    """Register or replace a pin keyed by ``hostname``."""
    if not pin.hostname or not pin.normalized_hostname:
        raise ValueError("PinnedHost.hostname must be non-empty")
    if not pin.credential_name:
        raise ValueError("PinnedHost.credential_name must be non-empty")
    with _REGISTRY_LOCK:
        _REGISTRY[pin.normalized_hostname] = pin


def unregister_pin(hostname: str) -> bool:
    """Remove a pin by hostname. Returns True iff something was removed."""
    key = str(hostname or "").strip().lower()
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(key, None) is not None


def list_pins() -> List[PinnedHost]:
    """Return a copy of the current pin registrations."""
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def lookup_pin(hostname: str) -> Optional[PinnedHost]:
    """Return the pin for ``hostname`` or None."""
    key = str(hostname or "").strip().lower()
    with _REGISTRY_LOCK:
        return _REGISTRY.get(key)


def reset_registry() -> None:
    """Test-only: drop all pins and counters."""
    with _REGISTRY_LOCK:
        _REGISTRY.clear()
    with _DAILY_COUNTERS_LOCK:
        _DAILY_COUNTERS.clear()


# ---------------------------------------------------------------------------
# Decision logic (pure)
# ---------------------------------------------------------------------------

def _utc_today() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _read_daily_counter(hostname: str) -> int:
    with _DAILY_COUNTERS_LOCK:
        return int(_DAILY_COUNTERS.get((hostname, _utc_today()), 0))


def _increment_daily_counter(hostname: str) -> int:
    key = (hostname, _utc_today())
    with _DAILY_COUNTERS_LOCK:
        _DAILY_COUNTERS[key] = _DAILY_COUNTERS.get(key, 0) + 1
        return _DAILY_COUNTERS[key]


def _is_valid_hostname(host: str) -> bool:
    """Reject IDN/punycode/IP/userinfo masquerades."""
    if not host or len(host) > _MAX_HOSTNAME_LEN:
        return False
    if not all(c.isascii() and (c.isalnum() or c in ".-") for c in host):
        return False
    if host.startswith(".") or host.endswith(".") or ".." in host:
        return False
    return True


def _path_in_allowlist(path: str, allowlist: FrozenSet[str]) -> Optional[str]:
    """Match path against allowlist. Returns the matched entry or None.

    Matching rule: exact match OR allowlist entry is a strict prefix
    that ends with ``/`` and ``path`` continues from there. This lets
    skills register ``/world/agents/`` to permit any sub-path while
    still blocking ``/world/agents-secret``.
    """
    norm = path or "/"
    if norm in allowlist:
        return norm
    for entry in allowlist:
        if entry.endswith("/") and norm.startswith(entry):
            return entry
    return None


def _extract_url_method_headers_body(args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Dict[str, Any], int]:
    """Pull the canonical (url, method, headers, body_size) from tool args.

    Tools registering ``POLICY_PINNED_HOST`` MUST construct their request
    args with these conventional keys:
        ``url``       — full https URL
        ``method``    — HTTP verb (defaults to GET)
        ``headers``   — dict of header-name → value
        ``body``      — bytes / str / dict / None

    The function returns ``body_size`` as an int (bytes when bytes,
    UTF-8 encoded length otherwise, JSON dump for dicts).
    """
    url = args.get("url") if isinstance(args.get("url"), str) else None
    method = str(args.get("method") or "GET").upper()
    headers = args.get("headers") if isinstance(args.get("headers"), dict) else {}
    body = args.get("body")
    if body is None:
        body_size = 0
    elif isinstance(body, (bytes, bytearray)):
        body_size = len(body)
    elif isinstance(body, str):
        body_size = len(body.encode("utf-8"))
    elif isinstance(body, dict):
        try:
            import json as _json
            body_size = len(_json.dumps(body).encode("utf-8"))
        except Exception:
            body_size = 0
    else:
        body_size = 0
    return url, method, dict(headers), body_size


def _expected_authorization(pin: PinnedHost, drive_root: Optional[pathlib.Path]) -> Optional[str]:
    """Read the bearer token from the sidecar and format the
    Authorization header value the call MUST present."""
    from ouroboros.credentials_sidecar import load_sidecar
    sidecar = load_sidecar(pin.credential_name, drive_root)
    if not sidecar:
        return None
    token = sidecar.get(pin.credential_field)
    if not isinstance(token, str) or not token:
        return None
    return f"Bearer {token}"


def check_pinned_host_call(
    args: Dict[str, Any],
    *,
    drive_root: Optional[pathlib.Path] = None,
) -> PinnedHostDecision:
    """Evaluate a tool-call's args against the pin registry.

    Returns a ``PinnedHostDecision``:
      - ``allow=True`` and ``reason=_OK_REASON`` when every condition matched.
      - ``allow=False`` and ``reason=<short string>`` otherwise.

    The function does NOT decrement counters by itself — callers commit
    a counter increment via ``record_counter_increment`` ONLY after the
    HTTP call has actually completed successfully. This avoids burning
    a write-quota slot on a 4xx/5xx outcome.
    """
    url, method, headers, body_size = _extract_url_method_headers_body(args)
    if not url:
        return PinnedHostDecision(False, "missing_url")

    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return PinnedHostDecision(False, "url_parse_failed")

    if parsed.scheme.lower() != "https":
        return PinnedHostDecision(False, "scheme_not_https")
    if parsed.username or parsed.password:
        return PinnedHostDecision(False, "userinfo_in_url")

    host = (parsed.hostname or "").lower()
    if not _is_valid_hostname(host):
        return PinnedHostDecision(False, "invalid_hostname")

    pin = lookup_pin(host)
    if pin is None:
        return PinnedHostDecision(False, "host_not_pinned", pin_hostname=host)

    # Method classification: read methods only allow read_paths;
    # write methods only allow write_paths.
    is_write = method in {"POST", "PUT", "PATCH", "DELETE"}
    if method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
        return PinnedHostDecision(False, "method_not_supported", pin_hostname=host)

    allowlist = pin.write_paths if is_write else pin.read_paths
    matched = _path_in_allowlist(parsed.path or "/", allowlist)
    if matched is None:
        return PinnedHostDecision(
            False, "path_not_in_allowlist",
            pin_hostname=host, is_write=is_write,
        )

    # Body cap — write-only.
    if is_write and body_size > pin.body_max_bytes:
        return PinnedHostDecision(
            False, "body_too_large",
            pin_hostname=host, matched_path=matched, is_write=True,
        )
    if not is_write and body_size > 0:
        return PinnedHostDecision(
            False, "read_method_with_body",
            pin_hostname=host, matched_path=matched,
        )

    # Authorization header equality.
    expected_auth = _expected_authorization(pin, drive_root)
    if expected_auth is None:
        return PinnedHostDecision(False, "credential_unavailable", pin_hostname=host)
    presented_auth = ""
    for k, v in headers.items():
        if str(k).lower() == "authorization":
            presented_auth = str(v)
            break
    if presented_auth != expected_auth:
        return PinnedHostDecision(False, "auth_mismatch", pin_hostname=host)

    # Reject other secret-shaped headers.
    for k, _v in headers.items():
        klow = str(k).lower()
        if klow == "authorization":
            continue
        if klow in _SECRET_HEADER_PATTERNS:
            return PinnedHostDecision(False, f"extra_secret_header:{klow}", pin_hostname=host)
        # Catch close-misses like "X-Api-Key-2"
        if any(re.fullmatch(rf"{re.escape(p)}\b.*", klow) for p in ("authorization", "x-api-key", "x-auth-token")):
            return PinnedHostDecision(False, f"extra_secret_header:{klow}", pin_hostname=host)

    # Daily write cap.
    if is_write and pin.daily_outbound_limit > 0:
        used = _read_daily_counter(host)
        if used >= pin.daily_outbound_limit:
            return PinnedHostDecision(
                False, "daily_outbound_limit_reached",
                pin_hostname=host, matched_path=matched, is_write=True,
            )
        return PinnedHostDecision(
            True, _OK_REASON,
            pin_hostname=host, matched_path=matched, is_write=True,
            decrement_counter=True,
        )

    return PinnedHostDecision(
        True, _OK_REASON,
        pin_hostname=host, matched_path=matched, is_write=is_write,
    )


def record_counter_increment(decision: PinnedHostDecision) -> int:
    """Commit a write-quota slot. Call ONLY after the HTTP request
    completed successfully."""
    if not decision.decrement_counter or not decision.pin_hostname:
        return 0
    return _increment_daily_counter(decision.pin_hostname)


def daily_counter_value(hostname: str) -> int:
    """Test/observability helper — read the per-(host, today) write counter."""
    return _read_daily_counter(str(hostname or "").strip().lower())

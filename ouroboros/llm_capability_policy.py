"""Route capability metadata and the diagnostic model-global effort band.

A route's capabilities are discovered, not declared: OpenRouter model metadata
answers what a model accepts, and an exact route's own rejections are adapted by
request-wire recovery (``request_wire_recovery``), never by model-global learning.
This module owns the catalogue caches, the read-only diagnostic effort band over
historical capability-evidence rows, and the per-call effort disclosure slot.
Claudexor metadata discovery and requested-model admission use the engine's
negotiated declaration, without inventing a model row or a capability claim.
"""


from __future__ import annotations

import contextvars
import logging
import pathlib
import threading
import time
from typing import Any, Dict, Optional, Set

from ouroboros.claudexor_daemon import read_owned_gateway
from ouroboros.gateways.claudexor import operation_query_supported
from ouroboros.provider_models import normalize_model_identity


# The moved warnings keep the logger identity they were emitted under.
log = logging.getLogger("ouroboros.llm")

# Effort clamp/projection disclosure slot: a ContextVar isolates threads AND
# concurrent asyncio tasks (same isolation contract as the reasoning pin note).
_EFFORT_CLAMP_CVAR = contextvars.ContextVar("ouroboros_effort_clamp_note", default=None)


_OPTIONAL_SAMPLING_PARAMS = ("temperature", "top_p", "top_k")


# Optional intent catalogue metadata may report unsupported (``_build_remote_kwargs``).
_OPTIONAL_DROPPABLE_PARAMS = _OPTIONAL_SAMPLING_PARAMS + (
    "response_format", "reasoning_effort", "output_config", "thinking",
)


# Historical import surface re-exported by ``ouroboros.llm``; request-wire recovery owns its markers.
_MANDATORY_VALUE_MARKERS = ("mandatory", "cannot be disabled", "must be enabled")


def _catalog_path(drive_root: Any) -> pathlib.Path:
    """The catalogue one clean OpenRouter /models answer recorded for every process of the install."""
    return pathlib.Path(drive_root) / "state" / "openrouter_models.json"


def model_catalog(source: str, credential_profile_id: str | None = None, *,
                  requested_model: str | None = None, timeout_sec: float | None = None) -> dict:
    """Metadata-only transport under one connect, negotiation and read budget; the capability
    evidence owner interprets the envelope. An elapsed budget reads nothing more."""
    started = time.monotonic()
    gateway = read_owned_gateway(**({} if timeout_sec is None else {"timeout_sec": timeout_sec}))

    def budget() -> dict:
        if timeout_sec is None:
            return {}
        remaining = timeout_sec - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Model metadata observation deadline elapsed")
        return {"timeout_sec": remaining}

    try:
        hint = {"requested_model": requested_model} if requested_model is not None else {}
        if requested_model is not None and operation_query_supported(
                gateway.operations(**budget()),
                method="GET", path="/v2/model-sources/:id/models", name="includeAdmission", value="true"):
            hint["include_admission"] = True
        return gateway.list_source_models(source, credential_profile_id, **hint, **budget())
    finally:
        gateway.close()


def catalog_admits_model(catalog: dict, model: str) -> bool:
    """An exact engine admission permits an attempt, without inventing a model row.

    Older engines retain membership semantics. Source/account/freshness binding
    belongs to the caller; this is neither entitlement nor generation evidence.
    """
    admission = catalog.get("admission")
    return (any(isinstance(row, dict) and row.get("id") == model for row in catalog.get("models", []))
            or (isinstance(admission, dict) and admission.get("requestedModel") == model
                and admission.get("inventoryAbsence") == "advisory"))


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    # v6.57.0: the accepted set is the EFFORT_SCALE SSOT (config.py), so adding a
    # tier (e.g. `max`) happens in one place. Imported lazily to avoid a config
    # import cycle at module load.
    try:
        from ouroboros.config import EFFORT_SCALE as _SCALE
        allowed = set(_SCALE)
    except Exception:
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


class _CapabilityPolicyMixin:
    """Capability metadata and the diagnostic effort band."""

    # Missing capabilities mean "unknown": keep kwargs instead of stripping them.
    _SUPPORTED_PARAMS_CACHE: Dict[str, set] = {}

    _SUPPORTED_PARAMS_FETCHED: bool = False

    # Did the one-shot /models fetch actually reach OpenRouter (HTTP 200 + parse)?
    # Distinguishes a provider OUTAGE from a route with no metadata, so Capability
    # Evidence can mark STATUS_FAILED (transient) vs STATUS_UNPROBEABLE (v6.33.0 P4).
    _CAPABILITIES_FETCH_OK: bool = False

    # OpenRouter-reported context window per model id (provider_metadata evidence).
    _CONTEXT_LENGTH_CACHE: Dict[str, int] = {}
    _CONTEXT_LENGTH_OBSERVED_AT: str = ""
    # When this process's last /models read failed: for the failed-evidence TTL that one failure
    # answers every reader (the window, the output limit, every route) instead of another read.
    _CAPABILITIES_FAILED_AT: str = ""
    # One /models read at a time in a process; a reader that waited takes the answer of the read it waited for.
    _CATALOGUE_READ = threading.Lock()
    _CATALOGUE_READS: int = 0

    @classmethod
    def _fetch_openrouter_capabilities(cls) -> None:
        """Populate the catalogue caches from OpenRouter /models; a clean answer is recorded for
        every process (``state/openrouter_models.json``). A read that failed within the
        failed-evidence TTL answers instead: one attempt per process, not one per reader. Reads run one
        at a time and only a finished read sets these flags: a reader that arrives while one is in flight
        waits for it and takes its answer, never a failure that has not happened nor a second GET."""
        from ouroboros.capability_evidence import _FAILED_TTL_SEC, _age_seconds

        finished = cls._CATALOGUE_READS
        with cls._CATALOGUE_READ:
            if cls._CATALOGUE_READS != finished:
                return
            if cls._CAPABILITIES_FAILED_AT and _age_seconds(cls._CAPABILITIES_FAILED_AT) <= _FAILED_TTL_SEC:
                return
            try:
                cls._read_openrouter_catalogue()
            finally:
                cls._CATALOGUE_READS += 1

    @classmethod
    def _read_openrouter_catalogue(cls) -> None:
        """One GET of OpenRouter /models, under ``_CATALOGUE_READ``: the caches, the evidence and the record."""
        from ouroboros.utils import utc_now_iso

        def failed() -> None:  # non-200 or transport: this failure answers every reader for the TTL
            cls._SUPPORTED_PARAMS_FETCHED, cls._CAPABILITIES_FETCH_OK = True, False
            cls._CAPABILITIES_FAILED_AT = utc_now_iso()

        try:
            import requests
            # 5s, not 15s: this fetch sits on the synchronous capability-probe path
            # behind the max-context-mode gate (settings save / max toggle); a slow
            # probe must fail-closed quickly, never hang the save (v6.33.0 WS4).
            from ouroboros.net_transport import requests_verify_kwargs
            resp = requests.get(
                "https://openrouter.ai/api/v1/models",
                timeout=5,
                **requests_verify_kwargs(),
            )
            if resp.status_code != 200:
                log.debug(
                    "OpenRouter /models returned %d; supported_parameters cache empty",
                    resp.status_code,
                )
                failed()
                return
            rows = resp.json().get("data", []) or []
            from ouroboros.response_limits import record_catalog_limits
            from ouroboros.capability_evidence import canonical_evidence_root
            record_catalog_limits(canonical_evidence_root(),
                [(m.get("id"), (m.get("top_provider") or {}).get("max_completion_tokens")) for m in rows],
                provider="openrouter", base_url="https://openrouter.ai/api/v1",
                field="top_provider.max_completion_tokens", source="OpenRouter")
            context_lengths, recorded = {}, {}
            for m in rows:
                mid = m.get("id") or ""
                sp = m.get("supported_parameters")
                if mid and isinstance(sp, list) and sp:
                    cls._SUPPORTED_PARAMS_CACHE[mid] = set(sp)
                    recorded.setdefault(mid, {})["supported_parameters"] = sorted(str(name) for name in sp)
                # Context window (provider_metadata Capability Evidence source).
                cl = m.get("context_length")
                if mid and isinstance(cl, (int, float)) and cl > 0:
                    context_lengths[mid] = recorded.setdefault(mid, {})["context_length"] = int(cl)
            cls._CONTEXT_LENGTH_CACHE, cls._CONTEXT_LENGTH_OBSERVED_AT = context_lengths, utc_now_iso()
            # Image input is a fact about this exact route: one parser, stored
            # under the OpenRouter route scope with this response's time, so any
            # process (a worker, a cold VLM child) reads it without a fetch.
            from ouroboros.vision_routing import record_catalog_image_input

            record_catalog_image_input("openrouter", "https://openrouter.ai/api/v1", rows,
                                       source="OpenRouter /models")
            cls._SUPPORTED_PARAMS_FETCHED = cls._CAPABILITIES_FETCH_OK = True  # reached the provider, parsed it
            cls._CAPABILITIES_FAILED_AT = ""
        except Exception:
            log.debug("Failed to fetch OpenRouter model capabilities", exc_info=True)
            failed()
            return
        try:  # the record is a cache of this answer: its loss costs another process one GET
            from ouroboros.capability_evidence import canonical_evidence_root
            from ouroboros.utils import atomic_write_json

            atomic_write_json(_catalog_path(canonical_evidence_root()),
                              {"observed_at": cls._CONTEXT_LENGTH_OBSERVED_AT, "models": recorded})
        except OSError:
            log.debug("Failed to record the OpenRouter catalogue", exc_info=True)

    @classmethod
    def _adopt_recorded_catalog(cls) -> bool:
        """Adopt the catalogue a process recorded while it is within the confirmed-evidence TTL:
        the caches, date and success its fetch left, without a GET. A model it lacks stays unknown."""
        from ouroboros.capability_evidence import _CONFIRMED_TTL_SEC, _age_seconds, canonical_evidence_root
        from ouroboros.utils import json_snapshot

        try:
            record = json_snapshot(_catalog_path(canonical_evidence_root())) or {}
            models, observed = record.get("models"), str(record.get("observed_at") or "")
            if not isinstance(models, dict) or not observed or _age_seconds(observed) > _CONFIRMED_TTL_SEC:
                return False
            rows = [(str(mid), row) for mid, row in models.items() if isinstance(row, dict)]
            params = {mid: {str(name) for name in row["supported_parameters"]} for mid, row in rows
                      if isinstance(row.get("supported_parameters"), list) and row["supported_parameters"]}
            lengths = {mid: row["context_length"] for mid, row in rows
                       if type(row.get("context_length")) is int and row["context_length"] > 0}
        except Exception:
            log.debug("The recorded OpenRouter catalogue is unreadable", exc_info=True)
            return False
        cls._SUPPORTED_PARAMS_CACHE.update(params)
        cls._CONTEXT_LENGTH_CACHE, cls._CONTEXT_LENGTH_OBSERVED_AT = lengths, observed
        cls._SUPPORTED_PARAMS_FETCHED = cls._CAPABILITIES_FETCH_OK = True
        return True

    @classmethod
    def prefetch_openrouter_catalog(cls) -> None:
        """At boot, off every turn: observe the catalogue unless a process recorded a current one."""
        if not cls._adopt_recorded_catalog():
            cls._fetch_openrouter_capabilities()

    @classmethod
    def metadata_fetch_attempted_and_failed(cls) -> bool:
        """True when the one-shot OpenRouter /models fetch RAN but did not succeed
        (non-200 or transport error) — i.e. the provider was unreachable, distinct
        from 'not fetched yet'. Capability Evidence uses this to record STATUS_FAILED
        (a transient outage) instead of STATUS_UNPROBEABLE (no metadata source)."""
        return bool(cls._SUPPORTED_PARAMS_FETCHED and not cls._CAPABILITIES_FETCH_OK)

    @classmethod
    def _get_supported_parameters(cls, model_id: str) -> Optional[set]:
        """Supported parameter names, or None when unknown (no stripping): what this process or
        the recorded catalogue holds. A request never waits for the network (unknown, not denied)."""
        if not cls._SUPPORTED_PARAMS_FETCHED:
            cls._adopt_recorded_catalog()
        return cls._SUPPORTED_PARAMS_CACHE.get(model_id)

    @classmethod
    def openrouter_context_length(cls, model_id: str, *, allow_fetch: bool = True) -> int:
        """Read the catalog (a recorded one first); with fetching allowed, observe an absent model or
        refresh expired evidence."""
        from ouroboros.capability_evidence import _age_seconds, _CONFIRMED_TTL_SEC

        mid = str(model_id or "")
        if not cls._SUPPORTED_PARAMS_FETCHED or _age_seconds(cls._CONTEXT_LENGTH_OBSERVED_AT) > _CONFIRMED_TTL_SEC:
            cls._adopt_recorded_catalog()
        needs_fetch = (not cls._SUPPORTED_PARAMS_FETCHED) or (
            allow_fetch and (not cls._CAPABILITIES_FETCH_OK or mid not in cls._CONTEXT_LENGTH_CACHE
                            or _age_seconds(cls._CONTEXT_LENGTH_OBSERVED_AT) > _CONFIRMED_TTL_SEC)
        )
        if allow_fetch and needs_fetch:
            cls._fetch_openrouter_capabilities()
        return int(cls._CONTEXT_LENGTH_CACHE.get(mid, 0) or 0)

    # v6.57.0 — learned reasoning-effort ceilings (Q7). In-process cache is the hot
    # path; a durable copy in capability_evidence.json (effort_ceilings namespace,
    # DATA_DIR-scoped) survives restart. Key = normalized model identity. Fail-open.
    _EFFORT_CEILING_CACHE: Dict[str, str] = {}

    _EFFORT_CEILING_LOADED: Set[str] = set()

    # v6.73.2 — learned reasoning-effort FLOORS: the value-too-low mirror of the
    # ceilings, for endpoints where reasoning is MANDATORY and "none"/"minimal"
    # 400s ("Reasoning is mandatory ... cannot be disabled"). Unlike the sticky
    # ceilings, floors EXPIRE in the durable store (provider policy changes), so
    # the process cache re-syncs hourly — a long-running process heals the
    # same way a restart does.
    _EFFORT_FLOOR_CACHE: Dict[str, str] = {}

    _EFFORT_FLOOR_LOADED: Dict[str, float] = {}

    _EFFORT_FLOOR_RELOAD_SEC = 3600.0

    @classmethod
    def _effort_floor_for(cls, model_id: str) -> str:
        key = normalize_model_identity(model_id) or str(model_id or "")
        if not key:
            return ""
        now = time.monotonic()
        loaded_at = cls._EFFORT_FLOOR_LOADED.get(key)
        if loaded_at is None or now - loaded_at >= cls._EFFORT_FLOOR_RELOAD_SEC:
            cls._EFFORT_FLOOR_LOADED[key] = now
            try:
                from ouroboros.capability_evidence import get_effort_floor
                from ouroboros.config import DATA_DIR
                # Replace (not union): the durable reader applies the 14-day
                # expiry, so replacing lets an expired floor actually evict.
                cls._EFFORT_FLOOR_CACHE[key] = get_effort_floor(DATA_DIR, key)
            except Exception:
                pass
        return cls._EFFORT_FLOOR_CACHE.get(key, "")

    @classmethod
    def _effort_ceiling_for(cls, model_id: str) -> str:
        key = normalize_model_identity(model_id) or str(model_id or "")
        if not key:
            return ""
        if key in cls._EFFORT_CEILING_CACHE:
            return cls._EFFORT_CEILING_CACHE[key]
        if key in cls._EFFORT_CEILING_LOADED:
            return ""
        cls._EFFORT_CEILING_LOADED.add(key)
        try:
            from ouroboros.capability_evidence import get_effort_ceiling
            from ouroboros.config import DATA_DIR
            ceil = get_effort_ceiling(DATA_DIR, key)
            if ceil:
                cls._EFFORT_CEILING_CACHE[key] = ceil
            return ceil
        except Exception:
            return ""

    @classmethod
    def clamp_effort_for_route(cls, model_id: str, effort: str) -> str:
        """Resolve the legacy diagnostic model-global effort band."""
        ceiling = cls._effort_ceiling_for(model_id)
        floor = cls._effort_floor_for(model_id)
        if not ceiling and not floor:
            return effort
        from ouroboros.config import clamp_effort_to, effort_rank
        applied = clamp_effort_to(effort, ceiling) if ceiling else effort
        if floor and 0 <= effort_rank(applied) < effort_rank(floor):
            applied = floor
        return applied

    def _pop_thread_disclosure(self, slot: str) -> Optional[Dict[str, Any]]:
        """Take and clear the disclosure staged in thread-local ``slot`` for THIS
        thread's call; these slots stage before or at send (pin and effort
        notes: ContextVar)."""
        tls = getattr(self, slot, None)
        pending = getattr(tls, "pending", None) if tls is not None else None
        if tls is not None:
            tls.pending = None
        return pending if isinstance(pending, dict) else None

    def _pop_effort_clamp_disclosure(self) -> Optional[Dict[str, Any]]:
        """The pending clamp record for THIS call's context (thread or asyncio task)."""
        pending = _EFFORT_CLAMP_CVAR.get()
        _EFFORT_CLAMP_CVAR.set(None)
        return pending if isinstance(pending, dict) else None

"""
Ouroboros — Shared configuration (single source of truth).

Paths, settings defaults, load/save with file locking.
Only imports ouroboros.platform_layer (platform abstraction, no circular deps).
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Optional

from ouroboros.platform_layer import pid_lock_acquire as _compat_pid_lock_acquire
from ouroboros.platform_layer import pid_lock_release as _compat_pid_lock_release
from ouroboros.provider_models import migrate_model_value


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HOME = pathlib.Path.home()
APP_ROOT = pathlib.Path(os.environ.get("OUROBOROS_APP_ROOT", HOME / "Ouroboros"))
REPO_DIR = pathlib.Path(os.environ.get("OUROBOROS_REPO_DIR", APP_ROOT / "repo"))
DATA_DIR = pathlib.Path(os.environ.get("OUROBOROS_DATA_DIR", APP_ROOT / "data"))
SETTINGS_PATH = pathlib.Path(os.environ.get("OUROBOROS_SETTINGS_PATH", DATA_DIR / "settings.json"))
PID_FILE = pathlib.Path(os.environ.get("OUROBOROS_PID_FILE", APP_ROOT / "ouroboros.pid"))
PORT_FILE = pathlib.Path(os.environ.get("OUROBOROS_PORT_FILE", DATA_DIR / "state" / "server_port"))

RESTART_EXIT_CODE = 42
PANIC_EXIT_CODE = 99
AGENT_SERVER_PORT = 8765


# ---------------------------------------------------------------------------
# Settings defaults
# ---------------------------------------------------------------------------
SETTINGS_DEFAULTS = {
    "OPENROUTER_API_KEY": "",
    "OPENAI_API_KEY": "",
    "OPENAI_BASE_URL": "",
    "OPENAI_COMPATIBLE_API_KEY": "",
    "OPENAI_COMPATIBLE_BASE_URL": "",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY": "",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL": "https://foundation-models.api.cloud.ru/v1",
    "ANTHROPIC_API_KEY": "",
    "TELEGRAM_BOT_TOKEN": "",
    "TELEGRAM_CHAT_ID": "",

    "OUROBOROS_NETWORK_PASSWORD": "",
    "OUROBOROS_SERVER_HOST": "127.0.0.1",
    "OUROBOROS_MODEL": "anthropic/claude-opus-4.6",
    "OUROBOROS_MODEL_CODE": "anthropic/claude-opus-4.6",
    "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4.6",
    "OUROBOROS_MODEL_FALLBACK": "anthropic/claude-sonnet-4.6",
    "CLAUDE_CODE_MODEL": "claude-opus-4-6[1m]",
    "OUROBOROS_MAX_WORKERS": 5,
    "TOTAL_BUDGET": 10.0,
    "OUROBOROS_PER_TASK_COST_USD": 20.0,
    "OUROBOROS_SOFT_TIMEOUT_SEC": 600,
    "OUROBOROS_HARD_TIMEOUT_SEC": 1800,
    "OUROBOROS_TOOL_TIMEOUT_SEC": 600,
    "OUROBOROS_BG_MAX_ROUNDS": 5,
    "OUROBOROS_BG_WAKEUP_MIN": 30,
    "OUROBOROS_BG_WAKEUP_MAX": 7200,
    "OUROBOROS_EVO_COST_THRESHOLD": 0.10,
    "OUROBOROS_WEBSEARCH_MODEL": "gpt-5.2",
    # Pre-commit review: comma-separated provider-tagged model list
    "OUROBOROS_REVIEW_MODELS": "openai/gpt-5.5,google/gemini-3.1-pro-preview,anthropic/claude-opus-4.6",
    # Pre-commit review enforcement: advisory | blocking
    "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
    # Runtime mode: light | advanced | pro.
    # "advanced" preserves the existing self-modifying evolutionary layer and
    # is the safe default for current installs. "pro" is reserved for a
    # direct protected-surface lane guarded by the normal triad+scope review gate.
    "OUROBOROS_RUNTIME_MODE": "advanced",
    # Optional EXTRA discovery root for an external skills/extensions
    # repository (the user's own git checkout). Ouroboros scans this on
    # top of the in-data-plane ``data/skills/`` tree (which is the
    # primary location since v4.50). Empty means "use only the data
    # plane". Ouroboros never clones or pulls this directory itself.
    "OUROBOROS_SKILLS_REPO_PATH": "",
    "OUROBOROS_CLAWHUB_REGISTRY_URL": "https://clawhub.ai/api/v1",
    "OUROBOROS_HUB_CATALOG_URL": "https://raw.githubusercontent.com/joi-lab/OuroborosHub/main/catalog.json",
    # Scope review: single-model blocking reviewer (runs after triad review)
    "OUROBOROS_SCOPE_REVIEW_MODEL": "openai/gpt-5.5",
    # Reasoning effort per task type: none | low | medium | high
    # OUROBOROS_INITIAL_REASONING_EFFORT remains a legacy alias for task/chat.
    "OUROBOROS_EFFORT_TASK": "medium",
    "OUROBOROS_EFFORT_EVOLUTION": "high",
    "OUROBOROS_EFFORT_REVIEW": "medium",
    "OUROBOROS_EFFORT_SCOPE_REVIEW": "high",
    "OUROBOROS_EFFORT_CONSCIOUSNESS": "low",
    "GITHUB_TOKEN": "",
    "GITHUB_REPO": "",
    # Local model (llama-cpp-python server)
    "LOCAL_MODEL_SOURCE": "",
    "LOCAL_MODEL_FILENAME": "",
    "LOCAL_MODEL_PORT": 8766,
    "LOCAL_MODEL_N_GPU_LAYERS": 0,
    "LOCAL_MODEL_CONTEXT_LENGTH": 16384,
    "LOCAL_MODEL_CHAT_FORMAT": "",
    "USE_LOCAL_MAIN": False,
    "USE_LOCAL_CODE": False,
    "USE_LOCAL_LIGHT": False,
    "USE_LOCAL_FALLBACK": False,
    "OUROBOROS_FILE_BROWSER_DEFAULT": "",
    # A2A (Agent-to-Agent) protocol — disabled by default; requires restart to toggle
    "A2A_ENABLED": False,
    "A2A_PORT": 18800,
    "A2A_HOST": "127.0.0.1",
    "A2A_AGENT_NAME": "",
    "A2A_AGENT_DESCRIPTION": "",
    "A2A_MAX_CONCURRENT": 3,
    "A2A_TASK_TTL_HOURS": 24,
}

_VALID_EFFORTS = ("none", "low", "medium", "high")
_DIRECT_PROVIDER_REVIEW_RUNS = 3

# Phase 2 three-layer refactor runtime mode. Separate axis from
# ``OUROBOROS_REVIEW_ENFORCEMENT`` — review strictness and self-modification
# scope are orthogonal concerns and must not collapse into one flag.
VALID_RUNTIME_MODES = ("light", "advanced", "pro")

# Privilege ranking: lower = stricter scope. Used by ``save_settings`` to
# refuse self-elevation attempts. ``light`` blocks repo self-modification;
# ``advanced`` opens evolutionary-layer writes; ``pro`` opens protected
# core/contract/release writes (still gated by triad+scope review at
# commit). Owner picks scope; agent must not raise it through any
# tool channel — see ``save_settings`` chokepoint below.
_RUNTIME_MODE_RANK = {"light": 0, "advanced": 1, "pro": 2}

# Boot-time runtime mode baseline. Captured once by
# ``initialize_runtime_mode_baseline`` (called from server lifespan and
# launcher startup AFTER initial settings load + apply_settings_to_env)
# and used by ``save_settings`` to refuse elevation against THIS value
# instead of against on-disk old. The on-disk-old baseline is bypassable
# by an out-of-process write (e.g. subprocess that goes around the
# chokepoint): if disk gets corrupted to ``pro``, a follow-up
# ``load_settings → save_settings`` round-trip sees ``incoming==disk_old``
# and lets the elevation through. Pinning the baseline to the value
# captured at process boot closes that loophole — the disk corruption
# can no longer be propagated into ``os.environ`` via any in-process
# ``save_settings`` caller.
#
# v5.1.2 iter-2 fix (multi-critic finding F2-1): the baseline is also
# exported to ``os.environ[BOOT_RUNTIME_MODE_ENV_KEY]`` whenever
# ``initialize_runtime_mode_baseline`` runs. ``save_settings`` falls
# back to that env var when the in-memory ``_BOOT_RUNTIME_MODE`` is
# still ``None`` — which is the case in fresh subprocesses spawned by
# ``run_shell`` / ``skill_exec`` / ``multiprocessing`` (spawn). Env
# vars are inherited across ``subprocess.Popen``, ``mp.spawn``, and
# ``mp.fork``, so a child of the supervisor sees the same baseline.
# The agent CAN clobber its own ``os.environ`` from Python, but a
# subprocess cannot reach back into the parent's ``os.environ``;
# therefore, even after a clobbered child, the parent supervisor's
# in-memory ``_BOOT_RUNTIME_MODE`` continues to gate writes via the
# in-process ``save_settings`` path.
_BOOT_RUNTIME_MODE: Optional[str] = None
BOOT_RUNTIME_MODE_ENV_KEY = "OUROBOROS_BOOT_RUNTIME_MODE"


def _resolve_baseline_from_env() -> Optional[str]:
    """Return the pinned-baseline value inherited via env var, or None.

    Used by ``save_settings`` so a fresh subprocess that re-imports
    this module sees the parent's pin instead of falling back to the
    permissive ``allow_elevation=True`` branch.
    """
    raw = os.environ.get(BOOT_RUNTIME_MODE_ENV_KEY, "")
    if not raw:
        return None
    return normalize_runtime_mode(raw)


def initialize_runtime_mode_baseline(mode: Optional[str] = None) -> None:
    """Pin the boot-time runtime-mode baseline used by ``save_settings``.

    **Order contract (must be respected by every entry point):**

    1. Call AFTER ``load_settings`` + ``apply_settings_to_env`` so the
       captured value reflects the owner's actual choice for this run
       (``OUROBOROS_RUNTIME_MODE`` is what the env carries at this
       point, which equals the on-disk value the launcher / lifespan
       already validated).
    2. Call BEFORE any agent code runs (worker spawn, supervisor
       start, anything that may import + call ``save_settings``). The
       chokepoint is only meaningful once this is set.
    3. Subsequent calls are no-ops — the baseline is immutable for the
       life of the process.

    Production callers MUST also propagate the baseline to subprocess
    children. This function does so automatically by exporting
    ``OUROBOROS_BOOT_RUNTIME_MODE`` (a parallel env var, distinct from
    ``OUROBOROS_RUNTIME_MODE`` so the application code reads the
    current mode and the chokepoint reads the boot-time baseline).
    Children that re-import ``ouroboros.config`` see the parent's pin
    via ``_resolve_baseline_from_env``.

    Tests that monkeypatch settings paths can call this with an
    explicit ``mode`` to set the baseline for a hermetic test, or pass
    ``None`` to read the current ``OUROBOROS_RUNTIME_MODE`` env var.
    """
    global _BOOT_RUNTIME_MODE
    if _BOOT_RUNTIME_MODE is not None:
        return
    if mode is None:
        # v5.1.2 iter-2 real triad+scope finding SR1: prefer the
        # parent-exported ``OUROBOROS_BOOT_RUNTIME_MODE`` over
        # ``OUROBOROS_RUNTIME_MODE``. A fresh subprocess inherits BOTH
        # vars, but the parent considers only the BOOT key authoritative
        # (the agent CAN clobber its own ``OUROBOROS_RUNTIME_MODE`` to
        # poison the baseline; ``OUROBOROS_BOOT_RUNTIME_MODE`` is only
        # set by ``initialize_runtime_mode_baseline`` itself and is the
        # actual owner-pinned value). Only fall back to RUNTIME_MODE on
        # processes where no baseline was ever pinned (true first
        # launch / hermetic test).
        inherited = _resolve_baseline_from_env()
        if inherited is not None:
            mode = inherited
        else:
            mode = os.environ.get("OUROBOROS_RUNTIME_MODE", "advanced") or "advanced"
    _BOOT_RUNTIME_MODE = normalize_runtime_mode(mode)
    # Propagate to env so subprocesses can inherit the pin.
    os.environ[BOOT_RUNTIME_MODE_ENV_KEY] = _BOOT_RUNTIME_MODE


def reset_runtime_mode_baseline_for_tests() -> None:
    """Test-only helper to clear the pinned baseline AND the env var.

    Production code MUST NOT call this. Tests that need to exercise
    different baselines call this between cases. Documented under a
    descriptive name so a casual reader cannot mistake it for a
    production API.
    """
    global _BOOT_RUNTIME_MODE
    _BOOT_RUNTIME_MODE = None
    os.environ.pop(BOOT_RUNTIME_MODE_ENV_KEY, None)


def _parse_model_list(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _exclusive_direct_remote_provider_env() -> str:
    has_openrouter = bool(str(os.environ.get("OPENROUTER_API_KEY", "") or "").strip())
    has_openai = bool(str(os.environ.get("OPENAI_API_KEY", "") or "").strip())
    has_anthropic = bool(str(os.environ.get("ANTHROPIC_API_KEY", "") or "").strip())
    has_legacy_base = bool(str(os.environ.get("OPENAI_BASE_URL", "") or "").strip())
    has_compatible = bool(str(os.environ.get("OPENAI_COMPATIBLE_API_KEY", "") or "").strip())
    has_cloudru = bool(str(os.environ.get("CLOUDRU_FOUNDATION_MODELS_API_KEY", "") or "").strip())
    if has_openrouter or has_legacy_base or has_compatible or has_cloudru:
        return ""
    if has_openai and not has_anthropic:
        return "openai"
    if has_anthropic and not has_openai:
        return "anthropic"
    return ""


def resolve_effort(task_type: str) -> str:
    """Return the configured reasoning effort for the given task type."""
    t = (task_type or "").lower().strip()

    if t == "evolution":
        key = "OUROBOROS_EFFORT_EVOLUTION"
        default = "high"
    elif t == "review":
        key = "OUROBOROS_EFFORT_REVIEW"
        default = "medium"
    elif t == "deep_self_review":
        key = "OUROBOROS_EFFORT_TASK"
        default = "high"
    elif t in ("scope_review", "scope-review"):
        key = "OUROBOROS_EFFORT_SCOPE_REVIEW"
        default = "high"
    elif t == "consciousness":
        key = "OUROBOROS_EFFORT_CONSCIOUSNESS"
        default = "low"
    else:
        legacy = os.environ.get("OUROBOROS_INITIAL_REASONING_EFFORT", "")
        key = "OUROBOROS_EFFORT_TASK"
        default = legacy if legacy in _VALID_EFFORTS else "medium"

    raw = os.environ.get(key, default)
    return raw if raw in _VALID_EFFORTS else default


def direct_provider_review_models_fallback(provider: str) -> list[str]:
    """Return the exact review-models list a direct-provider fallback would emit.

    Mirrors `server_runtime._normalize_direct_review_models`. Public so callers
    (e.g. `plan_task`'s quorum validator) can recognise the exact shape the
    auto-fallback would have produced and distinguish it from user-authored
    duplicate lists. Returns `[]` when `provider` is not one of the supported
    exclusive direct providers or when the main-model lane is not
    provider-prefixed.
    """
    if provider not in ("openai", "anthropic"):
        return []
    main_model = str(
        os.environ.get("OUROBOROS_MODEL", SETTINGS_DEFAULTS["OUROBOROS_MODEL"]) or ""
    ).strip()
    main_model = migrate_model_value(provider, main_model)
    provider_prefix = f"{provider}::"
    if not main_model.startswith(provider_prefix):
        return []
    from ouroboros.provider_models import (
        OPENAI_DIRECT_DEFAULTS, ANTHROPIC_DIRECT_DEFAULTS,
    )
    _defaults = {
        "openai": OPENAI_DIRECT_DEFAULTS,
        "anthropic": ANTHROPIC_DIRECT_DEFAULTS,
    }.get(provider, {})
    user_light_raw = str(os.environ.get("OUROBOROS_MODEL_LIGHT", "") or "").strip()
    user_light = migrate_model_value(provider, user_light_raw) if user_light_raw else ""
    default_light = migrate_model_value(provider, _defaults.get("light", ""))
    light_slot = user_light if user_light.startswith(provider_prefix) else default_light
    if light_slot and light_slot != main_model:
        return [main_model, light_slot, light_slot]
    return [main_model] * _DIRECT_PROVIDER_REVIEW_RUNS


def get_review_models() -> list[str]:
    """Return the configured pre-commit review model list."""
    default_str = SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"]
    models_str = os.environ.get("OUROBOROS_REVIEW_MODELS", default_str) or default_str
    models = _parse_model_list(models_str)
    provider = _exclusive_direct_remote_provider_env()
    if not provider:
        return models

    main_model = str(os.environ.get("OUROBOROS_MODEL", SETTINGS_DEFAULTS["OUROBOROS_MODEL"]) or "").strip()
    main_model = migrate_model_value(provider, main_model)
    provider_prefix = f"{provider}::"
    if not main_model.startswith(provider_prefix):
        return models

    migrated = [migrate_model_value(provider, model) for model in models]
    if not migrated or len(migrated) < 2 or any(not model.startswith(provider_prefix) for model in migrated):
        # v4.39.0: mirror `server_runtime._normalize_direct_review_models` —
        # the quorum-safe fallback shape is `[main, light, light]` (3 slots,
        # 2 unique) so both commit triad and plan_task work out of the box.
        # When light is missing or collapses to main (user overrode both
        # lanes identically), degrade to the legacy `[main] * N` shape.
        return direct_provider_review_models_fallback(provider)
    return migrated


def get_review_enforcement() -> str:
    """Return the configured pre-commit review enforcement mode."""
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"])
    raw = (os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT", default_val) or default_val).strip().lower()
    return raw if raw in {"advisory", "blocking"} else default_val


def normalize_runtime_mode(value: Any) -> str:
    """Clamp an arbitrary caller-supplied runtime mode to a valid value.

    Used on both the write path (``api_settings_post`` / onboarding save)
    and the read path (``get_runtime_mode``) so the stored value,
    ``/api/settings`` echo, ``/api/state``, and the UI segmented control
    can never drift — a typo like ``"turbo"`` is silently pinned to the
    default (``advanced``) everywhere instead of being accepted by the
    save path and clamped only at read time.

    Returns the canonical lowercase mode string. Non-string / empty /
    unknown inputs map to ``SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"]``.
    """
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"])
    text = str(value or "").strip().lower()
    return text if text in VALID_RUNTIME_MODES else default_val


def get_runtime_mode() -> str:
    """Return the configured runtime mode (light / advanced / pro).

    Reads ``OUROBOROS_RUNTIME_MODE`` from the environment with
    ``SETTINGS_DEFAULTS`` as fallback, then delegates to
    ``normalize_runtime_mode`` so unknown or empty values silently degrade
    to the default. Phase 2 is plumbing only — callers should still guard
    behaviour against this value on their own in Phase 3+.
    """
    default_val = str(SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"])
    return normalize_runtime_mode(
        os.environ.get("OUROBOROS_RUNTIME_MODE", default_val) or default_val
    )


def get_skills_repo_path() -> str:
    """Return the configured external skills repo checkout path (or empty).

    Expands a leading ``~`` so settings files written as ``~/Ouroboros/skills``
    resolve to the user home. Returns an empty string when unset.
    """
    raw = (
        os.environ.get("OUROBOROS_SKILLS_REPO_PATH", "") or ""
    ).strip()
    if not raw:
        return ""
    try:
        return str(pathlib.Path(raw).expanduser())
    except Exception:
        return raw


# ---------------------------------------------------------------------------
# Skills data layout
# ---------------------------------------------------------------------------
#
# v4.50 moved skill packages out of the git-tracked ``repo/skills/`` source
# tree into the data plane (``data/skills/``) so the launcher seed (still
# shipped under ``repo/skills/``) is a one-time bootstrap copy rather than
# the live runtime location. Subdirectories carry the discovery source so
# the Skills/Marketplace UI can group/filter by origin:
#
#   data/skills/native/<slug>/    -- bootstrapped from repo/skills/<slug>/
#   data/skills/clawhub/<slug>/   -- installed by the ClawHub marketplace
#   data/skills/external/<slug>/  -- user-managed (drop in manually)
#   data/skills/ouroboroshub/<slug>/ -- installed from the official static GitHub catalog
#
# ``OUROBOROS_SKILLS_REPO_PATH`` continues to work as an OPTIONAL extra
# discovery root for power users who keep skills in their own checkout.

SKILL_SOURCE_NATIVE = "native"
SKILL_SOURCE_CLAWHUB = "clawhub"
SKILL_SOURCE_EXTERNAL = "external"
SKILL_SOURCE_OUROBOROSHUB = "ouroboroshub"
SKILL_SOURCE_USER_REPO = "user_repo"

SKILL_SOURCE_SUBDIRS = (
    SKILL_SOURCE_NATIVE,
    SKILL_SOURCE_CLAWHUB,
    SKILL_SOURCE_EXTERNAL,
    SKILL_SOURCE_OUROBOROSHUB,
)


def get_data_skills_dir() -> pathlib.Path:
    """Return ``<DATA_DIR>/skills/`` (created on demand).

    Single root for all skill packages discovered by the runtime;
    subdirectories distinguish source (``native`` / ``clawhub`` /
    ``external``). The directory is created on first read so callers
    can rely on ``.iterdir()`` working without a separate bootstrap.
    """
    return ensure_data_skills_dir(DATA_DIR)


def ensure_data_skills_dir(data_dir: pathlib.Path) -> pathlib.Path:
    """Create + return ``<data_dir>/skills/{native,clawhub,external}/``.

    Split out from :func:`get_data_skills_dir` so callers that want
    to ENSURE the layout (launcher bootstrap, marketplace install)
    can opt-in to the side effect, while pure-lookup callers (skill
    discovery in tests) can use :func:`resolve_data_skills_dir` which
    is read-only. Cycle 2 critic finding (Opus #3): the previous
    behaviour of creating directories from inside a function whose
    name implied "resolve" surprised tests and bled mock state into
    the developer's real ``~/Ouroboros/data/`` tree.
    """
    root = data_dir / "skills"
    try:
        root.mkdir(parents=True, exist_ok=True)
        for sub in SKILL_SOURCE_SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return root


def resolve_data_skills_dir(data_dir: pathlib.Path) -> Optional[pathlib.Path]:
    """Return ``<data_dir>/skills/`` if it exists on disk, else ``None``.

    Pure read — does NOT create the directory. The marketplace +
    launcher call :func:`ensure_data_skills_dir` to create the layout
    explicitly; callers that just want to know "does this exist yet"
    use this helper.
    """
    candidate = data_dir / "skills"
    return candidate if candidate.is_dir() else None


def get_clawhub_skills_dir() -> pathlib.Path:
    """Return ``<DATA_DIR>/skills/clawhub/`` (created on demand)."""
    target = get_data_skills_dir() / SKILL_SOURCE_CLAWHUB
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def get_ouroboroshub_catalog_url() -> str:
    """Return the official OuroborosHub static catalog URL."""

    return str(load_settings().get("OUROBOROS_HUB_CATALOG_URL") or SETTINGS_DEFAULTS["OUROBOROS_HUB_CATALOG_URL"]).strip()


def get_ouroboroshub_skills_dir() -> pathlib.Path:
    """Return ``<DATA_DIR>/skills/ouroboroshub/`` (created on demand)."""

    target = get_data_skills_dir() / SKILL_SOURCE_OUROBOROSHUB
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def get_clawhub_enabled() -> bool:
    """Return True; ClawHub is no longer user-disabled by settings.

    Kept as a compatibility helper for older call sites and tests while
    the public switch is retired. Registry host validation remains the
    actual safety boundary.
    """
    return True


def get_clawhub_registry_url() -> str:
    """Return the configured ClawHub registry base URL (raw value).

    Trailing slashes are stripped + any query string / fragment is
    dropped so callers can append path segments without double-slash
    bugs and without accidentally appending after a ``?key=foo``.

    HOST ENFORCEMENT IS NOT PERFORMED HERE — this function only
    normalises the raw value. The actual allowlist check happens at
    HTTP-call time inside
    :func:`ouroboros.marketplace.clawhub._registry_base_url` (also
    re-applied per redirect hop via
    :class:`ouroboros.marketplace.clawhub._AllowlistRedirectHandler`).
    A future caller that uses this URL outside the marketplace client
    must re-validate the host explicitly.
    """
    raw = (os.environ.get("OUROBOROS_CLAWHUB_REGISTRY_URL", "") or "").strip()
    default_url = "https://clawhub.ai/api/v1"
    if not raw:
        return default_url
    import urllib.parse as _urlparse
    components = _urlparse.urlparse(raw)
    cleaned = _urlparse.urlunparse(
        (components.scheme, components.netloc, components.path.rstrip("/"), "", "", "")
    )
    return cleaned


# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
def read_version() -> str:
    try:
        if getattr(sys, "frozen", False):
            vp = pathlib.Path(sys._MEIPASS) / "VERSION"
        else:
            vp = pathlib.Path(__file__).parent.parent / "VERSION"
        return vp.read_text(encoding="utf-8").strip()
    except Exception:
        return "0.0.0"


# ---------------------------------------------------------------------------
# Settings file locking
# ---------------------------------------------------------------------------
_SETTINGS_LOCK = pathlib.Path(str(SETTINGS_PATH) + ".lock")


def _acquire_settings_lock(timeout: float = 2.0) -> Optional[int]:
    start = time.time()
    while time.time() - start < timeout:
        try:
            fd = os.open(str(_SETTINGS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            return fd
        except FileExistsError:
            try:
                if time.time() - _SETTINGS_LOCK.stat().st_mtime > 10:
                    _SETTINGS_LOCK.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.01)
        except Exception:
            break
    return None


def _release_settings_lock(fd: Optional[int]) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except Exception:
            pass
    try:
        _SETTINGS_LOCK.unlink()
    except Exception:
        pass


def _coerce_setting_value(key: str, value):
    default = SETTINGS_DEFAULTS.get(key)
    # Phase 2: runtime-mode is a closed enum. Normalize on the read path
    # (``load_settings``) so every downstream consumer — ``api_settings_get``,
    # the onboarding bootstrap, ``get_runtime_mode`` — sees the clamped
    # value. A legacy ``settings.json`` containing e.g. ``"turbo"`` cannot
    # leak an invalid mode into the UI or the runtime.
    if key == "OUROBOROS_RUNTIME_MODE":
        return normalize_runtime_mode(value)
    # Phase 2: whitespace around the opaque skills-repo path would leave the
    # ``skills_repo_configured`` boolean in ``/api/state`` non-deterministic.
    # Trim on load so empty-with-spaces truly reads as empty.
    if key == "OUROBOROS_SKILLS_REPO_PATH":
        return str(value or "").strip()
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            return default
    return str(value or "")


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------
def load_settings() -> dict:
    fd = _acquire_settings_lock()
    try:
        loaded: dict = {}
        if SETTINGS_PATH.exists():
            try:
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    loaded = {
                        key: _coerce_setting_value(key, value) if key in SETTINGS_DEFAULTS else value
                        for key, value in raw.items()
                    }
            except Exception:
                pass
        settings = dict(SETTINGS_DEFAULTS)
        settings.update(loaded)
        for key in SETTINGS_DEFAULTS:
            raw_env = os.environ.get(key)
            if raw_env is None or raw_env == "":
                continue
            if key in loaded and settings.get(key) not in {None, ""}:
                continue
            settings[key] = _coerce_setting_value(key, raw_env)
        return settings
    finally:
        _release_settings_lock(fd)


def save_settings(settings: dict, *, allow_elevation: bool = False) -> None:
    """Persist settings to disk.

    Single chokepoint for the runtime_mode self-elevation ratchet
    (v5.1.2): if the incoming ``OUROBOROS_RUNTIME_MODE`` ranks higher
    than the boot-time baseline (or on-disk old as fallback) and the
    caller did not pass ``allow_elevation=True``, the write is refused
    with ``PermissionError``. Same-mode and downgrade saves are
    unaffected.

    **Security note (iter-1 real triad+scope finding T1):**
    ``allow_elevation=True`` is honoured ONLY before the boot-time
    baseline has been pinned (i.e., during launcher / lifespan
    pre-agent initialization). Once ``_BOOT_RUNTIME_MODE`` is set, an
    agent-reachable subprocess that imports this function cannot use
    the public ``allow_elevation=True`` argument to bypass the rank
    comparison — the kwarg becomes inert. This makes the consent flag
    non-forgeable from agent code: the only way to elevate is to stop
    the agent (which clears ``_BOOT_RUNTIME_MODE``) and then either
    edit ``settings.json`` directly or restart through the launcher /
    server lifespan path.

    The baseline is the boot-time ``_BOOT_RUNTIME_MODE`` if pinned, or
    the on-disk value as a fallback for hermetic tests / pre-baseline
    saves. The on-disk fallback is bypassable by an out-of-process
    write (subprocess that goes around the chokepoint), so production
    code MUST call ``initialize_runtime_mode_baseline`` before any
    agent code runs.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = _acquire_settings_lock()
    try:
        # Baseline preference order:
        #   1. ``_BOOT_RUNTIME_MODE`` if the launcher/lifespan has
        #      pinned the boot-time mode in this Python process.
        #   2. ``_resolve_baseline_from_env()`` — the parent's pinned
        #      baseline propagated via env var. Closes the
        #      fresh-subprocess regression where ``_BOOT_RUNTIME_MODE``
        #      starts as ``None`` because Python re-imports
        #      ``ouroboros.config`` from scratch.
        #   3. On-disk old value as a final fallback for hermetic tests
        #      and pre-launcher scenarios where neither pin nor env
        #      var exists.
        #   4. ``"advanced"`` default (matches ``SETTINGS_DEFAULTS``).
        baseline_pinned_in_process = _BOOT_RUNTIME_MODE is not None
        baseline_inherited_from_env = (
            not baseline_pinned_in_process and _resolve_baseline_from_env() is not None
        )
        if baseline_pinned_in_process:
            baseline_mode = _BOOT_RUNTIME_MODE
        elif baseline_inherited_from_env:
            baseline_mode = _resolve_baseline_from_env()
        else:
            baseline_mode = "advanced"
            if SETTINGS_PATH.exists():
                try:
                    disk_settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                    baseline_mode = normalize_runtime_mode(disk_settings.get("OUROBOROS_RUNTIME_MODE"))
                except (OSError, json.JSONDecodeError):
                    pass
        new_mode = normalize_runtime_mode(settings.get("OUROBOROS_RUNTIME_MODE"))
        # Once the boot baseline is pinned (in this process OR inherited
        # from the parent supervisor via env), ``allow_elevation`` becomes
        # inert. Agent-reachable subprocesses that import ``save_settings``
        # and try to pass the public ``allow_elevation=True`` cannot bypass
        # the rank check — the consent flag is only honoured during
        # pre-agent initialization (launcher / lifespan), when both the
        # in-process global AND the inherited env var are absent.
        baseline_pinned = baseline_pinned_in_process or baseline_inherited_from_env
        consent_honoured = allow_elevation and not baseline_pinned
        if (_RUNTIME_MODE_RANK[new_mode] > _RUNTIME_MODE_RANK[baseline_mode]
                and not consent_honoured):
            if baseline_pinned and allow_elevation:
                hint = (
                    " The boot baseline is pinned for this run "
                    f"(source={'in-process' if baseline_pinned_in_process else 'env-var'}); "
                    "``allow_elevation=True`` is inert post-init. To "
                    "change the mode, stop the agent and edit "
                    "settings.json directly, then restart."
                )
            else:
                hint = (
                    " Runtime mode is owner-controlled — change it by "
                    "editing settings.json directly while the agent is "
                    "stopped, then restart."
                )
            raise PermissionError(
                f"OUROBOROS_RUNTIME_MODE elevation refused: "
                f"{baseline_mode!r} -> {new_mode!r}.{hint}"
            )
        try:
            tmp = SETTINGS_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(SETTINGS_PATH))
        except OSError:
            SETTINGS_PATH.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    finally:
        _release_settings_lock(fd)


def apply_settings_to_env(settings: dict) -> None:
    """Push settings into environment variables for supervisor modules."""
    env_keys = [
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY", "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "OUROBOROS_NETWORK_PASSWORD",
        "OUROBOROS_MODEL", "OUROBOROS_MODEL_CODE", "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK", "CLAUDE_CODE_MODEL",
        "TOTAL_BUDGET", "OUROBOROS_PER_TASK_COST_USD", "GITHUB_TOKEN", "GITHUB_REPO",
        "OUROBOROS_TOOL_TIMEOUT_SEC",
        "OUROBOROS_BG_MAX_ROUNDS", "OUROBOROS_BG_WAKEUP_MIN", "OUROBOROS_BG_WAKEUP_MAX",
        "OUROBOROS_EVO_COST_THRESHOLD", "OUROBOROS_WEBSEARCH_MODEL",
        "OUROBOROS_REVIEW_MODELS", "OUROBOROS_REVIEW_ENFORCEMENT",
        "OUROBOROS_SCOPE_REVIEW_MODEL",
        # Phase 2 runtime-mode + skills-repo plumbing (no runtime gating yet).
        "OUROBOROS_RUNTIME_MODE", "OUROBOROS_SKILLS_REPO_PATH",
        # v4.50+ ClawHub marketplace registry URL.
        "OUROBOROS_CLAWHUB_REGISTRY_URL",
        "OUROBOROS_EFFORT_TASK", "OUROBOROS_EFFORT_EVOLUTION",
        "OUROBOROS_EFFORT_REVIEW", "OUROBOROS_EFFORT_SCOPE_REVIEW",
        "OUROBOROS_EFFORT_CONSCIOUSNESS",
        "LOCAL_MODEL_SOURCE", "LOCAL_MODEL_FILENAME",
        "LOCAL_MODEL_PORT", "LOCAL_MODEL_N_GPU_LAYERS", "LOCAL_MODEL_CONTEXT_LENGTH",
        "LOCAL_MODEL_CHAT_FORMAT",
        "USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK",
        "OUROBOROS_FILE_BROWSER_DEFAULT",
        "A2A_ENABLED", "A2A_PORT", "A2A_HOST",
        "A2A_AGENT_NAME", "A2A_AGENT_DESCRIPTION",
        "A2A_MAX_CONCURRENT", "A2A_TASK_TTL_HOURS",
        # Local-LLM compatibility layer — Phase 1 master switch + profile
        # selection. Without these, settings.json values never reach the
        # agent's env and the compat layer stays dormant indefinitely.
        "OUROBOROS_COMPAT_ENABLED", "OUROBOROS_COMPAT_PROFILE",
        # Prompt-mode ladder (sparse/medium/dense) — agent.py reads this
        # per-task; without env propagation the profile-driven default
        # never engages.
        "OUROBOROS_PROMPT_MODE",
        # Resume-layer Phase 2 flag — auto-prepend task-checkpoint when
        # the user signals a continuation in the prompt.
        "OUROBOROS_RESUME_DETECTION",
        # Productivity-aware wall-clock + dedup escalation knobs.
        "OUROBOROS_WALL_BUDGET_SEC", "OUROBOROS_WALL_BUDGET_MAX_EXTENSIONS",
        "OUROBOROS_ESCALATION_DISABLED",
        # Phase-1 perf-audit telemetry + cache controls.
        "OUROBOROS_DEBUG_PROMPT_HASH", "OUROBOROS_DISABLE_CONTEXT_CACHE",
        # Bounded gate retries + checkpoint cadence overrides.
        "OUROBOROS_TOOL_DEDUP_CAP", "OUROBOROS_GATE_RETRY_CAP",
        "OUROBOROS_CHECKPOINT_INTERVAL",
        # Diff-aware preflight kill-switch.
        "OUROBOROS_PREFLIGHT_DIFF_AWARE",
        # OpenAI-compatible context-length explicit override (auto-probe
        # via /api/v0/models is the preferred path; this is the fallback).
        "OPENAI_COMPATIBLE_CONTEXT_LENGTH",
    ]
    for k in env_keys:
        val = settings.get(k)
        if val is None or val == "":
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(val)
    if not os.environ.get("OUROBOROS_REVIEW_MODELS"):
        os.environ["OUROBOROS_REVIEW_MODELS"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_MODELS"])
    if not os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT"):
        os.environ["OUROBOROS_REVIEW_ENFORCEMENT"] = str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"])


# ---------------------------------------------------------------------------
# PID lock (single instance) — crash-proof locking via ouroboros.platform_layer.
# On Unix the OS releases flock automatically when the process dies
# (even SIGKILL), so stale lock files can never block future launches.
# On Windows msvcrt.locking provides equivalent semantics.
# ---------------------------------------------------------------------------

def acquire_pid_lock() -> bool:
    APP_ROOT.mkdir(parents=True, exist_ok=True)
    return _compat_pid_lock_acquire(str(PID_FILE))


def release_pid_lock() -> None:
    _compat_pid_lock_release(str(PID_FILE))

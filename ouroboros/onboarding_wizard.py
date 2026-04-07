"""Shared onboarding wizard helpers for desktop and web."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, Tuple

from ouroboros.config import SETTINGS_DEFAULTS
from ouroboros.provider_models import (
    ANTHROPIC_DIRECT_DEFAULTS,
    CLOUDRU_DIRECT_DEFAULTS,
    OPENAI_DIRECT_DEFAULTS,
)

_ASSET_ROOT = pathlib.Path(__file__).resolve().parents[1] / "web"
_TEMPLATE_PATH = _ASSET_ROOT / "onboarding_template.html"
_CSS_PATH = _ASSET_ROOT / "onboarding.css"
_JS_PATH = _ASSET_ROOT / "modules" / "onboarding_wizard.js"

_OPENROUTER_MODEL_DEFAULTS = {
    "main": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL"]),
    "code": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL_CODE"]),
    "light": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL_LIGHT"]),
    "fallback": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL_FALLBACK"]),
}
_OPENAI_MODEL_DEFAULTS = dict(OPENAI_DIRECT_DEFAULTS)
_CLOUDRU_MODEL_DEFAULTS = dict(CLOUDRU_DIRECT_DEFAULTS)
_ANTHROPIC_MODEL_DEFAULTS = dict(ANTHROPIC_DIRECT_DEFAULTS)
_STEP_ORDER = ["providers", "models", "review_mode", "budget", "summary"]
_LOCAL_PRESETS: Dict[str, Dict[str, Any]] = {
    "qwen25-7b": {
        "label": "Qwen2.5-7B Instruct Q3_K_M",
        "source": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "filename": "qwen2.5-7b-instruct-q3_k_m.gguf",
        "context_length": 16384,
        "chat_format": "",
    },
    "qwen3-14b": {
        "label": "Qwen3-14B Instruct Q4_K_M",
        "source": "Qwen/Qwen3-14B-GGUF",
        "filename": "Qwen3-14B-Q4_K_M.gguf",
        "context_length": 16384,
        "chat_format": "",
    },
    "qwen3-32b": {
        "label": "Qwen3-32B Instruct Q4_K_M",
        "source": "Qwen/Qwen3-32B-GGUF",
        "filename": "Qwen3-32B-Q4_K_M.gguf",
        "context_length": 32768,
        "chat_format": "",
    },
}
_MODEL_SUGGESTIONS = [
    "anthropic/claude-opus-4.6",
    "anthropic/claude-sonnet-4.6",
    "anthropic::claude-opus-4-6",
    "anthropic::claude-sonnet-4-6",
    "google/gemini-3.1-pro-preview",
    "google/gemini-3-flash-preview",
    "openai/gpt-5.4",
    "openai::gpt-5.4",
    "openai::gpt-5.4-mini",
    "openai-compatible::meta-llama/compatible",
    "cloudru::GigaChat/GigaChat-2-Max",
]


def _string(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    return _string(value).lower() in {"1", "true", "yes", "on"}


def _float_setting(settings: dict, key: str, default: float) -> float:
    raw = settings.get(key, default)
    try:
        return float(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return float(default)


def _int_setting(settings: dict, key: str, default: int) -> int:
    raw = settings.get(key, default)
    try:
        return int(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return int(default)


def _read_asset(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _detect_local_preset(settings: dict) -> str:
    source = _string(settings.get("LOCAL_MODEL_SOURCE"))
    filename = _string(settings.get("LOCAL_MODEL_FILENAME"))
    if not source:
        return ""
    for preset_id, preset in _LOCAL_PRESETS.items():
        if source == preset["source"] and filename == preset["filename"]:
            return preset_id
    return "custom"


def _derive_provider_profile(settings: dict) -> str:
    has_openrouter = bool(_string(settings.get("OPENROUTER_API_KEY")))
    has_openai = bool(_string(settings.get("OPENAI_API_KEY")))
    has_anthropic = bool(_string(settings.get("ANTHROPIC_API_KEY")))
    has_cloudru = bool(_string(settings.get("CLOUDRU_FOUNDATION_MODELS_API_KEY")))
    has_local = bool(_string(settings.get("LOCAL_MODEL_SOURCE")))
    if has_openrouter:
        return "openrouter"
    if sum([has_openai, has_anthropic, has_cloudru]) > 1:
        return "direct-multi"
    if has_openai:
        return "openai"
    if has_anthropic:
        return "anthropic"
    if has_cloudru:
        return "cloudru"
    if has_local:
        return "local"
    return "openrouter"


def _derive_local_routing_mode(settings: dict) -> str:
    use_main = _truthy(settings.get("USE_LOCAL_MAIN"))
    use_code = _truthy(settings.get("USE_LOCAL_CODE"))
    use_light = _truthy(settings.get("USE_LOCAL_LIGHT"))
    use_fallback = _truthy(settings.get("USE_LOCAL_FALLBACK"))
    if use_main and use_code and use_light and use_fallback:
        return "all"
    if not use_main and not use_code and not use_light and use_fallback:
        return "fallback"
    return "cloud"


def _initial_models(settings: dict, provider_profile: str) -> dict:
    defaults = _OPENROUTER_MODEL_DEFAULTS
    if provider_profile == "openai":
        defaults = _OPENAI_MODEL_DEFAULTS
    elif provider_profile == "cloudru":
        defaults = _CLOUDRU_MODEL_DEFAULTS
    elif provider_profile == "anthropic":
        defaults = _ANTHROPIC_MODEL_DEFAULTS
    return {
        "main": _string(settings.get("OUROBOROS_MODEL")) or defaults["main"],
        "code": _string(settings.get("OUROBOROS_MODEL_CODE")) or defaults["code"],
        "light": _string(settings.get("OUROBOROS_MODEL_LIGHT")) or defaults["light"],
        "fallback": _string(settings.get("OUROBOROS_MODEL_FALLBACK")) or defaults["fallback"],
    }


def _bootstrap_local_presets() -> dict:
    out = {}
    for preset_id, preset in _LOCAL_PRESETS.items():
        out[preset_id] = {
            "label": preset["label"],
            "source": preset["source"],
            "filename": preset["filename"],
            "contextLength": int(preset["context_length"]),
            "chatFormat": str(preset["chat_format"]),
        }
    return out


def _build_bootstrap(settings: dict, host_mode: str) -> dict:
    provider_profile = _derive_provider_profile(settings)
    models = _initial_models(settings, provider_profile)
    return {
        "hostMode": host_mode,
        "supportsLocalRuntimeControls": host_mode == "web",
        "stepOrder": list(_STEP_ORDER),
        "modelDefaults": {
            "openrouter": dict(_OPENROUTER_MODEL_DEFAULTS),
            "openai": dict(_OPENAI_MODEL_DEFAULTS),
            "cloudru": dict(_CLOUDRU_MODEL_DEFAULTS),
            "anthropic": dict(_ANTHROPIC_MODEL_DEFAULTS),
            "local": dict(_OPENROUTER_MODEL_DEFAULTS),
        },
        "localPresets": _bootstrap_local_presets(),
        "modelSuggestions": list(_MODEL_SUGGESTIONS),
        "initialState": {
            "providerProfile": provider_profile,
            "openrouterKey": _string(settings.get("OPENROUTER_API_KEY")),
            "openaiKey": _string(settings.get("OPENAI_API_KEY")),
            "cloudruKey": _string(settings.get("CLOUDRU_FOUNDATION_MODELS_API_KEY")),
            "anthropicKey": _string(settings.get("ANTHROPIC_API_KEY")),
            "reviewEnforcement": _string(settings.get("OUROBOROS_REVIEW_ENFORCEMENT"))
            or str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"]),
            "totalBudget": _float_setting(
                settings,
                "TOTAL_BUDGET",
                float(SETTINGS_DEFAULTS["TOTAL_BUDGET"]),
            ),
            "perTaskCostUsd": _float_setting(
                settings,
                "OUROBOROS_PER_TASK_COST_USD",
                float(SETTINGS_DEFAULTS.get("OUROBOROS_PER_TASK_COST_USD", 20.0)),
            ),
            "localPreset": _detect_local_preset(settings),
            "localSource": _string(settings.get("LOCAL_MODEL_SOURCE")),
            "localFilename": _string(settings.get("LOCAL_MODEL_FILENAME")),
            "localContextLength": _int_setting(
                settings,
                "LOCAL_MODEL_CONTEXT_LENGTH",
                int(SETTINGS_DEFAULTS["LOCAL_MODEL_CONTEXT_LENGTH"]),
            ),
            "localGpuLayers": _int_setting(settings, "LOCAL_MODEL_N_GPU_LAYERS", -1),
            "localChatFormat": _string(settings.get("LOCAL_MODEL_CHAT_FORMAT")),
            "localRoutingMode": _derive_local_routing_mode(settings),
            "mainModel": models["main"],
            "codeModel": models["code"],
            "lightModel": models["light"],
            "fallbackModel": models["fallback"],
        },
    }


def build_onboarding_html(settings: dict, host_mode: str = "desktop") -> str:
    normalized_host_mode = "web" if host_mode == "web" else "desktop"
    bootstrap = _build_bootstrap(settings, normalized_host_mode)
    return (
        _read_asset(_TEMPLATE_PATH)
        .replace("__ONBOARDING_CSS__", _read_asset(_CSS_PATH))
        .replace("__ONBOARDING_BOOTSTRAP__", json.dumps(bootstrap, ensure_ascii=True))
        .replace("__ONBOARDING_JS__", _read_asset(_JS_PATH))
    )


def prepare_onboarding_settings(data: dict, current_settings: dict) -> Tuple[dict, str | None]:
    openrouter_key = _string(data.get("OPENROUTER_API_KEY"))
    openai_key = _string(data.get("OPENAI_API_KEY"))
    cloudru_key = _string(data.get("CLOUDRU_FOUNDATION_MODELS_API_KEY"))
    anthropic_key = _string(data.get("ANTHROPIC_API_KEY"))
    local_source = _string(data.get("LOCAL_MODEL_SOURCE"))
    local_filename = _string(data.get("LOCAL_MODEL_FILENAME"))
    local_chat_format = _string(data.get("LOCAL_MODEL_CHAT_FORMAT"))
    local_routing_mode = _string(data.get("LOCAL_ROUTING_MODE")) or "cloud"
    review_enforcement = _string(data.get("OUROBOROS_REVIEW_ENFORCEMENT")) or "advisory"

    if openrouter_key and len(openrouter_key) < 10:
        return {}, "OpenRouter API key looks too short."
    if openai_key and len(openai_key) < 10:
        return {}, "OpenAI API key looks too short."
    if cloudru_key and len(cloudru_key) < 10:
        return {}, "Cloud.ru Foundation Models API key looks too short."
    if anthropic_key and len(anthropic_key) < 10:
        return {}, "Anthropic API key looks too short."

    has_local = bool(local_source)
    if not openrouter_key and not openai_key and not cloudru_key and not anthropic_key and not has_local:
        return {}, "Configure OpenRouter, OpenAI, Cloud.ru, Anthropic, or a local model before continuing."

    if has_local and "/" in local_source and not local_source.startswith(("/", "~")) and not local_filename:
        return {}, "Local HuggingFace sources need a GGUF filename."

    if review_enforcement not in {"advisory", "blocking"}:
        return {}, "Choose advisory or blocking review mode."

    models = {
        "OUROBOROS_MODEL": _string(data.get("OUROBOROS_MODEL")),
        "OUROBOROS_MODEL_CODE": _string(data.get("OUROBOROS_MODEL_CODE")),
        "OUROBOROS_MODEL_LIGHT": _string(data.get("OUROBOROS_MODEL_LIGHT")),
        "OUROBOROS_MODEL_FALLBACK": _string(data.get("OUROBOROS_MODEL_FALLBACK")),
    }
    if not all(models.values()):
        return {}, "Confirm all four models before starting Ouroboros."

    try:
        total_budget = float(data.get("TOTAL_BUDGET") or SETTINGS_DEFAULTS["TOTAL_BUDGET"])
    except (TypeError, ValueError):
        return {}, "Budget must be a number."
    if total_budget <= 0:
        return {}, "Budget must be greater than zero."

    try:
        per_task_cost = float(
            data.get("OUROBOROS_PER_TASK_COST_USD")
            or SETTINGS_DEFAULTS.get("OUROBOROS_PER_TASK_COST_USD", 20.0)
        )
    except (TypeError, ValueError):
        return {}, "Per-task soft threshold must be a number."
    if per_task_cost <= 0:
        return {}, "Per-task soft threshold must be greater than zero."

    try:
        local_context_length = int(
            data.get("LOCAL_MODEL_CONTEXT_LENGTH")
            or SETTINGS_DEFAULTS["LOCAL_MODEL_CONTEXT_LENGTH"]
        )
        local_gpu_layers = int(
            data.get("LOCAL_MODEL_N_GPU_LAYERS")
            if data.get("LOCAL_MODEL_N_GPU_LAYERS") is not None
            else -1
        )
    except (TypeError, ValueError):
        return {}, "Local model context length and GPU layers must be integers."

    use_local = {
        "cloud": (False, False, False, False),
        "fallback": (False, False, False, True),
        "all": (True, True, True, True),
    }.get(local_routing_mode, (False, False, False, False))
    if not has_local:
        use_local = (False, False, False, False)
    if has_local and not openrouter_key and not openai_key and not cloudru_key and not anthropic_key and not any(use_local):
        return {}, "Local-only setups must route at least one model to the local runtime."

    prepared = dict(current_settings)
    prepared.update(models)
    prepared.update(
        {
            "OPENROUTER_API_KEY": openrouter_key,
            "OPENAI_API_KEY": openai_key,
            "CLOUDRU_FOUNDATION_MODELS_API_KEY": cloudru_key,
            "ANTHROPIC_API_KEY": anthropic_key,
            "TOTAL_BUDGET": total_budget,
            "OUROBOROS_PER_TASK_COST_USD": per_task_cost,
            "OUROBOROS_REVIEW_ENFORCEMENT": review_enforcement,
            "LOCAL_MODEL_SOURCE": local_source if has_local else "",
            "LOCAL_MODEL_FILENAME": local_filename if has_local else "",
            "LOCAL_MODEL_CONTEXT_LENGTH": local_context_length,
            "LOCAL_MODEL_N_GPU_LAYERS": local_gpu_layers,
            "LOCAL_MODEL_CHAT_FORMAT": local_chat_format if has_local else "",
            "USE_LOCAL_MAIN": use_local[0],
            "USE_LOCAL_CODE": use_local[1],
            "USE_LOCAL_LIGHT": use_local[2],
            "USE_LOCAL_FALLBACK": use_local[3],
        }
    )
    return prepared, None

"""Shared Settings/Onboarding setup contract and payload validation."""

from __future__ import annotations

from typing import Any, Dict, Tuple

from ouroboros.config import SETTINGS_DEFAULTS, VALID_RUNTIME_MODES
from ouroboros.provider_models import (
    ANTHROPIC_DIRECT_DEFAULTS,
    CLOUDRU_DIRECT_DEFAULTS,
    OPENAI_DIRECT_DEFAULTS,
)


def _rows(keys: tuple[str, ...], specs: tuple[tuple[Any, ...], ...]) -> list[dict]:
    rows = []
    for spec in specs:
        if len(spec) != len(keys):
            raise ValueError(f"setup contract row has {len(spec)} fields, expected {len(keys)}")
        rows.append(dict(zip(keys, spec)))
    return rows


_MODEL_DEFAULTS = {
    "openrouter": {
        "main": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL"]),
        "code": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL_CODE"]),
        "light": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL_LIGHT"]),
        "fallback": str(SETTINGS_DEFAULTS["OUROBOROS_MODEL_FALLBACK"]),
    },
    "openai": dict(OPENAI_DIRECT_DEFAULTS),
    "cloudru": dict(CLOUDRU_DIRECT_DEFAULTS),
    "anthropic": dict(ANTHROPIC_DIRECT_DEFAULTS),
}
_MODEL_DEFAULTS["local"] = dict(_MODEL_DEFAULTS["openrouter"])

_STEPS = _rows(("id", "title", "railCopy", "copy", "footer"), (
    ("providers", "Add your access", "Keys + local", "Fill at least one remote key or a local model source. The next step adapts to what you configured here.", "Paste only what you already have. OpenRouter, direct provider keys, and an optional local model can coexist."),
    ("models", "Choose models", "4 model slots", "Review the visible model defaults derived from your current setup, then edit anything you want before launch.", "Plain openai/... or anthropic/... remains router-style. Direct values use openai::... and anthropic::...."),
    ("review_mode", "Choose review mode", "Advisory vs blocking", "Decide how strict pre-commit review should be before Ouroboros starts modifying itself.", "Pick both review enforcement and the initial runtime mode before Ouroboros starts."),
    ("budget", "Set your budget", "Session limits", "Budget is its own step because it directly shapes how far Ouroboros can go in one session and in a single task.", "Total budget is global. Per-task cost cap is a soft reminder, not a hard kill switch."),
    ("summary", "Review before launch", "Final check", "Check the final provider, model, review, and budget picture. Ouroboros will save these onboarding values before starting.", "The same onboarding values remain editable later in Settings."),
))
_STEP_ORDER = [step["id"] for step in _STEPS]

_PROVIDER_FIELDS = _rows(("id", "stateKey", "settingKey", "settingsInputId", "label", "placeholder", "note"), (
    ("openrouter-key", "openrouterKey", "OPENROUTER_API_KEY", "s-openrouter", "OpenRouter API Key", "sk-or-v1-...", "Optional. Best when you want one router for OpenAI, Anthropic, Google, and more."),
    ("openai-key", "openaiKey", "OPENAI_API_KEY", "s-openai", "OpenAI API Key", "sk-...", "Optional. If this is the only remote key, the next step prefills direct openai::... models."),
    ("cloudru-key", "cloudruKey", "CLOUDRU_FOUNDATION_MODELS_API_KEY", "s-cloudru-key", "Cloud.ru Foundation Models API Key", "Cloud.ru API key", "Optional. If this is the only remote key, the next step prefills direct cloudru::... models."),
    ("anthropic-key", "anthropicKey", "ANTHROPIC_API_KEY", "s-anthropic", "Anthropic API Key", "sk-ant-...", "Optional. Saved for direct anthropic::... models and Claude tooling."),
))

_PROFILE_SPECS = {
    "openrouter": ("OpenRouter", "OpenRouter is present, so the next step keeps router-style defaults while still saving any extra direct keys you paste here.", "OpenRouter-style routing remains active. Unprefixed provider IDs like openai/gpt-5.5 or anthropic/claude-sonnet-4.6 continue to route through OpenRouter."),
    "openai": ("OpenAI", "OpenAI is present, so the next step prefills direct openai:: model values.", "OpenAI-only setup detected. These defaults are explicit and official."),
    "cloudru": ("Cloud.ru Foundation Models", "Cloud.ru is present, so the next step prefills direct cloudru:: model values.", "Cloud.ru-only setup detected. These defaults use explicit cloudru:: model IDs."),
    "anthropic": ("Anthropic", "Anthropic is present, so the next step prefills direct anthropic:: model values.", "Anthropic-only setup detected. These defaults are explicit and official."),
    "direct-multi": ("Direct multi-provider", "Multiple direct providers are present, so the next step keeps your model values editable without forcing one provider family.", "Multiple direct providers are configured. Start here, then split model slots across them if you want."),
    "local": ("Local-first", "No remote key is present yet, so local-only setup remains available below.", "Local-only setup detected. Review the model values and local routing before launch."),
}

_MODEL_SLOTS = _rows(("slot", "stateKey", "settingKey", "inputId", "label", "note", "settingsInputId", "settingsToggleId"), (
    ("main", "mainModel", "OUROBOROS_MODEL", "main-model", "Main Model", "Primary reasoning and long-form work.", "s-model", "s-local-main"),
    ("code", "codeModel", "OUROBOROS_MODEL_CODE", "code-model", "Code Model", "Tool-heavy coding and edits.", "s-model-code", "s-local-code"),
    ("light", "lightModel", "OUROBOROS_MODEL_LIGHT", "light-model", "Light Model", "Fast summaries and lightweight tasks.", "s-model-light", "s-local-light"),
    ("fallback", "fallbackModel", "OUROBOROS_MODEL_FALLBACK", "fallback-model", "Fallback Model", "Fallback and resilience path.", "s-model-fallback", "s-local-fallback"),
))

_REVIEW_MODES = _rows(("value", "label", "tone", "className", "copy"), (
    ("advisory", "Advisory", "Flexible", "advisory", "Faster and cheaper. Review still runs, but you decide how to handle findings. Best when you want iteration speed and can manually watch for drift."),
    ("blocking", "Blocking", "Strict", "blocking", "Slower and more expensive, but much safer. Critical review findings stop commits, which dramatically reduces the chance of gradual code degradation."),
))

_RUNTIME_MODES = _rows(("value", "label", "tone", "className", "copy"), (
    ("light", "Light", "Safest", "light", "Self-modification of the main repo is disabled. Best for trying Ouroboros out without repo self-modification."),
    ("advanced", "Advanced", "Default", "advanced", "Self-modification of the evolutionary layer is allowed (current behaviour). Protected core/contract/release files stay guarded by Advanced mode."),
    ("pro", "Pro", "Power", "pro", "Direct protected-surface mode. Protected core/contract/release edits are allowed on disk, but commits still require the normal triad + scope review gate."),
))

_LOCAL_ROUTING_MODES = _rows(("value", "buttonLabel", "label", "flags"), (
    ("cloud", "Cloud only", "Cloud models only", (False, False, False, False)),
    ("fallback", "Fallback local", "Fallback model local", (False, False, False, True)),
    ("all", "All models local", "All models local", (True, True, True, True)),
))

_BUDGET_FIELDS = [
    {"stateKey": "totalBudget", "settingKey": "TOTAL_BUDGET", "inputId": "total-budget", "title": "Total budget", "label": "Total Budget (USD)", "note": "Global spend budget across the runtime. Keep this editable even after onboarding.", "default": float(SETTINGS_DEFAULTS["TOTAL_BUDGET"])},
    {"stateKey": "perTaskCostUsd", "settingKey": "OUROBOROS_PER_TASK_COST_USD", "inputId": "per-task-budget", "title": "Per-task soft threshold", "label": "Per-task Cost Cap (USD)", "note": "This does not hard-stop the task. It injects a budget reminder when one task starts getting expensive.", "default": float(SETTINGS_DEFAULTS.get("OUROBOROS_PER_TASK_COST_USD", 20.0))},
]

_LOCAL_PRESETS: Dict[str, Dict[str, Any]] = {
    "qwen25-7b": {"label": "Qwen2.5-7B Instruct Q3_K_M", "source": "Qwen/Qwen2.5-7B-Instruct-GGUF", "filename": "qwen2.5-7b-instruct-q3_k_m.gguf", "contextLength": 16384, "chatFormat": ""},
    "qwen3-14b": {"label": "Qwen3-14B Instruct Q4_K_M", "source": "Qwen/Qwen3-14B-GGUF", "filename": "Qwen3-14B-Q4_K_M.gguf", "contextLength": 16384, "chatFormat": ""},
    "qwen3-32b": {"label": "Qwen3-32B Instruct Q4_K_M", "source": "Qwen/Qwen3-32B-GGUF", "filename": "Qwen3-32B-Q4_K_M.gguf", "contextLength": 32768, "chatFormat": ""},
}

_MODEL_SUGGESTIONS = list(dict.fromkeys(("google/gemini-3.5-flash", "anthropic/claude-sonnet-4.6", "anthropic/claude-opus-4.7", "anthropic/claude-opus-4.6", "anthropic::claude-opus-4-7", "anthropic::claude-opus-4-6", "anthropic::claude-sonnet-4-6", "openai/gpt-5.5", "openai::gpt-5.5", "openai::gpt-5.5-mini", "openai-compatible::meta-llama/compatible", "cloudru::zai-org/GLM-4.7")))


def _string(value: Any) -> str:
    return str(value or "").strip()


_truthy = lambda value: value is True or _string(value).lower() in {"1", "true", "yes", "on"}


def _number_setting(settings: dict, key: str, default: Any, cast: Any) -> Any:
    try:
        raw = settings.get(key, default)
        return cast(raw if raw not in (None, "") else default)
    except (TypeError, ValueError):
        return cast(default)


def derive_provider_profile(settings: dict) -> str:
    flags = {field["settingKey"]: bool(_string(settings.get(field["settingKey"]))) for field in _PROVIDER_FIELDS}
    if flags["OPENROUTER_API_KEY"]:
        return "openrouter"
    direct = [
        ("OPENAI_API_KEY", "openai"),
        ("CLOUDRU_FOUNDATION_MODELS_API_KEY", "cloudru"),
        ("ANTHROPIC_API_KEY", "anthropic"),
    ]
    configured = [name for key, name in direct if flags[key]]
    if len(configured) > 1:
        return "direct-multi"
    return configured[0] if configured else ("local" if _string(settings.get("LOCAL_MODEL_SOURCE")) else "openrouter")


def derive_local_routing_mode(settings: dict) -> str:
    flags = tuple(_truthy(settings.get(key)) for key in ("USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK"))
    if flags == (True, True, True, True):
        return "all"
    return "fallback" if flags == (False, False, False, True) else "cloud"


def local_routing_flags(mode: str, has_local: bool = True) -> tuple[bool, bool, bool, bool]:
    if not has_local:
        return (False, False, False, False)
    for item in _LOCAL_ROUTING_MODES:
        if item["value"] == mode:
            return tuple(bool(flag) for flag in item["flags"])  # type: ignore[return-value]
    return (False, False, False, False)


def model_defaults_for_profile(profile: str) -> dict:
    return dict(_MODEL_DEFAULTS.get(profile) or _MODEL_DEFAULTS["openrouter"])


def build_setup_contract(host_mode: str = "desktop") -> dict:
    return {
        "version": 1,
        "hostMode": "web" if host_mode == "web" else "desktop",
        "steps": [dict(item) for item in _STEPS],
        "providerFields": [dict(item) for item in _PROVIDER_FIELDS],
        "providerProfiles": {key: {"label": spec[0], "providerCopy": spec[1], "modelCopy": spec[2]} for key, spec in _PROFILE_SPECS.items()},
        "modelSlots": [dict(item) for item in _MODEL_SLOTS],
        "reviewModes": [dict(item) for item in _REVIEW_MODES],
        "runtimeModes": [dict(item) for item in _RUNTIME_MODES],
        "localRoutingModes": [dict(item) for item in _LOCAL_ROUTING_MODES],
        "budgetFields": [dict(item) for item in _BUDGET_FIELDS],
    }


def build_initial_setup_state(settings: dict, host_mode: str = "desktop") -> dict:
    profile = derive_provider_profile(settings)
    defaults = model_defaults_for_profile(profile)
    local_source = _string(settings.get("LOCAL_MODEL_SOURCE"))
    local_filename = _string(settings.get("LOCAL_MODEL_FILENAME"))
    local_preset = next(
        (preset_id for preset_id, preset in _LOCAL_PRESETS.items() if local_source == preset["source"] and local_filename == preset["filename"]),
        "custom" if local_source else "",
    )
    state = {
        "providerProfile": profile,
        "reviewEnforcement": _string(settings.get("OUROBOROS_REVIEW_ENFORCEMENT")) or str(SETTINGS_DEFAULTS["OUROBOROS_REVIEW_ENFORCEMENT"]),
        "runtimeMode": _string(settings.get("OUROBOROS_RUNTIME_MODE")) or str(SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"]),
        "skillsRepoPath": _string(settings.get("OUROBOROS_SKILLS_REPO_PATH")),
        "localPreset": local_preset,
        "localSource": local_source,
        "localFilename": local_filename,
        "localContextLength": _number_setting(settings, "LOCAL_MODEL_CONTEXT_LENGTH", int(SETTINGS_DEFAULTS["LOCAL_MODEL_CONTEXT_LENGTH"]), int),
        "localGpuLayers": _number_setting(settings, "LOCAL_MODEL_N_GPU_LAYERS", -1, int),
        "localChatFormat": _string(settings.get("LOCAL_MODEL_CHAT_FORMAT")),
        "localRoutingMode": derive_local_routing_mode(settings),
    }
    state.update({field["stateKey"]: _string(settings.get(field["settingKey"])) for field in _PROVIDER_FIELDS})
    state.update({field["stateKey"]: _number_setting(settings, field["settingKey"], field["default"], float) for field in _BUDGET_FIELDS})
    state.update({slot["stateKey"]: _string(settings.get(slot["settingKey"])) or defaults[slot["slot"]] for slot in _MODEL_SLOTS})
    return state


def build_setup_bootstrap(settings: dict, host_mode: str = "desktop") -> dict:
    normalized_host = "web" if host_mode == "web" else "desktop"
    return {
        "hostMode": normalized_host,
        "supportsLocalRuntimeControls": normalized_host == "web",
        "stepOrder": list(_STEP_ORDER),
        "modelDefaults": {key: dict(value) for key, value in _MODEL_DEFAULTS.items()},
        "localPresets": {key: dict(value) for key, value in _LOCAL_PRESETS.items()},
        "modelSuggestions": list(_MODEL_SUGGESTIONS),
        "contract": build_setup_contract(normalized_host),
        "initialState": build_initial_setup_state(settings, normalized_host),
    }


def validate_setup_payload(data: dict, current_settings: dict) -> Tuple[dict, str | None]:
    keys = {field["settingKey"]: _string(data.get(field["settingKey"])) for field in _PROVIDER_FIELDS}
    local_source = _string(data.get("LOCAL_MODEL_SOURCE"))
    local_filename = _string(data.get("LOCAL_MODEL_FILENAME"))
    local_chat_format = _string(data.get("LOCAL_MODEL_CHAT_FORMAT"))
    local_routing_mode = _string(data.get("LOCAL_ROUTING_MODE")) or "cloud"
    review_enforcement = _string(data.get("OUROBOROS_REVIEW_ENFORCEMENT")) or "advisory"
    raw_runtime_mode = _string(data.get("OUROBOROS_RUNTIME_MODE"))
    runtime_mode = raw_runtime_mode.lower() if raw_runtime_mode else _string(current_settings.get("OUROBOROS_RUNTIME_MODE")) or str(SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"])

    for field in _PROVIDER_FIELDS:
        value = keys[field["settingKey"]]
        if value and len(value) < 10:
            return {}, f"{field['label'].replace(' API Key', '')} API key looks too short."

    has_remote = any(keys.values())
    has_local = bool(local_source)
    if not has_remote and not has_local:
        return {}, "Configure OpenRouter, OpenAI, Cloud.ru, Anthropic, or a local model before continuing."
    if has_local and "/" in local_source and not local_source.startswith(("/", "~")) and not local_filename:
        return {}, "Local HuggingFace sources need a GGUF filename."
    if review_enforcement not in {"advisory", "blocking"}:
        return {}, "Choose advisory or blocking review mode."
    if runtime_mode not in VALID_RUNTIME_MODES:
        return {}, f"Choose a runtime mode from {sorted(VALID_RUNTIME_MODES)}."

    models = {slot["settingKey"]: _string(data.get(slot["settingKey"])) for slot in _MODEL_SLOTS}
    if not all(models.values()):
        return {}, "Confirm all four models before starting Ouroboros."

    parsed_budget: dict[str, float] = {}
    for field in _BUDGET_FIELDS:
        try:
            value = float(data.get(field["settingKey"]) or field["default"])
        except (TypeError, ValueError):
            return {}, "Budget must be a number." if field["settingKey"] == "TOTAL_BUDGET" else "Per-task soft threshold must be a number."
        if value <= 0:
            return {}, "Budget must be greater than zero." if field["settingKey"] == "TOTAL_BUDGET" else "Per-task soft threshold must be greater than zero."
        parsed_budget[field["settingKey"]] = value

    try:
        local_context_length = int(data.get("LOCAL_MODEL_CONTEXT_LENGTH") or SETTINGS_DEFAULTS["LOCAL_MODEL_CONTEXT_LENGTH"])
        local_gpu_layers = int(data.get("LOCAL_MODEL_N_GPU_LAYERS") if data.get("LOCAL_MODEL_N_GPU_LAYERS") is not None else -1)
    except (TypeError, ValueError):
        return {}, "Local model context length and GPU layers must be integers."

    use_local = local_routing_flags(local_routing_mode, has_local)
    if has_local and not has_remote and not any(use_local):
        return {}, "Local-only setups must route at least one model to the local runtime."

    prepared = dict(current_settings)
    prepared.update(models)
    prepared.update(keys)
    prepared.update(parsed_budget)
    prepared.update({
        "OUROBOROS_REVIEW_ENFORCEMENT": review_enforcement,
        "OUROBOROS_RUNTIME_MODE": runtime_mode,
        "OUROBOROS_SKILLS_REPO_PATH": _string(data.get("OUROBOROS_SKILLS_REPO_PATH")),
        "LOCAL_MODEL_SOURCE": local_source if has_local else "",
        "LOCAL_MODEL_FILENAME": local_filename if has_local else "",
        "LOCAL_MODEL_CONTEXT_LENGTH": local_context_length,
        "LOCAL_MODEL_N_GPU_LAYERS": local_gpu_layers,
        "LOCAL_MODEL_CHAT_FORMAT": local_chat_format if has_local else "",
        "USE_LOCAL_MAIN": use_local[0],
        "USE_LOCAL_CODE": use_local[1],
        "USE_LOCAL_LIGHT": use_local[2],
        "USE_LOCAL_FALLBACK": use_local[3],
    })
    return prepared, None

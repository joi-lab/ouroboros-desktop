"""Provider-specific model ID helpers and direct-provider defaults."""

from __future__ import annotations

OPENAI_DIRECT_DEFAULTS = {
    "main": "openai::gpt-5.4",
    "code": "openai::gpt-5.4",
    "light": "openai::gpt-5.4-mini",
    "fallback": "openai::gpt-5.4-mini",
}

CLOUDRU_DIRECT_DEFAULTS = {
    "main": "cloudru::GigaChat/GigaChat-2-Max",
    "code": "cloudru::GigaChat/GigaChat-2-Max",
    "light": "cloudru::GigaChat/GigaChat-2-Max",
    "fallback": "cloudru::GigaChat/GigaChat-2-Max",
}

ANTHROPIC_DIRECT_DEFAULTS = {
    "main": "anthropic::claude-opus-4-6",
    "code": "anthropic::claude-opus-4-6",
    "light": "anthropic::claude-sonnet-4-6",
    "fallback": "anthropic::claude-sonnet-4-6",
}

_ANTHROPIC_MODEL_ALIASES = {
    "claude-opus-4.6": "claude-opus-4-6",
    "claude-sonnet-4.6": "claude-sonnet-4-6",
}


def normalize_anthropic_model_id(model_id: str) -> str:
    text = str(model_id or "").strip()
    return _ANTHROPIC_MODEL_ALIASES.get(text, text)


def migrate_model_value(provider: str, value: str) -> str:
    text = str(value or "").strip()
    if provider == "openai":
        if text.startswith("openai/"):
            return f"openai::{text[len('openai/'):]}"
        return text
    if provider == "anthropic":
        if text.startswith("anthropic::"):
            return f"anthropic::{normalize_anthropic_model_id(text[len('anthropic::'):])}"
        if text.startswith("anthropic/"):
            return f"anthropic::{normalize_anthropic_model_id(text[len('anthropic/'):])}"
        return text
    return text


def normalize_model_identity(model: str) -> str:
    text = str(model or "").strip()
    if text.endswith(" (local)"):
        text = text[:-8]
    if text.startswith("openai::"):
        return f"openai/{text[len('openai::'):]}"
    if text.startswith("openai-compatible::"):
        return f"openai-compatible/{text[len('openai-compatible::'):]}"
    if text.startswith("cloudru::"):
        return f"cloudru/{text[len('cloudru::'):]}"
    if text.startswith("anthropic::"):
        return f"anthropic/{normalize_anthropic_model_id(text[len('anthropic::'):])}"
    if text.startswith("anthropic/"):
        return f"anthropic/{normalize_anthropic_model_id(text[len('anthropic/'):])}"
    return text

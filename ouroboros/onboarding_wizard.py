"""Shared onboarding wizard helpers for desktop and web."""

from __future__ import annotations

import json
import pathlib
from typing import Tuple

from ouroboros.settings_setup_contract import (
    build_setup_bootstrap,
    validate_setup_payload,
)

_ASSET_ROOT = pathlib.Path(__file__).resolve().parents[1] / "web"
_TEMPLATE_PATH = _ASSET_ROOT / "onboarding_template.html"
_CSS_PATH = _ASSET_ROOT / "onboarding.css"
_JS_PATH = _ASSET_ROOT / "modules" / "onboarding_wizard.js"


def _read_asset(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def build_onboarding_html(settings: dict, host_mode: str = "desktop") -> str:
    normalized_host_mode = "web" if host_mode == "web" else "desktop"
    bootstrap = build_setup_bootstrap(settings, normalized_host_mode)
    return (
        _read_asset(_TEMPLATE_PATH)
        .replace("__ONBOARDING_CSS__", _read_asset(_CSS_PATH))
        .replace("__ONBOARDING_BOOTSTRAP__", json.dumps(bootstrap, ensure_ascii=True))
        .replace("__ONBOARDING_JS__", _read_asset(_JS_PATH))
    )


def prepare_onboarding_settings(data: dict, current_settings: dict) -> Tuple[dict, str | None]:
    return validate_setup_payload(data, current_settings)

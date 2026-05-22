"""UI render schema validation for extension widgets/settings."""

from __future__ import annotations

import copy
from typing import Any, Dict

from ouroboros.contracts.plugin_api import ExtensionRegistrationError, VALID_EXTENSION_ROUTE_METHODS

_EXTENSION_SHORT_MAX = 24
_UI_RENDER_KINDS = {"", "iframe", "declarative", "module"}
_DECLARATIVE_WIDGET_COMPONENTS = {
    "action", "audio", "chart", "code", "file", "form", "gallery", "image",
    "json", "kv", "key_value", "markdown", "poll", "progress", "status",
    "stream", "subscription", "tabs", "table", "video",
    # Host-owned declarative components; no skill-supplied JS.
    "map", "calendar", "kanban",
}
_INTERACTIVE_CHILD_TYPES = {"form", "action", "poll", "subscription", "stream", "tabs"}
_MEDIA_TYPES = {"image", "audio", "video", "file"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _assert_ws_message_type(message_type: str) -> str:
    candidate = _text(message_type)
    if not candidate:
        raise ExtensionRegistrationError("ws message_type must be non-empty")
    if len(candidate) > _EXTENSION_SHORT_MAX:
        raise ExtensionRegistrationError(f"ws message_type must be <= {_EXTENSION_SHORT_MAX} characters: {candidate!r}")
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(f"ws message_type must be alnum/underscore only: {candidate!r}")
    return candidate


def _validate_component(component: Dict[str, Any], idx: int, schema_version: int) -> None:
    component_type = _text(component.get("type"))
    if component_type not in _DECLARATIVE_WIDGET_COMPONENTS:
        raise ExtensionRegistrationError(f"declarative widget component {idx} has unsupported type {component_type!r}")
    if component_type in {"form", "action", "poll", "stream"} and not _text(component.get("route") or component.get("api_route")):
        raise ExtensionRegistrationError(f"declarative widget component {idx} requires route or api_route")
    if component_type == "subscription":
        event_name = _text(component.get("event") or component.get("message_type"))
        if not event_name:
            raise ExtensionRegistrationError(f"declarative widget component {idx} requires event or message_type")
        _assert_ws_message_type(event_name)
        render_children = component.get("render", [])
        if render_children is not None and not isinstance(render_children, list):
            raise ExtensionRegistrationError(f"declarative widget component {idx} subscription render must be a list")
        if render_children:
            for child_idx, child in enumerate(render_children):
                child_type = _text(child.get("type")) if isinstance(child, dict) else ""
                if child_type in _INTERACTIVE_CHILD_TYPES:
                    raise ExtensionRegistrationError(f"declarative widget component {idx} subscription child {child_idx} cannot use interactive type {child_type!r}")
            validate_ui_render({"kind": "declarative", "schema_version": schema_version, "components": render_children})
    if component_type == "tabs":
        tabs = component.get("tabs")
        if not isinstance(tabs, list) or not tabs:
            raise ExtensionRegistrationError(f"declarative widget component {idx} requires non-empty tabs[]")
        for tab_idx, tab in enumerate(tabs):
            if not isinstance(tab, dict) or not _text(tab.get("label")):
                raise ExtensionRegistrationError(f"declarative widget component {idx} tab {tab_idx} requires label")
            tab_components = tab.get("components", [])
            if not isinstance(tab_components, list):
                raise ExtensionRegistrationError(f"declarative widget component {idx} tab {tab_idx} components must be a list")
            for child_idx, child in enumerate(tab_components):
                child_type = _text(child.get("type")) if isinstance(child, dict) else ""
                if child_type in _INTERACTIVE_CHILD_TYPES:
                    raise ExtensionRegistrationError(f"declarative widget component {idx} tab {tab_idx} child {child_idx} cannot use interactive type {child_type!r}")
            validate_ui_render({"kind": "declarative", "schema_version": schema_version, "components": tab_components})
    method = _text(component.get("method") or "GET").upper()
    if method not in VALID_EXTENSION_ROUTE_METHODS:
        raise ExtensionRegistrationError(f"declarative widget component {idx} has unsupported method {method!r}")
    if component_type == "stream" and method != "GET":
        raise ExtensionRegistrationError(f"declarative widget component {idx} stream method must be GET")
    for label, key in (("fields", "name"),) if component_type == "form" else (("fields", "path"),) if component_type == "kv" else (("columns", "path"),) if component_type == "table" else ():
        fields = component.get(label)
        if not isinstance(fields, list) or not fields:
            raise ExtensionRegistrationError(f"declarative widget component {idx} requires non-empty {label}[]")
        for field_idx, field in enumerate(fields):
            if not isinstance(field, dict) or not _text(field.get(key)):
                raise ExtensionRegistrationError(f"declarative widget component {idx} field {field_idx} requires {key}")
    if component_type == "key_value" and not _text(component.get("items_key") or component.get("path")):
        raise ExtensionRegistrationError(f"declarative widget component {idx} key_value requires items_key or path")
    if component_type in _MEDIA_TYPES and not any(_text(component.get(key)) for key in ("route", "api_route", "src", "path")):
        raise ExtensionRegistrationError(f"declarative widget component {idx} requires media source")
    if component_type == "gallery":
        if "items" in component and not isinstance(component.get("items"), list):
            raise ExtensionRegistrationError(f"declarative widget component {idx} items must be a list")
        for item_idx, item in enumerate(component.get("items") or []):
            if not isinstance(item, dict):
                raise ExtensionRegistrationError(f"declarative widget component {idx} item {item_idx} must be an object")
            item_type = _text(item.get("type") or "image")
            if item_type not in _MEDIA_TYPES:
                raise ExtensionRegistrationError(f"declarative widget component {idx} item {item_idx} has unsupported type {item_type!r}")
            if not any(_text(item.get(key)) for key in ("route", "api_route", "src", "path")):
                raise ExtensionRegistrationError(f"declarative widget component {idx} item {item_idx} requires media source")
    if component_type == "map":
        tiles_url = _text(component.get("tiles_url"))
        if tiles_url and not (tiles_url.startswith("https://") or tiles_url.startswith("http://localhost") or tiles_url.startswith("http://127.")):
            raise ExtensionRegistrationError(f"declarative widget component {idx} map tiles_url must be https or local")
        markers = component.get("markers")
        if markers is not None and not isinstance(markers, list):
            raise ExtensionRegistrationError(f"declarative widget component {idx} map markers must be a list")
        for marker_idx, marker in enumerate(markers or []):
            if not isinstance(marker, dict):
                raise ExtensionRegistrationError(f"declarative widget component {idx} marker {marker_idx} must be an object")
            try:
                float(marker.get("lat"))
                float(marker.get("lon"))
            except (TypeError, ValueError) as exc:
                raise ExtensionRegistrationError(f"declarative widget component {idx} marker {marker_idx} requires numeric lat/lon") from exc
    if component_type == "calendar":
        items = component.get("items")
        if items is not None and not isinstance(items, list):
            raise ExtensionRegistrationError(f"declarative widget component {idx} calendar items must be a list")
    if component_type == "kanban":
        columns = component.get("columns")
        if not isinstance(columns, list) or not columns:
            raise ExtensionRegistrationError(f"declarative widget component {idx} kanban requires non-empty columns[]")
        for col_idx, col in enumerate(columns):
            if not isinstance(col, dict) or not _text(col.get("id") or col.get("label")):
                raise ExtensionRegistrationError(f"declarative widget component {idx} kanban column {col_idx} requires id+label")
        if "on_move" in component:
            on_move = component.get("on_move")
            if not isinstance(on_move, dict) or not _text(on_move.get("route")):
                raise ExtensionRegistrationError(f"declarative widget component {idx} kanban on_move requires {{route}}")


def validate_ui_render(render: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the browser-hosted widget declaration surface."""
    if not isinstance(render, dict):
        raise ExtensionRegistrationError("ui render must be an object")
    clean = copy.deepcopy(render)
    kind = _text(clean.get("kind"))
    if kind not in _UI_RENDER_KINDS:
        raise ExtensionRegistrationError(f"ui render kind {kind!r} is unsupported; expected one of {sorted(_UI_RENDER_KINDS - {''})}")
    if kind == "module":
        entry = _text(clean.get("entry"))
        if not entry:
            raise ExtensionRegistrationError("module widget render requires entry filename (e.g. 'widget.js')")
        if "/" in entry or ".." in entry or entry.startswith(".") or entry.endswith("/"):
            raise ExtensionRegistrationError(f"module widget entry {entry!r} must be a bare filename inside the skill directory")
        if not entry.endswith((".js", ".mjs")):
            raise ExtensionRegistrationError("module widget entry must be a .js / .mjs file")
        return clean
    if kind == "declarative":
        try:
            schema_version = int(clean.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise ExtensionRegistrationError("declarative widget schema_version must be 1") from exc
        if schema_version != 1:
            raise ExtensionRegistrationError("declarative widget schema_version must be 1")
        components = clean.get("components")
        if not isinstance(components, list):
            raise ExtensionRegistrationError("declarative widget render requires components[]")
        for idx, component in enumerate(components):
            if not isinstance(component, dict):
                raise ExtensionRegistrationError(f"declarative widget component {idx} must be an object")
            _validate_component(component, idx, schema_version)
        return clean
    return clean


__all__ = ["validate_ui_render"]

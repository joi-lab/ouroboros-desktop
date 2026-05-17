"""UI render schema validation for extension widgets/settings."""

from __future__ import annotations

from typing import Any, Dict

from ouroboros.contracts.plugin_api import (
    ExtensionRegistrationError,
    VALID_EXTENSION_ROUTE_METHODS,
)

_EXTENSION_SHORT_MAX = 24


def _assert_ws_message_type(message_type: str) -> str:
    candidate = str(message_type or "").strip()
    if not candidate:
        raise ExtensionRegistrationError("ws message_type must be non-empty")
    if len(candidate) > _EXTENSION_SHORT_MAX:
        raise ExtensionRegistrationError(
            f"ws message_type must be <= {_EXTENSION_SHORT_MAX} characters: {candidate!r}"
        )
    if not candidate.replace("_", "").isalnum():
        raise ExtensionRegistrationError(
            f"ws message_type must be alnum/underscore only: {candidate!r}"
        )
    return candidate

_UI_RENDER_KINDS = {"", "iframe", "declarative", "module"}
_DECLARATIVE_WIDGET_COMPONENTS = {
    "action",
    "audio",
    "chart",
    "code",
    "file",
    "form",
    "gallery",
    "image",
    "json",
    "kv",
    "key_value",
    "markdown",
    "poll",
    "progress",
    "status",
    "stream",
    "subscription",
    "tabs",
    "table",
    "video",
    # v5.7.0 additions: host-owned declarative components for richer
    # widget surfaces. None of these add ``kind: "module"`` JS — they
    # are still pure declarative schemas that the browser renders with
    # vetted host code (Leaflet for ``map``, host SVG for ``calendar``,
    # HTML5 drag API for ``kanban``).
    "map",
    "calendar",
    "kanban",
}


def validate_ui_render(render: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the browser-hosted widget declaration surface."""
    if not isinstance(render, dict):
        raise ExtensionRegistrationError("ui render must be an object")
    clean = dict(render)
    kind = str(clean.get("kind") or "").strip()
    if kind not in _UI_RENDER_KINDS:
        raise ExtensionRegistrationError(
            f"ui render kind {kind!r} is unsupported; "
            f"expected one of {sorted(_UI_RENDER_KINDS - {''})}"
        )
    if kind == "module":
        # v5.7.0: ``kind: "module"`` lets a reviewed extension supply its
        # own widget.js. The host renderer mounts it inside a sandboxed
        # ``<iframe srcdoc>`` with a strict CSP so the script cannot
        # touch ``document.cookie`` / ``localStorage`` of the SPA origin
        # and can only ``fetch`` back into ``/api/extensions/<skill>/``.
        # The ``widget_module_safety`` review item enforces source-level
        # discipline; this validator only rejects pathological declarations.
        entry = str(clean.get("entry") or "").strip()
        if not entry:
            raise ExtensionRegistrationError(
                "module widget render requires entry filename (e.g. 'widget.js')"
            )
        if "/" in entry or ".." in entry or entry.startswith(".") or entry.endswith("/"):
            raise ExtensionRegistrationError(
                f"module widget entry {entry!r} must be a bare filename inside the skill directory"
            )
        if not entry.endswith(".js") and not entry.endswith(".mjs"):
            raise ExtensionRegistrationError(
                "module widget entry must be a .js / .mjs file"
            )
        return clean
    if kind == "declarative":
        try:
            schema_version = int(clean.get("schema_version", 1))
        except (TypeError, ValueError) as exc:
            raise ExtensionRegistrationError(
                "declarative widget schema_version must be 1"
            ) from exc
        if schema_version != 1:
            raise ExtensionRegistrationError(
                "declarative widget schema_version must be 1"
            )
        components = clean.get("components")
        if not isinstance(components, list):
            raise ExtensionRegistrationError(
                "declarative widget render requires components[]"
            )
        for idx, component in enumerate(components):
            if not isinstance(component, dict):
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} must be an object"
                )
            component_type = str(component.get("type") or "").strip()
            if component_type not in _DECLARATIVE_WIDGET_COMPONENTS:
                raise ExtensionRegistrationError(
                    "declarative widget component "
                    f"{idx} has unsupported type {component_type!r}"
                )
            if (
                component_type in {"form", "action", "poll"}
                and not str(component.get("route") or component.get("api_route") or "").strip()
            ):
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} requires route or api_route"
                )
            if component_type == "subscription":
                event_name = str(component.get("event") or component.get("message_type") or "").strip()
                if not event_name:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires event or message_type"
                    )
                _assert_ws_message_type(event_name)
                render_children = component.get("render", [])
                if render_children is not None and not isinstance(render_children, list):
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} subscription render must be a list"
                    )
                for child_idx, child in enumerate(render_children or []):
                    child_type = str((child or {}).get("type") or "") if isinstance(child, dict) else ""
                    if child_type in {"form", "action", "poll", "subscription", "stream", "tabs"}:
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} subscription child {child_idx} "
                            f"cannot use interactive type {child_type!r}"
                        )
                if render_children:
                    validate_ui_render({
                        "kind": "declarative",
                        "schema_version": schema_version,
                        "components": render_children,
                    })
            if component_type == "stream" and not str(component.get("route") or component.get("api_route") or "").strip():
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} requires route or api_route"
                )
            if component_type == "tabs":
                tabs = component.get("tabs")
                if not isinstance(tabs, list) or not tabs:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty tabs[]"
                    )
                for tab_idx, tab in enumerate(tabs):
                    if not isinstance(tab, dict) or not str(tab.get("label") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} tab {tab_idx} requires label"
                        )
                    tab_components = tab.get("components", [])
                    if not isinstance(tab_components, list):
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} tab {tab_idx} components must be a list"
                        )
                    for child_idx, child in enumerate(tab_components):
                        child_type = str((child or {}).get("type") or "") if isinstance(child, dict) else ""
                        if child_type in {"form", "action", "poll", "subscription", "stream", "tabs"}:
                            raise ExtensionRegistrationError(
                                f"declarative widget component {idx} tab {tab_idx} child {child_idx} "
                                f"cannot use interactive type {child_type!r}"
                            )
                    validate_ui_render({
                        "kind": "declarative",
                        "schema_version": schema_version,
                        "components": tab_components,
                    })
            method = str(component.get("method") or "GET").upper()
            if method not in VALID_EXTENSION_ROUTE_METHODS:
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} has unsupported method {method!r}"
                )
            if component_type == "stream" and method != "GET":
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} stream method must be GET"
                )
            if component_type == "form":
                fields = component.get("fields")
                if not isinstance(fields, list) or not fields:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty fields[]"
                    )
                for field_idx, field in enumerate(component.get("fields") or []):
                    if not isinstance(field, dict) or not str(field.get("name") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} field {field_idx} requires name"
                        )
            if component_type == "kv":
                fields = component.get("fields")
                if not isinstance(fields, list) or not fields:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty fields[]"
                    )
                for field_idx, field in enumerate(component.get("fields") or []):
                    if not isinstance(field, dict) or not str(field.get("path") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} field {field_idx} requires path"
                        )
            if component_type == "key_value" and not str(component.get("items_key") or component.get("path") or "").strip():
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} key_value requires items_key or path"
                )
            if component_type == "table":
                columns = component.get("columns")
                if not isinstance(columns, list) or not columns:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires non-empty columns[]"
                    )
                for col_idx, column in enumerate(component.get("columns") or []):
                    if not isinstance(column, dict) or not str(column.get("path") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} column {col_idx} requires path"
                        )
            if component_type in {"image", "audio", "video", "file"}:
                has_media_source = any(
                    str(component.get(key) or "").strip()
                    for key in ("route", "api_route", "src", "path")
                )
                if not has_media_source:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} requires media source"
                    )
            if component_type == "gallery" and "items" in component and not isinstance(component.get("items"), list):
                raise ExtensionRegistrationError(
                    f"declarative widget component {idx} items must be a list"
                )
            if component_type == "gallery":
                for item_idx, item in enumerate(component.get("items") or []):
                    if not isinstance(item, dict):
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} item {item_idx} must be an object"
                        )
                    item_type = str(item.get("type") or "image").strip()
                    if item_type not in {"image", "audio", "video", "file"}:
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} item {item_idx} has unsupported type {item_type!r}"
                        )
                    has_media_source = any(
                        str(item.get(key) or "").strip()
                        for key in ("route", "api_route", "src", "path")
                    )
                    if not has_media_source:
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} item {item_idx} requires media source"
                        )
            # v5.7.0: host-owned schemas for map / calendar / kanban.
            # All three are declarative-only — no skill-supplied JS, no
            # cross-origin fetches, the renderer is vetted host code.
            if component_type == "map":
                tiles_url = str(component.get("tiles_url") or "").strip()
                # Be permissive: ``tiles_url`` is optional (renderer falls
                # back to OpenStreetMap defaults) but if supplied it must
                # be https for non-local tiles.
                if tiles_url and not (tiles_url.startswith("https://") or tiles_url.startswith("http://localhost") or tiles_url.startswith("http://127.")):
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} map tiles_url must be https or local"
                    )
                markers = component.get("markers")
                if markers is not None and not isinstance(markers, list):
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} map markers must be a list"
                    )
                for m_idx, marker in enumerate(markers or []):
                    if not isinstance(marker, dict):
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} marker {m_idx} must be an object"
                        )
                    try:
                        float(marker.get("lat"))
                        float(marker.get("lon"))
                    except (TypeError, ValueError) as exc:
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} marker {m_idx} requires numeric lat/lon"
                        ) from exc
            if component_type == "calendar":
                items = component.get("items")
                if items is not None and not isinstance(items, list):
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} calendar items must be a list"
                    )
            if component_type == "kanban":
                columns = component.get("columns")
                if not isinstance(columns, list) or not columns:
                    raise ExtensionRegistrationError(
                        f"declarative widget component {idx} kanban requires non-empty columns[]"
                    )
                for col_idx, col in enumerate(columns):
                    if not isinstance(col, dict) or not str(col.get("id") or col.get("label") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} kanban column {col_idx} requires id+label"
                        )
                if "on_move" in component:
                    on_move = component.get("on_move")
                    if not isinstance(on_move, dict) or not str(on_move.get("route") or "").strip():
                        raise ExtensionRegistrationError(
                            f"declarative widget component {idx} kanban on_move requires {{route}}"
                        )
    return clean


__all__ = ["validate_ui_render"]

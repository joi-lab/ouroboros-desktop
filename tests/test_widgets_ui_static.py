"""Static contract checks for the Widgets page renderer."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _widgets_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "widgets.js").read_text(
        encoding="utf-8"
    )


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def test_weather_declarative_paths_match_route_payload():
    plugin = (REPO_ROOT / "skills" / "weather" / "plugin.py").read_text(encoding="utf-8")
    skill = (REPO_ROOT / "skills" / "weather" / "SKILL.md").read_text(encoding="utf-8")
    for path in ("resolved_to", "temp_c", "feels_like_c", "condition", "humidity_pct", "wind_kph", "wind_dir"):
        assert f'"{path}":' in plugin
        assert f"path: {path}" in skill
    assert "path: humidity\n" not in skill
    assert "path: wind\n" not in skill


def test_widgets_support_declarative_schema_components():
    """Spot-check that widgets.js exposes the declarative schema entry point
    and a representative set of components. Trimmed in v5.15.x — the full
    type-marker enumeration (15+ entries) was brittle to schema evolution
    and added little signal over a smoke check. Security/lifecycle pins
    moved to the dedicated tests below (escape/sanitize, media source guard,
    download host helper, etc.)."""
    source = _widgets_js()
    assert "render.kind === 'declarative'" in source
    # Sentinel components — proof the declarative router is wired
    assert "type === 'form'" in source
    assert "type === 'action'" in source
    assert "type === 'table'" in source
    assert "type === 'markdown'" in source
    # Lifecycle / cleanup discipline
    assert "disposeMountedWidgets();" in source
    assert "let widgetsMounted = false;" in source
    assert "let renderGeneration = 0;" in source
    page_shown_branch = source.split("window.addEventListener('ouro:page-shown'")[1]
    assert "disposeMountedWidgets();" in page_shown_branch


def test_widgets_escape_and_sanitize_untrusted_content():
    """Widgets must reach the sanitised markdown helper through the v5.8.3-rc.5
    SSOT (``web/modules/utils.js::renderMarkdownSafe``); the DOMPurify
    allowlist itself moved to that module and is pinned by
    ``tests/test_web_utils_ssot.py::test_render_markdown_safe_strips_dangerous_tags_and_attrs``.
    Widgets-side this test now only verifies the import and the
    escapeHtml-around-untrusted-content discipline that remains local
    (table cells, JSON dumps).
    """
    source = _widgets_js()
    assert "renderMarkdownSafe" in source
    # Widgets must NOT redeclare the SSOT helper locally.
    assert "function renderMarkdownSafe" not in source, (
        "widgets.js must use renderMarkdownSafe from utils.js (SSOT), not a local copy"
    )
    assert "escapeHtml(JSON.stringify(value, null, 2))" in source
    assert "escapeHtml(getPath(row, c.path, ''))" in source


def test_widgets_media_sources_are_constrained_to_extension_routes_or_data_urls():
    source = _widgets_js()
    assert "function safeMediaSrc" in source
    assert "const route = spec.route || spec.api_route || '';" in source
    assert "extensionRoutePath(tab.skill, route, params)" in source
    assert "data:(image\\/" in source
    assert "parsed.pathname.startsWith(expectedPrefix)" in source
    assert "parsed.origin === window.location.origin" in source
    assert "javascript:" not in source


def test_widgets_downloads_use_host_handler_not_navigation():
    source = _widgets_js()
    helper = _read("web/modules/ui_helpers.js")
    assert "data-widget-download-url" in source
    assert "event.preventDefault();" in source
    assert "downloadViaHostBridge(" in source
    assert "download_file_to_downloads" in helper
    assert "URL.createObjectURL" in helper
    assert "window.location.href" not in source
    assert "window.location.assign" not in source
    assert '<a class="btn btn-default" href' not in source


def test_widgets_treat_head_as_no_body_request():
    source = _widgets_js()
    assert "const noBody = method === 'GET' || method === 'HEAD';" in source
    assert "const init = noBody" in source


def test_widgets_keep_iframe_sandbox_locked_down():
    """The legacy ``kind: "iframe"`` widget surface mounts an extension
    route inside a <iframe> with the *empty* sandbox attribute (no
    permissions at all). v5.7.0 added ``kind: "module"``, which mounts
    extension-supplied JS inside a separate <iframe srcdoc> with
    ``sandbox="allow-scripts"`` BUT no ``allow-same-origin`` token —
    so the iframe is still an opaque origin (no SPA cookie / storage
    access) and is further constrained by a strict CSP. We check both
    invariants here:

    1. The legacy iframe path still uses the empty sandbox.
    2. The module iframe path adds ``allow-scripts`` but never adds
       ``allow-same-origin`` (the only token that would re-expose
       parent storage).
    """
    source = _widgets_js()
    assert 'sandbox=""' in source
    # ``allow-scripts`` is now legitimately present, but only inside the
    # ``kind === 'module'`` branch. The dangerous combined sandbox token
    # must never appear in an actual iframe attribute.
    assert 'sandbox="allow-scripts"' in source
    assert 'sandbox="allow-scripts allow-same-origin"' not in source
    assert 'sandbox="allow-scripts allow-forms allow-same-origin"' not in source
    assert "render.kind === 'module'" in source
    # Verify the module iframe carries a CSP that does NOT grant network
    # access directly. The parent injects a postMessage fetch bridge instead,
    # restricted to /api/extensions/<skill>/... from the parent side.
    assert "default-src 'none'" in source
    assert "script-src 'unsafe-inline'" in source
    assert "OuroborosWidget = { fetch: window.fetch }" in source
    assert "module widget fetch outside extension route prefix" in source


def test_widgets_use_design_radius_tokens():
    style = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    block_start = style.index(".widget-field input,")
    block_end = style.index("}", block_start)
    block = style[block_start:block_end]
    assert "border-radius: var(--radius-sm);" in block
    assert "border-radius: 9px;" not in block


def test_widgets_refresh_button_shows_loading_state():
    source = _widgets_js()
    css = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")

    assert "refreshBtn.classList.add('is-loading')" in source
    assert "refreshBtn.classList.remove('is-loading')" in source
    assert "refreshBtn.disabled = true" in source
    assert "#widgets-refresh.is-loading::after" in css


def test_widgets_cards_do_not_stretch_to_row_height():
    source = _widgets_js()
    css = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    masonry = (REPO_ROOT / "web" / "modules" / "masonry.js").read_text(encoding="utf-8")
    assert "const span = Number(tab.span || tab.grid_span || 1);" in source
    assert "widgets-card-span-2" in source
    assert "applyMasonry(list)" in source
    assert "function layout(container, config)" in masonry
    assert "itemResizeObserver" in masonry
    assert "observeItems()" in masonry
    widgets_block = css.split(".widgets-list {", 1)[1].split("}", 1)[0]
    assert "display: grid" not in widgets_block
    assert "position: relative;" in widgets_block
    assert ".widgets-card-span-2" in css


def test_widgets_inline_card_host_path_removed():
    source = _widgets_js()
    assert "render.kind === 'inline_card'" not in source
    assert "skill-widget-weather" not in source
    assert "const saved = widgetSessionState.get(persistenceKey) || {};" in source


def test_widgets_v5_7_0_new_components_render():
    """v5.7.0 host-owned declarative components: ``map`` (Leaflet-ready
    fallback list), ``calendar`` (host SVG-style row list), ``kanban``
    (HTML5 drag with on_move POST). All three must be present in the
    declarative renderer so authors can reference them in widgets, and
    none of them may bring skill-supplied JS into the SPA origin."""
    source = _widgets_js()
    assert "type === 'map'" in source
    assert "type === 'calendar'" in source
    assert "type === 'kanban'" in source
    # Module / arbitrary <script> from the skill must NEVER be inserted
    # into the host origin. ``data-widget-map-config`` carries the spec
    # as JSON in a data attribute (host renders); no runtime eval of
    # extension JS is acceptable in any of the new component renderers.
    assert "data-widget-map-config" in source
    assert "widget-kanban-card" in source


def test_widgets_render_subscription_children():
    source = _widgets_js()
    assert "type === 'subscription'" in source
    assert "component.render" in source
    assert "widget-subscription-render" in source
    assert "value_key" in source
    assert "items_key" in source
    assert "route_prefix" in source
    assert "type === 'key_value'" in source

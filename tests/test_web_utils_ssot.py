"""SSOT contract for shared web helpers (v5.8.3-rc.5 dedup pass).

``safeExternalHrefAttr``, ``renderMarkdownSafe``, ``boundedText``, and
``fetchJson`` previously had local copies in ``marketplace.js``,
``skills.js``, ``widgets.js``, and ``ouroboroshub.js``. They are now
owned by ``web/modules/utils.js`` for content helpers and
``web/modules/api_client.js`` for the gateway JSON-fetch helper, so the
URL/markdown/API error contracts cannot drift between modules.

These checks are static-text guards: they pin the helpers' presence in
``utils.js`` and the absence of duplicate function definitions in the
consumer modules. Any reintroduction of a local copy would be caught here
before the marketplace/skills/widgets surface diverged on a security
boundary (e.g. a publisher-supplied ``javascript:`` href slipping through
because one module's local helper was missing the protocol allowlist).
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_MODULES = REPO_ROOT / "web" / "modules"


def _read(name: str) -> str:
    return (WEB_MODULES / name).read_text(encoding="utf-8")


def test_utils_exports_shared_helpers():
    """``utils.js`` keeps content helpers and re-exports gateway fetchJson."""
    src = _read("utils.js")
    for sig in (
        "export function safeExternalHrefAttr(",
        "export function renderMarkdownSafe(",
        "export function boundedText(",
        "export { fetchJson } from './api_client.js';",
    ):
        assert sig in src, f"utils.js must export {sig.strip().rstrip('(')}"
    assert "export async function fetchJson(" not in src
    assert "export async function fetchJson(" in _read("api_client.js")


def test_api_client_owns_extension_route_helpers():
    src = _read("api_client.js")
    for sig in (
        "export function cleanExtensionRoute(",
        "export function extensionRoutePrefix(",
        "export function extensionRoutePath(",
    ):
        assert sig in src
    assert "route.includes('\\\\')" in src
    assert "part === '..'" in src
    assert "encodeURIComponent(skill)" in src


def test_api_client_owns_json_post_ok_false_handling():
    api_src = _read("api_client.js")
    mcp_src = _read("mcp_settings.js")

    assert "export function jsonPost(" in api_src
    assert "rejectOkFalse" in api_src
    assert "import { jsonPost } from './api_client.js';" in mcp_src
    assert "function postJson(" not in mcp_src
    assert "async function postJson(" not in mcp_src


def test_safe_external_href_attr_blocks_unsafe_schemes():
    """Schema allowlist must keep blocking javascript:/data:/vbscript:/mailto: hrefs."""
    src = _read("utils.js")
    block = src.split("export function safeExternalHrefAttr", 1)[1].split("export function", 1)[0]
    # Only http: and https: are allowed; mailto/javascript/data must NOT pass.
    assert "parsed.protocol === 'http:' || parsed.protocol === 'https:'" in block
    assert "escapeHtmlAttr(parsed.toString())" in block
    # Unparseable / unsafe → empty string (truthy gate at call sites).
    assert "return ''" in block


def test_render_markdown_safe_strips_dangerous_tags_and_attrs():
    """The DOMPurify allowlist must continue to ban script-bearing tags."""
    src = _read("utils.js")
    block = src.split("export function renderMarkdownSafe", 1)[1].split("export function", 1)[0]
    for forbidden_tag in ("script", "iframe", "object", "embed", "form", "input", "img"):
        assert f"'{forbidden_tag}'" in block, f"renderMarkdownSafe must FORBID_TAGS {forbidden_tag}"
    for forbidden_attr in ("style", "src", "srcset", "srcdoc"):
        assert f"'{forbidden_attr}'" in block, f"renderMarkdownSafe must FORBID_ATTR {forbidden_attr}"


def test_marketplace_does_not_redeclare_shared_helpers():
    """marketplace.js must import shared helpers, not redeclare them."""
    src = _read("marketplace.js")
    # Marketplace no longer renders package markdown after Details removal.
    assert "renderMarkdownSafe" not in src
    assert "safeExternalHrefAttr" in src
    assert "boundedText" in src
    assert "fetchJson" in src
    # No local function declarations of the SSOT helpers.
    assert "function boundedText(" not in src, "marketplace.js must use utils.boundedText"
    assert "async function fetchJson(" not in src, "marketplace.js must use utils.fetchJson"
    # ``safeExternalUrl`` may exist as a local *alias* (`const safeExternalUrl = safeExternalHrefAttr`)
    # but not as a function definition.
    assert "function safeExternalUrl(" not in src, "marketplace.js must alias to utils.safeExternalHrefAttr"


def test_skills_does_not_redeclare_shared_helpers():
    src = _read("skills.js")
    renderer = _read("skill_card_renderer.js")
    assert "boundedText" in src
    assert "safeExternalHrefAttr as safeExternalUrl" in renderer
    assert "function boundedText(" not in src
    assert "function safeExternalUrl(" not in src + renderer


def test_widgets_uses_shared_render_markdown():
    src = _read("widgets.js")
    assert "renderMarkdownSafe" in src
    # Local declaration removed in v5.8.3-rc.5.
    assert "function renderMarkdownSafe(" not in src


def test_ouroboroshub_uses_shared_fetch_json():
    src = _read("ouroboroshub.js")
    assert "fetchJson" in src
    # Local async fetchJson removed in v5.8.3-rc.5.
    assert "async function fetchJson(" not in src


def test_onboarding_wizard_remains_inline_iife_without_imports():
    """The onboarding wizard is inlined into a classic script, not loaded as an ES module."""
    src = _read("onboarding_wizard.js")
    assert src.startswith("(() => {")
    assert "\nimport " not in src


def test_accent_tokens_have_concrete_rgba_values():
    """The v5.8.3-rc.5 ``--accent-*`` numbered fade tokens added to
    ``web/style.css`` :root must each map to a concrete
    ``rgba(201, 53, 69, X)`` value — never to ``var(--accent-XX)`` (a
    self-reference forms an invalid CSS cycle and the entire crimson
    accent system silently fails to apply at computed-value time).

    Triad reviewers (gpt-5.5, gemini-3.1-pro, claude-opus-4.6) caught
    this exact regression in the first dry-run of v5.8.3-rc.5: a
    file-wide sed swap had also rewritten the :root definitions
    themselves, leaving every ``--accent-04: var(--accent-04);``-style
    cycle. This guard pins the fix and prevents the same regression
    class from returning silently.
    """
    import re
    src = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    root_match = re.search(r":root\s*\{([^}]+)\}", src, re.S)
    assert root_match, ":root block not found in web/style.css"
    root_body = root_match.group(1)

    expected = (
        "--accent-dim",
        "--accent-glow",
        "--accent-04",
        "--accent-05",
        "--accent-08",
        "--accent-10",
        "--accent-12",
        "--accent-18",
        "--accent-22",
        "--accent-25",
        "--accent-55",
    )
    for name in expected:
        decl_match = re.search(rf"{re.escape(name)}\s*:\s*([^;]+);", root_body)
        assert decl_match, f"{name} not declared in :root"
        value = decl_match.group(1).strip()
        # The value must be a literal rgba(...) anchored on the crimson
        # accent triple; never a ``var(<itself>)`` cycle, never a ``var()``
        # to a different accent token (that pattern works in CSS but
        # would silently break this token if its referent regresses).
        assert "var(" not in value, (
            f"{name} must not reference another CSS variable (cycle / "
            f"silent-drift risk); got: {value!r}"
        )
        assert "rgba(201, 53, 69," in value, (
            f"{name} must be defined on the crimson accent triple "
            f"rgba(201, 53, 69, X); got: {value!r}"
        )


def test_onboarding_escape_mirrors_utils():
    """``onboarding_wizard.js`` is an IIFE bundle and cannot ``import`` from
    ``utils.js`` without a bootstrap rewrite. Its local ``escapeHtml`` MUST
    therefore mirror the same chain of HTML-escape ``replace`` calls
    ``escapeHtmlAttr`` performs so the security contract (replace ``&``
    first, then the five HTML metas, then backtick) cannot drift between
    the wizard and the SPA. We compare on the normalized list of
    ``.replace(<from>, <to>)`` calls — indentation differs (IIFE has one
    extra wrap level) but the actual escape sequence must be identical.
    """
    import re
    onboarding = _read("onboarding_wizard.js")
    utils = _read("utils.js")
    wizard_body = onboarding.split("function escapeHtml(value) {", 1)[1].split("}", 1)[0]
    utils_body = utils.split("export function escapeHtmlAttr(value) {", 1)[1].split("}", 1)[0]
    pattern = re.compile(r"\.replace\(([^)]+)\)")
    wizard_replaces = pattern.findall(wizard_body)
    utils_replaces = pattern.findall(utils_body)
    assert wizard_replaces == utils_replaces, (
        "onboarding_wizard.escapeHtml drifted from utils.escapeHtmlAttr "
        f"— wizard chain: {wizard_replaces}, utils chain: {utils_replaces}"
    )
    # Smoke-check that the chain still escapes ampersand FIRST (any other
    # order would double-encode the entity numbers later in the chain).
    assert wizard_replaces[0].startswith("/&/g"), (
        "ampersand replacement must be first in the chain"
    )

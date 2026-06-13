"""Static UI contracts for page chrome: shared header helper, scroll regions,
and evolution/consciousness wiring.

Consolidated in v5.15.x from three small files that each guarded one slice
of the SPA page-chrome layer:

- ``test_page_header_ui_static.py``        — renderPageHeader / renderTabStrip SSOT
- ``test_settings_and_page_layout_static.py`` — secrets generality + scroll regions
- ``test_evolution_ui_guards.py``          — evolution page + server runtime-state wiring
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Shared page header helper (renderPageHeader / renderTabStrip SSOT)
# ---------------------------------------------------------------------------


def test_shared_page_header_helper_has_no_inline_styles():
    source = _read("web/modules/page_header.js")

    assert "export function renderPageHeader" in source
    assert "export function renderTabStrip" in source
    assert "style=" not in source
    assert "app-page-header" in source
    assert "app-tab-strip" in source


def test_primary_pages_use_shared_header_helper():
    for rel in [
        "web/modules/settings_ui.js",
        "web/modules/dashboard.js",
        "web/modules/skills.js",
        "web/modules/widgets.js",
        "web/modules/files.js",
        "web/modules/chat.js",
    ]:
        source = _read(rel)
        assert "page_header.js" in source
        assert "renderPageHeader" in source


# ---------------------------------------------------------------------------
# Settings secrets layout + skills/widgets scroll regions
# ---------------------------------------------------------------------------


def test_settings_secrets_are_generic_and_integrations_tab_removed():
    ui = _read("web/modules/settings_ui.js")
    settings = _read("web/modules/settings.js")
    assert "Integrations" not in ui
    assert "TELEGRAM_" not in ui
    assert "TELEGRAM_" not in settings
    assert "skill-requested-secrets" in ui
    assert "custom-secrets-list" in ui
    assert "Source Control" in ui


def test_settings_scope_review_effort_round_trips():
    ui = _read("web/modules/settings_ui.js")
    settings = _read("web/modules/settings.js")
    assert "s-effort-scope-review" in ui
    assert "OUROBOROS_EFFORT_SCOPE_REVIEW" in settings
    assert "['s-effort-scope-review', 'OUROBOROS_EFFORT_SCOPE_REVIEW', 'high']" in settings
    assert ".filter(([, key]) => key !== 'OUROBOROS_RUNTIME_MODE')" in settings


def test_skills_and_widgets_use_inner_scroll_regions():
    skills = _read("web/modules/skills.js")
    widgets = _read("web/modules/widgets.js")
    css = _read("web/style.css")
    assert 'class="skills-scroll scroll-fade-y"' in skills
    assert 'class="widgets-scroll scroll-fade-y"' in widgets
    assert ".skills-scroll" in css and "overflow-y: auto" in css
    assert ".widgets-scroll" in css and "overflow-y: auto" in css


# ---------------------------------------------------------------------------
# Evolution / consciousness UI wiring
# ---------------------------------------------------------------------------


def test_evolution_page_supports_refresh_and_runtime_state():
    source = _read("web/modules/evolution.js")

    assert 'id="evo-refresh"' in source
    assert "Runtime Status" in source
    assert "apiFetch(`/api/evolution-data${suffix}`" in source
    assert "ws.on('open', () => {" in source
    assert "window.addEventListener('ouro:page-shown'" in source
    assert "document.addEventListener('visibilitychange'" in source
    assert "renderRuntimeState(runtime, data.generated_at || '');" in source
    assert "evolution_state" in source
    assert "bg_consciousness_state" in source


def test_server_navigation_and_chat_static_contracts():
    server_source = _read("server.py")
    state_source = _read("ouroboros/gateway/state.py")
    control_source = _read("ouroboros/gateway/control.py")
    app_source = _read("web/app.js")
    evo_source = _read("web/modules/evolution.js")
    chat_source = _read("web/modules/chat.js")
    css = _read("web/style.css")
    ui = _read("web/modules/settings_ui.js")
    settings = _read("web/modules/settings.js")
    costs = _read("web/modules/costs.js")

    assert "def _describe_bg_consciousness_state(requested_enabled: bool) -> dict:" in server_source
    assert '"evolution_state": evolution_state,' in state_source
    assert '"bg_consciousness_state": bg_state,' in state_source
    assert 'request.query_params.get("force")' in control_source
    assert "window.dispatchEvent(new CustomEvent('ouro:page-shown', { detail: { page: name } }));" in app_source
    assert "evo-runtime-detail" in evo_source
    assert "data?.evolution_state?.detail" in chat_source
    assert "data?.bg_consciousness_state?.detail" in chat_source
    assert re.search(r'<input[^>]+id="chat-file-input"[^>]+multiple', chat_source)
    assert "MAX_PENDING_ATTACHMENTS = 10" in chat_source
    assert "MAX_ATTACHMENT_FILE_BYTES = 50 * 1024 * 1024" in chat_source
    assert "MAX_PENDING_ATTACHMENT_BYTES = 100 * 1024 * 1024" in chat_source
    assert "pendingAttachments" in chat_source
    assert "attachmentsUploading" in chat_source
    assert "setAttachmentUploadState" in chat_source
    assert "attachBtn.classList.toggle('uploading', uploading)" in chat_source
    assert "input.disabled = uploading;" in chat_source
    assert "cleanupUploadedAttachments" in chat_source
    assert "method: 'DELETE'" in chat_source
    assert "await cleanupUploadedAttachments(uploaded);" in chat_source
    assert "await cleanupUploadedAttachments(uploadedAttachments);" in chat_source
    assert "ws.send({" in chat_source and "{ queue: false }" in chat_source
    assert "result?.status !== 'sent'" in chat_source
    assert "data-attachment-remove" in chat_source
    assert "Promise.allSettled" in chat_source
    assert "budget_text: 'Connecting...'" in chat_source
    assert "send(msg, options = {})" in _read("web/modules/ws.js")
    assert "options.queue === false" in _read("web/modules/ws.js")
    assert ".chat-attachment-preview" in css and "flex-wrap" in css
    assert 'id="s-total-budget"' in ui
    assert 'id="s-settings-per-task-cost"' in ui
    assert "setupContract.budgetFields" in settings
    assert "'anthropic/claude-opus-4.7'" in settings
    assert "'anthropic::claude-opus-4-7'" in settings
    assert "currentSettings?.[field.settingKey]" in settings
    assert "window.addEventListener('ouro:settings-updated'" in settings
    assert "source: 'settings'" in settings
    assert "Budget values must be at least 0.01." in costs
    assert "COST_BUDGET_INPUTS" in costs
    assert "s?._meta?.setup_contract?.budgetFields" in costs
    assert "const MIN_BUDGET_VALUE" not in costs
    assert "new CustomEvent('ouro:settings-updated'" in costs
    assert "source: 'costs'" in costs
    assert "event.detail?.source === 'costs'" in costs

"""Regression checks for Files tab navigation and context menu behavior."""

import os
import pathlib

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_files_page_registers_navigation_guard():
    app_source = _read("web/app.js")
    files_source = _read("web/modules/files.js")

    assert "beforePageLeave" in app_source
    assert "setBeforePageLeave" in app_source
    assert "setBeforePageLeave(async ({ from })" in files_source
    assert "if (from !== 'files') return true;" in files_source


def test_new_file_discard_and_context_menu_clamp_regressions():
    source = _read("web/modules/files.js")

    assert "createNewFile({ force: true })" in source
    assert "window.innerWidth - rect.width" in source
    assert "window.innerHeight - rect.height" in source


def test_files_page_explains_manager_role_and_directory_affordance():
    source = _read("web/modules/files.js")

    assert "This is a file manager, not a chat attachment picker." in source
    assert "Open a folder or file from the left panel to browse, preview, or edit its contents." in source
    assert "button.type = 'button';" in source
    assert "(entry.type === 'file' ? formatFileSize(entry.size) : 'open')" in source


def test_files_layout_uses_internal_scroll_contract():
    css = _read("web/style.css")

    assert "flex: 1;" in css
    assert ".files-layout {" in css
    assert 'grid-template-areas: "sidebar preview";' in css
    assert ".files-sidebar {" in css
    assert "min-height: 0;" in css
    assert "overflow: hidden;" in css
    assert ".files-list {" in css
    assert "overscroll-behavior: contain;" in css
    assert "grid-template-rows: minmax(220px, 320px) minmax(0, 1fr);" in css
    assert 'max-height: none;' in css


def test_files_pdf_preview_and_download_bridge_are_safe():
    source = _read("web/modules/files.js")
    download_helper = _read("web/modules/ui_helpers.js")
    launcher = _read("launcher.py")
    assert 'class="files-preview-frame" sandbox="allow-same-origin"' in source
    assert "downloadViaHostBridge(" in source
    assert "download_file_to_downloads" in download_helper
    assert "URL.createObjectURL" in download_helper
    assert "encodeURI(data.content_url)" not in source
    assert 'parsed.path != "/api/files/download"' in launcher
    assert 'parsed.path.startswith("/api/extensions/")' in launcher
    assert "parsed.port != actual_port" in launcher


def test_files_confirm_dialog_results_are_normalized():
    source = _read("web/modules/files.js")
    helper = _read("web/modules/ui_helpers.js")
    toast = _read("web/modules/toast.js")

    assert "typeof result === 'boolean' ? { confirmed: result, value: '' } : result" in source
    assert "return Boolean(result?.confirmed);" in source
    assert "if (!result?.confirmed) return;" in source
    assert "normalizeTone(tone || 'info', 'info')" in toast
    assert "export function normalizeTone(tone = 'muted', fallback = 'muted')" in helper

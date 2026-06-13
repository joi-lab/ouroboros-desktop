"""Split extension-loader regression coverage kept below module size gates."""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import queue
import re
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict

import pytest

from ouroboros import extension_loader
from ouroboros.contracts.plugin_api import (
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    PluginAPI,
    VALID_EXTENSION_PERMISSIONS,
)
from ouroboros.skill_loader import (
    SkillReviewState,
    compute_content_hash,
    find_skill,
    save_enabled,
    save_review_state,
)
from tests._shared import clean_extension_runtime_state
from tests.test_extension_loader import (
    _add_fake_native_dep,
    _isolated_site_packages_dir,
    _mark_isolated_deps_installed,
    _prepare_extension,
    _write_ext_skill,
)


@pytest.fixture(autouse=True)
def _clear_loader_state(monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    clean_extension_runtime_state()
    yield
    clean_extension_runtime_state()


def test_load_extension_registers_route_with_prefix(tmp_path):
    plugin = (
        "def _handler(request): return {'ok': True}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods=('GET',))\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ext2", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert "/api/extensions/ext2/weather" in snap["routes"]


_ROUTE_REJECTION_CASES = [
    (
        "absolute_route",
        "ext_abs",
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('/absolute', _handler)\n",
        "absolute",
    ),
    (
        "traversal_route",
        "ext_traverse",
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('../escape', _handler)\n",
        None,
    ),
    (
        "unsupported_method",
        "ext_trace",
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods=('TRACE',))\n",
        "unsupported",
    ),
]


@pytest.mark.parametrize(
    "case_id,name,plugin,expected_substr",
    _ROUTE_REJECTION_CASES,
    ids=[c[0] for c in _ROUTE_REJECTION_CASES],
)
def test_load_extension_rejects_route(tmp_path, case_id, name, plugin, expected_substr):
    loaded, _, drive_root = _prepare_extension(tmp_path, name, plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    if expected_substr is not None:
        assert expected_substr in err.lower()


def test_load_extension_accepts_string_route_method(tmp_path):
    plugin = (
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods='GET')\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ext_get_string", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert "/api/extensions/ext_get_string/weather" in snap["routes"]


def test_load_extension_supports_nested_entry_relative_imports(tmp_path):
    repo_root = tmp_path / "skills"
    skill_dir = _write_ext_skill(
        repo_root,
        "ext_nested",
        permissions=["tool"],
        entry="pkg/plugin.py",
        plugin_body=(
            "from .helper import VALUE\n"
            "def register(api):\n"
            "    api.register_tool('t', lambda ctx: VALUE, description='', schema={})\n"
        ),
    )
    (skill_dir / "pkg" / "helper.py").write_text("VALUE = 'nested-ok'\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "ext_nested", True)
    content_hash = compute_content_hash(skill_dir, manifest_entry="pkg/plugin.py")
    save_review_state(
        drive_root,
        "ext_nested",
        SkillReviewState(status="pass", content_hash=content_hash),
    )
    loaded = find_skill(drive_root, "ext_nested", repo_path=str(repo_root))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("ext_nested", "t"))
    assert tool is not None
    assert tool["handler"](None) == "nested-ok"


def test_unload_dotted_prefix_skill_does_not_break_neighbor_imports(tmp_path):
    repo_root = tmp_path / "skills"
    foo_dir = _write_ext_skill(
        repo_root,
        "foo",
        permissions=["tool"],
        plugin_body=(
            "def register(api):\n"
            "    api.register_tool('t', lambda ctx: 'foo', description='', schema={})\n"
        ),
    )
    dotted_dir = _write_ext_skill(
        repo_root,
        "foo.bar",
        permissions=["tool"],
        plugin_body=(
            "def _lazy(ctx):\n"
            "    from .helper import VALUE\n"
            "    return VALUE\n"
            "def register(api):\n"
            "    api.register_tool('lazy', _lazy, description='', schema={})\n"
        ),
    )
    (dotted_dir / "helper.py").write_text("VALUE = 'still-live'\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    for name, skill_dir in (("foo", foo_dir), ("foo.bar", dotted_dir)):
        save_enabled(drive_root, name, True)
        save_review_state(
            drive_root,
            name,
            SkillReviewState(status="pass", content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py")),
        )
        loaded = find_skill(drive_root, name, repo_path=str(repo_root))
        assert loaded is not None
        assert extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root) is None

    extension_loader.unload_extension("foo")
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("foo.bar", "lazy"))
    assert tool is not None
    assert tool["handler"](None) == "still-live"


def test_load_extension_registers_ws_handler_with_namespace(tmp_path):
    plugin = (
        "async def _handler(payload):\n"
        "    return {'acked': True}\n"
        "def register(api):\n"
        "    api.register_ws_handler('message', _handler)\n"
    )
    loaded, _, drive_root = _prepare_extension(tmp_path, "ws1", plugin, permissions=["ws_handler"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    handlers = extension_loader.list_ws_handlers()
    assert extension_loader.extension_surface_name("ws1", "message") in handlers


def test_send_ws_message_broadcasts_namespaced_event(tmp_path):
    sent: list[dict] = []
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "push_ext",
        "def register(api):\n"
        "    api.send_ws_message('progress', {'pct': 40})\n",
        permissions=["ws_handler"],
    )
    extension_loader.set_ws_broadcaster(sent.append)

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is None, err
    assert sent == [
        {
            "type": extension_loader.extension_surface_name("push_ext", "progress"),
            "data": {"pct": 40},
            "skill": "push_ext",
        }
    ]


def test_send_ws_message_still_works_after_registration_phase(tmp_path):
    sent: list[dict] = []
    impl = extension_loader.PluginAPIImpl(
        skill_name="push_runtime",
        permissions=["ws_handler"],
        env_allowlist=[],
        state_dir=tmp_path,
        settings_reader=lambda: {},
    )
    extension_loader.set_ws_broadcaster(sent.append)

    impl._close_registration()
    impl.send_ws_message("progress", {"pct": 90})

    assert sent[0]["type"] == extension_loader.extension_surface_name("push_runtime", "progress")
    assert sent[0]["data"] == {"pct": 90}


def test_send_ws_message_requires_ws_permission(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "no_push_ext",
        "def register(api):\n"
        "    api.send_ws_message('progress', {'pct': 40})\n",
        permissions=[],
    )

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)

    assert err is not None
    assert "ws_handler" in err


def test_register_ui_tab_surfaces_hostable_widget(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "uiwait",
        "def register(api):\n"
        "    api.register_ui_tab('weather', 'Weather', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'markdown', 'text': 'ok'}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs_pending"] == []
    assert snap["ui_tabs"][0]["key"] == "uiwait:weather"
    assert snap["ui_tabs"][0]["ws_prefix"] == extension_loader.extension_name_prefix("uiwait")
    assert snap["ui_tabs"][0]["render"]["kind"] == "declarative"
    assert snap["ui_tabs"][0]["span"] == 1
    assert snap["ui_tabs"][0]["grid_span"] == 1

    extension_loader.unload_extension("uiwait")
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"] == []


def test_register_ui_tab_snapshots_nested_render_dicts(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "uicopy",
        "_RENDER = {'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'markdown', 'text': 'ok'}]}\n"
        "def register(api):\n"
        "    api.register_ui_tab('weather', 'Weather', render=_RENDER)\n"
        "    _RENDER['components'][0]['text'] = 'mutated after registration'\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err

    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["render"]["components"][0]["text"] == "ok"
    snap["ui_tabs"][0]["render"]["components"][0]["text"] = "mutated by caller"
    assert (
        extension_loader.snapshot()["ui_tabs"][0]["render"]["components"][0]["text"]
        == "ok"
    )

    extension_loader.unload_extension("uicopy")


def test_register_ui_tab_promotes_render_span_metadata(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "wideui",
        "def register(api):\n"
        "    api.register_ui_tab('wide', 'Wide', render={'kind': 'declarative', 'schema_version': 1, 'span': 2, 'components': [{'type': 'markdown', 'text': 'ok'}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["span"] == 2
    assert snap["ui_tabs"][0]["grid_span"] == 2
    assert snap["ui_tabs"][0]["render"]["span"] == 2

    extension_loader.unload_extension("wideui")


_UI_TAB_REJECTION_CASES = [
    (
        "unsupported_render_kind",
        "badui",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'script_module', 'src': 'x.js'})\n",
        "unsupported",
    ),
    (
        "bad_declarative_component",
        "baddecl",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'script'}]})\n",
        "unsupported type",
    ),
    (
        "declarative_form_without_route",
        "badform",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'form', 'fields': [{'name': 'q'}]}]})\n",
        "requires route or api_route",
    ),
    (
        "declarative_table_without_columns",
        "badtable",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'table', 'path': 'rows'}]})\n",
        "columns",
    ),
    (
        "declarative_media_without_source",
        "badmedia",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'image', 'label': 'Preview'}]})\n",
        "media source",
    ),
    (
        "bad_gallery_item",
        "badgallery",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'gallery', 'items': [None]}]})\n",
        "item 0 must be an object",
    ),
    (
        "non_object_render",
        "baduirender",
        "def register(api):\n"
        "    api.register_ui_tab('bad', 'Bad', render=[])\n",
        "ui render must be an object",
    ),
]


def test_register_ui_tab_accepts_declarative_poll_component(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "pollui",
        "def register(api):\n"
        "    api.register_ui_tab('poll', 'Poll', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'poll', 'route': 'status', 'auto_start': True}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["render"]["components"][0]["type"] == "poll"
    assert snap["ui_tabs"][0]["render"]["components"][0]["auto_start"] is True


def test_register_ui_tab_accepts_subscription_component(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "subui",
        "def register(api):\n"
        "    api.register_ui_tab('sub', 'Sub', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'subscription', 'event': 'progress', 'target': 'result'}, {'type': 'progress', 'path': 'pct'}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"][0]["render"]["components"][0]["type"] == "subscription"


def test_register_ui_tab_accepts_subscription_render_children(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "subrender",
        "def register(api):\n"
        "    api.register_ui_tab('sub', 'Sub', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'subscription', 'event': 'progress', 'target': 'result', 'render': [{'type': 'progress', 'value_key': 'progress_pct', 'label_key': 'message'}, {'type': 'gallery', 'items_key': 'frames', 'item_type': 'image', 'route_prefix': 'asset?path='}, {'type': 'key_value', 'items_key': 'stats'}]}]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    component = extension_loader.snapshot()["ui_tabs"][0]["render"]["components"][0]
    assert component["type"] == "subscription"
    assert [item["type"] for item in component["render"]] == ["progress", "gallery", "key_value"]


def test_register_ui_tab_accepts_widget_v2_components(tmp_path):
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "v2ui",
        "def register(api):\n"
        "    api.register_ui_tab('v2', 'V2', render={'kind': 'declarative', 'schema_version': 1, 'components': [\n"
        "        {'type': 'code', 'text': 'print(1)'},\n"
        "        {'type': 'chart', 'path': 'chart'},\n"
        "        {'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'markdown', 'text': 'ok'}]}]},\n"
        "        {'type': 'stream', 'route': 'events'}\n"
        "    ]})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    types = [item["type"] for item in extension_loader.snapshot()["ui_tabs"][0]["render"]["components"]]
    assert types == ["code", "chart", "tabs", "stream"]


_UI_TAB_REJECTION_CASES.extend([
    (
        "bad_tabs_component",
        "badtabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': []}]})\n",
        "tabs",
    ),
    (
        "invalid_nested_tab_component",
        "badnestedtabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'image'}]}]}]})\n",
        "media source",
    ),
    (
        "interactive_nested_tab_component",
        "badinteractivetabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'form', 'route': 'submit', 'fields': [{'name': 'q'}]}]}]}]})\n",
        "interactive type",
    ),
    (
        "nested_tabs_component",
        "badnestednestedtabs",
        "def register(api):\n"
        "    api.register_ui_tab('tabs', 'Tabs', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'tabs', 'tabs': [{'label': 'A', 'components': [{'type': 'tabs', 'tabs': [{'label': 'B', 'components': []}]}]}]}]})\n",
        "interactive type",
    ),
    (
        "stream_without_route",
        "badstream",
        "def register(api):\n"
        "    api.register_ui_tab('stream', 'Stream', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'stream'}]})\n",
        "requires route",
    ),
    (
        "stream_with_non_get_method",
        "badstreammethod",
        "def register(api):\n"
        "    api.register_ui_tab('stream', 'Stream', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'stream', 'route': 'events', 'method': 'POST'}]})\n",
        "stream method",
    ),
    (
        "subscription_without_event",
        "badsubui",
        "def register(api):\n"
        "    api.register_ui_tab('sub', 'Sub', render={'kind': 'declarative', 'schema_version': 1, 'components': [{'type': 'subscription'}]})\n",
        "requires event",
    ),
])


@pytest.mark.parametrize(
    "case_id,name,plugin,expected_substr",
    _UI_TAB_REJECTION_CASES,
    ids=[c[0] for c in _UI_TAB_REJECTION_CASES],
)
def test_register_ui_tab_rejects(tmp_path, case_id, name, plugin, expected_substr):
    loaded, _, drive_root = _prepare_extension(tmp_path, name, plugin, permissions=["widget"])
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert expected_substr in err

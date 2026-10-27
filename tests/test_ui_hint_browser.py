"""The one hint stays inside the window (docs/DESIGN.md "Hints", web/modules/ui_hint.js).

In Chromium and WebKit on the real composer: words that grow after the hint stood (the translation overlay rewrites
them in place) move it back inside a 1000 px window, where the docked sidebar leaves Effort's capsule far enough to
the right for its hint to pass the window's edge; and in a 320 x 300 phone window with a long draft, Subagents' longest hint never
starts above the window: it stands below the capsule or scrolls inside itself where neither side has the room.
"""
from __future__ import annotations

import json

import pytest

from tests.test_composer_subagents_browser import SUBAGENTS, _hold, _settle
from tests.test_effort_range_browser import _evidence_dir, _launch, _open_chat
from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

EFFORT = "#page-chat .chat-effort-range"
# The hint of the capsule `sel` against the window that shows it (the visual viewport where there is one).
WHERE = """(sel) => {
    const hint = document.querySelector(sel).querySelector('.ui-hint');
    const r = hint.getBoundingClientRect(), v = window.visualViewport;
    const top = v ? v.offsetTop : 0, height = v ? v.height : innerHeight;
    return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: document.documentElement.clientWidth,
        windowTop: top, windowBottom: top + height, side: hint.dataset.side || '', cramped: hint.dataset.cramped || '',
        hidden: hint.hidden, scrolls: hint.scrollHeight > hint.clientHeight + 1 };
}"""
# A translation the overlay could write: longer than the English, written after the hint stood.
LONGER = "Gilt für neue Schritte in diesem Chat; laufende Arbeit wird ohne Unterbrechung zu Ende geführt."


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_words_that_grow_after_the_hint_stood_keep_it_inside_the_window(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    seen = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 1000, "height": 820})
        try:
            _open_chat(page, url, width=1000, height=820)
            page.click(f"{EFFORT} .chat-capsule-head")
            page.wait_for_function("sel => document.querySelector(sel)?.dataset.open === 'true'", arg=EFFORT)
            _settle(page)
            seen["before"] = page.evaluate(WHERE, EFFORT)
            assert not seen["before"]["hidden"] and seen["before"]["right"] <= seen["before"]["width"] - 7, seen
            page.evaluate("([sel, words]) => { document.querySelector(sel).querySelector('.ui-hint-rule').textContent = words; }",
                          [EFFORT, LONGER])
            page.wait_for_timeout(250)
            seen["after"] = after = page.evaluate(WHERE, EFFORT)
            assert after["right"] > seen["before"]["right"] or after["left"] < seen["before"]["left"], \
                ("the words grew the hint", seen)
            assert after["left"] >= 7 and after["right"] <= after["width"] - 7, ("it moved back inside the window", seen)
        finally:
            (_evidence_dir(tmp_path) / f"ui-hint-grown-words-{engine}.json").write_text(json.dumps(seen, indent=1),
                                                                                      encoding="utf-8")
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_short_phone_window_never_puts_the_hint_above_it(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    seen = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 320, "height": 300}, is_mobile=True, has_touch=True)
        try:
            _hold(page, {"subagents_mode": "off", "subagents_availability": "available"})
            _open_chat(page, url, width=320, height=300)
            page.fill("#chat-input", "\n".join(f"A line of the draft, number {n}" for n in range(1, 7)))
            page.tap(f"{SUBAGENTS} .chat-capsule-head")
            page.wait_for_function("sel => document.querySelector(sel)?.dataset.open === 'true'", arg=SUBAGENTS)
            _settle(page)
            seen["hint"] = where = page.evaluate(WHERE, SUBAGENTS)
            assert not where["hidden"], seen
            assert where["top"] >= where["windowTop"] - 1 and where["bottom"] <= where["windowBottom"] + 1, \
                ("the hint stands inside the window, above, below or scrolling", seen)
            assert where["side"] in ("above", "below") and (where["cramped"] == "true") == where["scrolls"], seen
        finally:
            (_evidence_dir(tmp_path) / f"ui-hint-short-window-{engine}.json").write_text(json.dumps(seen, indent=1),
                                                                                       encoding="utf-8")
            browser.close()

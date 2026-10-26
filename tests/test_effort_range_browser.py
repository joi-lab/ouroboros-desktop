"""The effort range control in the real composer (DECISIONS v3 §6; DESIGN "Composer owner controls").

On a real server in Chromium and WebKit: the capsule `Effort <recommended level>` after Subagents and Context (no
range in words) opens its panel, never inline: a press opens a float panel above it on a desk, a bottom sheet on a
phone; hover opens nothing. The panel holds the ring and the name, the seven levels at a finger's size, the line that
says the range and how a change applies; Escape closes and returns focus, an outside press closes, the same capsule
closes. The row never moves; the strip stays whole inside the panel (seven finger-wide levels on a 360 px phone, shared
evenly on a 320 px one); the pill covers its word and the band meets the brackets. A drag and the keyboard save the full
triple through the owner endpoint, with primary independent from helper bounds. Each composer reads only its room; a
change made just before Send lands before the message; Settings → Behavior has no effort section and ends with Startup
& background, whose host fact reads as one plain note; Settings → Agents says Auto or the level in the model name.
"""
from __future__ import annotations

import json
import os
import pathlib
import time

import pytest

from tests import test_subscription_role_routes_browser as roles
from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]
subscription_ui = roles.subscription_ui
role_ui = roles.role_ui

CONTROL = "#page-chat .chat-effort-range"
HEAD = f"{CONTROL} .chat-effort-head"
# The capsule, its row and, while it is open, its panel (in the document only then; the head names it). Closed, the
# three levels are read from the button's name.
FACTS = """sel => {
    const el = document.querySelector(sel);
    const rect = (node) => node.getBoundingClientRect();
    const r = rect(el);
    const head = el.querySelector('.chat-effort-head');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    const strip = panel?.querySelector('.chat-effort-strip');
    const row = el.parentElement;
    const sub = row.querySelector('.chat-subagents-mode');
    const page = el.closest('#page-chat, .chat-instance-panel');
    const area = page.querySelector('#chat-input-area, .chat-input-area');
    const wrap = area.querySelector('.chat-input-wrap');
    const input = page.querySelector('#chat-input, .chat-input');
    const said = head.title.match(/^Primary effort: (.+?); helpers (.+?)–(.+?)\\./) || [];
    const text = (which, i) => (strip ? strip.querySelector('.chat-effort-' + which).getAttribute('aria-valuetext') : said[i]);
    const box = (node) => { const b = node ? rect(node) : null; return b && { left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height }; };
    // A word's glyphs, not its element: a word too wide for its level overflows its own box.
    const glyphs = (node) => { if (!node) return null; const range = document.createRange(); range.selectNodeContents(node); return box(range); };
    const recSeg = strip?.querySelector('.chat-effort-seg[data-rec="true"]');
    return {
        open: el.dataset.open, known: el.dataset.known, saving: el.dataset.saving || 'false',
        expanded: head.getAttribute('aria-expanded'), title: head.title,
        top: r.top, left: r.left, right: r.right, width: r.width, height: r.height,
        headWidth: rect(head).width, headHeight: rect(head).height,
        subTop: rect(sub).top, subHeight: rect(sub).height, rowRight: rect(row).right,
        sendHeight: rect(page.querySelector('.chat-send-inline')).height,
        inputTop: rect(input).top, view: { width: innerWidth, height: innerHeight },
        docScroll: document.documentElement.scrollWidth > innerWidth + 1,
        areaScroll: area.scrollWidth > area.clientWidth + 1 || wrap.scrollWidth > wrap.clientWidth + 1,
        areaHeight: area.offsetHeight,
        headFocused: document.activeElement === head, panelFocused: Boolean(panel) && document.activeElement === panel,
        recFocused: Boolean(strip) && document.activeElement === strip.querySelector('.chat-effort-rec'),
        recTabIndex: strip ? strip.querySelector('.chat-effort-rec').tabIndex : -1,
        min: text('min', 2), rec: text('rec', 1), max: text('max', 3),
        name: el.querySelector('.chat-capsule-name').textContent, value: el.querySelector('.chat-capsule-value').textContent,
        headText: head.textContent, rowHeight: rect(row).height,
        place: panel?.dataset.place || '', panel: box(panel), column: box(wrap), capsule: box(el),
        title2: panel?.querySelector('.capsule-panel-title')?.textContent || '',
        ring: Boolean(panel?.querySelector('.capsule-panel-glyph svg')),
        line: panel?.querySelector('.capsule-panel-line[data-on="true"]')?.textContent || '',
        rule: panel?.querySelector('.capsule-panel-rule')?.textContent || '',
        scrim: Boolean(document.querySelector('.capsule-panel-scrim')),
        extras: panel ? panel.querySelectorAll('.chat-effort-label, .chat-effort-sep, .chat-effort-reset').length : 0,
        pill: box(strip?.querySelector('.chat-effort-rec')), seg: box(recSeg), word: glyphs(recSeg?.querySelector('.chat-effort-seg-text')),
        band: box(strip?.querySelector('.chat-effort-band')), minCap: box(strip?.querySelector('.chat-effort-min')),
        maxCap: box(strip?.querySelector('.chat-effort-max')), strip: box(strip),
        segs: strip ? [...strip.querySelectorAll('.chat-effort-seg')].map(box) : [],
        words: strip ? [...strip.querySelectorAll('.chat-effort-seg-text')].map(glyphs) : [],
        segBoxes: strip ? [...strip.querySelectorAll('.chat-effort-seg')].map((seg) => [box(seg), glyphs(seg.querySelector('.chat-effort-seg-text'))]) : [],
        stripScrolls: Boolean(strip) && strip.scrollWidth > strip.clientWidth + 1,
    };
}"""


def _evidence_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    target = pathlib.Path(os.environ.get("OUROBOROS_UI_EVIDENCE_DIR", str(tmp_path)))
    target.mkdir(parents=True, exist_ok=True)
    return target


def _shot(page, tmp_path, name: str) -> None:
    page.screenshot(path=str(_evidence_dir(tmp_path) / f"{name}.png"), animations="disabled")


def _open_chat(page, url: str, *, width: int, height: int = 900, theme: str = "dark") -> None:
    page.add_init_script(f"try {{ localStorage.setItem('ouroboros.theme', '{theme}'); }} catch (e) {{}}")
    page.set_viewport_size({"width": width, "height": height})
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    page.wait_for_selector("#chat-input", timeout=30_000)
    # The control paints the one global value once /api/state answered.
    page.wait_for_function(f"() => document.querySelector('{CONTROL}')?.dataset.known === 'true'", timeout=30_000)


def _facts(page, selector: str = CONTROL) -> dict:
    return page.evaluate(FACTS, selector)


def _wait(page, predicate: str, selector: str = CONTROL, timeout: int = 5_000) -> None:
    page.wait_for_function(f"sel => {{ const el = document.querySelector(sel); return el && ({predicate}); }}",
                           arg=selector, timeout=timeout)


def _panel(page, selector: str = CONTROL) -> str:
    """The capsule's panel, as a selector (its head names it)."""
    return "#" + page.get_attribute(f"{selector} .chat-capsule-head", "aria-controls")


def _at_rest(page, selector: str = CONTROL) -> None:
    """The capsule's row has settled: the capsule's box is the same on three frames in a row (the row's fit runs in a
    frame after its first layout, and the names may give way then)."""
    page.evaluate("""sel => new Promise((resolve) => {
        const el = document.querySelector(sel);
        let last = '', same = 0;
        const tick = () => {
            const r = el.getBoundingClientRect(), now = [r.left, r.top, r.width].join();
            same = now === last ? same + 1 : 0;
            last = now;
            if (same >= 3) resolve(true); else requestAnimationFrame(tick);
        };
        requestAnimationFrame(tick);
    })""", selector)


def _settled(page, selector: str = CONTROL) -> None:
    """The capsule's panel is open and its entrance has played."""
    _wait(page, "el.dataset.open === 'true'", selector)
    page.wait_for_function("sel => { const p = document.querySelector(sel); return p && !p.getAnimations({subtree: true})"
                           ".some(a => a.playState === 'running' || a.pending); }", arg=_panel(page, selector), timeout=5_000)


def _segment_center(page, level: str, selector: str = CONTROL) -> tuple[float, float]:
    box = page.locator(f'{_panel(page, selector)} [data-effort-seg="{level}"]').bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def _assert_strip_geometry(facts: dict, *, air: float = 1.5) -> None:
    """The pill covers its whole segment and its word with air on both sides; the band, the
    pill and the brackets share one vertical box, and the band's ends are the brackets."""
    pill, seg, word, band = facts["pill"], facts["seg"], facts["word"], facts["band"]
    assert abs(pill["left"] - seg["left"]) <= 0.6 and abs(pill["right"] - seg["right"]) <= 0.6, facts
    assert pill["left"] <= word["left"] - air and pill["right"] >= word["right"] + air, facts
    for cap in (facts["minCap"], facts["maxCap"], pill):
        assert abs(cap["top"] - band["top"]) <= 0.6 and abs(cap["bottom"] - band["bottom"]) <= 0.6, facts
    assert abs(facts["minCap"]["left"] - band["left"]) <= 0.6 and abs(facts["maxCap"]["right"] - band["right"]) <= 0.6, facts
    # Every word stands inside its own level (a long one takes two lines where levels share a narrow strip).
    for seg, text in facts["segBoxes"]:
        assert seg["left"] - 0.5 <= text["left"] and text["right"] <= seg["right"] + 0.5, ("a word outgrew its level", seg, text)
    # A bracket's bar (its outer 2 px; its arms run along the band's top and bottom) keeps its air from every word.
    bars = ((facts["minCap"]["left"], facts["minCap"]["left"] + 2), (facts["maxCap"]["right"] - 2, facts["maxCap"]["right"]))
    for low, high in bars:
        assert all(word["right"] <= low - 1 or word["left"] >= high + 1 for word in facts["words"]), ((low, high), facts["words"])


def _assert_in_its_panel(facts: dict, *, phone: bool, finger: bool = True) -> None:
    """A float panel stands above the capsule inside the composer's column, its leading edge on the capsule's; a sheet
    spans the bottom of the screen. The strip stays whole inside it, every level a finger wide (unless `finger` is off),
    and nothing scrolls sideways."""
    panel, strip, view = facts["panel"], facts["strip"], facts["view"]
    if phone:
        assert facts["place"] == "sheet" and facts["scrim"], facts
        assert abs(panel["left"]) <= 0.5 and abs(panel["right"] - view["width"]) <= 0.5, facts
        assert abs(panel["bottom"] - view["height"]) <= 0.5, facts
    else:
        column, capsule = facts["column"], facts["capsule"]
        assert facts["place"] == "float" and not facts["scrim"], facts
        assert abs(panel["bottom"] - (capsule["top"] - 8)) <= 1, ("8 px above the capsule", facts)
        assert panel["left"] >= column["left"] - 0.5 and panel["right"] <= column["right"] + 0.5, ("inside the column", facts)
        edge = max(column["left"], min(capsule["left"], column["right"] - panel["width"]))
        assert abs(panel["left"] - edge) <= 1, ("its leading edge on the capsule's", facts)
    assert panel["left"] <= strip["left"] and strip["right"] <= panel["right"], facts
    assert strip["height"] >= 44 - 0.5 and not facts["stripScrolls"] and not facts["docScroll"], facts
    if finger:
        assert all(seg["width"] >= 44 - 0.5 for seg in facts["segs"]), ("every level a finger wide", facts["segs"])


def _launch(pw, engine: str, **context):
    from playwright.sync_api import Error as PlaywrightError

    try:
        browser = getattr(pw, engine).launch(headless=True)
    except PlaywrightError as exc:
        pytest.skip(f"{engine} is not installed: {exc}")
    return browser, browser.new_page(**context)


# A finger drawn down a sheet from its grabber, `share` of the sheet's height, in ten steps 30 ms apart (pointer events,
# as a touch delivers them; both engines).
SWIPE = """({ sel, share }) => new Promise((resolve) => {
    const panel = document.querySelector(sel);
    const grab = panel.querySelector('.capsule-panel-grab');
    const g = grab.getBoundingClientRect();
    const x = g.left + g.width / 2, y0 = g.top + g.height / 2;
    const dy = panel.getBoundingClientRect().height * share;
    const at = (y) => ({ bubbles: true, cancelable: true, pointerId: 7, pointerType: 'touch', isPrimary: true,
        clientX: x, clientY: y, button: 0 });
    grab.dispatchEvent(new PointerEvent('pointerdown', at(y0)));
    let i = 0;
    const step = () => {
        i += 1;
        panel.dispatchEvent(new PointerEvent('pointermove', at(y0 + dy * i / 10)));
        if (i < 10) setTimeout(step, 30);
        else { panel.dispatchEvent(new PointerEvent('pointerup', at(y0 + dy))); resolve(); }
    };
    setTimeout(step, 30);
})"""
ROW_KEYS = ("top", "left", "width", "height", "rowHeight", "areaHeight")


def _assert_row_still(opened: dict, closed: dict) -> None:
    for key in ROW_KEYS:
        assert opened[key] == closed[key], ("the row never moves", key, opened, closed)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_desk_press_opens_a_float_panel_and_escape_outside_or_the_capsule_closes_it(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 1280, "height": 900})
        try:
            _open_chat(page, url, width=1280)
            _at_rest(page)
            closed = _facts(page)
            assert closed["open"] == "false" and closed["expanded"] == "false" and closed["panel"] is None, closed
            # A labelled capsule of the other capsules' height on their row: ring, Effort and the recommended
            # level, and no range in words (the ring and the button's name carry it).
            assert abs(closed["height"] - 32) <= 1 and closed["headHeight"] >= 29, closed
            assert (closed["name"], closed["value"], closed["headText"]) == ("Effort", "Medium", "EffortMedium"), closed
            assert abs(closed["top"] - closed["subTop"]) <= 2, closed
            assert closed["title"] == "Primary effort: Medium; helpers Low–High. Applies to new participants in this room.", closed
            assert (closed["min"], closed["rec"], closed["max"]) == ("Low", "Medium", "High"), closed
            _shot(page, tmp_path, f"effort-closed-{engine}-dark")
            # Hover opens nothing: a press is the way in.
            page.hover(HEAD)
            page.wait_for_timeout(400)
            assert _facts(page)["open"] == "false", "hover opens nothing"
            page.click(HEAD)
            _settled(page)
            opened = _facts(page)
            assert opened["expanded"] == "true" and opened["panelFocused"], ("focus goes into the panel", opened)
            assert (opened["title2"], opened["ring"]) == ("Effort", True), ("the ring and the name head it", opened)
            assert opened["line"] == "Primary Medium; helpers Low–High.", opened
            assert opened["rule"] == "Applies to new work in this chat; running work keeps its level.", opened
            assert opened["extras"] == 0, "no title word of the strip's own, no separator element, no reset"
            assert opened["recTabIndex"] == 0, opened
            _assert_row_still(opened, closed)
            _assert_in_its_panel(opened, phone=False)
            _assert_strip_geometry(opened)
            _shot(page, tmp_path, f"effort-open-{engine}-dark")
            # The same capsule again closes it, focus back on the capsule.
            page.click(HEAD)
            _wait(page, "el.dataset.open === 'false'")
            assert _facts(page)["headFocused"] is True
            # Enter opens onto the primary handle; Escape closes and returns focus.
            page.keyboard.press("Enter")
            _wait(page, "el.dataset.open === 'true'")
            assert _facts(page)["recFocused"] is True
            page.keyboard.press("Escape")
            _wait(page, "el.dataset.open === 'false'")
            assert _facts(page)["headFocused"] is True
            # An outside press closes.
            page.click(HEAD)
            _wait(page, "el.dataset.open === 'true'")
            page.mouse.click(400, 300)
            _wait(page, "el.dataset.open === 'false'")
            _assert_row_still(_facts(page), closed)
            # Light theme, for the eye.
            page.add_init_script("try { localStorage.setItem('ouroboros.theme', 'light'); } catch (e) {}")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(f"() => document.querySelector('{CONTROL}')?.dataset.known === 'true'", timeout=30_000)
            _shot(page, tmp_path, f"effort-closed-{engine}-light")
            page.click(HEAD)
            _settled(page)
            _shot(page, tmp_path, f"effort-open-{engine}-light")
        finally:
            browser.close()


@pytest.mark.parametrize("engine,width", [("chromium", 390), ("webkit", 390), ("chromium", 360)])
def test_phone_press_opens_a_bottom_sheet_that_a_swipe_down_closes(direct_server_with_data, engine, width, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": width, "height": 800}, is_mobile=True, has_touch=True)
        try:
            _open_chat(page, url, width=width, height=800)
            _at_rest(page)
            closed = _facts(page)
            assert abs(closed["subHeight"] - closed["sendHeight"]) <= 1, closed
            assert abs(closed["height"] - closed["sendHeight"]) <= 1, "the round button keeps the capsules' height"
            assert closed["top"] < closed["inputTop"] and not closed["docScroll"] and not closed["areaScroll"], closed
            _shot(page, tmp_path, f"effort-phone-{width}-closed-{engine}")
            page.tap(HEAD)
            _settled(page)
            opened = _facts(page)
            _assert_row_still(opened, closed)
            _assert_in_its_panel(opened, phone=True)
            _assert_strip_geometry(opened)
            _shot(page, tmp_path, f"effort-phone-{width}-open-{engine}")
            # A tap on the scrim closes it.
            page.touchscreen.tap(width / 2, 120)
            _wait(page, "el.dataset.open === 'false'")
            # A short swipe from the grabber springs back; past a third of the sheet's height it goes, and focus
            # comes back to the capsule.
            page.tap(HEAD)
            _settled(page)
            page.evaluate(SWIPE, {"sel": _panel(page), "share": 0.1})
            page.wait_for_timeout(450)
            assert _facts(page)["open"] == "true", "a short swipe springs back"
            page.evaluate(SWIPE, {"sel": _panel(page), "share": 0.5})
            _wait(page, "el.dataset.open === 'false'")
            back = _facts(page)
            assert back["headFocused"] is True, back
            _assert_row_still(back, closed)
        finally:
            browser.close()


def test_project_pane_panel_stays_in_its_column_and_keeps_its_own_value(direct_server_with_data, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    from ouroboros.projects_registry import create_project

    url = direct_server_with_data["url"]
    create_project(direct_server_with_data["data_dir"], "pane", name="Pane")
    pane = "#panel-pchat-pane .chat-effort-range"
    with sync_playwright() as pw:
        browser, page = _launch(pw, "chromium", viewport={"width": 1440, "height": 900})
        try:
            _open_chat(page, url, width=1440)
            page.click('.nav-project-row[data-project-id="pane"]')
            page.wait_for_selector("#project-panel:not([hidden])", timeout=30_000)
            page.wait_for_function(f"() => document.querySelector('{pane}')?.dataset.known === 'true'", timeout=30_000)
            _at_rest(page, pane)
            main, closed = _facts(page), _facts(page, pane)
            assert (main["min"], main["rec"], main["max"]) == (closed["min"], closed["rec"], closed["max"]), (main, closed)
            width = page.locator("#project-panel").bounding_box()["width"]
            assert 340 <= width <= 440.5, width
            # The press opens the pane's own panel, inside the pane's column; opening saves nothing.
            saves = []
            page.on("request", lambda r: saves.append(r.url) if r.url.endswith("/api/owner/effort-range") else None)
            page.click(f"{pane} .chat-effort-head")
            _settled(page, pane)
            opened = _facts(page, pane)
            assert saves == [], saves
            _assert_row_still(opened, closed)
            _assert_in_its_panel(opened, phone=False)
            _assert_strip_geometry(opened)
            _shot(page, tmp_path, "effort-project-pane-open-chromium")
            # A change in the pane remains there after authoritative readback.
            page.focus(f"{_panel(page, pane)} .chat-effort-max")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range") and r.status == 200, timeout=30_000):
                page.keyboard.press("ArrowRight")
            _wait(page, "el.getAttribute('aria-valuetext') !== 'High'", f"{_panel(page, pane)} .chat-effort-max")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(f"() => document.querySelector('{CONTROL}')?.dataset.known === 'true'")
            assert _facts(page)["max"] == "High"
            page.click('.nav-project-row[data-project-id="pane"]')
            page.wait_for_function(f"() => document.querySelector('{pane}')?.dataset.known === 'true'")
            assert _facts(page, pane)["max"] == "X-High"
        finally:
            browser.close()


def test_drag_and_keyboard_save_the_full_triple_and_reload_reads_it_back(direct_server_with_data, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, "chromium", viewport={"width": 1440, "height": 900})
        try:
            _open_chat(page, url, width=1440)
            page.click(HEAD)
            _settled(page)
            panel = _panel(page)
            before = _facts(page)
            assert (before["min"], before["rec"], before["max"]) == ("Low", "Medium", "High"), before
            # Drag the max bracket up to Ultra: the full triple is posted once the pointer lifts.
            cap = page.locator(f"{panel} .chat-effort-max").bounding_box()
            x, y = _segment_center(page, "ultra")
            page.mouse.move(cap["x"] + cap["width"] / 2, cap["y"] + cap["height"] / 2)
            page.mouse.down()
            page.mouse.move(x, y, steps=8)
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.mouse.up()
            assert waited.value.status == 200, waited.value.text()
            assert json.loads(waited.value.request.post_data) == {"min": "low", "recommended": "medium", "max": "ultra"}
            _wait(page, "(el.dataset.saving || 'false') === 'false'")
            after = _facts(page)
            assert (after["min"], after["rec"], after["max"]) == ("Low", "Medium", "Ultra"), after
            assert after["line"] == "Primary Medium; helpers Low–Ultra.", ("the line says the new range", after)
            assert after["panel"]["height"] == before["panel"]["height"], ("a choice never changes the panel's height", after, before)
            # A level tap sets primary both inside and outside the helper range.
            x, y = _segment_center(page, "xhigh")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.mouse.click(x, y)
            assert json.loads(waited.value.request.post_data) == {"min": "low", "recommended": "xhigh", "max": "ultra"}
            x, y = _segment_center(page, "none")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.mouse.click(x, y)
            assert json.loads(waited.value.request.post_data) == {"min": "low", "recommended": "none", "max": "ultra"}
            # Keyboard: primary and helper bounds move independently.
            page.focus(f"{panel} .chat-effort-rec")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.keyboard.press("ArrowRight")
            assert json.loads(waited.value.request.post_data) == {"min": "low", "recommended": "low", "max": "ultra"}
            page.focus(f"{panel} .chat-effort-min")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.keyboard.press("End")
            assert json.loads(waited.value.request.post_data) == {"min": "ultra", "recommended": "low", "max": "ultra"}
            _shot(page, tmp_path, "effort-open-changed-chromium")
            # Moving primary below the helper interval leaves both brackets at Ultra.
            pill = page.locator(f"{panel} .chat-effort-rec").bounding_box()
            page.mouse.move(pill["x"] + pill["width"] / 2, pill["y"] + pill["height"] / 2)
            page.mouse.down()
            x, y = _segment_center(page, "none")
            page.mouse.move(x, y, steps=10)
            _wait(page, "el.querySelector('.chat-effort-min').getAttribute('aria-valuetext') === 'Ultra'", panel)
            x, y = _segment_center(page, "medium")
            page.mouse.move(x, y, steps=6)
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.mouse.up()
            assert json.loads(waited.value.request.post_data) == {"min": "ultra", "recommended": "medium", "max": "ultra"}
            _wait(page, "(el.dataset.saving || 'false') === 'false'")
            _assert_strip_geometry(_facts(page))
            # A reload reads the saved value back from /api/state.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(f"() => document.querySelector('{CONTROL}')?.dataset.known === 'true'", timeout=30_000)
            reloaded = _facts(page)
            assert (reloaded["min"], reloaded["rec"], reloaded["max"]) == ("Ultra", "Medium", "Ultra"), reloaded
            page.click(HEAD)
            _settled(page)
            panel = _panel(page)
            # A refused save: the server's sentence, the control back at the server's value.
            page.route("**/api/owner/effort-range", lambda route: route.fulfill(
                status=400, content_type="application/json",
                body=json.dumps({"error": "Effort range refused by the test.", "saved": False, "code": "effort_range_invalid"})))
            page.focus(f"{panel} .chat-effort-min")
            page.keyboard.press("ArrowLeft")
            page.wait_for_selector(".toast", timeout=10_000)
            assert "Effort range refused by the test." in page.locator(".toast").first.inner_text()
            _wait(page, "el.querySelector('.chat-effort-max').getAttribute('aria-valuetext') === 'Ultra'", panel)
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_320_px_phone_sheet_shares_the_strip_evenly(direct_server_with_data, engine, tmp_path):  # noqa: F811
    """Seven finger-wide levels do not fit a 320 px phone: the levels share the sheet's strip evenly, whole, unscrolled."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 320, "height": 640}, is_mobile=True, has_touch=True)
        try:
            _open_chat(page, url, width=320, height=640)
            _at_rest(page)
            closed = _facts(page)
            page.tap(HEAD)
            _settled(page)
            opened = _facts(page)
            _assert_row_still(opened, closed)
            _assert_in_its_panel(opened, phone=True, finger=False)
            # A word on two lines in a shared level (WebKit hyphenates "Medi-um") may come within a pixel of its pill.
            _assert_strip_geometry(opened, air=0.5)
            widths = [seg["width"] for seg in opened["segs"]]
            assert max(widths) - min(widths) <= 1, ("the levels share it evenly", widths)
            assert opened["headWidth"] > 20, "the ring stays"
            _shot(page, tmp_path, f"effort-phone-320-open-{engine}")
            # A tap sets primary without moving bounds.
            x, y = _segment_center(page, "ultra")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as waited:
                page.touchscreen.tap(x, y)
            assert json.loads(waited.value.request.post_data)["recommended"] == "ultra"
            assert json.loads(waited.value.request.post_data)["max"] == "high"
        finally:
            browser.close()


def test_a_change_made_before_send_lands_before_the_message(direct_server_with_data):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, "chromium", viewport={"width": 1440, "height": 900})
        try:
            page.add_init_script("""(() => {
                window.__sends = [];
                const send = WebSocket.prototype.send;
                WebSocket.prototype.send = function (data) {
                    try { const frame = JSON.parse(data); if (frame.type === 'chat') window.__sends.push({ at: Date.now(), content: frame.content }); } catch (e) {}
                    return send.call(this, data);
                };
                const fetch_ = window.fetch;
                window.fetch = async (...args) => {
                    const response = await fetch_(...args);
                    if (String(args[0]).endsWith('/api/owner/effort-range')) window.__effortSaved = Date.now();
                    return response;
                };
            })();""")
            _open_chat(page, url, width=1440)

            # The save is held open by the test until Send has been asked to wait for it.
            held = []
            page.route("**/api/owner/effort-range", lambda route: held.append(route))
            page.click(HEAD)
            _wait(page, "el.dataset.open === 'true'")
            page.focus(f"{_panel(page)} .chat-effort-rec")
            page.keyboard.press("ArrowRight")
            page.wait_for_function("() => true")  # let the request reach the route
            deadline = time.time() + 10
            while not held and time.time() < deadline:
                page.wait_for_timeout(50)
            assert held, "the gesture end posted the triple"
            page.fill("#chat-input", "use the new level")
            page.keyboard.press("Enter")
            # Send waits for the save: the button says so, the field keeps the draft meanwhile.
            page.wait_for_function("() => document.querySelector('#chat-send').textContent === 'Saving'", timeout=5_000)
            assert page.locator("#chat-input").input_value() == "use the new level"
            assert page.evaluate("() => window.__sends.length") == 0
            held[0].continue_()
            page.wait_for_function("() => window.__sends.length === 1", timeout=10_000)
            order = page.evaluate("() => ({ sent: window.__sends[0].at, saved: window.__effortSaved })")
            assert order["saved"] and order["sent"] >= order["saved"], order
            assert page.locator("#chat-input").input_value() == ""
        finally:
            browser.close()


def test_behavior_has_no_effort_section_and_ends_with_one_plain_startup_note(direct_server_with_data, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, "chromium", viewport={"width": 1440, "height": 900})
        try:
            _open_chat(page, url, width=1440)
            page.locator('[data-nav-page="settings"]').click()
            page.wait_for_selector(".settings-shell", timeout=20_000)
            page.locator('[data-settings-tab="behavior"]').click()
            panel = page.locator('[data-settings-panel="behavior"]')
            # The range lives in the composer alone.
            assert panel.locator("h3", has_text="Reasoning Effort").count() == 0
            assert page.locator("[data-effort-range-summary]").count() == 0
            assert page.locator('[data-effort-target^="s-effort-"]').count() == 0
            # This test host is not the packaged desktop app: both switches are off and disabled,
            # and the one reason is said once, as a note, at the label text's edge.
            page.wait_for_selector("[data-autostart-settings]:not([hidden])", timeout=20_000)
            page.wait_for_selector("[data-autostart-shared-note]:not([hidden])", timeout=20_000)
            facts = page.evaluate("""() => {
                const panel = document.querySelector('[data-settings-panel="behavior"]');
                const sections = [...panel.querySelectorAll(':scope > .form-section')];
                const card = panel.querySelector('[data-autostart-settings]');
                const note = card.querySelector('[data-autostart-shared-note]');
                const label = card.querySelector('[data-autostart-toggle]').closest('label');
                const text = [...label.childNodes].find((node) => node.nodeType === 3 && node.textContent.trim());
                const range = document.createRange();
                range.selectNodeContents(text);
                const textLeft = range.getBoundingClientRect().left;
                const noteStyle = getComputedStyle(note);
                return {
                    last: sections.at(-1) === card,
                    note: note.textContent,
                    noteTone: note.dataset.tone || '',
                    noteDot: getComputedStyle(note, '::before').content,
                    noteColor: noteStyle.color,
                    metaColor: getComputedStyle(card.querySelector('.settings-section-copy')).color,
                    noteTextLeft: note.getBoundingClientRect().left + parseFloat(noteStyle.paddingLeft),
                    textLeft,
                    rowsHidden: [...card.querySelectorAll('[data-autostart-status], [data-background-status]')].every((el) => el.hidden),
                    describedBy: [...card.querySelectorAll('input[type="checkbox"]')].map((box) => box.getAttribute('aria-describedby')),
                    disabled: [...card.querySelectorAll('input[type="checkbox"]')].map((box) => box.disabled),
                };
            }""")
            assert facts["last"], "Startup & background is the last card of Behavior"
            assert facts["note"] == "Available only when the host runs the packaged desktop app.", facts
            assert facts["noteTone"] == "" and facts["noteDot"] in ("none", "normal", ""), facts
            assert facts["noteColor"] == facts["metaColor"], "a fact in meta ink, not a warning colour"
            assert abs(facts["noteTextLeft"] - facts["textLeft"]) <= 1, facts
            assert facts["rowsHidden"] and facts["describedBy"] == ["settings-autostart-note"] * 2, facts
            assert facts["disabled"] == [True, True], facts
            page.locator("[data-autostart-settings]").scroll_into_view_if_needed()
            _shot(page, tmp_path, "settings-behavior-startup-chromium")
        finally:
            browser.close()


@pytest.mark.parametrize("width", [1360, 390])
def test_agents_rows_say_auto_or_the_level_in_the_model_name(role_ui, tmp_path, width):
    ui = role_ui
    roles.configure_mixed(ui)
    items = ui["settings"]["OUROBOROS_SUBAGENTS"]["items"]
    items[1]["route"] = {"kind": "api_model", "target_id": "claudexor::agy=gemini-3.7-flash-xhigh"}
    items[2]["route"] = {"kind": "agent_session", "target_id": "cursor=gpt-5.6-sol-high-fast", "credential_profile_id": "personal"}
    ui["page"].set_viewport_size({"width": width, "height": 900})
    page = roles.open_agents(ui)
    rows = page.locator("[data-subagent-row]")
    plain, api_named, session_named = rows.nth(0), rows.nth(1), rows.nth(2)
    assert plain.locator('[data-subagent-field="effort"] option[value=""]').inner_text() == "Auto (reviews at the top of the chat range)"
    plain.locator('[data-subagent-field="review_eligible"]').uncheck()
    assert plain.locator('[data-subagent-field="effort"] option[value=""]').inner_text() == "Auto (chat range)"
    for row, expected in ((api_named, "X-High · in the model name"), (session_named, "High · in the model name")):
        assert row.locator("[data-subagent-effort-named]").inner_text() == expected
        assert row.locator('[data-subagent-field="effort"]').count() == 0, "a named row offers no select"
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    _shot(page, tmp_path, f"settings-agents-effort-{width}")
    session_named.locator("[data-subagent-effort-named]").scroll_into_view_if_needed()
    _shot(page, tmp_path, f"settings-agents-effort-named-{width}")


@pytest.mark.parametrize("engine,touch", [("chromium", False), ("webkit", True)])
def test_room_cognition_is_independent_and_survives_project_switch(direct_server_with_data, tmp_path, engine, touch):  # noqa: F811 - fixture import
    """Main and two Projects keep their own values; primary can sit below both helper bounds."""
    import urllib.request
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]

    def request(path, body=None):
        req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.loads(response.read())

    original = {"min": "high", "recommended": "max", "max": "ultra"}
    request("/api/owner/effort-range", original)
    alpha = request("/api/projects", {"id": "cognition-alpha", "name": "Cognition Alpha"})["project"]
    beta = request("/api/projects", {"id": "cognition-beta", "name": "Cognition Beta"})["project"]
    request("/api/owner/effort-range", {"min": "low", "recommended": "medium", "max": "high"})
    # Hide these rooms from the bounded state projection: the addressed read remains authoritative.
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 1440, "height": 900}, has_touch=touch)
        try:
            page.route("**/api/state", lambda route: route.fulfill(json={
                **route.fetch().json(), "projects": [],
            }))
            _open_chat(page, url, width=1440)

            def open_room(project):
                page.evaluate("project => window.dispatchEvent(new CustomEvent('ouro:open-project', {detail: {project}}))", project)
                selector = f"#panel-pchat-{project['id']}"
                page.wait_for_function("sel => document.querySelector(sel + ' .chat-effort-range')?.dataset.known === 'true'", arg=selector)
                return selector

            panel = open_room(alpha)
            if touch:
                page.set_viewport_size({"width": 390, "height": 844})
            context = f"{panel} .chat-context-mode"
            page.locator(f"{context} .chat-capsule-head").tap() if touch else page.click(f"{context} .chat-capsule-head")
            low = f'{_panel(page, context)} .chat-seg[data-mode="low"]'
            with page.expect_response(lambda r: r.url.endswith("/api/owner/context-mode")) as changed:
                page.locator(low).tap() if touch else page.click(low)
            assert changed.value.status == 200, changed.value.text()
            assert json.loads(changed.value.request.post_data) == {"mode": "low", "project_id": alpha["id"]}
            page.keyboard.press("Escape")
            effort = f"{panel} .chat-effort-range"
            page.focus(f"{effort} .chat-effort-head")
            page.keyboard.press("Enter")
            page.focus(f"{_panel(page, effort)} .chat-effort-rec")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range")) as changed:
                page.keyboard.press("Home")
            assert changed.value.status == 200, changed.value.text()
            body = json.loads(changed.value.request.post_data)
            assert body == {**original, "recommended": "none", "project_id": alpha["id"]}
            _wait(page, "el.dataset.saving === 'false'", effort)
            assert (_facts(page, effort)["min"], _facts(page, effort)["rec"], _facts(page, effort)["max"]) == ("High", "None", "Ultra")
            _shot(page, tmp_path, f"cognition-primary-outside-range-{engine}")
            second = open_room(beta)
            assert _facts(page, f"{second} .chat-effort-range")["rec"] == "Max"
            assert page.locator(f"{second} .chat-context-mode").get_attribute("data-context-mode") == "max"
            panel = open_room(alpha)
            assert _facts(page, f"{panel} .chat-effort-range")["rec"] == "None"
            assert page.locator(f"{panel} .chat-context-mode").get_attribute("data-context-mode") == "low"
            main = request("/api/state")
            assert main["context_mode"] == "max" and main["effort_range"]["recommended"] == "medium"
            assert request(f"/api/projects?chat_id={beta['chat_id']}")["projects"][0]["cognition"]["effort_range"] == original
        finally:
            browser.close()

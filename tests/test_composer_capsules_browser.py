"""The composer's three owner capsules and their panels in the real composer (DESIGN "Composer owner controls").

On a real server in Chromium and WebKit: each capsule (Subagents, Context, Effort) opens a panel, never inline — on a
desk a float panel above it, its leading edge on the capsule's and inside the composer's column; on a phone a bottom
sheet over a scrim. At 390 and 1280 px, light and dark, in Main and in a Project, the row's boxes are the same to the byte
before a panel opens, on every frame while it opens, stands and closes, and after; every option in a panel is at least
44 × 44 px and nothing scrolls sideways. One panel is open at a time: a press on another capsule switches at once;
Escape, an outside press, the same capsule or, on a phone, the scrim closes it, focus coming back to the capsule from the
keyboard. A choice never changes the panel's height. `Context Max` chooses one of three modes (a choice posts the owner
endpoint at once, a refusal says why and keeps the saved mode, a reload reads it back; Enter opens onto the chosen mode,
arrows choose a neighbour). Effort's max bracket keeps clear of the strip's rounded end, Context's thumb stays concentric
with it. Settings-off locks Subagents on Off with the reason and Open Settings. Reduced motion opens and closes at once.
Closed, the capsules keep their names while the line holds them; long translated values give way on a 320 px phone, left
to right and right to left, while the panel shows the whole word; right to left the Effort strip mirrors.
"""
from __future__ import annotations

import json
import re
import urllib.request

import pytest

from tests.test_effort_range_browser import _at_rest, _evidence_dir, _launch, _open_chat, _panel, _settled, _shot, _wait
from tests.test_ui_smoke_playwright import direct_server_with_data  # noqa: F401 - pytest fixture import

pytestmark = [pytest.mark.ui_browser, pytest.mark.serial]

CONTEXT = "#page-chat .chat-context-mode"
EFFORT = "#page-chat .chat-effort-range"
SUBAGENTS = "#page-chat .chat-subagents-mode"
KINDS = {"subagents": ".chat-subagents-mode", "context": ".chat-context-mode", "effort": ".chat-effort-range"}
# Main's row and, for a capsule, its panel while open (in the document only then; the head names it).
ROW = """() => {
    const row = document.querySelector('#page-chat .chat-toolbar-row');
    const rect = (node) => node.getBoundingClientRect();
    const shown = (node) => Boolean(node) && getComputedStyle(node).display !== 'none';
    const capsule = (el) => {
        const head = el.querySelector('.chat-capsule-head');
        const panel = document.getElementById(head.getAttribute('aria-controls'));
        const segs = panel ? [...panel.querySelectorAll('.chat-seg')] : [];
        const active = document.activeElement;
        return {
            open: el.dataset.open, shown: shown(el), head: head.textContent, value: el.querySelector('.chat-capsule-value').textContent,
            expanded: head.getAttribute('aria-expanded'), disabled: el.dataset.disabled || '',
            mode: el.dataset.contextMode || el.dataset.mode || '', place: panel?.dataset.place || '',
            checked: segs.filter((seg) => seg.getAttribute('aria-checked') === 'true').map((seg) => seg.dataset.mode),
            tabbable: segs.filter((seg) => seg.tabIndex === 0).map((seg) => seg.dataset.mode),
            focused: active === head ? 'head' : (panel?.contains(active) ? (active.dataset.mode || active.dataset.handle || 'panel') : ''),
            line: panel?.querySelector('.capsule-panel-line[data-on="true"]')?.textContent || '',
            height: panel ? rect(panel).height : 0,
        };
    };
    const kids = [...row.children].filter(shown);
    const tops = kids.map((kid) => rect(kid).top);
    return {
        ctx: capsule(row.querySelector('.chat-context-mode')), eff: capsule(row.querySelector('.chat-effort-range')),
        sub: capsule(row.querySelector('.chat-subagents-mode')),
        panels: [...document.querySelectorAll('.capsule-panel:not([data-leaving])')].map((panel) => panel.dataset.kind),
        menuShown: shown(row.querySelector('.composer-menu')),
        namesShown: [...row.querySelectorAll('.chat-capsule-name')].map(shown),
        compact: row.dataset.ownerCompact || '',
        oneLine: Math.max(...tops) - Math.min(...tops) < 2, rowHeight: rect(row).height,
        docScroll: document.documentElement.scrollWidth > innerWidth + 1,
    };
}"""
# A room's row to the byte: its box and every child's, as one string.
ROW_BOX = """(root) => {
    const row = document.querySelector(`${root} .chat-toolbar-row`);
    const box = (node) => { const r = node.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
    return JSON.stringify({ row: box(row), kids: [...row.children].map((kid) => [kid.className, box(kid)]) });
}"""
# The row read on every animation frame for `ms` (an opening, a standing panel, a closing): the strings it showed.
ROW_FRAMES = """({ root, ms }) => {
    const row = document.querySelector(`${root} .chat-toolbar-row`);
    const box = (node) => { const r = node.getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
    const read = () => JSON.stringify({ row: box(row), kids: [...row.children].map((kid) => [kid.className, box(kid)]) });
    const seen = window.__rowFrames = [];
    const until = performance.now() + ms;
    const tick = () => { seen.push(read()); if (performance.now() < until) requestAnimationFrame(tick); else seen.done = true; };
    requestAnimationFrame(tick);
}"""
# An open capsule's panel: where it stands, its frame, its options as finger targets, its words.
PANEL = """(sel) => {
    const el = document.querySelector(sel);
    const head = el.querySelector('.chat-capsule-head');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    const rect = (node) => { const r = node.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
    const css = getComputedStyle(panel);
    return {
        kind: panel.dataset.kind, place: panel.dataset.place, panel: rect(panel), capsule: rect(el),
        column: rect(el.closest('.chat-input-wrap')), view: { width: innerWidth, height: innerHeight },
        docScroll: document.documentElement.scrollWidth > innerWidth + 1, panelScroll: panel.scrollWidth > panel.clientWidth + 1,
        targets: [...panel.querySelectorAll('.chat-seg, .chat-effort-seg, .chat-subagents-settings')]
            .filter((node) => node.getClientRects().length)
            .map((node) => ({ name: node.dataset.mode || node.dataset.effortSeg || node.textContent.trim(), ...rect(node) })),
        title: panel.querySelector('.capsule-panel-title').textContent, ring: Boolean(panel.querySelector('.capsule-panel-glyph svg')),
        line: panel.querySelector('.capsule-panel-line[data-on="true"]')?.textContent || '',
        rule: panel.querySelector('.capsule-panel-rule:not([hidden])')?.textContent || '',
        scrim: Boolean(document.querySelector('.capsule-panel-scrim:not([data-leaving])')),
        radius: [css.borderTopLeftRadius, css.borderBottomLeftRadius], glass: css.backdropFilter || css.webkitBackdropFilter || '',
        shadow: css.boxShadow !== 'none', opaque: css.backgroundImage !== 'none' || !css.backgroundColor.endsWith(', 0)'),
        focusInside: panel.contains(document.activeElement), headFill: getComputedStyle(head).backgroundColor,
        open: el.dataset.open, expanded: head.getAttribute('aria-expanded'),
    };
}"""
# A mark's clearance from the strip's rounded end: the straight gutter at the end, the rounded contour's distance at the
# mark's top and bottom corners, and its vertical insets (the strip is the standing capsule in the panel).
CLEARANCE = """({ sel, strip: stripSelector }) => {
    const head = document.querySelector(sel).querySelector('.chat-capsule-head');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    const strip = panel.querySelector(stripSelector);
    const shell = strip.getBoundingClientRect();
    const top = shell.top, bottom = shell.bottom, right = shell.right;
    const r = (bottom - top) / 2, cy = top + r, cx = right - r;
    const contour = (y) => cx + Math.sqrt(Math.max(0, r * r - (y - cy) ** 2));
    const mark = (node) => {
        if (!node) return null;
        const m = node.getBoundingClientRect();
        return { gutter: right - m.right, corners: Math.min(contour(m.top), contour(m.bottom)) - m.right, top: m.top - top, bottom: bottom - m.bottom };
    };
    return { cap: mark(strip.querySelector('.chat-effort-max')), thumb: mark(strip.querySelector('.chat-context-thumb')) };
}"""
# Long translated values on a 320 px phone, left to right and right to left (the test's own words).
LONG_VALUES = {
    "ltr": ({"language": "ru", "english": False, "revision": 1, "entries": {
        english: {"text": text, "provenance": "imported"}
        for english, text in (("Context", "Контекст"), ("Nano", "Компактный"), ("Low", "Сокращённый"), ("Max", "Максимальный"),
                              ("Effort", "Усилие"), ("code:effort.tier.medium", "Средний"), ("Swarm", "Рой"))}},
            "Максимальный", "Средний"),
    "rtl": ({"language": "ar", "english": False, "revision": 1, "profile": {"direction": "rtl"}, "entries": {
        english: {"text": text, "provenance": "imported"}
        for english, text in (("Context", "السياق"), ("Nano", "السياق المصغّر"), ("Low", "السياق المنخفض"), ("Max", "السياق الكامل"),
                              ("Effort", "الجهد"), ("code:effort.tier.medium", "متوسط الجهد"), ("Swarm", "السرب"))}},
            "السياق الكامل", "متوسط الجهد"),
}
# The closed capsules' values against their shells, and every shown control against the row's content box.
CAPPED = """() => {
    const row = document.querySelector('#page-chat .chat-toolbar-row');
    const rs = getComputedStyle(row);
    const r = row.getBoundingClientRect();
    const box = (node) => { const b = node.getBoundingClientRect(); return [b.left, b.right]; };
    const content = [r.left + parseFloat(rs.paddingLeft), r.right - parseFloat(rs.paddingRight)];
    const kids = [...row.children].filter((kid) => getComputedStyle(kid).display !== 'none' && kid.getBoundingClientRect().width > 0);
    return {
        contained: kids.every((kid) => { const [l, rr] = box(kid); return l >= content[0] - 1 && rr <= content[1] + 1; }),
        capsules: [...row.querySelectorAll('.chat-capsule')].map((el) => {
            const value = el.querySelector('.chat-capsule-value');
            const head = el.querySelector('.chat-capsule-head');
            const border = parseFloat(getComputedStyle(el).borderLeftWidth) || 0;
            const [l, rr] = box(el);
            return { word: value.textContent, cut: value.scrollWidth > value.clientWidth + 1, value: box(value),
                     shell: [l + border, rr - border], label: head.getAttribute('aria-label') || '', title: head.title };
        }),
    };
}"""
# The chosen mode's word in the open Context panel: whole inside its strip.
SHOWN = """mode => {
    const head = document.querySelector('#page-chat .chat-context-mode .chat-capsule-head');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    const strip = panel.querySelector('.chat-context-strip');
    const seg = strip.querySelector(`.chat-seg[data-mode="${mode}"]`);
    const s = strip.getBoundingClientRect(), m = seg.getBoundingClientRect();
    const range = document.createRange();
    range.selectNodeContents(seg.querySelector('.chat-seg-text'));
    const word = range.getBoundingClientRect();  // the glyphs, not the element: an overflowing word leaves its box
    return { word: seg.textContent, whole: m.left >= s.left - 1 && m.right <= s.right + 1 && word.left >= m.left && word.right <= m.right,
             docScroll: document.documentElement.scrollWidth > innerWidth + 1 };
}"""


def _row(page) -> dict:
    return page.evaluate(ROW)


def _until(page, predicate: str, timeout: int = 5_000) -> None:
    page.wait_for_function(f"() => {{ const r = ({ROW})(); return {predicate}; }}", timeout=timeout)


def _record(tmp_path, name: str, facts) -> None:
    (_evidence_dir(tmp_path) / f"{name}.json").write_text(json.dumps(facts, indent=1), encoding="utf-8")


def _install(page, payload) -> None:
    page.route("**/api/ui/i18n", lambda route: route.fulfill(json=payload))
    page.route("**/api/ui/i18n/missing", lambda route: route.fulfill(json={"ok": True}))


def _frames(page, root: str, act, ms: int = 900) -> list[str]:
    """The row on every frame from just before `act` until `ms` later."""
    page.evaluate(ROW_FRAMES, {"root": root, "ms": ms})
    act()
    page.wait_for_function("() => window.__rowFrames?.done === true", timeout=ms + 10_000)
    return page.evaluate("() => window.__rowFrames.slice()")


def _assert_panel(facts: dict, *, phone: bool, theme: str) -> None:
    """Where the panel stands, its frame, and its options at a finger's size, nothing scrolling sideways."""
    panel, view = facts["panel"], facts["view"]
    assert facts["open"] == "true" and facts["expanded"] == "true" and facts["ring"], facts
    if phone:
        assert facts["place"] == "sheet" and facts["scrim"] and facts["radius"] == ["24px", "0px"], facts
        assert abs(panel["left"]) <= 0.5 and abs(panel["right"] - view["width"]) <= 0.5, facts
        assert abs(panel["bottom"] - view["height"]) <= 0.5, facts
    else:
        column, capsule = facts["column"], facts["capsule"]
        assert facts["place"] == "float" and not facts["scrim"] and facts["radius"] == ["18px", "18px"], facts
        assert abs(panel["bottom"] - (capsule["top"] - 8)) <= 1, ("8 px above the capsule", facts)
        assert panel["left"] >= column["left"] - 0.5 and panel["right"] <= column["right"] + 0.5, ("inside the column", facts)
        edge = max(column["left"], min(capsule["left"], column["right"] - panel["width"]))
        assert abs(panel["left"] - edge) <= 1, ("its leading edge on the capsule's", facts)
    assert facts["shadow"] and facts["opaque"], ("the float shadow over an opaque surface", facts)
    assert (facts["glass"] not in ("", "none")) is (theme == "dark"), ("glass in dark only", facts)
    assert panel["left"] >= -0.5 and panel["right"] <= view["width"] + 0.5, facts
    assert not facts["docScroll"] and not facts["panelScroll"], facts
    assert facts["targets"] and all(t["width"] >= 43.5 and t["height"] >= 43.5 for t in facts["targets"]), \
        ("every option a finger's size", facts["targets"])


def _project(url: str, project_id: str) -> dict:
    request = urllib.request.Request(url + "/api/projects", data=json.dumps({"id": project_id, "name": project_id.title()}).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the test's own local server
        return json.loads(response.read())["project"]


def _open_project(page, project: dict) -> str:
    page.evaluate("project => window.dispatchEvent(new CustomEvent('ouro:open-project', {detail: {project}}))", project)
    root = f"#panel-pchat-{project['id']}"
    page.wait_for_function("root => document.querySelector(root + ' .chat-effort-range')?.dataset.known === 'true'", arg=root,
                           timeout=30_000)
    page.wait_for_function("root => document.querySelector(root + ' .chat-toolbar-row').getBoundingClientRect().width > 0",
                           arg=root, timeout=10_000)
    page.wait_for_timeout(400)  # the room's own entrance
    return root


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 1280), ("chromium", 390), ("webkit", 390)])
def test_a_panel_never_moves_the_row(direct_server_with_data, engine, width, tmp_path):  # noqa: F811
    """Light and dark, Main and a Project, each capsule: the row to the byte before a panel opens, on every frame of its
    opening, standing and closing, and after; the panel where it belongs, its options a finger's size."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    phone = width < 720
    project = _project(url, f"rows-{engine}-{width}")
    context = {"viewport": {"width": width, "height": 860}}
    if phone:
        context.update(is_mobile=True, has_touch=True)
    seen = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, **context)
        try:
            for theme in ("light", "dark"):
                _open_chat(page, url, width=width, height=860, theme=theme)
                for room in ("main", "project"):
                    root = "#page-chat" if room == "main" else _open_project(page, project)
                    for kind, capsule in KINDS.items():
                        sel = f"{root} {capsule}"
                        head = f"{sel} .chat-capsule-head"
                        key = f"{engine}-{width}-{theme}-{room}-{kind}"
                        before = page.evaluate(ROW_BOX, root)
                        opening = _frames(page, root, (lambda: page.tap(head)) if phone else (lambda: page.click(head)))
                        _settled(page, sel)
                        facts = page.evaluate(PANEL, sel)
                        seen[key] = {"panel": facts}
                        _assert_panel(facts, phone=phone, theme=theme)
                        assert facts["focusInside"], ("focus goes into the panel", key, facts)
                        _shot(page, tmp_path, f"panel-{key}")
                        standing = _frames(page, root, lambda: None, ms=300)
                        closing = _frames(page, root, lambda: page.keyboard.press("Escape"))
                        page.wait_for_function("sel => document.querySelector(sel).dataset.open === 'false'", arg=sel)
                        assert page.evaluate("sel => document.activeElement === document.querySelector(sel + ' .chat-capsule-head')", sel), key
                        after = page.evaluate(ROW_BOX, root)
                        frames = opening + standing + closing + [after]
                        moved = [frame for frame in frames if frame != before]
                        seen[key].update(frames=len(frames), moved=moved[:2], before=before)
                        assert len(opening) >= 3 and len(closing) >= 3, ("frames were read", key, len(opening), len(closing))
                        assert not moved, ("the row never moves", key, before, moved[:2])
        finally:
            _record(tmp_path, f"panels-row-{engine}-{width}", seen)
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_context_capsule_chooses_one_of_three_modes_and_reads_back(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 1280, "height": 900})
        try:
            _open_chat(page, url, width=1280, theme="light")
            closed = _row(page)
            assert closed["ctx"]["head"] == "ContextMax" and closed["ctx"]["mode"] == "max", closed
            assert closed["panels"] == [] and closed["oneLine"] and closed["namesShown"] == [True, True, True], closed
            posts = []
            page.on("request", lambda r: posts.append(json.loads(r.post_data)) if r.url.endswith("/api/owner/context-mode") else None)
            page.click(f"{CONTEXT} .chat-capsule-head")
            _settled(page, CONTEXT)
            opened = _row(page)
            assert opened["panels"] == ["context"] and opened["ctx"]["place"] == "float", opened
            assert opened["ctx"]["checked"] == ["max"] and opened["ctx"]["tabbable"] == ["max"], opened
            assert opened["ctx"]["line"] == "Everything the model's window holds.", opened
            assert posts == [], "opening saves nothing"
            panel = _panel(page, CONTEXT)
            # A press on a mode posts it at once; the capsule shows it once the server took it; the panel stays.
            with page.expect_response(lambda r: r.url.endswith("/api/owner/context-mode"), timeout=30_000) as waited:
                page.click(f'{panel} .chat-seg[data-mode="low"]')
            assert waited.value.status == 200 and posts[-1] == {"mode": "low"}, posts
            _until(page, "r.ctx.mode === 'low' && r.ctx.disabled === 'false'")
            chosen = _row(page)
            assert chosen["ctx"]["checked"] == ["low"] and chosen["ctx"]["open"] == "true", chosen
            assert chosen["ctx"]["line"] == "About 250K tokens; fits local models.", chosen
            assert chosen["ctx"]["height"] == opened["ctx"]["height"], ("a choice never changes the panel's height", chosen, opened)
            _shot(page, tmp_path, f"capsules-context-low-{engine}-light")
            # Keyboard: Esc returns to the capsule, Enter opens onto the chosen mode, an arrow chooses.
            page.keyboard.press("Escape")
            _until(page, "r.ctx.open === 'false' && r.ctx.focused === 'head'")
            assert _row(page)["ctx"]["head"] == "ContextLow"
            page.keyboard.press("Enter")
            _until(page, "r.ctx.open === 'true' && r.ctx.focused === 'low'")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/context-mode"), timeout=30_000):
                page.keyboard.press("ArrowLeft")
            _until(page, "r.ctx.mode === 'nano' && r.ctx.focused === 'nano' && r.ctx.tabbable.join() === 'nano'")
            _shot(page, tmp_path, f"capsules-context-nano-{engine}-light")
            # A refusal: the server's sentence, the saved mode stays.
            page.route("**/api/owner/context-mode", lambda route: route.fulfill(
                status=409, content_type="application/json",
                body=json.dumps({"error": "Context mode refused by the test."})))
            page.click(f'{panel} .chat-seg[data-mode="max"]')
            page.wait_for_selector(".toast", timeout=10_000)
            assert "Context mode refused by the test." in page.locator(".toast").first.inner_text()
            _until(page, "r.ctx.mode === 'nano' && r.ctx.disabled === 'false' && r.ctx.checked.join() === 'nano'")
            page.unroute("**/api/owner/context-mode")
            # The one global value: a reload reads Nano back from /api/state.
            page.add_init_script("try { localStorage.setItem('ouroboros.theme', 'dark'); } catch (e) {}")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(f"() => document.querySelector('{EFFORT}')?.dataset.known === 'true'", timeout=30_000)
            _until(page, "r.ctx.mode === 'nano' && r.ctx.head === 'ContextNano'", timeout=30_000)
            page.click(f"{CONTEXT} .chat-capsule-head")
            _settled(page, CONTEXT)
            _shot(page, tmp_path, f"capsules-context-nano-open-{engine}-dark")
        finally:
            browser.close()


# Every click the page delivered, by its target (capture phase): a tap on the scrim must land on the scrim alone.
CLICKS = """() => { window.__clicks = []; document.addEventListener('click', (event) => window.__clicks.push(
    event.target.className || event.target.id || event.target.tagName), true); }"""


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 1280), ("chromium", 390), ("webkit", 390)])
def test_one_panel_at_a_time_and_every_way_to_close_it(direct_server_with_data, engine, width, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    from tests.test_effort_range_browser import SWIPE

    url = direct_server_with_data["url"]
    phone = width < 720
    context = {"viewport": {"width": width, "height": 860}}
    if phone:
        context.update(is_mobile=True, has_touch=True)
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, **context)
        try:
            _open_chat(page, url, width=width, height=860)
            heads = {"sub": f"{SUBAGENTS} .chat-capsule-head", "ctx": f"{CONTEXT} .chat-capsule-head", "eff": f"{EFFORT} .chat-capsule-head"}
            press = page.tap if phone else page.click
            press(heads["sub"])
            _until(page, "r.sub.open === 'true' && r.panels.join() === 'subagents'")
            if not phone:
                # One press on another capsule switches at once: one panel in the document.
                press(heads["ctx"])
                _until(page, "r.ctx.open === 'true' && r.sub.open === 'false' && r.panels.join() === 'context'")
                press(heads["eff"])
                _until(page, "r.eff.open === 'true' && r.ctx.open === 'false' && r.panels.join() === 'effort'")
                _shot(page, tmp_path, f"capsules-switch-{engine}-{width}")
                # The same capsule again closes it; focus comes back to it.
                press(heads["eff"])
                _until(page, "r.eff.open === 'false' && r.panels.length === 0 && r.eff.focused === 'head'")
                # An outside press closes and leaves focus to what was pressed.
                press(heads["ctx"])
                _until(page, "r.ctx.open === 'true'")
                page.mouse.click(width / 2, 200)
                _until(page, "r.ctx.open === 'false' && r.panels.length === 0")
            else:
                # The sheet's scrim covers the page: a tap there closes, on its click, and nothing under it is pressed.
                page.evaluate(CLICKS)
                page.touchscreen.tap(width / 2, 140)
                _until(page, "r.sub.open === 'false' && r.panels.length === 0")
                assert page.evaluate("() => window.__clicks") == ["capsule-panel-scrim"], page.evaluate("() => window.__clicks")
                # A swipe down from the grabber closes, focus back on the capsule.
                press(heads["ctx"])
                _settled(page, CONTEXT)
                page.evaluate(SWIPE, {"sel": _panel(page, CONTEXT), "share": 0.6})
                _until(page, "r.ctx.open === 'false' && r.panels.length === 0 && r.ctx.focused === 'head'")
            # Escape closes and returns focus, from a keyboard open.
            page.focus(heads["eff"])
            page.keyboard.press("Enter")
            _until(page, "r.eff.open === 'true' && r.eff.focused === 'rec'")
            page.keyboard.press("Escape")
            _until(page, "r.eff.open === 'false' && r.eff.focused === 'head' && r.panels.length === 0")
            assert not _row(page)["docScroll"]
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_a_panel_reopened_after_the_phone_turns_stands_where_its_new_place_is(direct_server_with_data, engine, tmp_path):  # noqa: F811
    """Each capsule's same panel, opened in landscape (a float above the capsule) and reopened after the phone turned to
    portrait, is a full-width sheet at the bottom: none of the float's geometry stays; and turned back, a float again."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    landscape, portrait = {"width": 844, "height": 390}, {"width": 390, "height": 844}
    seen = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport=landscape, is_mobile=True, has_touch=True)
        try:
            _open_chat(page, url, width=landscape["width"], height=landscape["height"], theme="light")
            for kind, capsule in KINDS.items():
                sel = f"#page-chat {capsule}"
                for first, then, place in ((landscape, portrait, "sheet"), (portrait, landscape, "float")):
                    page.set_viewport_size(first)
                    _at_rest(page, sel)
                    page.tap(f"{sel} .chat-capsule-head")
                    _settled(page, sel)
                    page.keyboard.press("Escape")
                    _wait(page, "el.dataset.open === 'false'", sel)
                    page.set_viewport_size(then)
                    _at_rest(page, sel)
                    page.tap(f"{sel} .chat-capsule-head")
                    _settled(page, sel)
                    facts = seen[f"{kind}-{place}"] = page.evaluate(PANEL, sel)
                    _shot(page, tmp_path, f"capsules-reopened-{kind}-{place}-{engine}")
                    _assert_panel(facts, phone=place == "sheet", theme="light")
                    page.keyboard.press("Escape")
                    _wait(page, "el.dataset.open === 'false'", sel)
        finally:
            _record(tmp_path, f"capsules-reopened-{engine}", seen)
            browser.close()


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 1280), ("chromium", 390), ("webkit", 390)])
def test_the_marks_keep_clear_of_the_strips_rounded_ends(direct_server_with_data, engine, width, tmp_path):  # noqa: F811
    """Context's thumb at Max, the last mode, is concentric with the strip's rounded end; Effort's max bracket at Ultra,
    the last level, clears the curve at its corners, at rest and mid-drag."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    phone = width < 720
    context = {"viewport": {"width": width, "height": 860}, "device_scale_factor": 2}
    if phone:
        context.update(is_mobile=True, has_touch=True)
    measured = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, **context)
        try:
            _open_chat(page, url, width=width, height=860)
            (page.tap if phone else page.click)(f"{CONTEXT} .chat-capsule-head")
            _settled(page, CONTEXT)
            thumb = measured["context-thumb-max"] = page.evaluate(CLEARANCE, {"sel": CONTEXT, "strip": ".chat-context-strip"})["thumb"]
            assert abs(thumb["gutter"] - thumb["top"]) <= 1 and abs(thumb["top"] - thumb["bottom"]) <= 1, thumb
            page.keyboard.press("Escape")
            (page.tap if phone else page.click)(f"{EFFORT} .chat-capsule-head")
            _settled(page, EFFORT)
            panel = _panel(page, EFFORT)
            page.focus(f"{panel} .chat-effort-max")
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000):
                page.keyboard.press("End")
            page.wait_for_timeout(300)
            at_end = measured["effort-max-ultra"] = page.evaluate(CLEARANCE, {"sel": EFFORT, "strip": ".chat-effort-strip"})
            cap = at_end["cap"]
            # The square bracket clears the rounded end: its corners stand off the contour, and the straight gutter is at
            # least the band inset it keeps from the top and the bottom.
            assert cap["corners"] >= 2, at_end
            assert cap["gutter"] >= cap["top"] - 0.5 and abs(cap["top"] - cap["bottom"]) <= 1, at_end
            _shot(page, tmp_path, f"capsules-effort-ultra-end-{engine}-{width}")
            if not phone:
                # A drag: back to X-High, then the pointer carries the bracket to Ultra; mid-drag (the pointer still
                # down) the bracket already keeps its clearance.
                page.keyboard.press("ArrowLeft")
                page.wait_for_timeout(300)
                box = page.locator(f"{panel} .chat-effort-max").bounding_box()
                ultra = page.locator(f'{panel} [data-effort-seg="ultra"]').bounding_box()
                page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                page.mouse.down()
                page.mouse.move(ultra["x"] + ultra["width"] - 2, ultra["y"] + ultra["height"] / 2, steps=8)
                page.wait_for_timeout(100)
                dragging = measured["effort-max-mid-drag"] = page.evaluate(CLEARANCE, {"sel": EFFORT, "strip": ".chat-effort-strip"})
                with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000):
                    page.mouse.up()
                assert dragging["cap"]["corners"] >= 2, dragging
        finally:
            _record(tmp_path, f"capsules-clearance-{engine}-{width}", measured)
            browser.close()


# The open Subagents panel: its lock, its values (chosen, disabled), the line or the reason and the way to Settings.
SUBAGENTS_PANEL = """() => {
    const head = document.querySelector('#page-chat .chat-subagents-mode .chat-capsule-head');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    const shown = (node) => Boolean(node) && node.getClientRects().length > 0 && getComputedStyle(node).visibility !== 'hidden';
    const settings = panel.querySelector('.chat-subagents-settings');
    return {
        locked: panel.dataset.locked, lock: shown(panel.querySelector('.capsule-panel-lock')),
        values: [...panel.querySelectorAll('.chat-seg')].map((seg) => [seg.dataset.mode, seg.getAttribute('aria-checked'), seg.getAttribute('aria-disabled') || '']),
        line: shown(panel.querySelector('.capsule-panel-lines')), rule: shown(panel.querySelector('.capsule-panel-rule')),
        reason: shown(panel.querySelector('.chat-subagents-reason')) ? panel.querySelector('.chat-subagents-reason').textContent : '',
        settings: shown(settings) ? settings.getBoundingClientRect().height : 0,
    };
}"""


# The page's state read: the first `/api/state` and every `?bindings_since=` read after it (chat_activity.js).
STATE_READS = re.compile(r"/api/state(\?|$)")


def _state_with(page, facts: dict) -> dict:
    """The server's state reads with the room's subagents facts held by the test, so Settings-off needs no Settings
    write. Routed once: a save and the test change `facts` in place, and every later read answers them (routing again
    over a read in flight would answer it twice)."""
    page.route(STATE_READS, lambda route: route.fulfill(json={**route.fetch().json(), **facts}))

    def save(route):
        facts["subagents_mode"] = route.request.post_data_json["mode"]
        route.fulfill(json={"ok": True, "project_id": "", "subagents_mode": facts["subagents_mode"]})
    page.route("**/api/owner/subagents-mode", save)
    return facts


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_a_choice_keeps_the_panel_height_and_settings_off_locks_subagents(direct_server_with_data, engine, width, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    phone = width < 720
    context = {"viewport": {"width": width, "height": 860}}
    if phone:
        context.update(is_mobile=True, has_touch=True)
    seen = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, **context)
        press = page.tap if phone else page.click
        try:
            held = _state_with(page, {"subagents_mode": "auto", "subagents_availability": "available"})
            _open_chat(page, url, width=width, height=860, theme="light")
            # Each value's line stands in one cell: choosing another never changes the panel's height.
            for kind, sel, values, endpoint in (
                    ("subagents", SUBAGENTS, ("off", "swarm", "auto"), "/api/owner/subagents-mode"),
                    ("context", CONTEXT, ("nano", "low", "max"), "/api/owner/context-mode")):
                press(f"{sel} .chat-capsule-head")
                _settled(page, sel)
                panel = _panel(page, sel)
                heights = [page.evaluate("sel => document.querySelector(sel).getBoundingClientRect().height", panel)]
                lines = []
                for value in values:
                    with page.expect_response(lambda r, endpoint=endpoint: r.url.endswith(endpoint), timeout=30_000):
                        press(f'{panel} .chat-seg[data-mode="{value}"]')
                    page.wait_for_function("""({sel, value}) => { const el = document.querySelector(sel);
                        return (el.dataset.contextMode || el.dataset.mode) === value && el.dataset.disabled !== 'true'; }""",
                                           arg={"sel": sel, "value": value})
                    page.wait_for_timeout(250)  # the thumb's own move
                    heights.append(page.evaluate("sel => document.querySelector(sel).getBoundingClientRect().height", panel))
                    lines.append(page.evaluate("sel => document.querySelector(sel + ' .capsule-panel-line[data-on=\"true\"]').textContent", panel))
                    _shot(page, tmp_path, f"capsules-{kind}-{value}-{engine}-{width}-light")
                seen[kind] = {"heights": heights, "lines": lines}
                assert len(set(heights)) == 1, ("a choice never changes the panel's height", kind, heights)
                assert len(set(lines)) == len(values), ("each value says its own line", kind, lines)
                page.keyboard.press("Escape")
            # Settings turns subagents off: Off with a lock, Auto and Swarm disabled, the reason and Open Settings.
            held.update(subagents_mode="swarm", subagents_availability="disabled")
            page.add_init_script("try { localStorage.setItem('ouroboros.theme', 'dark'); } catch (e) {}")
            page.reload(wait_until="domcontentloaded")
            page.wait_for_function(f"() => document.querySelector('{SUBAGENTS}')?.dataset.locked === 'true'", timeout=30_000)
            assert page.locator(f"{SUBAGENTS} .chat-capsule-value").inner_text() == "Off"
            press(f"{SUBAGENTS} .chat-capsule-head")
            _settled(page, SUBAGENTS)
            locked = seen["locked"] = page.evaluate(SUBAGENTS_PANEL)
            assert locked["locked"] == "true" and locked["lock"], locked
            assert locked["values"] == [["off", "true", ""], ["auto", "false", "true"], ["swarm", "false", "true"]], locked
            assert not locked["line"] and not locked["rule"], ("the reason in place of the two lines", locked)
            assert locked["reason"] == "Subagents are turned off in Settings › Agents" and locked["settings"] >= 43.5, locked
            facts = page.evaluate(PANEL, SUBAGENTS)
            _assert_panel(facts, phone=phone, theme="dark")
            _shot(page, tmp_path, f"capsules-subagents-settings-off-{engine}-{width}-dark")
            # A press on a value Settings rules out (aria-disabled: the browser's own actionability waits for it, so the
            # press is forced) chooses nothing.
            locked_swarm = page.locator(f'{_panel(page, SUBAGENTS)} .chat-seg[data-mode="swarm"]')
            (locked_swarm.tap if phone else locked_swarm.click)(force=True)
            page.wait_for_timeout(300)
            assert page.locator(f"{SUBAGENTS}").get_attribute("data-mode") == "off", "a value Settings rules out is not chosen"
            # Open Settings closes the panel first, then goes to Settings › Agents.
            press(f"{_panel(page, SUBAGENTS)} .chat-subagents-settings")
            page.wait_for_selector('.settings-panel.active[data-settings-panel="agents"]', state="visible", timeout=20_000)
            assert page.locator(".capsule-panel:not([data-leaving])").count() == 0
            assert page.locator(SUBAGENTS).get_attribute("data-open") == "false"
        finally:
            _record(tmp_path, f"capsules-heights-settings-off-{engine}-{width}", seen)
            page.unroute_all(behavior="ignoreErrors")  # a state read still in flight ends with the browser
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_reduced_motion_opens_and_closes_at_once_and_closed_names_fit(direct_server_with_data, engine, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 1280, "height": 900}, reduced_motion="reduce")
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            _open_chat(page, url, width=1280)
            # Reduced motion: the panel stands at once and goes at once, no picture left behind.
            moving = page.evaluate(f"""() => {{
                const head = document.querySelector('{EFFORT} .chat-capsule-head');
                head.dispatchEvent(new MouseEvent('click', {{ bubbles: true, detail: 1 }}));
                const panel = document.getElementById(head.getAttribute('aria-controls'));
                const opened = panel.getAnimations({{ subtree: true }}).length;
                document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
                return {{ opened, left: document.querySelectorAll('.capsule-panel').length }};
            }}""")
            assert moving == {"opened": 0, "left": 0}, moving
            # Closed, the capsules keep their names while the line holds them; a narrower line keeps rings and values.
            # Probe both sides of the named row's width (system fonts differ; these are not product breakpoints).
            _until(page, "r.eff.open === 'false' && r.namesShown.every(Boolean) && r.oneLine")
            named_width = page.evaluate("""() => {
                const row = document.querySelector('#page-chat .chat-toolbar-row'), css = getComputedStyle(row);
                const kids = [...row.children].filter(n => getComputedStyle(n).display !== 'none');
                return Math.ceil(kids.reduce((sum, n) => sum + n.getBoundingClientRect().width, 0)
                    + parseFloat(css.columnGap) * (kids.length - 1) + parseFloat(css.paddingLeft) + parseFloat(css.paddingRight));
            }""")
            facts = {}
            for index, (width, names) in enumerate(((named_width + 4, True), (named_width - 4, False), (named_width + 4, True))):
                page.locator('#page-chat .chat-toolbar-row').evaluate('(row, width) => row.style.width = width + "px"', width)
                _until(page, f"r.namesShown.every(n => n === {str(names).lower()}) && r.oneLine")
                facts[f"closed-{index}-{width}"] = _row(page)
                _shot(page, tmp_path, f"capsules-closed-names-{engine}-{index}-{width}")
            assert not errors, errors
            _record(tmp_path, f"capsules-names-motion-{engine}", facts)
        finally:
            browser.close()


@pytest.mark.parametrize("engine,direction", [("chromium", "ltr"), ("webkit", "ltr"), ("chromium", "rtl"), ("webkit", "rtl")])
def test_long_closed_values_on_a_320px_phone_give_way_on_one_line(direct_server_with_data, engine, direction, tmp_path):  # noqa: F811
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    payload, context_word, effort_word = LONG_VALUES[direction]
    url = direct_server_with_data["url"]
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, viewport={"width": 320, "height": 860}, is_mobile=True, has_touch=True)
        seen = {}
        try:
            _install(page, payload)
            _open_chat(page, url, width=320, height=860)
            page.wait_for_function(f"""() => document.querySelector('{CONTEXT} .chat-capsule-value')?.textContent === '{context_word}'
                && document.querySelector('{EFFORT} .chat-capsule-value')?.textContent === '{effort_word}'""", timeout=30_000)
            # Closed: without the names the values still take two lines, so the longest give way to
            # one measured width; the row keeps one line and every control stays inside it.
            _until(page, "r.compact !== ''")  # the settled, closed row decided its fit
            page.wait_for_timeout(200)
            seen["closed"] = closed = _row(page)
            seen["capped"] = capped = page.evaluate(CAPPED)
            _shot(page, tmp_path, f"capsules-long-values-closed-{engine}-{direction}-320")
            assert closed["oneLine"] and closed["rowHeight"] < 40 and capped["contained"] and not closed["docScroll"], seen
            assert closed["compact"] == "values" and closed["namesShown"] == [False, False, False], seen
            assert closed["sub"]["shown"] and closed["ctx"]["shown"] and closed["eff"]["shown"], seen
            subagents, context, effort = capped["capsules"]
            assert context["word"] == context_word and context["cut"], seen
            for capsule in (subagents, context, effort):  # a value, cut or whole, keeps clear of both ends of its shell
                assert capsule["value"][0] >= capsule["shell"][0] + 1 and capsule["value"][1] <= capsule["shell"][1] - 1, seen
            # The whole word stays the button's: its accessible name, Effort's tooltip, the panel.
            assert context_word in page.locator(f"{CONTEXT} .chat-capsule-head").aria_snapshot(), seen
            assert effort_word in effort["label"] and effort_word in effort["title"], seen
            before = page.evaluate(ROW_BOX, "#page-chat")
            page.tap(f"{CONTEXT} .chat-capsule-head")
            _settled(page, CONTEXT)
            seen["open"] = page.evaluate(SHOWN, "max")
            _shot(page, tmp_path, f"capsules-long-values-open-{engine}-{direction}-320")
            assert seen["open"]["word"] == context_word and seen["open"]["whole"] and not seen["open"]["docScroll"], seen
            assert page.evaluate(ROW_BOX, "#page-chat") == before, "the cut row stays as it was while the panel is open"
            page.keyboard.press("Escape")
            _until(page, "r.ctx.open === 'false' && r.compact === 'values' && r.oneLine")
            # A wider line takes the cut back: the values show whole again.
            page.set_viewport_size({"width": 600, "height": 860})
            _until(page, "r.compact !== 'values' && r.oneLine")
            seen["wide"] = page.evaluate(CAPPED)
            assert not any(capsule["cut"] for capsule in seen["wide"]["capsules"]), seen
        finally:
            _record(tmp_path, f"capsules-long-values-{engine}-{direction}-320", seen)
            browser.close()


# Effort's marks in its open panel: boxes as [left, right] along the strip.
MARKS = """() => {
    const head = document.querySelector('#page-chat .chat-effort-range .chat-capsule-head');
    const panel = document.getElementById(head.getAttribute('aria-controls'));
    const strip = panel.querySelector('.chat-effort-strip');
    const box = (sel) => { const r = strip.querySelector(sel).getBoundingClientRect(); return [r.left, r.right]; };
    return {
        dir: getComputedStyle(strip).direction, band: box('.chat-effort-band'), pill: box('.chat-effort-rec'),
        seg: box('.chat-effort-seg[data-rec="true"]'), min: box('.chat-effort-min'), max: box('.chat-effort-max'),
        none: box('[data-effort-seg="none"]'), ultra: box('[data-effort-seg="ultra"]'), strip: [strip.getBoundingClientRect().left, strip.getBoundingClientRect().right],
    };
}"""
RTL = {"language": "ar", "english": False, "revision": 1, "profile": {"direction": "rtl"}, "entries": {
    english: {"text": text, "provenance": "imported"}
    for english, text in (("Context", "السياق"), ("Nano", "السياق المصغّر"), ("Low", "السياق المنخفض"), ("Max", "السياق الكامل"))}}


@pytest.mark.parametrize("engine,width", [("chromium", 1280), ("webkit", 390)])
def test_right_to_left_the_effort_strip_mirrors_in_its_panel(direct_server_with_data, engine, width, tmp_path):  # noqa: F811
    """Right to left the levels run leftwards: None on the right, the min bracket on the right of the band, the pill on
    its level; a press lands on the level under it."""
    pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")
    from playwright.sync_api import sync_playwright

    url = direct_server_with_data["url"]
    phone = width < 720
    context = {"viewport": {"width": width, "height": 860}}
    if phone:
        context.update(is_mobile=True, has_touch=True)
    seen = {}
    with sync_playwright() as pw:
        browser, page = _launch(pw, engine, **context)
        try:
            _install(page, RTL)
            _open_chat(page, url, width=width, height=860)
            page.wait_for_function("() => document.documentElement.dir === 'rtl'", timeout=30_000)
            (page.tap if phone else page.click)(f"{EFFORT} .chat-capsule-head")
            _settled(page, EFFORT)
            opened = seen["open"] = page.evaluate(MARKS)
            _shot(page, tmp_path, f"capsules-rtl-effort-open-{engine}-{width}")
            assert opened["dir"] == "rtl" and opened["none"][0] > opened["ultra"][1], seen
            assert abs(opened["pill"][0] - opened["seg"][0]) <= 0.6 and abs(opened["pill"][1] - opened["seg"][1]) <= 0.6, seen
            assert abs(opened["band"][1] - opened["min"][1]) <= 0.6 and abs(opened["band"][0] - opened["max"][0]) <= 0.6, seen
            assert opened["max"][1] < opened["pill"][0] < opened["pill"][1] < opened["min"][0], seen
            assert opened["strip"][0] <= opened["ultra"][0] and opened["none"][1] <= opened["strip"][1], seen
            # A press on None (outside the helper range) sets primary there without moving the min bracket.
            box = page.locator(f'{_panel(page, EFFORT)} [data-effort-seg="none"]').bounding_box()
            with page.expect_response(lambda r: r.url.endswith("/api/owner/effort-range"), timeout=30_000) as saved:
                (page.touchscreen.tap if phone else page.mouse.click)(box["x"] + box["width"] * 0.5, box["y"] + box["height"] / 2)
            body = json.loads(saved.value.request.post_data)
            assert body["recommended"] == "none" and body["min"] == "low", body
            page.wait_for_timeout(300)
            pressed = seen["press"] = page.evaluate(MARKS)
            assert abs(pressed["pill"][1] - pressed["none"][1]) <= 1 and abs(pressed["min"][1] - pressed["band"][1]) <= 0.6, seen
            _shot(page, tmp_path, f"capsules-rtl-effort-press-{engine}-{width}")
        finally:
            _record(tmp_path, f"capsules-rtl-{engine}-{width}", seen)
            browser.close()

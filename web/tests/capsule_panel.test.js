// A capsule's panel (capsule_panel.js; docs/DESIGN.md "Composer owner controls"): on a desk a float panel above the
// capsule, inside the composer's column; on a phone a bottom sheet over a scrim that a swipe down closes. One open at a
// time; Escape, an outside press, the same capsule or a swipe closes it; focus goes in and comes back; the meaning line
// keeps every value's line in its cell, so a choice never changes the panel's height.
import test from 'node:test';
import assert from 'node:assert/strict';

import { ElementStub } from './chat_dom_fixture.js';
import { createCapsulePanel, meaningLines, placeThumb, ruleLine } from '../modules/capsule_panel.js';
import { stubDocument, stubWindow } from './composer_controls_fixture.js';

const PHONE = '(max-width: 719.98px)';
const TOKENS = { '--motion-fade': '180ms', '--motion-open': '340ms', '--motion-close': '280ms', '--ease-sheet': 'cubic-bezier(0.32, 0.72, 0, 1)' };
const escape = (defaultPrevented = false) => ({ key: 'Escape', defaultPrevented, preventDefault() { this.defaultPrevented = true; } });
const fire = (node, type, event) => (node.listeners.get(type) || []).forEach((fn) => fn(event));
const last = (node, type) => (node.listeners.get(type) || []).at(-1);

/* A capsule in a composer column with its panel; `win.phone` answers the phone query and can change. */
function capsule({ doc = stubDocument(), phone = false, reduce = false, motion = false } = {}) {
    const media = { phone, reduce };
    const win = {
        ...stubWindow(),
        matchMedia: (query) => ({ matches: (media.phone && query === PHONE) || (media.reduce && query === '(prefers-reduced-motion: reduce)') }),
        ...(motion ? { document: { documentElement: doc.documentElement }, getComputedStyle: () => ({ getPropertyValue: (name) => TOKENS[name] || '' }) } : {}),
    };
    const column = doc.createElement('div');
    column.className = 'chat-input-wrap';
    column.getBoundingClientRect = () => ({ left: 100, right: 700, width: 600, top: 500, bottom: 700 });
    const el = doc.createElement('div');
    el.className = 'chat-capsule';
    el.getBoundingClientRect = () => ({ left: 200, right: 320, width: 120, top: 600, bottom: 636 });
    el.closest = (selector) => (selector === '.chat-input-wrap' ? column : null);
    const head = doc.createElement('button');
    head.className = 'chat-capsule-head';
    el.append(head);
    const option = doc.createElement('button');
    const repaints = [];
    let panel = null;
    panel = createCapsulePanel({
        el, head, kind: 'context', name: 'Context', focusTarget: () => option, win, doc,
        repaint: (open) => repaints.push([open, panel?.panel.dataset.settling]),
    });
    panel.panel.offsetWidth = 440;
    panel.panel.offsetHeight = 300;
    const lines = meaningLines(doc, { nano: 'About 85K tokens: the compact window.', max: "Everything the model's window holds." });
    panel.body.append(option, lines.cell, ruleLine(doc, 'Applies to new work in this chat.'));
    return {
        doc, win, media, el, head, option, panel, lines, repaints, node: panel.panel,
        press: (detail = 1) => fire(head, 'click', { detail }),
        down: (target) => doc.fire('pointerdown', { target }),
        inBody: () => doc.body.children.includes(panel.panel),
    };
}

/* Every element the panel makes can animate and be copied, as in a browser; `played` records each animation. */
function animatedDocument(played) {
    const doc = stubDocument();
    const make = doc.createElement;
    doc.createElement = (tag) => {
        const node = make(tag);
        node.animate = (frames, options) => { played.push({ node, frames, options }); return { finished: Promise.resolve() }; };
        node.cloneNode = () => Object.assign(doc.createElement(tag), { className: node.className, dataset: { ...node.dataset } });
        return node;
    };
    return doc;
}

test('the meaning line: every value\'s line stays in one cell and only the chosen one shows, so a choice never changes its height', () => {
    const doc = stubDocument();
    const lines = meaningLines(doc, { off: 'Alone.', auto: 'When worth it.', swarm: 'Several models, critics, one synthesis.' });
    assert.equal(lines.cell.className, 'capsule-panel-lines');
    assert.deepEqual(lines.cell.children.map((line) => [line.className, line.dataset.value, line.textContent]), [
        ['capsule-panel-line', 'off', 'Alone.'], ['capsule-panel-line', 'auto', 'When worth it.'],
        ['capsule-panel-line', 'swarm', 'Several models, critics, one synthesis.'],
    ]);
    lines.show('auto');
    assert.deepEqual(lines.cell.children.map((line) => line.dataset.on), ['false', 'true', 'false']);
    lines.show('swarm');
    assert.deepEqual(lines.cell.children.map((line) => line.dataset.on), ['false', 'false', 'true']);
    assert.equal(lines.cell.children.length, 3, 'nothing is added or taken away: the cell is as tall as its tallest line');
    assert.equal(ruleLine(doc, 'Applies to new steps.').className, 'capsule-panel-rule');
});

test('the thumb under the chosen option keeps from the strip\'s rounded ends the inset it keeps from its top and bottom', () => {
    const seg = (left, width) => ({ offsetLeft: left, offsetWidth: width });
    const segs = [seg(0, 100), seg(100, 100), seg(200, 100)];
    const thumb = { offsetTop: 4, style: {} };
    const at = (chosen, all = segs) => { placeThumb(thumb, chosen, all); return [thumb.style.left, thumb.style.width]; };
    assert.deepEqual(at(segs[0]), ['4px', '96px'], 'concentric with the leading end');
    assert.deepEqual(at(segs[1]), ['100px', '100px'], 'between options it covers its option');
    assert.deepEqual(at(segs[2]), ['200px', '96px'], 'concentric with the trailing end');
    const rtl = [seg(200, 100), seg(100, 100), seg(0, 100)];
    assert.deepEqual(at(rtl[0], rtl), ['200px', '96px'], 'right to left the first option stands at the right end');
    assert.deepEqual(at(seg(0, 0)), ['200px', '96px'], 'not laid out: it stays where it is');
});

test('a press opens a float panel above the capsule, inside the composer\'s column; focus goes into it and the capsule names it', () => {
    const c = capsule();
    assert.deepEqual([c.node.getAttribute('role'), c.node.getAttribute('aria-label'), c.node.dataset.kind], ['dialog', 'Context', 'context']);
    assert.deepEqual([c.head.getAttribute('aria-haspopup'), c.head.getAttribute('aria-controls'), c.head.getAttribute('aria-expanded')],
        ['dialog', c.node.id, 'false']);
    assert.equal(c.node.querySelector('.capsule-panel-title').textContent, 'Context', 'the capsule\'s name heads it');
    assert.equal(c.inBody(), false);
    c.press();
    assert.equal(c.panel.isOpen(), true);
    assert.deepEqual([c.el.dataset.open, c.head.getAttribute('aria-expanded'), c.node.dataset.place], ['true', 'true', 'float']);
    assert.equal(c.node.hasAttribute('aria-modal'), false, 'a float panel leaves the page live');
    assert.equal(c.inBody(), true, 'the panel stands in the document, never in the row');
    assert.equal(c.doc.activeElement, c.node, 'a press moves focus into the panel');
    // Its leading edge on the capsule's, its foot 8 px above the capsule, growing from the capsule's middle.
    assert.deepEqual([c.node.style.left, c.node.style.bottom, c.node.style.transformOrigin], ['200px', `${800 - 600 + 8}px`, '60px 100%']);
    assert.equal(c.node.style.getPropertyValue('--panel-column'), '600px');
    assert.deepEqual(c.repaints, [[true, 'true']], 'opening paints the strip once, its marks landing where they stand');
    assert.equal(c.node.dataset.settling, undefined);
    // A capsule near the column's end: the panel stays inside the column.
    c.el.getBoundingClientRect = () => ({ left: 500, right: 620, width: 120, top: 600, bottom: 636 });
    c.win.fire('resize');
    assert.deepEqual([c.node.style.left, c.node.style.transformOrigin], [`${700 - 440}px`, `${500 + 60 - 260}px 100%`]);
    assert.equal(c.repaints.length, 2, 'a resize lands the strip\'s marks again');
    c.press();
    assert.deepEqual([c.panel.isOpen(), c.el.dataset.open, c.head.getAttribute('aria-expanded'), c.inBody()], [false, 'false', 'false', false]);
    assert.equal(c.doc.activeElement, c.head, 'the same capsule again closes it, focus back on the capsule');
    assert.deepEqual(c.repaints.at(-1), [false, undefined]);
    c.press(0);  // Enter / Space
    assert.equal(c.doc.activeElement, c.option, 'the keyboard lands on the chosen option');
    c.panel.destroy();
});

test('Escape closes and returns focus; an Escape another layer took, or one with nothing open, is left alone', () => {
    const c = capsule();
    c.press(0);
    const taken = escape(true);
    c.doc.fire('keydown', taken);
    assert.equal(c.panel.isOpen(), true, 'a dialog above it took this Escape');
    const mine = escape();
    c.doc.fire('keydown', mine);
    assert.deepEqual([mine.defaultPrevented, c.panel.isOpen(), c.doc.activeElement], [true, false, c.head],
        'the panel takes its Escape: a Project room under it stays open');
    const later = escape();
    c.doc.fire('keydown', later);
    assert.equal(later.defaultPrevented, false, 'closed, Escape is the room\'s');
    c.doc.fire('keydown', { key: 'Enter', defaultPrevented: false, preventDefault() { throw new Error('not mine'); } });
    c.panel.destroy();
});

test('an outside press closes and leaves focus to the press; a press inside, or on another capsule of a composer row, does not', () => {
    const c = capsule();
    c.press();
    c.down(c.option);
    c.down(c.head);
    assert.equal(c.panel.isOpen(), true, 'inside the panel, or the capsule itself (its click answers)');
    const other = new ElementStub('button', c.doc);
    other.closest = (selector) => (selector === '.chat-toolbar-row .chat-capsule-head' ? other : null);
    c.down(other);
    assert.equal(c.panel.isOpen(), true, 'another capsule\'s press is its own: it switches to its panel at once');
    c.down(new ElementStub('div', c.doc));
    assert.deepEqual([c.panel.isOpen(), c.inBody(), c.doc.activeElement], [false, false, c.node]);
    c.panel.destroy();
});

test('one panel is open at a time on the page: opening another closes the first at once', () => {
    const doc = stubDocument();
    const main = capsule({ doc });
    const project = capsule({ doc });
    main.press();
    project.press();
    assert.deepEqual([main.panel.isOpen(), main.el.dataset.open, main.inBody()], [false, 'false', false]);
    assert.deepEqual([project.panel.isOpen(), project.inBody()], [true, true]);
    main.press();
    assert.deepEqual([main.panel.isOpen(), project.panel.isOpen()], [true, false]);
    main.panel.destroy();
    project.panel.destroy();
});

test('focus that leaves the panel for elsewhere closes it; to the capsule or to nowhere it stays', () => {
    const c = capsule();
    c.press();
    fire(c.node, 'focusout', { relatedTarget: c.head });
    fire(c.node, 'focusout', { relatedTarget: null });
    fire(c.node, 'focusout', { relatedTarget: c.option });
    assert.equal(c.panel.isOpen(), true);
    fire(c.node, 'focusout', { relatedTarget: new ElementStub('textarea', c.doc) });
    assert.equal(c.panel.isOpen(), false, 'Tab out of the panel closes it');
    c.panel.destroy();
});

test('a phone: a bottom sheet over a scrim; the scrim closes on its click, a swipe down past a third or a flick closes', () => {
    const c = capsule({ phone: true });
    c.press();
    assert.equal(c.node.dataset.place, 'sheet');
    assert.equal(c.node.getAttribute('aria-modal'), 'true', 'a sheet over its scrim is modal: Back gives it Escape first (layers.js)');
    const scrim = c.doc.body.children.find((node) => node.classList.contains('capsule-panel-scrim'));
    assert.ok(scrim, 'a light scrim under the sheet');
    assert.deepEqual(c.doc.body.children.map((node) => node.className), ['capsule-panel-scrim', 'capsule-panel'], 'the sheet stands over its scrim');
    assert.equal(scrim.getAttribute('aria-hidden'), 'true');
    assert.equal(c.node.style.left || '', '', 'the sheet\'s place is the stylesheet\'s: no float placement');
    c.down(scrim);
    assert.equal(c.panel.isOpen(), true, 'not on the press: what lies under the scrim is never pressed');
    scrim.click();
    assert.deepEqual([c.panel.isOpen(), c.doc.body.children.length], [false, 0], 'the scrim\'s click closes, the scrim goes with it');
    assert.equal(c.node.hasAttribute('aria-modal'), false, 'closed, it is no dialog for Back to reach');
    // A swipe from the grabber: a short one springs back, past a third of its height it goes.
    const grab = c.node.querySelector('.capsule-panel-grab');
    const swipe = (from, to, ms, at = 0) => {
        fire(grab, 'pointerdown', { button: 0, pointerId: 1, clientY: from, timeStamp: at });
        last(c.node, 'pointermove')({ pointerId: 1, clientY: to, timeStamp: at + ms });
        last(c.node, 'pointerup')({ pointerId: 1, clientY: to, timeStamp: at + ms });
    };
    c.press();
    fire(grab, 'pointerdown', { button: 0, pointerId: 1, clientY: 500, timeStamp: 0 });
    last(c.node, 'pointermove')({ pointerId: 1, clientY: 540, timeStamp: 200 });
    assert.equal(c.node.style.transform, 'translateY(40px)', 'the sheet follows the finger');
    last(c.node, 'pointermove')({ pointerId: 2, clientY: 700, timeStamp: 210 });
    assert.equal(c.node.style.transform, 'translateY(40px)', 'another finger is not this swipe');
    last(c.node, 'pointerup')({ pointerId: 1, clientY: 540, timeStamp: 220 });
    assert.deepEqual([c.panel.isOpen(), c.node.style.transform], [true, ''], 'a short swipe springs back');
    swipe(500, 620, 400, 1000);
    assert.deepEqual([c.panel.isOpen(), c.doc.activeElement], [false, c.head], 'past a third of 300 px it goes; focus back on the capsule');
    c.press();
    swipe(500, 530, 10, 2000);
    assert.equal(c.panel.isOpen(), false, 'a flick goes however short');
    c.panel.destroy();
});

test('the same panel opened as a float and then as a sheet keeps none of the float\'s geometry, and back', () => {
    const c = capsule();
    const geometry = () => [c.node.style.left || '', c.node.style.bottom || '', c.node.style.transformOrigin || '',
        c.node.style.getPropertyValue('--panel-column')];
    c.press();
    assert.deepEqual(geometry(), ['200px', `${800 - 600 + 8}px`, '60px 100%', '600px'], 'a float stands above its capsule');
    c.press();
    c.media.phone = true;  // a phone turned from landscape to portrait
    c.press();
    assert.equal(c.node.dataset.place, 'sheet');
    assert.deepEqual(geometry(), ['', '', '', ''], 'a sheet stands where the stylesheet puts it');
    c.press();
    c.media.phone = false;
    c.press();
    assert.deepEqual(geometry(), ['200px', `${800 - 600 + 8}px`, '60px 100%', '600px'], 'and a float is placed afresh');
    c.panel.destroy();
});

test('a sheet closed mid-swipe ends the swipe: the finger moves or closes nothing after it', () => {
    for (const how of ['escape', 'scrim']) {
        const c = capsule({ phone: true });
        c.node.removeEventListener = (type, fn) => {  // a real removal, as in a browser
            const listeners = c.node.listeners.get(type) || [];
            if (listeners.includes(fn)) listeners.splice(listeners.indexOf(fn), 1);
        };
        const swipeListeners = () => ['pointermove', 'pointerup', 'pointercancel'].map((type) => (c.node.listeners.get(type) || []).length);
        c.press();
        fire(c.node.querySelector('.capsule-panel-grab'), 'pointerdown', { button: 0, pointerId: 1, clientY: 500, timeStamp: 0 });
        fire(c.node, 'pointermove', { pointerId: 1, clientY: 540, timeStamp: 100 });
        assert.equal(c.node.style.transform, 'translateY(40px)');
        assert.deepEqual(swipeListeners(), [1, 1, 1], 'the swipe listens while the finger draws');
        if (how === 'escape') c.doc.fire('keydown', escape());
        else c.doc.body.children.find((node) => node.classList.contains('capsule-panel-scrim')).click();
        assert.equal(c.panel.isOpen(), false, how);
        assert.deepEqual(swipeListeners(), [0, 0, 0], `${how}: the swipe ended with the sheet`);
        c.press();  // the same finger, still down, crosses the reopened sheet and lets go far below
        fire(c.node, 'pointermove', { pointerId: 1, clientY: 760, timeStamp: 200 });
        fire(c.node, 'pointerup', { pointerId: 1, clientY: 760, timeStamp: 210 });
        assert.deepEqual([c.panel.isOpen(), c.node.style.transform], [true, ''], `${how}: the reopened sheet stays where it is`);
        c.panel.destroy();
    }
});

test('a resize across the phone\'s width closes the panel at once; a desk panel never swipes', () => {
    const c = capsule();
    c.press();
    fire(c.node.querySelector('.capsule-panel-grab'), 'pointerdown', { button: 0, pointerId: 1, clientY: 500, timeStamp: 0 });
    assert.equal(c.node.listeners.get('pointermove'), undefined, 'no swipe on a float panel');
    c.media.phone = true;
    c.win.fire('resize');
    assert.deepEqual([c.panel.isOpen(), c.inBody()], [false, false]);
    c.press();
    assert.equal(c.node.dataset.place, 'sheet', 'the next open is a sheet');
    c.panel.destroy();
});

test('motion: a float panel fades in from the capsule and leaves as its picture; a sheet rises on --ease-sheet; reduced motion moves nothing', async () => {
    const played = [];
    const desk = capsule({ doc: animatedDocument(played), motion: true });
    desk.press();
    assert.deepEqual(played.map(({ node, frames, options }) => [node, frames, options]), [[desk.node,
        [{ opacity: 0, transform: 'scale(0.96)' }, { opacity: 1, transform: 'none' }], { duration: 180, easing: TOKENS['--ease-sheet'] }]]);
    desk.press();
    assert.equal(desk.inBody(), false, 'the panel itself goes at once');
    const picture = played.at(-1).node;
    assert.deepEqual([picture.hasAttribute('data-leaving'), picture.getAttribute('aria-hidden'), picture.inert], [true, 'true', true],
        'its picture plays the leaving');
    assert.deepEqual([played.at(-1).frames, played.at(-1).options], [[{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'scale(0.96)' }],
        { duration: 180, easing: 'cubic-bezier(1, 0, 0.68, 0.28)', fill: 'forwards' }]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(desk.doc.body.children.includes(picture), false, 'and is gone once it played');
    desk.panel.destroy();

    played.length = 0;
    const phone = capsule({ doc: animatedDocument(played), motion: true, phone: true });
    phone.press();
    assert.deepEqual(played.map(({ frames, options }) => [frames, options]), [
        [[{ transform: 'translateY(100%)' }, { transform: 'none' }], { duration: 340, easing: TOKENS['--ease-sheet'] }],
        [[{ opacity: 0 }, { opacity: 1 }], { duration: 340, easing: TOKENS['--ease-sheet'] }],
    ], '--motion-open on --ease-sheet, the scrim with it');
    played.length = 0;
    phone.doc.fire('keydown', escape());
    assert.deepEqual(played.map(({ frames, options }) => [frames, options.duration, options.easing]), [
        [[{ transform: 'translateY(0px)' }, { transform: 'translateY(100%)' }], 280, TOKENS['--ease-sheet']],
        [[{ opacity: 1 }, { opacity: 0 }], 280, TOKENS['--ease-sheet']],
    ], '--motion-close on --ease-sheet');
    phone.panel.destroy();

    played.length = 0;
    const still = capsule({ doc: animatedDocument(played), motion: true, reduce: true });
    still.press();
    still.press();
    assert.deepEqual([played, still.inBody()], [[], false], 'reduced motion: it appears and goes at once');
    still.panel.destroy();
});

test('destroy closes the panel and lets go of the document and the window', () => {
    const c = capsule();
    assert.deepEqual([c.doc.count('pointerdown'), c.doc.count('keydown'), c.win.count('resize')], [1, 1, 1]);
    c.press();
    c.panel.destroy();
    assert.deepEqual([c.panel.isOpen(), c.inBody()], [false, false]);
    assert.deepEqual([c.doc.count('pointerdown'), c.doc.count('keydown'), c.win.count('resize')], [0, 0, 0]);
    c.panel.open();
    assert.equal(c.panel.isOpen(), false, 'a destroyed panel never opens');
});

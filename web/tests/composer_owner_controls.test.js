// The composer's owner controls (docs/DESIGN.md "Composer owner controls"): Context and Effort, two labelled capsules
// whose strips live in their panels (the panel itself: capsule_panel.test.js). The effort range's handle math, its
// panel opened by a press or the keyboard, keyboard and pointer, serialized saves with a refused save returning to the
// server's value, independent room readback, teardown; the context capsule's three modes, their lines and the save;
// one capsule open at a time, and the closed row's names that give way on a narrow line.
import test from 'node:test';
import assert from 'node:assert/strict';

import { ElementStub } from './chat_dom_fixture.js';
import { applyHandle, snapRange } from '../modules/composer_owner_controls.js';
import { mount, mountPair, sleep, stubWindow } from './composer_controls_fixture.js';

test('primary and helper bounds are independent, and helper bounds cannot cross', () => {
    const base = { min: 1, rec: 2, max: 3 };
    for (const rec of [0, 3, 6]) assert.deepEqual(applyHandle(base, 'rec', rec), { ...base, rec });
    assert.deepEqual(applyHandle(base, 'min', 6), { min: 3, rec: 2, max: 3 });
    assert.deepEqual(applyHandle(base, 'max', -4), { min: 1, rec: 2, max: 1 });
    assert.deepEqual(applyHandle(base, 'rec', 9), { min: 1, rec: 6, max: 3 });
    assert.deepEqual(snapRange({ min: 0.6, rec: 0.2, max: 2.4 }), { min: 1, rec: 0, max: 2 });
});

test('the control mounts after the context capsule, closed: ring, name and the recommended level only; its strip waits in the panel, three sliders out of the tab order', () => {
    const m = mount();
    assert.equal(m.el.dataset.open, 'false');
    assert.ok(m.el.classList.contains('chat-capsule'));
    assert.match(m.el.innerHTML, /class="chat-capsule-name">Effort</);
    // The capsule names only the recommended level: no range in words (the ring and the button's name carry the
    // range, the panel's line says it).
    assert.equal(m.el.querySelector('.chat-capsule-value').textContent, 'Medium');
    assert.equal(m.el.querySelector('.chat-capsule-value').dataset.i18nSkip, '', 'a catalog word the overlay leaves alone');
    assert.equal(m.head.getAttribute('aria-expanded'), 'false');
    assert.deepEqual([m.head.getAttribute('aria-haspopup'), m.head.getAttribute('aria-controls')], ['dialog', m.panel.id]);
    assert.equal(m.el.querySelector('[data-effort-strip]'), null, 'the row holds the capsule alone');
    assert.equal(m.doc.body.children.includes(m.panel), false, 'closed, the panel is not in the document');
    // The recommended level may go anywhere (it pushes the brackets); a bracket stops at the other one.
    const bounds = { min: ['0', '3'], rec: ['0', '6'], max: ['1', '6'] };
    for (const which of ['min', 'rec', 'max']) {
        assert.match(m.strip.innerHTML, new RegExp(`data-handle="${which}" role="slider" tabindex="-1"`));
        assert.equal(m.handles[which].tabIndex, -1);
        assert.deepEqual([m.handles[which].getAttribute('aria-valuemin'), m.handles[which].getAttribute('aria-valuemax')], bounds[which], which);
    }
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 3 }, 'the default before the first /api/state');
    assert.equal(m.el.dataset.known, 'false');
    assert.equal(m.handles.rec.getAttribute('aria-valuetext'), 'Medium');
    assert.match(m.head.title, /^Primary effort: Medium; helpers Low–High\. Applies to new participants in this room\.$/);
    assert.equal(m.head.getAttribute('aria-label'), m.head.title, 'the closed button says the state, not just "Effort"');
    assert.match(m.el.querySelector('[data-ring-band]').getAttribute('d'), /^M[\d.]+ [\d.]+ A70 70 0 0 1 /);
    assert.equal(m.segs.length, 7);
    // The strip is the seven levels alone: no title word of its own, no separator element, no reset button.
    for (const gone of ['.chat-effort-label', '.chat-effort-sep', '.chat-effort-reset']) assert.equal(m.panel.querySelector(gone), null, gone);
    const line = m.panel.querySelector('.capsule-panel-line[data-value="now"]');
    assert.equal(line.textContent, 'Primary Medium; helpers Low–High.', 'the panel\'s line says the range');
    assert.equal(line.dataset.i18nFmt, 'Primary {level}; helpers {min}–{max}.', 'a composed line the overlay leaves alone');
    assert.equal(m.panel.querySelector('.capsule-panel-rule').textContent, 'Applies to new work in this chat; running work keeps its level.');
    assert.deepEqual(m.segs.map((seg) => seg.dataset.in), ['false', 'true', 'true', 'true', 'false', 'false', 'false']);
    assert.deepEqual(m.segs.map((seg) => seg.dataset.rec), ['false', 'false', 'true', 'false', 'false', 'false', 'false']);
    assert.ok(m.row.children.includes(m.el));
});

test('an authoritative room value paints the control; a stored minimal shows at Low', () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'minimal', recommended: 'high', max: 'ultra' } });
    assert.equal(m.el.dataset.known, 'true');
    assert.deepEqual(m.control.shown(), { min: 1, rec: 3, max: 6 });
    assert.deepEqual(m.control.stored(), { min: 'minimal', recommended: 'high', max: 'ultra' });
    assert.equal(m.handles.min.getAttribute('aria-valuetext'), 'Low');
    assert.equal(m.el.querySelector('.chat-capsule-value').textContent, 'High', 'the closed capsule follows the saved level');
    m.control.syncState({ context_mode: 'max' });
    assert.deepEqual(m.control.shown(), { min: 1, rec: 3, max: 6 }, 'a snapshot without the range changes nothing');
});

test('a press opens the panel and focus goes into it; Enter or Space lands on the primary handle; Escape, the capsule or an outside press closes', () => {
    const m = mount();
    m.headClick(1);
    assert.equal(m.control.isOpen(), true);
    assert.deepEqual([m.el.dataset.open, m.head.getAttribute('aria-expanded')], ['true', 'true']);
    assert.ok(m.doc.body.children.includes(m.panel), 'the panel joins the document, not the row');
    assert.equal(m.panel.dataset.place, 'float', 'on a desk: a float panel');
    assert.equal(m.handles.rec.tabIndex, 0, 'open, the sliders take the tab order');
    assert.equal(m.doc.activeElement, m.panel, 'a press moves focus into the panel');
    m.headClick(1);
    assert.equal(m.control.isOpen(), false, 'the same capsule again closes it');
    assert.equal(m.doc.body.children.includes(m.panel), false);
    assert.equal(m.doc.activeElement, m.head, 'focus comes back to the capsule');
    assert.equal(m.handles.rec.tabIndex, -1);
    m.headClick(0);  // Enter / Space
    assert.equal(m.doc.activeElement, m.handles.rec, 'the keyboard lands on the primary handle');
    const escape = m.escape();
    assert.equal(escape.defaultPrevented, true, 'the panel took the Escape: a Project room under it stays open');
    assert.equal(m.control.isOpen(), false);
    assert.equal(m.doc.activeElement, m.head, 'Escape returns focus to the capsule');
    assert.equal(m.escape().defaultPrevented, false, 'closed, Escape is the room\'s');
    m.headClick(1);
    m.inside();
    assert.equal(m.control.isOpen(), true, 'a press inside keeps it open');
    m.down(10); m.up(10);
    assert.equal(m.control.isOpen(), true, 'a choice keeps it open: its line says the new range');
    m.outside();
    assert.equal(m.control.isOpen(), false, 'an outside press closes');
    assert.equal(m.doc.activeElement, m.handles.rec, 'an outside press leaves focus to the press');
});

test('keyboard moves primary beyond either bound while each helper handle leaves primary alone', async () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.key('rec', 'End');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'ultra', max: 'high' });
    m.key('rec', 'Home');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'none', max: 'high' });
    m.key('min', 'End');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'high', recommended: 'none', max: 'high' });
    m.key('max', 'Home');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 3, rec: 0, max: 3 });
    m.key('max', 'PageUp');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'high', recommended: 'none', max: 'xhigh' });
    m.control.destroy();
});

test('a stored tier outside the owner tiers survives until the owner moves that handle', async () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'minimal', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.key('max', 'ArrowRight');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'minimal', recommended: 'medium', max: 'xhigh' }, 'min keeps its stored minimal');
    m.key('min', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'none', recommended: 'medium', max: 'xhigh' }, 'moved: rewritten to an owner tier');
    m.control.syncState({ effort_range: { min: 'minimal', recommended: 'medium', max: 'high' } });
    m.key('rec', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 1, rec: 1, max: 3 }, 'rec meets the shown minimum');
    assert.deepEqual(m.saves.at(-1), { min: 'minimal', recommended: 'low', max: 'high' }, 'the untouched minimum keeps minimal');
    m.key('rec', 'ArrowLeft');
    await m.settle();
    assert.deepEqual(m.control.shown(), { min: 1, rec: 0, max: 3 }, 'primary may pass the shown minimum');
    assert.deepEqual(m.saves.at(-1), { min: 'minimal', recommended: 'none', max: 'high' }, 'the untouched bound keeps its stored tier');
});

test('pointer taps anywhere choose primary, a drag never moves helper bounds, and cancel restores', async () => {
    const m = mount();
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(1);
    m.down(60);
    m.move(250);
    assert.equal(m.handles.rec.style.left, '240px');
    assert.equal(m.handles.max.getAttribute('aria-valuenow'), '3');
    m.up(250);
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'ultra', max: 'high' });
    m.down(10); m.up(10);
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'none', max: 'high' });
    m.down(165, { handle: m.handles.max }); m.move(285); m.up(285);
    await m.settle();
    assert.deepEqual(m.saves.at(-1), { min: 'low', recommended: 'none', max: 'ultra' });
    const before = m.saves.length;
    m.down(10); m.move(270); m.cancel();
    await m.settle();
    assert.equal(m.saves.length, before);
    assert.deepEqual(m.control.shown(), { min: 1, rec: 0, max: 6 });
    m.down(10); m.move(200, 2); m.up(200, 2);
    assert.equal(m.handles.rec.dataset.dragging, 'true');
    m.up(10); await m.settle();
    assert.equal(m.saves.length, before);
    m.control.destroy();
});

test('saves are serialized, last wins; Send can wait for them; a refused save returns to the server value and says why', async () => {
    const pending = [];
    const m = mount({
        save: (triple) => new Promise((resolve, reject) => pending.push({ triple, resolve, reject })),
    });
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    assert.equal(m.control.hasPendingSave(), false);
    m.key('rec', 'ArrowRight');
    assert.equal(m.control.hasPendingSave(), true);
    assert.equal(m.el.dataset.saving, 'true');
    m.key('rec', 'ArrowLeft');
    m.key('max', 'ArrowRight');
    assert.equal(pending.length, 1, 'one request in flight; later gestures wait');
    assert.deepEqual(pending[0].triple, { min: 'low', recommended: 'high', max: 'high' });
    m.control.syncState({ effort_range: { min: 'none', recommended: 'none', max: 'none' } });
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 4 }, 'a stale poll never overwrites a save in flight');
    const waiter = m.control.pendingSave();
    pending[0].resolve({ ok: true, effort_range: pending[0].triple });
    await sleep(0);
    assert.equal(pending.length, 2, 'the latest queued triple follows, the superseded one never leaves');
    assert.deepEqual(pending[1].triple, { min: 'low', recommended: 'medium', max: 'xhigh' });
    pending[1].resolve({ ok: true, effort_range: pending[1].triple });
    assert.equal(await waiter, true);
    assert.equal(m.control.hasPendingSave(), false);
    assert.equal(m.el.dataset.saving, 'false');
    assert.deepEqual(m.control.stored(), { min: 'low', recommended: 'medium', max: 'xhigh' });
    assert.deepEqual(m.refreshes, [true]);
    // A refusal: the server's sentence as a toast, the control back at the server's value.
    m.key('min', 'ArrowLeft');
    assert.deepEqual(m.control.shown(), { min: 0, rec: 2, max: 4 });
    const refused = m.control.pendingSave();
    pending[2].reject(Object.assign(new Error('Effort range must be ordered: minimum at most the recommended level.'), { status: 400 }));
    assert.equal(await refused, false);
    assert.deepEqual(m.toasts, [['Effort range must be ordered: minimum at most the recommended level.', 'error']]);
    assert.deepEqual(m.control.shown(), { min: 1, rec: 2, max: 4 }, 'the unsaved preview is gone');
    assert.deepEqual(m.control.stored(), { min: 'low', recommended: 'medium', max: 'xhigh' });
    assert.equal(await m.control.pendingSave(), false, 'the last outcome stays readable');
});

test('an edit made while saves are on their way builds on the latest choice, never on an older answer', async () => {
    const pending = [];
    const m = mount({ save: (triple) => new Promise((resolve, reject) => pending.push({ triple, resolve, reject })) });
    m.control.syncState({ effort_range: { min: 'low', recommended: 'medium', max: 'high' } });
    m.headClick(0);
    m.key('min', 'Home');        // in flight: none · medium · high
    m.key('rec', 'ArrowRight');  // queued:    none · high · high
    pending[0].resolve({ ok: true, effort_range: pending[0].triple });
    await sleep(0);
    assert.deepEqual(m.control.shown(), { min: 0, rec: 3, max: 3 }, 'the older answer does not pull the draft back');
    m.key('max', 'End');         // while the second save is in flight
    pending[1].resolve({ ok: true, effort_range: pending[1].triple });
    await sleep(0);
    pending[2].resolve({ ok: true, effort_range: pending[2].triple });
    assert.equal(await m.control.pendingSave(), true);
    assert.deepEqual(pending.map((p) => p.triple), [
        { min: 'none', recommended: 'medium', max: 'high' },
        { min: 'none', recommended: 'high', max: 'high' },
        { min: 'none', recommended: 'high', max: 'ultra' },
    ]);
    assert.deepEqual(m.control.stored(), { min: 'none', recommended: 'high', max: 'ultra' });
    assert.deepEqual(m.control.shown(), { min: 0, rec: 3, max: 6 });
    // A refusal drops edits queued on top of it and returns to the server's value.
    m.key('rec', 'ArrowLeft');
    m.key('rec', 'ArrowLeft');
    pending[3].reject(new Error('Could not save.'));
    assert.equal(await m.control.pendingSave(), false);
    assert.equal(pending.length, 4, 'the edit queued on the refused draft never leaves');
    assert.deepEqual(m.control.shown(), { min: 0, rec: 3, max: 6 });
});

test('destroy closes the panel and removes the capsule and its document and window listeners', () => {
    const win = stubWindow();
    const m = mount({ win });
    assert.deepEqual([m.doc.count('pointerdown'), m.doc.count('keydown'), win.count('resize')], [1, 1, 1]);
    m.headClick(1);
    m.control.destroy();
    assert.deepEqual([m.doc.count('pointerdown'), m.doc.count('keydown'), win.count('resize')], [0, 0, 0]);
    assert.ok(!m.row.children.includes(m.el));
    assert.equal(m.doc.body.children.includes(m.panel), false, 'an open panel goes with it');
    assert.equal(m.control.isOpen(), false);
});

test('the context capsule: ring, Context and the mode; its panel holds exactly three modes, one tab stop among them, and the chosen mode\'s line', async () => {
    const m = mountPair();
    assert.ok(m.contextEl.classList.contains('chat-capsule'));
    assert.equal(m.contextEl.getAttribute('aria-label'), 'Context size mode');
    assert.match(m.contextEl.innerHTML, /class="chat-capsule-name">Context</);
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Max');
    assert.match(m.contextHead.title, /^Context mode \(owner setting\)\. Nano is the compact owner window/);
    assert.equal(m.contextEl.querySelectorAll('.chat-seg').length, 0, 'the row holds the capsule alone');
    const segs = m.contextStrip.querySelectorAll('.chat-seg');
    // Three modes, a position among them, never a fill level: three arcs and three radios.
    assert.deepEqual(segs.map((node) => node.dataset.mode), ['nano', 'low', 'max']);
    assert.equal(m.contextEl.querySelectorAll('.chat-context-arc').length, 3);
    assert.ok(m.panels.context.querySelectorAll('[data-mode]').every((node) => node.classList.contains('chat-seg')),
        'inside the panel [data-mode] still means a mode button');
    assert.deepEqual(segs.map((node) => node.getAttribute('aria-checked')), ['false', 'false', 'true']);
    assert.deepEqual(segs.map((node) => node.tabIndex), [-1, -1, -1], 'closed: out of the tab order');
    await m.press(m.contextHead, 0);  // Enter / Space
    assert.equal(m.contextEl.dataset.open, 'true');
    assert.equal(m.contextHead.getAttribute('aria-expanded'), 'true');
    assert.deepEqual(segs.map((node) => node.tabIndex), [-1, -1, 0], 'one tab stop: the chosen mode');
    assert.equal(m.doc.activeElement, m.seg('max'), 'the keyboard lands on the chosen mode');
    assert.equal(m.panels.context.dataset.mode, 'max', 'the panel wears the mode it shows');
    const lines = m.panels.context.querySelectorAll('.capsule-panel-line');
    assert.deepEqual(lines.map((line) => [line.dataset.value, line.dataset.on, line.textContent]), [
        ['nano', 'false', 'About 85K tokens: the compact window.'],
        ['low', 'false', 'About 250K tokens; fits local models.'],
        ['max', 'true', "Everything the model's window holds."],
    ], 'every line stays in its cell; only the chosen one shows');
    assert.equal(m.panels.context.querySelector('.capsule-panel-rule').textContent, 'Applies to new work in this chat; running work keeps its context.');
    const escape = m.escape();
    assert.equal(m.contextEl.dataset.open, 'false');
    assert.equal(escape.defaultPrevented, true, 'the panel took the Escape: a Project room under it stays open');
    assert.equal(m.doc.activeElement, m.contextHead, 'Esc returns focus to the head');
    assert.equal(m.escape().defaultPrevented, false, 'a closed capsule leaves Escape to the room');
    m.controls.syncState({ context_mode: 'nano' });
    assert.equal(m.contextEl.dataset.contextMode, 'nano', 'the Main composer reads Main mode');
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Nano');
    m.controls.syncState({ context_mode: 'huge' });
    assert.equal(m.contextEl.dataset.contextMode, 'max', 'an unknown mode reads as Max');
    m.controls.destroy();
});

test('a context choice posts the owner endpoint at once, waits for the answer, shows a refusal and always re-reads /api/state', async () => {
    const m = mountPair();
    await m.press(m.contextHead);
    await m.choose('low');
    assert.deepEqual(m.posts, [['/api/owner/context-mode', { mode: 'low' }]]);
    assert.equal(m.contextEl.dataset.contextMode, 'low');
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Low');
    assert.equal(m.seg('low').getAttribute('aria-checked'), 'true');
    assert.deepEqual([m.contextEl.dataset.open, m.panels.context.dataset.mode], ['true', 'low'], 'the panel stays open on the new mode');
    assert.equal(m.panels.context.querySelector('.capsule-panel-line[data-on="true"]').dataset.value, 'low');
    assert.equal(m.contextEl.dataset.disabled, 'false');
    assert.deepEqual(m.refreshes, [true]);
    await m.choose('low');
    assert.equal(m.posts.length, 1, 'the current mode is not re-posted');
    m.answer.value = { ok: false, json: async () => ({ error: 'Context mode can only be lowered while Ouroboros is idle.' }) };
    await m.choose('max');
    assert.equal(m.contextEl.dataset.contextMode, 'low', 'a refusal leaves the saved mode');
    assert.equal(m.contextEl.querySelector('.chat-capsule-value').textContent, 'Low');
    assert.deepEqual(m.toasts, [['Context mode can only be lowered while Ouroboros is idle.', 'error']]);
    assert.deepEqual(m.refreshes, [true, true]);
    // While a save is on its way the strip waits: a second choice and a stale poll change nothing.
    let release;
    m.answer.value = new Promise((resolve) => { release = resolve; });
    const pending = m.choose('nano');
    assert.equal(m.contextEl.dataset.disabled, 'true');
    await m.choose('max');
    m.controls.syncState({ context_mode: 'max' });
    assert.equal(m.contextEl.dataset.contextMode, 'low', 'the save answers for itself, not a stale poll');
    release({ ok: true });
    await pending;
    assert.deepEqual(m.posts.map(([, body]) => body.mode), ['low', 'max', 'nano']);
    assert.equal(m.contextEl.dataset.contextMode, 'nano');
    // Arrows move to a neighbour and choose it; Home and End go to the ends.
    m.answer.value = { ok: true };
    await m.key('nano', 'ArrowRight');
    assert.equal(m.doc.activeElement, m.seg('low'));
    assert.equal(m.contextEl.dataset.contextMode, 'low');
    await m.key('low', 'End');
    assert.equal(m.contextEl.dataset.contextMode, 'max');
    await m.key('max', 'ArrowDown');
    assert.equal(m.posts.length, 5, 'past the end nothing moves and nothing is posted');
    assert.equal(m.controls.hasPendingSave(), false, 'both saves are settled');
    assert.equal(await m.controls.pendingSave(), true);
    m.controls.destroy();
});

test('one capsule is open at a time: a press on another capsule switches to its panel at once; an outside press closes', async () => {
    const m = mountPair();
    await m.press(m.contextHead);
    assert.equal(m.contextEl.dataset.open, 'true');
    // The press on the other capsule is its own to answer: the document handler leaves it alone,
    // and opening Effort closes Context at once.
    m.docDown(m.effortHead);
    assert.equal(m.contextEl.dataset.open, 'true');
    await m.press(m.effortHead);
    assert.deepEqual([m.contextEl.dataset.open, m.effortEl.dataset.open], ['false', 'true']);
    assert.deepEqual(m.open(), [m.panels.effort], 'one panel in the document');
    await m.press(m.contextHead);
    assert.deepEqual([m.contextEl.dataset.open, m.effortEl.dataset.open], ['true', 'false']);
    assert.deepEqual(m.open(), [m.panels.context]);
    m.docDown(new ElementStub('div', m.doc));
    assert.deepEqual([m.contextEl.dataset.open, m.open()], ['false', []], 'an outside press closes it');
    m.controls.destroy();
});

test('the capsules keep their names while the line holds them; a narrower line keeps rings and values; a panel changes nothing in the row', async () => {
    const m = mountPair();
    const line = (top) => { m.effortEl.getBoundingClientRect = () => ({ top, bottom: top + 32, width: 130 }); };
    line(40);                         // the names push Effort onto a second line
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, 'true');
    line(0);
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, undefined, 'room again: the names come back');
    await m.press(m.contextHead);
    assert.deepEqual(m.row.children, [m.subagentsEl, m.contextEl, m.effortEl], 'the open panel is not in the row');
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, undefined, 'the row keeps its fit while a panel is open');
    m.controls.destroy();
});

test('a phone\'s ☰, gone down beside the field while the row folds, is not the line the capsules are read by', () => {
    const m = mountPair();
    const menu = new ElementStub('button', m.doc);
    menu.className = 'mobile-nav-toggle composer-menu';
    menu.getBoundingClientRect = () => ({ top: 21, bottom: 53, width: 32 });  // composer_fold.js: at the field's middle
    m.row.prepend(menu);
    m.effortEl.getBoundingClientRect = () => ({ top: 40, bottom: 72, width: 130 });  // the names push Effort onto a second line
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, 'true', 'the capsules\' own line still wraps: the row keeps its fit');
    m.effortEl.getBoundingClientRect = () => ({ top: 0, bottom: 32, width: 130 });
    m.controls.syncState({});
    assert.equal(m.row.dataset.ownerCompact, undefined, 'room again: the names come back');
    m.controls.destroy();
});

test('docking-box geometry and changed words fit the closed row before paint, with full disposal', () => {
    const sizes = [], texts = [], frames = new Map();
    let nextFrame = 0;
    const win = {
        ...stubWindow(),
        ResizeObserver: class {
            constructor(callback) { this.callback = callback; this.nodes = []; sizes.push(this); }
            observe(node) { this.nodes.push(node); }
            disconnect() { this.disconnected = true; }
        },
        MutationObserver: class {
            constructor(callback) { this.callback = callback; texts.push(this); }
            observe(node, options) { this.node = node; this.options = options; }
            disconnect() { this.disconnected = true; }
        },
        requestAnimationFrame(callback) { frames.set(++nextFrame, callback); return nextFrame; },
        cancelAnimationFrame(id) { frames.delete(id); },
    };
    const m = mountPair({ win, withBox: true });
    const boxObserver = sizes.find((observer) => observer.nodes.includes(m.box));
    assert.ok(boxObserver, 'chat supplies the independently sized docking box');
    assert.deepEqual(boxObserver.nodes, [m.box], 'not the descendant whose fit we change');
    const line = (top) => { m.effortEl.getBoundingClientRect = () => ({ top, bottom: top + 32, width: 130 }); };
    line(40);
    boxObserver.callback();
    assert.equal(m.row.dataset.ownerCompact, 'true', 'fits on the initial box delivery, without syncState');
    assert.equal(frames.size, 0, 'no one-frame deferral for independent geometry');
    assert.equal(texts.length, 1);
    assert.equal(texts[0].node, m.row);
    assert.deepEqual(texts[0].options, { childList: true, characterData: true, subtree: true });
    line(0);
    texts[0].callback();
    assert.equal(m.row.dataset.ownerCompact, undefined, 'new words fit the row again in the text microtask');
    sizes.filter((observer) => observer !== boxObserver).forEach((observer) => observer.callback());
    assert.ok(frames.size > 0, 'feedback from the changed row is still deferred');
    m.controls.destroy();
    assert.equal(frames.size, 0);
    assert.ok([...sizes, ...texts].every((observer) => observer.disconnected));
    line(40);
    boxObserver.callback();
    texts[0].callback();
    assert.equal(m.row.dataset.ownerCompact, undefined, 'already queued deliveries after destroy cannot write');
});

/* The refit of a wrapped row shrinks the composer that chat.js observes higher in the tree. Inside ResizeObserver
   delivery that is a resize of a box the pass cannot deliver (WebKit: "loop completed with undelivered notifications"),
   so the observer only schedules and the fit runs in a frame; a window resize, which precedes layout, fits at once. */
function observedWindow() {
    const observers = [];
    const frames = [];
    const cancelled = [];
    const listeners = new Map();
    class FakeObserver {
        constructor(callback) { this.callback = callback; observers.push(this); }
        observe() {}
        unobserve() {}
        disconnect() { this.disconnected = true; }
    }
    const win = {
        ...stubWindow(),
        ResizeObserver: FakeObserver,
        requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
        cancelAnimationFrame: (id) => cancelled.push(id),
        addEventListener: (type, fn) => listeners.set(type, [...(listeners.get(type) || []), fn]),
        removeEventListener: (type, fn) => listeners.set(type, (listeners.get(type) || []).filter((f) => f !== fn)),
    };
    return { win, observers, frames, cancelled, fire: (type) => (listeners.get(type) || []).forEach((fn) => fn({})), count: (type) => (listeners.get(type) || []).length };
}

test('the row refit never changes layout inside ResizeObserver delivery: the observer schedules a frame, the frame fits', () => {
    const w = observedWindow();
    const m = mountPair({ win: w.win });
    const fit = w.observers.at(-1);  // the row's own observer is the last one the controls create
    const line = (top) => { m.effortEl.getBoundingClientRect = () => ({ top, bottom: top + 32, width: 130 }); };
    line(40);  // the names push Effort onto a second line
    fit.callback([]);
    assert.equal(m.row.dataset.ownerCompact, undefined, 'nothing is written to the row while the observer delivers');
    assert.equal(w.frames.length, 1, 'one frame is asked for');
    fit.callback([]);
    fit.callback([]);
    assert.equal(w.frames.length, 1, 'a burst of deliveries asks for the one frame');
    w.frames[0]();
    assert.equal(m.row.dataset.ownerCompact, 'true', 'the frame fits the row');
    line(0);
    fit.callback([]);
    assert.equal(w.frames.length, 2, 'the next delivery can ask again once the frame ran');
    assert.equal(m.row.dataset.ownerCompact, 'true', 'room again, but not inside the delivery');
    w.frames[1]();
    assert.equal(m.row.dataset.ownerCompact, undefined, 'the names come back in the frame');
    m.controls.destroy();
});

test('a window resize fits the row immediately; destroy cancels a pending frame and the listener', () => {
    const w = observedWindow();
    const m = mountPair({ win: w.win });
    const line = (top) => { m.effortEl.getBoundingClientRect = () => ({ top, bottom: top + 32, width: 130 }); };
    line(40);
    w.fire('resize');
    assert.equal(m.row.dataset.ownerCompact, 'true', 'the resize listener fits synchronously');
    assert.equal(w.frames.length, 0, 'and no frame was needed');
    line(0);
    w.observers.at(-1).callback([]);
    assert.equal(w.frames.length, 1);
    m.controls.destroy();
    assert.deepEqual(w.cancelled, [1], 'the pending frame is cancelled');
    assert.equal(w.count('resize'), 0, 'no resize listener outlives the controls (the row\'s nor a panel\'s)');
    assert.ok(w.observers.every((observer) => observer.disconnected));
    w.frames[0]();
    assert.equal(m.row.dataset.ownerCompact, undefined, 'a late frame writes nothing');
});

test('Main and two projects read and save only their room, including delayed context saves', async () => {
    const main = mountPair(), alpha = mountPair({ projectId: 'alpha' }), beta = mountPair({ projectId: 'beta' });
    const data = { context_mode: 'max', effort_range: { min: 'low', recommended: 'ultra', max: 'high' }, projects: [
        { id: 'alpha', cognition: { context_mode: 'low', effort_range: { min: 'high', recommended: 'none', max: 'max' } } },
        { id: 'beta', cognition: { context_mode: 'nano', effort_range: { min: 'none', recommended: 'max', max: 'low' } } },
    ] };
    for (const m of [main, alpha, beta]) m.controls.syncState(data);
    assert.deepEqual([main, alpha, beta].map(m => m.contextEl.dataset.contextMode), ['max', 'low', 'nano']);
    assert.deepEqual([main, alpha, beta].map(m => m.controls.effort.stored().recommended), ['ultra', 'none', 'max']);
    alpha.controls.syncState({ context_mode: 'max', effort_range: data.effort_range });
    assert.equal(alpha.contextEl.dataset.contextMode, 'low', 'missing project never falls back to Main');
    let release;
    alpha.answer.value = new Promise(resolve => { release = resolve; });
    const saving = alpha.choose('nano');
    assert.equal(alpha.controls.hasPendingSave(), true);
    release({ ok: true }); await saving;
    assert.equal(await alpha.controls.pendingSave(), true);
    assert.deepEqual(alpha.posts[0], ['/api/owner/context-mode', { mode: 'nano', project_id: 'alpha' }]);
    assert.equal(main.contextEl.dataset.contextMode, 'max');
    assert.equal(beta.contextEl.dataset.contextMode, 'nano');
    alpha.answer.value = { ok: true, json: async () => ({ project_id: 'beta', context_mode: 'max' }) };
    const refused = alpha.choose('max');
    const send = alpha.controls.pendingSave();
    await refused;
    assert.equal(alpha.contextEl.dataset.contextMode, 'nano', 'foreign-scope response cannot repaint this room');
    assert.equal(await send, false, 'the save this Send awaited was refused');
    assert.equal(await alpha.controls.pendingSave(), true, 'a reported refusal does not block a later Send');
    for (const m of [main, alpha, beta]) m.controls.destroy();
});

for (const failed of ['context', 'effort']) for (const accepts of [true, false]) {
    test(`Send waits only for pending controls: old ${failed} refusal, new save ${accepts ? 'accepted' : 'refused'}`, async () => {
        const m = mountPair({ projectId: 'alpha' });
        const effort = () => {
            const handle = m.panels.effort.querySelector('.chat-effort-rec');
            for (const fn of handle.listeners.get('keydown') || []) fn({ key: 'ArrowRight', currentTarget: handle, preventDefault() {} });
        };
        try {
            if (failed === 'context') {
                m.answer.value = { ok: false, json: async () => ({ error: 'Earlier refusal' }) };
                await m.choose('low');
            } else {
                m.effortAnswer.value = Promise.reject(new Error('Earlier refusal'));
                effort(); await m.controls.effort.pendingSave();
            }
            assert.equal(m.toasts.length, 1);
            assert.equal(m.controls.hasPendingSave(), false);
            let resolve, reject;
            const pending = new Promise((yes, no) => { resolve = yes; reject = no; });
            if (failed === 'context') { m.effortAnswer.value = pending; effort(); }
            else { m.answer.value = pending; void m.choose('low'); }
            assert.equal(m.controls.hasPendingSave(), true);
            const send = m.controls.pendingSave();
            if (!accepts) reject(new Error('Current refusal'));
            else resolve(failed === 'context'
                ? { project_id: 'alpha', effort_range: { min: 'low', recommended: 'high', max: 'high' } }
                : { ok: true });
            assert.equal(await send, accepts, 'only this pending save decides whether Send proceeds');
            assert.equal(m.toasts.length, accepts ? 1 : 2, 'a refused current save remains visible');
            assert.equal(m.controls.hasPendingSave(), false);
        } finally { m.controls.destroy(); }
    });
}

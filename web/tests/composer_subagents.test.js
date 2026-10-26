// The Subagents capsule (docs/DESIGN.md "Composer owner controls"): the room's own Off · Auto · Swarm, the third
// capsule of the Context/Effort family. The capsule says the value; its panel says what the value means and how a
// change applies; Settings-off locks it on Off with the reason and a way to Settings; a choice saves the room's mode.
import assert from 'node:assert/strict';
import test from 'node:test';
import { SUBAGENT_LOCKS, SUBAGENT_MEANINGS, SUBAGENT_RULE, subagentsView } from '../modules/composer_subagents.js';
import { mountPair } from './composer_controls_fixture.js';

const value = (el) => el.querySelector('.chat-capsule-value').textContent;
const shown = (note, selector) => !note.querySelector(selector).hidden;
const meaning = (note) => note.querySelector('.capsule-panel-line[data-on="true"]');
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('the words: one meaning per value, the rule, and the two reasons Settings can lock it', () => {
    assert.deepEqual(subagentsView('swarm'), {
        mode: 'swarm', stored: 'swarm', locked: false, reason: '', word: 'Swarm', meaning: SUBAGENT_MEANINGS.swarm,
        said: `Subagents: Swarm. ${SUBAGENT_MEANINGS.swarm} ${SUBAGENT_RULE}`,
    });
    assert.equal(SUBAGENT_RULE, 'Applies to new steps in this chat; running helpers finish.');
    assert.equal(SUBAGENT_MEANINGS.off, 'Ouroboros works alone in this chat: no subagents, no Claude Code or Codex sessions. Reviews still run.');
    assert.equal(SUBAGENT_MEANINGS.auto, 'Ouroboros decides when helpers are worth it.');
    const locked = subagentsView('swarm', 'disabled');
    assert.deepEqual([locked.mode, locked.stored, locked.word, locked.reason], ['off', 'swarm', 'Off', 'Subagents are turned off in Settings › Agents']);
    assert.equal(subagentsView('auto', 'unsaved').reason, 'Subagents are not set up yet');
    assert.deepEqual(Object.keys(SUBAGENT_LOCKS), ['disabled', 'unsaved']);
    assert.equal(subagentsView('auto', 'available').locked, false);
    assert.equal(subagentsView('auto', undefined).locked, false, 'a server that does not say locks nothing');
    assert.equal(subagentsView('surprise').mode, 'auto', 'an unknown stored value reads as the default');
});

test('the capsule says the room\'s value, unlit until the room answers; it stands first in the row, its panel out of the document', () => {
    const m = mountPair({ subagents: true });
    const el = m.subagentsEl;
    assert.deepEqual(m.row.children, [el, m.contextEl, m.effortEl], 'Subagents · Context · Effort, and nothing else');
    assert.deepEqual([el.dataset.known, value(el), el.dataset.mode], ['false', 'Auto', 'auto'], 'unlit until the room answers');
    m.controls.syncState({ subagents_mode: 'swarm', subagents_availability: 'available' });
    assert.deepEqual([el.dataset.known, value(el), el.dataset.mode, el.dataset.locked], ['true', 'Swarm', 'swarm', 'false']);
    assert.equal(m.subHead.getAttribute('aria-label'), `Subagents: Swarm. ${SUBAGENT_MEANINGS.swarm} ${SUBAGENT_RULE}`);
    m.controls.syncState({ subagents_mode: 'off' });
    assert.deepEqual([value(el), el.dataset.mode], ['Off', 'off']);
    m.controls.syncState({ context_mode: 'low' });
    assert.equal(el.dataset.mode, 'off', 'an answer without the mode keeps the last one');
    assert.deepEqual(m.open(), [], 'closed, nothing is said: the panel is not in the document');
    m.controls.destroy();
});

test('open: Off · Auto · Swarm, the meaning and the rule; a choice saves the room\'s mode and reads the room back', async () => {
    const m = mountPair({ subagents: true, projectId: 'p7' });
    m.controls.syncState({ projects: [{ id: 'p7', cognition: { context_mode: 'max', subagents_mode: 'auto' } }],
        subagents_availability: 'available' });
    await m.press(m.subHead);
    const el = m.subagentsEl;
    assert.equal(el.dataset.open, 'true');
    assert.deepEqual(m.open(), [m.panels.subagents], 'its panel');
    assert.equal(m.panels.subagents.querySelector('.capsule-panel-title').textContent, 'Subagents', 'the ring and the name head it');
    assert.deepEqual([m.panels.subagents.dataset.mode, m.panels.subagents.dataset.locked], ['auto', 'false']);
    assert.equal(meaning(m.subNote).textContent, SUBAGENT_MEANINGS.auto);
    assert.equal(m.subNote.querySelector('.capsule-panel-rule').textContent, SUBAGENT_RULE);
    assert.equal(shown(m.subNote, '.chat-subagents-locked'), false);
    assert.deepEqual(['off', 'auto', 'swarm'].map((mode) => [m.subSeg(mode).getAttribute('aria-checked'), m.subSeg(mode).tabIndex]),
        [['false', -1], ['true', 0], ['false', -1]], 'one tab stop, the chosen value');
    await m.chooseSub('swarm');
    assert.equal(m.controls.hasPendingSave(), true, 'Send waits for it');
    assert.equal(await m.controls.pendingSave(), true);
    assert.deepEqual(m.posts.at(-1), ['/api/owner/subagents-mode', { mode: 'swarm', project_id: 'p7' }], 'this room only');
    assert.deepEqual([el.dataset.mode, value(el)], ['swarm', 'Swarm']);
    assert.equal(meaning(m.subNote).textContent, SUBAGENT_MEANINGS.swarm, 'the panel stays open and says the new value');
    assert.equal(m.subNote.querySelectorAll('.capsule-panel-line').length, 3, 'every line stays in its cell');
    assert.deepEqual(m.refreshes, [true], 'the room is read again after the save');
    await m.chooseSub('swarm');
    assert.equal(m.posts.length, 1, 'the chosen value again saves nothing');
    await m.press(m.subHead);
    assert.deepEqual([el.dataset.open, m.open()], ['false', []]);
});

test('a refused save keeps the saved value and says why', async () => {
    const m = mountPair({ subagents: true });
    m.controls.syncState({ subagents_mode: 'auto', subagents_availability: 'available' });
    m.subagentAnswer.value = Promise.reject(new Error('Choose off, auto or swarm.'));
    await m.press(m.subHead);
    await m.chooseSub('off');
    assert.equal(await m.controls.pendingSave(), false);
    assert.deepEqual(m.toasts.at(-1), ['Choose off, auto or swarm.', 'error']);
    assert.deepEqual([m.subagentsEl.dataset.mode, value(m.subagentsEl)], ['auto', 'Auto'], 'never shown as saved');
});

test('Settings-off: Off with a lock, only Off can be chosen, the reason and Open Settings; the room keeps its value', async () => {
    const m = mountPair({ subagents: true });
    const el = m.subagentsEl;
    m.controls.syncState({ subagents_mode: 'swarm', subagents_availability: 'disabled' });
    assert.deepEqual([el.dataset.locked, el.dataset.mode, value(el)], ['true', 'off', 'Off']);
    assert.equal(m.subHead.getAttribute('aria-label'), 'Subagents: Off. Subagents are turned off in Settings › Agents.');
    await m.press(m.subHead);
    assert.deepEqual(['auto', 'swarm'].map((mode) => [m.subSeg(mode).dataset.disabled, m.subSeg(mode).getAttribute('aria-disabled')]),
        [['true', 'true'], ['true', 'true']]);
    assert.equal(m.subSeg('off').getAttribute('aria-checked'), 'true');
    await m.chooseSub('swarm');
    assert.equal(m.posts.length, 0, 'a value Settings rules out saves nothing');
    assert.equal(m.panels.subagents.dataset.locked, 'true', 'the panel\'s head shows the lock by the name');
    assert.ok(m.panels.subagents.querySelector('.capsule-panel-lock'));
    assert.deepEqual([shown(m.subNote, '.capsule-panel-lines'), shown(m.subNote, '.capsule-panel-rule'),
        shown(m.subNote, '.chat-subagents-locked')], [false, false, true]);
    assert.equal(m.subNote.querySelector('.chat-subagents-reason').textContent, 'Subagents are turned off in Settings › Agents');
    m.subNote.querySelector('.chat-subagents-settings').click();
    assert.deepEqual(m.settingsOpened, ['agents']);
    assert.deepEqual([el.dataset.open, m.open()], ['false', []], 'the way to Settings closes the panel first');
    m.controls.syncState({ subagents_availability: 'unsaved' });
    assert.equal(m.subNote.querySelector('.chat-subagents-reason').textContent, 'Subagents are not set up yet');
    m.controls.syncState({ subagents_availability: 'available' });
    assert.deepEqual([el.dataset.locked, el.dataset.mode, value(el)], ['false', 'swarm', 'Swarm'], 'the room\'s own value applies again');
});

test('keyboard: arrows, Home and End choose among the values that can be chosen', async () => {
    const m = mountPair({ subagents: true });
    m.controls.syncState({ subagents_mode: 'off', subagents_availability: 'available' });
    await m.press(m.subHead, 0);
    assert.equal(m.subKey('off', 'ArrowRight').defaultPrevented, true);
    await flush();
    assert.deepEqual(m.posts.at(-1), ['/api/owner/subagents-mode', { mode: 'auto' }]);
    await m.controls.pendingSave();
    m.subKey('auto', 'End');
    await m.controls.pendingSave();
    assert.deepEqual(m.posts.at(-1), ['/api/owner/subagents-mode', { mode: 'swarm' }]);
    assert.equal(m.subKey('swarm', 'Enter').defaultPrevented, false, 'other keys are the browser\'s');
    m.controls.syncState({ subagents_availability: 'disabled' });
    const before = m.posts.length;
    assert.equal(m.subKey('off', 'ArrowRight').defaultPrevented, true);
    assert.equal(m.posts.length, before, 'locked: nothing past Off');
});

test('one panel is open at a time across all three capsules', async () => {
    const m = mountPair({ subagents: true });
    m.controls.syncState({ subagents_mode: 'auto', subagents_availability: 'available' });
    await m.press(m.subHead);
    assert.equal(m.subagentsEl.dataset.open, 'true');
    m.docDown(m.contextHead);
    await m.press(m.contextHead);
    assert.deepEqual([m.subagentsEl.dataset.open, m.contextEl.dataset.open, m.open()], ['false', 'true', [m.panels.context]]);
    m.docDown(m.effortHead);
    await m.press(m.effortHead);
    assert.deepEqual([m.contextEl.dataset.open, m.effortEl.dataset.open, m.open()], ['false', 'true', [m.panels.effort]]);
    m.docDown(m.subHead);
    await m.press(m.subHead);
    assert.deepEqual([m.effortEl.dataset.open, m.subagentsEl.dataset.open, m.open()], ['false', 'true', [m.panels.subagents]]);
    m.controls.destroy();
    assert.deepEqual(m.open(), [], 'the open panel goes with the composer');
});

test('painting the same state again writes no words: the row\'s text observer refits on every write', async () => {
    const m = mountPair({ subagents: true });
    m.controls.syncState({ subagents_mode: 'auto', subagents_availability: 'available' });
    await m.press(m.subHead);
    const writes = [];
    const spy = (node) => {
        const { get, set } = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), 'textContent');
        Object.defineProperty(node, 'textContent', { configurable: true, get() { return get.call(this); },
            set(text) { writes.push(text); set.call(this, text); } });
    };
    [...m.subNote.querySelectorAll('.capsule-panel-line'), m.subNote.querySelector('.chat-subagents-reason')].forEach(spy);
    spy(m.subagentsEl.querySelector('.chat-capsule-value'));
    m.controls.syncState({ subagents_mode: 'auto', subagents_availability: 'available' });
    m.controls.syncState({ subagents_availability: 'available' });
    assert.deepEqual(writes, [], 'an unchanged state repaints nothing a text observer sees');
    m.controls.syncState({ subagents_mode: 'swarm' });
    assert.deepEqual(writes, ['Swarm'], 'a change writes the changed word once; the lines only change which one shows');
});

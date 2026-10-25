import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { createChatInstance } from '../modules/chat.js';
import { installDom, restoreDom, walkCard } from './chat_dom_fixture.js';
const fixture = JSON.parse(readFileSync(new URL('./fixtures/consciousness_contract.json', import.meta.url), 'utf8'));

async function replay(history, check) {
    const { prior, mount } = installDom(async (url) => ({ ok: true, json: async () =>
        String(url).startsWith('/api/chat/history') ? structuredClone(history) : {} }));
    const handlers = new Map();
    const ws = { on(type, fn) { handlers.set(type, fn); return () => handlers.delete(type); }, isConnected: () => true, send() {} };
    let instance, generation = 0;
    const stateSnapshots = { begin: () => ({ generation: ++generation, requestedAt: Date.now() }),
        gate() { return Promise.resolve(this.begin()); }, isCurrent: () => true, apply() {} };
    try {
        instance = createChatInstance({ ws, state: { activePage: 'chat', projectChatIds: new Set(), unreadCount: 0 },
            updateUnreadBadge() {}, stateSnapshots, chatId: 1, idPrefix: 'chat', mountEl: mount, asPanel: true });
        await instance.refreshHistory({ revision: 1 });
        await new Promise(resolve => setTimeout(resolve, 0));
        await check(document.byId.get('chat-messages'), handlers, instance);
    } finally { instance?.destroy(); restoreDom(prior); }
}
const card = (feed, id) => walkCard(feed, id);
const text = (node) => [node.textContent, node.innerHTML, ...(node.children || []).map(text)].join(' ');

test('terminal-only producer history keeps quiet truth without a progress row', async () => {
    for (const [index, history] of fixture.terminal_only_history.entries()) {
        assert.ok(history.messages.every(row => !row.is_progress));
        await replay(history, feed => {
            const root = card(feed, '214a31b6');
            assert.ok(root);
            assert.equal(text(root).includes('Done · no message'), index === 0);
        });
    }
});

test('checkpoint-only history applies the admission title, with no fabricated title when absent', async () => {
    for (const named of [true, false]) {
        const history = structuredClone(fixture.running_history);
        assert.ok(history.messages.length > 0 && history.messages.every(row => row.is_progress));
        if (!named) for (const row of history.messages) delete row.suggested_name;
        await replay(history, feed => {
            const root = card(feed, 'w-running');
            assert.ok(root);
            assert.equal(text(root).includes('Own time'), named);
        });
    }
});

test('the authored summary is the first Details fact on replay, absent on older wakes', async () => {
    for (const messages of [fixture.history.messages, [...fixture.history.messages].reverse()]) await replay({ ...fixture.history, messages }, feed => {
        const named = card(feed, 'w-summary'), old = card(feed, '214a31b6');
        const summary = fixture.consciousness.recent_wakes.find(row => row.task_id === 'w-summary').summary;
        const findFacts = (node) => Object.hasOwn(node?.dataset || {}, 'liveFacts') ? node : (node?.children || []).map(findFacts).find(Boolean);
        const facts = findFacts(named);
        assert.ok(facts, 'the actual card Details facts');
        assert.match(facts.innerHTML, /^<div class="chat-live-fact"><dt>In its words<\/dt>/);
        assert.ok(facts.innerHTML.includes(`<span data-i18n-authored>${summary}</span>`));
        assert.doesNotMatch(text(old), /In its words/);
    });
});

test('live terminal frames add and clear the authored summary through card facts', async () => {
    await replay(fixture.running_history, (feed, handlers) => {
        const send = (summary) => handlers.get('log')({ chat_id: 1, data: { type: 'task_done',
            task_id: 'w-running', status: 'completed', wake_summary: summary, quiet_effective: true,
            ts: '2026-10-24T08:00:00+00:00' } });
        send('Kept my own words.');
        assert.match(card(feed, 'w-running').querySelector('[data-live-facts]').innerHTML, /In its words/);
        assert.match(card(feed, 'w-running').querySelector('[data-live-facts]').innerHTML, /data-i18n-authored>Kept my own words/);
        send(null);
        assert.doesNotMatch(card(feed, 'w-running').querySelector('[data-live-facts]').innerHTML, /In its words/);
    });
});

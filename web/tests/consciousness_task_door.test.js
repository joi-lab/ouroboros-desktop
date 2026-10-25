import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
const source = readFileSync(new URL('../app.js', import.meta.url), 'utf8');
const handler = source.slice(source.indexOf("window.addEventListener('ouro:open-task'"), source.indexOf('\nfunction freshProjectRow'));
test('the Dashboard door shows Main and reveals the exact task; unavailable destinations are said', async () => {
    const calls = [], notices = [];
    let listener, canShow = true, outcome = { state: 'found' };
    vm.runInNewContext(handler, { window: { addEventListener: (name, fn) => { assert.equal(name, 'ouro:open-task'); listener = fn; } },
        showPage: async name => { calls.push(name); return canShow; }, state: { activePage: 'chat' },
        revealQuotedSource: async (address, chatId, current) => { calls.push([address, chatId, current()]); return outcome; },
        showToast: text => notices.push(text), tr: (_, english) => english });
    await listener({ detail: { task_id: '214a31b6', chat_id: 1 } });
    assert.deepEqual(calls, ['chat', ['task:214a31b6', 1, true]]);
    assert.equal(notices.length, 0);
    calls.length = 0;
    await listener({ detail: { task_id: 'focus', chat_id: 1099511627777 } });
    assert.equal(calls.length, 0);
    outcome = { state: 'unavailable' };
    await listener({ detail: { task_id: 'missing', chat_id: 1 } });
    assert.deepEqual(notices, ['This task is unavailable.']);
    canShow = false; calls.length = 0;
    await listener({ detail: { task_id: 'held', chat_id: 1 } });
    assert.deepEqual(calls, ['chat']);
});

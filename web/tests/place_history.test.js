// Source proposal by readonly child 87500533; materialized and executed by parent.
import assert from 'node:assert/strict';
import test from 'node:test';
import { installCardDom, TextNode } from './helpers/card_dom.js';
import { applyPayload, createTranslator, setMissTransport } from '../modules/i18n.js';
import { normalizePlace, readPlaceRooms } from '../modules/consciousness_api.js';
import { mountPlaceHistory, capturedWindowNote, historySpeaker, historyFactWords, placeRoomsHtml, shownTime } from '../modules/place_history.js';
import { mountConnectorPlaces } from '../modules/connector_places.js';
const tick = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };
const unsafeText = '<img src="x" onerror="bad()"> & "hello"';
const rawPlace = (id = 'one') => ({ place_id: id, connector: 'telegram-user-session', label: unsafeText, account_id: `account-${id}`, state: 'active' });
const place = normalizePlace(rawPlace());
const room = { room_chat_id: 5_000_000_123, address: unsafeText, unanswered_count: null, label: null, last_at: '', conversation_id: '', thread_id: '' };
const coverage = { discovery: 'arrivals', importing: false, gaps: false };
const index = (rooms = [room]) => ({ place_id: place.id, rooms, coverage });
const row = (id, text = unsafeText) => ({ history_id: id, role: 'user', text, sender_label: unsafeText, place_history: {kind: 'message'} });
const page = (messages, next = null) => ({ messages, next_cursor: next, has_more: next !== null, window: { complete: next === null, truncated_by: next ? ['quota'] : [] } });
function fixture(t) {
    const dom = installCardDom(); const { doc, win } = dom;
    // Missing fixture DOM mechanics, not a replacement for production behavior.
    doc.defaultView = win; win.getComputedStyle = () => ({ visibility: 'visible' });
    const create = doc.createElement;
    doc.createElement = (tag) => {
        const node = create(tag);
        Object.defineProperties(node, { isConnected: { get: () => doc.body.contains(node) }, childElementCount: { get: () => node.children.length } });
        node.getClientRects = () => node.isConnected ? [{}] : [];
        node.scrollTop = 0; node.scrollHeight = 0;
        node.prepend = (...nodes) => { for (const child of [...nodes].reverse()) node.insertBefore(child, node.firstChild); };
        const matches = node.matches.bind(node);
        node.matches = (selector) => matches(selector.replace(/:disabled/g, '[disabled]'));
        return node;
    };
    doc.createDocumentFragment = () => doc.createElement('#fragment');
    doc.createTextNode = (text) => new TextNode(text, doc);
    doc.createTreeWalker = (root) => {
        const texts = []; const visit = (node) => { if (node.nodeType === 3) {
            Object.defineProperty(node, 'parentElement', { configurable: true, get: () => node.parentNode }); texts.push(node);
        } else (node.childNodes || []).forEach(visit); }; visit(root);
        let i = 0; return { nextNode: () => texts[i++] || null };
    };
    const requests = []; let answer = () => { throw new Error('Unexpected request: no network allowed'); };
    t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
        const request = { url: String(url), ...init }; requests.push(request);
        assert.equal(init.method || 'GET', 'GET', 'navigation must not write');
        const value = await answer(request);
        return { ok: true, status: 200, json: async () => value };
    });
    t.after(() => { for (const button of doc.body.querySelectorAll('[data-history-close], [data-place-close]')) button.click(); dom.restore(); });
    return { doc, requests, reply(fn) { answer = fn; }, q(selector) { return doc.body.querySelector(selector); } };
}
function mount(f, options = {}) {
    const host = f.doc.createElement('div'); f.doc.body.append(host);
    const reader = mountPlaceHistory(host, place, room, { onBack: () => reader.close(), ...options });
    return reader;
}

test('role-first names and facts never infer delivery or exact wake', () => {
    assert.deepEqual(['user', 'assistant', 'system'].map((role) => historySpeaker({role, sender_label: 'Alice'})), ['Alice', 'Ouroboros', 'System']);
    assert.deepEqual(historyFactWords({role: 'assistant'}), []);
    assert.deepEqual(historyFactWords({place_history: {delivery_state: 'authored'}}), ['Delivery not confirmed']);
    assert.deepEqual(historyFactWords({place_history: {attention: {decision:'focus_started'}}}), ['Focus started']);
});

test('page, pending import, range gaps and unknown coverage have distinct words', () => {
    assert.doesNotMatch(capturedWindowNote({has_more: false, window:{complete:false,truncated_by:['page']}}), /unavailable|incomplete/);
    assert.match(capturedWindowNote({has_more:true,window:{complete:false,truncated_by:['page']}}), /Earlier saved/);
    assert.match(capturedWindowNote({window:{complete:false,truncated_by:['chat_importing']}}), /still being imported/);
    assert.match(capturedWindowNote({window:{complete:false,truncated_by:['page']},coverage:{spans:{chat:{gaps:['malformed_jsonl']}}}}), /unavailable/);
    assert.match(capturedWindowNote({}), /not fully reported/);
    assert.match(placeRoomsHtml({rooms:[],coverage}), /No saved conversation found in this index/);
    assert.match(placeRoomsHtml({rooms:[],coverage:{...coverage,importing:true}}), /still being imported/);
});

test('reader forwards large IDs, retries exact cursor and excludes progress/evidence', async (t) => {
    const f=fixture(t); const cursor='opaque /+?&cursor'; let attempt=0;
    f.reply(({url}) => {
        const query=new URL(url,'http://local').searchParams;
        assert.equal(query.get('chat_id'),String(room.room_chat_id));
        assert.equal(query.get('cursor'),attempt===0 ? null : cursor); attempt+=1;
        if (attempt===1) return page([row('new'),{...row('thought','hidden thought'),is_progress:true}, {history_id:'evidence',text:'hidden evidence',role:'system'}],cursor);
        if (attempt===2) throw new Error(unsafeText);
        if (attempt===3) return page([],cursor);
        return page([row('old','older'),row('new')]);
    });
    const reader=mount(f); await tick();
    assert.equal(f.q('img'),null);
    assert.equal(f.q('[data-history-messages]').childElementCount,1);
    f.q('[data-history-older]').click(); await tick();
    assert.equal(f.q('[data-history-status]').textContent,unsafeText);
    f.q('[data-history-retry]').click(); await tick();
    assert.match(f.q('[data-history-status]').textContent,/no page was skipped/);
    f.q('[data-history-retry]').click(); await tick();
    assert.equal(f.q('[data-history-messages]').childElementCount,2);
    assert.equal(f.q('[data-history-messages]').firstElementChild.querySelector('.chat-markdown').textContent,'older');
    assert.equal(f.q('[data-history-older]').hidden,true); assert.equal(attempt,4); reader.close();
});

test('partial ranges retry their own page token and keep gaps on other loaded ranges', async (t) => {
    const f=fixture(t); const requests=[]; let repaired=false;
    const reader=mount(f,{readHistory:async ({cursor})=>{
        requests.push(cursor);
        if (cursor==='same-page') { repaired=true; return {...page([row('new')],'older'),page_cursor:'same-page'}; }
        if (cursor==='older') return {...page([row('old')]),window:{complete:false,truncated_by:['page']}};
        return {...page([row('new')],'older'),page_cursor:'same-page',window:{complete:false,truncated_by:['chat_malformed_jsonl']}};
    }}); await tick();
    f.q('[data-history-older]').click(); await tick();
    assert.match(f.q('[data-history-coverage]').textContent,/unavailable/);
    f.q('[data-history-range]').click(); await tick();
    assert.equal(repaired,true); assert.deepEqual(requests,[null,'older','same-page']);
    assert.doesNotMatch(f.q('[data-history-coverage]').textContent,/unavailable/); reader.close();
});

test('room API validates identity, safe integers, unknown count and coverage', async (t) => {
    const f=fixture(t); const controller=new AbortController(); f.reply(()=>index());
    assert.deepEqual(await readPlaceRooms(place.id,{signal:controller.signal}),{rooms:[room],coverage});
    assert.equal(f.requests[0].signal,controller.signal); assert.equal(f.requests[0].cache,'no-store');
    for(const invalid of [{...index(),place_id:'different'},{...index(),rooms:null},{...index(),coverage:null},index([{...room,room_chat_id:Number.MAX_SAFE_INTEGER+1}]),index([{...room,room_chat_id:0}]),index([{...room,room_chat_id:String(room.room_chat_id)}]),index([{...room,unanswered_count:-1}])]) {
        f.reply(()=>invalid); await assert.rejects(readPlaceRooms(place.id),/could not be read/);
    }
});

test('Back disposal aborts pending read and ignores its late answer', async (t) => {
    const f=fixture(t); const pending=deferred(); let signal;
    const reader=mount(f,{readHistory: (request)=>{signal=request.signal;return pending.promise}});
    f.q('[data-history-back]').click(); reader.close(); assert.equal(signal.aborted,true);
    pending.resolve(page([row('late')])); await tick(); assert.equal(f.q('.place-convo'),null);
});

test('connector section lists zero/one/many without selecting or writing, and aborts on disposal', async (t) => {
    const f=fixture(t);
    for(const count of [0,1,2]) {
        const host=f.doc.createElement('section'); f.doc.body.append(host);
        const supplied=Array.from({length:count},(_,i)=>normalizePlace(rawPlace(String(i))));
        let signal;
        const dispose=mountConnectorPlaces(host,'telegram-user-session',{read:async (request)=>{signal=request.signal;return {places:[...supplied,normalizePlace({...rawPlace('foreign'),connector:'elsewhere'})]}}});
        await tick(); assert.equal(host.querySelectorAll('[data-place-open]').length,count);
        assert.equal(Boolean(host.querySelector('[data-place-connect]')),count===0);
        assert.equal(f.q('[data-place-state]'),null); dispose(); assert.equal(signal.aborted,true); host.remove();
    }
});

test('an empty connector section uses the shared Connect control, filtered to its own connector', async (t) => {
    const f=fixture(t); const host=f.doc.createElement('section'); f.doc.body.append(host); let left=0;
    const dispose=mountConnectorPlaces(host,'telegram-user-session',{leave:()=>{left+=1;},read:async ()=>({places:[]})});
    await tick();
    f.reply(()=>({places:[],connectors:[{connector:'telegram-user-session'},{connector:'elsewhere'}]}));
    host.querySelector('[data-place-connect]').click(); await tick();
    assert.equal(left,1,'the connector sheet leaves before Connect opens');
    assert.deepEqual([...f.doc.body.querySelectorAll('[data-connect-act]')].map((button)=>button.dataset.connector),['telegram-user-session']);
    assert.deepEqual(f.requests.map((request)=>request.url),['/api/places']);
    dispose();
});

test('translation preserves authored messages while chrome translates', async (t) => {
    const f=fixture(t); f.reply(()=>page([row('authored','Settings')])); const reader=mount(f); await tick();
    const chrome=f.doc.createElement('button'); chrome.textContent='Settings'; f.doc.body.append(chrome);
    setMissTransport(()=>Promise.resolve({ok:true}));
    applyPayload({language:'fr',english:false,revision:1,entries:{Settings:{text:'Translated chrome'}}});
    t.after(()=>{applyPayload({language:'',english:true,entries:{}});setMissTransport(null)});
    createTranslator({skipRoots:'[data-i18n-authored]',userContent:'',excludeSelector:''}).applyTo(f.doc.body);
    assert.equal(f.q('.chat-markdown').textContent,'Settings'); assert.equal(chrome.textContent,'Translated chrome'); reader.close();
});

const viewChanged = () => Object.assign(new Error('history_view_changed'), { status: 409, body: { reason_code: 'history_view_changed' } });
const shownTexts = (f) => [...f.q('[data-history-messages]').children].map((node) => node.querySelector('.chat-markdown').textContent);

test('a changed view rereads the newest page and replaces the shown ranges only once it arrives', async (t) => {
    const f = fixture(t); const requests = []; const fresh = deferred(); let busyOnce = true;
    const reader = mount(f, { readHistory: async ({ cursor }) => {
        requests.push(cursor);
        if (requests.length === 1) return page([row('live', 'newest live')], 'stored-order');
        if (cursor === 'stored-order') throw viewChanged();  // the room's first import changed its view
        if (busyOnce) { busyOnce = false; throw new Error('Saved history is busy.'); }
        return fresh.promise;
    } });
    await tick();
    f.q('[data-history-older]').click(); await tick(); await tick();
    assert.deepEqual(requests, [null, 'stored-order', null], 'the refused cursor is never resent');
    assert.equal(f.q('[data-history-status]').textContent, 'Saved history is busy.');
    assert.deepEqual(shownTexts(f), ['newest live'], 'a failed fresh read keeps the shown ranges');
    f.q('[data-history-retry]').focus(); f.q('[data-history-retry]').click(); await tick();
    assert.deepEqual(requests, [null, 'stored-order', null, null], 'Try again rereads the newest page');
    assert.deepEqual(shownTexts(f), ['newest live'], 'the shown ranges stay while the fresh read runs');
    fresh.resolve(page([row('imported', 'earlier imported'), row('live', 'newest live')], 'time-order')); await tick();
    assert.deepEqual(shownTexts(f), ['earlier imported', 'newest live']);
    assert.equal(f.q('[data-history-status]').textContent, 'Saved history changed, so it was read again from the newest messages.');
    assert.equal(f.q('[data-history-retry]').hidden, true);
    assert.equal(f.doc.activeElement, f.q('[data-history-older]'), 'focus stays in the reader');
    f.q('[data-history-older]').click(); await tick();
    assert.equal(requests.at(-1), 'time-order'); reader.close();
});

test('a changed view met by Retry this range replaces the chain too; closing ignores the late fresh page', async (t) => {
    const f = fixture(t); const requests = []; const fresh = deferred(); let signal;
    const reader = mount(f, { readHistory: async ({ cursor, signal: aborted }) => {
        requests.push(cursor);
        if (requests.length === 1) return { ...page([row('live', 'newest live')], 'older'), page_cursor: 'same-page', window: { complete: false, truncated_by: ['chat_malformed_jsonl'] } };
        if (cursor === 'same-page') throw viewChanged();
        signal = aborted; return fresh.promise;
    } });
    await tick();
    f.q('[data-history-range]').click(); await tick(); await tick();
    assert.deepEqual(requests, [null, 'same-page', null]);
    reader.close(); assert.equal(signal.aborted, true);
    fresh.resolve(page([row('late', 'late answer')])); await tick();
    assert.equal(f.q('.place-convo'), null);
});

test('a time outside the current year names its year, in rows and in the room list', async (t) => {
    const year = new Date().getFullYear();
    const f = fixture(t);
    f.reply(() => page([{ ...row('then', 'two years ago'), ts: `${year - 2}-06-15T12:00:00Z` }, { ...row('now', 'this year'), ts: `${year}-06-15T12:00:00Z` }]));
    const reader = mount(f); await tick();
    const times = [...f.doc.body.querySelectorAll('time')].map((node) => node.textContent);
    assert.match(times[0], new RegExp(String(year - 2)));
    assert.doesNotMatch(times[1], new RegExp(String(year)));
    assert.match(placeRoomsHtml({ rooms: [{ ...room, last_at: `${year - 1}-03-01T08:00:00Z` }], coverage }), new RegExp(String(year - 1)));
    reader.close();
});

test('the start is the record saved when it was read; Refresh history reads the newest page again', async (t) => {
    const f = fixture(t); const requests = [];
    const reader = mount(f, { readHistory: async ({ cursor }) => {
        requests.push(cursor);
        if (requests.length === 1) return page([row('b', 'newest')], 'older');
        if (cursor === 'older') return page([row('a', 'oldest as read')]);
        return page([row('z', 'imported since'), row('b', 'newest')], 'after-import');
    } });
    await tick();
    assert.equal(f.q('[data-history-refresh]').hidden, false);
    f.q('[data-history-older]').click(); await tick();
    assert.match(f.q('[data-history-coverage]').textContent.trim(), /^Start of the record saved when this was read\. Refresh history to include anything saved since\./);
    f.q('[data-history-refresh]').click();
    assert.equal(f.q('[data-history-refresh]').disabled, true, 'one read at a time');
    await tick();
    assert.deepEqual(requests, [null, 'older', null]);
    assert.deepEqual(shownTexts(f), ['imported since', 'newest']);
    assert.match(f.q('[data-history-coverage]').textContent, /Earlier saved records are available\./);
    assert.equal(f.q('[data-history-refresh]').disabled, false); reader.close();
});

test('a row shows the send time its arrival recorded; the title keeps when it was actually saved', async (t) => {
    const imported = { ...row('i', 'imported'), ts: '2024-03-01T10:00:00+00:00', place_history: { kind: 'message',
        sent_at: '2024-03-01T10:00:00+00:00', historical_import: { authorship: 'participant', received_at: '2026-10-26T09:00:00+00:00' } } };
    const late = { ...row('l', 'late live'), ts: '2026-10-26T09:05:00+00:00', place_history: { kind: 'message', sent_at: '2026-10-26T08:00:00+00:00' } };
    const receipt = { ...row('r', 'receipt'), ts: '2026-10-26T09:06:00+00:00', place_history: { kind: 'delivery' } };
    assert.deepEqual(shownTime(imported), { at: imported.ts, title: 'Sent 2024-03-01T10:00:00+00:00 · saved 2026-10-26T09:00:00+00:00' });
    assert.deepEqual(shownTime(late), { at: '2026-10-26T08:00:00+00:00', title: 'Sent 2026-10-26T08:00:00+00:00 · saved 2026-10-26T09:05:00+00:00' });
    assert.deepEqual(shownTime(receipt), { at: receipt.ts, title: receipt.ts }, 'no send time is invented');
    const f = fixture(t); f.reply(() => page([imported, late, receipt])); const reader = mount(f); await tick();
    const times = [...f.doc.body.querySelectorAll('time')];
    assert.deepEqual(times.map((node) => node.getAttribute('datetime')), [imported.ts, '2026-10-26T08:00:00+00:00', receipt.ts]);
    assert.match(times[0].textContent, /2024/); reader.close();
});

// Source proposal by readonly child 87500533; materialized and executed by parent.
import assert from 'node:assert/strict';
import test from 'node:test';
import { installCardDom, TextNode } from './helpers/card_dom.js';
import { applyPayload, createTranslator, setMissTransport } from '../modules/i18n.js';
import { normalizePlace, readPlaceRooms } from '../modules/consciousness_api.js';
import { mountPlaceHistory, capturedWindowNote, historySpeaker, historyFactWords, placeRoomsHtml } from '../modules/place_history.js';
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

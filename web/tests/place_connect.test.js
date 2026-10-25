import assert from 'node:assert/strict';
import test from 'node:test';
import { connectChoices, connectListHtml, openConnectPlace } from '../modules/place_connect.js';
import { connectPlace, normalizePlace, normalizeConnector, readPlaceConnector } from '../modules/consciousness_api.js';
import { setup } from './dashboard_dom_fixture.js';

const raw = { connector: 'github', label: 'GitHub', icon: { kind: 'svg', svg: '<svg viewBox="0 0 24 24"><circle r="7"/></svg>' },
    source: { url: '/api/extensions/github/status', refresh_s: 60 }, settings: 'connection',
    readiness: { text: 'Connector on', tone: 'ok' }, wakes: ['mention'], facts: ['mention', { id: 'signal.words', label: 'Words you chose' }] };
const github = normalizeConnector(raw);
const rows = [{ account_id: 'writer', conversation_id: 'repo.123', label: 'example/project' }];
const place = normalizePlace({ ...raw, place_id: 'gh-1', label: 'GitHub · example/project', account_id: 'writer', conversation_id: 'repo.123', state: 'silent', revision: 7 });
const strip = (html) => html.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
const choicesHtml = (data, extra = {}) => connectListHtml({ connectors: [github], selected: github, choices: connectChoices(data), ...extra });

test('the reader keeps the single projection: service label, revision, sanitized icon, readiness and declarations', () => {
    assert.equal(github.label, 'GitHub');
    assert.equal(github.connectorLabel, undefined);
    assert.equal(place.revision, 7);
    assert.equal(place.icon.kind, 'svg');
    assert.deepEqual(place.readiness, raw.readiness);
    assert.deepEqual(place.facts, raw.facts);
    assert.deepEqual(place.source, raw.source);
    assert.equal(place.settings, 'connection');
    const missing = normalizePlace({ connector: 'raw-skill-id', connector_label: 'old title', icon: {} });
    assert.equal(missing.label, '');
    assert.equal(missing.revision, null);
    assert.equal(missing.readiness, null);
    assert.equal(missing.facts, null, 'absence differs from a declared empty vocabulary');
    assert.equal(missing.spent24hUsd, null);
    assert.equal(normalizePlace({ spent_24h_usd: 0 }).spent24hUsd, 0);
});

test('Connect starts Silent and still offers connected connectors with their exact place count', () => {
    const html = connectListHtml({ connectors: [github], places: [place, { ...place, id: 'gh-2' }] });
    assert.match(strip(html), /It starts Silent: it hears and thinks there and sends nothing until you make it Active\./);
    assert.match(strip(html), /Connected · 2 places · Connect another/);
    assert.match(html, /data-i18n-authored>GitHub</);
    assert.doesNotMatch(connectListHtml({ connectors: [github] }), /Connect another/);
    assert.match(connectListHtml({ connectors: [] }), /No installed connector/);
    assert.match(connectListHtml({}), /Reading the installed connectors/);
    assert.match(connectListHtml({ error: 'offline' }), /data-connect-act="retry"/);
});

test('the four instance-list states remain distinct and every state keeps Whole account', () => {
    const absent = choicesHtml({ text: 'Ready' });
    assert.match(absent, /Whole account/);
    assert.doesNotMatch(absent, /None offered|Could not read|retry-choices/);
    const known = choicesHtml({ connect: rows });
    assert.match(known, /data-i18n-authored>example\/project</);
    assert.match(known, /data-choice="0"/);
    assert.match(known, /Whole account/);
    const empty = choicesHtml({ connect: [] });
    assert.match(empty, /None offered yet/);
    assert.match(empty, /data-connect-act="settings"/);
    assert.doesNotMatch(empty, /retry-choices/);
    for (const data of [null, [], { connect: null }, { connect: {} }, { connect: 'bad' }]) {
        const html = choicesHtml(data);
        assert.match(html, /Could not read the connector’s list/);
        assert.match(html, /retry-choices/);
        assert.match(html, /Whole account/);
        assert.doesNotMatch(html, /None offered yet/);
    }
});

test('malformed rows are dropped without inventing an account-wide wildcard', () => {
    const bad = [null, { ...rows[0], account_id: null }, { ...rows[0], account_id: '' }, { ...rows[0], label: 5 }, { ...rows[0], conversation_id: null }];
    assert.deepEqual(connectChoices({ connect: [...bad, ...rows] }), { state: 'known', rows: [{ accountId: 'writer', conversationId: 'repo.123', label: 'example/project' }] });
    assert.equal(connectChoices({ connect: bad }).state, 'unavailable');
    const connected = choicesHtml({ connect: rows }, { places: [place] });
    assert.match(strip(connected), /example\/project Connected · Open/);
    assert.match(connected, /data-choice="whole"/, 'the existing instance does not consume Whole account');
    const broad = choicesHtml({ connect: rows }, { places: [{ ...place, accountId: '', conversationId: '' }] });
    assert.match(broad, /data-choice="0"/, 'a broad place does not consume an instance');
});

test('an unloaded connector explains the missing chooser and still connects Whole account', () => {
    const html = connectListHtml({ connectors: [github], selected: { ...github, source: null, readiness: { text: 'Off in Skills', tone: 'warn' } } });
    assert.match(html, /Turn the connector on to choose where/);
    assert.match(html, /Off in Skills/);
    assert.match(html, /data-choice="whole"/);
    assert.doesNotMatch(html, /repository|channel|Reading where/);
});

test('the declared route uses the extension helper and no-store; unrelated routes never get fetched', async (t) => {
    const { calls, routes } = setup(t);
    routes.set('/api/extensions/github/status', (_url, init) => {
        assert.equal(init.cache, 'no-store');
        return { ok: true, json: async () => ({ text: 'Ready', connect: rows }) };
    });
    assert.deepEqual((await readPlaceConnector(github)).connect, rows);
    for (const url of ['https://other/status', '/api/extensions/another/status', '/api/extensions/github/../status']) {
        await assert.rejects(readPlaceConnector({ ...github, source: { url } }), /route could not be read/);
    }
    assert.equal(calls.length, 1);
});

test('connecting an instance carries only its identity and connector-authored label', async (t) => {
    const { routes } = setup(t);
    const calls = [];
    routes.set('/api/places', (_url, init) => {
        calls.push(JSON.parse(init.body));
        return { ok: true, json: async () => ({ ok: true, place: { ...raw, place_id: 'gh-1', state: 'silent' } }) };
    });
    assert.equal((await connectPlace({ connector: 'github', ...connectChoices({ connect: rows }).rows[0] })).state, 'silent');
    await connectPlace({ connector: 'github' });
    assert.deepEqual(calls, [
        { connector: 'github', account_id: 'writer', conversation_id: 'repo.123', label: 'example/project' },
        { connector: 'github' },
    ]);
});

test('the live Connect flow fetches choices lazily, retries a failure, and opens the returned instance', async (t) => {
    const { document } = setup(t);
    let reads = 0;
    const writes = [], opened = [];
    const sheet = openConnectPlace({ read: async () => ({ connectors: [github], places: [] }),
        readStatus: async () => { if (++reads === 1) throw new Error('offline'); return { text: 'Ready for you', connect: rows }; },
        connect: async (request) => { writes.push(request); return place; }, openPlace: (value) => opened.push(value) });
    t.after(() => sheet.close());
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(reads, 0);
    const click = async (selector) => sheet.body.fire('click', { target: sheet.body.querySelector(selector) });
    await click('[data-connect-act="choose"]');
    assert.equal(reads, 1);
    assert.match(sheet.body.textContent, /Could not read the connector’s list/);
    assert.ok(sheet.body.querySelector('[data-choice="whole"]'));
    await click('[data-connect-act="retry-choices"]');
    assert.equal(reads, 2);
    assert.equal(sheet.body.querySelector('[data-i18n-authored][role="status"]').textContent, 'Ready for you');
    await click('[data-choice="0"]');
    assert.deepEqual(writes, [{ connector: 'github', accountId: 'writer', conversationId: 'repo.123', label: 'example/project' }]);
    assert.deepEqual(opened, [place]);
    assert.equal(document.body.querySelector('dialog'), null);
});

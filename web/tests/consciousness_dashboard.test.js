import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import { normalizeConsciousness, normalizePlacesResponse, normalizeWake } from '../modules/consciousness_api.js';
import { headlineView, liveWake, factsLineParts, countedRootsText, summaryHtml, dashboardPlaceRowHtml,
    wakesCardHtml, wakeRowHtml, initConsciousnessDashboard } from '../modules/consciousness_dashboard.js';
import { wakeCostText } from '../modules/consciousness_words.js';
const FIXTURE = JSON.parse(fs.readFileSync(new URL('./fixtures/consciousness_contract.json', import.meta.url), 'utf8'));
const full = () => normalizeConsciousness(FIXTURE.consciousness);
const fromState = () => normalizeConsciousness(FIXTURE.state.bg_consciousness_state, { enabled: true });
const wake = (id) => full().recentWakes.find((row) => row.taskId === id);
const strip = (html) => html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();

test('server status alone owns the headline even with finished or active rows in the list', () => {
    assert.equal(headlineView(full()).text, FIXTURE.consciousness.detail);
    assert.match(headlineView(full()).text, /^Sleeping until/);
    assert.equal(headlineView(full()).moving, false);
    assert.ok(liveWake(full()).live);
    assert.equal(liveWake({ recentWakes: [wake('214a31b6')] }), null);
    for (const [status, detail] of [['due', 'Due; waiting for current work.'],
        ['wake_failed', 'The last wake-up failed.'], ['wake_rejected', 'The last wake-up was refused.'],
        ['wake_paused', 'The last wake-up returned while paused.'], ['disabled', 'Background consciousness is off.'],
        ['waiting_for_first_conversation', 'No owner chat is bound yet; the first conversation binds it.']]) {
        assert.deepEqual(headlineView({ ...full(), status, detail }), { text: detail, moving: false });
    }
    assert.equal(headlineView({ ...full(), status: 'thinking' }).moving, true);
});

test('facts keep money with its cause and say when some calls have no price', () => {
    assert.deepEqual(factsLineParts(fromState(), 3).map(strip),
        ['on its own clock', '3 places', 'own $0.62', 'Slack · Acme $0.88', 'started work $0.48', 'Some calls have no price']);
    assert.deepEqual(factsLineParts({ ...fromState(), enabled: false, spendByCause: [] }, 1).map(strip), ['off', '1 place']);
    assert.equal(countedRootsText(fromState()), 'Its own tasks: 1 of 2 · Release notes draft (Slack · Acme)');
    assert.match(summaryHtml(fromState()), /data-cx-act="toggle" data-enabled="1">Stop</);
    assert.doesNotMatch(summaryHtml(normalizeConsciousness({})), /data-cx-act="toggle"/);
});

test('producer rows retain terminal truth and all five active phases', () => {
    assert.match(strip(wakeRowHtml(wake('214a31b6'))), /Done · no message/);
    assert.doesNotMatch(wakeRowHtml(wake('214a31b6')), /Working|is-live|data-shape="moving"/);
    assert.doesNotMatch(wakeRowHtml(wake('w-spoke')), /no message/);
    for (const [id, word] of [['running', 'Working'], ['waiting', 'Waiting'], ['queued', 'Queued'], ['paused', 'Paused'], ['unknown', 'State unknown']]) {
        const html = wakeRowHtml(wake('w-' + id));
        assert.ok(strip(html).includes(word));
        assert.equal(html.includes('data-shape="moving"'), id === 'running');
        if (id !== 'running') assert.doesNotMatch(html, /Working/);
    }
    assert.doesNotMatch(wakeRowHtml({ ...wake('214a31b6'), endedAt: '' }), /Working/);
});

test('summary is authored, note is whole and disclosed, only flagged checks get chips', () => {
    const old = wake('214a31b6'), named = wake('w-summary');
    const oldHtml = wakeRowHtml(old), namedHtml = wakeRowHtml(named);
    assert.doesNotMatch(oldHtml, /class="consciousness-wake-summary"/);
    assert.match(oldHtml, /9 rounds/);
    assert.ok(oldHtml.indexOf(old.note) > oldHtml.indexOf('<details'));
    assert.ok(namedHtml.includes(`class="consciousness-wake-summary" data-i18n-authored>${named.summary}</p>`));
    assert.ok(namedHtml.indexOf(named.summary) < namedHtml.indexOf('<details'));
    assert.ok(namedHtml.includes(named.note));
    assert.match(namedHtml, /Its own note/);
    assert.doesNotMatch(oldHtml, /ui-chip/);
    assert.equal((namedHtml.match(/ui-chip/g) || []).length, 3);
    assert.match(oldHtml, /Safety Supervisor checked run_command/);
    assert.match(namedHtml, /not checked \(update_memory\)/);
    assert.doesNotMatch(namedHtml, /Safety Supervisor checked update_memory|unmetered|omitted/);
    assert.match(namedHtml, /data-cx-open-task="w-summary" data-chat-id="1">Open in Main ↗/);
    assert.doesNotMatch(wakeRowHtml({ ...named, cause: 'focus', chatId: 1099511627777, placeLabel: 'Slack' }), /data-cx-open-task/);
    assert.match(wakesCardHtml(full().recentWakes, full().coverage), /wakes that reached a model call in the last 48 h/);
});

test('unknown, zero, known, open and mixed costs stay distinct in visible and accessible words', () => {
    assert.equal(wakeCostText(wake('214a31b6')), 'spend unknown');
    assert.equal(wakeCostText(wake('w-running')), 'up to $0.02 so far');
    const priced = (amount, extra = {}) => normalizeWake({ cost_presentation: {
        has_rows: true, tracked_amount: amount, tracked_final: true, has_unpriced: false, accounting_open: false, ...extra } });
    assert.equal(wakeCostText(priced(0)), '$0.00');
    assert.equal(wakeCostText(priced(1.23)), '$1.23');
    assert.equal(wakeCostText(priced(null)), 'spend unknown');
    assert.equal(wakeCostText(priced(1.23, { tracked_final: false, accounting_open: true })), 'up to $1.23 so far');
    assert.equal(wakeCostText(priced(1.23, { has_unpriced: true })), 'Tracked: $1.23');
    const { places } = normalizePlacesResponse(FIXTURE.places);
    const unknown = dashboardPlaceRowHtml(places[1]);
    assert.match(unknown, /aria-label="[^"\n]*spend unknown/);
    assert.match(strip(unknown), /spend unknown$/);
    assert.doesNotMatch(unknown, /nothing spent/);
    assert.match(dashboardPlaceRowHtml(places[2]), /\$0\.00 spent/);
});

function fakeMount() {
    const regions = new Map();
    return {
        innerHTML: '',
        regions,
        querySelector(selector) {
            if (!regions.has(selector)) regions.set(selector, { innerHTML: '', textContent: '', dataset: {}, querySelector: () => null });
            return regions.get(selector);
        },
        addEventListener() {}, removeEventListener() {},
    };
}

test('while the full snapshot is not served, /api/state and /api/places still give status and places', async () => {
    const priorWindow = globalThis.window;
    const priorDocument = globalThis.document;
    globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
    globalThis.document = { hidden: false };
    const mount = fakeMount();
    const notServed = Object.assign(new Error('HTTP 404'), { status: 404 });
    const controller = initConsciousnessDashboard({
        mount, ws: { send() {} }, state: { activePage: 'dashboard', dashboardActiveSubtab: 'other' },
        read: async () => { throw notServed; },
        readFallback: async () => fromState(),
        readPlacesFn: async () => normalizePlacesResponse(FIXTURE.places),
    });
    try {
        await controller.refresh();
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /Consciousness · on/);
        assert.equal((mount.regions.get('[data-cx-places]').innerHTML.match(/data-place-open=/g) || []).length, 3);
        assert.match(mount.regions.get('[data-cx-wakes]').innerHTML, /does not report its recent wakes yet/);
        assert.equal(mount.regions.get('[data-cx-status]').textContent, '');
    } finally {
        controller.destroy();
        globalThis.window = priorWindow;
        globalThis.document = priorDocument;
    }
});

test('a failed read keeps the earlier values and says so', async () => {
    const priorWindow = globalThis.window;
    const priorDocument = globalThis.document;
    globalThis.window = { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} };
    globalThis.document = { hidden: false };
    const mount = fakeMount();
    let fail = false;
    const controller = initConsciousnessDashboard({
        mount, state: { activePage: 'chat' },
        read: async () => { if (fail) throw new Error('offline'); return full(); },
        readFallback: async () => { throw new Error('offline'); },
        readPlacesFn: async () => normalizePlacesResponse(FIXTURE.places),
    });
    try {
        await controller.refresh();
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /Sleeping until/);
        fail = true;
        await controller.refresh();
        assert.match(mount.regions.get('[data-cx-status]').textContent, /Could not read consciousness: offline\. Earlier values are shown\./);
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /Sleeping until/);
        assert.match(mount.regions.get('[data-cx-summary]').innerHTML, /data-enabled="1" disabled>Stop</, 'no socket, no toggle press');
    } finally {
        controller.destroy();
        globalThis.window = priorWindow;
        globalThis.document = priorDocument;
    }
});

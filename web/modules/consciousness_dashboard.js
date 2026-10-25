// Dashboard › Consciousness: what Ouroboros does when nobody asked, now and lately. The status with Stop
// (the same /bg command the Activity row and the chat header send), where its money went by cause, the own
// tasks MAX_TASKS counts, its places (each opens its sheet; Connect a place) and the recent wakes with their
// cause, where, how fast it answered, what the Safety Supervisor did and what each cost. Facts come from
// `GET /api/consciousness`; while a server does not answer that, `/api/state` and `/api/places` still give
// the status and the places, and the wakes say they are not reported. Unknown is said, never zero.
import { escapeHtmlAttr as escapeHtml } from './ui_primitives.js';
import { statusMarkHtml } from './status_mark.js';
import { strokeIcon } from './page_icons.js';
import { chipHtml, skillIconHtml } from './skill_presentation.js';
import { allowanceLabel } from './utils.js';
import { PLACES_CHANGED_EVENT, readConsciousness, readConsciousnessFromState, readPlaces } from './consciousness_api.js';
import { CHECK_CHIPS, clockText, durationText, placeStatusText, usdText, wakePhaseText, wakeFactsText, wakeCheckText, wakeCostText } from './consciousness_words.js';
import { tr, fmt } from './i18n.js';
import { renderChatMarkdown } from './chat_markdown.js';
import { costPresentationDetail } from './chat_activity.js';
import { connectPlaceButtonHtml, openConnectPlace, openPlaceSheet } from './place_sheet.js';

const SEP = ' · ';
const REFRESH_MS = 20000;
// Its own time: the house brain glyph (Lucide geometry), beside the words that say it.
const OWN_GLYPH = strokeIcon('<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/>', 14);
// Work running now: the house status mark's moving dots, in the surface's own ink.
const dots = () => statusMarkHtml('consciousness-mark', { shape: 'moving', hue: 'ink' });
const facts = (parts) => parts.filter(Boolean).map((part) => `<span class="consciousness-fact">${part}</span>`).join(SEP);

/** Active rows are explicitly marked by the producer; their phase may still be waiting. */
export function liveWake(snapshot) {
    return (snapshot.recentWakes || []).find((wake) => wake.live) || null;
}

/** The alarm clock owns the headline, independently of the recent-wake list. */
export function headlineView(snapshot) {
    return { text: snapshot.detail ? tr(snapshot.detail, snapshot.detail) : tr('task.phase.unknown', 'State unknown'),
        moving: snapshot.status === 'thinking' };
}

const causeWord = (row) => (row.cause === 'own' ? tr('consciousness.own', 'own') : row.cause === 'started' ? tr('consciousness.started', 'started work') : row.label || row.cause.replace(/^place:/, ''));

/** «on its own clock · 3 places · own $0.62 · Slack · Acme $0.88 · started work $0.48 · some calls have no price» (the rolling 24 hours). */
export function factsLineParts(snapshot, placeCount = null) {
    const parts = [snapshot.enabled === false ? tr('consciousness.off', 'off') : snapshot.enabled === null ? '' : tr('consciousness.clock', 'on its own clock')];
    if (Number.isInteger(placeCount)) parts.push(fmt(placeCount === 1 ? '{n} place' : '{n} places', { n: placeCount }));
    const spend = snapshot.spendByCause.filter((row) => row.usd !== null);
    spend.forEach((row) => parts.push(`<span data-i18n-authored>${escapeHtml(causeWord(row))}</span> <span class="consciousness-num">${escapeHtml(usdText(row.usd))}</span>`));
    const unmetered = snapshot.spendByCause.reduce((sum, row) => sum + row.unmeteredCalls, 0);
    if (unmetered > 0) parts.push(tr('consciousness.unpriced', 'Some calls have no price'));
    return parts.filter(Boolean);
}

/** The own tasks MAX_TASKS counts, each with its cause; work the owner asked for never counts. */
export function countedRootsText(snapshot) {
    if (snapshot.maxTasks === null && !snapshot.countedRoots.length) return '';
    const running = snapshot.countedRoots.length || snapshot.tasksRunning || 0;
    const head = `Its own tasks: ${running}${snapshot.maxTasks !== null ? ` of ${snapshot.maxTasks}` : ''}`;
    const names = snapshot.countedRoots.map((root) => `${root.title || root.taskId}${root.causeLabel ? ` (${root.causeLabel})` : ''}`);
    return [head, ...names].join(' · ');
}

export function summaryHtml(snapshot, { placeCount = null, hasSocket = true } = {}) {
    const headline = headlineView(snapshot);
    const kicker = snapshot.enabled === null ? 'Consciousness · state unknown' : `Consciousness · ${snapshot.enabled ? 'on' : 'off'}`;
    const money = allowanceLabel(snapshot.spentUsd, snapshot.dailyUsd, snapshot.unknownUnmetered, snapshot.integrityDegraded);
    const roots = countedRootsText(snapshot);
    const toggle = snapshot.enabled === null ? '' : `<button type="button" class="btn btn-ghost btn-sm consciousness-stop" data-cx-act="toggle" data-enabled="${snapshot.enabled ? '1' : '0'}"${hasSocket ? '' : ' disabled'}>${snapshot.enabled ? 'Stop' : 'Start'}</button>`;
    return `<div class="storage-summary-head">
            <div class="storage-summary-text consciousness-summary-text">
                <div class="storage-kicker">${escapeHtml(kicker)}</div>
                <div class="consciousness-headline"><div class="storage-total">${escapeHtml(headline.text)}${headline.moving ? dots() : ''}</div>${toggle}</div>
                ${roots ? `<p class="consciousness-next" data-i18n-authored>${escapeHtml(roots)}</p>` : ''}
            </div>
        </div>
        <div class="consciousness-summary-foot">
            ${money ? `<span class="storage-meta consciousness-total">${escapeHtml(money)} in the last 24 hours</span>` : ''}
            <span class="consciousness-causes">${facts(factsLineParts(snapshot, placeCount))}</span>
        </div>`;
}

export function dashboardPlaceRowHtml(place, { awake = false } = {}) {
    const spend = place.spent24hUsd === null ? tr('consciousness.spend_unknown', 'spend unknown') : usdText(place.spent24hUsd);
    return `<li><button type="button" class="consciousness-dash-place" data-place-open="${escapeHtml(place.id)}"
            aria-label="${escapeHtml(`Open ${place.label}${awake ? ', awake here now' : ''}: ${placeStatusText(place)}; ${place.spent24hUsd === null ? spend : fmt('{amount} spent', { amount: spend })} in 24 hours`)}">
        <span class="consciousness-dash-place-main"><span class="place-glyph" aria-hidden="true">${skillIconHtml(place.icon, 18)}</span><span class="consciousness-place-name" data-i18n-authored>${escapeHtml(place.label)}</span>${awake ? dots() : ''}</span>
        <span class="consciousness-dash-place-meta">${escapeHtml(placeStatusText(place))}${place.status?.text ? `${SEP}${escapeHtml(place.status.text)}` : ''}</span>
        <span class="consciousness-spend${place.spent24hUsd === null ? ' is-none' : ''}">${escapeHtml(spend)}</span>
    </button></li>`;
}

export function placesCardHtml(places, { error = '', awakeIn = '' } = {}) {
    const body = error ? `<p class="ui-status" data-tone="warn">${escapeHtml(error)}</p>`
        : !places ? '<p class="storage-meta">Reading places…</p>'
            : !places.length ? '<p class="consciousness-copy">No place yet. Connect one from an installed connector.</p>'
                : `<ul class="consciousness-dash-places">${places.map((place) => dashboardPlaceRowHtml(place, { awake: place.id === awakeIn })).join('')}</ul>`;
    return `<div class="consciousness-card-head">
            <div class="consciousness-card-head-text">
                <h4 class="storage-card-title">Places</h4>
                <p class="consciousness-copy">A connector you install can connect a place here: Connect a place.</p>
            </div>
            ${connectPlaceButtonHtml()}
        </div>
        ${body}
        <p class="consciousness-foot">Allowance, own tasks and concurrent conversations: <button type="button" class="consciousness-inline-link ui-hit-area" data-cx-act="settings">Settings ›</button></p>`;
}

/** One view of the card: authored line, whole note on request, factual phase and cost. */
export function wakeRowHtml(wake, { open = false } = {}) {
    const working = wake.phase === 'running' || wake.phase === 'working';
    const where = wake.cause === 'focus'
        ? `<span class="consciousness-wake-where" data-i18n-authored>${escapeHtml(wake.placeLabel)}</span>`
        : `<span class="consciousness-wake-own"><span class="place-glyph" aria-hidden="true">${OWN_GLYPH}</span>${escapeHtml(tr('task.title.own_time', 'Own time'))}</span>`;
    const duration = durationText(wake.startedAt, wake.endedAt);
    const counts = escapeHtml(wakeFactsText(wake));
    const checks = wake.checks.map(wakeCheckText).filter(Boolean);
    const flags = wake.checks.filter((check) => CHECK_CHIPS[check.chip]?.tone === 'warn')
        .map((check) => chipHtml(wakeCheckText(check), 'warn')).join(' ');
    const note = wake.note ? `<p class="consciousness-wake-note-label">${escapeHtml(tr('consciousness.own_note', 'Its own note'))}</p>
        <div class="consciousness-wake-note ui-rich-content" data-i18n-authored>${renderChatMarkdown(wake.note)}</div>` : '';
    const costDetail = costPresentationDetail(wake.cost.cost_presentation);
    // This base has no place-room reader. Never draw a door that cannot land on its card.
    const door = wake.chatId === 1 && wake.taskId ? `<button type="button" class="consciousness-inline-link ui-hit-area" data-cx-open-task="${escapeHtml(wake.taskId)}" data-chat-id="${wake.chatId}">${escapeHtml(tr('consciousness.open_main', 'Open in Main'))} ↗</button>` : '';
    const line = wake.summary ? `<p class="consciousness-wake-summary" data-i18n-authored>${escapeHtml(wake.summary)}</p>`
        : counts ? `<p class="consciousness-wake-facts is-line">${counts}</p>` : '';
    return `<li class="consciousness-wake${wake.live ? ' is-live' : ''}" data-wake="${escapeHtml(wake.taskId)}">
        <time class="consciousness-wake-time"${wake.startedAt ? ` datetime="${escapeHtml(wake.startedAt)}"` : ''}>${escapeHtml(clockText(wake.startedAt) || '—')}</time>
        <div class="consciousness-wake-main">
            <div class="consciousness-wake-head">${where}${SEP}${escapeHtml(wakePhaseText(wake))}${working ? dots() : ''}${duration ? SEP + escapeHtml(duration) : ''}</div>
            ${line}${flags ? `<div class="consciousness-wake-checks">${flags}</div>` : ''}
            <details class="consciousness-wake-more"${open ? ' open' : ''}><summary class="ui-hit-area">${escapeHtml(tr('consciousness.more', 'more'))}</summary>
                ${note}${counts ? `<p class="consciousness-wake-facts">${counts}</p>` : ''}
                ${checks.map((words) => `<p class="consciousness-wake-facts">${escapeHtml(words)}</p>`).join('')}
                ${costDetail ? `<p class="consciousness-wake-facts">${escapeHtml(tr(costDetail, costDetail))}</p>` : ''}${door}
            </details>
        </div>
        <span class="consciousness-wake-cost">${escapeHtml(wakeCostText(wake))}</span>
    </li>`;
}

export function wakesCardHtml(wakes, coverage = '') {
    const body = !Array.isArray(wakes)
        ? `<p class="consciousness-copy">${escapeHtml(tr('consciousness.wakes_unavailable', 'This server does not report its recent wakes yet.'))}</p>`
        : !wakes.length ? `<p class="consciousness-copy">${escapeHtml(tr('consciousness.no_wakes', 'No wakes yet.'))}</p>`
            : `<ol class="consciousness-wakes">${wakes.map((wake) => wakeRowHtml(wake)).join('')}</ol>`;
    return `<div class="consciousness-card-head"><div class="consciousness-card-head-text">
            <h4 class="storage-card-title">${escapeHtml(tr('consciousness.recent', 'Recent wakes'))}</h4>
            ${coverage ? `<p class="storage-meta">${escapeHtml(tr(coverage, coverage))}</p>` : ''}
        </div></div>${body}`;
}

/** Keyed rows: an open disclosure is a reading surface, retained until its reader closes it. */
export function paintWakeRows(region, wakes, coverage, opened, put) {
    const list = region.querySelector('.consciousness-wakes');
    // Native toggle is queued: read the open property too when a refresh wins that race.
    for (const row of list?.children || []) if (row.querySelector('details')?.open) opened.add(row.dataset.wake);
    if (list && opened.size && (!Array.isArray(wakes) || !wakes.length)) return;
    if (!list || !Array.isArray(wakes) || !wakes.length) { put(region, wakesCardHtml(wakes, coverage)); return; }
    const scroller = region.closest('.costs-scroll');
    const reading = [...list.children].find((row) => opened.has(row.dataset.wake));
    const top = reading?.getBoundingClientRect().top;
    const rows = new Map([...list.children].map((row) => [row.dataset.wake, row]));
    let before = list.firstElementChild;
    for (const wake of wakes) {
        let row = rows.get(wake.taskId);
        if (!row) {
            const template = region.ownerDocument.createElement('template');
            template.innerHTML = wakeRowHtml(wake);
            row = template.content.firstElementChild;
        } else if (!opened.has(wake.taskId)) {
            const template = region.ownerDocument.createElement('template');
            template.innerHTML = wakeRowHtml(wake);
            put(row, template.content.firstElementChild.innerHTML);
            row.className = template.content.firstElementChild.className;
        }
        // Defer reordering existing rows while reading; moving a focused node drops its selection.
        if (row !== before && (!reading || !row.isConnected)) list.insertBefore(row, before);
        before = row.nextElementSibling;
        rows.delete(wake.taskId);
    }
    for (const [id, row] of rows) if (!opened.has(id)) row.remove();
    if (scroller && reading?.isConnected) scroller.scrollTop += reading.getBoundingClientRect().top - top;
}

/** Mount the tab into its Dashboard panel. */
export function initConsciousnessDashboard({ mount, ws, state, openSettingsTab, read = readConsciousness,
    readFallback = readConsciousnessFromState, readPlacesFn = readPlaces } = {}) {
    if (!mount) return { refresh: () => {}, destroy() {} };
    mount.innerHTML = `<div class="settings-embedded-content"><div class="costs-scroll scroll-fade-y">
        <div class="storage-root consciousness-root">
            <p class="ui-status consciousness-read-status" role="status" data-cx-status></p>
            <section class="storage-summary ui-card consciousness-summary" aria-label="Consciousness" data-cx-summary><p class="storage-meta">Reading…</p></section>
            <section class="ui-card consciousness-card" aria-label="Places" data-cx-places></section>
            <section class="ui-card consciousness-card" aria-label="Recent wakes" data-cx-wakes></section>
        </div></div></div>`;
    const regions = {
        status: mount.querySelector('[data-cx-status]'), summary: mount.querySelector('[data-cx-summary]'),
        places: mount.querySelector('[data-cx-places]'), wakes: mount.querySelector('[data-cx-wakes]'),
    };
    // The page binds every `.scroll-fade-y` scroller once, after the panels mount (app.js).
    let snapshot = null;
    let places = null;
    let generation = 0;
    let timer = 0;
    let destroyed = false;
    const shown = () => !document.hidden && state?.activePage === 'dashboard' && state?.dashboardActiveSubtab === 'consciousness';

    // A refresh that found nothing new leaves a region's nodes alone, so focus and reading position stay.
    const opened = new Set();
    const painted = new WeakMap();
    const put = (region, html) => {
        if (painted.get(region) === html || region.innerHTML === html) return;
        painted.set(region, html);
        region.innerHTML = html;
    };
    function paint({ placesError = '' } = {}) {
        if (snapshot) put(regions.summary, summaryHtml(snapshot, { placeCount: places ? places.length : null, hasSocket: Boolean(ws?.send) }));
        const awake = snapshot ? liveWake(snapshot) : null;
        put(regions.places, placesCardHtml(places, { error: placesError, awakeIn: awake?.cause === 'focus' ? places?.find((place) => place.rooms.some((room) => room.chatId === awake.chatId))?.id : '' }));
        paintWakeRows(regions.wakes, snapshot?.recentWakes ?? null, snapshot?.coverage || '', opened, put);
    }

    async function refresh({ refreshPlaces = false } = {}) {
        if (destroyed) return;
        const current = ++generation;
        regions.status.textContent = snapshot ? '' : 'Reading…';
        let next = null;
        let readError = '';
        try { next = await read(); } catch {
            try { next = await readFallback(); } catch (error) { readError = error?.message || String(error); }
        }
        let placesError = '';
        let nextPlaces = places;
        if (next && (refreshPlaces || !nextPlaces)) {
            try { nextPlaces = (await readPlacesFn()).places; } catch (error) { placesError = `Places could not be read: ${error?.message || error}`; }
        }
        if (destroyed || current !== generation) return;
        if (next) { snapshot = next; places = nextPlaces ?? places; }
        regions.status.textContent = readError ? `Could not read consciousness: ${readError}. ${snapshot ? 'Earlier values are shown.' : ''}` : '';
        regions.status.dataset.tone = readError ? 'error' : '';
        if (snapshot) paint({ placesError });
        schedule();
    }

    function schedule() {
        clearTimeout(timer);
        if (!destroyed && shown()) timer = setTimeout(refresh, REFRESH_MS);
    }

    const onToggle = (event) => {
        if (!event.target.matches?.('.consciousness-wake-more')) return;
        const id = event.target.closest('[data-wake]').dataset.wake;
        if (event.target.open) opened.add(id); else { opened.delete(id); paint(); }
    };
    const onClick = async (event) => {
        const door = event.target.closest('[data-cx-open-task]');
        if (door) {
            window.dispatchEvent(new CustomEvent('ouro:open-task', { detail: { task_id: door.dataset.cxOpenTask, chat_id: Number(door.dataset.chatId) } }));
            return;
        }
        const row = event.target.closest('[data-place-open]');
        if (row) {
            const place = (places || []).find((entry) => entry.id === row.dataset.placeOpen);
            if (place) openPlaceSheet(place, { returnFocus: row });
            return;
        }
        const connect = event.target.closest('[data-place-connect]');
        if (connect) { openConnectPlace({ returnFocus: connect }); return; }
        const button = event.target.closest('[data-cx-act]');
        if (!button || button.disabled) return;
        const act = button.dataset.cxAct;
        if (act === 'settings') {
            await openSettingsTab?.('behavior');
            document.getElementById('settings-consciousness')?.scrollIntoView({ block: 'start' });
            return;
        }
        if (act === 'toggle') {
            button.disabled = true;
            // The existing direct control command (the Activity row and the chat header send the same).
            ws?.send?.({ type: 'command', cmd: `/bg ${button.dataset.enabled === '1' ? 'stop' : 'start'}` });
            await new Promise((resolve) => setTimeout(resolve, 400));
            await refresh();
        }
    };
    const onShown = (event) => { if (event?.detail?.tab === 'consciousness') void refresh(); else clearTimeout(timer); };
    const onChanged = () => { places = null; if (shown()) void refresh(); };
    mount.addEventListener('click', onClick);
    mount.addEventListener('toggle', onToggle, true);
    window.addEventListener('ouro:dashboard-subtab-shown', onShown);
    window.addEventListener(PLACES_CHANGED_EVENT, onChanged);
    if (shown()) void refresh();
    return {
        refresh,
        destroy() {
            destroyed = true;
            clearTimeout(timer);
            mount.removeEventListener('click', onClick);
            mount.removeEventListener('toggle', onToggle, true);
            window.removeEventListener('ouro:dashboard-subtab-shown', onShown);
            window.removeEventListener(PLACES_CHANGED_EVENT, onChanged);
        },
    };
}

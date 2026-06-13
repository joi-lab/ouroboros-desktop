import {
    LOG_CATEGORIES,
    categorizeLogEvent,
    duplicateLogEventKey,
    getLogTaskGroupId,
    isGroupedTaskEvent,
    normalizeLogTs,
    prettyLogEvent,
    summarizeLogEvent,
} from './log_events.js';
import { escapeHtml } from './utils.js';

export function initLogs({ ws, state, mount = null, embedded = false, hostPage = 'dashboard', hostSubtab = 'logs' }) {
    const MAX_LOGS = 500;
    const MAX_TASK_EVENTS = 30;
    const duplicateWindowMs = 5000;
    const duplicateState = new Map();
    const taskGroups = new Map();

    state.activeFilters = state.activeFilters || Object.fromEntries(
        Object.keys(LOG_CATEGORIES).map((key) => [key, true]),
    );

    const page = document.createElement('div');
    page.id = 'page-logs';
    page.className = embedded ? 'settings-embedded-content settings-logs-panel' : 'page';
    // v5.7.0: when embedded inside the Dashboard tab strip, skip the inner
    // .page-header (the outer Dashboard header + tab pill already labels the
    // panel — drawing another "Logs" h2 here wasted ~44px of fixed vertical
    // space on every viewport). The Clear button moves into the filter row.
    const headerBlock = embedded
        ? ''
        : `
        <div class="page-header">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>
            <h2>Logs</h2>
            <div class="spacer"></div>
            <button class="btn btn-default" id="btn-clear-logs">Очистить</button>
        </div>`;
    const inlineClear = embedded
        ? `<button class="btn btn-default logs-inline-clear" id="btn-clear-logs">Очистить</button>`
        : '';
    page.innerHTML = `
        ${headerBlock}
        <div class="logs-filters" id="log-filters">${inlineClear}</div>
        <div id="log-entries"></div>
    `;
    (mount || document.getElementById('content')).appendChild(page);

    const filtersDiv = page.querySelector('#log-filters');
    const logEntries = page.querySelector('#log-entries');
    function isLogsVisible() {
        return embedded
            ? state.activePage === hostPage && state.dashboardActiveSubtab === hostSubtab
            : state.activePage === 'logs';
    }

    function scrollToLatest() {
        if (!isLogsVisible()) return;
        requestAnimationFrame(() => {
            logEntries.scrollTop = logEntries.scrollHeight;
        });
    }

    function updateVisibility(entry) {
        entry.hidden = !state.activeFilters[entry.dataset.category];
    }

    function renderFilters() {
        const inlineClear = filtersDiv.querySelector('.logs-inline-clear');
        filtersDiv.innerHTML = '';
        Object.entries(LOG_CATEGORIES).forEach(([key, cat]) => {
            const chip = document.createElement('button');
            chip.className = `filter-chip ${state.activeFilters[key] ? 'active' : ''}`;
            chip.textContent = cat.label;
            chip.addEventListener('click', () => {
                state.activeFilters[key] = !state.activeFilters[key];
                chip.classList.toggle('active');
                logEntries.querySelectorAll('.log-entry').forEach(updateVisibility);
                scrollToLatest();
            });
            filtersDiv.appendChild(chip);
        });
        if (inlineClear) filtersDiv.appendChild(inlineClear);
    }

    function trimEntries() {
        while (logEntries.children.length > MAX_LOGS) {
            const first = logEntries.firstElementChild;
            if (!first) break;
            const removeKey = [...duplicateState.entries()].find(([, tracked]) => tracked.entry === first)?.[0];
            if (removeKey) duplicateState.delete(removeKey);
            if (first.dataset.taskGroup) taskGroups.delete(first.dataset.taskGroup);
            first.remove();
        }
    }

    function metaPills(meta) {
        if (!meta.length) return '';
        return `<div class="log-meta">${meta.map((item) => `<span class="log-pill">${escapeHtml(item)}</span>`).join('')}</div>`;
    }

    function logMainHtml({ ts = '', type = '', phase = 'info', headline = 'Event', repeat = '', attrs = {} }) {
        const attr = (key) => attrs[key] ? ` ${attrs[key]}` : '';
        return `
            <div class="log-main">
                <span class="log-ts"${attr('ts')}>${escapeHtml(ts)}</span>
                ${type ? `<span class="log-type ${escapeHtml(type.className || '')}"${attr('type')}>${escapeHtml(type.label || '')}</span>` : ''}
                <span class="log-phase ${escapeHtml(phase)}"${attr('phase')}>${escapeHtml(phase)}</span>
                <span class="log-headline"${attr('headline')}>${escapeHtml(headline)}</span>
                <span class="log-repeat"${attr('repeat')}${repeat ? '' : ' hidden'}>${escapeHtml(repeat)}</span>
            </div>
        `;
    }

    function bindRawToggle(root) {
        root.querySelectorAll('.log-raw-toggle').forEach((rawToggle) => {
            if (rawToggle.dataset.bound === '1') return;
            const rawEl = rawToggle.parentElement?.nextElementSibling;
            if (!rawEl || !rawEl.classList.contains('log-raw')) return;
            rawToggle.dataset.bound = '1';
            rawToggle.addEventListener('click', () => {
                const isHidden = rawEl.hasAttribute('hidden');
                if (isHidden) {
                    rawEl.removeAttribute('hidden');
                    rawToggle.textContent = 'Скрыть raw';
                } else {
                    rawEl.setAttribute('hidden', '');
                    rawToggle.textContent = 'Raw';
                }
            });
        });
    }

    function createStandaloneEntry(evt) {
        const view = summarizeLogEvent(evt);
        const cat = categorizeLogEvent(evt);
        const dedupeKey = duplicateLogEventKey(evt);
        const now = (() => {
            const parsed = evt.ts ? Date.parse(evt.ts) : NaN;
            return Number.isFinite(parsed) ? parsed : Date.now();
        })();

        if (dedupeKey) {
            let last = duplicateState.get(dedupeKey);
            if (last && !logEntries.contains(last.entry)) {
                duplicateState.delete(dedupeKey);
                last = null;
            }
            if (last && now - last.ts <= duplicateWindowMs) {
                last.count += 1;
                last.ts = now;
                const repeatEl = last.entry.querySelector('.log-repeat');
                if (repeatEl) {
                    repeatEl.textContent = `x${last.count}`;
                    repeatEl.hidden = false;
                }
                const tsEl = last.entry.querySelector('.log-ts');
                if (tsEl) tsEl.textContent = normalizeLogTs(evt.ts || evt.timestamp);
                const rawEl = last.entry.querySelector('.log-raw');
                if (rawEl) rawEl.textContent = prettyLogEvent(evt);
                logEntries.appendChild(last.entry);
                updateVisibility(last.entry);
                return;
            }
        }

        const entry = document.createElement('div');
        entry.className = 'log-entry';
        entry.dataset.category = cat;
        const bodyHtml = view.body
            ? `<div class="log-body">${escapeHtml(view.body)}</div>`
            : '';
        entry.innerHTML = `
            ${logMainHtml({
                ts: normalizeLogTs(evt.ts || evt.timestamp),
                type: { className: cat, label: view.typeLabel },
                phase: view.phase || 'info',
                headline: view.headline || 'Event',
            })}
            ${metaPills(view.meta)}
            ${bodyHtml}
            <div class="log-actions">
                <button class="log-raw-toggle" type="button">Raw</button>
            </div>
            <pre class="log-raw" hidden>${escapeHtml(prettyLogEvent(evt))}</pre>
        `;
        bindRawToggle(entry);
        updateVisibility(entry);
        logEntries.appendChild(entry);

        if (dedupeKey) {
            duplicateState.set(dedupeKey, { entry, ts: now, count: 1 });
        }

        trimEntries();
        if (state.activeFilters[cat]) scrollToLatest();
    }

    function createTaskGroupCard(groupId, category) {
        const entry = document.createElement('div');
        entry.className = 'log-entry log-task-card';
        entry.dataset.category = category;
        entry.dataset.taskGroup = groupId;
        entry.innerHTML = `
            ${logMainHtml({
                type: { className: category, label: groupId === 'bg-consciousness' ? 'background' : 'task' },
                phase: 'info',
                headline: 'Task activity',
                attrs: {
                    ts: 'data-task-ts',
                    type: 'data-task-kind',
                    phase: 'data-task-phase',
                    headline: 'data-task-headline',
                    repeat: 'data-task-count',
                },
            })}
            <div class="log-task-summary" data-task-summary></div>
            <details class="log-task-details">
                <summary>Хронология</summary>
                <div class="log-task-timeline" data-task-timeline></div>
            </details>
        `;
        const record = {
            entry,
            ts: entry.querySelector('[data-task-ts]'),
            kind: entry.querySelector('[data-task-kind]'),
            phase: entry.querySelector('[data-task-phase]'),
            headline: entry.querySelector('[data-task-headline]'),
            count: entry.querySelector('[data-task-count]'),
            summary: entry.querySelector('[data-task-summary]'),
            timeline: entry.querySelector('[data-task-timeline]'),
            events: 0,
            category,
            recent: [],
        };
        taskGroups.set(groupId, record);
        return record;
    }

    function renderTaskTimeline(record) {
        record.timeline.innerHTML = record.recent.map((item) => `
            <div class="log-task-event">
                ${logMainHtml({
                    ts: item.ts,
                    phase: item.phase || 'info',
                    headline: item.headline,
                    repeat: item.count > 1 ? `x${item.count}` : '',
                })}
                ${metaPills(item.meta)}
                ${item.body ? `<div class="log-body">${escapeHtml(item.body)}</div>` : ''}
                <div class="log-actions">
                    <button class="log-raw-toggle" type="button">Raw</button>
                </div>
                <pre class="log-raw" hidden>${escapeHtml(item.raw || '')}</pre>
            </div>
        `).join('');
        bindRawToggle(record.timeline);
    }

    function updateTaskGroupCard(evt) {
        const groupId = getLogTaskGroupId(evt);
        if (!groupId) {
            createStandaloneEntry(evt);
            return;
        }

        const view = summarizeLogEvent(evt);
        const eventCategory = categorizeLogEvent(evt);
        const category = groupId === 'bg-consciousness'
            ? 'consciousness'
            : (eventCategory === 'errors' ? 'errors' : 'tasks');
        const record = taskGroups.get(groupId) || createTaskGroupCard(groupId, category);
        const ts = normalizeLogTs(evt.ts || evt.timestamp);

        record.events += 1;
        record.category = category;
        record.entry.dataset.category = category;
        record.ts.textContent = ts;
        record.kind.textContent = groupId === 'bg-consciousness' ? 'background' : `task ${groupId}`;
        record.kind.className = `log-type ${category}`;
        record.phase.textContent = view.phase || 'info';
        record.phase.className = `log-phase ${view.phase || 'info'}`;
        record.headline.textContent = view.headline || 'Task activity';
        record.count.textContent = `x${record.events}`;
        record.count.hidden = record.events <= 1;
        record.summary.innerHTML = metaPills([
            groupId === 'bg-consciousness' ? 'background' : `task=${groupId}`,
            ...view.meta,
        ]);

        const last = record.recent[record.recent.length - 1];
        const dedupeKey = duplicateLogEventKey(evt);
        if (last && last.dupKey && last.dupKey === dedupeKey) {
            last.count += 1;
            last.ts = ts;
            last.meta = view.meta;
            last.body = view.body;
            last.raw = prettyLogEvent(evt);
        } else {
            record.recent.push({
                ts,
                phase: view.phase || 'info',
                headline: view.headline || 'Task event',
                meta: view.meta,
                body: view.body,
                raw: prettyLogEvent(evt),
                count: 1,
                dupKey: dedupeKey,
            });
            if (record.recent.length > MAX_TASK_EVENTS) record.recent.shift();
        }

        renderTaskTimeline(record);
        updateVisibility(record.entry);
        logEntries.appendChild(record.entry);
        trimEntries();
        if (state.activeFilters[category]) scrollToLatest();
    }

    function addLogEntry(evt) {
        if (isGroupedTaskEvent(evt)) {
            updateTaskGroupCard(evt);
            return;
        }
        createStandaloneEntry(evt);
    }

    renderFilters();

    ws.on('log', (msg) => {
        if (msg.data) addLogEntry(msg.data);
    });

    page.querySelector('#btn-clear-logs').addEventListener('click', () => {
        duplicateState.clear();
        taskGroups.clear();
        logEntries.innerHTML = '';
    });

    window.addEventListener('ouro:dashboard-subtab-shown', (event) => {
        if (event.detail?.tab === 'logs') scrollToLatest();
    });
}

import { renderPageHeader } from './page_header.js';
import { PAGE_ICONS } from './page_icons.js';
import { applyMasonry } from './masonry.js';
import {
    apiClient,
    apiFetch,
    cleanExtensionRoute,
    extensionRoutePath,
    extensionRoutePrefix,
} from './api_client.js';
import {
    escapeHtmlAttr as escapeHtml,
    renderMarkdownSafe,
} from './utils.js';
import { downloadViaHostBridge } from './ui_helpers.js';

function pageTemplate() {
    return `
        <section class="page app-page-glass" id="page-widgets">
            ${renderPageHeader({
                title: 'Виджеты',
                icon: PAGE_ICONS.widgets,
                description: 'Проверенные интерфейсы расширений отображаются здесь, отдельно от каталога навыков.',
                actionsHtml: '<button id="widgets-refresh" class="btn btn-default btn-sm">Обновить</button>',
            })}
            <div class="widgets-scroll scroll-fade-y">
                <div id="widgets-list" class="widgets-list"></div>
            </div>
        </section>
    `;
}

function renderShell(host, tabs) {
    if (!tabs.length) {
        host.innerHTML = '<div class="muted">Активных виджетов пока нет. Проверьте и включите расширение с вкладкой пользовательского интерфейса.</div>';
        return;
    }
    host.innerHTML = tabs.map((tab) => {
        // Avoid leaking internal "skill:tab_id"; show skill only as needed.
        const title = tab.title || tab.tab_id || tab.skill;
        const subtitle = tab.skill && tab.skill !== title
            ? `<span class="widgets-card-source">from ${escapeHtml(tab.skill)}</span>`
            : '';
        const span = Number(tab.span || tab.grid_span || 1);
        const spanClass = span >= 2 ? ' widgets-card-span-2' : '';
        return `
        <article class="widgets-card${spanClass}" data-widget-key="${escapeHtml(tab.key || `${tab.skill}:${tab.tab_id}`)}">
            <div class="widgets-card-head">
                <strong>${escapeHtml(title)}</strong>
                ${subtitle}
            </div>
            <div class="widgets-card-body" data-widget-mount></div>
        </article>
        `;
    }).join('');
}

function applyWidgetsLayout(list) {
    applyMasonry(list, { minColumnWidth: Number.MAX_SAFE_INTEGER });
}

function cleanWidgetRoute(value) {
    const route = String(value || '').trim().replace(/^\/+/, '');
    const parts = route.split('/').filter(Boolean);
    if (!route || route.includes('\\') || parts.some((part) => part === '.' || part === '..')) {
        return '';
    }
    return parts.map(encodeURIComponent).join('/');
}

function extensionRouteUrl(tab, route, params) {
    const cleanRoute = cleanWidgetRoute(route);
    if (!cleanRoute) return '';
    const base = `/api/extensions/${encodeURIComponent(tab.skill)}/${cleanRoute}`;
    const query = params instanceof URLSearchParams && String(params) ? `?${params}` : '';
    return base + query;
}

function getPath(root, path, fallback = '') {
    if (!path) return root ?? fallback;
    let current = root;
    for (const part of String(path).split('.').filter(Boolean)) {
        if (current == null || typeof current !== 'object') return fallback;
        current = current[part];
    }
    return current ?? fallback;
}

function safeMediaSrc(tab, spec, state) {
    const route = spec.route || spec.api_route || '';
    if (route) {
        const params = new URLSearchParams();
        for (const [key, value] of Object.entries(spec.query || {})) {
            params.set(key, String(value ?? ''));
        }
        return extensionRoutePath(tab.skill, route, params);
    }
    const value = getPath(state[spec.target || 'result'], spec.path || '', spec.src || '');
    const text = String(value || '').trim();
    if (/^data:(image\/(?:png|jpeg|jpg|gif|webp)|audio\/(?:mpeg|wav|ogg)|video\/(?:mp4|webm|ogg));base64,[A-Za-z0-9+/=]+$/i.test(text)) {
        return text;
    }
    if (text.startsWith('/api/extensions/')) {
        try {
            const parsed = new URL(text, window.location.origin);
            const expectedPrefix = extensionRoutePrefix(tab.skill);
            if (parsed.origin === window.location.origin && parsed.pathname.startsWith(expectedPrefix)) {
                return parsed.pathname + parsed.search;
            }
        } catch {
            return '';
        }
    }
    return '';
}

function routePrefixToMediaSpec(routePrefix, value, itemType = 'image') {
    const text = String(value || '').trim();
    const prefix = String(routePrefix || '').trim();
    if (!prefix || !text) return { type: itemType, src: text };
    const [route, queryKey = 'path'] = prefix.split('?', 2);
    const key = queryKey.endsWith('=') ? queryKey.slice(0, -1) : queryKey;
    return {
        type: itemType,
        route,
        query: { [key || 'path']: text },
    };
}

function filenameFromWidgetUrl(url, fallback = 'download') {
    try {
        const parsed = new URL(url, window.location.origin);
        for (const key of ['filename', 'image_id', 'clip_id']) {
            const value = parsed.searchParams.get(key);
            if (value) return value.split('/').pop() || fallback;
        }
        const base = parsed.pathname.split('/').filter(Boolean).pop();
        return base || fallback;
    } catch {
        return fallback;
    }
}

function fieldValue(form, field) {
    const name = String(field.name || '');
    const input = form.elements[name];
    if (!input) return '';
    if (input.type === 'checkbox') return input.checked;
    return input.value;
}

function renderField(field, savedValues) {
    const name = escapeHtml(field.name || '');
    const label = escapeHtml(field.label || field.name || '');
    const rawName = String(field.name || '');
    const hasSaved = Object.prototype.hasOwnProperty.call(savedValues || {}, rawName);
    const saved = hasSaved ? savedValues[rawName] : field.default;
    const value = escapeHtml(saved ?? '');
    const required = field.required ? 'required' : '';
    if (field.type === 'textarea') {
        return `<label class="widget-field"><span>${label}</span><textarea name="${name}" ${required}>${value}</textarea></label>`;
    }
    if (field.type === 'select') {
        const options = (field.options || []).map((option) => {
            const optValue = typeof option === 'object' ? option.value : option;
            const optLabel = typeof option === 'object' ? (option.label ?? option.value) : option;
            return `<option value="${escapeHtml(optValue)}"${String(optValue) === String(saved ?? '') ? ' selected' : ''}>${escapeHtml(optLabel)}</option>`;
        }).join('');
        return `<label class="widget-field"><span>${label}</span><select name="${name}" ${required}>${options}</select></label>`;
    }
    if (field.type === 'checkbox') {
        return `<label class="widget-field widget-field-inline"><input type="checkbox" name="${name}" ${saved ? 'checked' : ''}> <span>${label}</span></label>`;
    }
    const type = ['text', 'number', 'url', 'email'].includes(field.type) ? field.type : 'text';
    return `<label class="widget-field"><span>${label}</span><input type="${type}" name="${name}" value="${value}" ${required}></label>`;
}

function chartConfig(component, data) {
    const type = ['line', 'bar'].includes(component.chart_type) ? component.chart_type : 'line';
    const labels = component.labels || getPath(data, component.labels_path || 'labels', []);
    const datasets = component.datasets || getPath(data, component.datasets_path || 'datasets', []);
    return {
        type,
        data: {
            labels: Array.isArray(labels) ? labels.map((item) => String(item ?? '')) : [],
            datasets: Array.isArray(datasets) ? datasets.map((dataset) => ({
                label: String(dataset?.label ?? 'Series'),
                data: Array.isArray(dataset?.data) ? dataset.data.map((value) => Number(value) || 0) : [],
            })) : [],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: { legend: { display: true } },
        },
    };
}

function renderDataComponent(tab, component, state, status, componentState = {}, componentKey = '') {
    const type = String(component.type || '');
    const target = component.target || 'result';
    const data = state[target] || {};
    if (component.condition_key && !getPath(data, component.condition_key, false)) {
        return '';
    }
    if (type === 'status') {
        const current = status[target] || 'idle';
        return `<div class="widget-status" data-state="${escapeHtml(current)}">${escapeHtml(component[current] || current)}</div>`;
    }
    if (type === 'kv') {
        const fields = component.fields || [];
        const rows = fields.map((field) => {
            const label = escapeHtml(field.label || field.path || '');
            const value = getPath(data, field.path, '—');
            return `<div class="widget-kv-row"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`;
        }).join('');
        return `<div class="widget-kv">${rows || '<div class="muted">No data.</div>'}</div>`;
    }
    if (type === 'key_value') {
        const rows = getPath(data, component.items_key || component.path || '', []);
        if (!Array.isArray(rows) || !rows.length) return '';
        return `<div class="widget-kv">${rows.map((row) => `<div class="widget-kv-row"><span>${escapeHtml(row?.key || row?.label || '')}</span><strong>${escapeHtml(row?.value ?? '')}</strong></div>`).join('')}</div>`;
    }
    if (type === 'table') {
        const rows = getPath(data, component.path || '', []);
        const cols = component.columns || [];
        if (!Array.isArray(rows)) return '<div class="muted">No rows.</div>';
        return `<div class="widget-table-wrap"><table class="widget-table"><thead><tr>${cols.map((c) => `<th>${escapeHtml(c.label || c.path || '')}</th>`).join('')}</tr></thead><tbody>${rows.map((row) => `<tr>${cols.map((c) => `<td>${escapeHtml(getPath(row, c.path, ''))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    if (type === 'markdown') {
        const value = component.text ?? getPath(data, component.path || '', '');
        return `<div class="widget-markdown">${renderMarkdownSafe(value)}</div>`;
    }
    if (type === 'json') {
        const value = component.path ? getPath(data, component.path, {}) : data;
        return `<details class="widget-json"><summary>${escapeHtml(component.label || 'JSON')}</summary><pre>${escapeHtml(JSON.stringify(value, null, 2))}</pre></details>`;
    }
    if (type === 'code') {
        const value = component.text ?? getPath(data, component.path || '', '');
        const label = component.label ? `<div class="widget-code-label">${escapeHtml(component.label)}</div>` : '';
        return `<div class="widget-code">${label}<pre><code>${escapeHtml(value)}</code></pre></div>`;
    }
    if (type === 'chart') {
        const config = chartConfig(component, component.path ? getPath(data, component.path, {}) : data);
        return `<div class="widget-chart"><canvas data-widget-chart-config="${escapeHtml(JSON.stringify(config))}"></canvas></div>`;
    }
    if (type === 'tabs') {
        const tabs = Array.isArray(component.tabs) ? component.tabs : [];
        const stateKey = `tab:${componentKey}`;
        const active = Math.max(0, Math.min(Number(componentState[stateKey] || 0), Math.max(tabs.length - 1, 0)));
        const buttons = tabs.map((item, idx) => (
            `<button type="button" class="widget-tab-btn ${idx === active ? 'active' : ''}" data-widget-tab-key="${escapeHtml(stateKey)}" data-widget-tab-idx="${idx}">${escapeHtml(item.label || `Tab ${idx + 1}`)}</button>`
        )).join('');
        const activeTab = tabs[active] || {};
        const body = (activeTab.components || [])
            .map((child, idx) => renderDataComponent(tab, child, state, status, componentState, `${componentKey}:${active}:${idx}`))
            .join('');
        return `<div class="widget-tabs"><div class="widget-tab-list">${buttons}</div><div class="widget-tab-body">${body || '<div class="muted">No content.</div>'}</div></div>`;
    }
    if (type === 'stream') {
        const current = status[target] || 'idle';
        return `<div class="widget-stream" data-state="${escapeHtml(current)}">${escapeHtml(component[current] || component.label || current)}</div>`;
    }
    if (['image', 'audio', 'video', 'file'].includes(type)) {
        const src = safeMediaSrc(tab, component, state);
        const label = escapeHtml(component.label || component.alt || type);
        if (!src) return `<div class="muted">${label}: no safe media source.</div>`;
        if (type === 'image') return `<figure class="widget-media"><img src="${escapeHtml(src)}" alt="${escapeHtml(component.alt || label)}"><figcaption>${label}</figcaption></figure>`;
        if (type === 'audio') return `<div class="widget-media"><div>${label}</div><audio controls src="${escapeHtml(src)}"></audio></div>`;
        if (type === 'video') return `<div class="widget-media"><div>${label}</div><video controls src="${escapeHtml(src)}"></video></div>`;
        const filename = escapeHtml(component.filename || filenameFromWidgetUrl(src, label || 'download'));
        return `<button class="btn btn-default widget-download" type="button" data-widget-download-url="${escapeHtml(src)}" data-widget-download-filename="${filename}">${label}</button>`;
    }
    if (type === 'gallery') {
        let items = component.items || getPath(data, component.path || component.items_key || '', []);
        if (!Array.isArray(items)) return '<div class="muted">No media items.</div>';
        if (component.items_key && component.route_prefix) {
            items = items.map((item) => routePrefixToMediaSpec(
                component.route_prefix,
                typeof item === 'object' ? (item.path || item.src || item.url || '') : item,
                component.item_type || 'image',
            ));
        }
        return `<div class="widget-gallery">${items.map((item, idx) => renderDataComponent(tab, { ...item, type: item.type || 'image' }, state, status, componentState, `${componentKey}:gallery:${idx}`)).join('')}</div>`;
    }
    if (type === 'progress') {
        const value = Number(getPath(data, component.path || component.value_key || 'progress', 0));
        const bounded = Number.isFinite(value) ? Math.max(0, Math.min(100, value)) : 0;
        const label = component.label_key ? getPath(data, component.label_key, '') : '';
        return `<div class="widget-progress"><progress max="100" value="${bounded}"></progress><span>${bounded}%${label ? ` · ${escapeHtml(label)}` : ''}</span></div>`;
    }
    // Host-owned map renderer; no skill-supplied JS reaches the SPA origin.
    if (type === 'map') {
        const markers = Array.isArray(component.markers) ? component.markers : [];
        const list = markers.length
            ? `<ul class="widget-map-list">${markers.map((m) => `<li><strong>${escapeHtml(m.label || `${m.lat}, ${m.lon}`)}</strong>${m.popup ? ` — ${escapeHtml(m.popup)}` : ''}</li>`).join('')}</ul>`
            : '<div class="muted">No map markers.</div>';
        return `<div class="widget-map" data-widget-map-config="${escapeHtml(JSON.stringify({ tiles_url: component.tiles_url, markers }))}">${list}</div>`;
    }
    if (type === 'calendar') {
        const items = Array.isArray(component.items) ? component.items : (Array.isArray(getPath(data, component.path || '', [])) ? getPath(data, component.path || '', []) : []);
        if (!items.length) return '<div class="muted">No calendar entries.</div>';
        const rows = items.map((item) => `<li class="widget-calendar-row"><strong>${escapeHtml(item.label || '—')}</strong>${item.start ? ` <span class="muted">${escapeHtml(item.start)}${item.end ? ' → ' + escapeHtml(item.end) : ''}</span>` : ''}${item.row ? ` <em>${escapeHtml(item.row)}</em>` : ''}</li>`).join('');
        return `<div class="widget-calendar"><ul class="widget-calendar-list">${rows}</ul></div>`;
    }
    if (type === 'kanban') {
        const columns = Array.isArray(component.columns) ? component.columns : [];
        if (!columns.length) return '<div class="muted">Kanban has no columns.</div>';
        const rawMoveRoute = component.on_move?.route || '';
        const moveRoute = cleanExtensionRoute(rawMoveRoute) ? rawMoveRoute : '';
        const cardsByCol = new Map();
        for (const col of columns) cardsByCol.set(col.id || col.label, []);
        const cardsList = Array.isArray(component.cards) ? component.cards : (Array.isArray(getPath(data, component.path || '', [])) ? getPath(data, component.path || '', []) : []);
        for (const card of cardsList) {
            const colKey = card.column || card.col || columns[0]?.id || columns[0]?.label;
            if (!cardsByCol.has(colKey)) cardsByCol.set(colKey, []);
            cardsByCol.get(colKey).push(card);
        }
        const colHtml = columns.map((col) => {
            const colKey = col.id || col.label;
            const cards = cardsByCol.get(colKey) || [];
            return `<div class="widget-kanban-col" data-widget-kanban-col="${escapeHtml(colKey)}">
                <div class="widget-kanban-col-head"><strong>${escapeHtml(col.label || colKey)}</strong></div>
                ${cards.map((c, idx) => `<div class="widget-kanban-card" draggable="true" data-widget-kanban-card="${escapeHtml(c.id || `${colKey}-${idx}`)}">${escapeHtml(c.label || c.title || '—')}</div>`).join('')}
            </div>`;
        }).join('');
        return `<div class="widget-kanban" data-widget-kanban-idx="${escapeHtml(componentKey)}" data-widget-kanban-route="${escapeHtml(moveRoute || '')}">${colHtml}</div>`;
    }
    if (type === 'subscription') {
        const children = Array.isArray(component.render) ? component.render : [];
        if (!children.length) return '';
        return `<div class="widget-subscription-render">${children.map((child, idx) => {
            if (!child || typeof child !== 'object') return '';
            const normalized = {
                ...child,
                target: child.target || target,
            };
            return renderDataComponent(tab, normalized, state, status, componentState, `${componentKey}:subscription:${idx}`);
        }).join('')}</div>`;
    }
    return '';
}

const widgetDisposers = new Map();
const widgetMessageHandlers = new Set();
const widgetSessionState = new Map();
let widgetsWsBridgeBound = false;

function boundedNumber(value, fallback, min, max) {
    const parsed = Number(value);
    const safe = Number.isFinite(parsed) ? parsed : fallback;
    return Math.max(min, Math.min(safe, max));
}

async function callWidgetRoute(tab, spec, values, signal) {
    const method = String(spec.method || 'GET').toUpperCase();
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values || {})) {
        params.set(key, String(value ?? ''));
    }
    const noBody = method === 'GET' || method === 'HEAD';
    const url = extensionRoutePath(tab.skill, spec.route || spec.api_route, noBody ? params : null);
    if (!url) throw new Error('invalid widget route');
    const init = noBody
        ? { method, signal }
        : {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(values || {}),
            signal,
        };
    const resp = await apiFetch(url, init);
    const contentType = resp.headers.get('content-type') || '';
    const data = contentType.includes('application/json')
        ? await resp.json().catch(() => ({}))
        : { text: await resp.text() };
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
}

async function mountDeclarativeWidget(mount, tab, render) {
    const components = Array.isArray(render.components) ? render.components : [];
    const persistenceKey = tab.key || `${tab.skill}:${tab.tab_id}`;
    const saved = widgetSessionState.get(persistenceKey) || {};
    const state = { ...(saved.state || {}) };
    const status = { ...(saved.status || {}) };
    const formValues = { ...(saved.formValues || {}) };
    const componentState = { ...(saved.componentState || {}) };
    const timers = new Set();
    const controllers = new Set();
    const chartInstances = new Set();
    const eventSources = new Map();
    const activePolls = new Set();
    const activeJobs = new Set();
    const autoStarted = new Set();
    const messageHandlers = new Set();
    const subscribed = new Set();
    let disposed = false;

    const downloadWidgetFile = async (url, filename) => {
        const resolvedUrl = new URL(url, window.location.origin);
        const expectedPrefix = extensionRoutePrefix(tab.skill);
        if (resolvedUrl.origin !== window.location.origin || !resolvedUrl.pathname.startsWith(expectedPrefix)) {
            throw new Error('download URL is outside this widget extension');
        }
        const safeName = filenameFromWidgetUrl(resolvedUrl.toString(), filename || 'download');
        await downloadViaHostBridge(resolvedUrl.pathname + resolvedUrl.search, safeName, { fetchOptions: { credentials: 'include' } });
    };

    const schedule = (fn, delay) => {
        if (disposed) return null;
        const timer = setTimeout(() => {
            timers.delete(timer);
            fn();
        }, delay);
        timers.add(timer);
        return timer;
    };
    const dispose = () => {
        widgetSessionState.set(persistenceKey, {
            state: { ...state },
            status: { ...status },
            formValues: { ...formValues },
            componentState: { ...componentState },
        });
        disposed = true;
        controllers.forEach((controller) => controller.abort());
        controllers.clear();
        chartInstances.forEach((chart) => chart.destroy());
        chartInstances.clear();
        eventSources.forEach((source) => source.close());
        eventSources.clear();
        timers.forEach((timer) => clearTimeout(timer));
        timers.clear();
        activePolls.clear();
        activeJobs.clear();
        messageHandlers.forEach((handler) => widgetMessageHandlers.delete(handler));
        messageHandlers.clear();
        subscribed.clear();
    };
    const callRoute = async (spec, values) => {
        if (disposed) throw new Error('widget disposed');
        const controller = new AbortController();
        controllers.add(controller);
        try {
            return await callWidgetRoute(tab, spec, values, controller.signal);
        } finally {
            controllers.delete(controller);
        }
    };
    const rememberFormValues = () => {
        mount.querySelectorAll('[data-widget-form]').forEach((form) => {
            const idx = form.dataset.widgetForm;
            formValues[idx] = formValues[idx] || {};
            const spec = components[Number(idx)] || {};
            for (const field of spec.fields || []) {
                formValues[idx][field.name] = fieldValue(form, field);
            }
        });
    };
    const startPoll = (idx) => {
        if (disposed || activePolls.has(idx)) return;
        const spec = components[Number(idx)] || {};
        const target = spec.target || 'result';
        const maxTicks = boundedNumber(spec.max_ticks, 20, 1, 100);
        const intervalMs = boundedNumber(spec.interval_ms, 2000, 1000, 30000);
        let ticks = 0;
        activePolls.add(idx);
        const poll = async () => {
            if (disposed) return;
            ticks += 1;
            status[target] = 'loading';
            renderAll();
            try {
                state[target] = await callRoute(spec, {});
                if (disposed) return;
                status[target] = 'success';
            } catch (err) {
                state[target] = { error: err.message || String(err) };
                status[target] = 'error';
            }
            const stopValue = getPath(state[target], spec.stop_path || '', undefined);
            if (ticks < maxTicks && String(stopValue) !== String(spec.stop_value ?? 'done')) {
                schedule(poll, intervalMs);
            } else {
                activePolls.delete(idx);
            }
            renderAll();
        };
        poll();
    };
    const startJobPoll = (idx, jobId) => {
        if (disposed || !jobId || activeJobs.has(idx)) return;
        const spec = components[Number(idx)] || {};
        const target = spec.target || 'result';
        const statusRoute = spec.status_route || spec.job_status_route || 'status';
        const intervalMs = boundedNumber(spec.interval_ms, 2000, 1000, 30000);
        const maxTicks = boundedNumber(spec.max_ticks, 240, 1, 1000);
        let ticks = 0;
        activeJobs.add(idx);
        componentState[`job:${idx}`] = { job_id: jobId, status_route: statusRoute };
        const pollJob = async () => {
            if (disposed) return;
            ticks += 1;
            try {
                const data = await callRoute({ route: statusRoute, method: 'GET' }, { job_id: jobId });
                if (disposed) return;
                const currentStatus = String(data.status || data.state || '').toLowerCase();
                if (currentStatus === 'done' || currentStatus === 'succeeded' || currentStatus === 'success') {
                    state[target] = data.result && typeof data.result === 'object' ? data.result : data;
                    status[target] = 'success';
                    delete componentState[`job:${idx}`];
                    activeJobs.delete(idx);
                    renderAll();
                    return;
                }
                if (currentStatus === 'error' || currentStatus === 'failed') {
                    state[target] = { error: data.error || 'job failed' };
                    status[target] = 'error';
                    delete componentState[`job:${idx}`];
                    activeJobs.delete(idx);
                    renderAll();
                    return;
                }
                state[target] = {
                    ...(state[target] || {}),
                    job_id: jobId,
                    progress: data.progress,
                    message: data.message,
                };
                status[target] = 'loading';
                renderAll();
                if (ticks < maxTicks) {
                    schedule(pollJob, intervalMs);
                } else {
                    state[target] = { error: 'job timed out waiting for result' };
                    status[target] = 'error';
                    delete componentState[`job:${idx}`];
                    activeJobs.delete(idx);
                    renderAll();
                }
            } catch (err) {
                state[target] = { error: err.message || String(err) };
                status[target] = 'error';
                delete componentState[`job:${idx}`];
                activeJobs.delete(idx);
                renderAll();
            }
        };
        pollJob();
    };
    const renderAll = () => {
        if (disposed) return;
        rememberFormValues();
        widgetSessionState.set(persistenceKey, {
            state: { ...state },
            status: { ...status },
            formValues: { ...formValues },
            componentState: { ...componentState },
        });
        chartInstances.forEach((chart) => chart.destroy());
        chartInstances.clear();
        mount.innerHTML = components.map((component, idx) => {
            const type = String(component.type || '');
            if (type === 'form') {
                const fields = (component.fields || [])
                    .map((field) => renderField(field, formValues[idx] || {}))
                    .join('');
                return `<form class="widget-form" data-widget-form="${idx}">${component.title ? `<h4>${escapeHtml(component.title)}</h4>` : ''}${fields}<button class="btn btn-primary" type="submit">${escapeHtml(component.submit_label || 'Submit')}</button></form>`;
            }
            if (type === 'action') {
                return `<button class="btn btn-default" data-widget-action="${idx}">${escapeHtml(component.label || 'Run')}</button>`;
            }
            if (type === 'poll') {
                return `<button class="btn btn-default" data-widget-poll="${idx}">${escapeHtml(component.label || 'Start polling')}</button>`;
            }
            return renderDataComponent(tab, component, state, status, componentState, String(idx));
        }).join('');
        mount.querySelectorAll('[data-widget-form]').forEach((form) => {
            form.addEventListener('submit', async (event) => {
                event.preventDefault();
                const spec = components[Number(form.dataset.widgetForm)] || {};
                const target = spec.target || 'result';
                const values = {};
                for (const field of spec.fields || []) values[field.name] = fieldValue(form, field);
                status[target] = 'loading';
                renderAll();
                try {
                    const data = await callRoute(spec, values);
                    if (disposed) return;
                    if (spec.job === true || spec.mode === 'job') {
                        const jobId = data.job_id || data.id;
                        if (!jobId) throw new Error('job response missing job_id');
                        state[target] = { job_id: jobId, message: data.message || 'Job started.' };
                        status[target] = 'loading';
                        startJobPoll(Number(form.dataset.widgetForm), jobId);
                    } else {
                        state[target] = data;
                        status[target] = 'success';
                    }
                } catch (err) {
                    state[target] = { error: err.message || String(err) };
                    status[target] = 'error';
                }
                renderAll();
            });
        });
        mount.querySelectorAll('[data-widget-action]').forEach((button) => {
            button.addEventListener('click', async () => {
                const spec = components[Number(button.dataset.widgetAction)] || {};
                const target = spec.target || 'result';
                status[target] = 'loading';
                renderAll();
                try {
                    const data = await callRoute(spec, spec.body || {});
                    if (disposed) return;
                    if (spec.job === true || spec.mode === 'job') {
                        const jobId = data.job_id || data.id;
                        if (!jobId) throw new Error('job response missing job_id');
                        state[target] = { job_id: jobId, message: data.message || 'Job started.' };
                        status[target] = 'loading';
                        startJobPoll(Number(button.dataset.widgetAction), jobId);
                    } else {
                        state[target] = data;
                        status[target] = 'success';
                    }
                } catch (err) {
                    state[target] = { error: err.message || String(err) };
                    status[target] = 'error';
                }
                renderAll();
            });
        });
        mount.querySelectorAll('[data-widget-poll]').forEach((button) => {
            button.addEventListener('click', () => {
                startPoll(Number(button.dataset.widgetPoll));
            });
        });
        mount.querySelectorAll('[data-widget-tab-key]').forEach((button) => {
            button.addEventListener('click', () => {
                componentState[button.dataset.widgetTabKey] = Number(button.dataset.widgetTabIdx || 0);
                renderAll();
            });
        });
        mount.querySelectorAll('[data-widget-download-url]').forEach((button) => {
            button.addEventListener('click', async (event) => {
                event.preventDefault();
                button.disabled = true;
                try {
                    await downloadWidgetFile(button.dataset.widgetDownloadUrl || '', button.dataset.widgetDownloadFilename || 'download');
                } catch (err) {
                    state.download = { error: err.message || String(err) };
                    status.download = 'error';
                } finally {
                    button.disabled = false;
                }
            });
        });
        mount.querySelectorAll('[data-widget-kanban]').forEach((board) => {
            const idx = Number(board.dataset.widgetKanbanIdx || 0);
            const spec = components[idx] || {};
            let draggedCardId = '';
            board.querySelectorAll('[data-widget-kanban-card]').forEach((card) => {
                card.addEventListener('dragstart', (event) => {
                    draggedCardId = card.dataset.widgetKanbanCard || '';
                    if (event.dataTransfer) {
                        event.dataTransfer.effectAllowed = 'move';
                        event.dataTransfer.setData('text/plain', draggedCardId);
                    }
                });
            });
            board.querySelectorAll('[data-widget-kanban-col]').forEach((column) => {
                column.addEventListener('dragover', (event) => {
                    if (!board.dataset.widgetKanbanRoute) return;
                    event.preventDefault();
                    if (event.dataTransfer) event.dataTransfer.dropEffect = 'move';
                });
                column.addEventListener('drop', async (event) => {
                    if (!board.dataset.widgetKanbanRoute) return;
                    event.preventDefault();
                    const cardId = event.dataTransfer?.getData('text/plain') || draggedCardId;
                    const columnId = column.dataset.widgetKanbanCol || '';
                    if (!cardId || !columnId) return;
                    const target = spec.target || 'result';
                    status[target] = 'loading';
                    renderAll();
                    try {
                        const response = await callRoute(
                            { route: board.dataset.widgetKanbanRoute, method: spec.on_move?.method || 'POST' },
                            { card_id: cardId, column_id: columnId },
                        );
                        if (disposed) return;
                        state[target] = response;
                        status[target] = 'success';
                    } catch (err) {
                        state[target] = { error: err.message || String(err) };
                        status[target] = 'error';
                    }
                    renderAll();
                });
            });
        });
        mount.querySelectorAll('[data-widget-chart-config]').forEach((canvas) => {
            if (typeof Chart === 'undefined') return;
            try {
                const config = JSON.parse(canvas.dataset.widgetChartConfig || '{}');
                chartInstances.add(new Chart(canvas, config));
            } catch (err) {
                console.warn('widgets: chart render failed', err);
            }
        });
        components.forEach((component, idx) => {
            if (String(component.type || '') === 'poll' && component.auto_start === true && !autoStarted.has(idx)) {
                autoStarted.add(idx);
                queueMicrotask(() => startPoll(idx));
            }
        });
        components.forEach((component, idx) => {
            if (!(component.job === true || component.mode === 'job')) return;
            const savedJob = componentState[`job:${idx}`];
            const jobId = savedJob && savedJob.job_id;
            if (jobId && status[component.target || 'result'] === 'loading') {
                queueMicrotask(() => startJobPoll(idx, jobId));
            }
        });
        components.forEach((component, idx) => {
            if (String(component.type || '') !== 'stream' || eventSources.has(idx)) return;
            const url = extensionRoutePath(tab.skill, component.route || component.api_route, new URLSearchParams());
            if (!url || typeof EventSource === 'undefined') return;
            const target = component.target || 'result';
            const source = new EventSource(url);
            eventSources.set(idx, source);
            status[target] = 'loading';
            source.onmessage = (event) => {
                if (disposed) return;
                try {
                    state[target] = JSON.parse(event.data);
                } catch {
                    state[target] = { text: event.data || '' };
                }
                status[target] = 'success';
                renderAll();
            };
            source.onerror = () => {
                if (disposed) return;
                status[target] = 'error';
                renderAll();
            };
        });
        components.forEach((component, idx) => {
            if (String(component.type || '') !== 'subscription' || subscribed.has(idx)) return;
            const event = String(component.event || component.message_type || '').trim();
            const prefix = String(tab.ws_prefix || '').trim();
            if (!event || !prefix) return;
            const expectedType = `${prefix}${event}`;
            const target = component.target || 'result';
            const handler = (msg) => {
                if (disposed || msg?.type !== expectedType) return;
                state[target] = msg.data || {};
                status[target] = 'success';
                renderAll();
            };
            subscribed.add(idx);
            messageHandlers.add(handler);
            widgetMessageHandlers.add(handler);
        });
    };
    renderAll();
    return dispose;
}

async function mountTab(card, tab) {
    const mount = card.querySelector('[data-widget-mount]');
    const render = tab.render || {};
    if (!mount) return;
    if (render.kind === 'iframe' && render.route) {
        const src = extensionRoutePath(tab.skill, render.route);
        if (!src) throw new Error('invalid widget iframe route');
        mount.innerHTML = `<iframe class="widgets-frame" sandbox="" src="${src}"></iframe>`;
        return;
    }
    if (render.kind === 'declarative') {
        return mountDeclarativeWidget(mount, tab, render);
    }
    if (render.kind === 'module' && render.entry) {
        // Reviewed JS runs in an opaque iframe; parent fetch bridge only allows
        // this skill's extension route prefix, preserving route IO without cookies.
        const entryName = String(render.entry).replace(/[^A-Za-z0-9._-]/g, '');
        const entryUrl = `${extensionRoutePrefix(tab.skill)}module/${encodeURIComponent(entryName)}`;
        const resp = await apiFetch(entryUrl, { cache: 'no-store' });
        const moduleSource = await resp.text();
        if (!resp.ok) {
            mount.innerHTML = `<div class="skills-load-error">module load failed: ${escapeHtml(moduleSource || `HTTP ${resp.status}`)}</div>`;
            return;
        }
        const expectedPrefix = extensionRoutePrefix(tab.skill);
        const nonce = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
        const csp = [
            "default-src 'none'",
            "script-src 'unsafe-inline'",
            "style-src 'unsafe-inline'",
            "img-src data:",
        ].join('; ');
        const escapeScript = (value) => String(value || '')
            .replace(/<\/script/gi, '<\\/script')
            .replace(/<!--/g, '<\\!--');
        const bridge = `
            (() => {
                const nonce = ${JSON.stringify(nonce)};
                let seq = 0;
                const pending = new Map();
                window.addEventListener('message', (event) => {
                    const msg = event.data || {};
                    if (msg.type !== 'ouro-widget-fetch-result' || msg.nonce !== nonce) return;
                    const item = pending.get(msg.id);
                    if (!item) return;
                    pending.delete(msg.id);
                    if (msg.error) {
                        item.reject(new Error(msg.error));
                        return;
                    }
                    item.resolve(new Response(msg.body || '', {
                        status: msg.status || 200,
                        headers: msg.headers || {},
                    }));
                });
                window.fetch = (url, init = {}) => {
                    const id = ++seq;
                    return new Promise((resolve, reject) => {
                        pending.set(id, { resolve, reject });
                        window.parent.postMessage({
                            type: 'ouro-widget-fetch',
                            nonce,
                            id,
                            url: String(url || ''),
                            init: {
                                method: init.method || 'GET',
                                headers: init.headers || {},
                                body: init.body || null,
                            },
                        }, '*');
                    });
                };
                window.OuroborosWidget = { fetch: window.fetch };
            })();
        `;
        const srcdoc = `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="${csp}"></head><body><div id="root"></div><script>${bridge}</script><script>${escapeScript(moduleSource)}</script></body></html>`;
        mount.innerHTML = `<iframe class="widgets-frame" sandbox="allow-scripts" srcdoc="${escapeHtml(srcdoc)}"></iframe>`;
        const iframe = mount.querySelector('iframe');
        const onMessage = async (event) => {
            if (event.source !== iframe.contentWindow) return;
            const msg = event.data || {};
            if (msg.type !== 'ouro-widget-fetch' || msg.nonce !== nonce) return;
            try {
                const parsed = new URL(String(msg.url || ''), window.location.origin);
                if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith(expectedPrefix)) {
                    throw new Error('module widget fetch outside extension route prefix');
                }
                const r = await apiFetch(parsed.pathname + parsed.search, {
                    method: String(msg.init?.method || 'GET').toUpperCase(),
                    headers: msg.init?.headers || {},
                    body: msg.init?.body || undefined,
                    credentials: 'same-origin',
                });
                const body = await r.text();
                iframe.contentWindow?.postMessage({
                    type: 'ouro-widget-fetch-result',
                    nonce,
                    id: msg.id,
                    status: r.status,
                    headers: { 'content-type': r.headers.get('content-type') || '' },
                    body,
                }, '*');
            } catch (err) {
                iframe.contentWindow?.postMessage({
                    type: 'ouro-widget-fetch-result',
                    nonce,
                    id: msg.id,
                    error: err.message || String(err),
                }, '*');
            }
        };
        window.addEventListener('message', onMessage);
        return () => window.removeEventListener('message', onMessage);
    }
    mount.innerHTML = `<div class="muted">Widget render kind <code>${escapeHtml(render.kind || 'unknown')}</code> is not supported yet.</div>`;
    return null;
}

function disposeMountedWidgets() {
    widgetDisposers.forEach((dispose) => {
        try {
            dispose();
        } catch (err) {
            console.warn('widgets: dispose failed', err);
        }
    });
    widgetDisposers.clear();
}

async function mountTrackedTab(card, tab) {
    const key = tab.key || `${tab.skill}:${tab.tab_id}`;
    const existing = widgetDisposers.get(key);
    if (existing) {
        existing();
        widgetDisposers.delete(key);
    }
    const dispose = await mountTab(card, tab);
    if (typeof dispose === 'function') {
        widgetDisposers.set(key, dispose);
        return;
    }
}

export function initWidgets(ctx = {}) {
    const page = document.createElement('div');
    page.innerHTML = pageTemplate();
    document.getElementById('content').appendChild(page.firstElementChild);
    const list = document.getElementById('widgets-list');
    const refreshBtn = document.getElementById('widgets-refresh');
    let renderGeneration = 0;
    let widgetsVisible = false;
    let widgetsMounted = false;
    let pendingWidgetKey = '';
    // Last good payload keeps revisits and slow refreshes from blanking the page.
    let lastTabs = null;
    if (ctx.ws && !widgetsWsBridgeBound) {
        widgetsWsBridgeBound = true;
        ctx.ws.on('message', (msg) => {
            widgetMessageHandlers.forEach((handler) => handler(msg));
        });
    }

    function focusWidget(key = '') {
        const targetKey = String(key || pendingWidgetKey || '').trim();
        if (!targetKey) {
            list.classList.remove('widgets-solo-mode');
            return false;
        }
        const card = list.querySelector(`[data-widget-key="${CSS.escape(targetKey)}"]`);
        if (!card) return false;
        list.querySelectorAll('.widgets-card.is-focused-from-sidebar').forEach((item) => {
            item.classList.remove('is-focused-from-sidebar');
        });
        card.classList.add('is-focused-from-sidebar');
        list.classList.add('widgets-solo-mode');
        card.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
        pendingWidgetKey = '';
        return true;
    }

    async function render(force = false) {
        const generation = ++renderGeneration;
        widgetsVisible = true;
        if (widgetsMounted && !force) {
            focusWidget();
            return;
        }
        if (!pendingWidgetKey) list.classList.remove('widgets-solo-mode');
        refreshBtn.disabled = true;
        refreshBtn.classList.add('is-loading');
        disposeMountedWidgets();
        if (lastTabs) {
            renderShell(list, lastTabs);
            applyWidgetsLayout(list);
        } else {
            list.innerHTML = '<div class="muted">Загрузка виджетов…</div>';
        }
        try {
            const data = await apiClient.extensions();
            if (!widgetsVisible || generation !== renderGeneration) return;
            const tabs = Array.isArray(data.live?.ui_tabs) ? data.live.ui_tabs : [];
            lastTabs = tabs;
            window.dispatchEvent(new CustomEvent('ouro:widgets-updated', { detail: { tabs } }));
            renderShell(list, tabs);
            applyWidgetsLayout(list);
            widgetsMounted = true;
            for (const tab of tabs) {
                if (!widgetsVisible || generation !== renderGeneration) return;
                const key = tab.key || `${tab.skill}:${tab.tab_id}`;
                const card = list.querySelector(`[data-widget-key="${CSS.escape(key)}"]`);
                if (!card) continue;
                try {
                    await mountTrackedTab(card, tab);
                    applyWidgetsLayout(list);
                } catch (err) {
                    const mount = card.querySelector('[data-widget-mount]');
                    if (mount) mount.innerHTML = `<div class="skills-load-error">widget failed: ${escapeHtml(err.message || err)}</div>`;
                    applyWidgetsLayout(list);
                }
            }
            applyWidgetsLayout(list);
            focusWidget();
        } catch (err) {
            if (!widgetsVisible || generation !== renderGeneration) return;
            // Preserve cached widgets on transient fetch errors.
            if (!lastTabs) {
                list.innerHTML = `<div class="skills-load-error">Failed to load widgets: ${escapeHtml(err.message || err)}</div>`;
            }
            widgetsMounted = false;
        } finally {
            if (widgetsVisible && generation === renderGeneration) {
                refreshBtn.disabled = false;
                refreshBtn.classList.remove('is-loading');
            }
        }
    }

    refreshBtn.addEventListener('click', () => render(true));
    window.addEventListener('ouro:page-shown', (event) => {
        if (event.detail?.page === 'widgets') {
            render();
        } else {
            // Hide stops stale paints; next render reuses lastTabs for instant repaint.
            widgetsVisible = false;
            widgetsMounted = false;
            disposeMountedWidgets();
        }
    });
    window.addEventListener('ouro:widget-open', (event) => {
        pendingWidgetKey = String(event.detail?.key || '');
        widgetsVisible = true;
        if (!focusWidget()) render();
    });
}

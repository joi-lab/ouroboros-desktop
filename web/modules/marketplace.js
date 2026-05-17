/**
 * Ouroboros ClawHub Marketplace UI (v4.50).
 *
 * Renders inside the ``Marketplace`` sub-tab of the Skills page.
 * Talks to ``/api/marketplace/clawhub/*`` and the existing
 * ``/api/skills/<name>/{toggle,review}`` endpoints. Uses the same
 * design-system primitives (``.btn``, ``.skills-badge``, ``.muted``,
 * ``.field-note``) as the rest of the app so dark/light theme parity
 * is automatic.
 */

import {
    getPending,
    getPendingBySlug,
    lifecycleCardClassFor,
    lifecycleSpinnerFor,
    setPending,
    startLifecyclePoller,
} from './lifecycle_card.js';
import { openConfirmDialog } from './confirm_dialog.js';
import {
    boundedText,
    emitSkillLifecycle,
    escapeHtmlAttr as escapeHtml,
    fetchJson,
    grantReady,
    isRateLimitError,
    renderSkillRepairPrompt,
    safeExternalHrefAttr,
} from './utils.js';

function installErrorCopy(message) {
    return isRateLimitError(message)
        ? `${message} Click Install again later to retry.`
        : message;
}

const safeExternalUrl = safeExternalHrefAttr;


function formatNumber(value) {
    const num = Number(value);
    if (!Number.isFinite(num) || num <= 0) return '—';
    if (num >= 1_000_000) return (num / 1_000_000).toFixed(1) + 'M';
    if (num >= 1_000) return (num / 1_000).toFixed(1) + 'k';
    return String(num);
}


const MARKETPLACE_SEARCH_LIMIT = 16;


function controlsTemplate() {
    return `
        <div class="marketplace-controls">
            <input type="search" id="mp-query" class="marketplace-search"
                   placeholder="Search ClawHub skills by name or summary…" autocomplete="off">
            <button class="btn btn-primary" data-mp-search>Search</button>
        </div>
        <div class="marketplace-filters">
            <label class="marketplace-filter-toggle">
                <input type="checkbox" id="mp-only-official">
                <span class="marketplace-filter-track" aria-hidden="true"></span>
                <span>Official only</span>
            </label>
        </div>
    `;
}


function paneTemplate({ includeControls = true } = {}) {
    return `
        <div class="marketplace-shell">
            ${includeControls ? controlsTemplate() : ''}
            <div id="mp-status" class="muted marketplace-status"></div>
            <div id="mp-results" class="marketplace-results"></div>
            <div id="mp-pagination" class="marketplace-pagination" hidden></div>
        </div>
    `;
}


function statusBadgeForReview(status) {
    const tone = status === 'clean' ? 'ok'
        : status === 'warnings' ? 'warn'
        : status === 'blockers' ? 'danger'
        : 'muted';
    return `<span class="skills-badge skills-badge-${tone}">${escapeHtml(status || 'pending')}</span>`;
}

function reviewReady(installed) {
    if (installed?.review_gate && typeof installed.review_gate.executable_review === 'boolean') {
        return installed.review_gate.executable_review && !installed?.review_stale;
    }
    return ['clean', 'warnings'].includes(installed?.review_status) && !installed?.review_stale;
}

function reviewStatusTone(status) {
    return ['clean', 'warnings'].includes(status) ? 'ok' : 'warn';
}

function reviewActionTone(status, error = '') {
    if (error) return 'danger';
    if (status === 'clean') return 'ok';
    if (status === 'blockers') return 'danger';
    return 'warn';
}

function hasInstalledUiTab(installed) {
    return installed?.has_ui_tab === true;
}

function topReviewFinding(installed) {
    const findings = Array.isArray(installed?.review_findings) ? installed.review_findings : [];
    if (!findings.length) return '';
    const first = findings[0] || {};
    const label = first.item || first.check || first.title || 'finding';
    const verdict = first.verdict || first.severity || '';
    const reason = first.reason || first.message || '';
    return `${verdict ? `${verdict} ` : ''}${label}: ${reason}`.trim();
}

function lifecycleFor(summary, installed, pending) {
    if (pending) {
        if (pending.failed === true) {
            return {
                tone: pending.tone || 'danger',
                label: pending.label || 'Failed',
                hint: pending.message || '',
                action: pending.retry_action || '',
                button: pending.retry_label || 'Retry',
                disabled: !pending.retry_action,
            };
        }
        return {
            tone: pending.tone || 'warn',
            label: pending.label || 'Working',
            hint: pending.message || '',
            action: '',
            button: pending.label || 'Working...',
            disabled: true,
        };
    }
    if (!installed) {
        return {
            tone: 'muted',
            label: 'Not installed',
            hint: 'Install runs the adapter and starts security review automatically.',
            action: 'install',
            button: 'Install',
        };
    }
    if (installed.load_error) {
        return {
            tone: 'danger',
            label: 'Install needs fix',
            hint: installed.load_error,
            action: 'fix',
            button: 'Repair',
        };
    }
    if (installed.review_status === 'blockers' && !reviewReady(installed)) {
        const finding = topReviewFinding(installed);
        return {
            tone: 'danger',
            label: 'Review blockers',
            hint: finding || 'Review has blocker findings; ask Ouroboros to repair the skill payload.',
            action: 'fix',
            button: 'Repair',
        };
    }
    if (!reviewReady(installed)) {
        const finding = topReviewFinding(installed);
        return {
            tone: 'warn',
            label: installed.review_stale ? 'Review stale' : `Review ${installed.review_status || 'pending'}`,
            hint: finding || 'Review must pass before this skill can run.',
            action: 'review',
            button: installed.review_stale ? 'Re-review' : 'Review',
        };
    }
    if (!grantReady(installed)) {
        const missing = installed.grants?.missing_keys || [];
        return {
            tone: 'warn',
            label: 'Needs key grant',
            hint: missing.length ? `Missing: ${missing.join(', ')}` : 'Human key grant required.',
            action: 'grant',
            button: 'Grant',
        };
    }
    if (!installed.enabled) {
        return {
            tone: 'ok',
            label: 'Ready',
            hint: 'Fresh executable review. Turn it on when you want the skill available.',
            action: 'enable',
            button: 'Enable',
        };
    }
    if (installed.type === 'extension' && hasInstalledUiTab(installed)) {
        return {
            tone: 'ok',
            label: 'Enabled',
            hint: 'Extension skills expose tools/routes and may add Widgets after loading.',
            action: 'widgets',
            button: 'Open widgets',
        };
    }
    if (installed.type === 'extension') {
        return {
            tone: 'ok',
            label: 'Enabled',
            hint: 'Extension is active. This skill does not expose a widget.',
            action: 'disable',
            button: 'Disable',
        };
    }
    return {
        tone: 'ok',
        label: 'Enabled',
        hint: 'Skill is enabled.',
        action: 'disable',
        button: 'Disable',
    };
}

function buildHealPrompt(installed, summary) {
    const findings = Array.isArray(installed?.review_findings) ? installed.review_findings : [];
    const diagnostics = {
        name: installed?.name || installed?.provenance?.sanitized_name || '',
        slug: summary?.slug || installed?.provenance?.slug || '',
        source: 'clawhub',
        payload_root: installed?.payload_root || '',
        type: installed?.type || 'unknown',
        review_status: installed?.review_status || 'pending',
        review_stale: Boolean(installed?.review_stale),
        load_error: boundedText(installed?.load_error || 'none', 2000),
        review_findings: findings.slice(0, 12).map((finding) => ({
            item: boundedText(finding.item || finding.check || finding.title || 'finding', 200),
            verdict: boundedText(finding.verdict || finding.severity || '', 80),
            reason: boundedText(finding.reason || finding.message || JSON.stringify(finding), 1200),
        })),
    };
    return renderSkillRepairPrompt(
        'Repair the ClawHub skill selected in the Marketplace UI.',
        JSON.stringify(diagnostics, null, 2),
    );
}


function summaryCard(summary, installedMap, isPlugin) {
    const slug = summary.slug;
    const pending = getPending(slug);
    const installed = installedMap.get(slug);
    const installedAtVersion = installed?.provenance?.version || installed?.version || '';
    const isInstalled = !!installed;
    const updateAvailable = isInstalled
        && summary.latest_version
        && installedAtVersion
        && summary.latest_version !== installedAtVersion;
    const downloads = formatNumber(summary.stats?.downloads);
    const stars = formatNumber(summary.stats?.stars);
    const license = summary.license || 'no-license';
    const homepageHref = safeExternalUrl(summary.homepage);
    const description = summary.summary || summary.description || '';
    const officialBadge = summary.badges?.official
        ? '<span class="skills-badge skills-badge-ok">official</span>'
        : '';
    const reviewBadge = isInstalled ? statusBadgeForReview(installed.review_status) : '';
    const lifecycle = lifecycleFor(summary, installed, pending);
    const lifecycleChip = '';
    const lifecycleHint = lifecycle.hint
        ? `<div class="marketplace-card-state-hint">${escapeHtml(lifecycle.hint)}</div>`
        : '';
    const workingIndicator = lifecycleSpinnerFor(pending);
    const primaryButton = isPlugin
        ? `<button class="btn btn-default" disabled title="OpenClaw Node/TypeScript plugins are not installable in Ouroboros. Use a Python port or MCP bridge.">Plugin</button>`
        : `<button class="btn btn-primary marketplace-next-action"
                   data-mp-action="${escapeHtml(lifecycle.action)}"
                   data-slug="${escapeHtml(slug)}"
                   ${lifecycle.disabled || !lifecycle.action ? 'disabled' : ''}>${escapeHtml(lifecycle.button)}</button>`;
    const secondaryButtons = isPlugin
        ? ''
        : isInstalled
            ? `
                ${updateAvailable ? `<button class="btn btn-default" data-mp-update="${escapeHtml(slug)}">Update</button>` : ''}
                ${installed.enabled && installed.type === 'extension' ? `<button class="btn btn-default" data-mp-action="disable" data-slug="${escapeHtml(slug)}">Disable</button>` : ''}
                <button class="btn btn-default" data-mp-uninstall="${escapeHtml(slug)}" data-name="${escapeHtml(installed.name || '')}">Uninstall</button>
            `
            : '';
    const buttons = `
        <div class="marketplace-primary-action">${primaryButton}</div>
        <div class="marketplace-secondary-actions">${secondaryButtons}</div>
    `;
    const cardClass = lifecycleCardClassFor(pending);
    const pluginBadge = isPlugin
        ? '<span class="skills-badge skills-badge-danger">plugin unsupported</span>'
        : '';
    const installedBadge = isInstalled
        ? `<span class="skills-badge skills-badge-ok">installed v${escapeHtml(installedAtVersion || summary.latest_version)}</span>`
        : '';
    const updateBadge = updateAvailable
        ? `<span class="skills-badge skills-badge-warn">update v${escapeHtml(summary.latest_version)}</span>`
        : '';
    const buttonsHtml = buttons;
    const badgesHtml = `
        ${officialBadge}
        ${pluginBadge}
        ${installedBadge}
        ${updateBadge}
        ${reviewBadge}
        ${lifecycleChip}
    `;
    return `
        <div class="${cardClass}" data-slug="${escapeHtml(slug)}">
            <div class="marketplace-card-head">
                <div class="marketplace-card-title">
                    <strong>${escapeHtml(summary.display_name || slug)}</strong>
                    <span class="muted">${escapeHtml(slug)} · v${escapeHtml(summary.latest_version || '—')}</span>
                </div>
                <div class="marketplace-card-badges">
                    ${badgesHtml}
                </div>
            </div>
            <div class="marketplace-card-body">${escapeHtml(description)}</div>
            <div class="marketplace-card-state marketplace-state-${lifecycle.tone}">
                <strong>${workingIndicator}${escapeHtml(lifecycle.label)}</strong>
                ${lifecycleHint}
            </div>
            <div class="marketplace-card-meta muted">
                <span>downloads: ${downloads}</span>
                <span>stars: ${stars}</span>
                <span>license: ${escapeHtml(license)}</span>
                ${homepageHref ? `<a href="${homepageHref}" target="_blank" rel="noopener noreferrer">homepage</a>` : ''}
                ${(summary.os || []).length ? `<span>os: ${(summary.os || []).map((o) => escapeHtml(o)).join(', ')}</span>` : ''}
            </div>
            <div class="marketplace-card-actions">${buttonsHtml}</div>
        </div>
    `;
}


function renderResults(host, summaries, installedMap, registryCount, diagnostics) {
    if (!summaries.length) {
        const query = String(diagnostics?.query || '').trim();
        const official = Boolean(diagnostics?.official);
        const mode = query ? 'matching your search' : 'in the marketplace browse list';
        const officialText = official ? ' official' : '';
        if (registryCount > 0) {
            host.innerHTML = `<div class="muted">No installable${officialText} skills found ${mode}.</div>`;
        } else {
            const attempts = Array.isArray(diagnostics?.attempts) && diagnostics.attempts.length
                ? `<details class="marketplace-debug"><summary>Registry diagnostics</summary><pre>${escapeHtml(JSON.stringify(diagnostics.attempts, null, 2))}</pre></details>`
                : '';
            host.innerHTML = `
                <div class="muted">
                    No installable${officialText} skills found ${mode}.
                </div>
                ${attempts}
            `;
        }
        return;
    }
    host.innerHTML = summaries
        .map((s) => summaryCard(s, installedMap, !!s.is_plugin))
        .join('');
}


function renderPagination(host, { query, limit, count, cursor, hasPrevious, nextCursor }) {
    const searchMode = Boolean(String(query || '').trim());
    if (searchMode || (!nextCursor && !hasPrevious)) {
        host.hidden = true;
        host.innerHTML = '';
        return;
    }
    host.hidden = false;
    host.innerHTML = `
        <button class="btn btn-default" data-mp-prev ${hasPrevious ? '' : 'disabled'}>Prev</button>
        <span class="muted">${cursor ? 'cursor page' : 'first page'} · ${count} shown</span>
        <button class="btn btn-default" data-mp-next ${nextCursor ? '' : 'disabled'}>Next</button>
    `;
}


function showStatus(host, message, tone) {
    const el = document.getElementById('mp-status');
    if (!el) return;
    el.dataset.tone = tone || '';
    el.textContent = message || '';
}


// ---------------------------------------------------------------------------
// Network helpers
// ---------------------------------------------------------------------------
//
// ``fetchJson`` is re-exported from ``utils.js`` and owned by
// ``api_client.js``. The contract: parsed body always returned; non-2xx
// throws ``Error`` with ``err.status`` (used here for 429 retry handling)
// and ``err.body``.


async function loadInstalled({ signal: externalSignal } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 3000);
    // v5.7.0: link an external (caller-owned) signal so refresh() can cancel
    // a stale loadInstalled() when a newer refresh starts in parallel.
    const onExternalAbort = () => controller.abort();
    if (externalSignal) {
        if (externalSignal.aborted) controller.abort();
        else externalSignal.addEventListener('abort', onExternalAbort, { once: true });
    }
    try {
        const [data, catalog] = await Promise.all([
            fetchJson('/api/marketplace/clawhub/installed', { signal: controller.signal }),
            fetchJson('/api/extensions', { signal: controller.signal }).catch(() => ({ skills: [] })),
        ]);
        const uiTabSkills = new Set(
            (catalog.live?.ui_tabs || [])
                .map((tab) => String(tab?.skill || tab?.skill_name || tab?.extension || ''))
                .filter(Boolean)
        );
        const byName = new Map();
        for (const skill of catalog.skills || []) {
            if (skill.name) byName.set(skill.name, { ...skill, has_ui_tab: uiTabSkills.has(skill.name) });
        }
        const map = new Map();
        for (const skill of data.skills || []) {
            const merged = { ...skill, ...(byName.get(skill.name) || {}) };
            const provSlug = skill.provenance?.slug;
            if (provSlug) map.set(provSlug, merged);
        }
        return map;
    } catch (err) {
        if (err?.name !== 'AbortError') {
            console.warn('marketplace: installed lookup failed', err);
        }
        return new Map();
    } finally {
        clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener('abort', onExternalAbort);
    }
}


async function runSearch(state, { signal } = {}) {
    const params = new URLSearchParams();
    const query = String(state.query || '').trim();
    if (query) params.set('q', query);
    params.set('limit', String(query ? MARKETPLACE_SEARCH_LIMIT : state.limit));
    if (!query && state.cursor) params.set('cursor', state.cursor);
    if (state.onlyOfficial) params.set('official', '1');
    return fetchJson(`/api/marketplace/clawhub/search?${params.toString()}`, { signal });
}


// ---------------------------------------------------------------------------
// Public init
// ---------------------------------------------------------------------------


export function initMarketplace(pane, controlsHost = null) {
    pane.innerHTML = paneTemplate({ includeControls: !controlsHost });
    if (controlsHost) {
        controlsHost.innerHTML = controlsTemplate();
    }

    const state = {
        query: '',
        limit: 25,
        onlyOfficial: false,
        results: [],
        installedMap: new Map(),
        cursor: '',
        cursorHistory: [],
        nextCursor: '',
        registryPath: 'packages',
        registryAttempts: [],
    };

    const controlsRoot = controlsHost || pane;
    const queryInput = controlsRoot.querySelector('#mp-query');
    const onlyOfficial = controlsRoot.querySelector('#mp-only-official');
    const searchBtn = controlsRoot.querySelector('[data-mp-search]');
    const resultsHost = pane.querySelector('#mp-results');
    const paginationHost = pane.querySelector('#mp-pagination');

    let debounceTimer = null;
    // v5.7.0: search race control. ``activeController`` is the AbortController
    // for the in-flight request; the next refresh() aborts it before kicking
    // off a new fetch. ``refreshToken`` is bumped on each refresh; after the
    // awaits a stale request bails out before touching state/DOM, so a slow
    // older response cannot overwrite a fresh newer render.
    let activeController = null;
    let refreshToken = 0;

    function syncControlsForMode() {
        const searchMode = Boolean(String(state.query || '').trim());
        onlyOfficial.title = searchMode
            ? 'Filters enriched search results to skills marked official.'
            : '';
    }

    async function refresh() {
        syncControlsForMode();
        const query = String(state.query || '').trim();
        showStatus(pane, query ? `Searching for "${query}"…` : 'Browsing ClawHub…', 'muted');
        // Cancel any prior in-flight refresh and stake a new token.
        if (activeController) {
            try { activeController.abort(); } catch (_) { /* ignore */ }
        }
        const myController = new AbortController();
        activeController = myController;
        const myToken = ++refreshToken;
        try {
            const [data, installedMap] = await Promise.all([
                runSearch(state, { signal: myController.signal }),
                loadInstalled({ signal: myController.signal }),
            ]);
            // Stale response — a newer refresh started; drop the result so
            // we never overwrite the fresher state with stale data.
            if (myToken !== refreshToken) return;
            state.results = data.results || [];
            state.installedMap = installedMap;
            state.installedMap.pendingBySlug = getPendingBySlug();
            state.nextCursor = data.next_cursor || '';
            state.registryPath = data.registry_path || 'packages';
            state.registryAttempts = data.registry_attempts || [];
            const registryWarnings = Array.isArray(data.registry_warnings) ? data.registry_warnings : [];
            renderResults(resultsHost, state.results, state.installedMap, state.results.length, {
                query,
                official: state.onlyOfficial,
                registryPath: state.registryPath,
                attempts: state.registryAttempts,
            });
            renderPagination(paginationHost, {
                query,
                limit: state.limit,
                count: state.results.length,
                cursor: state.cursor,
                hasPrevious: state.cursorHistory.length > 0,
                nextCursor: state.nextCursor,
            });
            const mode = query ? 'search' : 'browse';
            const official = state.onlyOfficial ? ' · official only' : '';
            if (registryWarnings.length) {
                showStatus(pane, `${state.results.length} skill${state.results.length === 1 ? '' : 's'} · ${mode}${official} · ${state.registryPath} · ${registryWarnings[0]}`, 'warn');
            } else {
                showStatus(pane, `${state.results.length} skill${state.results.length === 1 ? '' : 's'} · ${mode}${official} · ${state.registryPath}`, 'muted');
            }
        } catch (err) {
            // Stale-response abort: silent — a newer refresh is already in
            // flight (or just rendered) and owns the UI.
            if (err?.name === 'AbortError' || myToken !== refreshToken) return;
            const rawMessage = String(err?.body?.error || err?.message || err || '');
            const firstLine = rawMessage.split('\n').map((line) => line.trim()).filter(Boolean)[0] || 'Marketplace request failed';
            const timeout = /timed out|timeout/i.test(rawMessage);
            const message = timeout
                ? 'ClawHub did not respond in time. Try again, or search by name to narrow the request.'
                : firstLine.replace(/^Error:\s*/i, '');
            showStatus(pane, message, 'danger');
            resultsHost.innerHTML = `<div class="skills-load-error">${escapeHtml(message)}</div>`;
            paginationHost.hidden = true;
        } finally {
            if (activeController === myController) activeController = null;
        }
    }

    function scheduleRefresh(immediate) {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(refresh, immediate ? 0 : 300);
    }

    pane._marketplaceRefresh = () => scheduleRefresh(true);

    startLifecyclePoller(() => {
        state.installedMap.pendingBySlug = getPendingBySlug();
        renderResults(resultsHost, state.results, state.installedMap, state.results.length, {
            query: state.query,
            official: state.onlyOfficial,
            registryPath: state.registryPath,
            attempts: state.registryAttempts,
        });
    });

    async function toggleInstalledSkill(installed, enabled) {
        return fetchJson(`/api/skills/${encodeURIComponent(installed.name)}/toggle`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled }),
        });
    }

    async function runLifecycleAction(slug, action) {
        const summary = state.results.find((item) => item.slug === slug) || { slug };
        const installed = state.installedMap.get(slug);
        if (action === 'widgets') {
            document.querySelector('[data-page="widgets"]')?.click();
            return;
        }
        if (action === 'disable' && installed) {
            setPending(slug, { label: 'Turning off', tone: 'warn', message: 'Disabling skill…' });
            const result = await toggleInstalledSkill(installed, false);
            showStatus(pane, `${slug} disabled`, 'ok');
            emitSkillLifecycle('disable', installed.name, result);
            return;
        }
        if (action === 'enable' && installed) {
            setPending(slug, { label: 'Enabling', tone: 'warn', message: 'Turning skill on…' });
            const result = await toggleInstalledSkill(installed, true);
            showStatus(pane, `${slug} enabled`, 'ok');
            emitSkillLifecycle('enable', installed.name, result);
            return;
        }
        if (action === 'grant' && installed) {
            const keys = installed.grants?.missing_keys || installed.grants?.requested_keys || [];
            if (!keys.length) throw new Error('No grant keys reported for this skill.');
            const ok = await openConfirmDialog({
                title: `Grant access to ${installed.name}`,
                body: `Grant ${installed.name} access to these core settings keys?\n\n${keys.join('\n')}\n\nOnly grant access to reviewed skills you trust.`,
                confirmLabel: 'Grant access',
            });
            if (!ok) return;
            const bridge = window.pywebview?.api?.request_skill_key_grant;
            if (!bridge) {
                throw new Error('Skill key grants require the desktop launcher confirmation bridge.');
            }
            setPending(slug, { label: 'Granting', tone: 'warn', message: 'Waiting for human confirmation…' });
            const result = await bridge(installed.name, keys);
            if (!result?.ok) throw new Error(result?.error || 'Skill key grant was cancelled.');
            showStatus(pane, `${slug} grant saved`, 'ok');
            emitSkillLifecycle('grant', installed.name, result);
            return;
        }
        if (action === 'fix' && installed) {
            const ok = await openConfirmDialog({
                title: `Repair ${installed.name || slug}`,
                body: `Start a repair task for ${installed.name || slug}? Ouroboros will edit only the skill payload and re-run review.`,
                confirmLabel: 'Start repair',
            });
            if (!ok) return;
            setPending(slug, { label: 'Repair requested', tone: 'warn', message: 'Queueing repair task…' });
            await fetchJson('/api/command', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cmd: buildHealPrompt(installed, summary),
                    task_constraint: { mode: 'skill_repair', skill_name: installed.name || '', payload_root: installed.payload_root || '', allow_enable: false, allow_review: true },
                    visible_text: `Repair task queued for ${installed.name || slug}. Ouroboros will inspect the skill payload and re-run review.`,
                    visible_task_id: `skill_repair_${installed.name || slug}`,
                }),
            });
            showStatus(pane, `${slug}: repair task queued`, 'ok');
            emitSkillLifecycle('repair', installed.name || slug);
            document.querySelector('.nav-btn[data-page="chat"]')?.click();
            return;
        }
        if (action === 'review' && installed) {
            setPending(slug, { label: 'Reviewing', tone: 'warn', message: 'Running skill review…' });
            const result = await fetchJson(`/api/skills/${encodeURIComponent(installed.name)}/review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            showStatus(
                pane,
                `${slug}: review ${result.status}${result.error ? ` — ${result.error}` : ''}`,
                reviewActionTone(result.status, result.error),
            );
            emitSkillLifecycle('review', installed.name, result);
            return;
        }
        if (action === 'update' && installed) {
            setPending(slug, {
                label: 'Updating',
                tone: 'warn',
                message: 'Updating skill…',
                target: installed.name,
            });
            const result = await fetchJson(`/api/marketplace/clawhub/update/${encodeURIComponent(installed.name)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (!result.ok) throw new Error(result.error || 'update failed');
            showStatus(pane, `Updated ${slug} — review ${result.review_status}`, reviewStatusTone(result.review_status));
            emitSkillLifecycle('update', installed.name, result);
            return;
        }
        if (action === 'install') {
            setPending(slug, { label: 'Installing', tone: 'warn', message: 'Downloading, adapting, and reviewing…' });
            const result = await fetchJson('/api/marketplace/clawhub/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slug, auto_review: true }),
            });
            if (!result.ok) throw new Error(result.error || 'install failed');
            const installedName = result.sanitized_name;
            const requestedGrants = result.provenance?.requested_key_grants || [];
            if (['clean', 'warnings'].includes(result.review_status) && installedName && !requestedGrants.length) {
                showStatus(pane, `Installed ${slug}; review passed. Enable it from the card when ready.`, 'ok');
            } else if (['clean', 'warnings'].includes(result.review_status) && requestedGrants.length) {
                showStatus(pane, `Installed ${slug}; grant required before enabling`, 'warn');
            } else if (result.review_error) {
                showStatus(pane, `Installed ${slug}; review could not finish: ${result.review_error}`, 'warn');
            } else {
                showStatus(pane, `Installed ${slug}; review ${result.review_status || 'pending'}`, reviewStatusTone(result.review_status));
            }
            emitSkillLifecycle('install', installedName || slug, result);
        }
    }

    queryInput.addEventListener('input', (event) => {
        state.query = event.target.value || '';
        state.cursor = '';
        state.cursorHistory = [];
        scheduleRefresh(false);
    });
    queryInput.addEventListener('keydown', (event) => {
        // v5.7.0: Enter triggers a search; without this, users instinctively
        // pressed Enter (no-op) and then clicked Search, which used to
        // create the typing-debounce + click race the user complained about.
        if (event.key === 'Enter') {
            event.preventDefault();
            scheduleRefresh(true);
        }
    });
    onlyOfficial.addEventListener('change', () => {
        state.onlyOfficial = onlyOfficial.checked;
        state.cursor = '';
        state.cursorHistory = [];
        scheduleRefresh(true);
    });
    searchBtn.addEventListener('click', () => {
        // v5.7.0: clear cursor history on explicit Search so a user that
        // paginated through browse mode and then types a query gets a
        // fresh cursorless first page (matching the input/checkbox flows).
        state.cursor = '';
        state.cursorHistory = [];
        scheduleRefresh(true);
    });

    paginationHost.addEventListener('click', (event) => {
        const prev = event.target.closest('[data-mp-prev]');
        const next = event.target.closest('[data-mp-next]');
        if (prev) {
            state.cursor = state.cursorHistory.pop() || '';
            scheduleRefresh(true);
        } else if (next) {
            if (state.nextCursor) {
                state.cursorHistory.push(state.cursor || '');
                state.cursor = state.nextCursor;
            }
            scheduleRefresh(true);
        }
    });

    resultsHost.addEventListener('click', async (event) => {
        const actionBtn = event.target.closest('[data-mp-action]');
        const updateBtn = event.target.closest('[data-mp-update]');
        const uninstallBtn = event.target.closest('[data-mp-uninstall]');
        if (actionBtn) {
            const slug = actionBtn.dataset.slug;
            const action = actionBtn.dataset.mpAction;
            if (!slug || !action) return;
            actionBtn.disabled = true;
            let failedMessage = '';
            try {
                await runLifecycleAction(slug, action);
            } catch (err) {
                failedMessage = action === 'install'
                    ? installErrorCopy(err.message || String(err))
                    : (err.message || String(err));
                const tone = action === 'install' && isRateLimitError(failedMessage) ? 'warn' : 'danger';
                showStatus(pane, `${slug}: ${failedMessage}`, tone);
                setPending(slug, {
                    label: `${action} failed`,
                    tone,
                    message: failedMessage,
                    failed: true,
                    retry_action: action,
                    retry_label: action === 'install' ? 'Retry install' : `Retry ${action}`,
                });
            } finally {
                if (!failedMessage) setPending(slug, null);
                actionBtn.disabled = false;
                // v5.7.0: funnel through scheduleRefresh so back-to-back
                // action completions coalesce into one refresh, sharing
                // the abort/token guards in refresh().
                if (!failedMessage) scheduleRefresh(true);
            }
            return;
        }
        if (updateBtn) {
            updateBtn.disabled = true;
            const slug = updateBtn.dataset.mpUpdate;
            const installed = state.installedMap.get(slug);
            const sanitized = installed?.name;
            if (!sanitized) {
                showStatus(pane, `Cannot update ${slug}: no provenance found`, 'danger');
                updateBtn.disabled = false;
                return;
            }
            // Optional: let the operator pick a non-latest target via
            // a small prompt. The summary already lists every published
            // version; we offer a freeform prompt seeded with the
            // registry latest. Empty / cancelled = skip; the install
            // path treats falsy version as "latest".
            const summary = state.results.find((s) => s.slug === slug);
            const latest = summary?.latest_version || '';
            const userVersion = window.prompt(
                `Update ${slug} to which version? Leave empty for latest (${latest || 'unknown'}).`,
                latest,
            );
            if (userVersion === null) {
                // operator cancelled
                updateBtn.disabled = false;
                return;
            }
            const targetVersion = (userVersion || '').trim();
            showStatus(pane, `Updating ${slug}${targetVersion ? ` → v${targetVersion}` : ' (latest)'}…`, 'muted');
            setPending(slug, {
                label: 'Updating',
                tone: 'warn',
                message: 'Updating skill…',
                target: sanitized,
            });
            try {
                const body = targetVersion ? { version: targetVersion } : {};
                const result = await fetchJson(`/api/marketplace/clawhub/update/${encodeURIComponent(sanitized)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                });
                if (!result.ok) {
                    throw new Error(result.error || 'update failed');
                } else {
                    showStatus(pane, `Updated ${slug} — review ${result.review_status}`, reviewStatusTone(result.review_status));
                    setPending(slug, null);
                    emitSkillLifecycle('update', sanitized, result);
                }
            } catch (err) {
                setPending(slug, {
                    label: 'Failed',
                    tone: 'danger',
                    message: err.message || String(err),
                    failed: true,
                    retry_action: 'update',
                    retry_label: 'Retry update',
                    target: sanitized,
                });
                showStatus(pane, `Update error: ${err.message}`, 'danger');
            } finally {
                updateBtn.disabled = false;
                scheduleRefresh(true);
            }
            return;
        }
        if (uninstallBtn) {
            const slug = uninstallBtn.dataset.mpUninstall;
            const sanitized = uninstallBtn.dataset.name;
            const ok = await openConfirmDialog({
                title: `Uninstall ${slug}`,
                body: `Uninstall ${slug}? This deletes data/skills/clawhub/${sanitized}/.`,
                confirmLabel: 'Uninstall',
                danger: true,
            });
            if (!ok) return;
            uninstallBtn.disabled = true;
            try {
                await fetchJson(`/api/marketplace/clawhub/uninstall/${encodeURIComponent(sanitized)}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({}),
                });
                showStatus(pane, `Uninstalled ${slug}`, 'ok');
                emitSkillLifecycle('uninstall', sanitized);
            } catch (err) {
                showStatus(pane, `Uninstall error: ${err.message}`, 'danger');
            } finally {
                uninstallBtn.disabled = false;
                scheduleRefresh(true);
            }
        }
    });

    refresh();
}

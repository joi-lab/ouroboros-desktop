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
        ? `${message} Попробуйте установить снова позже.`
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
                   placeholder="Поиск по названию или описанию" autocomplete="off">
            <button class="btn btn-primary" data-mp-search>Найти</button>
        </div>
        <div class="marketplace-filters">
            <label class="marketplace-filter-toggle">
                <input type="checkbox" id="mp-only-official">
                <span class="marketplace-filter-track" aria-hidden="true"></span>
                <span>Только официальные</span>
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
                label: pending.label || 'Ошибка',
                hint: pending.message || '',
                action: pending.retry_action || '',
                button: pending.retry_label || 'Повторить',
                disabled: !pending.retry_action,
            };
        }
        return {
            tone: pending.tone || 'warn',
            label: pending.label || 'Выполняется',
            hint: pending.message || '',
            action: '',
            button: pending.label || 'Выполняется…',
            disabled: true,
        };
    }
    if (!installed) {
        return {
            tone: 'muted',
            label: 'Не установлен',
            hint: 'Установка запускает адаптер и проверку безопасности автоматически.',
            action: 'install',
            button: 'Установить',
        };
    }
    if (installed.load_error) {
        return {
            tone: 'danger',
            label: 'Ошибка установки',
            hint: installed.load_error,
            action: 'fix',
            button: 'Исправить',
        };
    }
    if (installed.review_status === 'blockers' && !reviewReady(installed)) {
        const finding = topReviewFinding(installed);
        return {
            tone: 'danger',
            label: 'Блокеры проверки',
            hint: finding || 'Проверка обнаружила блокеры; попросите Ouroboros исправить навык.',
            action: 'fix',
            button: 'Исправить',
        };
    }
    if (!reviewReady(installed)) {
        const finding = topReviewFinding(installed);
        return {
            tone: 'warn',
            label: installed.review_stale ? 'Проверка устарела' : `Проверка ${installed.review_status || 'pending'}`,
            hint: finding || 'Проверка должна пройти, прежде чем навык сможет работать.',
            action: 'review',
            button: installed.review_stale ? 'Перепроверить' : 'Проверить',
        };
    }
    if (!grantReady(installed)) {
        const missing = installed.grants?.missing_keys || [];
        return {
            tone: 'warn',
            label: 'Требуется разрешение',
            hint: missing.length ? `Отсутствуют: ${missing.join(', ')}` : 'Требуется подтверждение доступа.',
            action: 'grant',
            button: 'Разрешить',
        };
    }
    if (!installed.enabled) {
        return {
            tone: 'ok',
            label: 'Готов',
            hint: 'Проверка пройдена. Включите навык, когда он понадобится.',
            action: 'enable',
            button: 'Включить',
        };
    }
    if (installed.type === 'extension' && hasInstalledUiTab(installed)) {
        return {
            tone: 'ok',
            label: 'Включён',
            hint: 'Расширение предоставляет инструменты и может добавлять виджеты.',
            action: 'widgets',
            button: 'Открыть виджеты',
        };
    }
    if (installed.type === 'extension') {
        return {
            tone: 'ok',
            label: 'Включён',
            hint: 'Расширение активно. Этот навык не добавляет виджет.',
            action: 'disable',
            button: 'Отключить',
        };
    }
    return {
        tone: 'ok',
        label: 'Включён',
        hint: 'Навык включён.',
        action: 'disable',
        button: 'Отключить',
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
        ? '<span class="skills-badge skills-badge-ok">Официальный</span>'
        : '';
    const reviewBadge = isInstalled ? statusBadgeForReview(installed.review_status) : '';
    const lifecycle = lifecycleFor(summary, installed, pending);
    const lifecycleChip = '';
    const lifecycleHint = lifecycle.hint
        ? `<div class="marketplace-card-state-hint">${escapeHtml(lifecycle.hint)}</div>`
        : '';
    const workingIndicator = lifecycleSpinnerFor(pending);
    const detailsBtn = `<button class="btn btn-default" data-mp-detail="${escapeHtml(slug)}">Подробнее</button>`;
    const primaryButton = isPlugin
        ? `<button class="btn btn-default" disabled title="OpenClaw Node/TypeScript plugins are not installable in Ouroboros. Use a Python port or MCP bridge.">Plugin</button>`
        : isInstalled
            ? `<button class="btn btn-primary" disabled>Установлен v${escapeHtml(installedAtVersion || summary.latest_version || '—')}</button>`
            : `<button class="btn btn-default marketplace-next-action"
                       data-mp-action="${escapeHtml(lifecycle.action)}"
                       data-slug="${escapeHtml(slug)}"
                       ${lifecycle.disabled || !lifecycle.action ? 'disabled' : ''}>${escapeHtml(lifecycle.button)}</button>`;
    const lifecycleActionBtn = isInstalled && lifecycle.action && lifecycle.action !== 'disable'
        ? `<button class="btn btn-default marketplace-next-action"
                   data-mp-action="${escapeHtml(lifecycle.action)}"
                   data-slug="${escapeHtml(slug)}"
                   ${lifecycle.disabled ? 'disabled' : ''}>${escapeHtml(lifecycle.button)}</button>`
        : '';
    const secondaryButtons = isPlugin
        ? detailsBtn
        : isInstalled
            ? `
                ${lifecycleActionBtn}
                ${updateAvailable ? `<button class="btn btn-default" data-mp-update="${escapeHtml(slug)}">Обновить</button>` : ''}
                <button class="btn btn-default" data-mp-uninstall="${escapeHtml(slug)}" data-name="${escapeHtml(installed.name || '')}">Удалить</button>
                ${detailsBtn}
            `
            : detailsBtn;
    const buttons = `
        <div class="marketplace-primary-action">${primaryButton}</div>
        <div class="marketplace-secondary-actions">${secondaryButtons}</div>
    `;
    const cardClass = lifecycleCardClassFor(pending);
    const pluginBadge = isPlugin
        ? '<span class="skills-badge skills-badge-danger">plugin unsupported</span>'
        : '';
    const updateBadge = updateAvailable
        ? `<span class="skills-badge skills-badge-warn">update v${escapeHtml(summary.latest_version)}</span>`
        : '';
    const buttonsHtml = buttons;
    const badgesHtml = `
        ${officialBadge}
        ${pluginBadge}
        ${updateBadge}
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
            <div class="marketplace-card-bottom">
                <div class="marketplace-card-state marketplace-state-${lifecycle.tone}">
                    <strong>${workingIndicator}${escapeHtml(lifecycle.label)}</strong>
                    ${lifecycleHint}
                </div>
                <div class="marketplace-card-actions">${buttonsHtml}</div>
            </div>
        </div>
    `;
}


function renderResults(host, summaries, installedMap, registryCount, diagnostics) {
    if (!summaries.length) {
        const query = String(diagnostics?.query || '').trim();
        const official = Boolean(diagnostics?.official);
        const mode = query ? 'по вашему запросу' : 'в каталоге';
        const officialText = official ? ' официальных' : '';
        const failedAttempts = Array.isArray(diagnostics?.attempts)
            ? diagnostics.attempts.filter(a => !a.ok)
            : [];
        const debugBlock = failedAttempts.length
            ? `<details class="marketplace-debug"><summary>Диагностика реестра</summary><pre>${escapeHtml(JSON.stringify(failedAttempts, null, 2))}</pre></details>`
            : '';
        host.innerHTML = `<div class="muted">Устанавливаемых${officialText} навыков не найдено ${mode}.</div>${debugBlock}`;
        return;
    }
    host.innerHTML = summaries
        .map((s) => summaryCard(s, installedMap, !!s.is_plugin))
        .join('');
}


function renderPagination(host, { query, limit, count, cursor, hasPrevious, nextCursor }) {
    const searchMode = Boolean(String(query || '').trim());
    if (searchMode || (!nextCursor && !hasPrevious) || count === 0) {
        host.hidden = true;
        host.innerHTML = '';
        return;
    }
    host.hidden = false;
    host.innerHTML = `
        <button class="btn btn-default" data-mp-prev ${hasPrevious ? '' : 'disabled'}>Назад</button>
        <span class="muted">${cursor ? 'следующая страница' : 'первая страница'} · ${count} показано</span>
        <button class="btn btn-default" data-mp-next ${nextCursor ? '' : 'disabled'}>Далее</button>
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
            ? 'Фильтрует результаты поиска по официальным навыкам.'
            : '';
    }

    async function refresh() {
        syncControlsForMode();
        const query = String(state.query || '').trim();
        showStatus(pane, query ? `Поиск «${query}»…` : 'Загрузка ClawHub…', 'muted');
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
            const mode = query ? 'поиск' : 'обзор';
            const official = state.onlyOfficial ? ' · только официальные' : '';
            if (registryWarnings.length) {
                showStatus(pane, `${state.results.length} навык${state.results.length === 1 ? '' : (state.results.length < 5 ? 'а' : 'ов')} · ${mode}${official} · ${state.registryPath} · ${registryWarnings[0]}`, 'warn');
            } else {
                showStatus(pane, `${state.results.length} навык${state.results.length === 1 ? '' : (state.results.length < 5 ? 'а' : 'ов')} · ${mode}${official} · ${state.registryPath}`, 'muted');
            }
        } catch (err) {
            // Stale-response abort: silent — a newer refresh is already in
            // flight (or just rendered) and owns the UI.
            if (err?.name === 'AbortError' || myToken !== refreshToken) return;
            const rawMessage = String(err?.body?.error || err?.message || err || '');
            const firstLine = rawMessage.split('\n').map((line) => line.trim()).filter(Boolean)[0] || 'Ошибка запроса к маркетплейсу';
            const timeout = /timed out|timeout/i.test(rawMessage);
            const message = timeout
                ? 'ClawHub не ответил вовремя. Попробуйте ещё раз или уточните запрос.'
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
            setPending(slug, { label: 'Отключение', tone: 'warn', message: 'Отключение навыка…' });
            const result = await toggleInstalledSkill(installed, false);
            showStatus(pane, `${slug}: отключён`, 'ok');
            emitSkillLifecycle('disable', installed.name, result);
            return;
        }
        if (action === 'enable' && installed) {
            setPending(slug, { label: 'Включение', tone: 'warn', message: 'Включение навыка…' });
            const result = await toggleInstalledSkill(installed, true);
            showStatus(pane, `${slug}: включён`, 'ok');
            emitSkillLifecycle('enable', installed.name, result);
            return;
        }
        if (action === 'grant' && installed) {
            const keys = installed.grants?.missing_keys || installed.grants?.requested_keys || [];
            if (!keys.length) throw new Error('Навык не сообщил о ключах доступа.');
            const ok = await openConfirmDialog({
                title: `Разрешить доступ для ${installed.name}`,
                body: `Разрешить навыку ${installed.name} доступ к следующим ключам настроек?\n\n${keys.join('\n')}\n\nВыдавайте разрешения только проверенным навыкам.`,
                confirmLabel: 'Разрешить доступ',
            });
            if (!ok) return;
            const bridge = window.pywebview?.api?.request_skill_key_grant;
            if (!bridge) {
                throw new Error('Выдача разрешений требует десктопного лаунчера.');
            }
            setPending(slug, { label: 'Выдача разрешения', tone: 'warn', message: 'Ожидание подтверждения…' });
            const result = await bridge(installed.name, keys);
            if (!result?.ok) throw new Error(result?.error || 'Выдача разрешения отменена.');
            showStatus(pane, `${slug}: разрешение сохранено`, 'ok');
            emitSkillLifecycle('grant', installed.name, result);
            return;
        }
        if (action === 'fix' && installed) {
            const ok = await openConfirmDialog({
                title: `Исправить ${installed.name || slug}`,
                body: `Запустить задачу исправления для ${installed.name || slug}? Ouroboros изменит только payload навыка и повторит проверку.`,
                confirmLabel: 'Начать исправление',
            });
            if (!ok) return;
            setPending(slug, { label: 'Исправление запрошено', tone: 'warn', message: 'Постановка в очередь…' });
            await fetchJson('/api/command', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    cmd: buildHealPrompt(installed, summary),
                    task_constraint: { mode: 'skill_repair', skill_name: installed.name || '', payload_root: installed.payload_root || '', allow_enable: false, allow_review: true },
                    visible_text: `Задача исправления поставлена в очередь для ${installed.name || slug}. Ouroboros проверит payload и повторит проверку.`,
                    visible_task_id: `skill_repair_${installed.name || slug}`,
                }),
            });
            showStatus(pane, `${slug}: задача исправления поставлена в очередь`, 'ok');
            emitSkillLifecycle('repair', installed.name || slug);
            document.querySelector('.nav-btn[data-page="chat"]')?.click();
            return;
        }
        if (action === 'review' && installed) {
            setPending(slug, { label: 'Проверка', tone: 'warn', message: 'Запуск проверки навыка…' });
            const result = await fetchJson(`/api/skills/${encodeURIComponent(installed.name)}/review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            showStatus(
                pane,
                `${slug}: проверка ${result.status}${result.error ? ` — ${result.error}` : ''}`,
                reviewActionTone(result.status, result.error),
            );
            emitSkillLifecycle('review', installed.name, result);
            return;
        }
        if (action === 'update' && installed) {
            setPending(slug, {
                label: 'Обновление',
                tone: 'warn',
                message: 'Обновление навыка…',
                target: installed.name,
            });
            const result = await fetchJson(`/api/marketplace/clawhub/update/${encodeURIComponent(installed.name)}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            if (!result.ok) throw new Error(result.error || 'ошибка обновления');
            showStatus(pane, `${slug}: обновлён — проверка ${result.review_status}`, reviewStatusTone(result.review_status));
            emitSkillLifecycle('update', installed.name, result);
            return;
        }
        if (action === 'install') {
            setPending(slug, { label: 'Установка', tone: 'warn', message: 'Загрузка, адаптация и проверка…' });
            const result = await fetchJson('/api/marketplace/clawhub/install', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ slug, auto_review: true }),
            });
            if (!result.ok) throw new Error(result.error || 'ошибка установки');
            const installedName = result.sanitized_name;
            const requestedGrants = result.provenance?.requested_key_grants || [];
            if (['clean', 'warnings'].includes(result.review_status) && installedName && !requestedGrants.length) {
                showStatus(pane, `${slug}: установлен, проверка пройдена. Включите навык из карточки.`, 'ok');
            } else if (['clean', 'warnings'].includes(result.review_status) && requestedGrants.length) {
                showStatus(pane, `${slug}: установлен, необходимо разрешение перед включением`, 'warn');
            } else if (result.review_error) {
                showStatus(pane, `${slug}: установлен, проверка не завершена: ${result.review_error}`, 'warn');
            } else {
                showStatus(pane, `${slug}: установлен, проверка ${result.review_status || 'pending'}`, reviewStatusTone(result.review_status));
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
        const detailBtn = event.target.closest('[data-mp-detail]');
        if (detailBtn) {
            const slug = detailBtn.dataset.mpDetail;
            const summary = state.results?.find(s => s.slug === slug);
            if (!summary) return;
            const installed = state.installedMap?.get(slug);
            const desc = summary.summary || summary.description || '';
            const version = summary.latest_version || '—';
            const homepage = safeExternalUrl(summary.homepage);
            const license = summary.license || '—';
            const dl = formatNumber(summary.stats?.downloads);
            const stars = formatNumber(summary.stats?.stars);
            const isOfficial = summary.badges?.official;
            const backdrop = document.createElement('div');
            backdrop.className = 'marketplace-modal-backdrop';
            backdrop.innerHTML = `
                <div class="marketplace-modal" role="dialog" aria-modal="true" style="max-width:560px">
                    <div class="marketplace-modal-head">
                        <div>
                            <strong style="font-size:16px">${escapeHtml(summary.display_name || slug)}</strong>
                            <div style="font-size:13px;color:var(--text-secondary);margin-top:2px">${escapeHtml(slug)} · v${escapeHtml(version)}</div>
                        </div>
                        <button class="btn btn-default btn-sm" data-close>Закрыть</button>
                    </div>
                    <div class="marketplace-modal-body">
                        ${isOfficial ? `<div><span class="skills-badge skills-badge-ok" style="font-size:12px;font-weight:700">Официальный</span></div>` : ''}
                        ${desc ? `<p style="margin:0;line-height:1.6">${escapeHtml(desc)}</p>` : ''}
                        <table style="width:100%;font-size:13px;border-collapse:collapse">
                            ${license !== '—' ? `<tr><td style="padding:4px 0;color:var(--text-secondary);width:120px">Лицензия</td><td>${escapeHtml(license)}</td></tr>` : ''}
                            ${dl !== '—' ? `<tr><td style="padding:4px 0;color:var(--text-secondary)">Загрузки</td><td>${dl}</td></tr>` : ''}
                            ${stars !== '—' ? `<tr><td style="padding:4px 0;color:var(--text-secondary)">Звёзды</td><td>${stars}</td></tr>` : ''}
                            ${installed ? `<tr><td style="padding:4px 0;color:var(--text-secondary)">Установлен</td><td>v${escapeHtml(installed.provenance?.version || installed.version || version)}</td></tr>` : ''}
                        </table>
                        ${homepage ? `<a href="${homepage}" target="_blank" rel="noopener noreferrer" class="btn btn-default" style="align-self:flex-start">Открыть страницу</a>` : ''}
                    </div>
                </div>`;
            backdrop.addEventListener('click', (e) => {
                if (e.target === backdrop || e.target.closest('[data-close]')) backdrop.remove();
            });
            document.body.appendChild(backdrop);
            return;
        }
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
                    label: 'Ошибка',
                    tone,
                    message: failedMessage,
                    failed: true,
                    retry_action: action,
                    retry_label: action === 'install' ? 'Повторить установку' : 'Повторить',
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
                showStatus(pane, `Невозможно обновить ${slug}: данные о происхождении не найдены`, 'danger');
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
                `До какой версии обновить ${slug}? Оставьте пустым для последней (${latest || 'неизвестно'}).`,
                latest,
            );
            if (userVersion === null) {
                // operator cancelled
                updateBtn.disabled = false;
                return;
            }
            const targetVersion = (userVersion || '').trim();
            showStatus(pane, `Обновление ${slug}${targetVersion ? ` → v${targetVersion}` : ' (последняя)'}…`, 'muted');
            setPending(slug, {
                label: 'Обновление',
                tone: 'warn',
                message: 'Обновление навыка…',
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
                    showStatus(pane, `${slug}: обновлён — проверка ${result.review_status}`, reviewStatusTone(result.review_status));
                    setPending(slug, null);
                    emitSkillLifecycle('update', sanitized, result);
                }
            } catch (err) {
                setPending(slug, {
                    label: 'Ошибка',
                    tone: 'danger',
                    message: err.message || String(err),
                    failed: true,
                    retry_action: 'update',
                    retry_label: 'Повторить обновление',
                    target: sanitized,
                });
                showStatus(pane, `Ошибка обновления: ${err.message}`, 'danger');
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
                title: `Удалить ${slug}`,
                body: `Удалить ${slug}? Это удалит data/skills/clawhub/${sanitized}/.`,
                confirmLabel: 'Удалить',
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
                showStatus(pane, `${slug}: удалён`, 'ok');
                emitSkillLifecycle('uninstall', sanitized);
            } catch (err) {
                showStatus(pane, `Ошибка удаления: ${err.message}`, 'danger');
            } finally {
                uninstallBtn.disabled = false;
                scheduleRefresh(true);
            }
        }
    });

    refresh();
}
